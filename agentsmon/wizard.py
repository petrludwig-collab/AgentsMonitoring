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
    {"name": "OpenClaw", "pattern": "openclaw", "health_url": "http://127.0.0.1:18789/health"},
    {"name": "Hermes", "pattern": "hermes"},
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
                daemons.append(d)

    port = _ask("\nDashboard port", "8765")
    host = _ask("Dashboard host (127.0.0.1 = this machine only)", "127.0.0.1")

    cfg = config.load()
    cfg["agents"] = agents
    cfg["daemons"] = daemons
    cfg["dashboard"].update({"host": host, "port": int(port) if port.isdigit() else 8765})
    path = config.save(cfg)
    print(f"\n✓ Saved config to {path}")
    print(f"  Supervising {len(agents)} agent(s), watching {len(daemons)} daemon(s).")

    if _yes("\nInstall the boot service now (keepalive + dashboard, start on login/boot)?"):
        service.install()
    print("\nAll set. Check status anytime with:  agentsmon status")
    print(f"Dashboard: http://{host}:{port}")
    return 0
