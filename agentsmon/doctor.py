"""`agentsmon doctor` — quick sanity check of tools and config."""
from __future__ import annotations

import shutil

from . import config, detect


def run() -> int:
    print("Agents Monitoring · doctor\n")
    ok = True
    tmux = shutil.which("tmux")
    print(f"  tmux:    {'✓ ' + tmux if tmux else '✗ not found (agents need tmux)'}")
    ok = ok and bool(tmux)
    cfg = config.load()
    print(f"  config:  {config.DEFAULT_PATH} ({'exists' if config.DEFAULT_PATH.exists() else 'defaults only'})")
    print(f"  agents:  {len(cfg.get('agents', []))} configured")
    print(f"  daemons: {len(cfg.get('daemons', []))} watched")
    live = [a for a in detect.discover_agents(config.agent_matches(cfg)) if a["alive"]]
    print(f"  live now: {len(live)} agent(s) running in tmux")
    for a in cfg.get("agents", []):
        if a.get("enabled", True) and not a.get("restart"):
            print(f"  ⚠️  '{a['name']}' has no restart command — it can't be revived if it dies.")
    return 0 if ok else 1
