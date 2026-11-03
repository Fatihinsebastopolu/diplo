"""Live board: a small local web page that follows a running game.

Start a game with `--live`, then open the printed URL (http://localhost:8765 by
default) in a browser. The page polls the game once a second and shows the
current map (with orders once they are submitted), center counts, the latest
results, and messages. In `play` mode only messages the human sent or received
are shown, so the page reveals nothing the human could not see anyway.

Standard library only; it serves on 127.0.0.1, never on the network.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from diplomacy.engine.renderer import Renderer

from .engine import POWERS, sc_counts
from .viewer import CSS, POWER_COLORS

MAX_MESSAGES = 200


class LiveBoard:
    def __init__(self, port: int = 8765, viewer_power: str | None = None, delay: float = 0.0, title: str = ""):
        self.viewer_power = viewer_power  # None = spectator (sees all messages)
        self.delay = delay
        self.title = title
        self._lock = threading.Lock()
        self._state = {"version": 0, "phase": "", "status": "starting", "centers": {}, "messages": [],
                       "results": [], "title": title, "viewer": viewer_power}
        self._svg = ""
        self.server = ThreadingHTTPServer(("127.0.0.1", port), self._handler())
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://localhost:{self.port}/"

    def start(self) -> "LiveBoard":
        self.thread.start()
        return self

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    # ------------------------------------------------------------------ state updates
    def _update(self, svg: str | None = None, **changes) -> None:
        with self._lock:
            self._state.update(changes)
            self._state["version"] += 1
            if svg is not None:
                self._svg = svg

    def snapshot(self) -> tuple[dict, str]:
        with self._lock:
            return json.loads(json.dumps(self._state)), self._svg

    # ------------------------------------------------------------------ runner hooks
    def phase_start(self, game, phase: str) -> None:
        status = "negotiating, then choosing orders" if game.phase_type == "M" else "choosing orders"
        self._update(svg=Renderer(game).render(incl_orders=False), phase=phase, status=status,
                     centers=sc_counts(game))

    def press(self, msg) -> None:
        if self.viewer_power and self.viewer_power not in (msg.sender, msg.recipient):
            return
        with self._lock:
            msgs = self._state["messages"]
        entry = {"phase": msg.phase, "round": msg.round + 1, "sender": msg.sender,
                 "recipient": msg.recipient, "text": msg.text, "proposed": msg.proposed_orders,
                 "illegal": msg.illegal_proposals}
        self._update(messages=(msgs + [entry])[-MAX_MESSAGES:])

    def orders_set(self, game, phase: str) -> None:
        self._update(svg=Renderer(game).render(incl_orders=True), status="orders revealed; adjudicating")

    def phase_end(self, game, phase: str) -> None:
        res = game.result_history[phase]
        results = []
        for u, rs in sorted(res.items(), key=lambda kv: str(kv[0])):
            words = [str(r) for r in rs if str(r)]
            if words:
                results.append(f"{u}: {', '.join(words)}")
        self._update(results=results, last_phase=phase, centers=sc_counts(game))
        if self.delay:
            time.sleep(self.delay)

    def game_end(self, game, summary: dict) -> None:
        self._update(svg=Renderer(game).render(incl_orders=False), phase=game.get_current_phase(),
                     status="game over", centers=sc_counts(game))

    # ------------------------------------------------------------------ HTTP
    def _handler(self):
        board = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep the terminal clean for the human player
                pass

            def _send(self, body: bytes, ctype: str) -> None:
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                path = self.path.split("?")[0]
                if path == "/":
                    self._send(PAGE.encode(), "text/html; charset=utf-8")
                elif path == "/state":
                    state, _ = board.snapshot()
                    self._send(json.dumps(state).encode(), "application/json")
                elif path == "/board.svg":
                    _, svg = board.snapshot()
                    self._send(svg.encode(), "image/svg+xml")
                else:
                    self.send_error(404)

        return Handler


PAGE = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
    "<meta name='viewport' content='width=device-width, initial-scale=1'>"
    "<title>Diplomacy live board</title><style>" + CSS + """
.status { font-size:14px; } .results { font-size:12px; max-height:160px; overflow:auto; }
#msgs { max-height:60vh; overflow:auto; }
.conn { float:right; font-size:12px; }
</style></head><body>
<header><h1 id="title">Diplomacy live board</h1>
<div class="muted"><b id="phase"></b> &middot; <span class="status" id="status">connecting...</span>
<span class="conn" id="conn"></span></div></header>
<main><section><div class="grid">
<div class="map"><img id="board" alt="Current board"></div>
<div>
<table id="centers"></table>
<h3>Last results</h3><div class="results muted" id="results"></div>
<h3 id="msgtitle">Messages</h3><div id="msgs"></div>
</div></div></section></main>
<script>
const COLORS = """ + json.dumps(POWER_COLORS) + """;
const POWERS = """ + json.dumps(POWERS) + """;
let version = -1;
const esc = s => String(s).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const dot = p => `<span class="dot" style="background:${COLORS[p]||'#999'}"></span>${esc(p[0] + p.slice(1).toLowerCase())}`;
async function poll() {
  try {
    const r = await fetch('/state', {cache: 'no-store'});
    const s = await r.json();
    document.getElementById('conn').textContent = 'live';
    if (s.version !== version) {
      version = s.version;
      if (s.title) document.getElementById('title').textContent = s.title;
      document.getElementById('phase').textContent = s.phase;
      document.getElementById('status').textContent = s.status;
      document.getElementById('board').src = '/board.svg?v=' + s.version;
      document.getElementById('centers').innerHTML = '<tr><th>Power</th><th>Centers</th></tr>' +
        POWERS.map(p => `<tr><td>${dot(p)}</td><td>${s.centers[p] ?? ''}</td></tr>`).join('');
      document.getElementById('results').innerHTML = s.results.length
        ? `<b>${esc(s.last_phase || '')}</b><br>` + s.results.map(esc).join('<br>') : 'none yet';
      document.getElementById('msgtitle').textContent = s.viewer ? `Messages to and from ${s.viewer}` : 'Messages (all powers)';
      const box = document.getElementById('msgs');
      box.innerHTML = s.messages.slice().reverse().map(m =>
        `<div class="msg"><b>${dot(m.sender)} &rarr; ${dot(m.recipient)}</b> <span class="muted">${esc(m.phase)} r${m.round}</span><br>` +
        esc(m.text) + (m.proposed.length ? `<br><code>${esc(m.proposed.join('; '))}</code>` : '') +
        (m.illegal.length ? `<br><span class="warn">illegal proposal: ${esc(m.illegal.join('; '))}</span>` : '') + '</div>').join('');
    }
  } catch (e) { document.getElementById('conn').textContent = 'disconnected'; }
  setTimeout(poll, 1000);
}
poll();
</script></body></html>"""
)
