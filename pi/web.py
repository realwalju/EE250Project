"""Web dashboard, served by the hub at http://<PI_IP>:5000/ (open it on the laptop).

The page polls small JSON endpoints once a second, which keeps everything
simple and works fine on a local network.

AI-assisted: initial version generated with Claude (Anthropic), then reviewed
and tested by the team.
"""

from flask import Flask, jsonify, request


def create_app(hub):
    app = Flask(__name__)

    @app.get("/api/state")
    def state():
        return jsonify(hub.web_state())

    @app.get("/api/events")
    def events():
        since = request.args.get("since", default=0, type=int)
        return jsonify(hub.feed_since(since))

    @app.get("/")
    def dashboard():
        return DASHBOARD_HTML

    return app


DASHBOARD_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Desk Companion</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body { margin: 0; font-family: system-ui, sans-serif; background: #111418; color: #e8e8e8; }
  header { padding: 16px 24px; border-bottom: 1px solid #2a2f36; display: flex; justify-content: space-between; }
  h1 { font-size: 20px; margin: 0; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(280px, 1fr)); gap: 16px; padding: 24px; }
  .card { background: #1a1f25; border: 1px solid #2a2f36; border-radius: 12px; padding: 16px; }
  .card h2 { font-size: 13px; text-transform: uppercase; letter-spacing: .08em; color: #8a94a3; margin: 0 0 12px; }
  .big { font-size: 28px; font-weight: 600; }
  .row { display: flex; justify-content: space-between; padding: 4px 0; }
  .muted { color: #8a94a3; }
  #log { max-height: 320px; overflow-y: auto; font-family: ui-monospace, monospace; font-size: 13px; }
  #log div { padding: 3px 0; border-bottom: 1px solid #22272e; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  td, th { text-align: left; padding: 6px 4px; border-bottom: 1px solid #22272e; }
  .win { color: #f5c542; } .loss { color: #e05555; }
  #status.ok { color: #4caf50; } #status.bad { color: #e05555; }
</style></head><body>
<header><h1>Fortnite Desk Companion</h1><span id="status" class="bad">connecting...</span></header>
<div class="grid">
  <div class="card"><h2>Now</h2>
    <div class="big" id="mode">-</div>
    <div class="row"><span class="muted">Match elims</span><span id="elims">-</span></div>
    <div class="row"><span class="muted">Match knocks</span><span id="knocks">-</span></div>
    <div class="row"><span class="muted">Session</span><span id="session">-</span></div>
    <div class="row"><span class="muted">PC app</span><span id="app">-</span></div>
  </div>
  <div class="card"><h2>Companion</h2>
    <div class="big" id="mood">-</div>
    <p id="message" style="font-size:18px"></p>
    <p class="muted" id="recap"></p>
  </div>
  <div class="card"><h2>Room</h2>
    <div class="row"><span class="muted">Temperature</span><span id="temp">-</span></div>
    <div class="row"><span class="muted">Desk vibration (last 5 s)</span><span id="vib">-</span></div>
  </div>
  <div class="card" style="grid-column: span 2"><h2>Event log</h2><div id="log"></div></div>
  <div class="card"><h2>Matches</h2>
    <table><thead><tr><th>#</th><th>Result</th><th>Elims</th><th>Time</th></tr></thead><tbody id="matches"></tbody></table>
  </div>
</div>
<script>
const $ = id => document.getElementById(id);
let lastId = 0;
function fmtTime(ts) { return new Date(ts * 1000).toLocaleTimeString(); }
async function poll() {
  try {
    const s = await (await fetch('/api/state')).json();
    $('status').textContent = 'live'; $('status').className = 'ok';
    $('mode').textContent = s.study_mode ? 'Study mode' : (s.in_match ? 'In match' : 'Lobby');
    $('elims').textContent = s.match ? s.match.elims : '-';
    $('knocks').textContent = s.match ? s.match.knocks : '-';
    const ss = s.companion.session;
    $('session').textContent = `${ss.games} games, ${ss.wins} wins, ${ss.elims} elims`;
    const a = s.activity || {};
    $('app').textContent = a.app ? (a.gaming ? 'Fortnite' : a.app.slice(0, 30)) : '-';
    $('mood').textContent = s.companion.mood;
    $('message').textContent = s.companion.message;
    $('recap').textContent = s.companion.last_recap ? 'Last recap: ' + s.companion.last_recap.text : '';
    const r = s.sensors || {};
    $('temp').textContent = (typeof r.temp_c === 'number') ? `${(r.temp_c * 9/5 + 32).toFixed(1)} F` : '-';
    $('vib').textContent = s.vib_recent;
    $('matches').innerHTML = '';
    for (const m of s.matches) {
      const tr = document.createElement('tr');
      for (const [v, cls] of [[m.match_id], [m.result, m.result], [m.elims], [Math.round(m.duration_s) + ' s']]) {
        const td = document.createElement('td'); td.textContent = v; if (cls) td.className = cls; tr.appendChild(td);
      }
      $('matches').appendChild(tr);
    }
    const ev = await (await fetch('/api/events?since=' + lastId)).json();
    for (const e of ev.events) {
      const div = document.createElement('div');
      div.textContent = `${fmtTime(e.ts)}  ${e.kind.padEnd(9)} ${JSON.stringify(e.data)}`;
      $('log').prepend(div);
    }
    lastId = ev.last_id;
  } catch (err) { $('status').textContent = 'offline'; $('status').className = 'bad'; }
}
poll(); setInterval(poll, 1000);
</script></body></html>
"""
