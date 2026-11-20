"""A one-shot check must not count its own process as the daemon it is looking for.

The pattern of a plain daemon (no ``manager``, no ``bridge_config``) is matched with
``pgrep -f``. pgrep leaves out its own PID, but not the Python process that called it: when the
daemon's pattern also matches the command line of ``agentsmon status`` or of a one-shot
``agentsmon keepalive`` pass (for example a virtualenv path shared by agentsmon and the daemon),
that check saw itself and reported a daemon that is not running as up.

These tests run the real entry points as throwaway processes. The interpreter is started through a
symlink in a temporary directory whose name is a unique marker, so the scanner's own command line
carries the marker exactly as a shared virtualenv path would. HOME, config and state point into the
temporary directory, and every process started here is stopped at the end.
"""
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

from agentsmon import detect, keepalive

REPO = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('pgrep') and sys.platform != 'win32', 'needs pgrep')
class SelfMatch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='amlv-'))
        self.marker = 'am-liveness-' + uuid.uuid4().hex[:10]
        bindir = self.tmp / self.marker / 'bin'
        bindir.mkdir(parents=True)
        self.python = bindir / 'python'
        self.python.symlink_to(sys.executable)
        self.restarted = self.tmp / 'restarted'
        self.env = {**os.environ, 'HOME': str(self.tmp), 'PYTHONPATH': str(REPO),
                    'AGENTSMON_CONFIG': str(self.tmp / 'config.json'),
                    'AGENTSMON_STATE': str(self.tmp / 'state'), 'TMUX_TMPDIR': str(self.tmp)}
        self.env.pop('TMUX', None)
        self.procs = []

    def tearDown(self):
        for p in self.procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()
        deadline = time.time() + 5
        while self.marker in self.ps() and time.time() < deadline:
            time.sleep(0.1)
        left = self.ps()
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertNotIn(self.marker, left, 'a test process outlived the test')

    # helpers -------------------------------------------------------------------------------
    @staticmethod
    def ps():
        return subprocess.run(['ps', '-axo', 'command='], capture_output=True, text=True).stdout

    def configure(self, **daemon):
        spec = {'name': 'svc', 'pattern': self.marker,
                'restart': f'touch {self.restarted}', **daemon}
        (self.tmp / 'config.json').write_text(json.dumps(
            {'agents': [], 'daemons': [spec], 'keepalive': {'interval_seconds': 1}}))

    def cli(self, *args):
        return subprocess.run([str(self.python), '-m', 'agentsmon', *args], env=self.env, cwd=self.tmp,
                              capture_output=True, text=True, timeout=60)

    def spawn(self, code):
        """A process whose command line carries the marker (it runs the linked interpreter)."""
        p = subprocess.Popen([str(self.python), '-c', code], env=self.env, cwd=self.tmp,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                             start_new_session=True)
        self.procs.append(p)
        return p

    def daemon(self):
        p = self.spawn('import time; time.sleep(300)')
        deadline = time.time() + 5
        while str(p.pid) not in subprocess.run(['pgrep', '-f', '--', self.marker],
                                               capture_output=True, text=True).stdout.split():
            self.assertLess(time.time(), deadline, 'daemon did not start')
            time.sleep(0.05)
        return p

    def svc_line(self, out):
        return next(l for l in out.splitlines() if ' svc' in l)

    # the false positive --------------------------------------------------------------------
    def test_status_does_not_report_a_missing_daemon_up_because_of_its_own_process(self):
        self.configure()
        r = self.cli('status')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('🔴', self.svc_line(r.stdout))

    def test_keepalive_pass_restarts_a_daemon_that_only_the_pass_itself_matched(self):
        self.configure()
        r = self.cli('keepalive')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue(self.restarted.exists(), 'the missing daemon was not restarted')

    # real processes still count ------------------------------------------------------------
    def test_running_daemon_stays_up_and_is_not_restarted(self):
        self.configure()
        self.daemon()
        self.assertIn('🟢', self.svc_line(self.cli('status').stdout))
        r = self.cli('keepalive')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertFalse(self.restarted.exists(), 'a running daemon was restarted')

    def test_daemon_that_runs_the_check_itself_stays_up(self):
        """The real service is the parent of the check (it starts `agentsmon status`)."""
        self.configure()
        p = self.spawn('import subprocess, sys\n'
                       f'r = subprocess.run([{str(self.python)!r}, "-m", "agentsmon", "status"], '
                       'capture_output=True, text=True, timeout=60)\n'
                       'print(r.stdout, r.stderr)')
        out, _ = p.communicate(timeout=90)
        self.assertIn('🟢', self.svc_line(out))

    def test_long_running_process_calling_the_checks_in_process_stays_up(self):
        """A long-running daemon (e.g. one that loads agentsmon as a library) that calls
        status.render() and a single keepalive pass itself is the real service: still up, no
        restart."""
        self.configure()
        p = self.spawn('from agentsmon import keepalive, status\n'
                       'print(status.render(), flush=True)\n'
                       'keepalive.run()\n'
                       'print("PASS-DONE", flush=True)\n'
                       'import time; time.sleep(300)')
        lines = []
        deadline = time.time() + 60
        while 'PASS-DONE' not in lines and time.time() < deadline:
            line = p.stdout.readline()
            if not line:
                break
            lines.append(line.strip())
        self.assertIn('PASS-DONE', lines, lines)
        self.assertIn('🟢', self.svc_line('\n'.join(lines)))
        self.assertFalse(self.restarted.exists(), 'the running daemon was restarted')

    def test_child_of_the_checking_process_still_counts(self):
        """A matching child of the caller is a real process, not the caller."""
        self.daemon()        # a child of this test process, which is the caller here
        spec = {'name': 'svc', 'pattern': self.marker}
        self.assertTrue(detect.daemon_status([spec], ignore_pid=os.getpid())[0]['up'])

    def test_long_running_keepalive_loop_still_counts_its_own_process(self):
        """`keepalive --loop` is a long-running process that may itself be the configured
        service, so it keeps counting itself, as before: no restart while it runs."""
        self.configure()
        loop = subprocess.Popen([str(self.python), '-m', 'agentsmon', 'keepalive', '--loop'],
                                env=self.env, cwd=self.tmp, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, start_new_session=True)
        self.procs.append(loop)
        heartbeat = self.tmp / 'state' / 'keepalive_heartbeat'
        deadline = time.time() + 20
        passes = 0
        last = None
        while passes < 2 and time.time() < deadline:      # two completed passes
            stamp = heartbeat.stat().st_mtime_ns if heartbeat.exists() else None
            if stamp is not None and stamp != last:
                passes, last = passes + 1, stamp
            time.sleep(0.1)
        self.assertEqual(passes, 2, 'the loop did not complete two passes')
        self.assertIsNone(loop.poll())
        self.assertFalse(self.restarted.exists())


class IgnoredPidOnlyAffectsThePatternMatch(unittest.TestCase):
    """The new argument only removes one PID from the pgrep result; other branches are untouched."""

    def run_status(self, spec, pgrep_out, rc=0, ignore=4242):
        result = subprocess.CompletedProcess([], rc, stdout=pgrep_out, stderr='')
        from unittest.mock import patch
        with patch.object(detect, '_run', return_value=result) as run, \
             patch.object(detect, '_http_ok', return_value=False):
            return detect.daemon_status([spec], ignore_pid=ignore)[0], run

    def test_only_the_ignored_pid_matched(self):
        self.assertFalse(self.run_status({'name': 'x', 'pattern': 'p'}, '4242\n')[0]['up'])

    def test_another_pid_matched_too(self):
        self.assertTrue(self.run_status({'name': 'x', 'pattern': 'p'}, '4242\n17\n')[0]['up'])

    def test_pgrep_failure_is_unchanged(self):
        self.assertFalse(self.run_status({'name': 'x', 'pattern': 'p'}, '', rc=2)[0]['up'])

    def test_empty_pattern_is_unchanged(self):
        entry, run = self.run_status({'name': 'x', 'pattern': ''}, '')
        self.assertFalse(entry['up'])
        run.assert_not_called()

    def test_health_url_still_wins(self):
        from unittest.mock import patch
        with patch.object(detect, '_http_ok', return_value=True), \
             patch.object(detect, '_run', return_value=subprocess.CompletedProcess([], 0, '4242\n', '')):
            entry = detect.daemon_status([{'name': 'x', 'pattern': 'p', 'health_url': 'http://h'}],
                                         ignore_pid=4242)[0]
        self.assertEqual((entry['process_up'], entry['http_ok'], entry['up']), (False, True, True))

    def test_manager_and_bridge_branches_do_not_run_pgrep(self):
        from unittest.mock import patch
        from agentsmon import bridges, managed
        with patch.object(detect, '_run') as run, \
             patch.object(managed, 'active', return_value=True), \
             patch.object(bridges, 'running_pids', return_value=[4242]):
            out = detect.daemon_status([{'name': 'm', 'pattern': 'p', 'manager': {'type': 'systemd'}},
                                        {'name': 'b', 'pattern': 'p', 'bridge_config': '/c.json'}],
                                       ignore_pid=4242)
        self.assertEqual([e['up'] for e in out], [True, True])
        run.assert_not_called()


class Callers(unittest.TestCase):
    """Only the one-shot CLI processes leave their own PID out; library calls and the loop do not."""

    def test_cli_status_and_single_pass_ignore_themselves_loop_and_library_calls_do_not(self):
        from unittest.mock import patch
        from agentsmon import __main__ as cli, status
        seen = []

        def render(cfg=None, ignore_pid=None):
            seen.append(('render', ignore_pid))
            return ''

        def run(loop=False, ignore_pid=None):
            seen.append(('run', ignore_pid))
            return 0

        def fake(daemons, ignore_pid=None):
            seen.append(('daemon_status', ignore_pid))
            return []

        with patch.object(status, 'render', side_effect=render), \
             patch.object(keepalive, 'run', side_effect=run):
            cli.main(['status'])
            cli.main(['keepalive'])
            cli.main(['keepalive', '--loop'])
        with patch.object(detect, 'daemon_status', side_effect=fake):
            status.render({'agents': [], 'daemons': []})          # a library call
        self.assertEqual(seen, [('render', os.getpid()), ('run', os.getpid()), ('run', None),
                                ('daemon_status', None)])

    def test_run_hands_its_ignore_pid_to_tick(self):
        from unittest.mock import patch
        from agentsmon import config, service
        seen = []

        def tick(cfg, ignore_pid=None):
            seen.append(ignore_pid)
            return 0
        with tempfile.TemporaryDirectory() as d, \
             patch.object(config, 'state_dir', return_value=Path(d)), \
             patch.object(config, 'load', return_value={'agents': [], 'daemons': [], 'keepalive': {}}), \
             patch.object(service, 'refresh_launcher', return_value=False), \
             patch.object(keepalive, 'tick', side_effect=tick):
            keepalive.run(ignore_pid=4242)
            keepalive.run()
        self.assertEqual(seen, [4242, None])

    def test_tick_hands_its_ignore_pid_to_daemon_status(self):
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d, \
             patch.object(keepalive.config, 'state_dir', return_value=Path(d)), \
             patch.object(detect, 'daemon_status', return_value=[]) as status:
            keepalive.tick({'agents': [], 'daemons': []}, ignore_pid=4242)
            keepalive.tick({'agents': [], 'daemons': []})
        self.assertEqual([c.kwargs.get('ignore_pid') for c in status.call_args_list], [4242, None])


if __name__ == '__main__':
    unittest.main()
