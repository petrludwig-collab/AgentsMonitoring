"""An agent's or daemon's `enabled` that is not JSON true/false must not make agentsmon restart it.

The keepalive and doctor tests run the real `python -m agentsmon keepalive` / `doctor` as throwaway
processes. Every item is down and its restart only leaves a marker file, so "restarted" means the
marker exists after the pass. Isolation is set in the child's environment before agentsmon is
imported: HOME, config, state and the tmux socket directory (TMUX/TMUX_PANE removed, so tmux
starts its own server in the temporary directory) point into a temporary directory, and
`systemctl`, `loginctl` and `launchctl` on PATH are stubs that only record their arguments; the
user bus variables point into the same directory, so no real service manager is reached.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

# JSON values that are neither true nor false. A distinctive string checks the value is not echoed.
INVALID = ["false", "true", "off-7f3a", "", "0", 0, 1, 0.0, 1.5, None, [], [False], {}, {"on": False}]

STUB = """#!/bin/sh
# Records every call. For `systemctl --user show UNIT`, a unit is active once it was restarted.
echo "$(basename "$0") $*" >> "$AM_TEST_DIR/control.log"
if [ "$(basename "$0")" = systemctl ]; then
  case "$2" in
    restart|start) echo "$3" >> "$AM_TEST_DIR/active-units" ;;
    show) if grep -qx "$3" "$AM_TEST_DIR/active-units" 2>/dev/null; then s=active; else s=inactive; fi
          printf 'LoadState=loaded\\nActiveState=%s\\nMainPID=0\\nUnitFileState=enabled\\n' "$s" ;;
  esac
fi
# Lingering is on, so doctor has no unrelated service warning.
[ "$(basename "$0")" = loginctl ] && echo yes
exit 0
"""


class Processes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="am-enabled-")
        bin_dir = os.path.join(self.tmp, "bin")
        os.mkdir(bin_dir)
        for tool in ("systemctl", "loginctl", "launchctl"):
            path = os.path.join(bin_dir, tool)
            with open(path, "w") as f:
                f.write(STUB)
            os.chmod(path, 0o755)
        env = {k: v for k, v in os.environ.items() if k not in ("TMUX", "TMUX_PANE")}
        env.update(HOME=self.tmp, TMUX_TMPDIR=self.tmp, AM_TEST_DIR=self.tmp,
                   AGENTSMON_CONFIG=os.path.join(self.tmp, "config.json"),
                   AGENTSMON_STATE=os.path.join(self.tmp, "state"),
                   XDG_RUNTIME_DIR=os.path.join(self.tmp, "run"),
                   DBUS_SESSION_BUS_ADDRESS="unix:path=" + os.path.join(self.tmp, "run", "bus"),
                   PATH=bin_dir + os.pathsep + env.get("PATH", ""))
        self.env = env

    def tearDown(self):
        subprocess.run(["tmux", "kill-server"], env=self.env, capture_output=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- helpers -------------------------------------------------------------------------
    def mark(self, name):
        return os.path.join(self.tmp, f"restarted-{name}")

    def agent(self, name, enabled=None, omit=False):
        a = {"name": name, "match": f"am-enabled-no-such-agent-{name}", "cwd": self.tmp,
             "restart": f"touch {self.mark(name)}; sleep 30"}
        if not omit:
            a["enabled"] = enabled
        return a

    def daemon(self, name, enabled=None, omit=False, managed=False):
        d = {"name": name, "pattern": f"am-enabled-no-such-daemon-{name}"}
        if managed:
            d["manager"] = {"type": "systemd", "unit": f"{name}.service"}
        else:
            d["restart"] = f"touch {self.mark(name)}"
        if not omit:
            d["enabled"] = enabled
        return d

    def write_config(self, cfg):
        with open(self.env["AGENTSMON_CONFIG"], "w") as f:
            json.dump(cfg, f)
        with open(self.env["AGENTSMON_CONFIG"], "rb") as f:
            return f.read()

    def run_cli(self, *args):
        return subprocess.run([sys.executable, "-m", "agentsmon", *args], env=self.env,
                              capture_output=True, text=True, timeout=120)

    def restarted(self, name, wait=0.0):
        deadline = time.time() + wait
        while True:
            if os.path.exists(self.mark(name)):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.1)

    def read(self, name):
        try:
            with open(os.path.join(self.tmp, name)) as f:
                return f.read()
        except FileNotFoundError:
            return ""

    def log(self):
        return self.read(os.path.join("state", "keepalive.log"))

    def service_actions(self):
        """systemctl calls that change a unit (status queries are allowed)."""
        return [line for line in self.read("control.log").splitlines()
                if line.split()[:1] == ["systemctl"] and
                any(verb in line.split() for verb in ("restart", "start", "stop", "enable"))]

    def sessions(self):
        r = subprocess.run(["tmux", "list-sessions", "-F", "#{session_name}"], env=self.env,
                           capture_output=True, text=True)
        return set(r.stdout.split()) if r.returncode == 0 else set()

    # --- keepalive -----------------------------------------------------------------------
    def test_string_false_agent_alone_is_not_restarted(self):
        before = self.write_config({"agents": [self.agent("a", "false")], "daemons": []})
        r = self.run_cli("keepalive")
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertFalse(self.restarted("a", wait=3), self.log())
        self.assertNotIn("a", self.sessions())
        self.assertIn("agent 'a': 'enabled' must be true or false, not a string — not restarted", self.log())
        with open(self.env["AGENTSMON_CONFIG"], "rb") as f:
            self.assertEqual(f.read(), before)

    def test_string_false_daemon_alone_is_not_restarted(self):
        self.write_config({"agents": [], "daemons": [self.daemon("d", "false")]})
        r = self.run_cli("keepalive")
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertFalse(self.restarted("d"), self.log())
        self.assertIn("daemon 'd': 'enabled' must be true or false, not a string — not restarted", self.log())

    def test_mixed_config_restarts_only_the_valid_items(self):
        agents = [self.agent("a-true", True), self.agent("a-omitted", omit=True), self.agent("a-false", False)]
        daemons = [self.daemon("d-true", True), self.daemon("d-omitted", omit=True), self.daemon("d-false", False),
                   self.daemon("m-true", True, managed=True), self.daemon("m-false", False, managed=True)]
        for i, value in enumerate(INVALID):
            agents.append(self.agent(f"a-bad{i}", value))
            daemons.append(self.daemon(f"d-bad{i}", value))
            daemons.append(self.daemon(f"m-bad{i}", value, managed=True))
        before = self.write_config({"agents": agents, "daemons": daemons})
        r = self.run_cli("keepalive")
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        log = self.log()
        # Valid items keep their meaning: true and omitted are restarted, false is left alone.
        for name in ("a-true", "a-omitted"):
            self.assertTrue(self.restarted(name, wait=5), f"{name} not restarted\n{log}")
        for name in ("d-true", "d-omitted"):
            self.assertTrue(self.restarted(name), f"{name} not restarted\n{log}")
        self.assertIn("systemctl --user restart m-true.service", self.service_actions())
        for name in ("a-false", "d-false"):
            self.assertFalse(self.restarted(name), log)
        self.assertNotIn("m-false.service", " ".join(self.service_actions()))
        # Invalid items: no restart command, no tmux session, no service action, a named error.
        for i, value in enumerate(INVALID):
            with self.subTest(enabled=value):
                self.assertFalse(self.restarted(f"a-bad{i}"), log)
                self.assertFalse(self.restarted(f"d-bad{i}"), log)
                self.assertNotIn(f"a-bad{i}", self.sessions())
                self.assertNotIn(f"m-bad{i}.service", " ".join(self.service_actions()))
                for kind, name in (("agent", f"a-bad{i}"), ("daemon", f"d-bad{i}"), ("daemon", f"m-bad{i}")):
                    lines = [x for x in log.splitlines() if f"'{name}'" in x]
                    self.assertEqual(len(lines), 1, lines)
                    self.assertIn(f"{kind} '{name}': 'enabled' must be true or false, not ", lines[0])
                    self.assertTrue(lines[0].endswith(" — not restarted"), lines[0])
        self.assertNotIn("off-7f3a", log)
        # The restart count still counts only real restarts, and the pass completes as before.
        self.assertIn("pass complete: 5 restart(s)", log)
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "state", "keepalive_heartbeat")))
        with open(self.env["AGENTSMON_CONFIG"], "rb") as f:
            self.assertEqual(f.read(), before)

    def test_valid_values_log_no_type_error(self):
        self.write_config({"agents": [self.agent("a-true", True), self.agent("a-false", False),
                                      self.agent("a-omitted", omit=True)],
                           "daemons": [self.daemon("d-true", True), self.daemon("d-false", False),
                                       self.daemon("d-omitted", omit=True)]})
        r = self.run_cli("keepalive")
        self.assertEqual(r.returncode, 0, r.stderr[-500:])
        self.assertNotIn("must be true or false", self.log())
        self.assertTrue(self.restarted("a-true", wait=5) and self.restarted("a-omitted", wait=5), self.log())
        self.assertTrue(self.restarted("d-true") and self.restarted("d-omitted"), self.log())
        self.assertFalse(self.restarted("a-false") or self.restarted("d-false"), self.log())

    # --- doctor --------------------------------------------------------------------------
    def require_passing_doctor(self):
        """doctor's status is only meaningful here if it passes without the entries under test
        (it also fails without tmux or with a modified core)."""
        self.write_config({"agents": [], "daemons": []})
        r = self.run_cli("doctor")
        if r.returncode != 0:
            self.skipTest("doctor fails here even without agents or daemons:\n" + r.stdout)

    def test_doctor_reports_invalid_enabled_without_acting(self):
        self.require_passing_doctor()
        before = self.write_config({"agents": [self.agent("a-ok", True), self.agent("a-bad", "off-7f3a")],
                                    "daemons": [self.daemon("d-ok", omit=True), self.daemon("d-bad", 1),
                                                self.daemon("m-bad", None, managed=True)]})
        r = self.run_cli("doctor")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("✗ agent 'a-bad': 'enabled' must be true or false, not a string — keepalive does not "
                      "restart it", r.stdout)
        self.assertIn("✗ daemon 'd-bad': 'enabled' must be true or false, not a number", r.stdout)
        self.assertIn("✗ daemon 'm-bad': 'enabled' must be true or false, not null", r.stdout)
        self.assertNotIn("a-ok':", r.stdout)
        self.assertNotIn("d-ok':", r.stdout)
        self.assertNotIn("off-7f3a", r.stdout)
        # Status 1 comes from these entries alone: no other warning or failed check is printed.
        self.assertEqual([x for x in r.stdout.splitlines() if "Warning" in x or "⚠️" in x], [], r.stdout)
        self.assertEqual(self.service_actions(), [])
        self.assertEqual(self.sessions(), set())
        self.assertFalse(any(self.restarted(n) for n in ("a-ok", "a-bad", "d-ok", "d-bad")))
        with open(self.env["AGENTSMON_CONFIG"], "rb") as f:
            self.assertEqual(f.read(), before)

    def test_doctor_result_is_unchanged_for_valid_values(self):
        self.require_passing_doctor()
        self.write_config({"agents": [self.agent("a-true", True), self.agent("a-false", False),
                                      self.agent("a-omitted", omit=True)],
                           "daemons": [self.daemon("d-true", True), self.daemon("d-false", False),
                                       self.daemon("d-omitted", omit=True)]})
        r = self.run_cli("doctor")
        self.assertNotIn("must be true or false", r.stdout)
        self.assertEqual(r.returncode, 0, r.stdout)
        # One invalid agent, or one invalid (managed) daemon, alone turns a passing doctor into a failing one.
        for valid, invalid in (({"agents": [self.agent("a", True)], "daemons": []},
                                {"agents": [self.agent("a", "true")], "daemons": []}),
                               ({"agents": [], "daemons": [self.daemon("m", True, managed=True)]},
                                {"agents": [], "daemons": [self.daemon("m", "true", managed=True)]})):
            self.write_config(valid)
            self.assertEqual(self.run_cli("doctor").returncode, 0)
            self.write_config(invalid)
            self.assertEqual(self.run_cli("doctor").returncode, 1)


# The interactive commands below run in a child process too, so that agentsmon is imported only
# after HOME, config, state, tmux and the user bus point into the temporary directory. Inside it,
# the parts that would ask questions or look at real processes are patched; the code under test runs
# unpatched and the outcome is printed as JSON.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

RESTART_ALL = """
import io, json, sys
from contextlib import redirect_stdout
from unittest.mock import patch
from agentsmon import __main__ as cli, config, dashboard, detect
cfg, running = json.loads(sys.argv[1])
alive = [{"name": n, "alive": True} for n in running]
with patch.object(config, "load", return_value=cfg), \\
     patch.object(detect, "discover_agents", return_value=alive), \\
     patch.object(dashboard, "_agent_action", return_value=(True, "restarting")) as act, \\
     redirect_stdout(io.StringIO()) as out:
    rc = cli.main(["restart", "--all"])
print(json.dumps({"rc": rc, "calls": [list(c.args) for c in act.call_args_list], "out": out.getvalue(),
                  "config": str(config.DEFAULT_PATH), "state": str(config.state_dir())}))
"""

SETUP = """
import copy, io, json, sys
from contextlib import redirect_stdout
from unittest.mock import patch
from agentsmon import config, detect, managed, wizard
enabled = json.loads(sys.argv[1])
cfg = copy.deepcopy(config.DEFAULTS)
cfg["daemons"] = [{"name": "Hermes", "enabled": enabled, "pattern": "custom", "restart": "custom-start"}]
if enabled == "<omitted>":
    del cfg["daemons"][0]["enabled"]
candidate = {"kind": "daemon", "obj": wizard.COMMON_DAEMONS[1], "display": "Hermes"}
assert candidate["obj"]["name"] == "Hermes"
with patch.object(config, "load", return_value=copy.deepcopy(cfg)), \\
     patch.object(config, "save") as save, \\
     patch.object(wizard, "_scan_candidates", return_value=[candidate]), \\
     patch.object(wizard.shutil, "which", return_value="/fixture/tmux"), \\
     patch.object(managed, "discover", return_value={"type": "systemd", "unit": "hermes-gateway.service"}), \\
     patch.object(managed, "activate", return_value=(True, [])) as activate, \\
     patch.object(detect, "daemon_telegram_bot", return_value=""), \\
     patch("builtins.input", side_effect=["all", "n", "8765", "n", "n"]), \\
     redirect_stdout(io.StringIO()) as out:
    rc = wizard.run()
print(json.dumps({"rc": rc, "activated": activate.call_count, "out": out.getvalue(),
                  "saved": save.call_args.args[0]["daemons"][0].get("enabled", "<omitted>")}))
"""

ADD = """
import io, json, sys
from contextlib import redirect_stdout
from unittest.mock import patch
from agentsmon import config, managed, wizard
enabled = json.loads(sys.argv[1])
cfg = {"daemons": [{"name": "Hermes", "enabled": enabled}]}
if enabled == "<omitted>":
    del cfg["daemons"][0]["enabled"]
with patch.object(config.DEFAULT_PATH.__class__, "exists", return_value=True), \\
     patch.object(config, "load", return_value=cfg), \\
     patch.object(wizard, "_scan_candidates", return_value=[]), \\
     patch.object(managed, "discover", return_value=None) as discover, \\
     redirect_stdout(io.StringIO()):
    rc = wizard.add()
print(json.dumps({"rc": rc, "discovered": discover.call_count}))
"""


class InteractiveCommands(unittest.TestCase):
    setUp = Processes.setUp
    tearDown = Processes.tearDown

    def scenario(self, script, arg):
        env = dict(self.env, PYTHONPATH=REPO + os.pathsep + self.env.get("PYTHONPATH", ""))
        r = subprocess.run([sys.executable, "-c", script, json.dumps(arg)], env=env, cwd=self.tmp,
                           capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr[-1500:])
        return json.loads(r.stdout.strip().splitlines()[-1])

    CFG = {"agents": [{"name": "ok"}, {"name": "bad", "enabled": "false"}, {"name": "off", "enabled": False}]}

    def test_restart_all_skips_a_running_agent_with_invalid_enabled(self):
        got = self.scenario(RESTART_ALL, [self.CFG, ["ok", "bad", "off"]])
        self.assertTrue(got["config"].startswith(self.tmp) and got["state"].startswith(self.tmp), got)
        self.assertEqual(got["calls"], [["ok", "restart"]])
        self.assertIn("✗ bad: 'enabled' must be true or false, not a string — not restarted", got["out"])
        self.assertEqual(got["rc"], 1)

    def test_restart_all_does_not_mention_an_invalid_agent_that_is_not_running(self):
        got = self.scenario(RESTART_ALL, [self.CFG, ["ok"]])
        self.assertEqual((got["rc"], got["calls"]), (0, [["ok", "restart"]]))
        self.assertNotIn("bad", got["out"])

    def test_setup_does_not_activate_a_service_with_invalid_enabled(self):
        for value in INVALID:
            with self.subTest(enabled=value):
                got = self.scenario(SETUP, value)
                self.assertEqual(got["rc"], 0, got["out"])
                self.assertEqual(got["activated"], 0, got["out"])
                self.assertEqual(got["saved"], value)          # kept as written, not converted
                self.assertIn("✗ daemon 'Hermes': 'enabled' must be true or false, not ", got["out"])
                self.assertIn("— its service is not activated", got["out"])
                self.assertNotIn("off-7f3a", got["out"])

    def test_setup_keeps_activating_true_and_omitted_and_not_false(self):
        for value, activated in ((True, 1), ("<omitted>", 1), (False, 0)):
            with self.subTest(enabled=value):
                got = self.scenario(SETUP, value)
                self.assertEqual((got["rc"], got["activated"], got["saved"]), (0, activated, value), got["out"])
                self.assertNotIn("must be true or false", got["out"])

    def test_add_does_not_offer_a_service_with_invalid_enabled_for_repair(self):
        for value in INVALID:
            with self.subTest(enabled=value):
                self.assertEqual(self.scenario(ADD, value)["discovered"], 0)
        self.assertEqual(self.scenario(ADD, True)["discovered"], 1)
        self.assertEqual(self.scenario(ADD, "<omitted>")["discovered"], 1)
        self.assertEqual(self.scenario(ADD, False)["discovered"], 0)


if __name__ == "__main__":
    unittest.main()
