"""Keepalive supervisor — restart anything that died.

For each configured agent we check two things: does its tmux **session** exist, and is its
**agent process** actually alive inside it (a session can survive while the agent crashed to a
bare shell). If the session is gone we recreate it and launch the agent; if the agent died in a
surviving session we re-send the launch command. A restart counts only once the agent process is
seen running in its session afterwards; a typed command or a bare shell is not proof. Daemons with a ``restart`` command are
relaunched when down. A directory lock prevents overlapping runs (it's safe to call every minute
from cron/systemd/launchd).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

from . import config, detect, service


def _attempts_path() -> Path:
    return config.state_dir() / "keepalive_attempts.json"


def _load_attempts() -> dict:
    try:
        return json.loads(_attempts_path().read_text("utf-8"))
    except (OSError, ValueError):
        return {}


def _save_attempts(attempts: dict) -> None:
    try:
        _attempts_path().write_text(json.dumps(attempts), encoding="utf-8")
    except OSError:
        pass


def _log(msg: str) -> None:
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, flush=True)
    try:
        with (config.state_dir() / "keepalive.log").open("a", encoding="utf-8") as out:
            out.write(line + "\n")
    except OSError:
        pass


def heartbeat_path() -> Path:
    return config.state_dir() / "keepalive_heartbeat"


def _heartbeat() -> None:
    """Stamp the time of the last completed pass. A supervisor that silently stops running is
    the worst failure mode (dead agents stay dead and nothing says so); a fresh stamp is the
    only proof it is alive — a local module can watch it, the dashboard shows nothing otherwise."""
    try:
        heartbeat_path().write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass


def _acquire_lock(stale: int = 300) -> bool:
    """Directory lock; steal it if older than *stale* seconds (a crashed previous run)."""
    lock = config.state_dir() / "keepalive.lock"
    try:
        lock.mkdir()
        return True
    except FileExistsError:
        try:
            if time.time() - lock.stat().st_mtime > stale:
                lock.rmdir()
                lock.mkdir()
                return True
        except OSError:
            pass
        return False


def _release_lock() -> None:
    try:
        (config.state_dir() / "keepalive.lock").rmdir()
    except OSError:
        pass


def _alive(name: str, match_kw: str, sessions: dict, children: dict, procs: dict) -> tuple[bool, bool]:
    """Return (session_exists, agent_alive)."""
    if name not in sessions:
        return False, False
    tree = detect._subtree(detect._pane_pids(name), children)
    if not match_kw:
        return True, True
    return True, any(match_kw in procs.get(p, "") for p in tree)


# How long a launch may take to show its agent process. Seeing the command typed into tmux, or a
# shell in the session, is not proof that the agent runs; only the process is. This is the window
# for starting looks, not a hard limit for the whole restart (see _confirm).
CONFIRM_SECONDS = 15
_POLL_SECONDS = 0.5


def _start(agent: dict, tmux_bin: str, recreate: bool = False) -> dict:
    """Launch the agent and return a launch record for :func:`_confirm`.

    ``state`` is ``"failed"`` when tmux refused (with ``detail``), otherwise ``"sent"`` with the
    pane's shell PID and the command lines that already ran under it, so that only a process
    started by this launch (a new PID, or the shell itself after ``exec``) can confirm it.

    A timeout before ``send-keys`` means the launch command was never typed: that is a failed
    start. A ``send-keys`` client that times out may already have delivered the keys, so that
    launch stays ``"sent"`` (with a ``note``) and only the agent process decides; the command is
    never typed a second time."""
    name = agent["name"]
    cwd = os.path.expanduser(agent.get("cwd") or str(Path.home()))
    cmd = agent.get("restart", "")
    launch = {"name": name, "match": agent.get("match", ""), "state": "failed"}
    try:
        if recreate:
            # The session survives but the agent won't relaunch into it (the pane is stuck in a
            # half-dead state). Kill it so it's recreated clean instead of send-keys'ing into the mess.
            subprocess.run([tmux_bin, "kill-session", "-t", name], capture_output=True, timeout=10)
            time.sleep(0.5)
        if name not in {s["name"] for s in detect.tmux_sessions()}:
            r = subprocess.run([tmux_bin, "new-session", "-d", "-s", name, "-c", cwd],
                               capture_output=True, text=True, timeout=10)
            if r.returncode != 0:
                return {**launch, "detail": "tmux new-session failed: " + _first_line(r.stderr)}
        r = subprocess.run([tmux_bin, "display-message", "-p", "-t", name, "#{pane_pid}"],
                           capture_output=True, text=True, timeout=10)
        pane = r.stdout.strip()
        if r.returncode != 0 or not pane.isdigit():
            return {**launch, "detail": "no pane found in the session: "
                    + _first_line(r.stderr or r.stdout)}
        if not cmd:
            return {**launch, "detail": "no restart command configured"}
        procs, children = detect._proc_table()
        before = {p: procs.get(p, "") for p in detect._subtree([int(pane)], children)}
        # Small settle so the shell is ready to receive the command.
        time.sleep(0.5)
    except subprocess.TimeoutExpired as exc:
        return {**launch, "detail": f"tmux {exc.cmd[1]} timed out; the launch command was not sent"}
    except (subprocess.SubprocessError, OSError) as exc:
        return {**launch, "detail": f"tmux could not be run: {exc}"}
    sent = {**launch, "state": "sent", "pane": int(pane), "before": before, "tmux": tmux_bin}
    try:
        r = subprocess.run([tmux_bin, "send-keys", "-t", name, cmd, "Enter"],
                           capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        return {**sent, "note": "tmux send-keys timed out, the command may have been delivered"}
    except OSError as exc:
        return {**launch, "detail": f"tmux could not be run: {exc}"}
    if r.returncode != 0:
        return {**launch, "detail": "tmux send-keys failed: " + _first_line(r.stderr)}
    return sent


def _session_panes(tmux_bin: str) -> dict:
    """{session name: [pane PIDs]} for all sessions, in one call. Listing everything and comparing
    names exactly avoids ``-t``, which falls back to a longer name by prefix (or another session)
    once the session is gone."""
    try:
        r = subprocess.run([tmux_bin, "list-panes", "-a", "-F", "#{session_name}\t#{pane_pid}"],
                           capture_output=True, text=True, timeout=10)
    except (subprocess.SubprocessError, OSError):
        return {}
    panes: dict = {}
    for session, _, pid in (line.rpartition("\t") for line in r.stdout.splitlines()):
        if r.returncode == 0 and pid.isdigit():
            panes.setdefault(session, []).append(int(pid))
    return panes


def _first_line(text) -> str:
    return ((text or "").strip().splitlines() or ["no output"])[0][:200]


def _launched_pid(launch: dict, procs: dict, children: dict, panes: dict):
    """A process started by this launch that matches the agent, under the very pane we launched
    into while that pane still belongs to the agent's session; else None. The keyword rule is the
    one :func:`_alive` uses for the next pass."""
    if launch["pane"] not in panes.get(launch["name"], ()):
        return None          # the session was replaced, or the pane moved to another session
    found = [p for p in detect._subtree([launch["pane"]], children)
             if launch["match"] in procs.get(p, "") and procs.get(p) != launch["before"].get(p)]
    return min(found) if found else None


def _confirm(launches: list, seconds: float = None) -> list:
    """Look for *seconds* (one window for all launches together) until each sent launch shows its
    agent process, seen alive on two consecutive looks so a program that exits at once does not
    count. Every look starts before the deadline and costs one ``ps`` and one ``tmux`` call for all
    launches. A look that started in time runs to its end, and those calls have their own timeouts,
    so the call can return after the window: the window is not a hard limit, and the tmux calls of
    :func:`_start` come on top of it. Sets ``state`` to ``"confirmed"`` (with ``pid``) or
    ``"unconfirmed"``. A launch that is not confirmed is never started again here; the next pass
    decides as before."""
    seconds = CONFIRM_SECONDS if seconds is None else seconds
    pending = [x for x in launches if x.get("state") == "sent" and x["match"]]
    seen: dict = {}
    deadline = time.monotonic() + seconds
    while pending and time.monotonic() < deadline:      # no look starts after the window
        procs, children = detect._proc_table()
        panes = {b: _session_panes(b) for b in {x["tmux"] for x in pending}}   # one call per look
        for launch in list(pending):
            pid = _launched_pid(launch, procs, children, panes[launch["tmux"]])
            if pid is not None and seen.get(launch["name"]) == pid:
                launch.update(state="confirmed", pid=pid)
                pending.remove(launch)
            else:
                seen[launch["name"]] = pid
        if pending:
            time.sleep(max(0.0, min(_POLL_SECONDS, deadline - time.monotonic())))
    for launch in pending:
        launch.update(state="unconfirmed",
                      detail=f"no new '{launch['match']}' process in the session within {seconds:g} s")
    for launch in launches:
        if launch.get("state") == "sent":          # no keyword: nothing could ever confirm it
            launch.update(state="unconfirmed", detail="no 'match' keyword configured, so the "
                          "agent process cannot be identified")
    return launches


def _verdict(launch: dict) -> str:
    """One truthful line: the start failed, the agent process was seen, or it was not."""
    name = launch["name"]
    note = f" ({launch['note']})" if launch.get("note") else ""
    if launch.get("state") == "confirmed":
        return (f"agent '{name}': restart confirmed – agent process pid {launch['pid']} runs in the "
                f"session{note}")
    if launch.get("state") == "failed":
        return f"agent '{name}': restart failed – {launch.get('detail', 'unknown error')}"
    return f"agent '{name}': restart NOT confirmed – {launch.get('detail', 'not checked')}{note}"


def tick(cfg: dict) -> int:
    """One supervision pass. Returns the number of restarts confirmed by a running process."""
    tmux_bin = shutil.which(cfg.get("tmux_bin", "tmux")) or "tmux"
    sessions = {s["name"]: s for s in detect.tmux_sessions()}
    procs, children = detect._proc_table()
    restarts = 0
    launches = []
    attempts = _load_attempts()
    from . import identity
    live = detect.discover_agents(config.agent_matches(cfg)) if any(
        identity.requires_pin(a) for a in cfg.get('agents', [])) else []
    pins = identity.pin_sessions(cfg, live) if live else {}
    if not live:
        try:
            pins = json.loads((config.state_dir()/'session-pins.json').read_text())
        except (OSError, ValueError):
            pass
    for a in cfg.get("agents", []):
        if not a.get("enabled", True):
            continue
        name = a["name"]
        exists, alive = _alive(name, a.get("match", ""), sessions, children, procs)
        if alive:
            if a.get('kind') == 'claude-code' or a.get('match') == 'claude':
                from .dialogs import dismiss_optional_renderer
                if dismiss_optional_renderer(name):
                    _log(f"agent '{name}': declined the known optional fullscreen renderer dialog")
            attempts.pop(name, None)          # recovered → clear the failure counter
            continue
        n = attempts.get(name, 0) + 1
        attempts[name] = n
        # First failure → plain restart (send-keys into the session). If it's STILL dead next pass,
        # the pane is stuck (a half-dead agent + a bare shell), so kill + recreate the session for a
        # clean launch instead of send-keys'ing into the mess forever.
        recreate = exists and n >= 2
        safe = identity.safe_restart(a, live, pins)
        if safe is None:
            _log(f"agent '{name}': restart blocked – configure a safe fresh_start command")
            continue
        identity.record_restart(safe)
        if safe.get('restart_warning'):
            _log(f"agent '{name}': WARNING: {safe['restart_warning']}")
        _log(f"agent '{name}' {'dead in session' if exists else 'session missing'} → "
             f"{'recreating session' if recreate else 'restarting'} (attempt {n})")
        launches.append(_start(safe, tmux_bin, recreate=recreate))
    _save_attempts(attempts)
    for launch in _confirm(launches):
        _log(_verdict(launch))
        restarts += launch["state"] == "confirmed"
    for d in detect.daemon_status(cfg.get("daemons", [])):
        spec = next((x for x in cfg.get("daemons", []) if x.get("name") == d["name"]), {})
        if not spec.get("enabled", True):
            continue                       # explicitly stopped from the dashboard — don't revive it
        if not d["up"] and (spec.get("restart") or spec.get('manager')):
            _log(f"daemon '{d['name']}' down → restart")
            from . import managed
            ok, message = managed.action(spec, 'restart')
            _log(message)
            restarts += int(ok)
    return restarts


def run(loop: bool = False) -> int:
    cfg = config.load()
    interval = cfg.get("keepalive", {}).get("interval_seconds", 60)
    while True:
        # Dashboard boot persistence is independent of opt-in agent supervision.
        if service.refresh_launcher():
            _log("updated installed cron launcher")
        if not cfg.get("keepalive", {}).get("enabled", True):
            return 0                   # no agent restarts or heartbeat when disabled
        if _acquire_lock():
            try:
                n = tick(cfg)
                if n:
                    _log(f"pass complete: {n} restart(s)")
                _heartbeat()
            finally:
                _release_lock()
        else:
            _log("another keepalive run holds the lock — skipping")
        if not loop:
            return 0
        time.sleep(interval)
