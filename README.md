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
- **Stays alive across reboots** — installs a **cron** launcher (`@reboot` + every minute) that
  nohups the dashboard and runs keepalive. Works headless over SSH, with no `systemd --user` /
  D-Bus / login-session requirement (the usual "Failed to connect to bus" trap).
- **Restarts the right way** — a tmux session can outlive a crashed agent; we detect that and
  relaunch the agent (e.g. `claude --resume <id>`), not just recreate an empty shell.
  A restart counts only once the agent process is seen running in its session again (within
  15 s); otherwise the keepalive log, the dashboard and `agentsmon restart` say it was not confirmed.

---

## Quick start

### Safe conversation ownership (0.3.3)

Agents sharing a directory must have independent conversation IDs. Detection uses
explicit resume arguments, a transcript/presence file held by the process, or an
unambiguous creation timestamp during a fresh process's first minute. When exactly
one agent of a kind runs in a directory, its latest transcript/workspace mapping
is also accepted. This preserves older conversations opened with `--continue`.
With multiple agents of the same kind in that directory, this fallback is disabled;
an identity claimed by another agent is never borrowed.
An uncertain ID remains blank. After a crash, an agent without proven ownership
starts a **fresh conversation**, removing resume/continue selectors from its
configured CLI command. A persistent amber dashboard notice and keepalive log
warn that history was not resumed. Once identity is proven, a local pin enables
exact resume; another live agent's ID is never reused. Original histories stay on
disk. A later verified restart clears the notice.

`new` saves the full fresh launch command, including its absolute CLI path. For
custom shell wrappers, configure `fresh_start` with a direct CLI invocation:
arbitrary shell scripts cannot be safely rewritten. Only the exact optional Claude
fullscreen experiment is declined automatically; permission/login/trust prompts
remain untouched.

`agentsmon new` always lists the same agent types in the same order. Missing CLIs
are marked, invalid input is retried, and the final choice must be confirmed.
Besides PATH, common `~/.local/bin`, `~/.npm-global/bin` and `/opt/homebrew/bin`
installations are detected. Stopped/disabled configured names are still reserved.

### Daemons owned by a service manager

`agentsmon add` discovers user systemd ownership (including wrapped Hermes
gateways) and uses the unit for restart/stop. Selected units are enabled and
started; own-user lingering is attempted without sudo. If policy denies it,
the CLI prints the exact `loginctl enable-linger USER` command for an administrator.
`doctor` checks unit enablement and lingering. Calls under `su` fill missing
XDG/DBus variables from the existing `/run/user/<uid>` directory.
On macOS, detected launchd jobs use `bootout`/`kickstart`, with their original
plist used to bootstrap a previously unloaded job. No parallel standalone daemon
is started when a service manager owns it.
See [systemctl](https://www.freedesktop.org/software/systemd/man/latest/systemctl.html)
and [loginctl](https://www.freedesktop.org/software/systemd/man/latest/loginctl.html).

### Optional bridge registration API

Each Agent2Telegram config gets its own supervised daemon (restarted on failure). All bridges share one
"Telegram Bridge" availability card, which is green only when every enabled bridge runs and names any that is down. Integrators
can register without prompts or restarting the dashboard:

```bash
agentsmon add --non-interactive \
  --bridge-config "$HOME/.config/agent2telegram/example.json" \
  --bridge-python "/absolute/path/to/venv/bin/python" \
  --bridge-state "$HOME/.local/state/agent2telegram-example" \
  --bridge-pythonpath "/absolute/path/to/source"
```

The last two flags are optional. Repeated registration and later interactive
`add` remain idempotent. Only paths and the restart command are stored; bot tokens
are never copied. Disabled entries stay disabled. Registration does not install
boot services; use `agentsmon setup` if monitoring is not running yet.

Keep the dashboard on localhost by default. HTTP Basic Auth over unencrypted
HTTP exposes credentials on the network; use an SSH tunnel or Tailscale HTTPS
before making it reachable remotely.

### Prerequisites

**Ubuntu 22.04 / 24.04** (run the installer as your normal user, not root):

```bash
sudo apt-get update
sudo apt-get install -y python3 git tmux curl cron
sudo service cron start
```

Python 3.10+ is required; pip is optional. Minimal containers need a running cron
daemon and an init process to reap children (`docker run --init`). Users on one
machine must choose different dashboard ports.

**macOS** (with [Homebrew](https://brew.sh/) already installed):

```bash
brew install python tmux git
export PATH="$(brew --prefix)/bin:$HOME/.local/bin:$PATH"
python3 --version
```

Run setup from the normal user's Terminal. Allow cron access if macOS prompts for
it. Agents that need protected files may also require an explicit OS permission
for their launcher/tmux. The installer cannot grant permissions, and changing a
launcher does not change the permissions of an already-running tmux server.
Check the first scheduled run with `agentsmon doctor`; never restart active
sessions just to test OS permissions.

### Install and verify

```bash
curl -fsSL https://raw.githubusercontent.com/petrludwig-collab/AgentsMonitoring/main/install.sh | bash
```

If the installer had no controlling terminal (SSH without `-t`, `docker exec`,
or automation), it finishes successfully without running setup. Run the wizard
explicitly in your terminal:

```bash
export PATH="$HOME/.local/bin:$PATH"
agentsmon setup
agentsmon doctor
agentsmon status
```

Choose a port and HTTP login in the wizard, then open the printed URL. An
unauthenticated request to a protected dashboard returns HTTP 401; after login
it returns HTTP 200. Setup can register `@reboot` and every-minute cron entries
for the current user. It does not require root.

Git updates get three non-interactive attempts. If they all fail, the installer
stops and preserves the checkout; check the git error and re-run to try again.
Archive installations created by this installer carry an
`.agentsmon-archive-install` marker. Re-run the installer to update them: it
downloads and checks the new source beside the old directory, then replaces it
by renaming, restoring the previous directory if replacement fails. Configuration
and history outside the source directory remain untouched. Older unmarked archives
are recognised only at `~/.agentsmon-src`, with an `agents-monitoring` project in
`pyproject.toml`, `agentsmon/__init__.py`, no `.git` and no symlinks. Other unmarked
non-git directories are preserved and reported as an error; back them up and move
them aside explicitly before a fresh installation.

…or from a clone:

```bash
git clone https://github.com/petrludwig-collab/AgentsMonitoring.git
cd AgentsMonitoring
python3 -m agentsmon setup
```

The wizard scans tmux, lists the agents it found, lets you pick which to supervise (proposing a
restart command for each), optionally watches detected daemons, and installs the boot service.

To **spin up a fresh agent**, run `agentsmon new`: it asks for a type (Claude Code / Codex /
Antigravity — whichever CLIs are installed) and a name, launches it in its own
tmux session, and registers it for keepalive + the dashboard in one step.

`agentsmon restart NAME` (or `--all`) restarts supervised agents from the command line and resumes
their conversation, e.g. after an installer added a new MCP server.

In the dashboard, ✕ stops an agent (keepalive leaves it alone, ↻ starts it again). Pressing ✕ on
an already stopped agent removes it from monitoring; its conversation files stay on disk.

---

## Commands

```bash
agentsmon new               # create a new agent: pick a type + name, launch it, start monitoring
agentsmon update            # pull latest code + reload (no re-setup)
agentsmon add               # detect & add newly-started agents/daemons (no full re-setup)
agentsmon status            # live agent + daemon status in the terminal
agentsmon dashboard         # serve the status web page (default http://127.0.0.1:8765)
agentsmon keepalive         # one supervision pass (restart anything dead)
agentsmon keepalive --loop  # run continuously (what the service runs)
agentsmon service           # (re)install the boot service
agentsmon doctor            # sanity-check tools + config
agentsmon uninstall         # stop services, remove config + state (agents untouched)
```

## Plugins and clean core (0.3)

Keep organization-specific integrations outside this repository. Versioned plugins can add
agent tables, display columns, model/account detectors, hidden sessions and guarded row
actions. Every table uses the same renderer. Existing executable service-card modules remain
compatible. Failed/timed-out plugins produce a visible error without taking down the page.

Minimal layout in your configured `modules_dir`:

```text
example/
  agentsmon-plugin.json
  plugin.py
```

`agentsmon-plugin.json`:

```json
{"api_version":1,"id":"example","version":"1.0.0","module":"plugin.py","settings":{}}
```

`plugin.py`:

```python
def collect(context, settings):
    return {"tables": [{"id": "workers", "title": "Example workers", "rows": [
        {"id": "one", "name": "Example", "alive": True, "label": "My model"}
    ]}]}
```

Restart the dashboard after installing/changing the manifest. Plugins are trusted local
code, run in bounded subprocesses; this is failure isolation, **not an OS security sandbox**.
The API does not pass dashboard auth/config to plugins. See [API, actions, entry points and
optional account-scoped Usage](docs/PLUGINS.md), and [migration/rollback](docs/MIGRATION-0.3.md).

`agentsmon doctor` prints the package version, release identifier, content hash, clean/modified
state and loaded plugin versions/errors. The footer shows `core v0.3.1 (clean)` or `modified`.
The included manifest detects changed, missing and added package files. `clean` means equality
to that manifest; it is not a cryptographic publisher signature. A review candidate is not
yet a published GitHub release. Compare the manifest/content hash to the reviewed release
when installing. Do not regenerate the manifest to conceal local modifications.

For an isolated install without wizard/service registration or pip/network access:

```bash
bash install.sh --no-setup --no-pip
```

Run from your reviewed checkout. `--no-pip` keeps a standard-library launcher pointing to that
checkout; use a stable directory. `--no-setup` does not launch agents or install a boot service.
The normal interactive installer behavior is unchanged.

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
the command line: `claude` → Claude Code, `codex` → Codex, `agy` → Antigravity (add your own via the
config `match` keyword). A session whose tree is only a login shell is
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
  `probe.sla_window_days`, and a timeline. (e.g. the "Telegram Bridge Status" card.)
- **the system card** — a special synthetic service `{ "name": "Multi-Agent System
  Availability", "kind": "system", "metric": "system_latency" }` (added by setup). It isn't tied
  to one component: it is **up only when every monitored agent + daemon is up**, so its timeline
  is the availability of your whole system. Its latency is the average across all health-checked
  components.
- **pinned_daemons[]** — non-tmux processes to show at the **top** of the Persistent Agents
  table (e.g. a gateway, a worker): each has `name`, `process` (pgrep), `tag` (model/label shown),
  `vendor` (tag colour: anthropic/openai/google) and optional `name_color` (highlight the name:
  red/gold/green/blue).
- **pinned_daemons[].health_url** — give a pinned daemon a health endpoint. Healthy rows
  show "Running"; the Status tooltip includes the measured latency.
- **services[].metric** — the card's third metric: `latency` (this service's current latency,
  default), `system_latency` (average current latency across **all** health‑checked components —
  a system overview number), `avg_latency` (this service's average over the SLA window), or
  `agents` (running tmux‑agent count).
- **agents[].tag / agents[].vendor** — override the model label and tag colour shown for a
  detected agent (otherwise the detected type + maker colour are used).
- **Telegram link (optional)** — agents/daemons connected to a Telegram bot get a small Telegram
  icon that opens `t.me/<bot>`. Two ways it's filled, explicit wins:
  - **explicit** — set `"telegram": "@yourbot"` (or a full `t.me` URL) on a `pinned_daemons[]` /
    `agents[]` entry. Use this for daemons with their own native bot (e.g. Hermes).
  - **auto** — a tmux session bridged with
    [Agent2Telegram](https://github.com/petrludwig-collab/Agent2Telegram) is matched by reading
    only the non-secret bot `@username` from the bridge config (never the token); and OpenClaw's
    own bot is auto-detected from its config at setup.

  If nothing is connected, no icon appears — it's a soft integration, never required.
- **services[].command** — a shell command or script as the health check (exit 0 = up). It may
  print a JSON object: `{"up": true, "latency_ms": 12, "detail": "tailscale=up openvpn=up"}` —
  `up` overrides the exit code, `latency_ms` the measured wall time, `detail` the card's status
  line. `timeout_seconds` (default 15) caps a hanging check. Combine with `process`/`health_url`
  and the card is up only when all of them are.
- **Local check modules (`modules_dir`, default `~/.config/agentsmon/modules/`)** — every
  executable file in that folder becomes a service card automatically, with the same uptime
  history, SLA and timeline as any other service: a VPN tunnel, a database, your own product's
  endpoint, a backup job… Write a three-line shell script, `chmod +x`, done. Optional header
  lines set the card: `# agentsmon: name=VPN to the office, latency_label=round trip,
  timeout_seconds=7`. Files starting with `_` or `.` are helpers and ignored; a service of the
  same name written by hand in `services[]` wins. The folder is yours — nothing in it is part of
  this repo, so private checks stay private while the dashboard itself keeps updating from git.

- **services[].row_details** — opt-in boolean: show each module row's escaped `detail`
  text in the second table column instead of its address. Useful for counts and
  timestamps that should be visible without hovering. Default false preserves
  existing layouts. Row identifiers and history keys stay unchanged.

- **probe.min_outage_samples** — how many consecutive failed probes count as a real outage for
  the **Uptime** metric (default 3); isolated transient blips don't reset uptime (SLA still
  counts them).
- **agents[].match** — a substring of the agent's process command line that means "alive".
- **agents[].restart** — command used to relaunch an agent. Conversation-based CLIs
  resume only a verified identity; otherwise they start fresh with a warning.
- **agents[].fresh_start** — direct CLI launch for recovery without known identity,
  saved automatically by `new`; keeps CLI path and options after a pre-first-turn crash.
- **daemons[].restart** — optional command to run when the daemon is down.
- **dashboard.host** — keep `127.0.0.1` for local-only; set a VPN/LAN address to reach it
  remotely. Non-loopback binds require HTTP authentication; also restrict access
  with your firewall and use a trusted VPN or HTTPS reverse proxy.

---

## Security note

The dashboard includes **restart/stop actions** as well as status views. Bind it
to `127.0.0.1` (default) or a trusted VPN address, never directly to the public
internet. HTTP login is mandatory beyond loopback; use a trusted encrypted
transport because HTTP Basic auth alone does not encrypt traffic. Passwords are
stored as SHA-256 hashes. Restart commands and trusted plugins run as your user.

---

## License

MIT — see [LICENSE](LICENSE).
