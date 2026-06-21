# Agents Monitoring

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Dependencies](https://img.shields.io/badge/dependencies-none-brightgreen)
![License](https://img.shields.io/badge/license-MIT-green)

Keep your AI coding agents alive and watch their status — **Claude Code**, **Codex**, and
background daemons like **OpenClaw** or **Hermes**.

Agents Monitoring is a tiny, dependency-free supervisor. It **auto-detects** what's running in
your tmux sessions (you don't declare anything — it looks), shows a **live status page**, and
**restarts** anything that dies — surviving logout and reboot.

```
tmux sessions  ⇄  Agents Monitoring  ⇄  status page + keepalive
```

---

## Why it's built this way

- **Auto-detect, don't configure** — it enumerates tmux sessions, walks each process tree, and
  classifies what's running (Claude Code / Codex / …). Setup just confirms what it found.
- **Zero install friction** — pure **Python standard library**. Nothing to `pip install` for it
  to work; if pip is missing it just runs from the clone.
- **Stays alive across reboots** — installs a LaunchAgent (macOS) or `systemd --user` unit
  (Linux) for both the keepalive loop and the dashboard.
- **Restarts the right way** — a tmux session can outlive a crashed agent; we detect that and
  relaunch the agent (e.g. `claude --resume <id>`), not just recreate an empty shell.

---

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/petrludwig-collab/AgentsMonitoring/main/install.sh | bash
```

…or from a clone:

```bash
git clone https://github.com/petrludwig-collab/AgentsMonitoring.git
cd AgentsMonitoring
python3 -m agentsmon setup
```

The wizard scans tmux, lists the agents it found, lets you pick which to supervise (proposing a
restart command for each), optionally watches detected daemons, and installs the boot service.

---

## Commands

```bash
agentsmon status            # live agent + daemon status in the terminal
agentsmon dashboard         # serve the status web page (default http://127.0.0.1:8765)
agentsmon keepalive         # one supervision pass (restart anything dead)
agentsmon keepalive --loop  # run continuously (what the service runs)
agentsmon service           # (re)install the boot service
agentsmon doctor            # sanity-check tools + config
agentsmon uninstall         # stop services, remove config + state (agents untouched)
```

`agentsmon status` example:

```
  AGENTS (tmux)
    🟢 work-claude           Claude Code    age 2h 14m  [a1b2c3d4]
    🟢 ops-codex             Codex          age 3d 1h
    ⚪ scratch               shell (idle)   age 5d 0h

  DAEMONS
    🟢 OpenClaw                (proc ok, http ok)
```

---

## How detection works

Agents live in **tmux**, so for every session we walk the pane process tree and classify it by
the command line: `claude` → Claude Code, `codex` → Codex (plus `aider`, `gemini` out of the
box; add your own via the config `match` keyword). A session whose tree is only a login shell is
shown as **idle**. Background **daemons** aren't in tmux, so they're matched by a process pattern
(`pgrep -f`) and an optional HTTP health URL.

---

## Configuration

`~/.config/agentsmon/config.json` (written by `setup`, `0600`). All fields optional:

```json
{
  "agents": [
    { "name": "work-claude", "match": "claude",
      "restart": "claude --resume a1b2c3d4", "cwd": "~/code", "enabled": true }
  ],
  "daemons": [
    { "name": "OpenClaw", "pattern": "openclaw", "health_url": "http://127.0.0.1:18789/health" },
    { "name": "Hermes",   "pattern": "hermes", "restart": "hermes gateway restart" }
  ],
  "dashboard": {
    "host": "127.0.0.1", "port": 8765, "poll_seconds": 15,
    "auth": { "user": "admin", "pwhash": "<sha256 of the password>" }
  },
  "keepalive": { "enabled": true, "interval_seconds": 60 }
}
```

**Dashboard login (HTTP auth).** The setup wizard asks whether to protect the dashboard with a
username + password (defaulting to *yes* whenever you bind it to anything other than localhost).
If enabled, the dashboard requires HTTP Basic auth; the password is stored only as a SHA-256
hash (`dashboard.auth.pwhash`), never in plaintext. Remove the `auth` block to turn it off.

- **services[]** — components you want **uptime history + SLA** for (a gateway, a bridge, a
  daemon): each has a `name` (its dashboard card title), a `process` pattern and/or a
  `health_url`. The dashboard probes them on `probe.interval_seconds`, stores samples in a local
  SQLite, and renders a card with current status, **current uptime**, **SLA %** over
  `probe.sla_window_days`, and a timeline. (e.g. cards "Multi-agent system availability" and
  "Telegram Bridge Status".)
- **agents[].match** — a substring of the agent's process command line that means "alive".
- **agents[].restart** — shell command run in the session to relaunch the agent (empty = just
  recreate the tmux session). Without it, a dead agent can be detected but not revived.
- **daemons[].restart** — optional command to run when the daemon is down.
- **dashboard.host** — keep `127.0.0.1` for local-only; set a VPN/LAN address to reach it
  remotely (then protect it with your firewall — the dashboard has no auth).

---

## Security note

The dashboard is **read-only**. Bind it to `127.0.0.1` (default) or a trusted VPN address, never
the public internet. If you expose it beyond localhost, enable the **HTTP login** in setup
(stored as a SHA-256 hash). Restart commands run as your user.

---

## License

MIT — see [LICENSE](LICENSE).
