"""Regression test for #4: every agentsmon.db call site must close its SQLite
connection instead of leaking file descriptors on a long-running process."""
from __future__ import annotations

import os

import pytest

from agentsmon import db


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(db.config, "state_dir", lambda: tmp_path)


def _open_fd_count() -> int:
    return len(os.listdir("/proc/self/fd"))


def test_repeated_calls_do_not_leak_file_descriptors():
    before = _open_fd_count()
    for _ in range(300):
        db.record("svc", True, 0.1, "ok")
        db.last("svc")
        db.sla("svc", 3600)
        db.avg_latency("svc", 3600)
        db.uptime_seconds("svc")
        db.history_seconds("svc")
        db.timeline("svc", 3600, 12)
    after = _open_fd_count()
    # A handful of transient fds (e.g. pytest capture) is fine; hundreds is the leak.
    assert after - before < 10, (
        f"file descriptors grew by {after - before} over 300 iterations - "
        "SQLite connections are not being closed"
    )
