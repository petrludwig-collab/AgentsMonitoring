"""Auto-detection of running agents and daemons — the heart of the tool.

We never ask the user to declare what they have; we look. Agents run inside **tmux** sessions,
so we enumerate sessions, walk each session's process tree, and classify what's running by the
command line (claude / codex / a generic match). Background **daemons** (OpenClaw, Hermes, …)
aren't in tmux, so we detect those by a process pattern (and optionally an HTTP health URL).

Pure standard library: `tmux` + `ps` via subprocess, no third-party deps.
"""
from __future__ import annotations

import glob
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

#: Built-in classifiers: (kind, label, regex over the process command line). First match wins.
#: Users can add more via config ``agents[].match``; these cover the common CLIs out of the box.
KNOWN_AGENTS = [
    ("claude-code", "Claude Code", re.compile(r"(?:^|/)claude(?:\s|$)")),
    ("codex", "Codex", re.compile(r"(?:^|/)codex(?:\s|$|\sexec\b)")),
    ("antigravity", "Antigravity", re.compile(r"(?:^|/)agy(?:\s|$)")),
    ("aider", "Aider", re.compile(r"(?:^|/)aider(?:\s|$)")),
    ("gemini", "Gemini CLI", re.compile(r"(?:^|/)gemini(?:\s|$)")),
]

#: Maps a detected kind to the maker, which colours its tag in the UI (anthropic=orange,
#: openai=emerald, google=violet, other=slate).
KIND_VENDOR = {"claude-code": "anthropic", "codex": "openai", "gemini": "google",
               "antigravity": "google", "aider": "other"}

#: How to resume a session per kind ({id} → session id). Shown as a hover tooltip on the id.
RESUME_TEMPLATES = {
    "claude-code": "claude --resume {id}",
    "codex": "codex resume {id}",
    "antigravity": "agy --conversation {id}",
}

#: Login shells — a tmux session running only these has no agent (it's idle).
SHELLS = {"bash", "-bash", "zsh", "-zsh", "sh", "-sh", "fish", "-fish", "tmux"}


def _etime_to_secs(s: str) -> int | None:
    """Parse `ps -o etime` ([[dd-]hh:]mm:ss) into seconds."""
    s = s.strip()
    if not s:
        return None
    days = 0
    if "-" in s:
        d, s = s.split("-", 1)
        days = int(d)
    parts = [int(x) for x in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return days * 86400 + parts[-3] * 3600 + parts[-2] * 60 + parts[-1]


def _proc_age(pid: int) -> int | None:
    r = _run(["ps", "-p", str(pid), "-o", "etime="])
    if not r or r.returncode != 0:
        return None
    try:
        return _etime_to_secs(r.stdout.strip())
    except (ValueError, IndexError):
        return None


def pinned_agents(pinned: list[dict]) -> list[dict]:
    """Non-tmux processes (OpenClaw, Hermes, …) shown at the top of the agents table. If a daemon
    has a ``health_url`` we measure its warm round-trip latency, shown in place of the status."""
    from . import probe
    out = []
    for d in pinned:
        pat = d.get("process", "")
        r = _run(["pgrep", "-f", pat]) if pat else None
        pids = [int(x) for x in r.stdout.split()] if (r and r.returncode == 0) else []
        ages = [a for a in (_proc_age(p) for p in pids) if a is not None]
        age = max(ages) if ages else None    # oldest matching process = how long the service has been up
        lat = None
        if d.get("health_url") and pids:
            ok, secs = probe._http(d["health_url"], timeout=2)
            lat = round(secs * 1000) if secs is not None else None
        # Concrete model detected LIVE (so it stays current without rebuilding config); an
        # explicit config tag/vendor still wins if set.
        model = daemon_model(d.get("name", ""))
        out.append({
            "name": d.get("name"), "kind": "daemon",
            "label": d.get("tag") or model or d.get("name"),
            "vendor": d.get("vendor") or vendor_for_model(model),
            "name_color": d.get("name_color"),
            "session_id": None, "alive": bool(pids), "age": age, "latency_ms": lat,
            "health_url": d.get("health_url"),
        })
    return out


def _tmux_bin() -> str:
    return shutil.which("tmux") or "tmux"


def _run(args, timeout: float = 8):
    try:
        return subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (subprocess.SubprocessError, OSError):
        return None


def tmux_sessions() -> list[dict]:
    """All tmux sessions with their creation epoch (empty list if tmux/server absent)."""
    r = _run([_tmux_bin(), "list-sessions", "-F", "#{session_name}\t#{session_created}"])
    if not r or r.returncode != 0:
        return []
    out = []
    for line in r.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        created = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        out.append({"name": parts[0], "created": created})
    return out


def _pane_pids(session: str) -> list[int]:
    r = _run([_tmux_bin(), "list-panes", "-t", session, "-F", "#{pane_pid}"])
    if not r or r.returncode != 0:
        return []
    return [int(x) for x in r.stdout.split() if x.isdigit()]


def _proc_table() -> tuple[dict, dict]:
    """Return ({pid: command}, {ppid: [child pids]}) for the whole machine."""
    procs: dict[int, str] = {}
    children: dict[int, list[int]] = {}
    r = _run(["ps", "-axo", "pid=,ppid=,command="])
    if not r:
        return procs, children
    for line in r.stdout.splitlines():
        m = re.match(r"\s*(\d+)\s+(\d+)\s+(.*)", line)
        if not m:
            continue
        pid, ppid, cmd = int(m.group(1)), int(m.group(2)), m.group(3)
        procs[pid] = cmd
        children.setdefault(ppid, []).append(pid)
    return procs, children


def _subtree(roots, children) -> set[int]:
    seen: set[int] = set()
    stack = list(roots)
    while stack:
        p = stack.pop()
        if p in seen:
            continue
        seen.add(p)
        stack.extend(children.get(p, []))
    return seen


def _session_cwd(name: str) -> str | None:
    r = _run([_tmux_bin(), "display-message", "-p", "-t", name, "#{pane_current_path}"])
    return r.stdout.strip() if (r and r.returncode == 0 and r.stdout.strip()) else None


def _rollout_model(path: str) -> str | None:
    """The model a Codex session actually ran, from its rollout (turn_context records it even
    when ~/.codex/config.toml doesn't set one). Reads only the head of the file."""
    try:
        with open(path, encoding="utf-8") as fh:
            head = "".join(fh.readline() for _ in range(60))
    except OSError:
        return None
    found = re.findall(r'"model"\s*:\s*"([^"]+)"', head)   # exact "model" key, not model_provider
    return _pretty_model(found[-1]) if found else None


def _codex_info_for_cwd(cwd: str) -> tuple[str | None, str | None]:
    """Find the Codex session whose recorded cwd matches → (session UUID, concrete model)."""
    base = Path.home() / ".codex" / "sessions"
    if not cwd or not base.is_dir():
        return None, None
    target = os.path.realpath(cwd)
    files = glob.glob(str(base / "**" / "rollout-*.jsonl"), recursive=True)
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    for f in files[:300]:
        try:
            with open(f, encoding="utf-8") as fh:
                d = json.loads(fh.readline())
        except (OSError, ValueError):
            continue
        c = d.get("cwd") or (d.get("payload") or {}).get("cwd")
        if c and os.path.realpath(c) == target:
            m = UUID_RE.search(os.path.basename(f))
            return (m.group(0) if m else None), _rollout_model(f)
    return None, None


def _codex_model_any() -> str | None:
    """Model from the most recent Codex rollout (used for daemons like Hermes that run on the
    Codex provider but don't store the model themselves)."""
    base = Path.home() / ".codex" / "sessions"
    if not base.is_dir():
        return None
    files = glob.glob(str(base / "**" / "rollout-*.jsonl"), recursive=True)
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    for f in files[:5]:
        m = _rollout_model(f)
        if m:
            return m
    return None


def _codex_session_for_cwd(cwd: str) -> str | None:
    return _codex_info_for_cwd(cwd)[0]


def _claude_session_for_cwd(cwd: str) -> str | None:
    """Find the Claude Code session whose recorded cwd matches → session UUID. A freshly launched
    `claude` (no ``--resume`` on its command line) has no id to read from argv, so we resolve it
    from ~/.claude/projects/ — the newest transcript whose ``cwd`` matches this session's dir."""
    base = Path.home() / ".claude" / "projects"
    if not cwd or not base.is_dir():
        return None
    target = os.path.realpath(cwd)
    files = glob.glob(str(base / "**" / "*.jsonl"), recursive=True)
    files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
    for f in files[:300]:
        try:
            with open(f, encoding="utf-8") as fh:
                head = [fh.readline() for _ in range(5)]
        except OSError:
            continue
        for line in head:
            try:
                c = json.loads(line).get("cwd")
            except ValueError:
                continue
            if c and os.path.realpath(c) == target:
                m = UUID_RE.search(os.path.basename(f))
                return m.group(0) if m else Path(f).stem
    return None


def _classify(cmds: list[str], extra_matches: list[tuple]) -> tuple[str, str, str | None]:
    """Given the command lines in a session's process tree, return (kind, label, session_id).
    Built-in agents are matched FIRST so we get the real kind (and its maker colour); the
    user-supplied matches are only a fallback for agents we don't recognise out of the box."""
    for cmd in cmds:
        for kind, label, pat in KNOWN_AGENTS:
            if pat.search(cmd):
                sid = UUID_RE.search(cmd)
                return kind, label, (sid.group(0) if sid else None)
        for kind, label, pat in extra_matches:
            if pat.search(cmd):
                sid = UUID_RE.search(cmd)
                return kind, label, (sid.group(0) if sid else None)
    return "shell", "shell (idle)", None


def _pretty_model(raw: str) -> str:
    """'openai/gpt-5.5' → 'GPT-5.5'; leaves other model names readable."""
    raw = raw.split("/")[-1]
    return raw.upper() if raw[:1].lower() in ("g", "o") else raw


def _codex_model() -> str | None:
    """Best-effort concrete model for Codex, from ~/.codex/config.toml (e.g. 'GPT-5.5')."""
    try:
        txt = (Path.home() / ".codex" / "config.toml").read_text(encoding="utf-8")
    except OSError:
        return None
    m = re.search(r'(?m)^\s*model\s*=\s*["\']?([A-Za-z0-9._/-]+)', txt)
    return _pretty_model(m.group(1)) if m else None


def _openclaw_model() -> str | None:
    """OpenClaw's model from openclaw.json. The key may be a string ('model': 'openai/gpt-5.5')
    or an object ('model': {'primary': 'openai/gpt-5.5'}) — parse JSON and handle both, with a
    recursive fallback to any model-looking string under model/primary/name keys."""
    try:
        d = json.loads((Path.home() / ".openclaw" / "openclaw.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    m = d.get("model")
    if isinstance(m, str):
        return _pretty_model(m)
    if isinstance(m, dict):
        for k in ("primary", "name", "default", "model"):
            if isinstance(m.get(k), str):
                return _pretty_model(m[k])

    def _looks_like_model(v):
        return isinstance(v, str) and ("/" in v or any(
            x in v.lower() for x in ("gpt", "claude", "gemini", "opus", "sonnet", "haiku")))

    found = []

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in ("primary", "model", "name") and _looks_like_model(v):
                    found.append(v)
                walk(v)
        elif isinstance(o, list):
            for x in o:
                walk(x)
    walk(d)
    return _pretty_model(found[0]) if found else None


def daemon_model(name: str) -> str | None:
    """Concrete model a known daemon runs (best-effort, for the tag)."""
    n = name.lower()
    if "openclaw" in n:
        return _openclaw_model()
    if "hermes" in n:
        # Hermes here runs on the openai-codex provider → same model as Codex.
        return _codex_model() or _codex_model_any()
    return None


def vendor_for_model(model: str | None) -> str | None:
    """Maker → tag colour, inferred from a model name."""
    if not model:
        return None
    m = model.lower()
    if "gpt" in m or m[:1] == "o":
        return "openai"
    if any(k in m for k in ("claude", "opus", "sonnet", "haiku")):
        return "anthropic"
    if "gemini" in m:
        return "google"
    return None


def discover_agents(extra_matches: list[tuple] | None = None, now: float | None = None) -> list[dict]:
    """Every tmux session classified as a running agent (or an idle shell)."""
    now = now or time.time()
    extra_matches = extra_matches or []
    procs, children = _proc_table()
    codex_model = _codex_model()
    agents = []
    for s in tmux_sessions():
        pids = _pane_pids(s["name"])
        tree = _subtree(pids, children)
        cmds = [procs[p] for p in tree if p in procs]
        # Prefer non-shell commands when classifying.
        ranked = sorted(cmds, key=lambda c: c.split()[0].rsplit("/", 1)[-1] in SHELLS)
        kind, label, sid = _classify(ranked, extra_matches)
        # For Codex: resolve the session id + the concrete model from its rollout (the rollout
        # records the model even when ~/.codex/config.toml doesn't); show the model as the label.
        if kind == "codex":
            cwd = _session_cwd(s["name"])
            rsid, rmodel = _codex_info_for_cwd(cwd) if cwd else (None, None)
            if sid is None:
                sid = rsid
            model = rmodel or codex_model
            if model:
                label = model
        # Claude Code: a fresh `claude` has no id on its command line — resolve it by cwd so the
        # session id (and a working --resume) still show up.
        if kind == "claude-code" and sid is None:
            cwd = _session_cwd(s["name"])
            if cwd:
                sid = _claude_session_for_cwd(cwd)
        age = int(now - s["created"]) if s["created"] else None
        resume = RESUME_TEMPLATES.get(kind, "").format(id=sid) if (sid and kind in RESUME_TEMPLATES) else None
        agents.append({
            "name": s["name"], "kind": kind, "label": label, "session_id": sid,
            "vendor": KIND_VENDOR.get(kind), "alive": kind != "shell", "age": age,
            "resume_cmd": resume, "pids": sorted(tree),
        })
    return agents


def _tokens_under_telegram(obj, in_tg: bool = False) -> list[str]:
    """Bot-token-shaped strings (``<digits>:<secret>``) located anywhere under a key containing
    'telegram' — so we pick up the Telegram bot token however it's nested, but never an unrelated
    token (e.g. a gateway secret) elsewhere in the config."""
    found: list[str] = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            found += _tokens_under_telegram(v, in_tg or ("telegram" in str(k).lower()))
    elif isinstance(obj, list):
        for v in obj:
            found += _tokens_under_telegram(v, in_tg)
    elif in_tg and isinstance(obj, str) and re.match(r"\d{6,}:[A-Za-z0-9_-]{30,}$", obj):
        found.append(obj)
    return found


def _openclaw_telegram_bot() -> str:
    """OpenClaw's OWN Telegram bot @username (it doesn't use Agent2Telegram), resolved from its
    config via getMe — searching wherever the token lives under a 'telegram' key. Best-effort,
    called once at setup / migration, not per render."""
    try:
        d = json.loads((Path.home() / ".openclaw" / "openclaw.json").read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    seen: set[str] = set()
    for tok in _tokens_under_telegram(d):
        if tok in seen:
            continue
        seen.add(tok)
        u = _getme_username(tok)
        if u:
            return u
    return ""


def _getme_username(token: str) -> str:
    try:
        with urllib.request.urlopen(f"https://api.telegram.org/bot{token}/getMe", timeout=4) as r:
            return json.loads(r.read()).get("result", {}).get("username", "") or ""
    except Exception:
        return ""


def _hermes_telegram_bot() -> str:
    """Hermes' own Telegram bot @username, resolved from its ~/.hermes/.env token. Best-effort."""
    try:
        env = (Path.home() / ".hermes" / ".env").read_text("utf-8")
    except OSError:
        return ""
    m = re.findall(r'(?im)^[^#\n]*(?:TELEGRAM|BOT_?TOKEN)[^\n]*?=\s*["\']?(\d{6,}:[A-Za-z0-9_-]{30,})', env)
    return _getme_username(m[0]) if m else ""


def daemon_telegram_bot(name: str) -> str:
    """A known daemon's OWN Telegram bot @username (not via Agent2Telegram). '' if not resolvable.
    Called at setup / config migration, never per render."""
    if name == "OpenClaw":
        return _openclaw_telegram_bot()
    if name == "Hermes":
        return _hermes_telegram_bot()
    return ""


def telegram_links() -> dict[str, str]:
    """Map tmux-session name → bot @username for any agent connected to Telegram via Agent2Telegram.

    This is an OPTIONAL, soft integration — not a dependency. The agent↔bot mapping only exists in
    the bridge's own config, so we read it from there (``~/.config/agent2telegram/*.json``), taking
    ONLY the non-secret ``bot_username`` (never the token). If Agent2Telegram isn't installed, or a
    bridge predates the username field, the map is just empty and no link is shown — nothing breaks.
    """
    out: dict[str, str] = {}
    base = Path.home() / ".config" / "agent2telegram"
    if not base.is_dir():
        return out
    for p in base.glob("*.json"):
        try:
            d = json.loads(p.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        sess, user = d.get("tmux_session"), d.get("bot_username")
        if sess and user:
            out[sess] = user
    return out


def _http_ok(url: str, timeout: float = 4) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return 200 <= resp.status < 400
    except Exception:
        return False


def daemon_status(daemons: list[dict]) -> list[dict]:
    """For each configured daemon ({name, pattern, health_url?}), is it running / healthy?"""
    out = []
    for d in daemons:
        pat = d.get("pattern", "")
        proc_up = bool(pat) and _run(["pgrep", "-f", pat]) is not None and \
            _run(["pgrep", "-f", pat]).returncode == 0
        entry = {"name": d.get("name", pat), "pattern": pat, "process_up": proc_up}
        url = d.get("health_url")
        if url:
            entry["http_ok"] = _http_ok(url)
            entry["up"] = proc_up and entry["http_ok"]
        else:
            entry["up"] = proc_up
        out.append(entry)
    return out
