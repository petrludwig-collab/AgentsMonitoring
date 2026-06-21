"""Setup wizard — `agentsmon setup`.

Auto-detects the agents already running in tmux, lets you choose which to supervise, proposes a
restart command for each, optionally watches common daemons (OpenClaw, Hermes), writes the
config, and installs the boot service. Designed to need almost no typing.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

from . import config, detect, service

#: Default restart command per detected kind. ``{id}`` is filled with the session id when known.
RESTART_DEFAULTS = {
    "claude-code": "claude --resume {id}",
    "codex": "codex resume {id}",
    "aider": "aider",
    "gemini": "gemini",
}
MATCH_KEYWORD = {"claude-code": "claude", "codex": "codex", "aider": "aider", "gemini": "gemini"}
COMMON_DAEMONS = [
    {"name": "OpenClaw", "pattern": "openclaw", "health_url": "http://127.0.0.1:18789/health",
     "restart": "nohup openclaw gateway > ~/openclaw.log 2>&1 &"},
    {"name": "Hermes", "pattern": "hermes_cli.main gateway",
     "restart": "nohup hermes gateway run --replace > ~/hermes.log 2>&1 &"},
]


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return val or default


def _yes(prompt: str, default_yes: bool = True) -> bool:
    d = "Y/n" if default_yes else "y/N"
    ans = _ask(f"{prompt} ({d})").lower()
    if not ans:
        return default_yes
    return ans in ("y", "yes")


def _ask_secret(prompt: str) -> str:
    import getpass
    try:
        return getpass.getpass(f"{prompt}: ").strip()
    except (EOFError, Exception):
        return _ask(prompt)


def run() -> int:
    print("=== Agents Monitoring setup ===\n")
    if not shutil.which("tmux"):
        print("⚠️  tmux not found — agents run inside tmux, so install tmux first.")
    print("Scanning tmux for running agents…\n")
    found = detect.discover_agents()
    live = [a for a in found if a["alive"]]
    idle = [a for a in found if not a["alive"]]
    for a in live:
        sid = f"  [{a['session_id'][:8]}]" if a.get("session_id") else ""
        print(f"  • {a['name']}  →  {a['label']}{sid}")
    for a in idle:
        print(f"  · {a['name']}  (idle shell)")
    if not found:
        print("  (no tmux sessions found)")
    print()

    agents = []
    for a in live:
        if not _yes(f"Supervise '{a['name']}' ({a['label']})?"):
            continue
        kind = a["kind"]
        match = MATCH_KEYWORD.get(kind, kind)
        default_restart = RESTART_DEFAULTS.get(kind, "")
        if a.get("session_id"):
            default_restart = default_restart.replace("{id}", a["session_id"])
        else:
            default_restart = default_restart.replace(" {id}", "").replace("{id}", "")
        restart = _ask(f"    restart command for '{a['name']}'", default_restart)
        cwd = _ask("    working directory", str(Path.home()))
        agents.append({"name": a["name"], "label": a["label"], "match": match,
                       "restart": restart, "cwd": cwd, "enabled": True})

    daemons = []
    for d in COMMON_DAEMONS:
        if subprocess.run(["pgrep", "-f", d["pattern"]], capture_output=True).returncode == 0:
            if _yes(f"Watch daemon '{d['name']}' (detected running)?"):
                entry = {k: v for k, v in d.items() if k != "restart"}
                # A restart command lets keepalive revive it (and start it on boot); empty = monitor only.
                restart = _ask(f"    restart command for '{d['name']}' if it dies "
                               "(empty = monitor only)", d.get("restart", ""))
                if restart:
                    entry["restart"] = restart
                daemons.append(entry)

    port = _ask("\nDashboard port", "8765")
    host = _ask("Dashboard host (127.0.0.1 = this machine only)", "127.0.0.1")

    # HTTP auth — recommended whenever the dashboard isn't bound to localhost.
    cfg = config.load()
    cfg["dashboard"].update({"host": host, "port": int(port) if port.isdigit() else 8765})
    remote = host not in ("127.0.0.1", "localhost", "::1")
    if _yes("Protect the dashboard with a login (HTTP auth)?", default_yes=remote):
        from . import dashboard
        user = _ask("    username", "admin")
        pw = _ask_secret("    password (hidden)")
        while not pw:
            pw = _ask_secret("    password can't be empty (hidden)")
        cfg["dashboard"]["auth"] = {"user": user, "pwhash": dashboard.password_hash(pw)}
        print("    ✓ HTTP auth enabled (password stored only as a hash).")
    else:
        cfg["dashboard"].pop("auth", None)

    cfg["agents"] = agents
    cfg["daemons"] = daemons
    path = config.save(cfg)
    print(f"\n✓ Saved config to {path}")
    print(f"  Supervising {len(agents)} agent(s), watching {len(daemons)} daemon(s).")

    if _yes("\nInstall the boot service now (keepalive + dashboard, start on login/boot)?"):
        service.install()
    print("\nAll set. Check status anytime with:  agentsmon status")
    print(f"Dashboard: http://{host}:{port}")
    return 0
