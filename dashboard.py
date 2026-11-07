"""LoopRunner Control Panel — start loops, tick, pause, stop. Zero deps."""
import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

STATE_FILE = Path.home() / ".hermes" / "looprunner" / "loops.json"
PORT = 8800

HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LoopRunner Control Panel</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:system-ui,sans-serif;background:#0f1117;color:#e2e8f0;padding:1.5rem}
h1{font-size:1.5rem;margin-bottom:1rem;color:#7c3aed}
h2{font-size:1.1rem;margin-bottom:.75rem;color:#a78bfa}
.card{background:#1a1d27;border:1px solid #2d3142;border-radius:12px;padding:1.25rem;margin-bottom:1rem}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:.75rem;margin-bottom:.75rem}
.stat{background:#0f1117;border:1px solid #2d3142;border-radius:8px;padding:.75rem;text-align:center}
.stat .val{font-size:1.5rem;font-weight:700}
.stat .lbl{font-size:.75rem;color:#94a3b8;margin-top:.25rem}
.pass{color:#22c55e}.fail{color:#ef4444}
.badge{display:inline-block;padding:.15rem .5rem;border-radius:4px;font-size:.75rem;font-weight:600}
.badge-running{background:#1e3a5f;color:#60a5fa}
.badge-stopped{background:#374151;color:#9ca3af}
.badge-paused{background:#78350f;color:#fbbf24}
.badge-completed{background:#166534;color:#4ade80}
table{width:100%;border-collapse:collapse;font-size:.85rem}
th,td{padding:.5rem .75rem;text-align:left;border-bottom:1px solid #2d3142}
th{color:#94a3b8;font-weight:600}
input,select{background:#0f1117;border:1px solid #2d3142;color:#e2e8f0;padding:.5rem .75rem;border-radius:6px;font-size:.85rem;width:100%}
label{display:block;font-size:.8rem;color:#94a3b8;margin-bottom:.25rem;margin-top:.5rem}
.btn{padding:.5rem 1rem;border:none;border-radius:6px;cursor:pointer;font-size:.85rem;font-weight:600}
.btn-primary{background:#7c3aed;color:white}.btn-primary:hover{background:#6d28d9}
.btn-danger{background:#dc2626;color:white}.btn-danger:hover{background:#b91c1c}
.btn-sm{padding:.25rem .5rem;font-size:.75rem}
.btn-success{background:#16a34a;color:white}.btn-success:hover{background:#15803d}
.btn-warning{background:#d97706;color:white}.btn-warning:hover{background:#b45309}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:.75rem}
.actions{display:flex;gap:.5rem;flex-wrap:wrap;margin-top:.75rem}
.output{background:#0f1117;border:1px solid #2d3142;border-radius:6px;padding:.75rem;font-family:monospace;font-size:.75rem;max-height:120px;overflow-y:auto;white-space:pre-wrap;word-break:break-all;margin-top:.5rem}
.empty{text-align:center;color:#6b7280;padding:2rem}
.refresh{position:fixed;top:1rem;right:1rem;background:#7c3aed;color:white;border:none;padding:.5rem 1rem;border-radius:8px;cursor:pointer;font-size:.85rem;z-index:10}
.tab{display:inline-block;padding:.5rem 1rem;cursor:pointer;border-bottom:2px solid transparent;color:#94a3b8}
.tab.active{border-bottom-color:#7c3aed;color:#e2e8f0}
.tab-content{display:none}.tab-content.active{display:block}
.msg{padding:.75rem;border-radius:6px;margin-bottom:.75rem;font-size:.85rem}
.msg-ok{background:#166534;color:#4ade80}.msg-err{background:#7f1d1d;color:#f87171}
</style>
</head>
<body>
<button class="refresh" onclick="load()">Refresh</button>
<h1>LoopRunner Control Panel</h1>
<div style="margin-bottom:1rem">
  <span class="tab active" onclick="showTab('loops')">Active Loops</span>
  <span class="tab" onclick="showTab('start')">Start New Loop</span>
</div>
<div id="tab-loops" class="tab-content active"><div id="loops-content"></div></div>
<div id="tab-start" class="tab-content">
  <div class="card">
    <h2>Start New Loop — Point to a Project Directory</h2>
    <form id="start-form" onsubmit="startLoop(event)">
      <label>Repository Path (required)</label>
      <input name="repo_path" placeholder="C:/Users/.../my-project" required>
      <label>Verify Command</label>
      <input name="verify_cmd" placeholder="npm test  or  pytest -q  or  cargo test">
      <div class="form-row">
        <div><label>Goal</label><input name="goal" placeholder="fix bugs and improve quality"></div>
        <div><label>Max Rounds</label><input name="max_rounds" type="number" value="10"></div>
      </div>
      <div class="form-row">
        <div><label>Interval (minutes)</label><input name="interval_minutes" type="number" value="60"></div>
        <div><label>Verify Timeout (seconds)</label><input name="verify_timeout" type="number" value="300"></div>
      </div>
      <div class="form-row">
        <div><label>Branch Guard</label><select name="branch_guard"><option value="true">ON</option><option value="false">OFF</option></select></div>
        <div><label>Auto-Recommend</label><select name="auto_recommend"><option value="false">OFF</option><option value="true">ON</option></select></div>
      </div>
      <div style="margin-top:1rem"><button type="submit" class="btn btn-primary">Start Loop</button></div>
    </form>
    <div id="start-msg"></div>
  </div>
</div>
<script>
function showTab(name){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));document.querySelectorAll('.tab-content').forEach(c=>c.classList.remove('active'));event.target.classList.add('active');document.getElementById('tab-'+name).classList.add('active')}
async function load(){
  const res=await fetch('/api/state');const data=await res.json();const loops=data.loops||{};const ids=Object.keys(loops);
  if(ids.length===0){document.getElementById('loops-content').innerHTML='<div class="empty">No loops. Start one in the "Start New Loop" tab.</div>';return}
  let html='';
  for(const id of ids){
    const L=loops[id];const hist=L.history||[];const passes=hist.filter(h=>h.passed===true).length;const fails=hist.filter(h=>h.passed===false).length;
    const badgeClass='badge-'+(L.status||'stopped');
    html+='<div class="card"><h2>'+(L.goal||'Loop')+' <span class="badge '+badgeClass+'">'+L.status+'</span></h2>';
    html+='<div class="grid"><div class="stat"><div class="val">'+L.round+'/'+L.max_rounds+'</div><div class="lbl">Round</div></div><div class="stat"><div class="val pass">'+passes+'</div><div class="lbl">Passes</div></div><div class="stat"><div class="val fail">'+fails+'</div><div class="lbl">Fails</div></div><div class="stat"><div class="val">'+(L.interval_minutes||60)+'m</div><div class="lbl">Interval</div></div></div>';
    html+='<p style="font-size:.85rem;color:#94a3b8;margin-bottom:.25rem">Repo: '+(L.repo_path||'')+'</p>';
    html+='<p style="font-size:.85rem;color:#94a3b8;margin-bottom:.25rem">Verify: <code>'+(L.verify_cmd||'none')+'</code></p>';
    html+='<p style="font-size:.85rem;color:#94a3b8;margin-bottom:.75rem">Branch guard: '+(L.branch_guard?'ON':'OFF')+' | Auto-recommend: '+(L.auto_recommend?'ON':'OFF')+' | Timeout: '+(L.verify_timeout||300)+'s</p>';
    html+='<div class="actions"><button class="btn btn-sm btn-success" onclick="doAction(\''+id+'\',\'tick\')">Tick</button>';
    if(L.status==='running')html+='<button class="btn btn-sm btn-warning" onclick="doAction(\''+id+'\',\'pause\')">Pause</button>';
    else if(L.status==='paused')html+='<button class="btn btn-sm btn-success" onclick="doAction(\''+id+'\',\'resume\')">Resume</button>';
    html+='<button class="btn btn-sm btn-danger" onclick="doAction(\''+id+'\',\'stop\')">Stop</button><button class="btn btn-sm btn-primary" onclick="doAction(\''+id+'\',\'recommend\')">Recommend</button></div>';
    if(hist.length>0){html+='<table style="margin-top:.75rem"><thead><tr><th>Round</th><th>Result</th><th>Time</th></tr></thead><tbody>';const recent=hist.slice(-5).reverse();for(const h of recent){const badge=h.passed?'<span class="badge badge-pass">PASS</span>':'<span class="badge badge-fail">FAIL</span>';html+='<tr><td>'+h.round+'</td><td>'+badge+'</td><td>'+(h.at||'').slice(0,19)+'</td></tr>'}html+='</tbody></table>';const last=hist[hist.length-1];if(last.output)html+='<div class="output">'+last.output.slice(0,300).replace(/</g,'&lt;')+'</div>'}
    html+='</div>';
  }
  document.getElementById('loops-content').innerHTML=html;
}
async function doAction(id,action){const res=await fetch('/api/action',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({loop_id:id,action:action})});const data=await res.json();if(data.error)alert(data.error);else load()}
async function startLoop(e){e.preventDefault();const form=e.target;const data={};for(const el of form.elements){if(el.name){if(el.type==='number')data[el.name]=Number(el.value);else if(el.type==='select-one')data[el.name]=el.value==='true';else data[el.name]=el.value}}const res=await fetch('/api/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});const result=await res.json();const msg=document.getElementById('start-msg');if(result.error)msg.innerHTML='<div class="msg msg-err">'+result.error+'</div>';else{msg.innerHTML='<div class="msg msg-ok">Loop started: '+result.loop_id+'</div>';load();showTab('loops')}}
load();setInterval(load,15000);
</script>
</body>
</html>"""

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed=urlparse(self.path)
        if parsed.path=="/api/state":
            try:data=json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {"loops":{}}
            except Exception:data={"loops":{},"error":"could not read state"}
            body=json.dumps(data).encode("utf-8");self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",len(body));self.end_headers();self.wfile.write(body)
        else:
            body=HTML.encode("utf-8");self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.send_header("Content-Length",len(body));self.end_headers();self.wfile.write(body)
    def do_POST(self):
        parsed=urlparse(self.path);length=int(self.headers.get("Content-Length",0));body=self.rfile.read(length).decode("utf-8")
        try:data=json.loads(body) if body else {}
        except Exception:data={}
        sys.path.insert(0,str(Path(__file__).parent));import tools;tools._STATE_DIR=STATE_FILE.parent;tools._STATE_FILE=STATE_FILE
        if parsed.path=="/api/start":
            result=tools.loop_start(data);out=result.encode("utf-8") if isinstance(result,str) else json.dumps(result).encode("utf-8")
        elif parsed.path=="/api/action":
            loop_id=data.get("loop_id","");action=data.get("action","")
            if action=="tick":result=tools.loop_tick({"loop_id":loop_id})
            elif action=="pause":result=tools.loop_pause({"loop_id":loop_id})
            elif action=="resume":result=tools.loop_resume({"loop_id":loop_id})
            elif action=="stop":result=tools.loop_stop({"loop_id":loop_id})
            elif action=="recommend":result=tools.loop_recommend({"loop_id":loop_id})
            else:result=json.dumps({"error":f"unknown action: {action}"})
            out=result.encode("utf-8") if isinstance(result,str) else json.dumps(result).encode("utf-8")
        else:self.send_response(404);self.end_headers();return
        self.send_response(200);self.send_header("Content-Type","application/json");self.send_header("Content-Length",len(out));self.end_headers();self.wfile.write(out)
    def log_message(self,*args):pass

def main():
    port=PORT
    if "--port" in sys.argv:port=int(sys.argv[sys.argv.index("--port")+1])
    server=HTTPServer(("127.0.0.1",port),Handler)
    print(f"LoopRunner Control Panel running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    try:server.serve_forever()
    except KeyboardInterrupt:print("\\nStopped.");server.server_close()

if __name__=="__main__":main()
