#!/usr/bin/env python3
"""The server's v2 additions: X-Tasks-Version / X-Tasks-Docs on every response (http.server's own errors
included), docs_version, /api/changelog and the {{DOCS_VERSION}} token; client-made request ids and the
idempotent create; attention on tasks and requests (auto-clear, waiting, tasks_waiting, counts.waiting,
the stale and expiry exemptions); the event log (the events each change writes, their order and shape,
/api/events: baseline, cursor, limit, truncation, pruned detection); replayed writes (X-Tasks-Replay and
their `at`); the settings file (defaults, PUT validation, atomic writes, hand edits, DELETE, a broken
file, --config / TASKS_CONFIG / the default path); event pruning by the maintenance thread; and the
in-place migration of a real v1 database (made by the v1 server from git), and a v2 one; reports (PUT,
GET, DELETE, the sandboxed page, validation and limits, events, replays, the Markdown renderer). TLS is the
tls suite's.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the checkout, even when run without run_all.sh
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from harness import (REPO, SERVER, ScratchServer, Suite, clean_env, connects, docs_version,  # noqa: E402
                     free_port, scratch_home, tasks_copy, tempdir, wait_until)
from harness import call as http_call  # noqa: E402

S = Suite('server_v2')
check = S.check
TMP = tempdir('server-v2')
scratch_home(TMP)
V1_COMMIT = 'c57f075'   # the last v1 release: its server.py makes the v1 database the migration starts from
EVENT_KEYS = ['id', 'ts', 'type', 'request_id', 'task_id', 'request_title', 'task_title', 'status', 'percent',
              'message', 'replayed']
REPLAY = {'X-Tasks-Replay': '1'}
EVIL = {'Origin': 'http://evil.example'}


def raw_request(port: int, data: bytes):
    """Bytes straight to the socket (what http.client refuses to send): (status, headers, body)."""
    with socket.create_connection(('127.0.0.1', port), 5) as s:
        s.sendall(data)
        out = b''
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
    head, _, body = out.partition(b'\r\n\r\n')
    lines = head.decode('latin-1').split('\r\n')
    headers = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip() for line in lines[1:] if ':' in line}
    return int(lines[0].split()[1]), headers, body


def kinds(events, *fields):
    """(type, task_id) per event, or the named fields."""
    fields = fields or ('type', 'task_id')
    return [tuple(e[f] for f in fields) for e in events]


def phase_report_port():
    S.section('the report port: report pages in an origin of their own, and nothing else')
    rport = free_port()
    srv = ScratchServer(TMP, '--report-port', str(rport), name='rport').start()
    try:
        call = srv.call
        rid = call('POST', '/api/requests', {'title': 'On the report port'})[2]['id']
        s, h, b = call('PUT', f'/api/requests/{rid}/report', {'format': 'html', 'content': '<title>T</title><p>x</p>\n'})
        page = f'http://127.0.0.1:{rport}/api/requests/{rid}/report/page'
        check('a report\'s page is on the report port, on the host the board was reached by', s == 201 and b['page'] == page, b)
        check('the request (detail and list) and GET report say the same page', call('GET', f'/api/requests/{rid}')[2]['report']['page'] == page
              and [r for r in call('GET', '/api/requests')[2]['requests'] if r['id'] == rid][0]['report']['page'] == page
              and call('GET', f'/api/requests/{rid}/report')[2]['page'] == page)
        b = call('GET', '/api/health')[2]
        check('health: the report port, no https twin', b['report_port'] == rport and b['report_tls_port'] is None, b)
        s, h, body = http_call(rport, 'GET', f'/api/requests/{rid}/report/page')
        check('on the report port: the page, sandboxed with an origin of its own (allow-same-origin) and the artifact policy',
              s == 200 and h['content-security-policy'] == REPORT_CSP_OWN and body.startswith(ARTIFACT_HEAD) and h['cache-control'] == 'no-store', h)
        s, h, body = http_call(rport, 'HEAD', f'/api/requests/{rid}/report/page')
        check('HEAD on the report port: the headers', s == 200 and not body and h['content-security-policy'] == REPORT_CSP_OWN, (s, h))
        for method, path in (('GET', '/api/requests'), ('GET', '/api/health'), ('GET', '/'), ('GET', f'/r/{rid}'), ('GET', f'/api/requests/{rid}'),
                             ('GET', f'/api/requests/{rid}/report'), ('POST', '/api/requests'), ('DELETE', f'/api/requests/{rid}'),
                             ('PUT', f'/api/requests/{rid}/report'), ('GET', '/api/events'), ('GET', '/api/skill/taskctl.py')):
            s, h, b = http_call(rport, method, path, {} if method in ('POST', 'PUT') else None)
            check(f'on the report port, {method} {path}: 404, report pages only (naming the board\'s port)', s == 404
                  and b['error'] == f'this port serves report pages only: no {method} {path}' and b['hint'] == f'the board is on port {srv.port}', b)
        check('the request is untouched by those', call('GET', f'/api/requests/{rid}')[0] == 200 and call('GET', f'/api/requests/{rid}/report')[0] == 200)
        s, h, body = http_call(rport, 'GET', '/favicon.ico')
        check('the report port\'s favicon: 204', s == 204, s)
        s, h, body = call('GET', f'/api/requests/{rid}/report/page')
        check('the board\'s own port still serves the page, in an opaque origin', s == 200 and h['content-security-policy'] == REPORT_CSP, h)
    finally:
        srv.stop()
    check('the log names the report port', f'report pages on http://0.0.0.0:{rport}' in srv.log_text() or f':{rport}' in srv.log_text(),
          srv.log_text()[-600:])
    srv = ScratchServer(TMP, '--report-port', '0', name='rport0').start()
    try:
        rid = srv.call('POST', '/api/requests', {'title': 'No report port'})[2]['id']
        b = srv.call('PUT', f'/api/requests/{rid}/report', {'format': 'markdown', 'content': '# x'})[2]
        check('--report-port 0: no report port, the page on the board\'s own port', b['page'] == f'{srv.url}/api/requests/{rid}/report/page'
              and srv.call('GET', '/api/health')[2]['report_port'] is None, b)
    finally:
        srv.stop()
    # the default: --port + 1; when that is taken, the board starts anyway, its pages on its own port, with a warning
    port = free_port()
    blocker = socket.socket()
    try:
        blocker.bind(('0.0.0.0', port + 1))
        blocker.listen(1)
    except OSError:
        blocker.close()
        blocker = None
    if blocker is None:
        S.skip('report port default', f'port {port + 1} is not free here')
    else:
        try:
            srv = ScratchServer(TMP, port=port, name='rportbusy', env=clean_env(TASKS_REPORT_PORT=None)).start()
            try:
                check('the default report port (--port + 1) taken: the board starts, with no report port, and says why',
                      srv.call('GET', '/api/health')[2]['report_port'] is None and 'report pages: error: --report-port: port' in srv.log_text()
                      and "served on the board's own port instead" in srv.log_text(), srv.log_text()[-600:])
            finally:
                srv.stop()
            res = subprocess.run([sys.executable, str(SERVER), '--port', str(free_port()), '--report-port', str(port + 1), '--db', str(TMP / 'busy.db'),
                                  '--pidfile', str(TMP / 'busy.pid'), '--config', str(TMP / 'busy.json')], capture_output=True, text=True, timeout=15, env=clean_env())
            check('a --report-port that is taken: exit 2, a clear error', res.returncode == 2
                  and f'error: --report-port: port {port + 1} already in use' in res.stderr, (res.returncode, res.stderr[-300:]))
        finally:
            blocker.close()
    free = free_port()
    srv = ScratchServer(TMP, port=free, name='rportdef', env=clean_env(TASKS_REPORT_PORT=None))
    try:
        srv.start()
        b = srv.call('GET', '/api/health')[2]
        check('no --report-port and no TASKS_REPORT_PORT: the report port is --port + 1 (or the next free one)',
              b['report_port'] in (free + 1, free + 2, free + 3, None), b)
    finally:
        srv.stop()
    for extra, msg in ((['--report-tls-port', '9999'], '--report-tls-port needs --tls-port'), (['--report-port', '70000'], '--report-port must be 1..65535'),
                       (['--report-port', 'x'], 'invalid int value')):
        res = subprocess.run([sys.executable, str(SERVER), '--port', '18765', *extra], capture_output=True, text=True, timeout=15, env=clean_env())
        check(f'{" ".join(extra)}: exit 2, {msg}', res.returncode == 2 and msg in res.stderr, res.stderr[-200:])
    res = subprocess.run([sys.executable, str(SERVER), '--port', '18765', '--report-port', '18765'], capture_output=True, text=True, timeout=15, env=clean_env())
    check('--report-port the same as --port: exit 2', res.returncode == 2 and 'must differ from the other ports' in res.stderr, res.stderr[-200:])


# ---------------------------------------------------------------- reports

REPORT_KEYS = ['format', 'title', 'artifact_url', 'size', 'version', 'created_at', 'updated_at', 'url', 'page']
# The artifact viewer's sandbox and content policy, which the board's report page takes on (see server.py, Reports).
REPORT_POLICY = ("default-src 'none'; script-src 'unsafe-inline' 'unsafe-eval' blob: https://cdnjs.cloudflare.com https://cdn.jsdelivr.net/npm/ "
              "https://unpkg.com https://cdn.tailwindcss.com https://code.jquery.com; style-src 'unsafe-inline' https://fonts.googleapis.com; "
              "font-src data: https://fonts.gstatic.com; img-src data: blob:; media-src data: blob:; connect-src data: blob:; worker-src blob:; "
              "frame-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'")
REPORT_CSP = 'sandbox allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms allow-pointer-lock; ' + REPORT_POLICY
REPORT_CSP_OWN = 'sandbox allow-same-origin allow-scripts allow-popups allow-popups-to-escape-sandbox allow-forms allow-pointer-lock; ' + REPORT_POLICY
# The skeleton claude.ai serves every artifact page in (copied from the pages it serves), up to the end of its <head>.
ARTIFACT_HEAD = ('<!doctype html><html><head><meta charset=utf8><meta name=viewport content="width=device-width,initial-scale=1,'
                 'viewport-fit=cover"><style>:root{color-scheme:light;box-sizing:border-box;padding-top:env(safe-area-inset-top,0px);'
                 'padding-bottom:env(safe-area-inset-bottom,0px)}html{scroll-padding-top:env(safe-area-inset-top,0px)}body{margin:0;'
                 'padding:0;font:14px -apple-system,BlinkMacSystemFont,sans-serif;background:#fff;color:#000}img{max-width:100%}'
                 '[hidden]:not([hidden=until-found i]){display:none!important}</style>')


def phase_reports():
    S.section('reports: PUT, GET, DELETE, the page, validation, events, replays')
    srv = ScratchServer(TMP, name='reports').start()
    call = srv.call
    sys.path.insert(0, str(REPO / 'tasks'))
    import server as board  # the renderer, for its units below
    try:
        rid = call('POST', '/api/requests', {'title': 'With a report', 'tasks': ['a']})[2]['id']
        s, h, b = call('GET', f'/api/requests/{rid}/report')
        check('no report yet: 404 with a hint, and the request says null', s == 404 and b['error'] == f'request {rid} has no report'
              and 'taskctl report' in b['hint'] and call('GET', f'/api/requests/{rid}')[2]['report'] is None, b)
        doc = ('<!doctype html>\n<html lang="en">\n<head>\n<meta charset="utf-8">\n<title>Build &amp; test — résumé</title>\n'
               '<style>:root{--bg:#fff}</style>\n</head>\n<body>\n<h1>Weekly ✓</h1>\n<script>document.body.dataset.ran = 1</script>\n'
               '</body>\n</html>\n')
        s, h, b = call('PUT', f'/api/requests/{rid}/report', {'format': 'html', 'content': doc,
                                                              'artifact_url': 'https://claude.ai/artifact/0123'})
        base = f'http://127.0.0.1:{srv.port}'
        check('PUT html: 201, the Report shape in order, the title from <title> (entities read), size in UTF-8 bytes, version 1',
              s == 201 and list(b) == REPORT_KEYS + ['request_id', 'now'] and b['format'] == 'html' and b['title'] == 'Build & test — résumé'
              and b['size'] == len(doc.encode()) and b['version'] == 1 and b['created_at'] == b['updated_at']
              and b['artifact_url'] == 'https://claude.ai/artifact/0123' and b['url'] == f'{base}/r/{rid}/report'
              and b['page'] == f'{base}/api/requests/{rid}/report/page' and b['request_id'] == rid, b)
        first = b
        s, h, b = call('GET', f'/api/requests/{rid}/report')
        check('GET: the same report and its content, exactly as sent', s == 200 and list(b) == REPORT_KEYS + ['request_id', 'content', 'now']
              and b['content'] == doc and {k: b[k] for k in REPORT_KEYS} == {k: first[k] for k in REPORT_KEYS}, b)
        d = call('GET', f'/api/requests/{rid}')[2]
        lst = [r for r in call('GET', '/api/requests')[2]['requests'] if r['id'] == rid][0]
        check('the request (detail and list) carries the Report without its content; the write bumped updated_at',
              d['report'] == {k: first[k] for k in REPORT_KEYS} and lst['report'] == d['report'] and 'content' not in d['report']
              and d['updated_at'] == first['updated_at'], d['report'])
        md = '# Findings: *all* good\n\n- one\n- two\n'
        s, h, b = call('PUT', f'/api/requests/{rid}/report', {'format': 'Markdown', 'content': md, 'title': '  Final   report  '})
        check('PUT again: 200, replaced (version 2, created_at kept), format case-insensitive, the given title squashed, '
              'artifact_url gone (a PUT replaces it all)', s == 200 and b['version'] == 2 and b['format'] == 'markdown'
              and b['created_at'] == first['created_at'] and b['updated_at'] > first['updated_at'] and b['title'] == 'Final report'
              and b['artifact_url'] is None, b)
        s, h, b = call('PUT', f'/api/requests/{rid}/report', {'format': 'markdown', 'content': md})
        check('a Markdown title from its first heading, its marks dropped', b['title'] == 'Findings: all good', b)

        S.section('reports: validation and limits')
        for body, field in (({'content': 'x'}, 'format'), ({'format': 'pdf', 'content': 'x'}, 'format'), ({'format': 1, 'content': 'x'}, 'format'),
                            ({'format': 'html'}, 'content'), ({'format': 'html', 'content': '  \n '}, 'content'),
                            ({'format': 'html', 'content': ['x']}, 'content'), ({'format': 'html', 'content': 'x', 'artifact_url': 'ftp://x/y'}, 'artifact_url'),
                            ({'format': 'html', 'content': 'x', 'artifact_url': 'javascript:alert(1)'}, 'artifact_url'),
                            ({'format': 'html', 'content': 'x', 'artifact_url': 5}, 'artifact_url'),
                            ({'format': 'html', 'content': 'x', 'artifact_url': 'https://a.example/' + 'x' * 2048}, 'artifact_url'),
                            ({'format': 'html', 'content': 'x', 'artifact_url': 'https://a.example/a b'}, 'artifact_url'),
                            ({'format': 'html', 'content': 'x', 'title': 7}, 'title')):
            s, h, b = call('PUT', f'/api/requests/{rid}/report', body)
            check(f'PUT {json.dumps(body)[:70]}: 400 field {field}', s == 400 and b.get('field') == field, b)
        s, h, b = call('PUT', f'/api/requests/{rid}/report', {'format': 'html', 'content': 'x\0y', 'title': 'T' * 300, 'extra': 1})
        check('a long title truncated, an unknown field ignored (warnings); NULs dropped from the content', s == 200 and len(b['title']) == 200
              and b['warnings'] == ["unknown field 'extra' ignored", 'title truncated to 200 characters']
              and call('GET', f'/api/requests/{rid}/report')[2]['content'] == 'xy', b)
        big = '<p>' + 'x' * (16 * 1024 * 1024 - 7) + '</p>'
        s, h, b = call('PUT', f'/api/requests/{rid}/report', {'format': 'html', 'content': big}, timeout=60)
        check('a report of exactly 16 MiB, an artifact page\'s limit: accepted', s == 200 and b['size'] == 16 * 1024 * 1024, (s, b.get('error')))
        s, h, b = call('PUT', f'/api/requests/{rid}/report', {'format': 'html', 'content': big + 'x'}, timeout=60)
        check('a byte more: 413 naming the content', s == 413 and b.get('field') == 'content'
              and b['error'] == "content is larger than 16 MiB (an artifact page's limit)", b)
        def claim(method, path, length):     # only the headers: the board answers 413 before it reads a body that large
            s, h, raw = raw_request(srv.port, f'{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{srv.port}\r\nContent-Type: application/json\r\n'
                                              f'Content-Length: {length}\r\nConnection: close\r\n\r\n'.encode())
            return s, json.loads(raw)
        s, b = claim('PUT', f'/api/requests/{rid}/report', 32 * 1024 * 1024 + 1)
        check('a body over 32 MiB: 413', s == 413 and b['error'] == 'request body is larger than 32768 KiB', b)
        s, b = claim('POST', f'/api/requests/{rid}/attention', 64 * 1024 + 1)
        check('other routes keep their 64 KiB limit', s == 413 and b['error'] == 'request body is larger than 64 KiB', b)
        s, h, b = call('PUT', '/api/requests/zzzzzz/report', {'format': 'html', 'content': 'x'})
        check('an unknown request: 404', s == 404 and b['error'] == "unknown request 'zzzzzz'", b)
        s, h, b = call('PUT', f'/api/requests/{rid}-1/report', {'format': 'html', 'content': 'x'})
        check('a task id: 400 naming its request', s == 400 and b['hint'] == f'{rid}-1 is a task id; its request is {rid}', b)
        s, h, b = call('PUT', f'/api/requests/{rid}/report', {'format': 'html', 'content': 'x'}, headers={'Origin': 'null'})
        check('a write from a sandboxed report (Origin: null): 403', s == 403, b)
        s, h, b = call('PUT', f'/api/requests/{rid}/report', {'format': 'html', 'content': 'x'}, headers=EVIL)
        check('a cross-origin write: 403', s == 403, b)
        done = call('POST', '/api/requests', {'title': 'Already done'})[2]['id']
        call('POST', f'/api/requests/{done}/complete', {'status': 'done'})
        s, h, b = call('PUT', f'/api/requests/{done}/report', {'format': 'markdown', 'content': '# Wrap-up'})
        check('a report on a done request: allowed, and the request stays done', s == 201
              and call('GET', f'/api/requests/{done}')[2]['status'] == 'done', b)

        S.section('reports: the page')
        call('PUT', f'/api/requests/{rid}/report', {'format': 'html', 'content': doc})
        s, h, page = call('GET', f'/api/requests/{rid}/report/page')
        check('the page: text/html, sandboxed by its CSP, not cached, not sniffed, no referrer, no CORS',
              s == 200 and h['content-type'] == 'text/html; charset=utf-8' and h['content-security-policy'] == REPORT_CSP
              and h['cache-control'] == 'no-store' and h['x-content-type-options'] == 'nosniff' and h['referrer-policy'] == 'no-referrer'
              and 'access-control-allow-origin' not in h, h)
        shim = board.REPORT_SHIM % 'null'
        check('the server\'s skeleton is the one claude.ai serves', board.ARTIFACT_HEAD == ARTIFACT_HEAD)
        check('an HTML document of its own: wrapped all the same, as claude.ai wraps every page, the shim last in the skeleton\'s head',
              page == ARTIFACT_HEAD + shim + '</head><body>\n' + doc + '</body></html>', page[:300])
        s, h, page = call('GET', f'/api/requests/{rid}/report/page?theme=dark')
        check('?theme=dark: the shim sets data-theme "dark"', board.REPORT_SHIM % '"dark"' in page, page[:300])
        s, h, page = call('GET', f'/api/requests/{rid}/report/page?theme=sepia')
        check('an unknown theme: none (the system\'s)', shim in page)
        s, h, body = call('HEAD', f'/api/requests/{rid}/report/page')
        check('HEAD: the headers only', s == 200 and h['content-security-policy'] == REPORT_CSP and not body, (s, body))
        source = '<title>T</title>\n<style>p { color: red }</style>\n<p>An artifact\'s source, as an agent writes it.</p>\n'
        call('PUT', f'/api/requests/{rid}/report', {'format': 'html', 'content': source})
        page = call('GET', f'/api/requests/{rid}/report/page')[2]
        served = ARTIFACT_HEAD + '</head><body>\n' + source + '</body></html>'
        check('an artifact\'s source: the page claude.ai serves for it, byte for byte, plus the shim', page == served.replace('</head>', shim + '</head>', 1), page)
        call('PUT', f'/api/requests/{rid}/report', {'format': 'html', 'content': '\ufeff' + served})
        check('the page as claude.ai serves it (a BOM, the skeleton and all): the same page, not wrapped twice',
              call('GET', f'/api/requests/{rid}/report/page')[2] == page)
        check('mermaid diagrams: the shim loads the renderer only when the page has one', board.MERMAID_JS in shim
              and "querySelector('pre.mermaid')" in shim)
        call('PUT', f'/api/requests/{rid}/report', {'format': 'markdown', 'content': '# A <b>bold</b> & title\n\nText', 'title': 'x < y'})
        page = call('GET', f'/api/requests/{rid}/report/page?theme=light')[2]
        check('a Markdown page: the board\'s page around the rendering, the title escaped, the theme set, the styles in',
              '<title>x &lt; y</title>' in page and board.REPORT_SHIM % '"light"' in page and board.MARKDOWN_CSS in page
              and '<article class="md">\n<h1 id="a-bboldb--title">A &lt;b&gt;bold&lt;/b&gt; &amp; title</h1>\n<p>Text</p>\n</article>' in page, page[-400:])

        S.section('reports: DELETE, events, replays, and the request going')
        s, h, b = call('DELETE', f'/api/requests/{rid}/report')
        check('DELETE: deleted, the request says null', s == 200 and b == {'deleted': True, 'request_id': rid, 'now': b['now']}
              and call('GET', f'/api/requests/{rid}')[2]['report'] is None, b)
        s, h, b = call('DELETE', f'/api/requests/{rid}/report')
        check('DELETE again: 200, deleted false', s == 200 and b['deleted'] is False, b)
        s, h, page = call('GET', f'/api/requests/{rid}/report/page')
        check('the page of no report: 404, a small page saying so, sandboxed too', s == 404 and 'This request has no report.' in page
              and h['content-security-policy'] == REPORT_CSP, page)
        evs = [e for e in srv.events(0)['events'] if e['type'].startswith('report_') and e['request_id'] == rid]
        check('events: report_updated per write (message: the title), report_deleted once', [e['type'] for e in evs].count('report_updated') == 9
              and [e['type'] for e in evs][-1] == 'report_deleted' and evs[0]['message'] == 'Build & test — résumé'
              and evs[-1]['message'] == 'x < y' and all(list(e) == EVENT_KEYS for e in evs), [(e['type'], e['message']) for e in evs])
        before = call('GET', f'/api/requests/{rid}')[2]['updated_at']
        at = time.time() - 30
        s, h, b = call('PUT', f'/api/requests/{rid}/report', {'format': 'markdown', 'content': 'late', 'at': at}, headers=REPLAY)
        check('a replayed PUT: its own time, never before the request\'s last write; the event replayed', s == 201
              and b['updated_at'] == max(round(at, 3), before) and srv.events(0)['events'][-1]['replayed'] is True, (b, before, at))
        s, h, b = call('PUT', f'/api/requests/{rid}/report', {'format': 'markdown', 'content': 'x', 'at': at})
        check('"at" without the replay header: an unknown field', s == 200 and b['warnings'] == ["unknown field 'at' ignored"], b)
        call('DELETE', f'/api/requests/{rid}')
        with sqlite3.connect(srv.db) as db:
            left = db.execute('SELECT COUNT(*) FROM reports WHERE request_id = ?', (rid,)).fetchone()[0]
        check('deleting the request deletes its report', left == 0, left)
    finally:
        srv.stop()
    check('the reports board logged no tracebacks', 'Traceback' not in srv.log_text(), srv.log_text()[-1500:])

    S.section('reports: the Markdown renderer')
    md = board.render_markdown
    cases = (
        ('# Title *x*\n## Two\n### Two', '<h1 id="title-x">Title <em>x</em></h1>\n<h2 id="two">Two</h2>\n<h3 id="two-1">Two</h3>'),
        ('Setext\n===\n\nAnd\n---', '<h1 id="setext">Setext</h1>\n<h2 id="and">And</h2>'),
        ('**b** __b__ *i* _i_ ~~s~~ `c<d>` snake_case_name', '<p><strong>b</strong> <strong>b</strong> <em>i</em> <em>i</em> <del>s</del> '
                                                            '<code>c&lt;d&gt;</code> snake_case_name</p>'),
        ('a  \nb\\\nc\nd', '<p>a<br>\nb<br>\nc\nd</p>'),
        ('[t](https://x.example/a_(b) "T") <https://y.example> https://z.example/p.', '<p><a href="https://x.example/a_(b)" title="T">t</a> '
         '<a href="https://y.example">https://y.example</a> <a href="https://z.example/p">https://z.example/p</a>.</p>'),
        ('[x](javascript:alert(1)) ![y](javascript:x) ![z](data:image/png;base64,AA) [**w**](#top)',
         '<p>x y <img src="data:image/png;base64,AA" alt="z"> <a href="#top"><strong>w</strong></a></p>'),
        ('<script>alert(1)</script> & "q"', '<p>&lt;script&gt;alert(1)&lt;/script&gt; &amp; &quot;q&quot;</p>'),
        ('[a](http://e.example/" onmouseover="x)',   # no link (a destination has no spaces), and the quote stays text
         '<p>[a](<a href="http://e.example/">http://e.example/</a>&quot; onmouseover=&quot;x)</p>'),
        ('\\*no\\* \\_no\\_ \\`no\\`', '<p>*no* _no_ `no`</p>'),
        ('- a\n- b\n  - c\n- [x] d\n- [ ] e', '<ul>\n<li>a</li>\n<li>b\n<ul>\n<li>c</li>\n</ul></li>\n<li class=task><input type="checkbox" disabled checked> d</li>\n'
                                                '<li class=task><input type="checkbox" disabled> e</li>\n</ul>'),
        ('- a\n\n- b', '<ul>\n<li><p>a</p></li>\n<li><p>b</p></li>\n</ul>'),
        ('3. x\n4. y', '<ol start="3">\n<li>x</li>\n<li>y</li>\n</ol>'),
        ('> q **b**\n> > n', '<blockquote>\n<p>q <strong>b</strong></p>\n<blockquote>\n<p>n</p>\n</blockquote>\n</blockquote>'),
        ('```js\nif (a < b) {}\n```\n\n    code\n\n***', '<pre><code class="language-js">if (a &lt; b) {}\n</code></pre>\n<pre><code>code\n</code></pre>\n<hr>'),
        ('| A | B |\n|:--|--:|\n| 1 | **2** |\n| x \\| y |', '<div class="table"><table><thead><tr><th style="text-align:left">A</th>'
         '<th style="text-align:right">B</th></tr></thead><tbody><tr><td style="text-align:left">1</td><td style="text-align:right">'
         '<strong>2</strong></td></tr><tr><td style="text-align:left">x | y</td><td style="text-align:right"></td></tr></tbody></table></div>'),
        ('a | b\n---', '<h2 id="a--b">a | b</h2>'),
        ('para\n# head\n- item', '<p>para</p>\n<h1 id="head">head</h1>\n<ul>\n<li>item</li>\n</ul>'),
        ('```mermaid\ngraph TD; A-->B\n```', '<pre class="mermaid">graph TD; A--&gt;B</pre>'),
        ('', ''),
    )
    for src, want in cases:
        got = md(src)
        check(f'markdown {src[:40]!r}', got == want, (got, want))


# ---------------------------------------------------------------- headers, docs, changelog

def phase_headers():
    S.section('X-Tasks-Version / X-Tasks-Docs on every response, docs_version, /api/changelog')
    tree = tasks_copy(TMP / 'tree')
    with ScratchServer(TMP, script=tree / 'server.py', name='headers') as srv:
        P, call = srv.port, srv.call
        docs = docs_version(tree)
        check('the repo tree and its copy have the same docs version', docs == docs_version())
        s, h, b = call('GET', '/api/health')
        check('health: v2 fields, in order', s == 200 and b['version'] == '3' and b['docs_version'] == docs and b['tls_port'] is None
              and b['config_path'] == str(srv.config) and b['report_port'] is None and b['report_tls_port'] is None
              and list(b)[-5:] == ['docs_version', 'tls_port', 'config_path', 'report_port', 'report_tls_port'], b)
        check('health headers', h.get('x-tasks-version') == '3' and h.get('x-tasks-docs') == docs, h)
        routes = [('GET', '/'), ('GET', '/r/k3m9qa'), ('GET', '/api/usage'), ('GET', '/api/rule'), ('GET', '/api/changelog'),
                  ('GET', '/api/skill/SKILL.md'), ('GET', '/api/skill/taskctl'), ('GET', '/api/skill/taskctl.py'),
                  ('GET', '/favicon.ico'), ('HEAD', '/api/health'), ('GET', '/api/events'), ('GET', '/api/settings'),
                  ('GET', '/api/requests'), ('POST', '/api/requests'), ('GET', '/nope'), ('PUT', '/api/requests'),
                  ('GET', '/api/requests/zzzzzz'), ('GET', '/api/requests/bogus!'), ('GET', '/api/events?since=x'),
                  ('POST', '/api/tasks/zzzzzz-1/attention'), ('DELETE', '/api/settings'), ('GET', '/api/stream'),
                  ('POST', '/api/stream'), ('DELETE', '/api/stream'), ('GET', '/api/stream/media')]
        for method, path in routes:
            s, h, b = call(method, path, {} if method == 'POST' else None,
                           headers={'Accept': 'text/html'} if path == '/' else None)
            check(f'{method} {path} ({s}): both headers', h.get('x-tasks-version') == '3' and h.get('x-tasks-docs') == docs, h)
        s, h, b = call('POST', '/api/requests', {'title': 'x'}, headers=EVIL)
        check('403 cross-origin: both headers', s == 403 and h.get('x-tasks-version') == '3' and h.get('x-tasks-docs') == docs, h)
        s, h, b = call('POST', '/api/requests', raw=b'{"title": "' + b'x' * 70000 + b'"}')
        check('413: both headers', s == 413 and h.get('x-tasks-docs') == docs, (s, h))
        for label, data in (('400 malformed request line', b'GARBAGE\r\n\r\n'), ('501 unknown method', b'FOO / HTTP/1.0\r\n\r\n'),
                            ('414 long URI', b'GET /' + b'a' * 70000 + b' HTTP/1.0\r\n\r\n'),
                            ('431 long header', b'GET / HTTP/1.0\r\nX: ' + b'a' * 70000 + b'\r\n\r\n'),
                            ('505 HTTP/2.0', b'GET / HTTP/2.0\r\n\r\n')):
            s, h, b = raw_request(P, data)
            check(f"http.server's own {label} ({s}): both headers", h.get('x-tasks-version') == '3' and h.get('x-tasks-docs') == docs, h)

        s, h, b = call('GET', '/api/changelog')
        want = (tree / 'templates' / 'changelog.md').read_text()
        check('/api/changelog: the file, text/plain, no-store', s == 200 and h['content-type'] == 'text/plain; charset=utf-8'
              and h.get('cache-control') == 'no-store' and b == want.replace('{{BASE_URL}}', f'http://127.0.0.1:{P}')
              .replace('{{PUBLIC_URL}}', f'http://127.0.0.1:{P}').replace('{{VERSION}}', '3').replace('{{DOCS_VERSION}}', docs), b[:200])
        heads = [line for line in b.splitlines() if line.startswith('## ')]
        check('changelog: newest first, one "## v<N> — <YYYY-MM-DD>" heading per entry, v3, v2, v1',
              [line.split()[1] for line in heads] == ['v3', 'v2', 'v1'] and b.startswith('## v3 — ')
              and all(re.fullmatch(r'## v\d+ — \d{4}-\d\d-\d\d', line) for line in heads), heads)
        s, h, b = call('GET', '/api/skill/taskctl.py')
        check('taskctl.py: BAKED_DOCS and BAKED_API rendered', f'BAKED_DOCS = "{docs}"' in b and 'BAKED_API = "3"' in b
              and '{{DOCS_VERSION}}' not in b and '{{VERSION}}' not in b, [x for x in b.splitlines() if x.startswith('BAKED_')])

        S.section('docs_version follows the agent files, and only them')
        (tree / 'static' / 'index.html').write_text('<p>another page</p>')
        s, h, b = call('GET', '/api/health')
        check('editing the page (not an agent file) keeps the docs version', b['docs_version'] == docs and h['x-tasks-docs'] == docs, b)
        for rel in ('skill/SKILL.md', 'templates/usage.md', 'templates/rule.md', 'templates/changelog.md', 'taskctl', 'taskctl.py'):
            before = call('GET', '/api/health')[2]['docs_version']
            path = tree / rel
            path.write_bytes(path.read_bytes() + b'\n# edited\n')
            s, h, b = call('GET', '/api/health')
            check(f'editing {rel} changes the docs version (header and health agree)', b['docs_version'] != before
                  and b['docs_version'] == docs_version(tree) and h['x-tasks-docs'] == b['docs_version'], (before, b['docs_version']))
        (tree / 'templates' / 'usage.md').write_text('X {{DOCS_VERSION}} {{VERSION}} {{BASE_URL}}\n')
        s, h, b = call('GET', '/api/usage')
        check('the {{DOCS_VERSION}} token renders the docs version of the files being served', b == f'X {docs_version(tree)} 3 http://127.0.0.1:{P}\n'
              and h['x-tasks-docs'] == docs_version(tree), (b, h.get('x-tasks-docs')))
        (tree / 'templates' / 'changelog.md').unlink()
        s, h, b = call('GET', '/api/changelog')
        check('a missing changelog: 500 template missing; the docs version counts it as missing', s == 500
              and b == {'error': 'template missing: templates/changelog.md'} and h['x-tasks-docs'] == docs_version(tree), (s, b, h))
        (tree / 'templates' / 'changelog.md').write_text('## v2 — 2026-09-28\n- back\n')
        check('the changelog back: served again, the version follows', call('GET', '/api/changelog')[2] == '## v2 — 2026-09-28\n- back\n'
              and call('GET', '/api/health')[2]['docs_version'] == docs_version(tree))
    check('headers server: no tracebacks', 'Traceback' not in srv.log_text(), srv.log_text()[-1500:])


# ---------------------------------------------------------------- the main v2 API

def phase_api():
    S.section('client-made request ids: an idempotent create')
    srv = ScratchServer(TMP, name='api').start()
    call = srv.call
    try:
        s, h, b = call('POST', '/api/requests', {'title': 'client id', 'id': 'K3M9QA', 'tasks': ['a', 'b']})
        check('create with an id: 201, lowercased, existing false, its task ids', s == 201 and b['id'] == 'k3m9qa'
              and b['existing'] is False and [t['id'] for t in b['tasks']] == ['k3m9qa-1', 'k3m9qa-2'], b)
        base = srv.events(None)['cursor']
        s, h, b = call('POST', '/api/requests', {'title': 'client id', 'id': 'k3m9qa', 'tasks': ['a', 'b', 'c']})
        check('the same id and title again: 200, existing true, the request as it is (nothing written)', s == 200
              and b['existing'] is True and b['id'] == 'k3m9qa' and len(b['tasks']) == 2 and 'now' in b, b)
        check('an idempotent replay writes no event', srv.events(None)['cursor'] == base)
        s, h, b = call('POST', '/api/requests', {'title': 'someone else', 'id': 'k3m9qa'})
        check('the same id with another title: 409 request id already exists', s == 409 and b == {'error': 'request id already exists'}, b)
        for bad in ('k3m9q', 'k3m9qa1', 'k3m9q0', 'k3m9ql', 5, '', 'k3m9qa-1', ['k3m9qa']):
            s, h, b = call('POST', '/api/requests', {'title': 't', 'id': bad})
            check(f'a malformed id {bad!r}: 400 field id', s == 400 and b.get('field') == 'id', b)
        s, h, b = call('POST', '/api/requests', {'title': 't', 'id': None})
        check('id null: the server makes one', s == 201 and len(b['id']) == 6 and b['existing'] is False, b)
        s, h, b = call('POST', '/api/requests', {'title': 't'})
        check('no id (curl users): the server makes one', s == 201 and len(b['id']) == 6 and b['id'] != 'k3m9qa', b)

        S.section('events: what each change writes, in order')
        c0 = srv.events(None)['cursor']
        r = call('POST', '/api/requests', {'title': 'ev', 'tasks': ['one', 'two', 'three']})[2]
        rid = r['id']
        t1, t2, t3 = (f'{rid}-{i}' for i in (1, 2, 3))
        call('POST', f'/api/tasks/{t1}/progress', {'message': 'start', 'percent': 10})
        call('POST', f'/api/tasks/{t1}/progress', {'message': 'more', 'percent': 20})
        call('POST', f'/api/tasks/{t1}/complete', {'message': 'fin'})
        call('POST', f'/api/tasks/{t1}/complete', {})
        call('POST', f'/api/tasks/{t1}/complete', {'status': 'done', 'message': 'message only'})
        call('POST', f'/api/tasks/{t1}/complete', {'status': 'failed'})
        b = srv.events(c0)
        check('create, start, progress, done; a same-status complete writes nothing; an overwrite writes its status',
              kinds(b['events']) == [('request_created', None), ('task_started', t1), ('task_progress', t1), ('task_done', t1),
                                      ('task_failed', t1)], kinds(b['events']))
        e = {x['type']: x for x in b['events']}
        check('event keys, in order', all(list(x) == EVENT_KEYS for x in b['events']), list(b['events'][0]))
        check('task event fields: titles, the status and percent after the change, the message', e['task_progress']['request_title'] == 'ev'
              and e['task_progress']['task_title'] == 'one' and e['task_progress']['percent'] == 20.0
              and e['task_progress']['message'] == 'more' and e['task_progress']['status'] == 'running'
              and e['task_started']['message'] == 'start' and e['task_done']['message'] == 'fin' and e['task_done']['percent'] == 100.0
              and e['task_failed']['status'] == 'failed', e)
        check('request_created: running, 0%, no task, no message', e['request_created']['status'] == 'running'
              and e['request_created']['percent'] == 0.0 and e['request_created']['task_id'] is None
              and e['request_created']['task_title'] is None and e['request_created']['message'] is None, e['request_created'])
        check('not replayed, ts within this test', all(x['replayed'] is False and r['created_at'] <= x['ts'] <= time.time() + 1
                                                     for x in b['events']), b['events'])
        check('ids ascending, cursor = the last one, not truncated', [x['id'] for x in b['events']] == sorted(x['id'] for x in b['events'])
              and b['cursor'] == b['events'][-1]['id'] and b['truncated'] is False, b)
        c1 = b['cursor']
        call('POST', f'/api/tasks/{t2}/progress', {'message': 'w'})
        call('POST', f'/api/requests/{rid}/complete', {'message': 'all done'})
        call('POST', f'/api/requests/{rid}/complete', {'status': 'done'})
        call('POST', f'/api/requests/{rid}/complete', {'status': 'done', 'message': 'reworded'})
        call('POST', f'/api/requests/{rid}/complete', {'status': 'failed', 'message': 'nope'})
        call('POST', f'/api/requests/{rid}/tasks', {'title': 'again'})
        call('POST', f'/api/requests/{rid}/tasks', {'titles': ['p', 'q']})
        call('POST', f'/api/requests/{rid}/tasks', {'title': 'queued', 'start': False})
        call('DELETE', f'/api/requests/{rid}')
        b = srv.events(c1)
        check('request events: done (no cascade task events), no-ops write nothing, failed, reopened, added, started, deleted',
              kinds(b['events']) == [('task_started', t2), ('request_done', None), ('request_failed', None), ('request_reopened', None),
                                     ('task_added', f'{rid}-4'), ('task_started', f'{rid}-4'), ('task_added', f'{rid}-5'),
                                     ('task_added', f'{rid}-6'), ('task_added', f'{rid}-7'), ('request_deleted', None)],
              kinds(b['events']))
        e = {x['type']: x for x in b['events']}
        check("request_done: the request's message, status and percent (cancelled tasks excluded)", e['request_done']['message'] == 'all done'
              and e['request_done']['status'] == 'done' and e['request_done']['task_id'] is None
              and e['request_done']['percent'] == 100.0, e['request_done'])
        check('request_failed: its message', e['request_failed']['message'] == 'nope' and e['request_failed']['status'] == 'failed', e['request_failed'])
        check('request_reopened: running, no message', e['request_reopened']['status'] == 'running' and e['request_reopened']['message'] is None,
              e['request_reopened'])
        check('task_added: pending with its title; a started single add also writes task_started',
              [x['status'] for x in b['events'] if x['type'] == 'task_added'] == ['running', 'pending', 'pending', 'pending']
              and next(x for x in b['events'] if x['type'] == 'task_added')['task_title'] == 'again',
              [x for x in b['events'] if x['type'] == 'task_added'])
        check('request_deleted keeps the titles (events outlive the delete)', e['request_deleted']['request_title'] == 'ev'
              and e['request_deleted']['message'] is None and call('GET', f'/api/requests/{rid}')[0] == 404
              and any(x['request_id'] == rid for x in srv.events(0)['events']), e['request_deleted'])
        with sqlite3.connect(srv.db) as db:
            fks = db.execute('PRAGMA foreign_key_list(events)').fetchall()
            idx = [row[1] for row in db.execute("PRAGMA index_list(events)")]
        check('the events table has no foreign key and an index on ts', fks == [] and 'events_ts' in idx, (fks, idx))

        S.section('attention on a task')
        c2 = srv.events(None)['cursor']
        r = call('POST', '/api/requests', {'title': 'att', 'tasks': ['a', 'b']})[2]
        rid = r['id']
        a1, a2 = f'{rid}-1', f'{rid}-2'
        s, h, b = call('POST', f'/api/tasks/{a1}/attention', {'message': '  which\tbranch?\n\nmain or dev '})
        check('attention on a pending task: 200 Task + now, running, started, the question cleaned like a progress message',
              s == 200 and b['status'] == 'running' and b['started_at'] is not None and b['attention']['message'] == 'which branch?\n\nmain or dev'
              and set(b['attention']) == {'message', 'since'} and list(b)[-2:] == ['attention', 'now'], b)
        since1 = b['attention']['since']
        check('attention bumps updated_at', b['updated_at'] == since1, b)
        time.sleep(0.05)
        s, h, b = call('POST', f'/api/tasks/{a1}/attention', {'message': 'second question'})
        check('attention again replaces the message and since', b['attention']['message'] == 'second question' and b['attention']['since'] > since1, b)
        s, h, b = call('GET', f'/api/requests/{rid}')
        check("the request waits through its task: waiting, tasks_waiting 1, its own attention null", b['waiting'] is True
              and b['tasks_waiting'] == 1 and b['attention'] is None and b['updated_at'] >= since1, b)
        s, h, b = call('GET', '/api/requests')
        check('counts.waiting', b['counts']['waiting'] == 1 and list(b['counts']) == ['running', 'stale', 'done', 'failed', 'waiting'], b['counts'])
        check('the list item says waiting too', next(x for x in b['requests'] if x['id'] == rid)['waiting'] is True)
        for bad in ({}, {'message': '  '}, {'message': 5}, {'message': None}):
            s, h, b = call('POST', f'/api/tasks/{a1}/attention', bad)
            check(f'attention with a bad message {bad}: 400 field message', s == 400 and b.get('field') == 'message', b)
        s, h, b = call('POST', f'/api/tasks/{a1}/attention', {'message': 'm' * 600})
        check('the question is capped at 500 with a warning', len(b['attention']['message']) == 500 and b['attention']['message'].endswith('…')
              and b.get('warnings'), b)
        s, h, b = call('POST', f'/api/tasks/{a1}/attention', {'message': 'x', 'percent': 5})
        check('an unknown field is ignored with a warning', s == 200 and b.get('warnings') == ["unknown field 'percent' ignored"], b)
        w = srv.events(None)['waiting']
        check('/api/events waiting lists the task', len(w) == 1 and w[0]['task_id'] == a1 and w[0]['request_id'] == rid
              and w[0]['task_title'] == 'a' and w[0]['request_title'] == 'att' and w[0]['message'] == 'x'
              and list(w[0]) == ['request_id', 'request_title', 'task_id', 'task_title', 'message', 'since'], w)
        s, h, b = call('POST', f'/api/tasks/{a1}/progress', {'message': 'got it', 'percent': 50})
        check('progress clears the task\'s attention', b['attention'] is None and b['status'] == 'running', b)
        s, h, b = call('DELETE', f'/api/tasks/{a1}/attention')
        check('DELETE with nothing set: 200, a no-op', s == 200 and b['attention'] is None and 'now' in b, b)
        call('POST', f'/api/tasks/{a1}/attention', {'message': 'q3'})
        s, h, b = call('DELETE', f'/api/tasks/{a1}/attention')
        check('DELETE clears it, the task keeps running', s == 200 and b['attention'] is None and b['status'] == 'running', b)
        call('POST', f'/api/tasks/{a1}/attention', {'message': 'q4'})
        s, h, b = call('POST', f'/api/tasks/{a1}/complete', {})
        check('complete clears it', b['attention'] is None and b['status'] == 'done', b)
        s, h, b = call('POST', f'/api/tasks/{a1}/attention', {'message': 'late'})
        check('attention on a closed task: 409 {error, status}', s == 409 and b == {'error': f'task {a1} is already done', 'status': 'done'}, b)
        s, h, b = call('DELETE', f'/api/tasks/{a1}/attention')
        check('DELETE on a closed task: 200 (idempotent)', s == 200 and b['attention'] is None, b)
        b = srv.events(c2)
        got = kinds(b['events'], 'type', 'task_id', 'message')
        check('attention events: started first, each set, each actual clear (a no-op DELETE writes none)', [x[:2] for x in got] == [
            ('request_created', None), ('task_started', a1), ('attention', a1), ('attention', a1), ('attention', a1),
            ('attention', a1), ('attention_cleared', a1), ('task_progress', a1), ('attention', a1), ('attention_cleared', a1),
            ('attention', a1), ('attention_cleared', a1), ('task_done', a1)], got)
        check("attention's message is the question; attention_cleared's the question it cleared", got[2][2] == 'which branch?\n\nmain or dev'
              and got[6][2] == 'x' and got[9][2] == 'q3' and got[11][2] == 'q4', got)
        s, h, b = call('POST', f'/api/tasks/{rid}/attention', {'message': 'x'})
        check('a request id on the task attention route: 400 + hint', s == 400
              and b['hint'] == f'{rid} is a request id; use /api/requests/{rid}/attention', b)
        s, h, b = call('DELETE', f'/api/requests/{a2}/attention')
        check('a task id on the request attention route (DELETE): 400 + hint', s == 400
              and b['hint'] == f'{a2} is a task id; use /api/tasks/{a2}/attention', b)
        s, h, b = call('GET', f'/api/tasks/{a2}/attention')
        check('GET attention: 405 Allow POST, DELETE', s == 405 and h.get('allow') == 'POST, DELETE', (s, h.get('allow')))
        s, h, b = call('POST', '/api/tasks/zzzzzz-1/attention', {'message': 'x'})
        check('attention on an unknown task: 404', s == 404, b)
        for method, path in (('POST', f'/api/tasks/{a2}/attention'), ('DELETE', f'/api/tasks/{a2}/attention'),
                             ('POST', f'/api/requests/{rid}/attention'), ('DELETE', f'/api/requests/{rid}/attention')):
            s, h, b = call(method, path, {'message': 'x'} if method == 'POST' else None, headers=EVIL)
            check(f'{method} {path.replace(rid, "RID")} with a foreign Origin: 403', s == 403, b)

        S.section('attention on a request')
        c3 = srv.events(None)['cursor']
        s, h, b = call('POST', f'/api/requests/{rid}/attention', {'message': 'req q'})
        check('request attention: 200 Request + tasks + now, waiting, tasks_waiting 0', s == 200 and b['attention']['message'] == 'req q'
              and b['waiting'] is True and b['tasks_waiting'] == 0 and 'tasks' in b and 'now' in b, b)
        call('POST', f'/api/tasks/{a2}/attention', {'message': 'task q'})
        w = srv.events(None)['waiting']
        check('/api/events waiting: the request-level one (task_id null) and the task, oldest first',
              [x['task_id'] for x in w] == [None, a2] and w[0]['task_title'] is None and w[0]['message'] == 'req q', w)
        s, h, b = call('DELETE', f'/api/requests/{rid}/attention')
        check("request DELETE clears only the request's own; its task still waits", b['attention'] is None and b['waiting'] is True
              and b['tasks_waiting'] == 1 and next(t for t in b['tasks'] if t['id'] == a2)['attention']['message'] == 'task q', b)
        s, h, b = call('DELETE', f'/api/requests/{rid}/attention')
        check('request DELETE again: 200 (idempotent)', s == 200 and b['attention'] is None, b)
        call('POST', f'/api/requests/{rid}/attention', {'message': 'req q2'})
        s, h, b = call('POST', f'/api/requests/{rid}/complete', {'message': 'bye'})
        check("request complete clears its own and every task's attention", b['attention'] is None and b['waiting'] is False
              and b['tasks_waiting'] == 0 and all(t['attention'] is None for t in b['tasks']), b)
        s, h, b = call('POST', f'/api/requests/{rid}/attention', {'message': 'late'})
        check('request attention on a closed request: 409 with the hint', s == 409
              and b == {'error': f'request {rid} is already done', 'status': 'done', 'hint': 'the request is closed'}, b)
        s, h, b = call('DELETE', f'/api/requests/{rid}/attention')
        check('request DELETE on a closed request: 200', s == 200, b)
        s, h, b = call('POST', f'/api/requests/{rid}/attention', {})
        check('request attention without a message: 400', s == 400 and b.get('field') == 'message', b)
        evs = srv.events(c3)['events']
        check('request attention events: the cascade clears (tasks by seq, then the request), then request_done',
              kinds(evs) == [('attention', None), ('task_started', a2), ('attention', a2), ('attention_cleared', None), ('attention', None),
                             ('attention_cleared', a2), ('attention_cleared', None), ('request_done', None)], kinds(evs))
        e = next(x for x in evs if x['type'] == 'attention_cleared' and x['task_id'] == a2)
        check("the cascade's attention_cleared carries the closed task and its question", e['status'] == 'done' and e['message'] == 'task q', e)
        check('a request-level attention event: task_id null, the question', evs[0]['task_id'] is None and evs[0]['message'] == 'req q'
              and evs[0]['status'] == 'running', evs[0])
        check('nothing waits on the board now', srv.events(None)['waiting'] == [] and call('GET', '/api/requests')[2]['counts']['waiting'] == 0)

        S.section('/api/events: baseline, limit, truncated, pruned')
        s, h, b = call('GET', '/api/events')
        check('baseline: events [], cursor = the last id, keys in order', s == 200 and b['events'] == [] and b['truncated'] is False
              and b['cursor'] > 0 and list(b) == ['now', 'cursor', 'events', 'truncated', 'waiting', 'stale', 'settings_version', 'stream', 'clock'], b)
        top = b['cursor']
        s, h, b = call('GET', f'/api/events?since={top - 5}&limit=2')
        check('limit 2: two events, truncated, cursor = the last returned', [x['id'] for x in b['events']] == [top - 4, top - 3]
              and b['truncated'] is True and b['cursor'] == top - 3, b)
        s, h, b = call('GET', f'/api/events?since={b["cursor"]}&limit=3')
        check('the rest exactly: not truncated, cursor = the last id', [x['id'] for x in b['events']] == [top - 2, top - 1, top]
              and b['truncated'] is False and b['cursor'] == top, b)
        s, h, b = call('GET', f'/api/events?since={top}')
        check('nothing new: [], cursor unchanged', b['events'] == [] and b['cursor'] == top and b['truncated'] is False, b)
        s, h, b = call('GET', f'/api/events?since={top + 100}')
        check('since beyond the last id: [], cursor = the last id', b['events'] == [] and b['cursor'] == top, b)
        s, h, b = call('GET', '/api/events?since=0&limit=1000')
        check('since=0: every event, the default and max limit apply', len(b['events']) == top and b['truncated'] is False, len(b['events']))
        for query, field in (('since=abc', 'since'), ('since=-1', 'since'), ('since=1.5', 'since'), ('since=99999999999999999999', 'since'),
                             ('limit=x', 'limit')):
            s, h, b = call('GET', f'/api/events?{query}')
            check(f'{query}: 400 field {field}', s == 400 and b.get('field') == field, b)
        for query, warning in (('limit=5000', 'limit 5000 clamped to 1..1000'), ('limit=0', 'limit 0 clamped to 1..1000')):
            s, h, b = call('GET', f'/api/events?since=0&{query}')
            check(f'{query}: clamped with a warning', s == 200 and b.get('warnings') == [warning], b.get('warnings'))
        check('limit=0 clamps to 1', len(call('GET', '/api/events?since=0&limit=0')[2]['events']) == 1)
        s, h, b = call('POST', '/api/events', {})
        check('POST /api/events: 405', s == 405 and h.get('allow') == 'GET', (s, h.get('allow')))
        with sqlite3.connect(srv.db) as db:
            db.execute('DELETE FROM events WHERE id <= 10')
        s, h, b = call('GET', '/api/events?since=5')
        check('events after since were pruned: truncated, the oldest kept first', b['truncated'] is True and b['events'][0]['id'] == 11, b['truncated'])
        s, h, b = call('GET', '/api/events?since=10')
        check('since = the first kept id - 1: not truncated', b['truncated'] is False, b['truncated'])
        with sqlite3.connect(srv.db) as db:
            db.execute('DELETE FROM events')
        s, h, b = call('GET', '/api/events')
        check('every event pruned: the cursor stays at the last id ever handed out', b['cursor'] == top, b)
        s, h, b = call('GET', f'/api/events?since={top - 3}')
        check('every event pruned, since below the last id: truncated', b['truncated'] is True and b['events'] == [] and b['cursor'] == top, b)
        call('POST', '/api/requests', {'title': 'after the prune'})
        b = srv.events(top)
        check('new ids continue after the pruned ones', [x['id'] for x in b['events']] == [top + 1] and b['truncated'] is False, b)

        S.section('replayed writes: X-Tasks-Replay: 1 and at')
        now = time.time()
        s, h, b = call('POST', '/api/requests', {'title': 'replayed', 'id': 'r2p9ay', 'tasks': ['a'], 'at': now - 600}, headers=REPLAY)
        check('a replayed create: created_at and updated_at = at, no warning', s == 201 and abs(b['created_at'] - (now - 600)) < 0.01
              and b['updated_at'] == b['created_at'] and b['tasks'][0]['created_at'] == b['created_at'] and 'warnings' not in b, b)
        s, h, b = call('POST', '/api/tasks/r2p9ay-1/progress', {'message': 'p', 'eta_seconds': 60, 'at': now - 500}, headers=REPLAY)
        check('a replayed progress: started_at and updated_at = at, the ETA counts from at', abs(b['started_at'] - (now - 500)) < 0.01
              and abs(b['updated_at'] - (now - 500)) < 0.01 and abs(b['eta_at'] - (now - 440)) < 0.01 and b['eta_remaining'] == 0, b)
        check("computed fields use the real clock (elapsed ~500s, now = the server's now)", 499 < b['elapsed'] < 505 and abs(b['now'] - time.time()) < 2, b)
        s, h, b = call('POST', '/api/tasks/r2p9ay-1/progress', {'message': 'older', 'at': now - 550}, headers=REPLAY)
        check("an at before the task's updated_at is raised to it (silently)", abs(b['updated_at'] - (now - 500)) < 0.01
              and b['message'] == 'older' and 'warnings' not in b, b)
        s, h, b = call('POST', '/api/tasks/r2p9ay-1/attention', {'message': 'q', 'at': now - 400}, headers=REPLAY)
        check('a replayed attention: since = at', abs(b['attention']['since'] - (now - 400)) < 0.01, b)
        s, h, b = call('DELETE', '/api/tasks/r2p9ay-1/attention', {'at': now - 300}, headers=REPLAY)
        check('a replayed resume: cleared at at', b['attention'] is None and abs(b['updated_at'] - (now - 300)) < 0.01, b)
        s, h, b = call('POST', '/api/tasks/r2p9ay-1/complete', {'at': now - 200}, headers=REPLAY)
        check('a replayed task complete: completed_at = at', abs(b['completed_at'] - (now - 200)) < 0.01, b)
        call('POST', '/api/requests/r2p9ay/attention', {'message': 'rq', 'at': now - 150}, headers=REPLAY)
        call('DELETE', '/api/requests/r2p9ay/attention', {'at': now - 120}, headers=REPLAY)
        s, h, b = call('POST', '/api/requests/r2p9ay/complete', {'at': now - 100}, headers=REPLAY)
        check('a replayed request complete: completed_at = updated_at = at', abs(b['completed_at'] - (now - 100)) < 0.01
              and abs(b['updated_at'] - (now - 100)) < 0.01, b)
        evs = [x for x in srv.events(0)['events'] if x['request_id'] == 'r2p9ay']
        check('replayed events: replayed true, ts = the effective time', all(x['replayed'] is True for x in evs)
              and [(x['type'], round(now - x['ts'])) for x in evs] == [
                  ('request_created', 600), ('task_started', 500), ('task_progress', 500), ('attention', 400), ('attention_cleared', 300),
                  ('task_done', 200), ('attention', 150), ('attention_cleared', 120), ('request_done', 100)],
              [(x['type'], round(now - x['ts'])) for x in evs])
        s, h, b = call('POST', '/api/requests', {'title': 'replayed', 'id': 'r2p9ay', 'at': now}, headers=REPLAY)
        check('the replayed create once more: 200 existing, unchanged', s == 200 and b['existing'] is True and b['status'] == 'done', b)
        s, h, b = call('POST', '/api/requests/r2p9ay/complete', {'at': now - 50}, headers=REPLAY)
        check("a replayed request complete never moves updated_at back", abs(b['updated_at'] - (now - 100)) < 0.01, b)
        s, h, b = call('POST', '/api/requests', {'title': 'old', 'at': now - 999999}, headers=REPLAY)
        check('an at older than 24h: clamped to 24h ago, with a warning', abs(b['created_at'] - (b['now'] - 86400)) < 1
              and b.get('warnings') == ['at clamped to 24h ago'], b)
        s, h, b = call('POST', '/api/requests', {'title': 'future', 'at': now + 3600}, headers=REPLAY)
        check('an at in the future: clamped to now, with a warning', abs(b['created_at'] - b['now']) < 0.05
              and b['warnings'][0].startswith('at clamped to now'), b)
        s, h, b = call('POST', '/api/requests', {'title': 'bad at', 'at': 'yesterday'}, headers=REPLAY)
        check('a non-numeric at: ignored with a warning, still a replay', s == 201
              and b['warnings'] == ['at ignored: it must be a number (epoch seconds)']
              and [x for x in srv.events(0)['events'] if x['request_id'] == b['id']][0]['replayed'] is True, b)
        s, h, b = call('POST', '/api/requests', {'title': 'no at'}, headers=REPLAY)
        check('the header without at: the server time, still a replay', s == 201 and 'warnings' not in b
              and [x for x in srv.events(0)['events'] if x['request_id'] == b['id']][0]['replayed'] is True, b)
        s, h, b = call('POST', '/api/requests', {'title': 'no header', 'at': now - 600})
        check('at without the header: an unknown field, ignored', abs(b['created_at'] - b['now']) < 0.05
              and b['warnings'] == ["unknown field 'at' ignored"]
              and [x for x in srv.events(0)['events'] if x['request_id'] == b['id']][0]['replayed'] is False, b)
        s, h, b = call('POST', '/api/requests', {'title': 'header 0', 'at': now - 600}, headers={'X-Tasks-Replay': '0'})
        check('X-Tasks-Replay: 0 is not a replay', b['warnings'] == ["unknown field 'at' ignored"], b)
        rr = call('POST', '/api/requests', {'title': 'add is not replayable'})[2]
        s, h, b = call('POST', f"/api/requests/{rr['id']}/tasks", {'title': 'x', 'at': now - 600}, headers=REPLAY)
        check('adding tasks is never replayed: at is an unknown field', b['warnings'] == ["unknown field 'at' ignored"]
              and abs(b['created_at'] - b['now']) < 0.05, b)
    finally:
        code = srv.stop()
    check('api server: clean exit, no tracebacks', code == 0 and 'Traceback' not in srv.log_text(), srv.log_text()[-1500:])
    log = srv.log_text()
    check('the log notes waiting and replays', 'is waiting for input' in log and '(replayed)' in log
          and 'already exists (a replayed create)' in log, log[-1500:])


def phase_stale_expiry():
    S.section('stale and expiry exemptions for waiting work (--stale-after 1, --expire-after 3)')
    with ScratchServer(TMP, '--stale-after', '1', '--expire-after', '3', name='stale') as srv:
        call = srv.call
        quiet = call('POST', '/api/requests', {'title': 'quiet', 'tasks': ['a']})[2]['id']
        wait_t = call('POST', '/api/requests', {'title': 'a task waits', 'tasks': ['a', 'b']})[2]['id']
        wait_r = call('POST', '/api/requests', {'title': 'the request waits', 'tasks': ['a']})[2]['id']
        call('POST', f'/api/tasks/{quiet}-1/progress', {'message': 'x'})
        call('POST', f'/api/tasks/{wait_t}-1/progress', {'message': 'x'})
        call('POST', f'/api/tasks/{wait_t}-2/attention', {'message': 'need you'})
        call('POST', f'/api/tasks/{wait_r}-1/progress', {'message': 'x'})
        call('POST', f'/api/requests/{wait_r}/attention', {'message': 'need you too'})
        time.sleep(1.6)
        b = call('GET', f'/api/requests/{wait_t}')[2]
        tk = {t['seq']: t for t in b['tasks']}
        check('a request waiting through a task is never stale; the waiting task is not, its silent sibling is',
              b['stale'] is False and tk[2]['stale'] is False and tk[1]['stale'] is True, (b['stale'], tk[1]['stale'], tk[2]['stale']))
        b = call('GET', f'/api/requests/{wait_r}')[2]
        check('a request waiting on its own question is never stale (its silent task still is)', b['stale'] is False
              and b['tasks'][0]['stale'] is True, b)
        check('the quiet one is stale', call('GET', f'/api/requests/{quiet}')[2]['stale'] is True)
        ev = srv.events(None)
        check('/api/events stale: only the quiet request, {request_id, request_title}',
              ev['stale'] == [{'request_id': quiet, 'request_title': 'quiet'}], ev['stale'])
        lst = call('GET', '/api/requests')[2]
        check('counts: stale 1 (waiting excluded), waiting 2', lst['counts']['stale'] == 1 and lst['counts']['waiting'] == 2, lst['counts'])
        gone = wait_until(lambda: call('GET', f'/api/requests/{quiet}')[2]['status'] == 'failed', 10, 0.3)
        check('the quiet request expires', gone)
        time.sleep(1.2)
        check('waiting requests never expire', call('GET', f'/api/requests/{wait_t}')[2]['status'] == 'running'
              and call('GET', f'/api/requests/{wait_r}')[2]['status'] == 'running')
        e = [x for x in srv.events(0)['events'] if x['request_id'] == quiet and x['type'] == 'request_failed']
        check('expiry writes request_failed with the expiry message, no task events', len(e) == 1
              and e[0]['message'] == 'expired: no activity for 3s'
              and not [x for x in srv.events(0)['events'] if x['request_id'] == quiet and x['type'] in ('task_failed', 'task_done')], e)
        call('DELETE', f'/api/tasks/{wait_t}-2/attention')
        call('DELETE', f'/api/requests/{wait_r}/attention')
        gone = wait_until(lambda: all(call('GET', f'/api/requests/{r}')[2]['status'] == 'failed' for r in (wait_t, wait_r)), 12, 0.3)
        check('once answered and silent, they expire too', gone)


def phase_settings():
    S.section('settings: defaults, PUT, the file, hand edits, DELETE')
    cfg = TMP / 'conf' / 'global.json'
    srv = ScratchServer(TMP, '--config', str(cfg), name='settings').start()
    call = srv.call

    def version(settings):
        return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:12]

    def shape(b):
        return list(b) == ['now', 'path', 'version', 'exists', 'error', 'settings', 'defaults']
    try:
        s, h, b = call('GET', '/api/settings')
        D = b['defaults']
        check('GET: the shape, no file yet, the defaults', s == 200 and shape(b) and b['exists'] is False and b['error'] is None
              and b['path'] == str(cfg) and b['settings'] == D, b)
        check('the defaults: every KEY, in order, with its four channels', list(D['events']) == [
            'waiting', 'task_failed', 'request_failed', 'request_done', 'task_done', 'task_started', 'task_progress',
            'request_created', 'stale'] and all(list(v) == ['highlight', 'title', 'sound', 'notify'] for v in D['events'].values())
              and D['sound'] == {'volume': 0.6}, D)
        check('the defaults: events, sound and tasks, in that order; tasks are listed by number', list(D) == ['events', 'sound', 'tasks']
              and D['tasks'] == {'order': 'number'}, D)
        table = {k: ''.join('T' if v[c] else 'F' for c in ('highlight', 'title', 'sound', 'notify')) for k, v in D['events'].items()}
        check('the defaults table', table == {'waiting': 'TTTT', 'task_failed': 'TTTT', 'request_failed': 'TTTT', 'request_done': 'TTTT',
                                              'task_done': 'TFFF', 'task_started': 'TFFF', 'task_progress': 'TFFF',
                                              'request_created': 'TFFF', 'stale': 'TTFF'}, table)
        v0 = b['version']
        check('version = sha256 of the compact sorted JSON, [:12]', v0 == version(D), v0)
        check('/api/health config_path and /api/events settings_version', call('GET', '/api/health')[2]['config_path'] == str(cfg)
              and srv.events(None)['settings_version'] == v0)
        s, h, b = call('PUT', '/api/settings', {'settings': {'events': {'task_done': {'sound': True}}, 'sound': {'volume': 1}}})
        check('PUT a partial: merged onto the current, the file exists, a new version', s == 200 and shape(b) and b['exists'] is True
              and b['settings']['events']['task_done'] == {'highlight': True, 'title': False, 'sound': True, 'notify': False}
              and b['settings']['sound']['volume'] == 1.0 and b['version'] != v0 and b['version'] == version(b['settings'])
              and b['error'] is None, b)
        text = cfg.read_text()
        check('the file: the full settings, indent 2, a trailing newline', json.loads(text) == b['settings'] and text.endswith('}\n')
              and text.startswith('{\n  "events": {\n    "waiting": {\n      "highlight": true'), text[:120])
        check('the write left no temp file beside it', sorted(p.name for p in cfg.parent.iterdir()) == ['global.json'],
              sorted(p.name for p in cfg.parent.iterdir()))
        check('/api/events settings_version follows', srv.events(None)['settings_version'] == b['version'])
        s, h, b = call('PUT', '/api/settings', {'settings': {'events': {'waiting': {'notify': False}}}})
        check('a second PUT merges onto the saved settings', b['settings']['events']['task_done']['sound'] is True
              and b['settings']['events']['waiting']['notify'] is False and b['settings']['sound']['volume'] == 1.0, b)
        before = b['version']
        s, h, b = call('PUT', '/api/settings', {'settings': {'tasks': {'order': 'progress'}}})
        check('PUT tasks.order progress: saved, the rest kept, a new version', s == 200 and b['settings']['tasks'] == {'order': 'progress'}
              and b['settings']['events']['waiting']['notify'] is False and b['settings']['sound']['volume'] == 1.0
              and b['version'] not in (before, v0) and json.loads(cfg.read_text())['tasks'] == {'order': 'progress'}, b)
        check('/api/events settings_version follows the task order', srv.events(None)['settings_version'] == b['version'])
        rid = call('POST', '/api/requests', {'title': 'order', 'tasks': ['a', 'b', 'c']})[2]['id']
        call('POST', f'/api/tasks/{rid}-3/progress', {'message': 'going', 'percent': 50})
        call('POST', f'/api/tasks/{rid}-1/complete', {})
        check('the API still lists a request\'s tasks by number (the order is the dashboard\'s)',
              [t['seq'] for t in call('GET', f'/api/requests/{rid}')[2]['tasks']] == [1, 2, 3])
        call('DELETE', f'/api/requests/{rid}')
        s, h, b = call('PUT', '/api/settings', {'settings': {'tasks': {'order': 'number'}}})
        check('PUT tasks.order number: back again', s == 200 and b['settings']['tasks'] == {'order': 'number'}, b)
        for body, field in (({'settings': {'events': {'bogus': {'sound': True}}}}, 'settings.events.bogus'),
                            ({'settings': {'events': {'task_done': {'flash': True}}}}, 'settings.events.task_done.flash'),
                            ({'settings': {'events': {'task_done': {'sound': 1}}}}, 'settings.events.task_done.sound'),
                            ({'settings': {'events': {'task_done': {'sound': 'true'}}}}, 'settings.events.task_done.sound'),
                            ({'settings': {'events': {'task_done': {'sound': None}}}}, 'settings.events.task_done.sound'),
                            ({'settings': {'events': {'task_done': []}}}, 'settings.events.task_done'),
                            ({'settings': {'sound': {'volume': 1.5}}}, 'settings.sound.volume'),
                            ({'settings': {'sound': {'volume': -0.1}}}, 'settings.sound.volume'),
                            ({'settings': {'sound': {'volume': True}}}, 'settings.sound.volume'),
                            ({'settings': {'sound': {'volume': '0.5'}}}, 'settings.sound.volume'),
                            ({'settings': {'sound': {'loud': 1}}}, 'settings.sound.loud'),
                            ({'settings': {'tasks': {'order': 'seq'}}}, 'settings.tasks.order'),
                            ({'settings': {'tasks': {'order': 'Progress'}}}, 'settings.tasks.order'),
                            ({'settings': {'tasks': {'order': None}}}, 'settings.tasks.order'),
                            ({'settings': {'tasks': {'order': ['number']}}}, 'settings.tasks.order'),
                            ({'settings': {'tasks': {'order': 1}}}, 'settings.tasks.order'),
                            ({'settings': {'tasks': {'sort': 'number'}}}, 'settings.tasks.sort'),
                            ({'settings': {'tasks': 'progress'}}, 'settings.tasks'),
                            ({'settings': {'colour': {}}}, 'settings.colour'), ({'settings': {'events': []}}, 'settings.events'),
                            ({'settings': 'x'}, 'settings'), ({}, 'settings')):
            s, h, b = call('PUT', '/api/settings', body)
            check(f'PUT {json.dumps(body)[:70]}: 400 field {field}', s == 400 and b.get('field') == field, b)
        s, h, b = call('PUT', '/api/settings', {'settings': {'events': {'waitng': {}}}})
        check('an unknown KEY: a did-you-mean hint', s == 400 and b.get('hint') == "did you mean 'waiting'?", b)
        s, h, b = call('PUT', '/api/settings', {'settings': {'tasks': {'order': 'seq'}}})
        check('an unknown task order: the error names the three orders', s == 400 and b['error'] == 'settings.tasks.order must be one of: number, progress, updated', b)
        s, h, b = call('PUT', '/api/settings', {'settings': {'tasks': {'order': 'updated'}}})
        check('PUT tasks.order updated: saved', s == 200 and b['settings']['tasks'] == {'order': 'updated'}, b)
        s, h, b = call('PUT', '/api/settings', {'settings': {'task': {'order': 'progress'}}})
        check('a misspelt tasks: a did-you-mean hint', s == 400 and b.get('field') == 'settings.task' and b.get('hint') == "did you mean 'tasks'?", b)
        s, h, b = call('PUT', '/api/settings', raw=b'{"settings": {"sound": {"volume": NaN}}}')
        check('volume NaN: 400', s == 400 and b.get('field') == 'settings.sound.volume', b)
        check('no invalid PUT changed the file', json.loads(cfg.read_text())['events']['waiting']['notify'] is False
              and json.loads(cfg.read_text())['sound']['volume'] == 1.0)
        s, h, b = call('PUT', '/api/settings', {'settings': {}, 'extra': 1})
        check('an empty partial: 200; an unknown top-level field: a warning', s == 200 and b.get('warnings') == ["unknown field 'extra' ignored"], b)
        s, h, b = call('PUT', '/api/settings', {'settings': {}}, headers=EVIL)
        check('PUT with a foreign Origin: 403', s == 403, b)
        s, h, b = call('DELETE', '/api/settings', headers=EVIL)
        check('DELETE with a foreign Origin: 403, the file kept', s == 403 and cfg.exists(), b)
        s, h, b = call('PUT', '/api/settings', {'settings': {'sound': {'volume': 0.5}}},
                       headers={'Origin': f'http://127.0.0.1:{srv.port}'})
        check('PUT same-origin (the page): 200', s == 200 and b['settings']['sound']['volume'] == 0.5, b)
        s, h, b = call('POST', '/api/settings', {})
        check('POST /api/settings: 405 Allow GET, PUT, DELETE', s == 405 and h.get('allow') == 'GET, PUT, DELETE', h.get('allow'))

        S.section('settings: hand edits of the file are picked up')
        cfg.write_text('{"events": {"task_started": {"sound": true}}}\n')
        s, h, b = call('GET', '/api/settings')
        check('a valid partial file: merged onto the defaults (not onto what was saved before)', b['error'] is None and b['exists'] is True
              and b['settings']['events']['task_started']['sound'] is True and b['settings']['events']['waiting']['notify'] is True
              and b['settings']['events']['task_done']['sound'] is False and b['settings']['sound']['volume'] == 0.6, b)
        check('/api/events settings_version follows a hand edit', srv.events(None)['settings_version'] == b['version'])
        check('a file without tasks (as an older board wrote it): the default task order', b['settings']['tasks'] == {'order': 'number'}, b)
        cfg.write_text('{"tasks": {"order": "progress"}}\n')
        s, h, b = call('GET', '/api/settings')
        check('a hand-edited task order is picked up, the alerts keep their defaults', b['error'] is None and b['settings']['tasks'] == {'order': 'progress'}
              and b['settings']['events'] == D['events'] and srv.events(None)['settings_version'] == b['version'], b)
        cfg.write_text('{"tasks": {"order": "random"}, "sound": {"volume": 0.1}}\n')
        s, h, b = call('GET', '/api/settings')
        check('an unknown order in the file: the whole file ignored (the defaults), the error names the field', b['settings'] == D
              and 'tasks.order must be one of: number, progress, updated' in b['error'], b)
        cfg.write_text('{"events": ')
        s, h, b = call('GET', '/api/settings')
        check('invalid JSON: the defaults, error says invalid JSON, exists true', b['settings'] == D and b['exists'] is True
              and b['error'].startswith('invalid JSON') and b['version'] == v0, b)
        check('/api/events settings_version = the defaults\'', srv.events(None)['settings_version'] == v0)
        cfg.write_text('{"events": {"task_done": {"sound": "yes"}, "task_started": {"sound": true}}}')
        s, h, b = call('GET', '/api/settings')
        check('a schema error: the whole file ignored (the defaults), the error names the field', b['settings'] == D
              and 'events.task_done.sound must be true or false' in b['error'], b)
        cfg.write_text('[1, 2]')
        s, h, b = call('GET', '/api/settings')
        check('not an object: the defaults and an error', b['settings'] == D and 'must be a JSON object' in b['error'], b)
        for _ in range(3):
            call('GET', '/api/settings')
            srv.events(None)
        s, h, b = call('PUT', '/api/settings', {'settings': {'sound': {'volume': 0.2}}})
        check('PUT over a broken file: allowed, overwrites it (onto the defaults)', s == 200 and b['error'] is None
              and b['settings']['sound']['volume'] == 0.2 and b['settings']['events']['task_done']['sound'] is False
              and json.loads(cfg.read_text()) == b['settings'], b)
        s, h, b = call('DELETE', '/api/settings')
        check('DELETE: the file removed, the defaults again', s == 200 and shape(b) and not cfg.exists() and b['exists'] is False
              and b['settings'] == D and b['version'] == v0 and b['error'] is None, b)
        s, h, b = call('DELETE', '/api/settings')
        check('DELETE again: 200 (idempotent)', s == 200 and b['exists'] is False, b)
        cfg.parent.chmod(0o500)
        try:
            s, h, b = call('PUT', '/api/settings', {'settings': {'sound': {'volume': 0.3}}})
        finally:
            cfg.parent.chmod(0o700)
        check('PUT into an unwritable directory: 500 cannot write, nothing left behind', s == 500
              and b['error'].startswith(f'cannot write {cfg}') and not cfg.exists(), b)
    finally:
        srv.stop()
    log = srv.log_text()
    check('each broken version of the file is logged once', log.count('using the defaults') == 4,
          [line for line in log.splitlines() if 'settings' in line])
    check('saves, hand edits and resets are logged', 'settings saved to' in log and 'changed; reloaded' in log and 'settings reset' in log)
    check('the listening line names the settings file', f'settings {cfg})' in log, log[:600])

    S.section('settings: the default path, TASKS_CONFIG, --config, a broken file at startup')
    (TMP / 'defcfg').mkdir()
    with ScratchServer(TMP / 'defcfg', name='defcfg', own_config=False) as srv:
        b = srv.call('GET', '/api/settings')[2]
        check('the default is <db dir>/settings.json', b['path'] == str(TMP / 'defcfg' / 'settings.json') and b['exists'] is False, b)
        srv.call('PUT', '/api/settings', {'settings': {'sound': {'volume': 0.1}}})
        check('...and a PUT writes it there', json.loads((TMP / 'defcfg' / 'settings.json').read_text())['sound']['volume'] == 0.1)
    envcfg = TMP / 'env.json'
    envcfg.write_text('[]')
    with ScratchServer(TMP, name='envcfg', own_config=False, env=clean_env(TASKS_CONFIG=envcfg)) as srv:
        b = srv.call('GET', '/api/settings')[2]
        check('TASKS_CONFIG names the file', b['path'] == str(envcfg) and b['error'] and 'must be a JSON object' in b['error'], b)
        check('a broken file is reported at startup', 'settings file' in srv.log_text() and 'using the defaults' in srv.log_text(),
              srv.log_text()[-600:])
    flagcfg = TMP / 'flag.json'
    flagcfg.write_text('{"sound": {"volume": 0.25}}')
    with ScratchServer(TMP, '--config', str(flagcfg), name='flagcfg', env=clean_env(TASKS_CONFIG=envcfg)) as srv:
        b = srv.call('GET', '/api/settings')[2]
        check('--config beats TASKS_CONFIG, a valid file applies', b['path'] == str(flagcfg) and b['error'] is None
              and b['settings']['sound']['volume'] == 0.25, b)
    rel = TMP / 'relcwd'
    rel.mkdir()
    port = free_port()
    proc = subprocess.Popen([sys.executable, str(SERVER), '--port', str(port), '--db', str(TMP / 'rel.db'), '--pidfile', str(TMP / 'rel.pid'),
                             '--config', 'sub/rel.json'], cwd=rel, env=clean_env(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ok = wait_until(lambda: connects('127.0.0.1', port, 0.5), 10)
        b = http_call(port, 'GET', '/api/settings')[2] if ok else {}
        check('a relative --config resolves against the cwd, absolute in the reply', b.get('path') == str(rel / 'sub' / 'rel.json'), b)
    finally:
        proc.terminate()
        proc.wait(10)

    S.section('settings: a volume too large for a float (a JSON integer of 401 digits)')
    huge = '1' + '0' * 400
    bigcfg = TMP / 'big.json'
    bigcfg.write_text('{"sound": {"volume": %s}}' % huge)
    with ScratchServer(TMP, '--config', str(bigcfg), name='bigcfg') as srv:
        s, h, b = srv.call('GET', '/api/settings')
        check('in the file at startup: the server starts, the defaults, an error naming the field', s == 200
              and b['exists'] is True and b['settings'] == b['defaults'] and 'sound.volume must be a number from 0 to 1' in (b['error'] or ''), b)
        check('...and /api/events answers', srv.call('GET', '/api/events')[0] == 200)
        s, h, b = srv.call('PUT', '/api/settings', raw=b'{"settings": {"sound": {"volume": %s}}}' % huge.encode())
        check('PUT it: 400 with the field, not a 500', s == 400 and b.get('field') == 'settings.sound.volume', (s, b))
        s, h, b = srv.call('PUT', '/api/settings', {'settings': {'sound': {'volume': 0.4}}})
        check('a valid PUT replaces the file', s == 200 and b['error'] is None and b['settings']['sound']['volume'] == 0.4, b)
        time.sleep(0.05)
        bigcfg.write_text('{"sound": {"volume": %s}, "events": {}}' % huge)
        s, h, b = srv.call('GET', '/api/events')
        check('hand-edited to it while running: /api/events still 200', s == 200, (s, b))
        s, h, b = srv.call('GET', '/api/settings')
        check('...and the settings are the defaults with an error, not the previous values', s == 200 and b['exists'] is True
              and b['settings'] == b['defaults'] and 'sound.volume' in (b['error'] or ''), b)
    check('the bigcfg board logged no traceback', 'Traceback' not in srv.log_text(), srv.log_text()[-1500:])


def phase_prune():
    S.section('event pruning by the maintenance thread (--expire-after 2: every second)')
    with ScratchServer(TMP, '--expire-after', '2', name='prune') as srv:
        srv.call('POST', '/api/requests', {'title': 'fresh'})
        now = time.time()
        with sqlite3.connect(srv.db) as db:
            db.executemany("INSERT INTO events (ts, type, request_id) VALUES (?, 'task_progress', 'aaaaaa')",
                           [(now - 8 * 86400,)] * 5 + [(now - 3600,)] * 10050)

        def count():
            with sqlite3.connect(srv.db) as db:
                return db.execute('SELECT COUNT(*) FROM events').fetchone()[0]
        ok = wait_until(lambda: count() == 10000, 10, 0.3)
        with sqlite3.connect(srv.db) as db:
            n, old, first, newest = db.execute('SELECT COUNT(*), SUM(ts < ?), MIN(id), MAX(id) FROM events', (now - 7 * 86400,)).fetchone()
        check('older than 7 days deleted, then all but the newest 10000', ok and n == 10000 and not old and first == newest - 9999,
              (n, old, first, newest))
        b = srv.call('GET', '/api/events?since=0&limit=1')[2]
        check('a cursor from before the prune: truncated, continues at the oldest kept', b['truncated'] is True
              and b['events'][0]['id'] == newest - 9999, b['events'])
        check('the baseline cursor is the newest', srv.events(None)['cursor'] == newest)
        time.sleep(1.5)
        check('the next passes keep 10000', count() == 10000, count())


def phase_clock():
    S.section("/api/events: the board host's clock (its time zone), for the dashboard's header")

    def zone_now(zone):
        # what this machine's C library says for the zone right now (the test process takes it on for a moment)
        old = os.environ.get('TZ')
        os.environ['TZ'] = zone
        time.tzset()
        try:
            local = time.localtime()
            return {'utc_offset': local.tm_gmtoff, 'tz': local.tm_zone}
        finally:
            if old is None:
                os.environ.pop('TZ', None)
            else:
                os.environ['TZ'] = old
            time.tzset()

    # POSIX zone strings need no tz database; the three fixed ones are checked against literals (east is positive)
    fixed = (('UTC0', {'utc_offset': 0, 'tz': 'UTC'}), ('IST-5:30', {'utc_offset': 19800, 'tz': 'IST'}), ('EST5', {'utc_offset': -18000, 'tz': 'EST'}))
    rules = ('NST3:30NDT,M3.2.0,M11.1.0', 'CET-1CEST,M3.5.0,M10.5.0/3')  # daylight saving: whichever applies today
    for zone, want in fixed + tuple((z, zone_now(z)) for z in rules):
        with ScratchServer(TMP, name='clock', env=clean_env(TZ=zone)) as srv:
            s, h, b = srv.call('GET', '/api/events')
            check(f'TZ={zone}: clock is {want}', s == 200 and b['clock'] == want, b.get('clock'))
            check(f'TZ={zone}: the offset is a whole number of seconds and the zone name a short string', type(b['clock']['utc_offset']) is int
                  and isinstance(b['clock']['tz'], str) and 0 < len(b['clock']['tz']) <= 16, b['clock'])
            check(f'TZ={zone}: the board\'s local time is now + utc_offset', abs((b['now'] + b['clock']['utc_offset']) - (time.time() + want['utc_offset'])) < 5, b)
            s, h, b2 = srv.call('GET', f'/api/events?since={b["cursor"]}&limit=1')
            check(f'TZ={zone}: every events reply carries it, a cursor one too, as the last key', s == 200 and b2['clock'] == want and list(b2)[-1] == 'clock', list(b2))
        check(f'TZ={zone}: no tracebacks', 'Traceback' not in srv.log_text(), srv.log_text()[-800:])


# ---------------------------------------------------------------- migration of a real v1 database

def schema_of(path: Path):
    with sqlite3.connect(path) as db:
        tables = {t: [(r[1], r[2], r[3], r[4]) for r in db.execute(f'PRAGMA table_info({t})')] for t in ('requests', 'tasks', 'events', 'reports')}
        indexes = sorted(r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'index' AND name NOT LIKE 'sqlite_%'"))
        version = db.execute('PRAGMA user_version').fetchone()[0]
    return tables, indexes, version


def v1_server_copy() -> Path | None:
    """tasks/server.py as released in v1, from git, in a directory of its own (it needs no templates)."""
    if not shutil.which('git'):
        return None
    res = subprocess.run(['git', '-C', str(REPO), 'show', f'{V1_COMMIT}:tasks/server.py'], capture_output=True, timeout=30)
    if res.returncode != 0 or b"VERSION = '1'" not in res.stdout:
        return None
    path = TMP / 'v1' / 'server.py'
    path.parent.mkdir()
    path.write_bytes(res.stdout)
    return path


def phase_migration():
    S.section(f'migration: a database made by the v1 server ({V1_COMMIT}) upgrades in place')
    v1 = v1_server_copy()
    if v1 is None:
        S.skip('the v1 migration checks', f'git or commit {V1_COMMIT} is not available in this checkout')
        return
    old = ScratchServer(TMP, '--stale-after', '600', script=v1, name='v1db', own_config=False)
    with old:
        call = old.call
        check('the v1 server answers version 1', call('GET', '/api/health')[2]['version'] == '1')
        r = call('POST', '/api/requests', {'title': 'still running', 'tasks': ['build', 'test', 'ship']})[2]
        run_id = r['id']
        call('POST', f'/api/tasks/{run_id}-1/progress', {'message': 'compiling', 'percent': 40, 'eta_seconds': 600})
        call('POST', f'/api/tasks/{run_id}-2/complete', {'message': 'green'})
        r = call('POST', '/api/requests', {'title': 'finished', 'tasks': ['only']})[2]
        done_id = r['id']
        call('POST', f'/api/tasks/{done_id}-1/progress', {'message': 'x'})
        call('POST', f'/api/requests/{done_id}/complete', {'message': 'finished'})
        r = call('POST', '/api/requests', {'title': 'gave up', 'tasks': ['a', 'b']})[2]
        fail_id = r['id']
        call('POST', f'/api/tasks/{fail_id}-1/progress', {'message': 'x', 'percent': 30})
        call('POST', f'/api/requests/{fail_id}/complete', {'status': 'failed', 'message': 'no luck'})
        before = {rid: call('GET', f'/api/requests/{rid}')[2] for rid in (run_id, done_id, fail_id)}
    db = old.db
    with sqlite3.connect(db) as conn:
        check('the v1 database is at user_version 1, with no events table', conn.execute('PRAGMA user_version').fetchone()[0] == 1
              and not conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'events'").fetchone())
        rows_before = {t: conn.execute(f'SELECT * FROM {t} ORDER BY id').fetchall() for t in ('requests', 'tasks')}
    keep = TMP / 'v1-pristine.db'
    shutil.copy2(db, keep)

    new = ScratchServer(TMP, name='v1db', fresh_db=False)
    with new:
        call = new.call
        log = new.log_text()
        check('the upgrade is logged', f'database {db} upgraded from schema 1 to 3' in log, log[-800:])
        for rid, old_body in before.items():
            b = call('GET', f'/api/requests/{rid}')[2]
            # (url: another port; elapsed, eta_remaining: the clock moved on)
            same = all(b[k] == old_body[k] for k in old_body if k not in ('now', 'elapsed', 'tasks', 'stale', 'url'))
            same_tasks = all(all(t[k] == o[k] for k in o if k not in ('elapsed', 'eta_remaining', 'stale', 'url'))
                             for t, o in zip(b['tasks'], old_body['tasks'], strict=True))
            check(f"v1 request {old_body['title']!r}: every v1 field as it was, the v2 fields empty", same and same_tasks
                  and b['attention'] is None and b['waiting'] is False and b['tasks_waiting'] == 0 and b['report'] is None
                  and all(t['attention'] is None for t in b['tasks']), (b, old_body))
        ev = new.events(None)
        check('the migrated database has an empty event log', ev['cursor'] == 0 and ev['waiting'] == [] and new.events(0)['events'] == [], ev)
        s, h, b = call('POST', f'/api/tasks/{run_id}-3/attention', {'message': 'which one?'})
        check('attention works on a migrated task', s == 200 and b['status'] == 'running' and b['attention']['message'] == 'which one?', b)
        check('events are written on the migrated database', kinds(new.events(0)['events']) == [('task_started', f'{run_id}-3'),
                                                                                                ('attention', f'{run_id}-3')])
        check('a migrated request reopens like any other', call('POST', f'/api/requests/{done_id}/tasks', {'title': 'more'})[2]['reopened'] is True)
    tables, indexes, version = schema_of(db)
    check('user_version 3, with a reports table', version == 3 and tables['reports'] != [], (version, tables.get('reports')))
    with sqlite3.connect(db) as conn:
        cols = {t: [r[1] for r in conn.execute(f'PRAGMA table_info({t})')] for t in ('requests', 'tasks')}
        check('the attention columns were added at the end', cols['requests'][-2:] == ['attention_message', 'attention_at']
              and cols['tasks'][-2:] == ['attention_message', 'attention_at'], cols)
        untouched = [tuple(r) for r in conn.execute('SELECT * FROM requests WHERE id = ?', (fail_id,))]
        check('an untouched v1 row: byte for byte, plus two nulls', untouched == [tuple(r) + (None, None) for r in rows_before['requests']
                                                                                if r[0] == fail_id], untouched)
    with ScratchServer(TMP, name='v1db', fresh_db=False) as again:
        check('a second start: no upgrade, the events kept', 'upgraded' not in again.log_text() and len(again.events(0)['events']) == 5,
              again.log_text()[-600:])
    fresh = ScratchServer(TMP, name='freshdb').start()
    fresh.stop()
    check('a fresh database has exactly the upgraded schema and indexes', schema_of(fresh.db) == (tables, indexes, 3)
          and 'upgraded' not in fresh.log_text(), (schema_of(fresh.db), (tables, indexes)))

    partial = TMP / 'partial.db'
    shutil.copy2(keep, partial)
    with sqlite3.connect(partial) as conn:
        conn.execute('ALTER TABLE requests ADD COLUMN attention_message TEXT')
    with ScratchServer(TMP, name='partial', fresh_db=False) as srv:
        pass
    check('a half-upgraded v1 database finishes the upgrade', schema_of(srv.db) == (tables, indexes, 3), schema_of(srv.db))
    v2db = TMP / 'v2.db'     # a v2 database (the schema before reports): the reports table added, the rest kept
    shutil.copy2(fresh.db, v2db)
    with sqlite3.connect(v2db) as conn:
        conn.execute('DROP TABLE reports')
        conn.execute('PRAGMA user_version=2')
    with ScratchServer(TMP, name='v2', fresh_db=False) as srv2:
        upgraded = f'database {srv2.db} upgraded from schema 2 to 3' in srv2.log_text()
        rid2 = srv2.call('POST', '/api/requests', {'title': 'on a v2 database'})[2]['id']
        put2 = srv2.call('PUT', f'/api/requests/{rid2}/report', {'format': 'markdown', 'content': '# Done'})
    check('a v2 database upgrades to 3 (logged), and takes a report', upgraded and put2[0] == 201
          and schema_of(srv2.db) == (tables, indexes, 3), (srv2.log_text()[-400:], put2))
    newer = TMP / 'newer.db'
    shutil.copy2(fresh.db, newer)
    with sqlite3.connect(newer) as conn:
        conn.execute('PRAGMA user_version = 4')
    res = subprocess.run([sys.executable, str(SERVER), '--port', str(free_port()), '--db', str(newer), '--pidfile', str(TMP / 'newer.pid'),
                          '--config', str(TMP / 'newer.json')], capture_output=True, text=True, timeout=15, env=clean_env())
    check('a newer schema (4) is refused: exit 2, a clear error, the database untouched', res.returncode == 2
          and 'schema version 4 is newer than this server supports (3)' in res.stderr
          and schema_of(newer)[2] == 4, (res.returncode, res.stderr[-300:]))
    res = subprocess.run([sys.executable, str(v1), '--port', str(free_port()), '--db', str(fresh.db), '--pidfile', str(TMP / 'down.pid')],
                         capture_output=True, text=True, timeout=15, env=clean_env())
    check('the v1 server refuses a v3 database (no silent downgrade)', res.returncode == 2 and 'newer than this server supports' in res.stderr,
          (res.returncode, res.stderr[-300:]))


if __name__ == '__main__':
    phase_headers()
    phase_api()
    phase_stale_expiry()
    phase_settings()
    phase_prune()
    phase_clock()
    phase_migration()
    phase_reports()
    phase_report_port()
    sys.exit(S.finish())
