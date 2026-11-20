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


def render(cfg: dict | None = None, ignore_pid: int | None = None) -> str:
    cfg = cfg or config.load()
    agents = detect.configured_agents(cfg)
    daemons = detect.daemon_status(cfg.get("daemons", []), ignore_pid=ignore_pid)
    lines = ["", "  AGENTS (tmux)"]
    if not agents:
        lines.append("    (no tmux sessions found — is tmux running?)")
    for a in agents:
        dot = "🟡" if a.get('needs_login') else ("🟢" if a["alive"] else "⚪")
        sid = f"  [{a['session_id'][:8]}]" if a.get("session_id") else ""
        state = f"  {a['status']}" if a.get('status') else ''
        lines.append(f"    {dot} {a['name']:<28} {a['label']:<14} age {_fmt_age(a['age'])}{sid}{state}")
    if daemons:
        lines += ["", "  DAEMONS"]
        for d in daemons:
            dot = "🟢" if d["up"] else "🔴"
            extra = ""
            if "http_ok" in d:
                extra = f"  (proc {'ok' if d['process_up'] else 'down'}, http {'ok' if d['http_ok'] else 'down'})"
            lines.append(f"    {dot} {d['name']:<28}{extra}")
    lines += _dashboard_lines(cfg)
    lines.append("")
    return "\n".join(lines)


def _dashboard_lines(cfg: dict) -> list[str]:
    """Where the web report lives, and whether it is actually up.

    The URL used to be printed once by the setup wizard and never again, so the only way
    to find it later was to read config.json. `agentsmon status` is where people look.
    """
    dcfg = cfg.get("dashboard") or {}
    port = dcfg.get("port", 8765)
    host = str(dcfg.get("host") or "127.0.0.1")
    local = f"http://127.0.0.1:{port}"
    if host in ("", "127.0.0.1", "localhost"):
        url, also = local, ""
    else:
        # 0.0.0.0 means "all interfaces" — show the address someone can actually open.
        shown = _primary_ip() if host == "0.0.0.0" else host
        url, also = f"http://{shown}:{port}", f"   (local: {local})"
    # Probe the address it is actually BOUND to. Probing 127.0.0.1 unconditionally showed a
    # false red whenever the dashboard binds to one specific interface (e.g. a Tailscale IP),
    # which is exactly how it is set up on the machine this was written on.
    probe_host = "127.0.0.1" if host in ("", "0.0.0.0", "localhost") else host
    up = _port_open(probe_host, port)
    dot = "🟢" if up else "🔴"
    out = ["", "  DASHBOARD", f"    {dot} {url}{also}"]
    if not up:
        out.append("       not responding — start it with:  agentsmon dashboard")
    if dcfg.get("auth"):
        out.append(f"       login required (user: {dcfg['auth'].get('user', 'admin')})")
    return out


def _port_open(host: str, port: int) -> bool:
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.4)
            return s.connect_ex((host, int(port))) == 0
    except OSError:
        return False


def _primary_ip() -> str:
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
