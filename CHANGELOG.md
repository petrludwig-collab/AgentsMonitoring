# Changelog

## 0.3.14 (2026-10-25)

- Persistent-agent quota cells are exactly two compact 5h/week gauges with accessible hover/keyboard/touch tooltips; 1920-wide layout uses up to 1480px.
- Multi-agent system health skips disabled legacy bridges (native Hermes LANA lane remains authoritative) and Czech status copy.
- ChatGPT/Codex quotas prefer live Hermes `usage --json` over stale rollout JSONL; GitHub Actions collector probes usage + usage/summary with honest permission blockers.

## 0.3.13 (2026-10-25)

- Dashboard UI is Czech (`lang=cs`, `cs-CZ` formatting) with responsive mobile-first layout, compact remaining quota gauges (text + progressbar, not color alone), pauseable auto-refresh, and merged Provider-quotas sections/columns.
- Plugin cell bars may carry `remaining` / `reset_label` / `state`; unknown remaining stays null (never coerced to 0 % or 100 %).

## 0.3.12 (2026-10-22)

- The login hint for a Claude agent quotes the session name, so `tmux attach -t "T - Claude"` works when pasted. A test checks every attach hint.
- The `lsof` parsing test no longer reads a real `/proc/755` on Linux runners, where that PID can exist and made the test fail at random.

## 0.3.11 (2026-10-21)

- New `agentsmon restart NAME…` / `agentsmon restart --all`: the dashboard restart from the command line, resuming each agent's conversation. Installers use it so running agents load new configuration (such as an MCP server) without a manual click.

## 0.3.10 (2026-10-21)

- Setup discards keys typed while the installer was still running before it asks whether to expose the dashboard, and then prints the resulting dashboard address so a missed answer is visible at once.

## 0.3.9 (2026-10-21)

- Claude Code model is found even when a large record follows the last assistant turn (the dashboard showed only "Claude Code").
- `agentsmon new` waits until the agent screen is fully drawn before sending the first greeting, so Claude Code no longer misses it.

## 0.3.8 (2026-10-21)

- All Telegram bridges share one "Telegram Bridge" availability card. It is green only when every enabled bridge runs; the detail shows how many run and names any that is down. Each bridge is still restarted on its own.
- Bridges are named after the bridged tmux session instead of the config file name and a hash. Older automatic names are renamed on the next registration; custom names are kept.

## 0.3.7 (2026-10-21)

- Dashboard: pressing ✕ on an already stopped agent removes it from monitoring (with confirmation). Conversation files and the CLI are untouched.
- `agentsmon new`: the first greeting is typed and submitted with a separate Enter, so Codex no longer treats it as a paste and leaves it unsent.

## 0.3.6 (2026-10-21)

- `agentsmon new`: when exactly one agent CLI is installed, Enter selects it; the final "Continue?" now defaults to yes.

## 0.3.5 (2026-10-21)

- `agentsmon new` and agent detection now offer only Claude Code, Codex and Antigravity. Aider and Gemini CLI were removed (Antigravity is Google's current agent CLI). Use the config `match` keyword to monitor any other CLI.

## 0.3.4 (2026-10-19)

- Check Codex login status before sending a first greeting; keep the device-code choice untouched on unsigned installations.
- Preserve configured agents, custom daemon settings and explicit stops during setup reruns.
- Detect wrapped gateways through their service owner or listener, including current Hermes launchers.
- Keep stopped agents visible and mark recognized Codex/Claude login screens as needing login without restarting their live processes.
- Bootstrap a missing user systemd bus through own-user lingering during explicit setup/add; keep installed-unit ownership under `su` and enable boot persistence.
- Tolerate exited or non-inspectable process descriptors during boot without inventing conversation identity or crashing discovery.

## 0.3.3 (2026-10-18)

- Restore conversation identity and the recorded model for a single Claude Code
  agent continuing an older transcript. Single Codex and Antigravity agents use
  the same directory fallback when stronger process evidence is unavailable.
- Keep exact resume pins across restarts. Multiple agents of the same kind in one
  directory still require unambiguous evidence; already claimed identities and
  conflicting directory mappings are never borrowed.
- Respect `CODEX_HOME` when resolving a single agent's most recent rollout.

## 0.3.2 (2026-10-18)

- Bind conversation IDs to explicit resume arguments, process-held files or
  unambiguous session creation times. Shared working directories no longer select
  the newest conversation; uncertain identities recover fresh with a visible
  warning instead of resuming another agent's history or staying dead.
- Pin newly created sessions for exact resume. Keep stable CLI menu numbers,
  discover common user-bin installations, confirm the selected agent and reject
  duplicate configured names, including stopped agents.
- Respect systemd user units and launchd jobs when stopping/restarting daemons;
  verify outcomes. Supply missing user-bus environment under `su`, enable selected
  user units and attempt own-user lingering with explicit administrator guidance.
- Monitor each Agent2Telegram config independently. Add idempotent noninteractive
  bridge registration for optional integrations; preserve interpreter, config,
  state and source path without copying credentials.
- Warn that HTTP Basic Auth needs encrypted transport outside localhost.
- Refresh checked-hash bytecode on installer re-runs before any package import,
  including `--no-setup --no-pip`, so same-second upgrades report the new version.
- Decline the known optional fullscreen-renderer dialog with its full descriptive
  text and preceding session output; never dismiss login/trust/permission dialogs.

## 0.3.1 (2026-10-18)

- Bootstrap the configured tmux server from the cron launcher's shell before
  starting Python. On macOS this preserves the OS permission attribution of the
  launcher; it does not grant or bypass TCC permissions. Existing sessions survive.
- Optional `services[].row_details` shows escaped probe details in the shared
  service table, preserving row identities and history. Default remains off.
- English source identifiers and examples, fresh-install instructions for macOS
  and Ubuntu, and corrected documentation of authentication and action controls.
- Refresh the package integrity manifest and verify release metadata consistency.
- Browser acceptance waits for the action's delayed refresh before measuring
  table geometry. No production renderer change is needed for that test race.
- Python 3.10–3.14 CI runs the standard-library test suite, lint and package install.
- Retry transient git update failures three times without prompting. Preserve
  existing checkouts and unmarked non-git directories on failure.
- Recognise legacy unmarked archives only at the default source path with the
  expected project files and no symlinks. Protect other unmarked directories.
- Mark archive installations so re-running the installer can update them using
  a fully staged replacement with rollback on failure, without changing config
  or history outside the source directory.
- Refresh checked-hash Python bytecode before service reloads, so rapid
  same-second updates and rollbacks cannot reuse a stale timestamp cache.

## 0.3.0 (2026-10-15)

### Core/plugin boundary

- Plugin API v1: declarative agent tables, shared renderer, extra columns, scoped actions,
  model/account display detectors and hidden sessions/services. File manifests plus opt-in
  Python entry points. No private runtime imports or deployment names in core.
- Bounded isolated worker processes, background collection, stale result expiration, visible
  error cards and guarded/namespaced action endpoint. Plugin auth is never implicit.
- Package manifest, `doctor` integrity/plugin diagnostics, clean/modified footer.
- Shared fixed-column tables, safe clipping for long IDs, optional usage-column width,
  English labels, service-card subrows/history. Legacy modules retained.
- Clean-HOME unattended installer mode and real-browser/HTTP/plugin acceptance tests.
- Legacy service modules reject non-boolean health and non-finite/negative metrics, keeping
  malformed output from faking a healthy state or breaking the dashboard's JSON.

### Fixes and community review

- Keepalive atomically refreshes an existing cron launcher, including when agent supervision
  is disabled. This repairs the first upgrade executed by older updater code on shared hosts.
- Installer parses its full body before a pull can overwrite its own file. Uninstall removes
  an unmodified local CLI wrapper and suggests manual PATH cleanup without editing shell rc files.
- Initial/expired plugin checks show neutral loading with no stale rows or actions. Actual
  errors/timeouts stay red. Dashboard startup announces its URL only after auth/bind succeeds.
- Uninstall uses PID-aware dashboard shutdown and a current-user, full-command keepalive
  match. Daemon Stop is also scoped to the current user, with patterns separated from options.
- Headless installation without a controlling terminal succeeds and prints `agentsmon setup`.
- Dashboard launcher and stop fallback are scoped to the current user. Updates stop via the
  PID-aware service helper and regenerate an existing launcher instead of broad `pkill`.
- Off-loopback auth errors point to `agentsmon setup`. Service uptime displays the observed
  streak duration, not a count of healthy day buckets; tests reap their child processes.
- Healthy daemon/port agents show `Running`, matching tmux agents. Health-check latency
  remains in the status tooltip; explicit error/down states retain precedence.
- [Issue #4](https://github.com/petrludwig-collab/AgentsMonitoring/issues/4), reported by
  **jenicek001**: close SQLite uptime connections after transactions, including initialization
  failures. Also close Antigravity connections on query errors, reviewed against
  jenicek001's `fix/sqlite-fd-leak` fork (fb7e3883, 95c28661).
- [Issue #6](https://github.com/petrludwig-collab/AgentsMonitoring/issues/6), reported by
  **jenicek001**: Hermes reads its own model configuration regardless of key order and never
  borrows Codex's model on failure. Wizard no longer freezes the discovered model as a manual
  tag. Manual existing overrides remain operator-controlled.
- [PR #3](https://github.com/petrludwig-collab/AgentsMonitoring/pull/3), **tomasvida**:
  adopt actual Claude process `--model` over an ambiguous shared-cwd transcript. Preserve
  exact-session transcript precedence for runtime `/model` changes; ignore helper argv.
- [PR #8](https://github.com/petrludwig-collab/AgentsMonitoring/pull/8), **TomasErnest**:
  account-scoped **opt-in Usage plugin** inspired by the proposal. Adapted to explicit local
  snapshots instead of vendor-global caches and automatic OAuth requests. Missing/expired
  data is visibly unknown; no invented quotas. This is a reviewed adaptation, not a merge of
  the original PR's networking implementation.

See [community review](docs/COMMUNITY-REVIEW.md) for other fork findings and scope decisions.
