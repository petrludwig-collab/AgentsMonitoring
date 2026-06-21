"""Config load/save. One JSON file at ``~/.config/agentsmon/config.json`` (override with
``AGENTSMON_CONFIG``). Everything is optional with sensible defaults so the tool works before
setup, and `setup` just writes the agents/daemons it auto-detected."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

DEFAULT_PATH = Path(os.environ.get("AGENTSMON_CONFIG",
                                   str(Path.home() / ".config" / "agentsmon" / "config.json")))


def state_dir() -> Path:
    d = Path(os.environ.get("AGENTSMON_STATE", str(Path.home() / ".local" / "state" / "agentsmon")))
    d.mkdir(parents=True, exist_ok=True)
    return d


DEFAULTS = {
    "tmux_bin": "tmux",
    # Agents to keep alive. Each: name (tmux session), match (process keyword that means "alive"),
    # restart (shell command to relaunch in a fresh session; empty = just recreate the session),
    # cwd, enabled.
    "agents": [],
    # Background daemons to watch live (not in tmux), no history. Each: name, pattern, health_url?, restart?
    "daemons": [],
    # Services to track with uptime history + SLA + timeline. Each: name, process (pgrep), health_url?
    # Rendered as their own dashboard card (e.g. "Multi-agent system availability", "Telegram Bridge Status").
    "services": [],
    # Non-tmux processes to pin at the TOP of the Persistent-agents table (e.g. OpenClaw, Hermes).
    # Each: name, process (pgrep -f), tag (display label), vendor (anthropic|openai|google for the tag colour).
    "pinned_daemons": [],
    # Name of the agent/daemon that is the orchestrator (the main one) — shown first and highlighted.
    "orchestrator": "",
    "dashboard": {"host": "127.0.0.1", "port": 8765, "poll_seconds": 15},
    "probe": {"interval_seconds": 60, "sla_window_days": 90, "timeline_days": 90, "min_outage_samples": 3},
    "keepalive": {"enabled": True, "interval_seconds": 60},
}


def _expand(obj):
    if isinstance(obj, str):
        return os.path.expanduser(os.path.expandvars(obj))
    if isinstance(obj, list):
        return [_expand(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _expand(v) for k, v in obj.items()}
    return obj


def load(path: Path | None = None) -> dict:
    path = path or DEFAULT_PATH
    cfg = json.loads(json.dumps(DEFAULTS))   # deep copy
    if path.exists():
        user = json.loads(path.read_text("utf-8"))
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    return cfg


def save(cfg: dict, path: Path | None = None) -> Path:
    path = path or DEFAULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def agent_matches(cfg: dict) -> list[tuple]:
    """Turn config agents' ``match`` keywords into (kind, label, compiled-regex) for the classifier."""
    out = []
    for a in cfg.get("agents", []):
        kw = a.get("match")
        if kw:
            out.append((a.get("name", kw), a.get("label", a.get("name", kw)),
                        re.compile(re.escape(kw))))
    return out
