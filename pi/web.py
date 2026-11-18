"""Web dashboard and OBS overlay, served by the hub on port 5000.

  http://<PI_IP>:5000/          dashboard (laptop browser)
  http://<PI_IP>:5000/overlay   OBS browser source (1920x1080, transparent)

Both pages poll small JSON endpoints, which keeps everything simple and
works fine on a local network.

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

    @app.get("/overlay")
    def overlay():
        return OVERLAY_HTML

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
  </div>
  <div class="card"><h2>Companion</h2>
    <div class="big" id="mood">-</div>
    <p id="message" style="font-size:18px"></p>
    <p class="muted" id="recap"></p>
  </div>
  <div class="card"><h2>Room</h2>
    <div class="row"><span class="muted">Temperature</span><span id="temp">-</span></div>
    <div class="row"><span class="muted">Light</span><span id="light">-</span></div>
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
    $('mood').textContent = s.companion.mood;
    $('message').textContent = s.companion.message;
    $('recap').textContent = s.companion.last_recap ? 'Last recap: ' + s.companion.last_recap.text : '';
    const r = s.sensors || {};
    $('temp').textContent = (typeof r.temp_c === 'number') ? `${(r.temp_c * 9/5 + 32).toFixed(1)} F` : '-';
    $('light').textContent = (r.light_pct ?? '-') + (r.light_pct != null ? ' %' : '');
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

OVERLAY_HTML = """<!doctype html>
<html><head><meta charset="utf-8"><title>Overlay</title>
<style>
  html, body { margin: 0; width: 1920px; height: 1080px; background: transparent; overflow: hidden;
               font-family: 'Arial Black', Impact, sans-serif; }
  #flash { position: absolute; inset: 0; opacity: 0; pointer-events: none; }
  #banner { position: absolute; top: 180px; width: 100%; text-align: center; font-size: 110px;
            color: #fff; opacity: 0; text-shadow: 0 0 12px rgba(0,0,0,.8), 0 6px 0 rgba(0,0,0,.6);
            letter-spacing: 4px; }
  #bubble { position: absolute; right: 40px; bottom: 40px; max-width: 520px; padding: 18px 24px;
            background: rgba(10,12,16,.85); color: #fff; border-radius: 18px; font-family: system-ui, sans-serif;
            font-size: 26px; font-weight: 600; border: 3px solid #fff; opacity: 0; transition: opacity .4s; }
  #bubble small { display: block; font-size: 16px; opacity: .7; text-transform: uppercase; margin-bottom: 6px; }
  .show { animation: pop 2.2s ease-out forwards; }
  .flash { animation: fl .8s ease-out forwards; }
  @keyframes pop { 0% { opacity: 0; transform: scale(.6); } 12% { opacity: 1; transform: scale(1.08); }
                   20% { transform: scale(1); } 80% { opacity: 1; } 100% { opacity: 0; } }
  @keyframes fl { 0% { opacity: .45; } 100% { opacity: 0; } }
</style></head><body>
<div id="flash"></div><div id="banner"></div>
<div id="bubble"><small id="mood"></small><span id="text"></span></div>
<script>
const FX = {
  elim:       { text: 'ELIMINATION',    color: '#ffffff' },
  knocked:    { text: 'KNOCKED',        color: '#ff3b3b' },
  eliminated: { text: 'ELIMINATED',     color: '#ff3b3b' },
  victory:    { text: 'VICTORY ROYALE', color: '#ffc629' },
  tilt:       { text: 'TILT DETECTED',  color: '#ff8a00' },
};
const banner = document.getElementById('banner'), flash = document.getElementById('flash');
let lastId = null, bubbleTimer = null;
function play(name) {
  const fx = FX[name]; if (!fx) return;
  banner.textContent = fx.text; banner.style.color = fx.color;
  flash.style.background = fx.color;
  for (const el of [banner, flash]) { el.className = ''; void el.offsetWidth; }
  banner.className = 'show'; flash.className = 'flash';
}
function say(mood, text) {
  document.getElementById('mood').textContent = mood;
  document.getElementById('text').textContent = text;
  const b = document.getElementById('bubble'); b.style.opacity = 1;
  clearTimeout(bubbleTimer); bubbleTimer = setTimeout(() => b.style.opacity = 0, 9000);
}
async function poll() {
  try {
    if (lastId === null) { lastId = (await (await fetch('/api/state')).json()).last_id; return; }
    const ev = await (await fetch('/api/events?since=' + lastId)).json();
    for (const e of ev.events) {
      if (e.kind === 'effect') play(e.data.effect);
      if (e.kind === 'companion') say(e.data.mood, e.data.message);
    }
    lastId = ev.last_id;
  } catch (err) {}
}
setInterval(poll, 250);
</script></body></html>
"""
