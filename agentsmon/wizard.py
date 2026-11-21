"""Setup wizard — `agentsmon setup`.

Auto-detects the agents already running in tmux, lets you choose which to supervise, proposes a
restart command for each, optionally watches common daemons (OpenClaw, Hermes), writes the
config, and installs the boot service. Designed to need almost no typing.
"""
from __future__ import annotations

import copy
import json
import os
import shlex
import shutil
import subprocess
import time
import sys
from pathlib import Path

from . import config, detect, service

#: Auto-derived restart command per kind ({id} = session id). Includes the "run unattended" flag,
#: since a supervised agent must come back able to work without an approval prompt.
RESTART_DEFAULTS = {
    "claude-code": "claude --dangerously-skip-permissions --resume {id}",
    "codex": "codex --dangerously-bypass-approvals-and-sandbox resume {id}",
    "antigravity": "agy --conversation {id} --dangerously-skip-permissions",
}
MATCH_KEYWORD = {"claude-code": "claude", "codex": "codex", "antigravity": "agy"}

#: For `agentsmon new` — create a brand-new agent. Per kind: the CLI binary to check, a human
#: label and fresh launch command. Exact pins resume history; unknown identities
#: recover with a fresh conversation and a visible warning.
AGENT_TYPES = [
    {"kind": "claude-code", "label": "Claude Code", "bin": "claude",
     "launch": "claude --dangerously-skip-permissions"},
    {"kind": "codex", "label": "Codex", "bin": "codex",
     "launch": "codex --dangerously-bypass-approvals-and-sandbox"},
    {"kind": "antigravity", "label": "Antigravity", "bin": "agy",
     "launch": "agy --dangerously-skip-permissions"},
]


def pretrust_claude(cwd: str, home: Path | None = None) -> str:
    """Answer Claude Code's first-run prompts up front. Returns what was recorded, "" if nothing.

    Claude Code asks "Is this a project you created or one you trust?" the first time it runs in a
    directory, and waits for an answer. Launched detached into tmux there is nobody to answer: the
    agent never starts, writes no session, and the dashboard stays empty — it looks like the tool
    failed to add it (2026-09-03, on a freshly installed server). Sending the greeting afterwards
    doesn't reliably save it either, because the keys land before the TUI is up.

    The user has just named this directory for an agent they are creating, so recording that trust
    is what they asked for — but it IS a security prompt, so `new()` says out loud that it did it.
    Written atomically: a half-written config would lock the user out of Claude entirely.
    """
    p = (home or Path.home()) / ".claude.json"
    try:
        d = json.loads(p.read_text("utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return ""                      # unreadable or not JSON — never overwrite what we can't read
    if not isinstance(d, dict):
        return ""
    recorded = []
    if not d.get("hasCompletedOnboarding"):
        d["hasCompletedOnboarding"] = True
        recorded.append("onboarding")
    projects = d.setdefault("projects", {})
    project = projects.setdefault(cwd, {})
    if not project.get("hasTrustDialogAccepted"):
        project["hasTrustDialogAccepted"] = True
        recorded.append(f"trusted folder {cwd}")
    if recorded and not _write_json(p, d):
        return ""
    recorded += _skip_bypass_warning(home or Path.home())
    return " + ".join(recorded)


def _write_json(p: Path, data: dict) -> bool:
    """Atomic, owner-only write. A half-written config would lock the user out of Claude."""
    tmp = p.with_name(p.name + ".agentsmon-tmp")
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(data, indent=2), "utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, p)
        return True
    except OSError:
        tmp.unlink(missing_ok=True)
        return False


def _skip_bypass_warning(home: Path) -> list[str]:
    """Third first-run gate. `--dangerously-skip-permissions` prints a WARNING screen whose
    highlighted option is "No, exit", and waits. It is recorded in ~/.claude/settings.json, NOT
    in ~/.claude.json, so answering the trust dialog alone still leaves the agent stuck on a
    brand-new machine — measured on a pristine HOME, 2026-09-03.

    Every agent this wizard launches runs in that mode; the user chose that by creating the
    agent, and `new` prints what was recorded."""
    p = home / ".claude" / "settings.json"
    try:
        d = json.loads(p.read_text("utf-8")) if p.exists() else {}
    except (OSError, ValueError):
        return []                      # unreadable or not JSON — never overwrite what we can't read
    if not isinstance(d, dict) or d.get("skipDangerousModePermissionPrompt"):
        return []
    d["skipDangerousModePermissionPrompt"] = True
    return ["bypass-mode warning"] if _write_json(p, d) else []


def claude_is_logged_in(home: Path | None = None) -> bool:
    """Best-effort: does Claude Code have credentials to work with?

    The one first-run gate that CANNOT be pre-answered is the login — and it must not be, it is
    authentication. A fresh agent then starts fine but sits at "Not logged in · Run /login" and
    does nothing, which looks exactly like a broken tool. Saying so out loud beats a silent
    half-success. Env-var auth counts; a false "logged in" only costs us the hint."""
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return True
    return ((home or Path.home()) / ".claude" / ".credentials.json").exists()


def codex_is_logged_in(executable: str) -> bool:
    """Check the selected CLI's credentials without printing or copying them."""
    try:
        result = subprocess.run([executable, 'login', 'status'], stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def wait_for_tui(name: str, timeout: float = 25.0) -> bool:
    """Wait until the tmux pane runs something other than a shell — i.e. the agent TUI is up.

    The greeting used to be sent after a flat 4 s. On a slow or cold machine the TUI isn't up yet,
    the keys go nowhere, and the agent registers neither session id nor model until the user types
    something themselves."""
    import time
    shells = {"sh", "bash", "zsh", "fish", "dash", "-sh", "-bash", "-zsh"}
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        r = subprocess.run(["tmux", "display-message", "-p", "-t", name, "#{pane_current_command}"],
                           capture_output=True, text=True)
        if r.returncode == 0 and r.stdout.strip() and r.stdout.strip() not in shells:
            break
        time.sleep(0.5)
    else:
        return False
    # The process is up, but its input box may still be drawing (Claude Code takes a few
    # seconds); keys sent now are lost. Wait until the screen stops changing.
    previous, stable = None, 0
    while time.monotonic() < deadline:
        r = subprocess.run(["tmux", "capture-pane", "-p", "-t", name], capture_output=True, text=True)
        screen = r.stdout if r.returncode == 0 else ''
        stable = stable + 1 if screen.strip() and screen == previous else 0
        if stable >= 3:
            return True
        previous = screen
        time.sleep(0.5)
    return True


def _auto_restart(a: dict) -> str:
    """Build the restart command for a detected agent — no user typing needed."""
    tpl = RESTART_DEFAULTS.get(a["kind"], "")
    if not tpl:
        return ""
    sid = a.get("session_id")
    if a.get('identity_conflict'):
        return ''
    if sid:
        command = tpl.replace("{id}", sid)
        binary, _, rest = command.partition(' ')
        return shlex.quote(find_cli(binary) or binary) + (' '+rest if rest else '')
    return '' if '{id}' in tpl else tpl


def find_cli(binary):
    found = shutil.which(binary)
    if found:
        return os.path.abspath(found)
    for folder in (Path.home()/'.local/bin', Path.home()/'.npm-global/bin', Path('/opt/homebrew/bin')):
        candidate = folder/binary
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def choose_agent():
    choices = [{**t, 'executable':find_cli(t['bin'])} for t in AGENT_TYPES]
    for i, entry in enumerate(choices, 1):
        missing = ' (not installed / not on PATH)' if not entry['executable'] else ''
        print(f"  [{i}] {entry['label']}{missing}")
    installed = [i for i, entry in enumerate(choices, 1) if entry['executable']]
    # Exactly one installed CLI → Enter picks it; otherwise the choice stays explicit.
    default = str(installed[0]) if len(installed) == 1 else ''
    prompt = f'\nNumber [{default}]: ' if default else '\nNumber (required; Ctrl-C to cancel): '
    while True:
        try:
            value = input(prompt).strip() or default
        except EOFError:
            return None
        if not value.isdigit() or not 1 <= int(value) <= len(choices):
            print('Choose an available agent by its displayed number.')
            continue
        selected = choices[int(value)-1]
        if not selected['executable']:
            print('That CLI is unavailable. Check PATH or open a new terminal after installing it.')
            continue
        return selected


def name_taken(name, cfg, sessions):
    return name in ({s['name'] for s in sessions} | {x.get('name') for key in
                     ('agents','daemons','services','pinned_daemons') for x in cfg.get(key, [])})


def primary_ip() -> str:
    """This machine's primary outbound IP — the usable address when the dashboard is exposed
    (``0.0.0.0``). Falls back to localhost if offline."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"
COMMON_DAEMONS = [
    {"name": "OpenClaw", "pattern": "openclaw", "binary": "openclaw", "name_color": "red",
     "health_url": "http://127.0.0.1:18789/health",
     "restart": "nohup openclaw gateway > ~/openclaw.log 2>&1 &"},
    # Liveness = health_url OR process match (either signal counts, see detect.daemon_status),
    # so a health-endpoint change in a new Hermes version can't blind us while the process runs.
    # The pattern must match BOTH Hermes launch forms: the old `…/bin/hermes gateway run` AND the
    # current venv/pip form `…python -m hermes_cli.main gateway run`. A bare "hermes gateway"
    # matches only the old form (the new one has `hermes_cli.main gateway`, no "hermes gateway"),
    # so we alternate. Neither form matches the OpenClaw node gateway (`…/index.js gateway`).
    {"name": "Hermes", "pattern": r"hermes gateway run|hermes_cli\.main gateway",
     "unit_hint": "hermes-gateway.service",
     "binary": "hermes", "name_color": "gold",
     "health_url": "http://127.0.0.1:8642/health",
     "restart": "nohup hermes gateway run --replace > ~/hermes.log 2>&1 &"},
]


def _running(pattern: str) -> bool:
    return bool(pattern) and subprocess.run(["pgrep", "-f", pattern],
                                            capture_output=True).returncode == 0


def _parse_selection(text: str, n: int) -> set:
    """Parse a checklist answer: '' or 'all' → everything, 'none' → nothing, else the listed
    numbers (comma/space separated)."""
    t = text.strip().lower()
    if t in ("", "all", "a"):
        return set(range(1, n + 1))
    if t in ("none", "n", "-"):
        return set()
    out = set()
    for part in t.replace(",", " ").split():
        if part.isdigit() and 1 <= int(part) <= n:
            out.add(int(part))
    return out


def _ask(prompt: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    try:
        val = input(f"{prompt}{suffix}: ").strip()
    except EOFError:
        return default
    return val or default


def _flush_typeahead() -> None:
    """Drop keys pressed while the installer was still downloading, so a stray Enter can't
    silently answer the next question with its default."""
    try:
        import termios
        if sys.stdin.isatty():
            termios.tcflush(sys.stdin, termios.TCIFLUSH)
    except (ImportError, OSError, ValueError):
        pass


def _yes(prompt: str, default_yes: bool = True) -> bool:
    d = "Y/n" if default_yes else "y/N"
    ans = _ask(f"{prompt} ({d})").lower()
    if not ans:
        return default_yes
    return ans in ("y", "yes")


def _ask_secret(prompt: str) -> str:
    # Show an asterisk per typed character (same UX as the wiki installer), so the user can see
    # the password is being captured. Falls back to no-echo getpass when stdin isn't a real
    # terminal (piped/headless) or raw mode isn't available.
    if sys.stdin.isatty():
        try:
            import termios
            import tty
            sys.stdout.write(f"{prompt}: ")
            sys.stdout.flush()
            fd = sys.stdin.fileno()
            old = termios.tcgetattr(fd)
            chars: list[str] = []
            try:
                tty.setraw(fd)
                while True:
                    ch = sys.stdin.read(1)
                    if ch in ("\r", "\n", ""):
                        break
                    if ch == "\x03":                    # Ctrl-C
                        raise KeyboardInterrupt
                    if ch in ("\x7f", "\b"):            # backspace → erase one star
                        if chars:
                            chars.pop()
                            sys.stdout.write("\b \b")
                            sys.stdout.flush()
                        continue
                    chars.append(ch)
                    sys.stdout.write("*")
                    sys.stdout.flush()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
            sys.stdout.write("\n")
            sys.stdout.flush()
            return "".join(chars).strip()
        except KeyboardInterrupt:
            raise
        except Exception:
            pass
    import getpass
    try:
        return getpass.getpass(f"{prompt}: ").strip()
    except (EOFError, Exception):
        return _ask(prompt)


def _agent_entry(a: dict) -> dict:
    return {"name": a["name"], "label": a["label"],
            "match": MATCH_KEYWORD.get(a["kind"], a["kind"]),
            "restart": _auto_restart(a),
            "cwd": detect._session_cwd(a["name"]) or str(Path.home()),
            "enabled": True, 'identity_required':a['kind'] in detect.RESUME_TEMPLATES,
            'kind':a['kind'], 'session_id':a.get('session_id')}


#: The system-wide availability card. It's synthetic — not tied to any single component — and is
#: "up" only when every monitored agent + daemon is up (computed in probe._system_health). Its
#: latency metric is the average across all health-checked components.
SYSTEM_SERVICE = {"name": "Multi-Agent System Availability", "kind": "system",
                  "metric": "system_latency"}


def migrate_config(cfg: dict) -> bool:
    """Bring an older config up to the current schema. Currently: replace the per-daemon
    availability cards (OpenClaw/Hermes, or an OpenClaw-health card) with the single synthetic
    *Multi-Agent System Availability* card, while keeping genuinely separate cards (e.g. the
    Telegram Bridge). Idempotent. Returns True if anything changed."""
    svcs = cfg.get("services", [])
    pinned_pats = {d.get("process") for d in cfg.get("pinned_daemons", []) if d.get("process")}
    pinned_names = {d.get("name") for d in cfg.get("pinned_daemons", []) if d.get("name")}
    kept = []
    for s in svcs:
        if s.get("kind") == "system":
            continue                                   # re-inserted canonically below
        url = s.get("health_url") or ""
        is_daemon_card = (s.get("process") in pinned_pats or s.get("name") in pinned_names
                          or url.endswith(":18789/health")
                          or s.get("name") in ("Multi-Agent System Availability", "Multi-agent system availability"))  # both spellings accepted in existing configs
        if not is_daemon_card:
            kept.append(s)
    new_services = [dict(SYSTEM_SERVICE)] + kept
    changed = False
    if new_services != svcs:
        cfg["services"] = new_services
        changed = True
    # Backfill the Telegram @username for known daemons (OpenClaw/Hermes) that don't have one yet,
    # so their t.me icon appears on existing installs without a re-setup.
    for pin in cfg.get("pinned_daemons", []):
        if pin.get("name") and not pin.get("telegram"):
            bot = detect.daemon_telegram_bot(pin["name"])
            if bot:
                pin["telegram"] = bot
                changed = True
    return changed


def _daemon_entries(d: dict) -> tuple:
    """(keepalive daemon, pinned Persistent-Agents row) for a daemon. Daemons no longer get their
    own availability card — their health folds into the synthetic Multi-Agent System card; they
    just appear as a highlighted row at the top of Persistent Agents (with live model + colour)."""
    daemon = dict(d)
    from . import managed
    owner = managed.discover(d)
    if owner:
        daemon['manager'] = owner
    pinned = {"name": d["name"], "process": d["pattern"]}
    if owner:
        pinned['manager'] = owner
    if d.get("health_url"):
        pinned["health_url"] = d["health_url"]
    if d.get("name_color"):
        pinned["name_color"] = d["name_color"]
    # Daemons with their own native Telegram bot (OpenClaw, Hermes) — auto-fill the @username so
    # the dashboard shows a t.me link (any daemon can also set a `telegram` field explicitly).
    tg = d.get("telegram") or detect.daemon_telegram_bot(d.get("name", ""))
    if tg:
        pinned["telegram"] = tg
    # Model/account detection stays live. Only manually entered tags override it.
    return daemon, pinned


def _scan_candidates(known: set) -> list:
    """tmux agents (running) + known daemons (running or installed), excluding names in *known*."""
    out = []
    for a in (x for x in detect.discover_agents() if x["alive"]):
        if a["name"] not in known:
            out.append({"kind": "agent", "obj": a, "display": f"{a['name']}  →  {a['label']}"})
    for d in COMMON_DAEMONS:
        from . import managed
        owner = managed.discover(d)
        running = (managed.active(owner) is True if owner else bool(managed.process_ids(d)))
        if (owner or running or find_cli(d.get("binary", ""))) and d["name"] not in known:
            out.append({"kind": "daemon", "obj": {**d, **({'manager':owner} if owner else {})},
                        "display": f"{d['name']}  (daemon{'' if running else ', not running'})"})
    from . import bridges
    for bridge in bridges.candidates():
        if bridge['name'] not in known:
            out.append({'kind':'bridge', 'obj':bridge, 'display':bridge['name']})
    return out


def add() -> int:
    """`agentsmon add` — detect agents/daemons not yet monitored and add them, no full re-setup."""
    if not config.DEFAULT_PATH.exists():
        print("No config yet — run 'agentsmon setup' first.")
        return 1
    cfg = config.load()
    # Ensure the synthetic system availability card exists (configs from before it was introduced
    # won't have it). It carries the health of the whole system, not any single daemon.
    svcs = cfg.setdefault("services", [])
    if not any(s.get("kind") == "system" for s in svcs):
        svcs.insert(0, dict(SYSTEM_SERVICE))
    known = set()
    for key in ("agents", "daemons", "services", "pinned_daemons"):
        known |= {x.get("name") for x in cfg.get(key, []) if x.get("name")}
    candidates = _scan_candidates(known)
    from . import managed
    for current in cfg.get('daemons', []):
        if config.enabled_error(current) or not current.get('enabled', True):
            continue  # An explicit stop is not a broken service installation, nor an unreadable one.
        defaults = next((d for d in COMMON_DAEMONS if d['name'] == current.get('name')), {})
        owner = managed.discover({**defaults, **current})
        if owner and owner.get('type') == 'systemd':
            props = managed.properties(owner['unit'])
            if not current.get('manager') or props.get('UnitFileState') != 'enabled' or props.get('ActiveState') != 'active':
                candidates.append({'kind':'daemon', 'obj':{**current, 'manager':owner},
                                   'display':current['name']+' (repair user-service supervision)'})
    if not candidates:
        print("Nothing new — everything detected is already monitored. ✓")
        return 0
    print("New (not yet monitored). Select which to add:\n")
    for i, c in enumerate(candidates, 1):
        print(f"  [{i}] {c['display']}")
    chosen = _parse_selection(_ask("\nNumbers, 'all', or 'none'", "all"), len(candidates))
    added = 0
    for i, c in enumerate(candidates, 1):
        if i not in chosen:
            continue
        if c["kind"] == "agent":
            cfg.setdefault("agents", []).append(_agent_entry(c["obj"]))
        elif c["kind"] == "daemon":
            dmn, pin = _daemon_entries(c["obj"])
            if dmn.get('manager'):
                from . import managed
                ok, notes = managed.activate(dmn['manager'])
                for note in notes:
                    print(note)
                if not ok:
                    return 1
            cfg['daemons'] = [d for d in cfg.get('daemons', []) if d.get('name') != dmn['name']] + [dmn]
            cfg['pinned_daemons'] = [d for d in cfg.get('pinned_daemons', []) if d.get('name') != pin['name']] + [pin]
        elif c["kind"] == "bridge":
            from . import bridges
            bridges.register(cfg, c['obj'])
        added += 1
    if not added:
        print("Nothing selected.")
        return 0
    config.save(cfg)
    print(f"\n✓ Added {added}. Reloading the boot service + dashboard…")
    service.install()
    print("Done — check:  agentsmon status")
    return 0


def new() -> int:
    """`agentsmon new` — create a brand-new agent: pick a type, give it a name. It's launched in a
    fresh tmux session and immediately registered for keepalive + the dashboard."""
    if not shutil.which("tmux"):
        print("⚠️  tmux not found — agents run inside tmux. Install tmux first.")
        return 1
    print("=== Create a new agent ===\n")
    print("Step 1 — choose the agent type:\n")
    chosen = choose_agent()
    if chosen is None:
        return 1

    cfg = config.load()
    sessions = detect.tmux_sessions()
    name = ""
    while not name:
        try:
            name = input('\nStep 2 – name for the agent: ').strip()
        except EOFError:
            return 1
        if not name:
            continue
        if any(c in name for c in ".:"):
            print("  Name can't contain '.' or ':' (tmux limitation) — pick another.")
            name = ""
        elif name_taken(name, cfg, sessions):
            print(f"  '{name}' is already configured or running — pick another name.")
            name = ""

    cwd = str(Path(_ask("Working directory", str(Path.home()))).expanduser())
    print(f"Creating {chosen['label']}: {name} in {cwd}. Executable: {chosen['executable']}")
    if not _yes('Continue?', default_yes=True):
        return 0

    # Answer Claude's first-run prompts BEFORE launching, or the agent sits on the trust dialog
    # forever and never appears anywhere.
    if chosen["kind"] == "claude-code":
        recorded = pretrust_claude(cwd)
        if recorded:
            print(f"  ✓ pre-answered Claude's first-run prompts ({recorded})")
        if not claude_is_logged_in():
            print("  ! Claude is not logged in yet — the agent will start but can't work.")
            print(f"    Log in once:  tmux attach -t {shlex.quote(name)}   then  /login")

    # Create the session detached and launch the agent inside it.
    mk = subprocess.run(["tmux", "new-session", "-d", "-s", name, "-c", cwd], capture_output=True, text=True)
    if mk.returncode != 0:
        print(f"✗ couldn't create tmux session: {mk.stderr.strip()}")
        return 1
    launch = shlex.quote(chosen['executable'])+' '+chosen['launch'].partition(' ')[2]
    subprocess.run(["tmux", "send-keys", "-t", name, launch.strip(), "Enter"], capture_output=True)

    cfg = config.load()
    cfg.setdefault("agents", []).append({
        "name": name, "label": chosen["label"], "match": MATCH_KEYWORD[chosen["kind"]],
        "restart": '' if chosen['kind'] in detect.RESUME_TEMPLATES else launch.strip(),
        'fresh_start':launch.strip(),
        'kind':chosen['kind'], 'identity_required':chosen['kind'] in detect.RESUME_TEMPLATES,
        "cwd": cwd, "enabled": True})
    config.save(cfg)
    print(f"\n✓ Created '{name}' ({chosen['label']}), launched in tmux, and added to monitoring.")
    service.install()
    # Kick off a first turn so the agent registers its session id + model right away — a brand-new
    # session shows neither until its first message. Wait for the TUI to actually come up first;
    # a flat sleep sent the keys into a shell on a cold machine and nothing registered.
    if chosen['kind'] == 'codex' and not codex_is_logged_in(chosen['executable']):
        print('  ! Codex is not logged in yet (or login status could not be verified).')
        print(f'    Run tmux attach -t {shlex.quote(name)}, press Esc if needed and choose Sign in with Device Code.')
    elif not wait_for_tui(name):
        print("  ! the agent TUI didn't come up in time — send it a first message yourself")
    else:
        if chosen['kind'] == 'claude-code':
            from .dialogs import dismiss_optional_renderer
            if dismiss_optional_renderer(name):
                print('  Declined the optional fullscreen renderer experiment.')
        send_first_message(name, "Hello! Briefly introduce yourself.")
    print(f"\nAttach to interact (or finish login):  tmux attach -t {shlex.quote(name)}")
    print('It now shows on the dashboard. Exact resume is pinned after session identity is verified.')
    return 0


def send_first_message(name: str, text: str) -> None:
    """Type the text, pause, then press Enter separately. Codex treats text and Enter that arrive
    in one burst as a paste, so the Enter becomes a newline and the message is never sent."""
    subprocess.run(["tmux", "send-keys", "-t", name, "-l", text], capture_output=True)
    time.sleep(0.8)
    subprocess.run(["tmux", "send-keys", "-t", name, "Enter"], capture_output=True)


def run() -> int:
    print("=== Agents Monitoring setup ===\n")
    if not shutil.which("tmux"):
        print("⚠️  tmux not found — agents run inside tmux, so install tmux first.")
    print("Scanning for agents and daemons…\n")
    candidates = _scan_candidates(set())   # everything (fresh setup)
    previous = copy.deepcopy(config.load())
    # Missing/stopped entries still belong to the user's configuration. Offer
    # them without interpreting setup's default selection as permission to start.
    seen = {c['obj']['name'] for c in candidates}
    for key, kind in (('agents', 'agent'), ('daemons', 'daemon')):
        for entry in previous.get(key, []):
            if entry['name'] not in seen:
                candidates.append({'kind':kind, 'obj':entry, 'existing':True,
                                   'display':entry['name']+' (configured; keep current settings)'})
                seen.add(entry['name'])

    chosen: set = set()
    if not candidates:
        print("  No running agents or daemons found.")
        print("  (Start your agents in tmux first, then re-run setup.)")
    else:
        print("Found the following. Select which to monitor + auto-restart:\n")
        for i, c in enumerate(candidates, 1):
            print(f"  [{i}] {c['display']}")
        print()
        sel = _ask("Numbers to include (comma-separated), 'all', or 'none'", "all")
        chosen = _parse_selection(sel, len(candidates))

    agents, daemons, bridge_entries = [], [], []
    for i, c in enumerate(candidates, 1):
        if i not in chosen:
            continue
        if c["kind"] == "agent":
            old = next((a for a in previous.get('agents', []) if a['name'] == c['obj']['name']), None)
            agents.append(dict(old) if old is not None else _agent_entry(c['obj']))
        elif c['kind'] == 'bridge':
            bridge_entries.append(c['obj'])
        else:
            old = next((d for d in previous.get('daemons', []) if d['name'] == c['obj']['name']), {})
            daemons.append({**c['obj'], **old})
    print(f"\n  → will monitor {len(agents)} agent(s) + {len(daemons)} daemon(s), with auto-restart.")

    # Dashboard reach: localhost always works; ask whether to also expose it on the machine's IP.
    print("\nThe dashboard is always reachable on this machine (http://127.0.0.1).")
    _flush_typeahead()
    expose = _yes("Also make it reachable from outside — on the server's IP / the internet?",
                  default_yes=False)
    host = "0.0.0.0" if expose else "127.0.0.1"
    port = _ask("Dashboard port", "8765")
    shown = f"http://{primary_ip()}:" if expose else "http://127.0.0.1:"
    print(f"  → Dashboard: {shown}{port if port.isdigit() else 8765}"
          + ("" if expose else "  (this machine only; rerun setup to change)"))

    cfg = config.load()
    cfg["dashboard"].update({"host": host, "port": int(port) if port.isdigit() else 8765})
    if expose:
        print('WARNING: HTTP Basic Auth over plain HTTP exposes the password on the network.')
        print('Prefer localhost with an SSH tunnel or encrypted Tailscale HTTPS access.')
    # HTTP auth — default yes when exposed.
    if _yes("Protect the dashboard with a login (HTTP auth)?", default_yes=expose):
        from . import dashboard
        user = _ask("    username", "admin")
        pw = _ask_secret("    password (hidden)")
        while not pw:
            pw = _ask_secret("    password can't be empty (hidden)")
        cfg["dashboard"]["auth"] = {"user": user, "pwhash": dashboard.password_hash(pw)}
        print("    ✓ HTTP auth enabled (password stored only as a hash).")
    else:
        cfg["dashboard"].pop("auth", None)

    # Build the full dashboard by default (the layout we run ourselves): each selected daemon
    # becomes a keepalive target and a highlighted row at the top of Persistent Agents (with live
    # model + colour). tmux agents already carry their maker colour automatically. The first
    # availability card is the synthetic *Multi-Agent System Availability* — health of the whole
    # system, independent of any single daemon.
    cfg["agents"] = agents
    cfg["daemons"], cfg["pinned_daemons"] = [], []
    cfg["services"] = [dict(SYSTEM_SERVICE)]
    for d in daemons:
        dmn, pin = _daemon_entries(d)
        old_pin = next((p for p in previous.get('pinned_daemons', []) if p['name'] == d['name']), {})
        pin.update(old_pin)
        pin['enabled'] = dmn.get('enabled', True)
        invalid = config.enabled_error(dmn)
        if invalid:
            print(f"  ✗ daemon '{dmn['name']}': {invalid} — its service is not activated")
        elif dmn.get('manager') and dmn.get('enabled', True):
            from . import managed
            ok, notes = managed.activate(dmn['manager'])
            for note in notes:
                print(note)
            if not ok:
                return 1
        cfg["daemons"].append(dmn)
        cfg["pinned_daemons"].append(pin)
    # Auto-add a Telegram Bridge availability card if an Agent2Telegram bridge is running,
    # AND keep it alive (restart from its current command line, so it returns after a reboot).
    from . import bridges
    for bridge in bridge_entries:
        bridges.register(cfg, bridge)
    path = config.save(cfg)
    print(f"\n✓ Saved config to {path}")
    print(f"  Supervising {len(agents)} agent(s), watching {len(daemons)} daemon(s).")

    if _yes("\nInstall the boot service now (keepalive + dashboard, start on login/boot)?"):
        service.install()
    print("\nAll set. Check status anytime with:  agentsmon status")
    if host in ("0.0.0.0", "::"):
        print(f"Dashboard: http://{primary_ip()}:{port}   (local: http://127.0.0.1:{port})")
    else:
        print(f"Dashboard: http://127.0.0.1:{port}")
    return 0
