"""Regression test for #6: Hermes daemon tag must reflect Hermes' OWN configured
model/provider, not whatever Codex happens to be running."""
from __future__ import annotations

from pathlib import Path

from agentsmon import detect


def test_hermes_model_reads_its_own_config(tmp_path, monkeypatch):
    hermes_dir = tmp_path / ".hermes"
    hermes_dir.mkdir()
    (hermes_dir / "config.yaml").write_text(
        "some_other_key: 1\n"
        "model:\n"
        "  default: claude-sonnet-5\n"
        "  provider: copilot\n"
        "another_key: true\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    assert detect._hermes_model() == "claude-sonnet-5 (copilot)"
    assert detect.daemon_model("hermes") == "claude-sonnet-5 (copilot)"


def test_hermes_model_falls_back_to_codex_when_own_config_unreadable(tmp_path, monkeypatch):
    # No ~/.hermes/config.yaml at all -> _hermes_model() must return None so
    # daemon_model() falls back to the Codex-based detection instead of crashing.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert detect._hermes_model() is None
