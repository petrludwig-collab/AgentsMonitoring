"""`agentsmon uninstall` — stop the services and remove config + state."""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
from pathlib import Path

from . import config, service


def run(yes: bool = False) -> int:
    print("This stops the keepalive + dashboard services and removes config + state.")
    if not yes:
        try:
            if input("Continue? (y/N): ").strip().lower() not in ("y", "yes"):
                print("Aborted.")
                return 0
        except EOFError:
            print("Aborted — no terminal; rerun with --yes.")
            return 0

    system = platform.system()
    if system == "Darwin":
        uid = os.getuid()
        for label in (service.LABEL_KA, service.LABEL_DB):
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
            p = Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"
            if p.exists():
                p.unlink()
                print(f"  removed {p.name}")
    elif system == "Linux":
        for unit in ("agentsmon-keepalive.service", "agentsmon-dashboard.service"):
            subprocess.run(["systemctl", "--user", "disable", "--now", unit], capture_output=True)
            p = Path.home() / ".config" / "systemd" / "user" / unit
            if p.exists():
                p.unlink()
                print(f"  removed {unit}")

    for d in (config.DEFAULT_PATH.parent, Path.home() / ".local" / "state" / "agentsmon",
              Path.home() / ".agentsmon-src"):
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            print(f"  removed {d}")

    subprocess.run([os.sys.executable, "-m", "pip", "uninstall", "-y", "agents-monitoring"],
                   capture_output=True)
    print("\n✓ Agents Monitoring removed. (Your tmux agents keep running untouched.)")
    return 0
