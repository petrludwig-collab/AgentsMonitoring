"""`agentsmon doctor` — quick sanity check of tools and config."""
from __future__ import annotations

import shutil

from . import config, detect, plugins, integrity


def run() -> int:
    print("Agents Monitoring · doctor\n")
    ok = True
    tmux = shutil.which("tmux")
    print(f"  tmux:    {'✓ ' + tmux if tmux else '✗ not found (agents need tmux)'}")
    ok = ok and bool(tmux)
    cfg = config.load()
    core = integrity.status()
    print(f"  core:    v{core['version']} ({core['state']}) · {core['release'] or 'no release manifest'}")
    print(f"  content: {core['content_id'] or 'unverified'}")
    ok = ok and core['state'] == 'clean'
    extensions = plugins.Manager(cfg).collect([])
    for extension in extensions['plugins']:
        print(f"  plugin:  {extension['id']} v{extension['version']} API {extension['api_version']} · {extension['error'] or 'OK'}")
    ok = ok and not extensions['errors']
    print(f"  config:  {config.DEFAULT_PATH} ({'exists' if config.DEFAULT_PATH.exists() else 'defaults only'})")
    print(f"  agents:  {len(cfg.get('agents', []))} configured")
    print(f"  daemons: {len(cfg.get('daemons', []))} watched")
    live = [a for a in detect.discover_agents(config.agent_matches(cfg)) if a["alive"]]
    print(f"  live now: {len(live)} agent(s) running in tmux")
    from . import identity
    warnings = identity.restart_warnings(cfg)
    for a in cfg.get("agents", []):
        invalid = config.enabled_error(a)
        if invalid:
            print(f"  ✗ agent '{a.get('name')}': {invalid} — keepalive does not restart it")
            ok = False
        elif a.get("enabled", True) and not a.get("restart") and not identity.fresh_command(a):
            print(f"  ⚠️  '{a['name']}' has no restart command — it can't be revived if it dies.")
        if a['name'] in warnings:
            print(f"  Warning: {a['name']}: {warnings[a['name']]}")
    from . import managed
    for daemon in cfg.get('daemons', []):
        invalid = config.enabled_error(daemon)
        if invalid:
            print(f"  ✗ daemon '{daemon.get('name')}': {invalid} — keepalive does not restart it")
            ok = False
        for warning in managed.warnings(daemon):
            print('  Warning: '+warning)
            ok = False
    return 0 if ok else 1
