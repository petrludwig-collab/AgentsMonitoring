"""Service probing — record an up/down sample for each configured service.

A *service* is anything you want SLA/uptime history for (a daemon, a gateway, a bridge): it has a
``process`` pattern (pgrep) and/or a ``health_url``. ``probe_once`` writes one sample per service
to the uptime DB. The dashboard runs this on a background thread, so simply leaving the dashboard
open builds the history — no separate probe service required.
"""
from __future__ import annotations

import http.client
import json
import math
import os
import subprocess
import time
import urllib.parse

from . import db


def _proc_up(pattern: str) -> bool:
    if not pattern:
        return True
    return subprocess.run(["pgrep", "-f", pattern], capture_output=True).returncode == 0


def _http(url: str, timeout: float = 4) -> tuple[bool, float | None]:
    """Health check returning (ok, **warm** round-trip latency). We do a warm-up request to
    establish the TCP+TLS connection, then time a second request on the same connection — so the
    reported latency is the server's actual response time, not the one-off handshake cost (which
    for a remote HTTPS endpoint can be ~55 ms and would otherwise dwarf the real latency)."""
    p = urllib.parse.urlparse(url)
    path = (p.path or "/") + (f"?{p.query}" if p.query else "")
    cls = http.client.HTTPSConnection if p.scheme == "https" else http.client.HTTPConnection
    conn = None
    try:
        conn = cls(p.hostname, p.port, timeout=timeout)
        conn.request("GET", path)          # warm-up: TCP + TLS handshake happens here
        conn.getresponse().read()
        t0 = time.time()                   # timed request reuses the established connection
        conn.request("GET", path)
        r = conn.getresponse()
        r.read()
        return (200 <= r.status < 400), round(time.time() - t0, 3)
    except Exception:
        return False, None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _command(cmd: str, timeout: float = 15) -> tuple[bool, float | None, str]:
    """Backwards-compatible 3-tuple; see ``_run`` for the full contract including ``rows``."""
    ok, lat, detail, _rows = _run(cmd, timeout)
    return ok, lat, detail


def _rows_from(d: dict) -> list[dict]:
    """Normalise the optional ``rows`` list of a module's JSON: one line per sub-check
    (a website, a database, a tunnel…), each with its own uptime history. ``name`` and ``up`` are
    required, ``latency_ms``, ``detail`` and ``url`` optional. Junk is skipped, never fatal."""
    out = []
    rows=d.get('rows') or []
    if not isinstance(rows,list): return []
    for r in rows[:50]:
        if not isinstance(r, dict) or not r.get("name") or type(r.get('up')) is not bool:
            continue
        lat = r.get("latency_ms")
        try:
            lat = round(float(lat) / 1000, 3) if lat is not None else None
            if lat is not None and (not math.isfinite(lat) or lat<0): lat=None
        except (TypeError, ValueError):
            lat = None
        out.append({"name": str(r["name"])[:80], "up": bool(r["up"]), "latency": lat,
                    "detail": str(r.get("detail") or "")[:200], "url": str(r.get("url") or "")[:200]})
    return out[:50]


def _run(cmd: str, timeout: float = 15) -> tuple[bool, float | None, str, list[dict]]:
    """Run a check command/script. Exit code 0 = up. If stdout is a JSON object it may carry
    ``up`` (bool), ``latency_ms`` (number) and ``detail`` (str) — that is the whole module
    contract, so a module can be a three-line shell script or anything that prints JSON.
    Without JSON, latency is the command's wall time and detail its first output line."""
    t0 = time.time()
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout,
                           env={**os.environ, "AGENTSMON_MODULE": "1"})
    except subprocess.TimeoutExpired:
        return False, None, f"timeout after {timeout:.0f}s", []
    except OSError as e:
        return False, None, f"cannot run: {e.__class__.__name__}", []
    lat = round(time.time() - t0, 3)
    ok = r.returncode == 0
    rows: list[dict] = []
    out = (r.stdout or "").strip()
    detail = (out or (r.stderr or "").strip()).splitlines()[0][:200] if (out or r.stderr) else ("up" if ok else f"exit {r.returncode}")
    if out.startswith("{"):
        try:
            d = json.loads(out)
            if isinstance(d, dict):
                if "up" in d:
                    if type(d['up']) is not bool: raise ValueError('Invalid health flag')
                    ok = bool(d["up"]) and ok
                if d.get("latency_ms") is not None:
                    lat = round(float(d["latency_ms"]) / 1000, 3)
                    if not math.isfinite(lat) or lat<0: raise ValueError('Invalid metric')
                if d.get("detail"):
                    detail = str(d["detail"])[:200]
                rows = _rows_from(d)
        except (ValueError, TypeError):
            return False,None,'Invalid module JSON or metrics',[]
    return ok, lat, detail, rows


def _fresh_sample(name: str, cfg: dict) -> dict | None:
    """Latest recorded sample for a service, if it is younger than two probe intervals."""
    cur = db.last(name)
    if not cur:
        return None
    max_age = 2 * int(cfg.get("probe", {}).get("interval_seconds", 60)) + 5
    return cur if (time.time() - cur["ts"]) <= max_age else None


def _system_health(cfg: dict) -> tuple[bool, float | None, str]:
    """Availability of the **whole multi-agent system**, not any single component.

    Strict rule (chosen deliberately): the system is *up* only when **every** monitored
    component is up — all configured agents alive, all daemons running, all pinned daemons and
    real services healthy. Any one down = a system outage. Latency = the average current latency
    across all health-checked components (a single system-wide number)."""
    from . import config as _config, detect
    down: list[str] = []
    lats: dict[str, float] = {}

    alive = {a["name"] for a in detect.discover_agents(_config.agent_matches(cfg))
             if a.get("alive") and not a.get('needs_login')}
    for a in cfg.get("agents", []):
        if a.get("enabled", True) and a.get("name") and a["name"] not in alive:
            down.append(a["name"])

    # Daemons (keepalive list) + pinned daemons + real (non-system) services. Anything that
    # advertises a health endpoint is judged by that endpoint — authoritative. The process
    # pattern is the liveness signal ONLY when there's no health_url, since command lines vary
    # by install method (venv / pip --user / pipx) and a stale regex would fake an outage.
    checks = list(cfg.get("daemons", []))
    checks += list(cfg.get("pinned_daemons", []))
    checks += [s for s in cfg.get("services", []) if s.get("kind") not in ("system", "bridges")]
    for c in checks:
        # Disabled / legacy-abandoned components are non-applicable — never false-healthy
        # and never false-outage. Native Hermes LANA health is a separate module card.
        if c.get("enabled") is False:
            continue
        name = c.get("name") or c.get("process") or c.get("pattern") or "?"
        if c.get('bridge_config'):
            from . import bridges
            if not bridges.running_pids(c):
                down.append(name)
            continue
        if c.get('manager'):
            from . import managed
            if managed.active(c['manager']) is not True:
                down.append(name)
            continue
        url = c.get("health_url")
        if c.get("command"):
            # A module is judged by its latest recorded sample — running every module again
            # inside the system check would double the load (and the wait) for no new information.
            cur = _fresh_sample(name, cfg)
            if cur is None or not cur["up"]:
                down.append(name)
            elif cur.get("latency") is not None:
                lats.setdefault("cmd:" + name, cur["latency"])
            continue
        if url:
            ok, lat = _http(url)
            if lat is not None:
                lats.setdefault(url, lat)
            if not ok:
                down.append(name)
        else:
            pat = c.get("process") or c.get("pattern") or ""
            if pat and not _proc_up(pat):
                down.append(name)

    # The supervisor itself. A keepalive that silently stopped is a system outage in waiting:
    # nothing would revive the next dead agent. Judged by its heartbeat (stamped every pass).
    ka = cfg.get("keepalive", {})
    if ka.get("enabled", True):
        from . import keepalive as _keepalive
        try:
            age = time.time() - _keepalive.heartbeat_path().stat().st_mtime
        except OSError:
            age = None
        limit = 3 * int(ka.get("interval_seconds", 60)) + 30
        if age is None or age > limit:
            down.append("keepalive (žádný průchod %s)" % ("délší dobu" if age is None else f"za {int(age)} s"))

    uniq: list[str] = []
    for n in down:
        if n not in uniq:
            uniq.append(n)
    up = not uniq
    avg = round(sum(lats.values()) / len(lats), 3) if lats else None
    detail = ("všechny sledované komponenty v pořádku" if up
              else "výpadek: " + ", ".join(uniq[:5]))
    return up, avg, detail


ROW_SEP = " › "          # sample name of a module row: "<service> › <row>"


def _rows_path() -> str:
    from . import config   # local import: probe is imported by config-free helpers/tests too
    return str(config.state_dir() / "service_rows.json")


def _record_rows(service: str, rows: list[dict]) -> None:
    """Record each module row as its own sample (so it gets uptime/SLA like any service) and
    remember the row order + urls for the dashboard. No rows → nothing written, nothing removed."""
    if not rows:
        return
    for r in rows:
        db.record(service + ROW_SEP + r["name"], r["up"], r["latency"], r["detail"])
    try:
        path = _rows_path()
        try:
            with open(path, encoding="utf-8") as stream:
                data = json.load(stream)
            if not isinstance(data,dict): data={}
        except (OSError, ValueError):
            data = {}
        data[service] = [{"name": r["name"], "url": r["url"]} for r in rows]
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


def service_rows(service: str) -> list[dict]:
    """Row names/urls last reported by a module (empty when it has none)."""
    try:
        with open(_rows_path(), encoding="utf-8") as stream:
            data=json.load(stream)
        return list(data.get(service) or []) if isinstance(data,dict) else []
    except (OSError, ValueError, TypeError):
        return []


def probe_once(cfg: dict) -> None:
    for s in cfg.get("services", []):
        name = s.get("name")
        if not name:
            continue
        if s.get("kind") == "system":
            ok, lat, detail = _system_health(cfg)
            db.record(name, ok, lat, detail)
            continue
        if s.get('kind') == 'bridges':
            from . import bridges
            state = bridges.health(cfg)
            if state is not None:
                db.record(name, state[0], None, state[1])
            continue
        if s.get('bridge_config'):
            from . import bridges
            up = bool(bridges.running_pids(s))
            db.record(name, up, None, 'bridge running' if up else 'bridge process missing')
            continue
        proc = _proc_up(s.get("process", ""))
        ok, lat, detail = proc, None, f"proc={'up' if proc else 'down'}"
        if s.get("health_url"):
            http_ok, lat = _http(s["health_url"])
            ok = proc and http_ok
            detail += f" http={'ok' if http_ok else 'down'}"
        if s.get("command"):
            cmd_ok, cmd_lat, cmd_detail, rows = _run(s["command"], float(s.get("timeout_seconds", 15)))
            _record_rows(name, rows)
            ok = ok and cmd_ok
            lat = lat if lat is not None else cmd_lat
            # The command's own words lead; process/HTTP are mentioned only when they fail.
            extra = [x for x, bad in (("proc=down", s.get("process") and not proc),
                                      ("http=down", s.get("health_url") and lat is None and not ok)) if bad]
            detail = " ".join([cmd_detail] + extra)
        db.record(name, ok, lat, detail)
