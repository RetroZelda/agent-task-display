#!/usr/bin/env python3
"""UI test server: the real tasks/server.py (same arguments, same handler) plus test-only hooks.
Only test_ui.py runs it, on a scratch port with a scratch database.

    python3 tests/ui/harness.py --port N --db ... --pidfile ... [server.py options]
    UI_RESULTS_DIR=<dir> receives the results the page scripts post.

Hooks:
  GET  /__slow?ms=N        sleeps N ms, then 404s. An <img> of it holds the page's load event, and
                           with it Firefox's headless screenshot, until the test script has run.
  POST /__result?name=N    stores the posted JSON as $UI_RESULTS_DIR/N.json
  GET  /__log[?clear=1]    JSON list of [method, path] for every request since the last clear
  GET  /__offline?ms=N     for N ms, drops the connection of every request without an X-Test header
                           (the board "goes offline" for the page, while the test script still reaches it)
  GET  /__backdate?rid=R&secs=N   moves request R's and its tasks' updated_at N seconds back (and drops
                           their ETAs), so it goes stale on the next poll
  POST /__settings_file    writes the posted bytes into the board's settings file, as a hand edit would
  GET  /__test/NAME.js     serves tests/ui/NAME.js
  A page URL with ?__test=NAME[&__wait=MS] is served with, injected: a shim at the top of <head>
  that records alert/confirm calls (confirm answers yes) and JS errors, and before </body> the
  test script (after _lib.js, its helpers) plus the slow <img>. More query options for the shim:
    __stub=1               Notification and AudioContext replaced by recorders (window.__rec: notes,
                           closes, tones, nodes, perms); __perm=default|granted|denied (default granted);
                           __suspended=1: the AudioContext starts held (suspended), as without a click,
                           and resume() lets it go only once the script sets __rec.gesture = true
    __local=<json>         this browser's alert settings (localStorage taskstatus.settings.local)
    __insecure=1           window.isSecureContext reads false
"""
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.dont_write_bytecode = True
UI = Path(__file__).resolve().parent
sys.path.insert(0, str(UI.parent.parent / 'tasks'))
import server  # noqa: E402

RESULTS = Path(os.environ.get('UI_RESULTS_DIR') or Path(tempfile.gettempdir()) / 'tasks-ui-results')
# The shim runs before the page's own script, which may rewrite the URL (a task id in the path is
# replaced by its request's), so it keeps the test's query string for _lib.js.
SHIM = r"""<script>
window.__testQuery = location.search; window.__alerts = []; window.__errors = [];
window.alert = function (m) { __alerts.push('alert:' + m); };
window.confirm = function (m) { __alerts.push('confirm:' + m); return true; };
addEventListener('error', function (e) { __errors.push(String(e.message)); });
addEventListener('unhandledrejection', function (e) { __errors.push('rejection: ' + String(e.reason)); });
(function () {
    var q = new URLSearchParams(location.search);
    try { localStorage.clear(); if (q.get('__local')) localStorage.setItem('taskstatus.settings.local', q.get('__local')); } catch (e) {}
    window.__rec = { notes: [], instances: [], closes: [], tones: [], nodes: 0, perms: 0, contexts: 0, gesture: false };
    if (q.get('__insecure')) Object.defineProperty(window, 'isSecureContext', { configurable: true, get: function () { return false; } });
    if (!q.get('__stub')) return;
    var FakeNotification = function (title, opts) {
        opts = opts || {};
        var self = this;
        this.title = title; this.tag = opts.tag;
        __rec.notes.push({ title: title, body: opts.body, tag: opts.tag, requireInteraction: !!opts.requireInteraction, at: Date.now() });
        __rec.instances.push(this);
        this.close = function () { __rec.closes.push({ tag: opts.tag, at: Date.now() }); if (self.onclose) self.onclose(); };
    };
    FakeNotification.permission = q.get('__perm') || 'granted';
    FakeNotification.requestPermission = function (cb) {
        __rec.perms++; FakeNotification.permission = 'granted'; if (cb) cb('granted'); return Promise.resolve('granted');
    };
    window.Notification = FakeNotification;
    var FakeAC = function () {
        this.state = q.get('__suspended') ? 'suspended' : 'running'; this.currentTime = 0; this.destination = {}; __rec.contexts++;
    };
    FakeAC.prototype.resume = function () {
        var self = this;
        if (self.state !== 'running' && (!q.get('__suspended') || __rec.gesture)) {
            self.state = 'running';
            setTimeout(function () { if (self.onstatechange) self.onstatechange(); }, 0);
        }
        return Promise.resolve();
    };
    FakeAC.prototype.createGain = function () {
        return { gain: { value: 1, setValueAtTime: function () {}, linearRampToValueAtTime: function () {},
                         exponentialRampToValueAtTime: function () {} }, connect: function (x) { return x; } };
    };
    FakeAC.prototype.createOscillator = function () {
        __rec.nodes++;
        var o = { type: 'sine', frequency: { value: 0, setValueAtTime: function (v) { o.frequency.value = v; } },
                  connect: function (x) { return x; }, stop: function () {} };
        o.start = function () { __rec.tones.push({ f: o.frequency.value, at: Date.now() }); };
        return o;
    };
    window.AudioContext = FakeAC;
    window.webkitAudioContext = FakeAC;
})();
</script>""".encode()
OFFLINE_UNTIL = [0.0]
LOG = []
LOCK = threading.Lock()


class Handler(server.Handler):
    def _raw(self, code, body, ctype):
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _hooked(self):
        split = urlsplit(self.path)
        q = parse_qs(split.query)
        if split.path == '/__slow':
            time.sleep(int(q.get('ms', ['4000'])[0]) / 1000)
            return self._raw(404, b'', 'text/plain')
        if split.path == '/__result' and self.command == 'POST':
            data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            RESULTS.mkdir(parents=True, exist_ok=True)
            (RESULTS / (q['name'][0] + '.json')).write_bytes(data)
            return self._raw(204, b'', 'text/plain')
        if split.path == '/__log':
            with LOCK:
                body = json.dumps(LOG).encode()
                if q.get('clear'):
                    LOG.clear()
            return self._raw(200, body, 'application/json')
        if split.path == '/__offline':
            OFFLINE_UNTIL[0] = time.time() + int(q.get('ms', ['5000'])[0]) / 1000
            return self._raw(200, b'{}', 'application/json')
        if split.path == '/__backdate':
            rid, secs = q['rid'][0], float(q.get('secs', ['700'])[0])
            store = self.server.store
            with store._lock:
                store._db.execute('UPDATE requests SET updated_at = updated_at - ? WHERE id = ?', (secs, rid))
                store._db.execute('UPDATE tasks SET updated_at = updated_at - ?, eta_at = NULL WHERE request_id = ?', (secs, rid))
            return self._raw(200, b'{}', 'application/json')
        if split.path == '/__settings_file' and self.command == 'POST':
            data = self.rfile.read(int(self.headers.get('Content-Length', 0)))
            self.server.settings.path.write_bytes(data)
            return self._raw(204, b'', 'text/plain')
        if split.path.startswith('/__test/'):
            name = split.path[len('/__test/'):]
            if not re.fullmatch(r'_?[a-z]+\.js', name) or not (UI / name).is_file():
                return self._raw(404, b'', 'text/plain')
            return self._raw(200, (UI / name).read_bytes(), 'text/javascript')
        if time.time() < OFFLINE_UNTIL[0] and not self.headers.get('X-Test'):
            self.close_connection = True
            try:
                self.connection.shutdown(2)
            except OSError:
                pass
            return
        with LOCK:
            LOG.append([self.command, self.path])
        if self.command == 'GET' and '__test' in q and (split.path == '/' or split.path.startswith('/r')):
            name, wait = q['__test'][0], q.get('__wait', ['4000'])[0]
            if not re.fullmatch(r'[a-z]+', name):
                return self._raw(400, b'bad __test', 'text/plain')
            page = (server.TASKS_DIR / 'static' / 'index.html').read_bytes()
            page = page.replace(b'<head>', b'<head>' + SHIM, 1)
            inject = (f'<script src="/__test/_lib.js"></script><script src="/__test/{name}.js"></script>'
                      f'<img src="/__slow?ms={int(wait)}" alt="" style="position:absolute;width:1px;height:1px;opacity:0">')
            return self._raw(200, page.replace(b'</body>', inject.encode() + b'</body>', 1), 'text/html; charset=utf-8')
        return server.Handler._dispatch(self)

    do_GET = do_HEAD = do_POST = do_DELETE = do_PUT = do_PATCH = do_OPTIONS = _hooked


server.Handler = Handler  # server.listen() looks the handler class up at call time

if __name__ == '__main__':
    sys.exit(server.main(sys.argv[1:]))
