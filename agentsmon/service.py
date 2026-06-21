"""Boot persistence — install OS services so keepalive + dashboard survive logout/reboot.

macOS → two LaunchAgents (``~/Library/LaunchAgents``, RunAtLoad + KeepAlive).
Linux → two ``systemd --user`` units (with a hint to enable linger for boot-without-login).
Everything runs as the current user; no root needed.
"""
from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

LABEL_KA = "com.agentsmon.keepalive"
LABEL_DB = "com.agentsmon.dashboard"


def _py() -> str:
    return sys.executable or "python3"


def _plist(label: str, args: list[str]) -> str:
    body = "".join(f"      <string>{a}</string>\n" for a in [_py(), "-m", "agentsmon", *args])
    logs = Path.home() / ".local" / "state" / "agentsmon"
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key><array>
{body}  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{logs}/{label}.out.log</string>
  <key>StandardErrorPath</key><string>{logs}/{label}.err.log</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict></plist>
"""


def _systemd_unit(desc: str, args: list[str]) -> str:
    exec_start = " ".join([_py(), "-m", "agentsmon", *args])
    return f"""[Unit]
Description={desc}
After=default.target

[Service]
ExecStart={exec_start}
Restart=always
RestartSec=10
Environment=PATH=/usr/local/bin:/usr/bin:/bin

[Install]
WantedBy=default.target
"""


def install() -> int:
    system = platform.system()
    (Path.home() / ".local" / "state" / "agentsmon").mkdir(parents=True, exist_ok=True)
    if system == "Darwin":
        d = Path.home() / "Library" / "LaunchAgents"
        d.mkdir(parents=True, exist_ok=True)
        for label, args in ((LABEL_KA, ["keepalive", "--loop"]), (LABEL_DB, ["dashboard"])):
            path = d / f"{label}.plist"
            path.write_text(_plist(label, args), encoding="utf-8")
            uid = os.getuid()
            subprocess.run(["launchctl", "bootout", f"gui/{uid}/{label}"], capture_output=True)
            subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", str(path)], capture_output=True)
            print(f"  ✓ loaded {label}")
        print("Installed LaunchAgents (start at login + restart on crash).")
        return 0
    if system == "Linux":
        d = Path.home() / ".config" / "systemd" / "user"
        d.mkdir(parents=True, exist_ok=True)
        for unit, desc, args in (("agentsmon-keepalive.service", "Agents Monitoring keepalive", ["keepalive", "--loop"]),
                                 ("agentsmon-dashboard.service", "Agents Monitoring dashboard", ["dashboard"])):
            (d / unit).write_text(_systemd_unit(desc, args), encoding="utf-8")
            subprocess.run(["systemctl", "--user", "enable", "--now", unit], capture_output=True)
            print(f"  ✓ enabled {unit}")
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        print("Installed systemd --user units. For boot-without-login run:  loginctl enable-linger $USER")
        return 0
    print(f"Unsupported OS '{system}'. Run `agentsmon keepalive --loop` and `agentsmon dashboard` "
          "under your own process manager.")
    return 1


def main() -> int:
    return install()
