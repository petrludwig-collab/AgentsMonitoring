"""Keepalive supervisor — restart anything that died.

For each configured agent we check two things: does its tmux **session** exist, and is its
**agent process** actually alive inside it (a session can survive while the agent crashed to a
bare shell). If the session is gone we recreate it and launch the agent; if the agent died in a
surviving session we re-send the launch command. Daemons with a ``restart`` command are
relaunched when down. A lock held for the whole pass prevents overlapping runs (it's safe to call
every minute from cron/systemd/launchd).
"""
from __future__ import annotations

import errno
import fcntl
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


LOCK_NAME = "keepalive.flock"
LEGACY_LOCK_NAME = "keepalive.lock"


class LockError(Exception):
    """The pass lock could not be taken for a reason other than another running pass."""


def _acquire_lock() -> int | None:
    """Take the pass lock: an exclusive ``flock`` on a file that stays in place.

    Returns the open descriptor, which holds the lock until ``_release_lock`` or until the process
    ends (also when it is killed), so a long pass never looks stale. Returns None when another
    pass holds it. Raises LockError for anything else, so a pass never runs unprotected. The
    descriptor is not inherited by programs a pass starts (Python opens it close-on-exec).

    Versions before this one used a ``keepalive.lock`` directory and took it over after five
    minutes. Such a directory may belong to an older pass that is still running, so it is
    reported and left alone, never removed here.
    """
    state = config.state_dir()
    path = state / LOCK_NAME
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError as exc:
        raise LockError(f"cannot open {path}: {exc}") from exc
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        os.close(fd)
        if exc.errno in (errno.EWOULDBLOCK, errno.EAGAIN):
            return None
        raise LockError(f"cannot lock {path}: {exc}") from exc
    legacy = state / LEGACY_LOCK_NAME
    try:
        os.lstat(legacy)
    except FileNotFoundError:
        return fd
    except OSError as exc:
        _release_lock(fd)
        raise LockError(f"cannot check {legacy}: {exc}") from exc
    _release_lock(fd)
    raise LockError(f"{legacy} is left from an older agentsmon version, whose keepalive pass may "
                    f"still be running; once no older keepalive runs, remove it")


def _release_lock(fd: int) -> None:
    """Unlock and close; the file itself stays, so every pass locks the same file."""
    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _alive(name: str, match_kw: str, sessions: dict, children: dict, procs: dict) -> tuple[bool, bool]:
    """Return (session_exists, agent_alive)."""
    if name not in sessions:
        return False, False
    tree = detect._subtree(detect._pane_pids(name), children)
    if not match_kw:
        return True, True
    return True, any(match_kw in procs.get(p, "") for p in tree)


def _start(agent: dict, tmux_bin: str, recreate: bool = False) -> None:
    name = agent["name"]
    cwd = os.path.expanduser(agent.get("cwd") or str(Path.home()))
    cmd = agent.get("restart", "")
    if recreate:
        # The session survives but the agent won't relaunch into it (the pane is stuck in a
        # half-dead state). Kill it so it's recreated clean instead of send-keys'ing into the mess.
        subprocess.run([tmux_bin, "kill-session", "-t", name], capture_output=True, timeout=10)
        time.sleep(0.5)
    if name not in {s["name"] for s in detect.tmux_sessions()}:
        subprocess.run([tmux_bin, "new-session", "-d", "-s", name, "-c", cwd],
                       capture_output=True, timeout=10)
    if cmd:
        # Small settle so the shell is ready to receive the command.
        time.sleep(0.5)
        subprocess.run([tmux_bin, "send-keys", "-t", name, cmd, "Enter"],
                       capture_output=True, timeout=10)


def tick(cfg: dict) -> int:
    """One supervision pass. Returns the number of restarts performed."""
    tmux_bin = shutil.which(cfg.get("tmux_bin", "tmux")) or "tmux"
    sessions = {s["name"]: s for s in detect.tmux_sessions()}
    procs, children = detect._proc_table()
    restarts = 0
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
        _start(safe, tmux_bin, recreate=recreate)
        restarts += 1
    _save_attempts(attempts)
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
        rc = 0
        try:
            lock = _acquire_lock()
        except LockError as exc:
            _log(f"cannot take the keepalive lock, pass not run: {exc}")
            lock, rc = None, 1
        else:
            if lock is None:
                _log("another keepalive run holds the lock — skipping")
        if lock is not None:
            try:
                n = tick(cfg)
                if n:
                    _log(f"pass complete: {n} restart(s)")
                _heartbeat()
            finally:
                _release_lock(lock)
        if not loop:
            return rc
        time.sleep(interval)
