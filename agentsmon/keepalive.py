"""Keepalive supervisor — restart anything that died.

For each configured agent we check two things: does its tmux **session** exist, and is its
**agent process** actually alive inside it (a session can survive while the agent crashed to a
bare shell). If the session is gone we recreate it and launch the agent; if the agent died in a
surviving session we re-send the launch command. Daemons with a ``restart`` command are
relaunched when down. A directory lock prevents overlapping runs (it's safe to call every minute
from cron/systemd/launchd).
"""
from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

from . import config, detect


def _log(msg: str) -> None:
    line = f"{time.strftime('%F %T')} {msg}"
    print(line, flush=True)
    try:
        (config.state_dir() / "keepalive.log").open("a", encoding="utf-8").write(line + "\n")
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


def _start(agent: dict, tmux_bin: str) -> None:
    name = agent["name"]
    cwd = os.path.expanduser(agent.get("cwd") or str(Path.home()))
    cmd = agent.get("restart", "")
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
    for a in cfg.get("agents", []):
        if not a.get("enabled", True):
            continue
        exists, alive = _alive(a["name"], a.get("match", ""), sessions, children, procs)
        if alive:
            continue
        _log(f"agent '{a['name']}' {'dead in session' if exists else 'session missing'} → restarting")
        _start(a, tmux_bin)
        restarts += 1
    for d in detect.daemon_status(cfg.get("daemons", [])):
        spec = next((x for x in cfg.get("daemons", []) if x.get("name") == d["name"]), {})
        if not d["up"] and spec.get("restart"):
            _log(f"daemon '{d['name']}' down → restart")
            subprocess.run(spec["restart"], shell=True, capture_output=True, timeout=30)
            restarts += 1
    return restarts


def run(loop: bool = False) -> int:
    cfg = config.load()
    interval = cfg.get("keepalive", {}).get("interval_seconds", 60)
    while True:
        if _acquire_lock():
            try:
                n = tick(cfg)
                if n:
                    _log(f"pass complete: {n} restart(s)")
            finally:
                _release_lock()
        else:
            _log("another keepalive run holds the lock — skipping")
        if not loop:
            return 0
        time.sleep(interval)
