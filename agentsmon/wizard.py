"""Setup wizard — `agentsmon setup`.

Auto-detects the agents already running in tmux, lets you choose which to supervise, proposes a
restart command for each, optionally watches common daemons (OpenClaw, Hermes), writes the
config, and installs the boot service. Designed to need almost no typing.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from . import config, detect, service

#: Auto-derived restart command per kind ({id} = session id). Includes the "run unattended" flag,
#: since a supervised agent must come back able to work without an approval prompt.
RESTART_DEFAULTS = {
    "claude-code": "claude --dangerously-skip-permissions --resume {id}",
    "codex": "codex --dangerously-bypass-approvals-and-sandbox resume {id}",
    "antigravity": "agy --conversation {id} --dangerously-skip-permissions",
    "aider": "aider",
    "gemini": "gemini",
}
MATCH_KEYWORD = {"claude-code": "claude", "codex": "codex", "antigravity": "agy",
                 "aider": "aider", "gemini": "gemini"}


def _auto_restart(a: dict) -> str:
    """Build the restart command for a detected agent — no user typing needed."""
    tpl = RESTART_DEFAULTS.get(a["kind"], "")
    if not tpl:
        return ""
    sid = a.get("session_id")
    if sid:
        return tpl.replace("{id}", sid)
    # No session id → drop the resume/conversation argument, keep the base launch.
    return re.sub(r"\s*(--resume|resume|--conversation)\s*\{id\}", "", tpl).strip()


def primary_ip() -> str:
    """This machine's primary outbound IP — the usable address when the dashboard is exposed
    (``0.0.0.0``). Falls back to localhost if offline."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
COMMON_DAEMONS = [
    {"name": "OpenClaw", "pattern": "openclaw", "binary": "openclaw",
     "health_url": "http://127.0.0.1:18789/health",
     "restart": "nohup openclaw gateway > ~/openclaw.log 2>&1 &"},
    {"name": "Hermes", "pattern": "hermes_cli.main gateway", "binary": "hermes",
     "restart": "nohup hermes gateway run --replace > ~/hermes.log 2>&1 &"},
]


def _running(pattern: str) -> bool:
    return bool(pattern) and subprocess.run(["pgrep", "-f", pattern],
                                            capture_output=True).returncode == 0


def _parse_selection(text: str, n: int) -> set:
    """Parse a checklist answer: '' or 'all' → everything, 'none' → nothing, else the listed
    numbers (comma/space separated)."""
    t = text.strip().lower()
    if t in ("", "all", "a"):
        return set(range(1, n + 1))
    if t in ("none", "n", "-"):
        return set()
    out = set()
    for part in t.replace(",", " ").split():
        if part.isdigit() and 1 <= int(part) <= n:
            out.add(int(part))
    return out


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
    print("Scanning for agents and daemons…\n")
    # tmux agents (running) + known daemons (running, or installed but currently down).
    candidates = []
    for a in (x for x in detect.discover_agents() if x["alive"]):
        candidates.append({"kind": "agent", "obj": a,
                           "display": f"{a['name']}  →  {a['label']}"})
    for d in COMMON_DAEMONS:
        running = _running(d["pattern"])
        if running or shutil.which(d.get("binary", "")):
            candidates.append({"kind": "daemon", "obj": d,
                               "display": f"{d['name']}  (daemon{'' if running else ', not running'})"})

    chosen: set = set()
    if not candidates:
        print("  No running agents or daemons found.")
        print("  (Start your agents in tmux first, then re-run setup.)")
    else:
        print("Found the following. Select which to monitor + auto-restart:\n")
        for i, c in enumerate(candidates, 1):
            print(f"  [{i}] {c['display']}")
        print()
        sel = _ask("Numbers to include (comma-separated), 'all', or 'none'", "all")
        chosen = _parse_selection(sel, len(candidates))

    agents, daemons = [], []
    for i, c in enumerate(candidates, 1):
        if i not in chosen:
            continue
        if c["kind"] == "agent":
            a = c["obj"]
            restart = _auto_restart(a)
            cwd = detect._session_cwd(a["name"]) or str(Path.home())
            agents.append({"name": a["name"], "label": a["label"],
                           "match": MATCH_KEYWORD.get(a["kind"], a["kind"]),
                           "restart": restart, "cwd": cwd, "enabled": True})
        else:
            daemons.append(dict(c["obj"]))
    print(f"\n  → will monitor {len(agents)} agent(s) + {len(daemons)} daemon(s), with auto-restart.")

    # Dashboard reach: localhost always works; ask whether to also expose it on the machine's IP.
    print("\nThe dashboard is always reachable on this machine (http://127.0.0.1).")
    expose = _yes("Also make it reachable from outside — on the server's IP / the internet?",
                  default_yes=False)
    host = "0.0.0.0" if expose else "127.0.0.1"
    port = _ask("Dashboard port", "8765")

    cfg = config.load()
    cfg["dashboard"].update({"host": host, "port": int(port) if port.isdigit() else 8765})
    if expose:
        print("⚠️  Exposed beyond localhost — a login is strongly recommended.")
    # HTTP auth — default yes when exposed.
    if _yes("Protect the dashboard with a login (HTTP auth)?", default_yes=expose):
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
    # Build the full dashboard by default (the layout we run ourselves): each selected daemon
    # becomes both a row at the top of Persistent Agents AND its own availability card with
    # uptime/SLA/latency. tmux agents already carry their maker colour automatically.
    cfg["pinned_daemons"] = [
        {"name": d["name"], "process": d["pattern"],
         **({"health_url": d["health_url"]} if d.get("health_url") else {})}
        for d in daemons]
    cfg["services"] = [
        {"name": d["name"], "process": d["pattern"],
         **({"health_url": d["health_url"]} if d.get("health_url") else {})}
        for d in daemons]
    path = config.save(cfg)
    print(f"\n✓ Saved config to {path}")
    print(f"  Supervising {len(agents)} agent(s), watching {len(daemons)} daemon(s).")

    if _yes("\nInstall the boot service now (keepalive + dashboard, start on login/boot)?"):
        service.install()
    print("\nAll set. Check status anytime with:  agentsmon status")
    if host in ("0.0.0.0", "::"):
        print(f"Dashboard: http://{primary_ip()}:{port}   (local: http://127.0.0.1:{port})")
    else:
        print(f"Dashboard: http://127.0.0.1:{port}")
    return 0
