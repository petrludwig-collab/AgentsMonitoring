"""Auto-detection of running agents and daemons — the heart of the tool.

We never ask the user to declare what they have; we look. Agents run inside **tmux** sessions,
so we enumerate sessions, walk each session's process tree, and classify what's running by the
command line (claude / codex / a generic match). Background **daemons** (OpenClaw, Hermes, …)
aren't in tmux, so we detect those by a process pattern (and optionally an HTTP health URL).

Pure standard library: `tmux` + `ps` via subprocess, no third-party deps.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import time
import urllib.request

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
    """Non-tmux processes (OpenClaw, Hermes, …) shown at the top of the agents table."""
    out = []
    for d in pinned:
        pat = d.get("process", "")
        r = _run(["pgrep", "-f", pat]) if pat else None
        pids = [int(x) for x in r.stdout.split()] if (r and r.returncode == 0) else []
        ages = [a for a in (_proc_age(p) for p in pids) if a is not None]
        age = max(ages) if ages else None    # oldest matching process = how long the service has been up
        out.append({
            "name": d.get("name"), "kind": "daemon", "label": d.get("tag", d.get("name")),
            "vendor": d.get("vendor"), "name_color": d.get("name_color"),
            "session_id": None, "alive": bool(pids), "age": age,
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


def _classify(cmds: list[str], extra_matches: list[tuple]) -> tuple[str, str, str | None]:
    """Given the command lines in a session's process tree, return (kind, label, session_id).
    User-supplied (kind,label,pattern) tuples are tried first, then the built-ins."""
    for cmd in cmds:
        for kind, label, pat in extra_matches:
            if pat.search(cmd):
                sid = UUID_RE.search(cmd)
                return kind, label, (sid.group(0) if sid else None)
        for kind, label, pat in KNOWN_AGENTS:
            if pat.search(cmd):
                sid = UUID_RE.search(cmd)
                return kind, label, (sid.group(0) if sid else None)
    return "shell", "shell (idle)", None


def discover_agents(extra_matches: list[tuple] | None = None, now: float | None = None) -> list[dict]:
    """Every tmux session classified as a running agent (or an idle shell)."""
    now = now or time.time()
    extra_matches = extra_matches or []
    procs, children = _proc_table()
    agents = []
    for s in tmux_sessions():
        pids = _pane_pids(s["name"])
        tree = _subtree(pids, children)
        cmds = [procs[p] for p in tree if p in procs]
        # Prefer non-shell commands when classifying.
        ranked = sorted(cmds, key=lambda c: c.split()[0].rsplit("/", 1)[-1] in SHELLS)
        kind, label, sid = _classify(ranked, extra_matches)
        age = int(now - s["created"]) if s["created"] else None
        resume = RESUME_TEMPLATES.get(kind, "").format(id=sid) if (sid and kind in RESUME_TEMPLATES) else None
        agents.append({
            "name": s["name"], "kind": kind, "label": label, "session_id": sid,
            "vendor": KIND_VENDOR.get(kind), "alive": kind != "shell", "age": age,
            "resume_cmd": resume, "pids": sorted(tree),
        })
    return agents


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
