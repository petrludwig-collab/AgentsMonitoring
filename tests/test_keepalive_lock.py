"""Only one keepalive pass at a time, for as long as that pass really runs.

These tests start the real entry point, ``python -m agentsmon keepalive``, as throwaway processes.
Each pass has one daemon that is down, and its restart command is a gate script: it records which
pass reached the protected work and then waits until the test lets that pass go on. So the test
knows exactly which passes are inside a pass at any moment, without guessing with sleeps. HOME,
config, state and the tmux socket directory point into a temporary directory, and every process
started here is stopped at the end.
"""
import errno
import fcntl
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
from unittest import mock
import uuid

from agentsmon import keepalive

REPO = Path(__file__).resolve().parents[1]

GATE = r'''t=$AM_TAG; d=$AM_DIR
echo "$t" >> "$d/entered"; : > "$d/in.$t"
if [ -n "$AM_CHILD" ]; then
  "$AM_PY" -c 'import os, sys, time
os.setsid()
with open(sys.argv[1] + ".tmp", "w") as f:
    f.write(str(os.getpid()))
os.replace(sys.argv[1] + ".tmp", sys.argv[1])
time.sleep(120)' "$d/child.$t" </dev/null >/dev/null 2>&1 &
fi
i=0
while [ ! -e "$d/go.$t" ]; do
  i=$((i+1)); if [ $i -gt 250 ]; then : > "$d/timeout.$t"; exit 1; fi
  sleep 0.1
done
: > "$d/done.$t"
'''


def wait_until(pred, timeout=20.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.05)
    return pred()


@unittest.skipUnless(sys.platform != 'win32', 'POSIX only')
class Passes(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='amlock-'))
        self.state = self.tmp / 'state'
        gate = self.tmp / 'gate.sh'
        gate.write_text(GATE)
        cfg = {'keepalive': {'enabled': True, 'interval_seconds': 3600}, 'agents': [],
               'daemons': [{'name': 'svc', 'pattern': 'amlock-nothing-' + uuid.uuid4().hex,
                            'restart': f'sh {gate}'}]}
        (self.tmp / 'config.json').write_text(json.dumps(cfg))
        self.env = {**os.environ, 'HOME': str(self.tmp), 'PYTHONPATH': str(REPO),
                    'AGENTSMON_CONFIG': str(self.tmp / 'config.json'),
                    'AGENTSMON_STATE': str(self.state), 'TMUX_TMPDIR': str(self.tmp),
                    'AM_DIR': str(self.tmp), 'AM_PY': sys.executable}
        self.env.pop('TMUX', None)
        self.procs = {}

    def tearDown(self):
        for tag in self.procs:
            (self.tmp / f'go.{tag}').touch()
        for p in self.procs.values():
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            p.wait()
        timeouts = sorted(p.name for p in self.tmp.glob('timeout.*'))
        for f in self.tmp.glob('child.*'):
            try:
                os.kill(int(f.read_text()), signal.SIGKILL)
            except (ValueError, ProcessLookupError):
                pass
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(timeouts, [], 'a pass was never let go by the test')

    # -- helpers -------------------------------------------------------------------------------
    def start(self, tag, *args, go=False, child=False):
        if go:
            (self.tmp / f'go.{tag}').touch()
        env = {**self.env, 'AM_TAG': tag}
        if child:
            env['AM_CHILD'] = '1'
        out = open(self.tmp / f'out.{tag}', 'w')
        p = subprocess.Popen([sys.executable, '-m', 'agentsmon', 'keepalive', *args], cwd=self.tmp,
                             env=env, stdout=out, stderr=subprocess.STDOUT, start_new_session=True)
        out.close()
        self.procs[tag] = p
        return p

    def inside(self, tag):
        return (self.tmp / f'in.{tag}').exists() and not (self.tmp / f'done.{tag}').exists()

    def entered(self):
        try:
            return (self.tmp / 'entered').read_text().split()
        except FileNotFoundError:
            return []

    def output(self, tag):
        return (self.tmp / f'out.{tag}').read_text()

    def lock_free(self):
        """True once nobody holds the pass lock (probed with the same flock, released at once)."""
        fd = os.open(self.state / keepalive.LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return False
        finally:
            os.close(fd)
        return True

    def heartbeat(self):
        return (self.state / 'keepalive_heartbeat').exists()

    def age_lock(self):
        """Make whatever keepalive lock exists look an hour old (no five-minute wait)."""
        old = time.time() - 3600
        aged = [p for p in self.state.iterdir() if 'lock' in p.name]
        for p in aged:
            os.utime(p, (old, old))
        self.assertTrue(aged, 'no lock entry to age')

    def hold(self, tag, *args, **kw):
        p = self.start(tag, *args, **kw)
        self.assertTrue(wait_until(lambda: self.inside(tag)), f'{tag} never reached the protected work')
        return p

    def finish(self, tag, rc=0):
        (self.tmp / f'go.{tag}').touch()
        self.assertEqual(self.procs[tag].wait(timeout=30), rc, self.output(tag))

    def skipped(self, tag):
        """The pass ended (or got stuck in the gate, which counts as having entered)."""
        p = self.procs[tag]
        wait_until(lambda: p.poll() is not None or (self.tmp / f'in.{tag}').exists())
        return p.poll() is not None and tag not in self.entered()

    # -- AK1: a running pass with an old-looking lock keeps the next pass out ------------------
    def test_old_lock_of_running_pass_keeps_second_pass_out(self):
        self.hold('A')
        self.age_lock()
        inode = (self.state / getattr(keepalive, 'LOCK_NAME', 'keepalive.lock')).stat().st_ino
        self.start('B')
        self.assertTrue(self.skipped('B'), 'B entered the pass while A was still in it')
        self.assertEqual(self.procs['B'].returncode, 0)
        self.assertIn('holds the lock', self.output('B'))
        self.assertFalse(self.heartbeat(), 'B stamped the heartbeat while A was still running')
        self.finish('A')
        self.assertTrue(self.heartbeat())
        # positive control: once A is done the next pass really gets in
        self.start('C', go=True)
        self.assertEqual(self.procs['C'].wait(timeout=30), 0, self.output('C'))
        self.assertEqual(self.entered(), ['A', 'C'])
        self.assertEqual((self.state / keepalive.LOCK_NAME).stat().st_ino, inode,
                         'the passes locked different files under the same path')

    def test_end_of_an_earlier_pass_does_not_unlock_the_current_one(self):
        """Old code: B took over A's aged lock, then A's release removed B's lock and C got in too."""
        self.hold('A')
        self.age_lock()
        self.start('B')
        wait_until(lambda: self.procs['B'].poll() is not None or self.inside('B'))
        self.finish('A')
        self.start('C')
        wait_until(lambda: self.procs['C'].poll() is not None or self.inside('C'))
        both = [t for t in ('B', 'C') if self.inside(t)]
        self.assertLessEqual(len(both), 1, f'two passes inside at once: {both}')

    # -- AK2: the lock spans the --loop pass and is free between passes -----------------------
    def test_loop_holds_the_lock_for_its_pass_and_not_while_waiting(self):
        self.hold('A', '--loop')
        self.age_lock()
        self.start('B')
        self.assertTrue(self.skipped('B'), 'B entered while the --loop pass was running')
        (self.tmp / 'go.A').touch()
        self.assertTrue(wait_until(self.heartbeat), 'the --loop pass did not complete')
        self.assertTrue(wait_until(self.lock_free), '--loop kept the lock after its pass')
        self.assertIsNone(self.procs['A'].poll(), '--loop should keep running')
        self.start('C', go=True)
        self.assertEqual(self.procs['C'].wait(timeout=30), 0, self.output('C'))
        self.assertEqual(self.entered(), ['A', 'C'], 'the lock was held while --loop was waiting')

    def test_a_killed_pass_does_not_block_the_next_one(self):
        self.hold('A')
        os.killpg(self.procs['A'].pid, signal.SIGKILL)
        self.procs['A'].wait()
        self.start('B', go=True)
        self.assertEqual(self.procs['B'].wait(timeout=30), 0, self.output('B'))
        self.assertIn('B', self.entered(), 'the next pass waited for a stale timeout')
        self.assertTrue(self.heartbeat())

    def test_a_restarted_child_does_not_keep_the_lock_after_its_parent_dies(self):
        """The restart command leaves a long-running child behind; then only the pass is killed."""
        self.hold('A', child=True)
        # the child writes its PID itself, after it has started its own session
        self.assertTrue(wait_until(lambda: (self.tmp / 'child.A').exists()))
        os.kill(int((self.tmp / 'child.A').read_text()), 0)
        os.kill(self.procs['A'].pid, signal.SIGKILL)       # the gate and its child stay alive
        self.procs['A'].wait()
        self.start('B', go=True)
        self.assertEqual(self.procs['B'].wait(timeout=30), 0, self.output('B'))
        self.assertIn('B', self.entered(), "a child of the dead pass still held the lock")

    def test_the_lock_is_one_lasting_file(self):
        self.start('A', go=True).wait(timeout=30)
        lock = self.state / keepalive.LOCK_NAME
        first = lock.stat()
        self.start('B', go=True).wait(timeout=30)
        self.assertEqual(lock.stat().st_ino, first.st_ino, 'the lock path was replaced')
        self.assertEqual(self.entered(), ['A', 'B'])

    # -- AK3/AK4: busy is not an error, an error is not busy, an old lock is not free ----------
    def test_unusable_lock_file_is_an_error_not_a_skip(self):
        self.state.mkdir()
        (self.state / keepalive.LOCK_NAME).mkdir()          # cannot be opened as a file
        p = self.start('A', go=True)
        self.assertEqual(p.wait(timeout=30), 1, self.output('A'))
        self.assertIn('cannot take the keepalive lock', self.output('A'))
        self.assertNotIn('holds the lock', self.output('A'))
        self.assertEqual(self.entered(), [])
        self.assertFalse(self.heartbeat())

    def test_old_version_lock_directory_is_not_taken_over(self):
        self.state.mkdir()
        legacy = self.state / 'keepalive.lock'
        legacy.mkdir()
        old = time.time() - 3600
        os.utime(legacy, (old, old))
        p = self.start('A', go=True)
        self.assertEqual(p.wait(timeout=30), 1, self.output('A'))
        self.assertIn('older agentsmon', self.output('A'))
        self.assertEqual(self.entered(), [])
        self.assertFalse(self.heartbeat())
        self.assertTrue(legacy.is_dir(), 'the old lock directory was removed')

    def test_unknown_entry_at_old_lock_path_is_reported_not_skipped(self):
        self.state.mkdir()
        (self.state / 'keepalive.lock').write_text('?')
        p = self.start('A', go=True)
        self.assertEqual(p.wait(timeout=30), 1, self.output('A'))
        self.assertNotIn('holds the lock', self.output('A'))
        self.assertEqual(self.entered(), [])
        self.assertTrue((self.state / 'keepalive.lock').is_file())


class InProcess(unittest.TestCase):
    """The lock helpers within one process: same-process callers exclude each other too."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='amlock-in-')
        patcher = mock.patch.dict(os.environ, {'AGENTSMON_STATE': self.tmp})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.cfg = {'keepalive': {'enabled': True}, 'agents': [], 'daemons': []}

    def run_once(self, tick):
        with mock.patch.object(keepalive.config, 'load', return_value=self.cfg), \
             mock.patch.object(keepalive.service, 'refresh_launcher', return_value=False), \
             mock.patch.object(keepalive, 'tick', side_effect=tick):
            return keepalive.run(loop=False)

    def test_a_second_take_in_the_same_process_is_busy(self):
        lock = keepalive._acquire_lock()
        try:
            self.assertIsNone(keepalive._acquire_lock())
        finally:
            keepalive._release_lock(lock)
        again = keepalive._acquire_lock()
        self.assertIsNotNone(again)
        keepalive._release_lock(again)

    def test_the_lock_is_not_inherited_by_started_programs(self):
        seen = []

        def tick(cfg):
            fds = [int(fd) for fd in os.listdir('/proc/self/fd')]
            path = os.path.realpath(os.path.join(self.tmp, keepalive.LOCK_NAME))
            for fd in fds:
                try:
                    if os.readlink(f'/proc/self/fd/{fd}') == path:
                        seen.append(os.get_inheritable(fd))
                except OSError:
                    pass
            return 0
        if not os.path.isdir('/proc/self/fd'):
            self.skipTest('needs /proc')
        self.assertEqual(self.run_once(tick), 0)
        self.assertEqual(seen, [False])

    def test_an_exception_in_the_pass_releases_the_lock(self):
        def boom(cfg):
            raise RuntimeError('boom')
        with self.assertRaises(RuntimeError):
            self.run_once(boom)
        self.assertEqual(self.run_once(lambda cfg: 0), 0)
        self.assertTrue(keepalive.heartbeat_path().exists())

    def test_a_lock_error_other_than_busy_runs_no_pass(self):
        ticks = []
        with mock.patch.object(keepalive.fcntl, 'flock', side_effect=OSError(errno.ENOLCK, 'No locks available')):
            rc = self.run_once(lambda cfg: ticks.append(1) or 0)
        self.assertEqual(rc, 1)
        self.assertEqual(ticks, [])
        self.assertFalse(keepalive.heartbeat_path().exists())

    def test_the_heartbeat_is_stamped_while_the_lock_is_held(self):
        held = []
        real = keepalive._heartbeat

        def heartbeat():
            other = keepalive._acquire_lock()
            held.append(other is None)
            if other is not None:
                keepalive._release_lock(other)
            real()
        with mock.patch.object(keepalive, '_heartbeat', side_effect=heartbeat):
            self.assertEqual(self.run_once(lambda cfg: 0), 0)
        self.assertEqual(held, [True], 'the heartbeat was stamped after the lock was released')

    def test_loop_logs_a_lock_error_and_tries_again_without_a_pass(self):
        ticks, sleeps = [], []

        def sleep(seconds):
            sleeps.append(seconds)
            if len(sleeps) == 2:
                raise KeyboardInterrupt
        with mock.patch.object(keepalive.fcntl, 'flock', side_effect=OSError(errno.ENOLCK, 'No locks available')), \
             mock.patch.object(keepalive.config, 'load', return_value=self.cfg), \
             mock.patch.object(keepalive.service, 'refresh_launcher', return_value=False), \
             mock.patch.object(keepalive, 'tick', side_effect=lambda cfg: ticks.append(1) or 0), \
             mock.patch.object(keepalive.time, 'sleep', side_effect=sleep), \
             mock.patch.object(keepalive, '_log') as log:
            with self.assertRaises(KeyboardInterrupt):
                keepalive.run(loop=True)
        self.assertEqual(ticks, [])
        self.assertEqual(len(sleeps), 2, 'the loop did not carry on after a lock error')
        self.assertFalse(keepalive.heartbeat_path().exists())
        msgs = [c.args[0] for c in log.call_args_list]
        self.assertEqual(sum('cannot take the keepalive lock' in m for m in msgs), 2, msgs)

    def test_busy_skips_quietly(self):
        lock = keepalive._acquire_lock()
        ticks = []
        try:
            rc = self.run_once(lambda cfg: ticks.append(1) or 0)
        finally:
            keepalive._release_lock(lock)
        self.assertEqual((rc, ticks), (0, []))
        self.assertFalse(keepalive.heartbeat_path().exists())


if __name__ == '__main__':
    unittest.main()
