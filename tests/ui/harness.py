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
  GET  /__test/NAME.js     serves tests/ui/NAME.js
  A page URL with ?__test=NAME[&__wait=MS] is served with, injected: a shim at the top of <head>
  that records alert/confirm calls (confirm answers yes) and JS errors, and before </body> the
  test script (after _lib.js, its helpers) plus the slow <img>.
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
SHIM = (b'<script>window.__testQuery=location.search;window.__alerts=[];window.__errors=[];'
        b'window.alert=function(m){__alerts.push("alert:"+m)};'
        b'window.confirm=function(m){__alerts.push("confirm:"+m);return true};'
        b'addEventListener("error",function(e){__errors.push(String(e.message))});</script>')
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
        if split.path.startswith('/__test/'):
            name = split.path[len('/__test/'):]
            if not re.fullmatch(r'_?[a-z]+\.js', name) or not (UI / name).is_file():
                return self._raw(404, b'', 'text/plain')
            return self._raw(200, (UI / name).read_bytes(), 'text/javascript')
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
