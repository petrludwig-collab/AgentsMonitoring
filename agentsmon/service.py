"""Boot persistence — keep the dashboard + keepalive running across reboots.

We use **cron** (a launcher run `@reboot` and every minute) rather than systemd ``--user`` or a
macOS LaunchAgent. On a headless server reached over SSH there's often no user D-Bus / systemd
instance (``systemctl --user`` fails with "Failed to connect to bus: No medium found") and a
macOS LaunchAgent needs a GUI login session. A cron launcher that nohups the dashboard (guarded
by pgrep) and runs one keepalive pass works everywhere, no login session required.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import agentsmon
from . import config

MARKER = "agentsmon-launch.sh"   # identifies our crontab lines


def _python() -> str:
    return sys.executable or "python3"


def _pythonpath() -> str:
    # Parent of the package dir, so the launcher imports agentsmon whether pip-installed or run
    # straight from a clone.
    return str(Path(agentsmon.__file__).resolve().parent.parent)


def _launcher_path() -> Path:
    return config.state_dir() / MARKER


def _write_launcher() -> Path:
    state = config.state_dir()
    log = state / "agentsmon.log"
    path = _launcher_path()
    path.write_text(f"""#!/bin/sh
# Agents Monitoring launcher — started by cron (@reboot + every minute). Idempotent: starts the
# dashboard only if it isn't running, then runs one keepalive pass (a no-op if disabled / no agents).
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"
export PYTHONPATH="{_pythonpath()}"
export AGENTSMON_CONFIG="{config.DEFAULT_PATH}"
export AGENTSMON_STATE="{config.state_dir()}"
PY="{_python()}"
mkdir -p "{state}"
pgrep -f "agentsmon dashboard" >/dev/null 2>&1 || \\
  nohup "$PY" -m agentsmon dashboard >> "{log}" 2>&1 &
"$PY" -m agentsmon keepalive >> "{log}" 2>&1
""", encoding="utf-8")
    path.chmod(0o755)
    return path


def install() -> int:
    if not shutil.which("cron") and not shutil.which("crontab"):
        print("⚠️  crontab not found. Run these yourself under any process manager:")
        print(f"    {_python()} -m agentsmon dashboard &")
        print(f"    {_python()} -m agentsmon keepalive --loop &")
        return 1
    launcher = _write_launcher()
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except OSError:
        existing = ""
    lines = [ln for ln in existing.splitlines() if MARKER not in ln]
    lines.append(f"@reboot {launcher}")
    lines.append(f"* * * * * {launcher}")
    proc = subprocess.run(["crontab", "-"], input="\n".join(lines) + "\n", text=True,
                          capture_output=True)
    if proc.returncode != 0:
        print(f"✗ couldn't update crontab: {proc.stderr.strip()}")
        return 1
    # Stop any dashboard already running, so the launcher restarts it with the CURRENT config
    # (host/port/auth). Without this, a re-run can't change a live dashboard — its pgrep guard
    # would just leave the stale one bound to the old address. (Safe: our own process is
    # "agentsmon setup/service", not "agentsmon dashboard".)
    if shutil.which("pkill"):
        subprocess.run(["pkill", "-f", "agentsmon dashboard"], capture_output=True)
        # WAIT until the old dashboard is actually gone (and its port freed) before relaunching.
        # Otherwise the launcher's pgrep guard still sees the dying process (so it skips starting a
        # fresh one), or the new one hits "address already in use" and exits — either way the STALE
        # pre-config dashboard keeps serving and changes like HTTP auth never take effect.
        for i in range(25):
            gone = subprocess.run(["pgrep", "-f", "agentsmon dashboard"],
                                  capture_output=True).returncode != 0
            if gone:
                break
            if i == 15:                       # stubborn → escalate to SIGKILL
                subprocess.run(["pkill", "-9", "-f", "agentsmon dashboard"], capture_output=True)
            time.sleep(0.3)
    # Kick it once now so the dashboard comes up immediately on the configured host.
    subprocess.run(["sh", str(launcher)], capture_output=True)
    print("  ✓ installed cron launcher (@reboot + every minute) — survives logout/reboot.")
    print(f"    launcher: {launcher}")
    print("    No systemd/launchd needed; works headless over SSH.")
    return 0


def uninstall_cron() -> None:
    """Remove our crontab lines (used by the uninstaller)."""
    try:
        existing = subprocess.run(["crontab", "-l"], capture_output=True, text=True).stdout
    except OSError:
        return
    kept = [ln for ln in existing.splitlines() if MARKER not in ln]
    subprocess.run(["crontab", "-"], input="\n".join(kept) + ("\n" if kept else ""), text=True,
                   capture_output=True)


def main() -> int:
    return install()
