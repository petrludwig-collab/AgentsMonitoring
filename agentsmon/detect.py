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
import shlex
import shutil
import sqlite3
from contextlib import closing
import subprocess
import sys
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
]

#: Maps a detected kind to the maker, which colours its tag in the UI (anthropic=orange,
#: openai=emerald, google=violet, other=slate).
KIND_VENDOR = {"claude-code": "anthropic", "codex": "openai", "antigravity": "google"}

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


def _daemon_port(health_url: str | None) -> int | None:
    """The daemon's gateway port — taken from its configured health_url (reliable + meaningful).
    We deliberately do NOT probe the process with lsof: a gateway often holds several sockets
    (internal/ephemeral) and lsof picks an arbitrary one, which is misleading."""
    if not health_url:
        return None
    m = re.search(r":(\d+)", health_url)
    return int(m.group(1)) if m else None


def pinned_agents(pinned: list[dict]) -> list[dict]:
    """Non-tmux processes (OpenClaw, Hermes, …) shown at the top of the agents table.

    Liveness: a daemon that advertises a ``health_url`` is "up" iff that endpoint answers — NOT
    whether a process-name regex matched. Process command lines vary by install method (venv,
    pip ``--user``, pipx, distro package): e.g. one host runs ``venv/bin/hermes gateway`` while
    another runs ``python -m hermes_cli gateway``, so any single regex silently fails somewhere.
    Worse, a loose pattern can match an unrelated process (an OpenClaw node launched from a
    ``.hermes/node`` path matches ``hermes.*gateway``). The health endpoint sidesteps all of that.
    The ``process`` pattern is only the liveness signal for daemons WITHOUT a ``health_url``;
    when a ``health_url`` is set the pattern is best-effort, used solely to report uptime."""
    from . import probe
    out = []
    for d in pinned:
        pat = d.get("process", "")
        r = _run(["pgrep", "-f", pat]) if pat else None
        pids = [int(x) for x in r.stdout.split()] if (r and r.returncode == 0) else []
        ages = [a for a in (_proc_age(p) for p in pids) if a is not None]
        if d.get('manager'):
            from . import managed
            if d['manager'].get('type') == 'systemd':
                pid = managed.properties(d['manager']['unit']).get('MainPID')
                age = _proc_age(int(pid)) if pid and pid.isdigit() and int(pid)>0 else None
                ages = [age] if age is not None else []
        age = max(ages) if ages else None    # oldest matching process = how long the service has been up
        health_url = d.get("health_url")
        lat = None
        if health_url:
            ok, secs = probe._http(health_url, timeout=2)
            alive = bool(ok)                              # health endpoint is authoritative
            lat = round(secs * 1000) if secs is not None else None
        else:
            if d.get('manager'):
                from . import managed
                alive = managed.active(d['manager']) is True
            else:
                alive = bool(pids)
        # Concrete model detected LIVE (so it stays current without rebuilding config); an
        # explicit config tag/vendor still wins if set.
        model = daemon_model(d.get("name", ""))
        out.append({
            "name": d.get("name"), "kind": "daemon",
            "label": d.get("tag") or model or d.get("name"),
            "vendor": d.get("vendor") or vendor_for_agent(None, model),
            "name_color": d.get("name_color"),
            "session_id": None, "alive": alive, "age": age, "latency_ms": lat,
            "health_url": health_url,
            "port": _daemon_port(health_url) if (health_url and alive) else None,
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
    base = Path(os.environ.get('CODEX_HOME', Path.home()/'.codex')) / 'sessions'
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


def label_for_claude(transcript_model: str | None, cmds: list[str], fallback: str, *, exact_session=True) -> str:
    """The model tag for a Claude Code row — the SINGLE place that decides it.

    A transcript tied to an explicit session ID reports runtime /model switches and wins.
    A cwd-only transcript can belong to another agent, so explicit argv wins over that
    ambiguous estimate. The generic kind label is the last resort.
    """
    if exact_session:
        return transcript_model or _model_from_argv(cmds) or fallback
    return _model_from_argv(cmds) or transcript_model or fallback


def _model_from_argv(cmds: list[str]) -> str | None:
    """The model a session was launched with, straight off its command line.

    A transcript only exists once the session has actually answered something, so a freshly
    started agent showed the generic kind label ("Claude Code") with no model for its first
    minutes. `--model` is on argv from the very first second, so use it when the transcript
    cannot answer yet.
    """
    for cmd in cmds:
        try: parts = shlex.split(cmd)
        except ValueError: continue
        if not parts or os.path.basename(parts[0]) not in ('claude','codex','agy','antigravity'): continue
        for i,part in enumerate(parts):
            if part == '--model' and i+1 < len(parts): return prettify_model(parts[i+1])
            if part.startswith('--model='): return prettify_model(part.split('=',1)[1])
    return None


def prettify_model(raw: str) -> str:
    """``claude-fable-5-1`` → ``Fable 5.1``, ``gemini-3-flash`` → ``Gemini 3 Flash``.

    Routing by family matters: sending everything through the Claude prettifier left Gemini
    ids on screen exactly as the process was launched with them.
    """
    r = raw or ""
    if r.startswith("gemini"):
        return _pretty_gemini_model(r)
    if r.split("/")[-1][:3].lower() in ("gpt", "o1-", "o3-", "o4-") or re.match(r"o\d", r):
        return _pretty_model(r)                  # Codex launched with --model gpt-6-astra
    return _pretty_claude_model(r)


def _pretty_claude_model(raw: str) -> str:
    """``claude-opus-4-8`` → ``Opus 4.8`` (family + version); unknown ids returned as-is."""
    m = re.match(r"claude-(opus|sonnet|haiku|fable)-(\d+)(?:-(\d+))?", raw or "")
    if not m:
        if raw in ('opus','sonnet','haiku','fable'): return raw.capitalize()
        return raw
    ver = m.group(2) + (f".{m.group(3)}" if m.group(3) else "")
    return f"{m.group(1).capitalize()} {ver}"


def _claude_model_from_transcript(path: str) -> str | None:
    """The model a Claude Code session is actually running, from the tail of its transcript (each
    assistant turn records ``message.model``); we take the latest real one, ignoring synthetic."""
    # A young session can end in a large non-assistant record (attachments, tool output), so the
    # tail alone may hold no model at all; widen the window backwards up to a fixed cap.
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            window = 65536
            while True:
                start = max(0, size - window)
                fh.seek(start)
                data = fh.read().decode("utf-8", "ignore")
                for m in reversed(re.findall(r'"model"\s*:\s*"([^"]+)"', data)):
                    if m and m != "<synthetic>":
                        return _pretty_claude_model(m)
                if start == 0 or window >= 8 * 1024 * 1024:
                    return None
                window *= 4
    except OSError:
        return None


def claude_project_dirs(cwd: str) -> list[Path]:
    """Candidate transcript directories for a working directory.

    Claude Code stores a session under ``~/.claude/projects/<cwd with / replaced by ->``:
    ``/Users/me/dev/app`` → ``-Users-me-dev-app``. Both the literal cwd and its resolved form
    are tried, because they differ where the path crosses a symlink — on macOS
    ``/home/x`` resolves to ``/System/Volumes/Data/home/x``, and the directory is named after
    whichever form the agent was started with. Deriving the name forward is exact; reading it
    backwards would be ambiguous, and we never need to.
    """
    base = Path.home() / ".claude" / "projects"
    formy, videne = [], set()
    for c in (cwd, os.path.realpath(cwd)):
        name = c.replace("/", "-")
        if name not in videne:
            videne.add(name)
            formy.append(base / name)
    return formy


def _claude_model_for_session(sid: str) -> str | None:
    if not UUID_RE.fullmatch(sid): return None
    paths = list((Path.home()/'.claude/projects').glob('*/'+sid+'.jsonl'))
    return _claude_model_from_transcript(str(paths[0])) if len(paths)==1 else None


def _claude_info_for_cwd(cwd: str, taken: set | None = None) -> tuple[str | None, str | None]:
    """Session UUID and concrete model for a `claude` started without ``--resume``.

    The transcript FILE is created (and named after the session uuid) the moment the agent
    starts — its first line already carries ``sessionId``. Earlier this scanned transcript
    CONTENT for a matching ``cwd``, which only appears once the session has received a user
    message, so a young agent showed no id and no model for its first minutes. Going through
    the project directory instead makes both available from the first second.

    ``taken`` holds ids already assigned to other agents, so two agents sharing one working
    directory do not both claim the newest transcript.
    """
    if not cwd:
        return None, None
    taken = taken or set()
    for d in claude_project_dirs(cwd):
        if not d.is_dir():
            continue
        for f in sorted(d.glob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
            m = UUID_RE.search(f.name)
            sid = m.group(0) if m else f.stem
            if sid in taken:
                continue
            return sid, _claude_model_from_transcript(str(f))
    # Fallback: older layouts, or a cwd that does not map to a directory we can see.
    base = Path.home() / ".claude" / "projects"
    if not base.is_dir():
        return None, None
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
                sid = m.group(0) if m else Path(f).stem
                if sid in taken:
                    continue
                return sid, _claude_model_from_transcript(f)
    return None, None


def _antigravity_base() -> Path:
    return Path.home() / ".gemini" / "antigravity-cli"


def _pretty_gemini_model(raw: str) -> str:
    """``gemini-3-flash-agent`` → ``Gemini 3 Flash``. Drops the internal ``-agent``/``-a`` suffix
    AND the per-turn reasoning level (``-low``/``-medium``/``-high``) — the level is a volatile,
    per-response setting recorded in the conversation store, not part of the model name, so showing
    it (e.g. a stale '...Low' while the CLI is on Medium) is misleading."""
    s = re.sub(r"-(agent|a|low|medium|high)$", "", raw or "")
    return " ".join(p if any(c.isdigit() for c in p) else p.capitalize() for p in s.split("-"))


MODEL_ID_RE = re.compile(r"gemini-\d+(?:\.\d+)?-[a-z][a-z0-9.\-]*"
                         r"|claude-(?:opus|sonnet|haiku|fable)-[0-9][0-9-]*")


def _model_like(s: str) -> str | None:
    """The model id inside a blob, or None. A version must follow the family name — without
    that, `gemini-backup` and `gemini-20260425-144749` were both read as models."""
    m = MODEL_ID_RE.search(s or "")
    return m.group(0) if m else None


def _antigravity_model_from_db(sid: str | None) -> str | None:
    """The model a specific Antigravity conversation ran, from its SQLite store (gen_metadata).
    Works even before the global model is set in settings.json."""
    if not sid:
        return None
    db = _antigravity_base() / "conversations" / f"{sid}.db"
    if not db.exists():
        return None
    ids: list[str] = []
    # A live conversation store is in WAL mode, and `mode=ro` still needs to create the -shm
    # sidecar — which fails, so the lookup silently returned nothing for every RUNNING agent
    # (the only kind we ever ask about). `immutable=1` reads the main file without that.
    for uri in (f"file:{db}?mode=ro", f"file:{db}?immutable=1"):
        try:
            with closing(sqlite3.connect(uri, uri=True)) as con:
                for (v,) in con.execute("SELECT data FROM gen_metadata ORDER BY idx DESC LIMIT 5"):
                    s = v.decode("utf-8", "ignore") if isinstance(v, (bytes, bytearray)) else str(v)
                    ids += MODEL_ID_RE.findall(s)
            break
        except sqlite3.Error:
            continue
    if not ids:
        return None
    raw = ids[0]
    return _pretty_claude_model(raw) if raw.startswith("claude") else _pretty_gemini_model(raw)


def open_files(pids: list) -> list[str]:
    """Paths a process currently holds open. Linux reads /proc directly; macOS shells out to
    lsof. Best-effort: an empty list simply means we fall back to other clues."""
    out = []
    for pid in pids or []:
        fd = Path("/proc") / str(pid) / "fd"
        if fd.is_dir():
            try:
                links = list(fd.iterdir())
            except OSError:
                continue  # Exited/non-dumpable child during boot; identity stays unknown.
            for link in links:
                try:
                    out.append(os.path.realpath(link))
                except OSError:
                    pass
            continue
        # -Fn is lsof's machine-readable mode: one field per line, names prefixed with "n".
        # Parsing the human table by column breaks on paths with spaces and on rows where a
        # column is empty — which is why it silently returned nothing the first time.
        # lsof lives in /usr/sbin on macOS, which is not on the PATH a tmux agent inherits —
        # calling it by bare name silently produced nothing at all.
        exe = shutil.which("lsof") or next(
            (c for c in ("/usr/sbin/lsof", "/usr/bin/lsof") if os.path.exists(c)), None)
        if not exe:
            continue
        r = _run([exe, "-p", str(pid), "-Fn"], timeout=3)
        if r and r.returncode == 0:
            for line in r.stdout.splitlines():
                if line.startswith("n/"):
                    out.append(line[1:])
    return out


def antigravity_label(settings_model: str | None, sid: str | None, cmds: list[str],
                      fallback: str) -> str:
    """The model tag for an Antigravity row — the SINGLE place that decides it.

    Order: the globally selected model, then the conversation's own store (which is why this
    runs AFTER the id is resolved — asking with no id was the reason a fresh agy stayed
    unlabelled even once its id showed up), then `--model` on argv, then the kind name.
    """
    return (settings_model or _antigravity_model_from_db(sid)
            or _model_from_argv(cmds) or fallback)


def antigravity_sid(mapped: str | None, pids: list) -> str | None:
    """Conversation id for an Antigravity row — the SINGLE place that decides it.

    The workspace map wins where it exists; the presence lock covers a session young enough
    that the map has not been written yet.
    """
    # The cwd cache may describe a different live process in the same directory.
    return _antigravity_sid_from_presence(pids)


def _antigravity_sid_from_presence(pids: list) -> str | None:
    """The conversation id of a RUNNING agy, from the presence lock it holds open.

    ``~/.gemini/antigravity-cli/presence/<conversation-uuid>.lock`` is opened at startup, so
    this answers immediately — unlike cache/last_conversations.json, which is only written
    once the conversation has been persisted and left a brand-new agent with no id at all.
    """
    found = set()
    for f in open_files(pids):
        if "/presence/" in f and f.endswith(".lock"):
            m = UUID_RE.search(os.path.basename(f))
            if m:
                found.add(m.group(0))
    return next(iter(found)) if len(found) == 1 else None


def _antigravity_info_for_cwd(cwd: str) -> tuple[str | None, str | None]:
    """(conversation id, model) for an Antigravity session. A fresh `agy` has no ``--conversation``
    id on argv; it maps the current workspace → conversation in cache/last_conversations.json, and
    records the selected model in settings.json (global) or the conversation's .db (per-session)."""
    sid = None
    try:
        mapping = json.loads((_antigravity_base() / "cache" / "last_conversations.json").read_text("utf-8"))
        if cwd:
            target = os.path.realpath(cwd)
            sid = next((v for k, v in mapping.items() if os.path.realpath(k) == target), None)
    except (OSError, ValueError):
        pass
    model = None
    try:
        d = json.loads((_antigravity_base() / "settings.json").read_text("utf-8"))
        model = d.get("model") or None
    except (OSError, ValueError):
        pass
    # The DB lookup deliberately does NOT happen here: it needs the conversation id, and at
    # this point we may not have one yet (the workspace map is empty for a young session).
    # antigravity_label() retries it once the id is known — from the presence lock if need be.
    return sid, model


def _classify(cmds: list[str], extra_matches: list[tuple]) -> tuple[str, str, str | None]:
    """Given the command lines in a session's process tree, return (kind, label, session_id).
    Built-in agents are matched FIRST so we get the real kind (and its maker colour); the
    user-supplied matches are only a fallback for agents we don't recognise out of the box."""
    for cmd in cmds:
        for kind, label, pat in KNOWN_AGENTS:
            if pat.search(cmd):
                from .identity import explicit_id
                return kind, label, explicit_id(cmd, UUID_RE)
        for kind, label, pat in extra_matches:
            if pat.search(cmd):
                sid = UUID_RE.search(cmd)
                return kind, label, (sid.group(0) if sid else None)
    return "shell", "shell (idle)", None


def _pretty_model(raw: str) -> str:
    """OpenAI ids the way Claude's already read: ``gpt-6-astra`` → ``GPT-6 Astra``,
    ``openai/gpt-5.6-sol`` → ``GPT-5.6 Sol``, ``gpt-5.5`` → ``GPT-5.5``, ``o3-mini`` → ``O3 Mini``.

    Upper-casing the whole id (``GPT-6-ASTRA``) put a shouting tag next to ``Fable 5.1``
    in the same dashboard. Family stays upper-case, the version keeps its dash, the codename
    after it becomes a capitalised word. Anything that is not an OpenAI id is left alone.
    """
    raw = (raw or "").split("/")[-1]
    if raw[:1].lower() not in ("g", "o"):
        return raw
    parts = raw.split("-")
    label = parts[0].upper()
    rest = parts[1:]
    if rest and rest[0][:1].isdigit():          # version glued to the family: GPT-5.6, GPT-4o
        label += "-" + rest[0]
        rest = rest[1:]
    words = [w.capitalize() if w[:1].isalpha() else w for w in rest]
    return " ".join([label] + words)


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


def _hermes_model() -> str | None:
    """Hermes' configured model from ~/.hermes/config.yaml (``model.default``). Hermes runs on
    the openai-codex *provider* but selects its own model there, so the provider default (Codex)
    is not authoritative — read the agent's own config so the tag follows a model switch.
    Best-effort; returns None on any failure so the caller can fall back."""
    try:
        with (Path.home() / ".hermes" / "config.yaml").open(encoding="utf-8") as stream:
            head = stream.read(65536)
    except OSError:
        return None
    # Scalar values only: no arbitrary YAML tags or assumptions about key order.
    block = False
    for line in head.splitlines():
        if re.match(r'^model:\s*(?:#.*)?$',line): block=True; continue
        if block and line.strip() and not line[0].isspace(): break
        m=re.match(r'^\s+default:\s*(.*?)\s*$',line) if block else None
        if m:
            try:
                lex=shlex.shlex(m.group(1),posix=True);lex.whitespace_split=True;lex.commenters='#'
                values=list(lex)
            except ValueError: return None
            if len(values)==1 and re.fullmatch(r'[A-Za-z0-9_./:-]+',values[0]): return _pretty_model(values[0])
            return None
    return None


def daemon_model(name: str) -> str | None:
    """Concrete model a known daemon runs (best-effort, for the tag)."""
    n = name.lower()
    if "openclaw" in n:
        return _openclaw_model()
    if "hermes" in n:
        # Hermes owns its model selection. Unknown configuration must stay unknown;
        # another CLI's rollout can belong to an entirely different account/session.
        return _hermes_model()
    return None


def vendor_for_agent(kind: str | None, label: str | None) -> str | None:
    """Tag colour for one agent row — the SINGLE place that decides it.

    A recognised kind wins (Codex stays OpenAI orange even if the label is odd); anything
    matched by a user-supplied pattern has no built-in kind, so the model name decides.
    Before this was factored out, daemons used the model fallback and tmux agents did not,
    so the same model showed a green tag in one row and a grey one in the next.
    """
    return KIND_VENDOR.get(kind or "") or vendor_for_model(label)


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
    from .identity import resolve
    rows = []
    for s in tmux_sessions():
        pids = _pane_pids(s["name"])
        tree = _subtree(pids, children)
        # Parents precede their workers; set iteration order must never decide
        # which conversation belongs to the visible TUI.
        ordered, pending, seen = [], list(pids), set()
        while pending:
            pid = pending.pop(0)
            if pid in seen:
                continue
            seen.add(pid)
            ordered.append(pid)
            pending.extend(children.get(pid, []))
        cmds = [procs[p] for p in ordered if p in procs]
        # Prefer non-shell commands when classifying.
        ranked = sorted(cmds, key=lambda c: c.split()[0].rsplit("/", 1)[-1] in SHELLS)
        kind, label, sid = _classify(ranked, extra_matches)
        matching = [p for p in tree if any(k == kind and pat.search(procs.get(p,''))
                    for k, _, pat in KNOWN_AGENTS)]
        ages = [_proc_age(p) for p in matching]
        started = now-max(ages) if ages and all(a is not None for a in ages) else None
        cwd = _session_cwd(s['name'])
        rows.append({'name':s['name'], 'kind':kind, 'label':label, 'session_id':sid,
                     'cwd':os.path.realpath(cwd) if cwd else '',
                     'started':started, 'pids':sorted(tree), 'commands':ranked,
                     'age':int(now-s['created']) if s['created'] else None})
    resolve(rows, sys.modules[__name__], now)
    for row in rows:
        kind, label, sid = row['kind'], row['label'], row['session_id']
        ranked = row['commands']
        if kind == "codex":
            model = ((_rollout_model(row['transcript']) if row['transcript'] else None)
                     or row.get('resolved_model') or _model_from_argv(ranked) or codex_model)
            if model:
                label = model
        if kind == "claude-code":
            cmodel = _claude_model_for_session(sid) if sid else None
            label = label_for_claude(cmodel, ranked, label, exact_session=bool(sid))
        elif kind == "antigravity":
            _, amodel = _antigravity_info_for_cwd(row['cwd'])
            label = antigravity_label(amodel, sid, ranked, label)
        resume = RESUME_TEMPLATES[kind].format(id=sid) if (sid and kind in RESUME_TEMPLATES
                  and not row.get('identity_conflict')) else None
        agents.append({
            "name": row['name'], "kind": kind, "label": label, "session_id": sid,
            "vendor": vendor_for_agent(kind, label),
            "alive": kind != "shell", "age": row['age'],
            "resume_cmd": resume, "pids": row['pids'],
            "identity_conflict": row.get('identity_conflict', False),
        })
        if kind in ('codex', 'claude-code'):
            pane = _run([_tmux_bin(), 'capture-pane', '-p', '-t', row['name']])
            if pane and pane.returncode == 0 and login_screen(kind, pane.stdout):
                agents[-1].update(needs_login=True, status='Needs login')
    return agents


def login_screen(kind: str, text: str) -> bool:
    """Recognize current login UI only; never inspect a shared credential store."""
    clean = re.sub(r'\x1b\[[0-9;?]*[A-Za-z]', '', text)
    lines = [line.strip().lstrip('›❯').strip() for line in clean.splitlines() if line.strip()][-18:]
    if kind == 'codex':
        return (any(re.fullmatch(r'>?\s*1\. Sign in with ChatGPT', line) for line in lines)
                and '2. Sign in with Device Code' in lines
                and any('API key' in line and line.startswith('3.') for line in lines))
    if kind == 'claude-code':
        return any(re.fullmatch(r'(?:⎿\s*|⏵⏵ bypass permissions on \(shift\+tab to cycle\)\s+)?'
                                r'Not logged in\s*[·.]\s*(?:Please run|Run) /login',
                                line, re.I) for line in lines[-6:])
    return False


def configured_agents(cfg: dict) -> list[dict]:
    """Include configured agents even after their tmux session was stopped."""
    from . import config
    rows = discover_agents(config.agent_matches(cfg))
    names = {r['name'] for r in rows}
    for entry in cfg.get('agents', []):
        if entry['name'] not in names:
            rows.append({'name':entry['name'], 'kind':entry.get('kind', 'custom-match'),
                         'label':entry.get('label') or entry.get('kind', 'Agent'),
                         'session_id':None, 'resume_cmd':None, 'pids':[], 'age':None,
                         'alive':False, 'vendor':vendor_for_agent(entry.get('kind', ''), ''),
                         'status':'Stopped (manual)' if not entry.get('enabled', True) else 'Not running'})
    return rows


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


def daemon_status(daemons: list[dict], ignore_pid: int | None = None) -> list[dict]:
    """For each configured daemon ({name, pattern, health_url?}), is it running / healthy?

    ``pgrep`` leaves out its own PID but not the process that called it. The one-shot CLI checks
    (``agentsmon status``, ``agentsmon keepalive`` without ``--loop``) pass their own PID as
    *ignore_pid* so that a pattern that also matches their command line does not report a missing
    daemon as running. Only that one PID is left out; parents and children of the check still count.

    When a daemon advertises a ``health_url`` the health check is authoritative for ``up`` —
    the process pattern is NOT required to match. Process command lines differ across install
    methods (venv / pip --user / pipx), so gating on the pattern would make keepalive needlessly
    restart a daemon that is demonstrably healthy. The pattern is the liveness signal only when
    there is no ``health_url``."""
    out = []
    for d in daemons:
        from . import managed
        pat = d.get("pattern", "")
        if d.get('bridge_config'):
            from . import bridges
            proc_up = bool(bridges.running_pids(d))
        elif d.get('manager'):
            proc_up = managed.active(d['manager']) is True
        else:
            process = _run(['pgrep','-u',str(os.getuid()),'-f','--',pat]) if pat else None
            proc_up = bool(process and process.returncode == 0)
            if proc_up and ignore_pid is not None:
                proc_up = any(p != str(ignore_pid) for p in process.stdout.split())
        entry = {"name": d.get("name", pat), "pattern": pat, "process_up": proc_up}
        url = d.get("health_url")
        if url:
            entry["http_ok"] = _http_ok(url)
            # Up if EITHER signal is positive. Health endpoint is primary, but the process match
            # is a valid fallback: a new daemon version can move/rename its /health endpoint while
            # the process keeps running fine (e.g. Hermes still answering on Telegram). Requiring
            # http alone then falsely marks a live daemon "down" and triggers a needless restart.
            entry["up"] = entry["http_ok"] or proc_up
        else:
            entry["up"] = proc_up
        out.append(entry)
    return out
