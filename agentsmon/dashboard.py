"""Live status web page — `agentsmon dashboard`.

Pure standard-library HTTP server (no Flask/FastAPI). Serves one self-contained HTML page that
polls ``/api/status`` and renders agent + daemon health. Binds to 127.0.0.1 by default; set the
host to a VPN/LAN address in config if you want to reach it from another machine.
"""
from __future__ import annotations

import json
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, detect

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Agents Monitoring</title>
<style>
 :root{color-scheme:dark}
 body{margin:0;background:#0b0f17;color:#e5e9f0;font:15px/1.5 system-ui,sans-serif}
 .wrap{max-width:820px;margin:0 auto;padding:28px 18px}
 h1{font-size:20px;margin:0 0 4px} .sub{color:#8b95a7;font-size:13px;margin-bottom:22px}
 h2{font-size:14px;text-transform:uppercase;letter-spacing:.06em;color:#8b95a7;margin:26px 0 10px}
 .card{display:flex;align-items:center;gap:12px;background:#131a26;border:1px solid #1e2737;
   border-radius:10px;padding:11px 14px;margin:7px 0}
 .dot{width:10px;height:10px;border-radius:50%;flex:0 0 auto}
 .up{background:#34d399;box-shadow:0 0 8px #34d39988} .down{background:#f87171}
 .idle{background:#475569} .name{font-weight:600} .meta{color:#8b95a7;font-size:13px;margin-left:auto;text-align:right}
 .tag{font-size:11px;color:#9aa6b8;background:#1c2434;border-radius:5px;padding:1px 7px;margin-left:8px}
 .muted{color:#64748b}
</style></head><body><div class="wrap">
<h1>🤖 Agents Monitoring</h1>
<div class="sub" id="sub">loading…</div>
<h2>Agents (tmux)</h2><div id="agents"></div>
<div id="daemonsWrap" style="display:none"><h2>Daemons</h2><div id="daemons"></div></div>
</div><script>
function age(s){if(s==null)return "?";let d=Math.floor(s/86400),h=Math.floor(s%86400/3600),m=Math.floor(s%3600/60);
 return d?d+"d "+h+"h":h?h+"h "+m+"m":m+"m";}
function card(dot,name,label,meta){return `<div class="card"><span class="dot ${dot}"></span>
 <span class="name">${name}</span>${label?`<span class="tag">${label}</span>`:""}
 <span class="meta">${meta}</span></div>`;}
async function refresh(){
 try{const r=await fetch("/api/status");const d=await r.json();
  document.getElementById("sub").textContent="updated "+new Date(d.time*1000).toLocaleTimeString();
  document.getElementById("agents").innerHTML=d.agents.length?d.agents.map(a=>
    card(a.alive?"up":"idle",a.name,a.alive?a.label:"idle",
     (a.alive?"running":"<span class='muted'>idle shell</span>")+" · age "+age(a.age)+
     (a.session_id?` · <span class="muted">${a.session_id.slice(0,8)}</span>`:""))).join(""):
    "<div class='card muted'>no tmux sessions found</div>";
  const dw=document.getElementById("daemonsWrap");
  if(d.daemons.length){dw.style.display="";document.getElementById("daemons").innerHTML=
    d.daemons.map(x=>card(x.up?"up":"down",x.name,"",x.up?"up":"down")).join("");}
 }catch(e){document.getElementById("sub").textContent="connection lost…";}
}
refresh();setInterval(refresh,POLL*1000);
</script></body></html>"""


def _payload() -> bytes:
    cfg = config.load()
    data = {
        "time": int(time.time()),
        "agents": detect.discover_agents(config.agent_matches(cfg)),
        "daemons": detect.daemon_status(cfg.get("daemons", [])),
    }
    return json.dumps(data).encode()


def serve(host: str, port: int) -> None:
    cfg = config.load()
    poll = cfg.get("dashboard", {}).get("poll_seconds", 15)
    page = PAGE.replace("POLL", str(poll)).encode()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/api/status"):
                body = _payload()
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
