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
from pathlib import Path
import os
import shutil
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, db, detect, keepalive, probe, plugins


def password_hash(password: str) -> str:
    """Hash a dashboard password for storage (we never keep the plaintext in config)."""
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


def _action_allowed(headers) -> bool:
    """A state-changing request must carry the ``X-Agentsmon`` header the page sets. A form or a
    plain fetch from another site cannot add a custom header without a CORS preflight (which
    this server never answers), so a page you happen to have open elsewhere cannot stop or
    restart your agents behind your back — even with HTTP auth cached (audit 2026-09-05)."""
    try:
        return (headers.get("X-Agentsmon") or "").strip().lower() == "action"
    except Exception:
        return False


def _auth_ok(header: str | None, user: str, pwhash: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
    except Exception:
        return False
    u, _, pw = raw.partition(":")
    return hmac.compare_digest(u, user) and hmac.compare_digest(password_hash(pw), pwhash)


AGENT_TABLE = r"""
  <section class="mb-6" data-svc="KEY" HIDDEN>
    <div class="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 bg-white border-slate-200">
      <span class="svc-dot h-3 w-3 rounded-full bg-slate-300 shrink-0"></span>
      <h2 class="text-base font-semibold">TITLE</h2>
      <span class="agents-count ml-auto text-sm font-medium text-slate-400">loading…</span>
      <span class="svc-chev text-slate-400 text-xs ml-2 select-none" title="collapse / expand">▾</span>
    </div>
    <div class="svc-body">
    <div class="rounded-lg border border-slate-200 bg-white overflow-x-auto">
      <table class="w-full text-sm" style="table-layout:fixed;min-width:680px"><colgroup><col style="width:21%"><col style="width:13%"><col style="width:32%"><col style="width:12%"><col style="width:14%"><col style="width:8%"></colgroup><thead>
        <tr class="text-[11px] uppercase tracking-wide text-slate-400 border-b border-slate-100">
          <th class="text-left font-medium px-2 py-2">Agent</th>
          <th class="text-left font-medium px-2 py-2">Model</th>
          <th class="text-left font-medium px-2 py-2">Session ID / Port</th>
          <th class="text-left font-medium px-2 py-2">Started</th>
          <th class="text-left font-medium px-2 py-2">Status</th>
          <th class="text-right font-medium px-2 py-2"></th>
        </tr></thead>
        <tbody class="agent-rows"><tr><td colspan="6" class="px-3 py-3 text-slate-400">loading…</td></tr></tbody>
      </table>
    </div>
    <p class="agents-note text-[11px] text-slate-400 mt-2">NOTE</p>
    </div>
  </section>"""


PAGE = r"""<!DOCTYPE html><html lang="en"><head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Agents Monitoring</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100'><text y='.9em' font-size='90'>🤖</text></svg>">
<script src="/static/tailwind.js"></script>
<style>.bar{transition:opacity .15s ease}.bar:hover{opacity:.65}.copied-flash{color:#059669!important;transition:color .1s}#toast{transition:opacity .2s ease}.agent-rows td{overflow:hidden}.sid{display:block;overflow:hidden;text-overflow:ellipsis}</style>
</head><body class="bg-slate-50 text-slate-800 antialiased">
<div id="toast" class="fixed left-1/2 bottom-5 -translate-x-1/2 bg-slate-800 text-white text-sm px-2 py-1.5 rounded-md shadow-lg opacity-0 pointer-events-none" style="z-index:50">&nbsp;</div>
<div id="app" class="mx-auto px-5 py-6" style="max-width:850px">
AGENT_TABLES
  <div id="services"></div>
  <p class="text-center text-[11px] text-slate-300" id="footer">auto-refresh</p>
</div>

<template id="svc-tpl">
  <section class="mb-6">
    <div class="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 bg-white border-slate-200">
      <span class="svc-dot h-3 w-3 rounded-full bg-slate-300 shrink-0"></span>
      <h2 class="svc-name text-base font-semibold"></h2>
      <span class="svc-state ml-auto text-sm font-medium text-slate-400">loading…</span>
      <span class="svc-chev text-slate-400 text-xs ml-2 select-none" title="collapse / expand">▾</span>
    </div>
    <div class="svc-body">
    <div class="grid grid-cols-3 gap-3 mb-3">
      <div class="rounded-lg border border-slate-200 bg-white p-3">
        <p class="text-[11px] uppercase tracking-wide text-slate-400">Uptime</p>
        <p class="m-uptime text-lg font-semibold mt-0.5">–</p>
        <p class="text-[11px] text-slate-400">since first check or last outage</p>
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
    <div class="svc-rows-wrap rounded-lg border border-slate-200 bg-white overflow-x-auto mb-3" hidden>
      <table class="w-full text-sm" style="table-layout:fixed;min-width:680px"><colgroup><col style="width:24%"><col style="width:30%"><col style="width:12%"><col style="width:12%"><col style="width:22%"></colgroup><thead>
        <tr class="text-[11px] uppercase tracking-wide text-slate-400 border-b border-slate-200">
          <th class="text-left font-medium px-2 py-2">Check</th>
          <th class="rows-value-label text-left font-medium px-2 py-2">Address</th>
          <th class="text-left font-medium px-2 py-2">Latency</th>
          <th class="text-left font-medium px-2 py-2">Uptime</th>
          <th class="text-left font-medium px-2 py-2">Status</th>
        </tr></thead>
        <tbody class="svc-rows"></tbody>
      </table>
    </div>
    <div class="rounded-lg border border-slate-200 bg-white p-4">
      <div class="flex items-center justify-between mb-2">
        <h3 class="text-xs font-semibold text-slate-500">Availability History</h3>
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
  const tl=q(root,".svc-timeline"); tl.innerHTML=""; const GREEN=98;
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
  q(root,".svc-head").className="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 cursor-pointer "+st[3];
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
  renderRows(root, s.rows||[], s.row_details===true);
}
// Module rows (one website / tunnel / db per line) — same shape as the agent tables.
function renderRows(root, rows, details=false){
  const wrap=q(root,".svc-rows-wrap"); if(!wrap) return;
  wrap.hidden=!rows.length; if(!rows.length) return;
  q(root,".rows-value-label").textContent=details?"Detail":"Address";
  const tb=q(root,".svc-rows");
  tb.innerHTML=rows.map(r=>{
    const st=STATE[r.state]||STATE.nodata;
    const addr=r.url?r.url.replace(/^https?:\/\//,"").replace(/\/$/,""):"";
    const sla=r.sla!=null?` · ${r.sla.toFixed(2)} % / ${r.sla_samples||0} samples`:"";
    return `<tr class="border-b border-slate-100 last:border-0">`+
      `<td class="px-2 py-1.5 font-medium text-slate-700 whitespace-nowrap">${esc(r.name)}</td>`+
      `<td class="px-2 py-1.5"><span class="text-xs text-slate-500 ${details?'break-words':'font-mono whitespace-nowrap'}">${esc(details?(r.detail||''):addr)}</span></td>`+
      `<td class="px-2 py-1.5 text-slate-600 text-xs whitespace-nowrap">${fmtLat(r.latency_ms)}</td>`+
      `<td class="px-2 py-1.5 text-slate-600 text-xs whitespace-nowrap" title="time since the last real outage${esc(sla)}">${fmtDuration(r.uptime_seconds)}</td>`+
      `<td class="px-2 py-1.5" title="${esc(r.detail||"")}${r.last_ts?" · "+esc(fmtTime(r.last_ts)):""}"><span class="inline-flex items-center gap-1.5 whitespace-nowrap text-xs font-medium ${st[2]}"><span class="h-2 w-2 rounded-full ${st[0]} shrink-0"></span>${st[1]}</span></td>`+
    `</tr>`;}).join("");
}
function renderCell(cell){
  if(!cell) return "–";
  const tones={ok:"text-emerald-600",warning:"text-amber-600",error:"text-rose-600",muted:"text-slate-400"};
  const bars=(cell.bars||[]).map(b=>`<div title="${esc(b.title)}" class="mb-1"><span>${esc(b.label)} ${b.percent}%</span><div class="h-1 bg-slate-100 rounded"><div class="h-1 rounded ${b.percent>=90?'bg-rose-500':b.percent>=70?'bg-amber-500':'bg-emerald-500'}" style="width:${b.percent}%"></div></div></div>`).join("");
  return `<span title="${esc(cell.title)}" class="${tones[cell.tone]||''}">${esc(cell.text)}</span>`+bars;
}
function renderColumns(root,columns){
  const table=q(root,"table"), head=q(root,"thead tr");
  head.querySelectorAll(".plugin-column").forEach(n=>n.remove());
  columns.forEach(c=>{const th=document.createElement("th");th.className="plugin-column text-left font-medium px-2 py-2";th.textContent=c.title;head.insertBefore(th,head.lastElementChild);});
  const widths=[170,105,260,97,113,...columns.map(()=>100),65];
  q(root,"colgroup").innerHTML=columns.length?widths.map(w=>`<col style="width:${w}px">`).join(''):'<col style="width:21%"><col style="width:13%"><col style="width:32%"><col style="width:12%"><col style="width:14%"><col style="width:8%">';
  table.style.minWidth=(columns.length?810+columns.length*100:680)+"px";
}
function renderActions(a,table){
  const stopped=a.status==="Stopped (manual)";
  const defs=a.plugin?(a.row_actions||[]):(a.actions===false?[]:[{id:"restart",label:"Restart "+a.name,icon:"restart"},
    stopped?{id:"stop",label:"Remove "+a.name,icon:"stop",confirm:`Remove agent "${a.name}" from Agents Monitoring? Its conversation files stay on disk.`}
           :{id:"stop",label:"Stop "+a.name,icon:"stop"}]);
  const paths={restart:'M21 12a9 9 0 1 1-2.64-6.36M21 3v6h-6',stop:'M18 6 6 18M6 6l12 12',run:'M8 5l11 7-11 7z'};
  const buttons=defs.map(x=>`<button class="agent-act inline-flex align-middle p-1 rounded text-slate-400 ${x.icon==='stop'?'ml-0.5 hover:text-rose-600 hover:bg-rose-50':'hover:text-sky-600 hover:bg-slate-100'}" data-act="${esc(x.id)}" data-name="${esc(a.name)}" ${a.plugin?`data-table="${esc(table)}" data-row="${esc(a.id)}"`:''} data-confirm="${esc(x.confirm||'')}" title="${esc(x.label)}"><svg viewBox="0 0 24 24" class="h-4 w-4" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="${paths[x.icon]||paths.run}"/></svg></button>`).join("");
  return buttons+((a.actions===false||a.actions_locked)?'<span class="text-xs text-slate-300 ml-0.5 align-middle" title="Actions controlled by the provider">🔒</span>':'');
}
function renderAgents(root, agents, summary=null, columns=[]){
  renderColumns(root, columns);
  const tb=q(root,".agent-rows");
  const on=agents.some(a=>a.alive&&!a.needs_login); const running=agents.filter(a=>a.alive&&!a.needs_login).length;
  q(root,".svc-head").className="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 cursor-pointer "+(on?"bg-emerald-50 border-emerald-200":"bg-white border-slate-200");
  q(root,".svc-dot").className="svc-dot h-3 w-3 rounded-full shrink-0 "+(on?"bg-emerald-500":"bg-slate-300");
  const cnt=q(root,".agents-count");
  // Use the same health summary for every provider's table.
  cnt.textContent=`${running}/${agents.length} healthy`;
  cnt.className="agents-count ml-auto text-sm font-medium "+(agents.length&&running===agents.length?"text-emerald-600":running?"text-amber-600":"text-slate-400");
  if(summary){
    const state=summary.up?STATE.operational:(summary.rows.length||summary.error?STATE.outage:STATE.nodata);
    q(root,".svc-head").className="svc-head flex items-center gap-2.5 mb-3 rounded-lg border px-3 py-2 cursor-pointer "+state[3];
    q(root,".svc-dot").className="svc-dot h-3 w-3 rounded-full shrink-0 "+state[0];
    cnt.textContent=`${running}/${agents.length} healthy`;
    cnt.className="agents-count ml-auto text-sm font-medium "+state[2];
    q(root,".agents-note").textContent=summary.error||summary.note||"";
  }
  if(!agents.length){tb.innerHTML=`<tr><td colspan="${6+columns.length}" class="px-3 py-3 text-slate-400">${esc(summary?(summary.error||"No agents found."):"No tmux sessions found.")}</td></tr>`;return;}
  tb.innerHTML="";
  agents.forEach(a=>{
    const tcls=VENDOR[a.vendor]||"bg-slate-100 text-slate-600";
    const copy=a.resume_cmd||a.session_id||"";
    const tip=copy?`${copy} (click to copy)`:"no resume";
    let sid;
    if(a.session_id){
      sid=`<span class="sid font-mono text-xs text-slate-600 whitespace-nowrap cursor-pointer hover:text-sky-600" title="${esc(tip)}" data-copy="${esc(copy)}">${esc(a.session_id)}</span>`;
    }else if(a.kind==="daemon" && a.port){
      // Daemons (gateways) have no session id — show the port they actually listen on instead.
      sid=`<span class="font-mono text-xs text-slate-500 whitespace-nowrap" title="gateway port">:${a.port}</span>`;
    }else{
      sid=`<span class="text-xs text-slate-300 cursor-help" title="${esc(tip)}">— none</span>`;
    }
    const ok=a.alive&&!a.needs_login; const dot=a.needs_login?"bg-amber-500":(ok?"bg-emerald-500":(a.plugin?"bg-rose-500":"bg-slate-300"));
    // Health is the visible status for every agent; latency is supplementary detail.
    const statusTxt=a.status||(ok?"Running":"Idle");
    const statusTip=[a.status_detail||a.status,
      ok&&a.latency_ms!=null?`latency ${fmtLat(a.latency_ms)}`:""].filter(Boolean).join(" · ");
    const stCls=a.needs_login?"text-amber-600":(ok?"text-slate-600":"text-slate-400");
    const tr=document.createElement("tr"); tr.className="border-b border-slate-100 last:border-0";
    const nameBg=NAME_BG[a.name_color];
    const nameInner=nameBg?`<span class="inline-block rounded px-1.5 py-0.5 ${nameBg}">${esc(a.name)}</span>`:esc(a.name);
    // Only tmux agents can be attached to; daemons (kind "daemon", e.g. OpenClaw/Hermes) aren't in
    // tmux. For a tmux agent, clicking its name copies the attach command (toast feedback so the
    // column width never jumps).
    const attachCmd=`tmux attach -t "${a.tmux_session||a.name}"`;
    const nameCell=(a.kind!=="daemon")
      ? `<span class="agent-name cursor-pointer hover:text-sky-600" data-copy="${esc(attachCmd)}" title="${esc(attachCmd)} (click to copy)">${nameInner}</span>`
      : nameInner;
    // Telegram deep-link icon for agents bridged to a bot (opens t.me/<bot> in a new tab).
    const tg=a.telegram_url?` <a href="${esc(a.telegram_url)}" target="_blank" rel="noopener" title="Open @${esc(a.telegram_bot)} in Telegram" class="inline-flex align-middle ml-1 hover:opacity-70"><svg viewBox="0 0 24 24" class="h-4 w-4" fill="#229ED9"><path d="M9.78 18.65l.28-4.23 7.68-6.92c.34-.31-.07-.46-.52-.19L7.74 13.3 3.64 12c-.88-.25-.89-.86.2-1.3l15.97-6.16c.73-.27 1.43.18 1.15 1.3l-2.72 12.81c-.19.91-.74 1.13-1.5.71L12.6 16.3l-1.99 1.93c-.23.23-.42.42-.83.42z"/></svg></a>`:"";
    const recovery=a.restart_warning?`<br><span class="text-[11px] text-amber-600" title="${esc(a.restart_warning)}">⚠ Fresh conversation</span>`:"";
    tr.innerHTML=
      `<td class="px-2 py-1.5 font-medium text-slate-700 whitespace-nowrap">${nameCell}${tg}${recovery}</td>`+
      `<td class="px-2 py-1.5 whitespace-nowrap"><span title="${esc(a.account?'Account: '+a.account:a.label)}" class="inline-block rounded px-1.5 py-0.5 text-[11px] font-medium ${tcls}">${esc(a.label)}</span>${a.metadata_error?'<br><span class="text-[11px] text-amber-600">Incomplete data</span>':""}</td>`+
      `<td class="px-2 py-1.5">${sid}</td>`+
      `<td class="px-2 py-1.5 text-slate-500 text-xs whitespace-nowrap" title="${esc(a.status_detail||'')}">${a.age!=null?"ago "+fmtDuration(a.age):"–"}</td>`+
      `<td class="px-2 py-1.5" title="${esc(statusTip)}"><span class="inline-flex items-center gap-1.5 whitespace-nowrap ${stCls}"><span class="h-2 w-2 rounded-full ${dot} shrink-0"></span><span class="flex flex-col">${esc(statusTxt)}</span></span></td>`+
      columns.map(c=>`<td class="px-2 py-1.5 text-xs">${renderCell((a.cells||{})[c.id])}</td>`).join("")+
      `<td class="px-2 py-1.5 text-right whitespace-nowrap">${renderActions(a, root.dataset.svc)}</td>`;
    tb.appendChild(tr);
  });
}
// Collapsible blocks. The first OPEN_BY_DEFAULT blocks (agents table + the first service cards)
// start expanded, the rest fold to their header line; a click on any header toggles it and the
// choice is remembered per block in localStorage (a long list of modules stays scannable).
const OPEN_BY_DEFAULT=3;
const collapseKey=n=>"agentsmon.collapsed:"+n;
function setCollapsed(root,collapsed,persist){
  const b=q(root,".svc-body"),c=q(root,".svc-chev"); if(!b) return;
  b.hidden=collapsed; if(c) c.textContent=collapsed?"▸":"▾"; root.dataset.collapsed=collapsed?"1":"0";
  if(persist){ try{ localStorage.setItem(collapseKey(root.dataset.cname||""),collapsed?"1":"0"); }catch(e){} }
}
function initCollapse(root,name,index){
  if(root.dataset.cname===name) return;
  root.dataset.cname=name; let v=null; try{ v=localStorage.getItem(collapseKey(name)); }catch(e){}
  setCollapsed(root, v==null ? index>=OPEN_BY_DEFAULT : v==="1", false);
}
document.addEventListener("click",e=>{
  const h=e.target.closest(".svc-head"); if(!h||e.target.closest("a,button")) return;
  const root=h.closest("section"); if(!root) return;
  setCollapsed(root, root.dataset.collapsed!=="1", true);
});
async function refresh(){
  try{
    const d=await (await fetch("/api/state",{credentials:"same-origin"})).json();
    document.getElementById('app').style.maxWidth=(850+(d.columns||[]).length*100)+'px';
    const agentsRoot=document.querySelector('section[data-svc="agents"]');
    renderAgents(agentsRoot, d.agents, null, d.columns||[]); initCollapse(agentsRoot,"agents",0);
    const tables=document.getElementById("plugin-tables");
    const wanted=(d.agent_tables||[]).map(t=>t.id).join("|");
    if(tables.dataset.keys!==wanted){
      tables.innerHTML="";
      (d.agent_tables||[]).forEach(t=>{
        const root=document.getElementById("agent-table-tpl").content.firstElementChild.cloneNode(true);
        root.dataset.svc=t.id; tables.appendChild(root);
      });
      tables.dataset.keys=wanted;
    }
    (d.agent_tables||[]).forEach((t,i)=>{
      const root=tables.children[i]; q(root,"h2").textContent=t.title;
      renderAgents(root,t.rows,t,d.columns||[]); initCollapse(root,t.id,i+1);
    });
    document.getElementById("plugin-loading").hidden=!d.plugin_loading;
    const errors=document.getElementById("plugin-errors"); errors.replaceChildren();
    (d.plugin_errors||[]).forEach(p=>{
      const box=document.createElement("div"); box.className="mb-4 border border-rose-200 bg-rose-50 rounded-lg p-3";
      box.textContent=p.name+": "+p.detail; errors.appendChild(box);
    });
    const box=document.getElementById("services"); const tpl=document.getElementById("svc-tpl");
    if(box.childElementCount!==d.services.length){
      box.innerHTML=""; d.services.forEach(()=>box.appendChild(tpl.content.cloneNode(true)));
    }
    const sections=box.querySelectorAll("section");
    d.services.forEach((s,i)=>{ const root=sections[i]; q(root,".svc-name").textContent=s.name; renderService(root,s); initCollapse(root,s.name,i+1+(d.agent_tables||[]).length); });
    document.getElementById("footer").textContent="updated "+new Date().toLocaleTimeString()+` · core v${d.core.version} (${d.core.state})`;
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
// Restart / Stop buttons → POST /api/agent/action, then refresh. Stop asks for confirmation.
document.addEventListener("click", async e=>{
  const btn=e.target.closest(".agent-act"); if(!btn) return;
  const name=btn.dataset.name, act=btn.dataset.act;
  if(btn.dataset.confirm && !confirm(btn.dataset.confirm)) return;
  if(act==="stop" && !btn.dataset.confirm && !confirm(`Stop agent "${name}"? It won't be auto-restarted until you press ↻.`)) return;
  btn.disabled=true; btn.classList.add("opacity-40");
  showToast((btn.dataset.table?"Running action for ":act==="restart"?"↻ Restarting ":"✕ Stopping ")+name+"…");
  try{
    const r=await fetch("/api/agent/action",{method:"POST",credentials:"same-origin",
      headers:{"Content-Type":"application/json","X-Agentsmon":"action"},body:JSON.stringify({name,action:act,table:btn.dataset.table,row:btn.dataset.row})});
    const j=await r.json().catch(()=>({}));
    showToast((j&&j.ok?"✓ ":"⚠ ")+((j&&j.message)||(r.ok?"done":"failed")));
  }catch(err){ showToast("⚠ "+err); }
  setTimeout(refresh, act==="restart"?2500:600);
});
refresh(); setInterval(refresh, POLL*1000);
</script></body></html>"""

PAGE=PAGE.replace('AGENT_TABLES',
    AGENT_TABLE.replace('KEY','agents').replace('HIDDEN','').replace('TITLE','Persistent Agents')
        .replace('NOTE','tmux sessions running an agent, linked by their <code>--resume</code> session id')+
    '<div id="plugin-tables"></div>'+
    '<div id="plugin-loading" role="status" hidden class="mb-4 border border-slate-200 bg-slate-50 text-slate-600 rounded-lg p-3">'
    'Plugins loading: Awaiting a fresh bounded check</div><div id="plugin-errors"></div>'+
    '<template id="agent-table-tpl">'+AGENT_TABLE.replace('KEY','plugin').replace('HIDDEN','')
        .replace('TITLE','Agents').replace('NOTE','')+'</template>')


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
            "metric_sub": s.get("latency_label", "module" if s.get("command") else "health check"),
            "source": s.get("source", "config"),
            "row_details": s.get("row_details") is True,
            "rows": _service_rows(name, win_days, min_outage),
            "uptime_seconds": db.uptime_seconds(name, min_outage),
            "sla": sla_pct, "sla_window_days": win_days, "sla_samples": samples,
            "timeline": db.timeline(name, tdays * 86400, tdays), "timeline_days": tdays,
        })
    return out


def _service_rows(service: str, win_days: int, min_outage: int) -> list[dict]:
    """Per-row state for a module that reports ``rows`` (one website/tunnel/db per line). Each row
    is its own sample series ``"<service> › <row>"`` with uptime and SLA like a service."""
    out = []
    for r in probe.service_rows(service):
        rname = service + probe.ROW_SEP + r["name"]
        cur = db.last(rname)
        sla_pct, samples = db.sla(rname, win_days * 86400)
        lat = cur["latency"] if (cur and cur["latency"] is not None) else None
        out.append({"name": r["name"], "url": r.get("url") or "",
                    "up": bool(cur and cur["up"]),
                    "state": "operational" if (cur and cur["up"]) else ("outage" if cur else "nodata"),
                    "detail": cur["detail"] if cur else "no data yet", "last_ts": cur["ts"] if cur else None,
                    "latency_ms": round(lat * 1000) if lat is not None else None,
                    "sla": sla_pct, "sla_samples": samples,
                    "uptime_seconds": db.uptime_seconds(rname, min_outage)})
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
    from .identity import restart_warnings
    warnings = restart_warnings(cfg)
    # Sessions listed in `hidden_sessions` are owned by a dedicated module (e.g. the sandbox
    # agents block) and must not also appear in this auto-discovered list, where they get a wrong
    # model tag. Each entry is an exact session name or a `prefix*` glob (reported 2026-09-15).
    hidden = cfg.get("hidden_sessions", []) or []
    def _hidden(name):
        for h in hidden:
            if h.endswith("*") and name.startswith(h[:-1]):
                return True
            if name == h:
                return True
        return False
    tmux = [a for a in detect.configured_agents(cfg)
            if (a["alive"] or a['name'] in overrides) and not _hidden(a["name"])]
    for a in tmux:
        ov = overrides.get(a["name"], {})
        if a['name'] in warnings:
            a['restart_warning'] = warnings[a['name']]
        if ov.get("tag"):
            a["label"] = ov["tag"]
        if ov.get("vendor"):
            a["vendor"] = ov["vendor"]
        if ov.get("restart") and not a.get('identity_conflict') and a.get('session_id'):
            from .identity import explicit_id
            old = explicit_id(ov['restart'], detect.UUID_RE)
            if old == a['session_id']:
                a['resume_cmd'] = ov['restart']
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


def _set_enabled(name: str, enabled: bool) -> None:
    """Persist enabled=true/false on the named agent/daemon in the RAW config file (so keepalive
    stops reviving a stopped agent). Targeted edit — preserves the rest of the user's file."""
    path = config.DEFAULT_PATH
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return
    changed = False
    for key in ("agents", "daemons"):
        for entry in raw.get(key, []):
            if entry.get("name") == name:
                entry["enabled"] = enabled
                changed = True
    if changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
        os.chmod(path, 0o600)


def _remove_agent(name: str) -> bool:
    """Drop a stopped agent's entry from the RAW config file (targeted edit, rest preserved)."""
    path = config.DEFAULT_PATH
    try:
        raw = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return False
    agents = raw.get("agents", [])
    kept = [a for a in agents if a.get("name") != name]
    if len(kept) == len(agents):
        return False
    raw["agents"] = kept
    path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(path, 0o600)
    return True


def _agent_action(name: str, action: str) -> tuple[bool, str]:
    """Dashboard row actions. action: 'restart' | 'stop'. Returns (ok, message)."""
    cfg = config.load()
    tmux_bin = shutil.which(cfg.get("tmux_bin", "tmux")) or "tmux"
    agent = next((a for a in cfg.get("agents", []) if a.get("name") == name), None)
    daemon = next((d for d in cfg.get("daemons", []) if d.get("name") == name), None)
    if not agent and not daemon:
        return False, f"unknown agent '{name}'"
    if daemon and not daemon.get('manager'):
        from . import managed, wizard
        owner = managed.discover(daemon)
        if owner:
            # bootout removes the live PID. Remember its owner before stopping,
            # so a later restart cannot fall back to a standalone command.
            try:
                raw = json.loads(config.DEFAULT_PATH.read_text())
                entry = next(d for d in raw['daemons'] if d['name'] == name)
                entry['manager'] = owner
                if not wizard._write_json(config.DEFAULT_PATH, raw):
                    raise OSError('save failed')
            except (OSError, ValueError, KeyError, StopIteration):
                return False, 'Could not preserve the service owner; action refused.'
            daemon = {**daemon, 'manager':owner}
    if action == "restart":
        if agent:
            from . import identity
            live = detect.discover_agents(config.agent_matches(cfg))
            pins = identity.pin_sessions(cfg, live)
            agent = identity.safe_restart(agent, live, pins)
            if agent is None:
                return False, 'Restart blocked: configure a safe fresh_start command.'
        # Re-enable (in case it was stopped) and relaunch fresh so it picks up new config (e.g. MCP).
        _set_enabled(name, True)
        if agent:
            identity.record_restart(agent)
            if agent.get('restart_warning'):
                keepalive._log(f"agent '{name}': WARNING: {agent['restart_warning']}")
            launch = keepalive._start(agent, tmux_bin, recreate=True)   # kill + recreate + resume cmd
            launch = keepalive._confirm([launch])[0]
            keepalive._log(keepalive._verdict(launch))
            ok = launch['state'] == 'confirmed'
            message = (f"restarted {name} (agent process confirmed)" if ok else
                       f"{name}: restart {'failed' if launch['state'] == 'failed' else 'not confirmed'}"
                       f" – {launch.get('detail', '')}")
            if launch.get('note'):
                message += f" ({launch['note']})"
            warning = agent.get('restart_warning')
            return ok, (warning + ' ' + message) if warning else message
        from . import managed
        return managed.action(daemon, 'restart')
    if action == "stop":
        # A second stop on an already stopped agent removes it from the dashboard/config.
        # Only the monitoring entry goes; conversation files and the CLI stay untouched.
        if agent and not agent.get("enabled", True) and subprocess.run(
                [tmux_bin, "has-session", "-t", name], capture_output=True, timeout=10).returncode != 0:
            return (True, f"removed {name}") if _remove_agent(name) else (False, f"could not remove {name}")
        # Disable first so keepalive won't revive it, then kill the session / process.
        _set_enabled(name, False)
        if agent:
            subprocess.run([tmux_bin, "kill-session", "-t", name], capture_output=True, timeout=10)
            return True, f"stopped {name}"
        from . import managed
        return managed.action(daemon, 'stop')
    return False, f"unknown action '{action}'"


def _state() -> bytes:
    cfg = config.load()
    agents = _agents_state(cfg)
    for a in agents:
        a['id'] = a.get('name', '')
    extra = plugins.collect(cfg, agents)
    hidden = set(extra['hidden_sessions'])
    agents = [a for a in agents if a.get('name') not in hidden]
    for patch in extra['patches']:
        candidates=[(a['id'],a) for a in agents]
        candidates += [(t['id']+':'+a['id'],a) for t in extra['tables'] for a in t['rows']]
        for identity,a in candidates:
            if identity == patch['row_id']:
                a.setdefault('cells', {}).update(patch.get('cells', {}))
                a.update({k:v for k,v in patch.items() if k not in ('row_id','cells')})
    running = sum(1 for a in agents if a.get("alive"))
    services = _service_state(cfg, running)
    services = [s for s in services if s['name'] not in extra['hidden_services']]
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
    from .integrity import status as core_status
    data = {"time": int(time.time()), "agents": agents, "agent_tables": extra['tables'],
            "columns": extra['columns'], "plugin_errors": extra['errors'], "plugins": extra['plugins'],
            "plugin_loading": extra.get('loading', False),
            "core": core_status(), "services": services}
    return json.dumps(data).encode()


def _probe_loop(stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            probe.probe_once(config.load())
        except Exception:
            pass
        stop.wait(int(config.load().get("probe", {}).get("interval_seconds", 60)))


_STATIC_TAILWIND = (Path(__file__).parent / "static" / "tailwind.js").read_bytes()


def _loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1")


def require_auth_or_die(host: str, cfg: dict) -> None:
    """Fail closed: a dashboard reachable from anything but loopback must have HTTP auth
    configured (audit 2026-09-08). Without it anyone on that network could stop or restart
    agents. `agentsmon setup` sets dashboard.auth; loopback-only installs stay optional."""
    auth = cfg.get("dashboard", {}).get("auth") or {}
    if _loopback(host) or (auth.get("user") and auth.get("pwhash")):
        return
    raise SystemExit(
        f"refusing to listen on {host} without dashboard.auth (user + pwhash) in the config — "
        "the page can stop and restart agents. Bind 127.0.0.1 or set the password via `agentsmon setup`."
    )


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
    server = make_server(host, port, cfg)
    print(f"Agents Monitoring dashboard → http://{host}:{port}  (Ctrl-C to stop)")
    if cfg.get("services"):
        threading.Thread(target=_probe_loop, args=(threading.Event(),), daemon=True).start()
    write_pidfile()
    server.serve_forever()


def make_server(host: str, port: int, cfg: dict):
    """Same handler for production and isolated HTTP acceptance tests."""
    require_auth_or_die(host, cfg)
    poll = cfg.get("dashboard", {}).get("poll_seconds", 15)
    page = PAGE.replace("POLL", str(poll)).encode()
    auth = cfg.get("dashboard", {}).get("auth") or {}
    auth_user, auth_hash = auth.get("user"), auth.get("pwhash")

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
            elif self.path == "/static/tailwind.js":
                # Served from the package, not cdn.tailwindcss.com: a third party that can change
                # a script running on this origin could drive the control endpoint (audit 2026-09-08).
                body = _STATIC_TAILWIND
                self.send_response(200)
                self.send_header("Content-Type", "application/javascript; charset=utf-8")
                self.send_header("Cache-Control", "public, max-age=86400")
            else:
                self.send_response(404)
                self.end_headers()
                return
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            if auth_user and auth_hash and not _auth_ok(self.headers.get("Authorization"),
                                                        auth_user, auth_hash):
                return self._denied()
            from urllib.parse import urlparse, parse_qs
            parsed = urlparse(self.path)
            if parsed.path != "/api/agent/action":
                self.send_response(404)
                self.end_headers()
                return
            if not _action_allowed(self.headers):
                body = json.dumps({"ok": False, "error": "missing X-Agentsmon header (cross-site request?)"}).encode()
                self.send_response(403)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            try: length = int(self.headers.get("Content-Length", 0) or 0)
            except ValueError: length = -1
            if not 0 <= length <= 16384:
                self.send_error(413); return
            raw = self.rfile.read(length) if length else b""
            params = {}
            try:
                params = json.loads(raw or b"{}")
            except ValueError:
                params = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
            if not isinstance(params, dict):
                self.send_error(400); return
            qs = parse_qs(parsed.query)
            name = params.get("name") or (qs.get("name", [None])[0])
            act = params.get("action") or (qs.get("action", [None])[0])
            table, row = params.get('table'), params.get('row')
            if table is not None:
                if not all(isinstance(x,str) and 0<len(x)<=240 for x in (table,row,act)):
                    self.send_error(400); return
                ok, msg = plugins.manager(config.load()).action(table,row,act)
                body = json.dumps({'ok':ok,'message':msg}).encode()
                status = 200 if ok else 409
            elif not isinstance(name,str) or not name or act not in ("restart", "stop"):
                body = json.dumps({"ok": False, "error": "need name + action (restart|stop)"}).encode()
                status = 400
            else:
                try:
                    ok, msg = _agent_action(name, act)
                except Exception as exc:                      # never 500 the dashboard on a bad action
                    ok, msg = False, str(exc)
                body = json.dumps({"ok": ok, "message": msg}).encode()
                status = 200 if ok else 500
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return ThreadingHTTPServer((host, port), Handler)


def pidfile_path():
    return config.state_dir() / "dashboard.pid"


def write_pidfile() -> None:
    """Record our PID so the installer can stop exactly this process. It used to
    ``pkill -f "agentsmon dashboard"``, which also killed any shell whose command line merely
    mentioned that string — including the script that was running the install (2026-09-04)."""
    try:
        pidfile_path().write_text(str(os.getpid()), encoding="utf-8")
    except OSError:
        pass
