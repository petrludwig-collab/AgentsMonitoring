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
    {"name": "OpenClaw", "pattern": "openclaw", "binary": "openclaw", "name_color": "red",
     "health_url": "http://127.0.0.1:18789/health",
     "restart": "nohup openclaw gateway > ~/openclaw.log 2>&1 &"},
    {"name": "Hermes", "pattern": "hermes.* gateway", "binary": "hermes", "name_color": "gold",
     "restart": "nohup hermes gateway run --replace > ~/hermes.log 2>&1 &"},
]


def _running(pattern: str) -> bool:
    return bool(pattern) and subprocess.run(["pgrep", "-f", pattern],
                                            capture_output=True).returncode == 0


def _bridge_restart_cmd() -> str | None:
    """Capture the running Agent2Telegram bridge → a nohup restart command. Critically we also
    capture the env it relies on (PYTHONPATH for a run-from-clone install, AGENT2TELEGRAM_CONFIG)
    from /proc/<pid>/environ — the command line alone misses those, so the restart would fail
    with 'No module named agent2telegram' after a reboot."""
    out = subprocess.run(["pgrep", "-af", "agent2telegram run"], capture_output=True, text=True)
    for line in out.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) != 2 or "agent2telegram run" not in parts[1]:
            continue
        pid, cmd = parts[0], parts[1]
        env_prefix = ""
        try:
            raw = Path(f"/proc/{pid}/environ").read_text("utf-8").split("\0")
            envd = dict(e.split("=", 1) for e in raw if "=" in e)
            for k in ("PYTHONPATH", "AGENT2TELEGRAM_CONFIG"):
                if envd.get(k):
                    env_prefix += f'{k}="{envd[k]}" '
        except (OSError, ValueError):
            pass
        log = "$HOME/.local/state/agentsmon/bridge.log"
        return f"nohup {env_prefix}{cmd} >> {log} 2>&1 &"
    return None


def _telegram_bridge_service() -> dict | None:
    """If an Agent2Telegram bridge is running, build a 'Telegram Bridge Status' availability card.
    Latency = round-trip to the Telegram API. We deliberately probe a **token-less** endpoint so
    no bot token is ever written into this tool's config (it would leak via greps/screenshots)."""
    if not _running("agent2telegram run"):
        return None
    return {"name": "Telegram Bridge Status", "process": "agent2telegram run",
            "health_url": "https://api.telegram.org/"}


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


def _agent_entry(a: dict) -> dict:
    return {"name": a["name"], "label": a["label"],
            "match": MATCH_KEYWORD.get(a["kind"], a["kind"]),
            "restart": _auto_restart(a),
            "cwd": detect._session_cwd(a["name"]) or str(Path.home()),
            "enabled": True}


def _daemon_entries(d: dict) -> tuple:
    """(keepalive daemon, pinned Persistent-Agents row, availability service) for a daemon."""
    daemon = dict(d)
    pinned = {"name": d["name"], "process": d["pattern"]}
    service = {"name": d["name"], "process": d["pattern"]}
    if d.get("health_url"):
        pinned["health_url"] = d["health_url"]
        service["health_url"] = d["health_url"]
    if d.get("name_color"):
        pinned["name_color"] = d["name_color"]
    model = detect.daemon_model(d["name"])
    if model:
        pinned["tag"] = model
        v = detect.vendor_for_model(model)
        if v:
            pinned["vendor"] = v
    return daemon, pinned, service


def _scan_candidates(known: set) -> list:
    """tmux agents (running) + known daemons (running or installed), excluding names in *known*."""
    out = []
    for a in (x for x in detect.discover_agents() if x["alive"]):
        if a["name"] not in known:
            out.append({"kind": "agent", "obj": a, "display": f"{a['name']}  →  {a['label']}"})
    for d in COMMON_DAEMONS:
        running = _running(d["pattern"])
        if (running or shutil.which(d.get("binary", ""))) and d["name"] not in known:
            out.append({"kind": "daemon", "obj": d,
                        "display": f"{d['name']}  (daemon{'' if running else ', not running'})"})
    return out


def add() -> int:
    """`agentsmon add` — detect agents/daemons not yet monitored and add them, no full re-setup."""
    if not config.DEFAULT_PATH.exists():
        print("No config yet — run 'agentsmon setup' first.")
        return 1
    cfg = config.load()
    known = set()
    for key in ("agents", "daemons", "services", "pinned_daemons"):
        known |= {x.get("name") for x in cfg.get(key, []) if x.get("name")}
    candidates = _scan_candidates(known)
    tb = _telegram_bridge_service()
    if tb and tb["name"] not in known:
        candidates.append({"kind": "bridge", "obj": tb, "display": "Telegram Bridge Status"})
    if not candidates:
        print("Nothing new — everything detected is already monitored. ✓")
        return 0
    print("New (not yet monitored). Select which to add:\n")
    for i, c in enumerate(candidates, 1):
        print(f"  [{i}] {c['display']}")
    chosen = _parse_selection(_ask("\nNumbers, 'all', or 'none'", "all"), len(candidates))
    added = 0
    for i, c in enumerate(candidates, 1):
        if i not in chosen:
            continue
        if c["kind"] == "agent":
            cfg.setdefault("agents", []).append(_agent_entry(c["obj"]))
        elif c["kind"] == "daemon":
            dmn, pin, svc = _daemon_entries(c["obj"])
            cfg.setdefault("daemons", []).append(dmn)
            cfg.setdefault("pinned_daemons", []).append(pin)
            cfg.setdefault("services", []).append(svc)
        elif c["kind"] == "bridge":
            cfg.setdefault("services", []).append(c["obj"])
            r = _bridge_restart_cmd()
            if r:
                cfg.setdefault("daemons", []).append({"name": "Telegram Bridge",
                                                      "pattern": "agent2telegram run", "restart": r})
        added += 1
    if not added:
        print("Nothing selected.")
        return 0
    config.save(cfg)
    print(f"\n✓ Added {added}. Reloading the boot service + dashboard…")
    service.install()
    print("Done — check:  agentsmon status")
    return 0


def run() -> int:
    print("=== Agents Monitoring setup ===\n")
    if not shutil.which("tmux"):
        print("⚠️  tmux not found — agents run inside tmux, so install tmux first.")
    print("Scanning for agents and daemons…\n")
    candidates = _scan_candidates(set())   # everything (fresh setup)

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
            agents.append(_agent_entry(c["obj"]))
        else:
            daemons.append(c["obj"])
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

    # Build the full dashboard by default (the layout we run ourselves): each selected daemon
    # becomes a keepalive target, a row at the top of Persistent Agents, AND its own availability
    # card. tmux agents already carry their maker colour automatically.
    cfg["agents"] = agents
    cfg["daemons"], cfg["pinned_daemons"], cfg["services"] = [], [], []
    for d in daemons:
        dmn, pin, svc = _daemon_entries(d)
        cfg["daemons"].append(dmn)
        cfg["pinned_daemons"].append(pin)
        cfg["services"].append(svc)
    # Auto-add a Telegram Bridge availability card if an Agent2Telegram bridge is running,
    # AND keep it alive (restart from its current command line, so it returns after a reboot).
    tb = _telegram_bridge_service()
    if tb:
        cfg["services"].append(tb)
        restart = _bridge_restart_cmd()
        if restart:
            cfg["daemons"].append({"name": "Telegram Bridge", "pattern": "agent2telegram run",
                                   "restart": restart})
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
