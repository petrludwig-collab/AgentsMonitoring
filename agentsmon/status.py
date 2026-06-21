"""Terminal status view — `agentsmon status`."""
from __future__ import annotations

from . import config, detect


def _fmt_age(sec) -> str:
    if sec is None:
        return "?"
    d, r = divmod(int(sec), 86400)
    h, r = divmod(r, 3600)
    m = r // 60
    if d:
        return f"{d}d {h}h"
    if h:
        return f"{h}h {m}m"
    return f"{m}m"


def render(cfg: dict | None = None) -> str:
    cfg = cfg or config.load()
    agents = detect.discover_agents(config.agent_matches(cfg))
    daemons = detect.daemon_status(cfg.get("daemons", []))
    lines = ["", "  AGENTS (tmux)"]
    if not agents:
        lines.append("    (no tmux sessions found — is tmux running?)")
    for a in agents:
        dot = "🟢" if a["alive"] else "⚪"
        sid = f"  [{a['session_id'][:8]}]" if a.get("session_id") else ""
        lines.append(f"    {dot} {a['name']:<28} {a['label']:<14} age {_fmt_age(a['age'])}{sid}")
    if daemons:
        lines += ["", "  DAEMONS"]
        for d in daemons:
            dot = "🟢" if d["up"] else "🔴"
            extra = ""
            if "http_ok" in d:
                extra = f"  (proc {'ok' if d['process_up'] else 'down'}, http {'ok' if d['http_ok'] else 'down'})"
            lines.append(f"    {dot} {d['name']:<28}{extra}")
    lines.append("")
    return "\n".join(lines)
