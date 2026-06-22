"""Live status web page — `agentsmon dashboard`.

Pure standard-library HTTP server (no Flask/FastAPI). Serves one self-contained page that polls
``/api/state`` and renders the same layout as a clean status page: a **Persistent agents** table
and one availability card per **service** (status dot, Uptime / Availability-SLA / Latency
metrics, and a day-by-day availability timeline). A background thread probes services on an
interval and appends to the uptime DB, so just leaving the dashboard running builds history.
Binds 127.0.0.1 by default; optional HTTP Basic auth (config.dashboard.auth). UI text is English.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db, detect, probe


def password_hash(password: str) -> str:
    """Hash a dashboard password for storage (we never keep the plaintext in config)."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _auth_ok(header: str | None, user: str, pwhash: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:
        return False
    u, _, pw = raw.partition(":")
    return hmac.compare_digest(u, user) and hmac.compare_digest(password_hash(pw), pwhash)


PAGE = r"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agents Monitoring</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🤖</text></svg>">
<script src="https://cdn.tailwindcss.com"></script>
<style>.bar{transition:opacity .15s ease}.bar:hover{opacity:.65}.copied-flash{color:#059669!important;transition:color .1s}#toast{transition:opacity .2s ease}</style>
</head><body class="bg-slate-50 text-slate-800 antialiased">
<div id="toast" class="fixed left-1/2 bottom-5 -translate-x-1/2 bg-slate-800 text-white text-sm px-3 py-1.5 rounded-md shadow-lg opacity-0 pointer-events-none" style="z-index:50">&nbsp;</div>
<div class="max-w-3xl mx-auto px-5 py-6">

  <section class="mb-6" data-svc="agents">
    <div class="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 bg-white border-slate-200">
      <span class="svc-dot h-3 w-3 rounded-full bg-slate-300 shrink-0"></span>
      <h2 class="text-base font-semibold">Persistent Agents</h2>
      <span class="agents-count ml-auto text-sm font-medium text-slate-400">loading…</span>
    </div>
    <div class="rounded-lg border border-slate-200 bg-white overflow-x-auto">
      <table class="w-full text-sm"><thead>
        <tr class="text-[11px] uppercase tracking-wide text-slate-400 border-b border-slate-100">
          <th class="text-left font-medium px-3 py-2">Agent</th>
          <th class="text-left font-medium px-3 py-2">Model</th>
          <th class="text-left font-medium px-3 py-2">Session ID</th>
          <th class="text-left font-medium px-3 py-2">Started</th>
          <th class="text-left font-medium px-3 py-2">Status</th>
        </tr></thead>
        <tbody id="agents-rows"><tr><td colspan="5" class="px-3 py-3 text-slate-400">loading…</td></tr></tbody>
      </table>
    </div>
    <p class="text-[11px] text-slate-400 mt-2">tmux sessions running an agent, linked by their <code>--resume</code> session id</p>
  </section>

  <div id="services"></div>
  <p class="text-center text-[11px] text-slate-300" id="footer">auto-refresh</p>
</div>

<template id="svc-tpl">
  <section class="mb-6">
    <div class="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 bg-white border-slate-200">
      <span class="svc-dot h-3 w-3 rounded-full bg-slate-300 shrink-0"></span>
      <h2 class="svc-name text-base font-semibold"></h2>
      <span class="svc-state ml-auto text-sm font-medium text-slate-400">loading…</span>
    </div>
    <div class="grid grid-cols-3 gap-3 mb-3">
      <div class="rounded-lg border border-slate-200 bg-white p-3">
        <p class="text-[11px] uppercase tracking-wide text-slate-400">Uptime</p>
        <p class="m-uptime text-lg font-semibold mt-0.5">–</p>
        <p class="text-[11px] text-slate-400">current streak</p>
      </div>
      <div class="rounded-lg border border-slate-200 bg-white p-3">
        <p class="text-[11px] uppercase tracking-wide text-slate-400">Availability</p>
        <p class="m-sla text-lg font-semibold mt-0.5">–</p>
        <p class="m-sla-sub text-[11px] text-slate-400">SLA</p>
      </div>
      <div class="rounded-lg border border-slate-200 bg-white p-3">
        <p class="m-x-label text-[11px] uppercase tracking-wide text-slate-400">Latency</p>
        <p class="m-x text-lg font-semibold mt-0.5">–</p>
        <p class="m-x-sub text-[11px] text-slate-400">health check</p>
      </div>
    </div>
    <div class="rounded-lg border border-slate-200 bg-white p-4">
      <div class="flex items-center justify-between mb-2">
        <h3 class="text-xs font-semibold text-slate-500">Availability history</h3>
        <span class="svc-tl-window text-[11px] text-slate-400"></span>
      </div>
      <div class="svc-timeline flex items-end gap-[2px] h-8"></div>
      <div class="flex items-center justify-between mt-2 text-[11px] text-slate-400">
        <span class="svc-tl-start"></span>
        <div class="flex items-center gap-3">
          <span class="flex items-center gap-1"><span class="h-2 w-2 rounded-sm bg-emerald-500"></span>Operational</span>
          <span class="flex items-center gap-1"><span class="h-2 w-2 rounded-sm bg-rose-500"></span>Outage</span>
          <span class="flex items-center gap-1"><span class="h-2 w-2 rounded-sm bg-slate-200"></span>No data</span>
        </div>
        <span>now</span>
      </div>
    </div>
  </section>
</template>

<script>
const STATE = {
  operational: ["bg-emerald-500", "Operational", "text-emerald-600", "bg-emerald-50 border-emerald-200"],
  outage:      ["bg-rose-500", "Outage", "text-rose-600", "bg-rose-50 border-rose-200"],
  nodata:      ["bg-slate-300", "No data / not running", "text-slate-400", "bg-white border-slate-200"],
};
const VENDOR = { "anthropic":"bg-orange-100 text-orange-700", "openai":"bg-emerald-100 text-emerald-700",
  "google":"bg-violet-100 text-violet-700", "gold":"bg-amber-100 text-amber-700",
  "red":"bg-rose-100 text-rose-700", "other":"bg-slate-100 text-slate-600" };
// Optional background highlight behind the agent NAME (like the model tags), text stays normal.
const NAME_BG = { "red":"bg-rose-100", "gold":"bg-amber-100", "green":"bg-emerald-100", "blue":"bg-sky-100" };
function fmtDuration(s){if(s==null)return "–";const d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
  return d?`${d}d ${h}h`:h?`${h}h ${m}m`:`${m}m`;}
function fmtTime(u){return u?new Date(u*1000).toLocaleString():"";}
function fmtLat(ms){return ms==null?"–":(ms<1?"<1 ms":ms+" ms");}
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
const q=(r,s)=>r.querySelector(s);

function renderTimeline(root, buckets, windowDays){
  const tl=q(root,".svc-timeline"); tl.innerHTML=""; const GREEN=99;
  buckets.forEach(b=>{
    const el=document.createElement("div"); let cls,status;
    if(b.uptime_pct==null){cls="bg-slate-200";status="no data";}
    else if(b.uptime_pct>=GREEN){cls="bg-emerald-500";status=`${b.uptime_pct}% uptime`;}
    else{cls="bg-rose-500";status=`outage (${b.uptime_pct}% uptime)`;}
    el.className="bar flex-1 rounded-sm h-full "+cls;
    el.title=`${new Date(b.start*1000).toLocaleDateString()} · ${status}`;
    tl.appendChild(el);
  });
  q(root,".svc-tl-window").textContent="last "+windowDays+" days";
  if(buckets.length) q(root,".svc-tl-start").textContent=new Date(buckets[0].start*1000).toLocaleDateString();
}
function renderService(root, s){
  const st=STATE[s.state]||STATE.nodata;
  q(root,".svc-head").className="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 "+st[3];
  q(root,".svc-dot").className="svc-dot h-3 w-3 rounded-full shrink-0 "+st[0];
  const se=q(root,".svc-state"); se.textContent=st[1]; se.className="svc-state ml-auto text-sm font-medium "+st[2];
  q(root,".m-uptime").textContent=fmtDuration(s.uptime_seconds);
  q(root,".m-sla").textContent=s.sla!=null?s.sla.toFixed(2)+" %":"–";
  q(root,".m-sla-sub").textContent="over "+s.sla_window_days+" days ("+(s.sla_samples||0)+" samples)";
  if(s.metric==="agents"){
    q(root,".m-x-label").textContent="Agents";
    q(root,".m-x").textContent=(s.metric_value!=null?s.metric_value:"–")+(s.metric_value!=null?" running":"");
    q(root,".m-x-sub").textContent="live in tmux";
  }else if(s.metric==="system_latency"){
    q(root,".m-x-label").textContent="Avg latency";
    q(root,".m-x").textContent=fmtLat(s.system_latency_ms);
    q(root,".m-x-sub").textContent=(s.system_latency_n||0)+" source"+(s.system_latency_n===1?"":"s");
  }else if(s.metric==="avg_latency"){
    q(root,".m-x-label").textContent="Avg latency";
    q(root,".m-x").textContent=fmtLat(s.avg_latency_ms);
    q(root,".m-x-sub").textContent="over "+s.sla_window_days+" days";
  }else{
    q(root,".m-x-label").textContent="Latency";
    q(root,".m-x").textContent=fmtLat(s.latency_ms);
    q(root,".m-x-sub").textContent=s.metric_sub||"health check";
  }
  renderTimeline(root, s.timeline, s.timeline_days);
}
function renderAgents(root, agents){
  const tb=document.getElementById("agents-rows");
  const on=agents.some(a=>a.alive); const running=agents.filter(a=>a.alive).length;
  q(root,".svc-head").className="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 "+(on?"bg-emerald-50 border-emerald-200":"bg-white border-slate-200");
  q(root,".svc-dot").className="svc-dot h-3 w-3 rounded-full shrink-0 "+(on?"bg-emerald-500":"bg-slate-300");
  const cnt=q(root,".agents-count");
  cnt.textContent=running?`${running} agent${running===1?"":"s"} running`:"no agents running";
  cnt.className="agents-count ml-auto text-sm font-medium "+(running?"text-emerald-600":"text-slate-400");
  if(!agents.length){tb.innerHTML=`<tr><td colspan="5" class="px-3 py-3 text-slate-400">No tmux sessions found.</td></tr>`;return;}
  tb.innerHTML="";
  agents.forEach(a=>{
    const tcls=VENDOR[a.vendor]||"bg-slate-100 text-slate-600";
    const copy=a.resume_cmd||a.session_id||"";
    const tip=copy?`${copy} (click to copy)`:"no resume";
    const sid=a.session_id?`<span class="sid font-mono text-xs text-slate-600 whitespace-nowrap cursor-pointer hover:text-sky-600" title="${esc(tip)}" data-copy="${esc(copy)}">${esc(a.session_id)}</span>`
      :`<span class="text-xs text-slate-300 cursor-help" title="${esc(tip)}">— none</span>`;
    const ok=a.alive; const dot=ok?"bg-emerald-500":"bg-slate-300";
    // A daemon with a health endpoint shows its latency in place of Running/Idle.
    const statusTxt=(a.latency_ms!=null&&ok)?fmtLat(a.latency_ms):(ok?"Running":"Idle");
    const stCls=ok?"text-slate-600":"text-slate-400";
    const tr=document.createElement("tr"); tr.className="border-b border-slate-100 last:border-0";
    const nameBg=NAME_BG[a.name_color];
    const nameInner=nameBg?`<span class="inline-block rounded px-1.5 py-0.5 ${nameBg}">${esc(a.name)}</span>`:esc(a.name);
    // Only tmux agents can be attached to; daemons (kind "daemon", e.g. OpenClaw/Hermes) aren't in
    // tmux. For a tmux agent, clicking its name copies the attach command (toast feedback so the
    // column width never jumps).
    const attachCmd=`tmux attach -t "${a.name}"`;
    const nameCell=(a.kind!=="daemon")
      ? `<span class="agent-name cursor-pointer hover:text-sky-600" data-copy="${esc(attachCmd)}" title="${esc(attachCmd)} (click to copy)">${nameInner}</span>`
      : nameInner;
    // Telegram deep-link icon for agents bridged to a bot (opens t.me/<bot> in a new tab).
    const tg=a.telegram_url?` <a href="${esc(a.telegram_url)}" target="_blank" rel="noopener" title="Open @${esc(a.telegram_bot)} in Telegram" class="inline-flex align-middle ml-1 hover:opacity-70"><svg viewBox="0 0 24 24" class="h-4 w-4" fill="#229ED9"><path d="M9.78 18.65l.28-4.23 7.68-6.92c.34-.31-.07-.46-.52-.19L7.74 13.3 3.64 12c-.88-.25-.89-.86.2-1.3l15.97-6.16c.73-.27 1.43.18 1.15 1.3l-2.72 12.81c-.19.91-.74 1.13-1.5.71L12.6 16.3l-1.99 1.93c-.23.23-.42.42-.83.42z"/></svg></a>`:"";
    tr.innerHTML=
      `<td class="px-3 py-1.5 font-medium text-slate-700 whitespace-nowrap">${nameCell}${tg}</td>`+
      `<td class="px-3 py-1.5 whitespace-nowrap"><span class="inline-block rounded px-1.5 py-0.5 text-[11px] font-medium ${tcls}">${esc(a.label)}</span></td>`+
      `<td class="px-3 py-1.5">${sid}</td>`+
      `<td class="px-3 py-1.5 text-slate-500 text-xs whitespace-nowrap">${a.age!=null?"ago "+fmtDuration(a.age):"–"}</td>`+
      `<td class="px-3 py-1.5"><span class="inline-flex items-center gap-1.5 whitespace-nowrap ${stCls}"><span class="h-2 w-2 rounded-full ${dot} shrink-0"></span>${statusTxt}</span></td>`;
    tb.appendChild(tr);
  });
}
async function refresh(){
  try{
    const d=await (await fetch("/api/state",{credentials:"same-origin"})).json();
    renderAgents(document.querySelector('section[data-svc="agents"]'), d.agents);
    const box=document.getElementById("services"); const tpl=document.getElementById("svc-tpl");
    if(box.childElementCount!==d.services.length){
      box.innerHTML=""; d.services.forEach(()=>box.appendChild(tpl.content.cloneNode(true)));
    }
    const sections=box.querySelectorAll("section");
    d.services.forEach((s,i)=>{ const root=sections[i]; q(root,".svc-name").textContent=s.name; renderService(root,s); });
    document.getElementById("footer").textContent="updated "+new Date().toLocaleTimeString()+" · auto-refresh";
  }catch(e){document.getElementById("footer").textContent="connection lost…";}
}
function copyText(text){
  if(navigator.clipboard && window.isSecureContext){ return navigator.clipboard.writeText(text); }
  // Fallback for plain http (e.g. over a VPN IP): clipboard API needs a secure context.
  const ta=document.createElement("textarea"); ta.value=text; ta.style.position="fixed"; ta.style.opacity="0";
  document.body.appendChild(ta); ta.focus(); ta.select();
  try{ document.execCommand("copy"); }catch(e){} document.body.removeChild(ta);
  return Promise.resolve();
}
let _toastTimer;
function showToast(msg){
  const t=document.getElementById("toast"); if(!t) return;
  t.textContent=msg; t.style.opacity="1";
  clearTimeout(_toastTimer); _toastTimer=setTimeout(()=>{ t.style.opacity="0"; }, 1300);
}
document.addEventListener("click", e=>{
  const el=e.target.closest("[data-copy]"); if(!el) return;
  copyText(el.dataset.copy).then(()=>{
    // Flash the element green + toast — no textContent swap, so column width never jumps.
    el.classList.add("copied-flash");
    setTimeout(()=>el.classList.remove("copied-flash"), 700);
    showToast("✓ Copied to clipboard");
  });
});
refresh(); setInterval(refresh, POLL*1000);
</script></body></html>"""


def _service_state(cfg: dict, running_agents: int = 0) -> list[dict]:
    win_days = int(cfg.get("probe", {}).get("sla_window_days", 90))
    tdays = int(cfg.get("probe", {}).get("timeline_days", 90))
    min_outage = int(cfg.get("probe", {}).get("min_outage_samples", 3))
    out = []
    for s in cfg.get("services", []):
        name = s.get("name")
        if not name:
            continue
        cur = db.last(name)
        sla_pct, samples = db.sla(name, win_days * 86400)
        lat = cur["latency"] if (cur and cur["latency"] is not None) else None
        metric = s.get("metric", "latency")   # "latency" | "agents" (show running-agent count)
        out.append({
            "name": name,
            "up": bool(cur and cur["up"]),
            "state": "operational" if (cur and cur["up"]) else ("outage" if cur else "nodata"),
            "detail": cur["detail"] if cur else "no data yet",
            "last_ts": cur["ts"] if cur else None,
            "latency_ms": round(lat * 1000) if lat is not None else None,
            "avg_latency_ms": (lambda a: round(a * 1000) if a is not None else None)(db.avg_latency(name, win_days * 86400)),
            "health_url": s.get("health_url"),
            "metric": metric, "metric_value": running_agents if metric == "agents" else None,
            "metric_sub": s.get("latency_label", "health check"),
            "uptime_seconds": db.uptime_seconds(name, min_outage),
            "sla": sla_pct, "sla_window_days": win_days, "sla_samples": samples,
            "timeline": db.timeline(name, tdays * 86400, tdays), "timeline_days": tdays,
        })
    return out


def _tg_link(value: str) -> tuple[str, str]:
    """Normalise a Telegram reference (``@bot``, ``bot``, or a full ``https://t.me/bot`` URL) into
    (url, username)."""
    v = (value or "").strip()
    if v.startswith("http"):
        return v, v.rstrip("/").split("/")[-1].lstrip("@")
    uname = v.lstrip("@")
    return f"https://t.me/{uname}", uname


def _agents_state(cfg: dict) -> list[dict]:
    """Pinned daemons (OpenClaw/Hermes…) first, then running tmux agents, with config display
    overrides (tag → label, vendor → tag colour) applied."""
    overrides = {a["name"]: a for a in cfg.get("agents", []) if a.get("name")}
    tmux = [a for a in detect.discover_agents(config.agent_matches(cfg)) if a["alive"]]
    for a in tmux:
        ov = overrides.get(a["name"], {})
        if ov.get("tag"):
            a["label"] = ov["tag"]
        if ov.get("vendor"):
            a["vendor"] = ov["vendor"]
        if ov.get("restart"):
            a["resume_cmd"] = ov["restart"]
    agents = detect.pinned_agents(cfg.get("pinned_daemons", [])) + tmux
    # Telegram deep-link icon. Two sources, explicit wins:
    #  1) an explicit `telegram` field on an agent/daemon config entry (for daemons with their OWN
    #     native bot, e.g. OpenClaw/Hermes) — value is a @username or a full t.me URL;
    #  2) auto: a tmux session bridged via Agent2Telegram (reads only the non-secret bot username).
    explicit = {d["name"]: d["telegram"] for d in
                (cfg.get("pinned_daemons", []) + cfg.get("agents", []))
                if d.get("name") and d.get("telegram")}
    links = detect.telegram_links()
    for a in agents:
        bot = explicit.get(a.get("name")) or links.get(a.get("name"))
        if bot:
            url, uname = _tg_link(bot)
            a["telegram_bot"] = uname
            a["telegram_url"] = url
    return agents


def _state() -> bytes:
    cfg = config.load()
    agents = _agents_state(cfg)
    running = sum(1 for a in agents if a.get("alive"))
    services = _service_state(cfg, running)
    # System-wide current latency = average across every component with a health endpoint
    # (pinned daemons + services), deduped by URL — feeds a "system_latency" metric card.
    lat_by_url: dict[str, int] = {}
    for item in agents + services:
        url, lm = item.get("health_url"), item.get("latency_ms")
        if url and lm is not None:
            lat_by_url.setdefault(url, lm)
    sysavg = round(sum(lat_by_url.values()) / len(lat_by_url)) if lat_by_url else None
    for s in services:
        s["system_latency_ms"] = sysavg
        s["system_latency_n"] = len(lat_by_url)
    data = {"time": int(time.time()), "agents": agents, "services": services}
    return json.dumps(data).encode()


def _probe_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            probe.probe_once(config.load())
        except Exception:
            pass
        stop.wait(int(config.load().get("probe", {}).get("interval_seconds", 60)))


def serve(host: str, port: int) -> None:
    # Bring an older config up to the current schema on startup (e.g. fold per-daemon availability
    # cards into the synthetic Multi-Agent System card). The dashboard restarts on every update,
    # so this self-heals existing installs without needing a special migration step.
    try:
        from . import wizard
        cfg0 = config.load()
        if wizard.migrate_config(cfg0):
            config.save(cfg0)
    except Exception:
        pass
    cfg = config.load()
    poll = cfg.get("dashboard", {}).get("poll_seconds", 15)
    page = PAGE.replace("POLL", str(poll)).encode()
    auth = cfg.get("dashboard", {}).get("auth") or {}
    auth_user, auth_hash = auth.get("user"), auth.get("pwhash")

    if cfg.get("services"):
        threading.Thread(target=_probe_loop, args=(threading.Event(),), daemon=True).start()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _denied(self):
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="Agents Monitoring"')
            self.end_headers()

        def do_GET(self):
            if auth_user and auth_hash and not _auth_ok(self.headers.get("Authorization"),
                                                        auth_user, auth_hash):
                return self._denied()
            if self.path.startswith("/api/state"):
                body = _state()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
            elif self.path == "/" or self.path.startswith("/index"):
                body = page
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    print(f"Agents Monitoring dashboard → http://{host}:{port}  (Ctrl-C to stop)")
    ThreadingHTTPServer((host, port), Handler).serve_forever()
