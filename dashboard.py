"""LoopRunner Desktop Dashboard — zero-dependency web UI.

Run:  python dashboard.py [--port 8765]
Then open http://localhost:8765 in your browser.
"""
import json
import os
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse

STATE_FILE = Path.home() / ".hermes" / "looprunner" / "loops.json"
PORT = 8765

DASHBOARD_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>LoopRunner Dashboard</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, -apple-system, sans-serif; background: #0f1117; color: #e2e8f0; padding: 2rem; }
  h1 { font-size: 1.5rem; margin-bottom: 1.5rem; color: #7c3aed; }
  .card { background: #1a1d27; border: 1px solid #2d3142; border-radius: 12px; padding: 1.25rem; margin-bottom: 1rem; }
  .card h2 { font-size: 1.1rem; margin-bottom: 0.75rem; color: #a78bfa; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 0.75rem; margin-bottom: 0.75rem; }
  .stat { background: #0f1117; border: 1px solid #2d3142; border-radius: 8px; padding: 0.75rem; text-align: center; }
  .stat .val { font-size: 1.5rem; font-weight: 700; }
  .stat .lbl { font-size: 0.75rem; color: #94a3b8; margin-top: 0.25rem; }
  .pass { color: #22c55e; }
  .fail { color: #ef4444; }
  .running { color: #3b82f6; }
  .stopped { color: #6b7280; }
  .completed { color: #22c55e; }
  table { width: 100%; border-collapse: collapse; font-size: 0.85rem; }
  th, td { padding: 0.5rem 0.75rem; text-align: left; border-bottom: 1px solid #2d3142; }
  th { color: #94a3b8; font-weight: 600; }
  .badge { display: inline-block; padding: 0.15rem 0.5rem; border-radius: 4px; font-size: 0.75rem; font-weight: 600; }
  .badge-pass { background: #166534; color: #4ade80; }
  .badge-fail { background: #7f1d1d; color: #f87171; }
  .empty { text-align: center; color: #6b7280; padding: 2rem; }
  .refresh { position: fixed; top: 1rem; right: 1rem; background: #7c3aed; color: white; border: none; padding: 0.5rem 1rem; border-radius: 8px; cursor: pointer; font-size: 0.85rem; }
  .refresh:hover { background: #6d28d9; }
  .history-row { font-family: monospace; font-size: 0.8rem; }
  .output { background: #0f1117; border: 1px solid #2d3142; border-radius: 6px; padding: 0.75rem; font-family: monospace; font-size: 0.75rem; max-height: 200px; overflow-y: auto; white-space: pre-wrap; word-break: break-all; margin-top: 0.5rem; }
</style>
</head>
<body>
<button class="refresh" onclick="load()">Refresh</button>
<h1>LoopRunner Dashboard</h1>
<div id="content"></div>
<script>
async function load() {
  const res = await fetch('/api/state');
  const data = await res.json();
  const loops = data.loops || {};
  const ids = Object.keys(loops);
  if (ids.length === 0) {
    document.getElementById('content').innerHTML = '<div class="empty">No loops found. Start one with loop_start().</div>';
    return;
  }
  let html = '';
  for (const id of ids) {
    const L = loops[id];
    const hist = L.history || [];
    const passes = hist.filter(h => h.passed === true).length;
    const fails = hist.filter(h => h.passed === false).length;
    const statusClass = L.status || 'stopped';
    html += '<div class="card">';
    html += '<h2>' + (L.goal || 'Loop') + ' <span class="' + statusClass + '">(' + L.status + ')</span></h2>';
    html += '<div class="grid">';
    html += '<div class="stat"><div class="val">' + L.round + '/' + L.max_rounds + '</div><div class="lbl">Round</div></div>';
    html += '<div class="stat"><div class="val pass">' + passes + '</div><div class="lbl">Passes</div></div>';
    html += '<div class="stat"><div class="val fail">' + fails + '</div><div class="lbl">Fails</div></div>';
    html += '<div class="stat"><div class="val">' + (L.interval_minutes || 60) + 'm</div><div class="lbl">Interval</div></div>';
    html += '</div>';
    html += '<p style="font-size:0.85rem;color:#94a3b8;margin-bottom:0.5rem;">Repo: ' + (L.repo_path || '') + '</p>';
    html += '<p style="font-size:0.85rem;color:#94a3b8;margin-bottom:0.5rem;">Verify: <code>' + (L.verify_cmd || '') + '</code></p>';
    html += '<p style="font-size:0.85rem;color:#94a3b8;margin-bottom:0.75rem;">Branch guard: ' + (L.branch_guard ? 'ON' : 'OFF') + ' | Auto-recommend: ' + (L.auto_recommend ? 'ON' : 'OFF') + ' | Timeout: ' + (L.verify_timeout || 300) + 's</p>';
    if (hist.length > 0) {
      html += '<table><thead><tr><th>Round</th><th>Result</th><th>Time</th></tr></thead><tbody>';
      const recent = hist.slice(-10).reverse();
      for (const h of recent) {
        const badge = h.passed ? '<span class="badge badge-pass">PASS</span>' : '<span class="badge badge-fail">FAIL</span>';
        html += '<tr class="history-row"><td>' + h.round + '</td><td>' + badge + '</td><td>' + (h.at || '').slice(0,19) + '</td></tr>';
      }
      html += '</tbody></table>';
      const last = hist[hist.length-1];
      if (last.output) {
        html += '<div class="output">' + last.output.slice(0, 500).replace(/</g,'&lt;') + '</div>';
      }
    }
    html += '</div>';
  }
  document.getElementById('content').innerHTML = html;
}
load();
setInterval(load, 10000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            try:
                data = json.loads(STATE_FILE.read_text(encoding="utf-8")) if STATE_FILE.exists() else {"loops": {}}
            except Exception:
                data = {"loops": {}, "error": "could not read state"}
            body = json.dumps(data).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        else:
            body = DASHBOARD_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress request logs


def main():
    port = PORT
    if "--port" in sys.argv:
        idx = sys.argv.index("--port")
        port = int(sys.argv[idx + 1])
    server = HTTPServer(("127.0.0.1", port), Handler)
    print(f"LoopRunner Dashboard running at http://localhost:{port}")
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
        server.server_close()


if __name__ == "__main__":
    main()
