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
    __keep=1               the shim leaves localStorage alone (it clears it at every load otherwise, so a
                           second board in the same browser would wipe the first one's settings)
    __skew=S               this browser's clock runs S seconds ahead of the board's (Date.now)
    __tune=NAME:N,...      sets page constants (const STREAM_PARK_MS = 60000; becomes 3000), so a timer
                           measured in minutes can be tested in seconds (the page's STREAM_ numbers only)
  A page URL with ?__shim=1 gets the shim (and the options above) but no test script and no slow image: a
  board that behaves as it would for a user, its load event not held. It is what the stream cases load in
  the frames of /__frame (below), where the slow image belongs to the frame's parent instead.

The live stream (tests/ui/stream*.js, tests/lib/fake_ffmpeg.py as the board's ffmpeg):
  GET  /__frame?__test=NAME[&__wait=MS]   a bare page with the shim, _lib.js, NAME.js and the slow image. The
                           script puts boards in iframes of chosen sizes (__t.frame, __t.size, __t.drop):
                           each board loads and attaches its stream as for a user, while the slow image holds
                           only this page's load event, and with it the screenshot. A script removes its
                           frames and closes the stream (__t.cleanup) before it posts, so no media request
                           is left in flight. __t.pane(frame) reads what a board's stream pane shows
  __media=stub             (with ?__shim=1, in tests/ui/_media.js) a fake MediaSource, SourceBuffer and
                           media element: it reads the appended MP4 boxes, buffers by their times, plays in
                           real time (__rate=N: N times faster), and records it all in window.__rec.media.
                           remove() runs on to the next keyframe, as in a browser, and unmuting without a
                           gesture pauses the element. Options: __autoplay=allowed|muted|blocked (what play()
                           is let do before the script sets __rec.gesture = true: unmuted, muted only, not
                           at all), __vw/__vh (the picture's size, 1920x1080), __mse=ms|mms|both|none (which
                           of MediaSource and ManagedMediaSource exist, ms), __open=never (sourceopen never
                           fires), __rate=N. The script reads and steers it through window.__rec.media: plays,
                           unmutes, seeks, appends, removes, fetches (the page's media requests), fragments,
                           supports, srcs, loads, opens, inits; hold (the playhead stands still), refuseUnmute,
                           failNext = 'quota', fail(), resizeTo(w, h), endStreaming(), startStreaming(), snap()
  GET  /__stream?url=U[&state=live][&ago=S][&error=T][&id=ID][&shown=URL][&video=C][&audio=C][&viewers=N]
                           makes /api/events carry this stream object, in place of the board's own (U is a
                           tests/lib/fake_ffmpeg.py URL; live sets the codecs and the size from it), and serves
                           its media at /api/stream/media for as long as it is injected. /__stream?off=1 ends it.
                           /__stream?raw=<json>[&src=U] injects that JSON itself as the "stream" (raw=missing:
                           no such key, as an older board answers): what a page must cope with
  __shot=1                 (read by _lib.js) __t.cleanup() leaves the frames and the stream as they are, so the
                           screenshot shows them (test_ui.py passes it when UI_SHOTS_DIR is set)
"""
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

sys.dont_write_bytecode = True
UI = Path(__file__).resolve().parent
sys.path.insert(0, str(UI.parent.parent / 'tasks'))
sys.path.insert(0, str(UI.parent / 'lib'))
import server  # noqa: E402
try:
    import fake_ffmpeg  # noqa: E402  its boxes make the media of an injected stream
except ImportError:
    fake_ffmpeg = None

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
    try { if (!q.get('__keep')) localStorage.clear(); if (q.get('__local')) localStorage.setItem('taskstatus.settings.local', q.get('__local')); } catch (e) {}
    window.__rec = { notes: [], instances: [], closes: [], tones: [], nodes: 0, perms: 0, contexts: 0, gesture: false };
    if (Number(q.get('__skew'))) { var realNow = Date.now, skew = Number(q.get('__skew')) * 1000; Date.now = function () { return realNow() + skew; }; }
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
# The bare page /__frame serves: the shim goes in at <head>, the test script and the slow image before </body>.
FRAME = (b'<!doctype html>\n<html lang="en"><head><meta charset="utf-8"><title>Task Status frames</title>'
         b'<style>html, body { margin: 0; background: #9aa0a6; } iframe { position: absolute; border: 0; background: #fff; }</style>'
         b'</head><body></body></html>')
STREAM = [None]     # the injected stream: {'obj': what /api/events carries, 'url': the fake ffmpeg URL its media is made from}
MISSING = object()  # as 'obj': /api/events has no stream key at all (an older board)


def tuned(page: bytes, spec: str) -> bytes:
    """The page with constants set (__tune=STREAM_PARK_MS:3000,...): its `const NAME = value;` lines, STREAM_ ones only."""
    for item in spec.split(','):
        name, _, value = item.partition(':')
        if not re.fullmatch(r'STREAM_[A-Z_]+', name) or not re.fullmatch(r'[0-9.]+', value):
            raise ValueError(f'bad __tune item {item!r}')
        page, found = re.subn(rb'(    const %s = )[^;]+;' % name.encode(), rb'\g<1>%s;' % value.encode(), page)
        if found != 1:
            raise ValueError(f'__tune: no constant {name} in the page')
    return page


def served(base: bytes, q: dict, test: str | None = None, wait: str = '4000') -> bytes:
    """base (the page, or the bare frame page) with the shim and, for a test, its script and the slow image."""
    if q.get('__tune'):
        base = tuned(base, q['__tune'][0])
    shim = SHIM
    if q.get('__media', [''])[0] == 'stub':
        shim += b'<script>' + (UI / '_media.js').read_bytes() + b'</script>'
    page = base.replace(b'<head>', b'<head>' + shim, 1)
    if test is None:
        return page
    inject = (f'<script src="/__test/_lib.js"></script><script src="/__test/{test}.js"></script>'
              f'<img src="/__slow?ms={int(wait)}" alt="" style="position:absolute;width:1px;height:1px;opacity:0">')
    return page.replace(b'</body>', inject.encode() + b'</body>', 1)


class Handler(server.Handler):
    def _reply(self, code, payload):
        injected = STREAM[0]
        if injected is not None and code == 200 and urlsplit(self.path).path.rstrip('/') == '/api/events':
            if injected['obj'] is MISSING:
                payload.pop('stream', None)
            else:
                payload['stream'] = injected['obj']
        super()._reply(code, payload)

    def _stream_hook(self, q):
        """/__stream?url=U&state=live...: the board reports this stream (see the docstring) until /__stream?off=1."""
        if q.get('off'):
            STREAM[0] = None
            return self._raw(200, b'{"stream": null}', 'application/json')
        one = lambda name, default=None: q.get(name, [default])[0]  # noqa: E731
        if q.get('raw') and fake_ffmpeg is not None:
            # exactly this JSON (or `missing`: no key) as the events' stream, whatever it is: an odd answer for the page
            try:
                obj = MISSING if one('raw') == 'missing' else json.loads(one('raw'))
            except ValueError:
                return self._raw(400, b'raw is not JSON', 'text/plain')
            STREAM[0] = {'obj': obj, 'url': one('src') or 'http://fake.invalid/live'}
            return self._raw(200, b'{}', 'application/json')
        url = one('url')
        if not url or fake_ffmpeg is None:
            return self._raw(400, b'/__stream needs url=<a tests/lib/fake_ffmpeg.py URL>', 'text/plain')
        mode, p = fake_ffmpeg.parse_url(url)
        live, now = one('state', 'connecting') == 'live', time.time()
        has_video = live and mode != 'audio'
        video = one('video') or (('hev1' if mode == 'hevc' else 'avc1.' + p.str('codec', '42c01f').upper()) if has_video else None)
        audio = one('audio') or ('mp4a.40.2' if live and p.flag('audio', True) else None)
        sid = one('id') or secrets.token_hex(6)
        obj = {'id': sid, 'url': one('shown') or url, 'opened_at': now, 'state': 'live' if live else 'connecting',
               'since': now - float(one('ago', 0)), 'error': one('error'), 'video': video, 'audio': audio,
               'width': p.int('w', 320) if video else None, 'height': p.int('h', 180) if video else None,
               'viewers': int(one('viewers', 0)), 'media': f'/api/stream/media?id={sid}'}
        STREAM[0] = {'obj': obj, 'url': url}
        return self._raw(200, json.dumps(obj).encode(), 'application/json')

    def _injected_media(self, q):
        """GET /api/stream/media?id= of the injected stream: what the fake ffmpeg would write for its URL, paced like it."""
        injected = STREAM[0]
        obj = injected['obj']
        if not isinstance(obj, dict) or q.get('id', [''])[0] != obj.get('id'):
            return self._raw(410, b'{"error": "that stream was replaced or closed"}', 'application/json')
        if obj.get('state') != 'live':
            return self._raw(503, b'{"error": "the stream is not connected yet"}', 'application/json')
        mode, p = fake_ffmpeg.parse_url(injected['url'])
        run = fake_ffmpeg.Run([], injected['url'], mode, p)
        plan = run.plan()
        codecs = ','.join(c for c in (obj.get('video'), obj.get('audio')) if c)
        self.send_response(200)
        self.send_header('Content-Type', f"{'video' if obj.get('video') else 'audio'}/mp4; codecs=\"{codecs}\"")
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Connection', 'close')
        self.end_headers()
        self.close_connection = True
        try:
            self.wfile.write(run.init())
            self.wfile.flush()
            k = 0
            while STREAM[0] is injected:
                fragment = run.fragment(**run.schedule(k, plan['lead'], plan['drop_after']))
                run.pos += len(fragment)
                self.wfile.write(fragment)
                self.wfile.flush()
                k += 1
                time.sleep(run.pace)
        except OSError:
            pass  # the page went away

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
        if split.path == '/__stream':
            return self._stream_hook(q)
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
        if self.command == 'GET' and split.path == '/api/stream/media' and STREAM[0] is not None and fake_ffmpeg is not None:
            return self._injected_media(q)
        is_page = split.path == '/' or split.path.startswith('/r')
        if self.command == 'GET' and (split.path == '/__frame' or is_page) and ('__test' in q or ('__shim' in q and is_page)):
            name, wait = q.get('__test', [None])[0], q.get('__wait', ['4000'])[0]
            if name is not None and not re.fullmatch(r'[a-z]+', name):
                return self._raw(400, b'bad __test', 'text/plain')
            base = FRAME if split.path == '/__frame' else (server.TASKS_DIR / 'static' / 'index.html').read_bytes()
            try:
                page = served(base, q, name, wait)
            except ValueError as exc:
                return self._raw(400, str(exc).encode(), 'text/plain')
            return self._raw(200, page, 'text/html; charset=utf-8')
        return server.Handler._dispatch(self)

    do_GET = do_HEAD = do_POST = do_DELETE = do_PUT = do_PATCH = do_OPTIONS = _hooked


server.Handler = Handler  # server.listen() looks the handler class up at call time

if __name__ == '__main__':
    sys.exit(server.main(sys.argv[1:]))
