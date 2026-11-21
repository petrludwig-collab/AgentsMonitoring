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
    # Local check modules: every executable in this folder becomes a service card (exit 0 = up,
    # optional JSON on stdout: {"up", "latency_ms", "detail"}). Header lines "# agentsmon: key=value"
    # may set name / latency_label / timeout_seconds. Nothing here is required or published.
    "modules_dir": "~/.config/agentsmon/modules",
    "plugin_entry_points": [],  # opt-in names in the agentsmon.plugins entry point group
    "usage_plugin": None,       # explicit row -> account/source mapping; no automatic credentials
    "hidden_sessions": [],
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
    cfg["services"] = list(cfg.get("services", [])) + discover_modules(cfg)
    return cfg


MODULE_HEADER = re.compile(r"^\s*(?:#|//|;|--)\s*agentsmon:\s*(.+)$")


def _module_header(path: Path) -> dict:
    """``# agentsmon: name=VPN status, latency_label=round trip`` in the first lines of a
    module → its card settings. Absent = defaults (name from the file name)."""
    out: dict = {}
    try:
        with open(path, encoding="utf-8", errors="ignore") as f:
            for _ in range(10):
                line = f.readline()
                if not line:
                    break
                m = MODULE_HEADER.match(line)
                if not m:
                    continue
                for part in m.group(1).split(","):
                    if "=" in part:
                        k, v = part.split("=", 1)
                        out[k.strip()] = v.strip()
    except OSError:
        pass
    return out


def discover_modules(cfg: dict) -> list[dict]:
    """Executable files in ``modules_dir`` as service entries. Files starting with ``_`` or ``.``
    are helpers and ignored; a module whose name already exists in ``services`` is skipped, so a
    hand-written entry always wins."""
    d = Path(_expand(cfg.get("modules_dir") or ""))
    if not str(d) or not d.is_dir():
        return []
    taken = {s.get("name") for s in cfg.get("services", []) if s.get("name")}
    found = []
    for f in sorted(d.iterdir()):
        if f.name.endswith('.agentsmon.json'):
            continue
        if not f.is_file() or f.name.startswith((".", "_")) or not os.access(f, os.X_OK):
            continue
        hdr = _module_header(f)
        name = hdr.get("name") or f.stem.replace("_", " ").replace("-", " ").strip().title()
        if name in taken:
            continue
        entry = {"name": name, "command": str(f), "source": "module"}
        if hdr.get("latency_label"):
            entry["latency_label"] = hdr["latency_label"]
        if hdr.get("timeout_seconds"):
            try:
                entry["timeout_seconds"] = float(hdr["timeout_seconds"])
            except ValueError:
                pass
        if hdr.get("metric"):
            entry["metric"] = hdr["metric"]
        found.append(entry)
        taken.add(name)
    return found


def save(cfg: dict, path: Path | None = None) -> Path:
    path = path or DEFAULT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def enabled_error(entry: dict) -> str | None:
    """Why an agent's or daemon's ``enabled`` cannot be read, or None when it is JSON true/false
    or absent. ``"false"`` is not false and ``1`` is not true: such an entry is neither on nor off,
    so callers do not restart it and report this message (it names the JSON type, not the value)."""
    value = entry.get("enabled", True)
    if isinstance(value, bool):
        return None
    kind = ("null" if value is None else "a string" if isinstance(value, str)
            else "a number" if isinstance(value, (int, float)) else "a list" if isinstance(value, list)
            else "an object" if isinstance(value, dict) else type(value).__name__)
    return f"'enabled' must be true or false, not {kind}"


def agent_matches(cfg: dict) -> list[tuple]:
    """Turn config agents' ``match`` keywords into (kind, label, compiled-regex) for the classifier."""
    out = []
    for a in cfg.get("agents", []):
        kw = a.get("match")
        if kw:
            out.append((a.get("name", kw), a.get("label", a.get("name", kw)),
                        re.compile(re.escape(kw))))
    return out
