"""A restart counts only when the agent process is seen running in its session afterwards.

These tests drive real, disposable processes: a private tmux server (its own TMUX_TMPDIR, with
TMUX unset so no outer session is touched) and small throwaway "agent" scripts with a unique
name. Nothing from a real installation is used, and every process is stopped at the end.
"""
import io
import os
from pathlib import Path
import shlex
import signal
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import redirect_stdout
from unittest.mock import patch

from agentsmon import __main__ as cli, config, dashboard, detect, keepalive

TMUX = shutil.which('tmux')


@unittest.skipUnless(TMUX and shutil.which('ps') and sys.platform != 'win32',
                     'needs tmux and ps')
class RestartConfirmation(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='amrc-'))
        self.marker = 'am-fake-agent-' + uuid.uuid4().hex[:10]
        env = {'TMUX_TMPDIR': str(self.tmp), 'HOME': str(self.tmp)}
        self.env = patch.dict(os.environ, env)
        self.env.start()
        os.environ.pop('TMUX', None)          # never talk to an outer tmux server
        self.state = patch.object(config, 'state_dir', return_value=self.tmp)
        self.state.start()
        self.window = patch.object(keepalive, 'CONFIRM_SECONDS', 4, create=True)
        self.window.start()
        self.foreign = []

    def tearDown(self):
        for p in self.foreign:
            os.killpg(p.pid, signal.SIGKILL)
            p.wait()
        # The private server only: TMUX_TMPDIR points into our temp dir and TMUX is unset.
        assert os.environ['TMUX_TMPDIR'] == str(self.tmp) and 'TMUX' not in os.environ
        subprocess.run([TMUX, 'kill-server'], capture_output=True, timeout=10)
        self.window.stop()
        self.state.stop()
        self.env.stop()
        deadline = time.time() + 5
        while self.marker in detect._run(['ps', '-axo', 'command=']).stdout and time.time() < deadline:
            time.sleep(0.1)
        left = detect._run(['ps', '-axo', 'command=']).stdout
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertNotIn(self.marker, left, 'a test process outlived the test')

    # helpers -------------------------------------------------------------------------------
    def script(self, body: str) -> str:
        path = self.tmp / self.marker
        path.write_text('#!/bin/sh\n' + body + '\n')
        path.chmod(0o755)
        return str(path)

    def long_agent(self) -> str:
        """An agent that keeps running; one process that carries the marker for its whole life."""
        return self.script(f"exec {shlex.quote(sys.executable)} -c 'import time; time.sleep(300)' "
                           + self.marker)

    def agent(self, restart: str, match=None, suffix='') -> dict:
        return {'name': 'rc-' + self.marker[-6:] + suffix, 'cwd': str(self.tmp), 'restart': restart,
                'match': self.marker if match is None else match}

    def tick(self, agent, cfg_extra=None):
        cfg = {'agents': [agent], 'keepalive': {}, **(cfg_extra or {})}
        with patch.object(keepalive, '_log') as log, \
             patch.object(keepalive, '_start', wraps=keepalive._start) as start:
            began = time.monotonic()
            n = keepalive.tick(cfg)
            took = time.monotonic() - began
        lines = [c.args[0] for c in log.call_args_list]
        return n, lines, start.call_count, took

    def pane_pid(self, name):
        pids = detect._pane_pids(name)
        self.assertEqual(len(pids), 1)
        return pids[0]

    # scenarios -----------------------------------------------------------------------------
    def test_started_agent_is_confirmed_by_its_process_in_the_session(self):
        a = self.agent(self.long_agent())
        n, lines, starts, _ = self.tick(a)
        self.assertEqual((n, starts), (1, 1))
        verdict = lines[-1]
        self.assertIn('restart confirmed', verdict)
        pid = int(verdict.split('pid ')[1].split()[0])
        procs, children = detect._proc_table()
        self.assertIn(pid, detect._subtree([self.pane_pid(a['name'])], children))
        self.assertIn(self.marker, procs[pid])

    def test_shell_without_agent_is_not_confirmed_and_not_relaunched_in_the_same_pass(self):
        a = self.agent('true')
        n, lines, starts, took = self.tick(a)
        self.assertEqual((n, starts), (0, 1))
        self.assertIn('NOT confirmed', lines[-1])
        self.assertIn(a['name'], [s['name'] for s in detect.tmux_sessions()])   # shell is there
        self.assertLess(took, 4 + 3)

    def test_missing_program_is_not_confirmed(self):
        a = self.agent(str(self.tmp / 'missing' / self.marker))
        n, lines, _, _ = self.tick(a)
        self.assertEqual(n, 0)
        self.assertIn('NOT confirmed', lines[-1])

    def test_program_that_exits_at_once_is_not_confirmed(self):
        a = self.agent(self.script('sleep 0.2; exit 1'))
        n, lines, _, _ = self.tick(a)
        self.assertEqual(n, 0)
        self.assertIn('NOT confirmed', lines[-1])

    def test_delayed_start_within_the_window_is_confirmed(self):
        a = self.agent('sleep 1; ' + self.long_agent())
        n, lines, _, _ = self.tick(a)
        self.assertEqual(n, 1)
        self.assertIn('restart confirmed', lines[-1])

    def test_start_later_than_the_window_times_out_as_not_confirmed(self):
        with patch.object(keepalive, 'CONFIRM_SECONDS', 1.5, create=True):
            a = self.agent('sleep 6; ' + self.long_agent())
            n, lines, starts, took = self.tick(a)
        self.assertEqual((n, starts), (0, 1))
        self.assertIn('NOT confirmed', lines[-1])
        self.assertLess(took, 1.5 + 3)

    def test_matching_process_outside_the_session_does_not_confirm(self):
        self.foreign.append(subprocess.Popen([self.long_agent()], start_new_session=True))
        a = self.agent('true')
        n, lines, _, _ = self.tick(a)
        self.assertEqual(n, 0)
        self.assertIn('NOT confirmed', lines[-1])

    def test_agent_without_match_keyword_cannot_be_confirmed(self):
        a = self.agent(self.long_agent(), match='')
        n, lines, _, took = self.tick(a)
        self.assertEqual(n, 0)
        self.assertIn("NOT confirmed – no 'match' keyword", lines[-1])
        self.assertLess(took, 3)              # nothing to wait for

    def test_failing_tmux_is_reported_as_a_failed_start(self):
        fake = self.tmp / 'tmux-fails'
        fake.write_text('#!/bin/sh\necho "no server" >&2\nexit 1\n')
        fake.chmod(0o755)
        a = self.agent(self.long_agent())
        n, lines, _, _ = self.tick(a, {'tmux_bin': str(fake)})
        self.assertEqual(n, 0)
        self.assertIn('restart failed – tmux new-session failed: no server', lines[-1])

    def tmux_failing_on(self, subcommand):
        fake = self.tmp / ('tmux-fails-' + subcommand)
        fake.write_text(f'#!/bin/sh\nif [ "$1" = {subcommand} ]; then echo "refused" >&2; exit 1; fi\n'
                        f'exec {shlex.quote(TMUX)} "$@"\n')
        fake.chmod(0o755)
        return str(fake)

    def test_failing_display_message_or_send_keys_is_a_failed_start(self):
        for step in ('display-message', 'send-keys'):
            with self.subTest(step=step):
                a = self.agent(self.long_agent(), suffix='-' + step[:4])
                n, lines, _, _ = self.tick(a, {'tmux_bin': self.tmux_failing_on(step)})
                self.assertEqual(n, 0)
                self.assertIn('restart failed – ', lines[-1])
                self.assertIn('refused', lines[-1])

    # a tmux client that gives up -------------------------------------------------------------
    def tmux_timing_out(self, subcommand, deliver):
        """A tmux whose client hangs on *subcommand* until it is killed by the timeout. With
        *deliver*, tmux first does the real work (the keys reach the pane), as when the server is
        slow to answer. Every call is logged, so a second send-keys would show."""
        calls = self.tmp / 'tmux-calls'
        real = shlex.quote(TMUX)
        work = f'{real} "$@"; ' if deliver else ''
        fake = self.tmp / f'tmux-hangs-{subcommand}-{int(deliver)}'
        fake.write_text(f'#!/bin/sh\necho "$1" >> {shlex.quote(str(calls))}\n'
                        f'if [ "$1" = {subcommand} ]; then {work}exec sleep 30; fi\n'
                        f'exec {real} "$@"\n')
        fake.chmod(0o755)
        return str(fake), calls

    def short_timeout_for(self, tmux_bin, subcommand):
        """Let the client of *subcommand* time out after 1 s instead of the production 10 s."""
        real_run = subprocess.run

        def run(args, **kw):
            if list(args[:2]) == [tmux_bin, subcommand]:
                kw['timeout'] = 1
            return real_run(args, **kw)
        return patch.object(keepalive.subprocess, 'run', run)

    def counted_agent(self):
        """Like long_agent, but every start of the agent is appended to a file."""
        starts = self.tmp / 'agent-starts'
        body = (f"echo start >> {shlex.quote(str(starts))}; exec {shlex.quote(sys.executable)} "
                f"-c 'import time; time.sleep(300)' " + self.marker)
        return self.script(body), starts

    def test_send_keys_timeout_after_delivery_confirms_the_running_agent(self):
        tmux_bin, calls = self.tmux_timing_out('send-keys', deliver=True)
        command, starts = self.counted_agent()
        with self.short_timeout_for(tmux_bin, 'send-keys'):
            n, lines, launches, _ = self.tick(self.agent(command), {'tmux_bin': tmux_bin})
        self.assertEqual((n, launches), (1, 1), lines)
        self.assertIn('restart confirmed', lines[-1])
        self.assertIn('send-keys timed out', lines[-1])
        self.assertEqual(calls.read_text().split().count('send-keys'), 1)   # typed once
        self.assertEqual(starts.read_text().split(), ['start'])             # started once

    def test_dashboard_reports_the_send_keys_timeout_with_the_unconfirmed_restart(self):
        tmux_bin, calls = self.tmux_timing_out('send-keys', deliver=True)
        with self.short_timeout_for(tmux_bin, 'send-keys'):
            ok, message = self.dashboard_restart(self.agent('true'), tmux_bin=tmux_bin)
        self.assertFalse(ok)
        self.assertIn('not confirmed', message)
        self.assertIn('send-keys timed out', message)
        self.assertEqual(calls.read_text().split().count('send-keys'), 1)

    def test_send_keys_timeout_without_a_running_agent_is_not_confirmed_rather_than_failed(self):
        for deliver, command in ((True, 'true'), (False, None)):
            with self.subTest(delivered=deliver):
                tmux_bin, calls = self.tmux_timing_out('send-keys', deliver)
                agent_cmd, starts = self.counted_agent()
                a = self.agent(command or agent_cmd, suffix=f'-{int(deliver)}')
                with self.short_timeout_for(tmux_bin, 'send-keys'):
                    n, lines, launches, _ = self.tick(a, {'tmux_bin': tmux_bin})
                self.assertEqual((n, launches), (0, 1), lines)
                self.assertIn('restart NOT confirmed', lines[-1])
                self.assertIn('send-keys timed out', lines[-1])
                self.assertEqual(calls.read_text().split().count('send-keys'), 1)
                self.assertFalse(starts.exists())       # nothing was started a second time
                calls.unlink()

    def test_timeout_before_the_command_is_sent_is_a_failed_start_that_sends_nothing(self):
        for step in ('new-session', 'display-message'):
            with self.subTest(step=step):
                tmux_bin, calls = self.tmux_timing_out(step, deliver=step == 'new-session')
                a = self.agent(self.long_agent(), suffix='-' + step[:3])
                with self.short_timeout_for(tmux_bin, step):
                    n, lines, _, _ = self.tick(a, {'tmux_bin': tmux_bin})
                self.assertEqual(n, 0)
                self.assertIn(f'restart failed – tmux {step} timed out', lines[-1])
                self.assertIn('launch command was not sent', lines[-1])
                self.assertNotIn('send-keys', calls.read_text().split())
                calls.unlink()

    def test_agent_already_running_in_the_pane_does_not_confirm_a_new_launch(self):
        a = self.agent('true')
        subprocess.run([TMUX, 'new-session', '-d', '-s', a['name']], check=True, timeout=10)
        subprocess.run([TMUX, 'send-keys', '-t', a['name'], self.long_agent(), 'Enter'],
                       check=True, timeout=10)
        deadline = time.time() + 5           # the agent runs as a child of the pane's shell
        while not keepalive._alive(a['name'], self.marker, {a['name']: {}},
                                   *reversed(detect._proc_table()))[1]:
            self.assertLess(time.time(), deadline, 'pre-existing agent did not start')
            time.sleep(0.1)
        launch = keepalive._confirm([keepalive._start(a, TMUX)])[0]
        self.assertEqual(launch['state'], 'unconfirmed', launch)

    def test_session_replaced_after_the_launch_does_not_confirm(self):
        a = self.agent('sleep 2; true')
        launch = keepalive._start(a, TMUX)
        self.assertEqual(launch['state'], 'sent', launch)
        subprocess.run([TMUX, 'kill-session', '-t', a['name']], check=True, timeout=10)
        subprocess.run([TMUX, 'new-session', '-d', '-s', a['name'], self.long_agent()],
                       check=True, timeout=10)
        launch = keepalive._confirm([launch])[0]
        self.assertEqual(launch['state'], 'unconfirmed', launch)

    def test_agent_that_replaces_the_shell_with_exec_is_confirmed(self):
        a = self.agent('exec ' + self.long_agent())
        n, lines, _, _ = self.tick(a)
        self.assertEqual(n, 1)
        self.assertIn('restart confirmed', lines[-1])
        self.assertIn(f"pid {self.pane_pid(a['name'])} ", lines[-1])

    def test_pane_moved_to_another_session_does_not_confirm(self):
        a = self.agent('sleep 2; ' + self.long_agent())
        launch = keepalive._start(a, TMUX)
        self.assertEqual(launch['state'], 'sent', launch)
        other = a['name'] + '-other'
        subprocess.run([TMUX, 'new-session', '-d', '-s', other], check=True, timeout=10)
        subprocess.run([TMUX, 'move-window', '-k', '-s', a['name'] + ':0', '-t', other + ':0'],
                       check=True, timeout=10)
        launch = keepalive._confirm([launch])[0]
        self.assertEqual(launch['state'], 'unconfirmed', launch)

    def test_one_pass_confirms_several_agents_together(self):
        good = self.agent(self.long_agent(), suffix='-a')
        bad = self.agent('true', suffix='-b')
        more = [self.agent('true', suffix=f'-{i}') for i in 'cd']
        cfg = {'agents': [good, bad, *more], 'keepalive': {}}
        with patch.object(keepalive, '_log') as log:
            began = time.monotonic()
            n = keepalive.tick(cfg)
            took = time.monotonic() - began
        lines = [c.args[0] for c in log.call_args_list]
        self.assertEqual(n, 1)
        self.assertTrue(any(good['name'] in l and 'restart confirmed' in l for l in lines), lines)
        self.assertTrue(any(bad['name'] in l and 'NOT confirmed' in l for l in lines), lines)
        self.assertLess(took, 2 * 4)          # one shared window, not one per agent (3 x 4 s)

    # callers -------------------------------------------------------------------------------
    def dashboard_restart(self, agent, **extra):
        cfg = {'agents': [agent], **extra}
        with patch.object(config, 'load', return_value=cfg), \
             patch.object(dashboard, '_set_enabled'), patch.object(keepalive, '_log'):
            return dashboard._agent_action(agent['name'], 'restart')

    def test_dashboard_restart_reports_success_only_when_confirmed(self):
        ok, message = self.dashboard_restart(self.agent(self.long_agent()))
        self.assertTrue(ok, message)
        self.assertIn('confirmed', message)
        ok, message = self.dashboard_restart(self.agent('true'))
        self.assertFalse(ok)
        self.assertIn('not confirmed', message)
        fake = self.tmp / 'tmux-fails'
        fake.write_text('#!/bin/sh\necho "no server" >&2\nexit 1\n')
        fake.chmod(0o755)
        ok, message = self.dashboard_restart(self.agent(self.long_agent()), tmux_bin=str(fake))
        self.assertFalse(ok)
        self.assertIn('restart failed – ', message)

    def test_restart_cli_fails_when_the_restart_is_not_confirmed(self):
        a = self.agent('true')
        cfg = {'agents': [a]}
        with patch.object(config, 'load', return_value=cfg), \
             patch.object(dashboard, '_set_enabled'), patch.object(keepalive, '_log'), \
             redirect_stdout(io.StringIO()) as out:
            rc = cli.main(['restart', a['name']])
        self.assertEqual(rc, 1)
        self.assertIn('✗', out.getvalue())


class ConfirmationWindow(unittest.TestCase):
    """The window with a controlled clock: no look may start once it has passed."""

    def test_no_look_starts_after_the_deadline(self):
        clock = {'t': 100.0}
        looks = []

        def look(launch, procs, children, panes):
            looks.append(clock['t'])
            return 4242 if clock['t'] >= 100.0 + 1.5 else None   # first seen just before the end

        def sleep(seconds):
            clock['t'] += seconds + 0.01                          # every sleep overshoots a bit

        launch = {'name': 'w', 'match': 'agent', 'state': 'sent', 'pane': 1, 'before': {},
                  'tmux': 'tmux'}
        with patch.object(keepalive.time, 'monotonic', lambda: clock['t']), \
             patch.object(keepalive.time, 'sleep', sleep), \
             patch.object(keepalive.detect, '_proc_table', return_value=({}, {})), \
             patch.object(keepalive, '_session_panes', return_value={}), \
             patch.object(keepalive, '_launched_pid', side_effect=look):
            result = keepalive._confirm([launch], seconds=2)[0]
        self.assertTrue(looks and all(t < 102.0 for t in looks), looks)
        self.assertEqual(result['state'], 'unconfirmed')     # seen once in time, not twice


if __name__ == '__main__':
    unittest.main()
