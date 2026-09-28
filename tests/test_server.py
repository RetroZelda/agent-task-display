#!/usr/bin/env python3
"""Server contract: every route, shape, validation rule, state rule and error of tasks/server.py.

Phases: the API against the real server.py; rendering against a copy of server.py next to fixture
templates; every file route with its file missing; --public-url with retention; argument errors.
"""
from __future__ import annotations

import json
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the checkout, even when run without run_all.sh
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from harness import (FIXTURES, SERVER, TASKS, ScratchServer, Suite, free_port, scratch_home,  # noqa: E402
                     tempdir, wait_until)

S = Suite('server')
check = S.check
TMP = tempdir('server')
scratch_home(TMP)

TASK_KEYS = ['id', 'request_id', 'seq', 'title', 'status', 'percent', 'message', 'eta_at', 'eta_remaining',
             'created_at', 'started_at', 'start_inferred', 'updated_at', 'completed_at', 'elapsed', 'stale', 'url']
REQ_KEYS = ['id', 'title', 'origin', 'status', 'message', 'created_at', 'updated_at', 'completed_at', 'elapsed',
            'percent', 'tasks_total', 'tasks_done', 'tasks_failed', 'tasks_running', 'tasks_pending',
            'tasks_cancelled', 'current', 'eta_at', 'stale', 'url']
CRAFTED = 'x";rm -rf'
FILE_ROUTES = (('/', 'static/index.html', 'text/html,*/*'), ('/', 'templates/usage.md', '*/*'),
               ('/api', 'templates/usage.md', None), ('/api/usage', 'templates/usage.md', None),
               ('/api/rule', 'templates/rule.md', None), ('/r/abc', 'static/index.html', None),
               ('/api/skill/SKILL.md', 'skill/SKILL.md', None), ('/api/skill/taskctl', 'taskctl', None),
               ('/api/skill/taskctl.py', 'taskctl.py', None))


def phase_api():
    S.section('API against the real server.py (--stale-after 2, --expire-after 4)')
    srv = ScratchServer(TMP, '--stale-after', '2', '--expire-after', '4', name='api').start()
    P = srv.port
    call = srv.call

    def new_request(title='req', tasks=None, **kw):
        status, _, body = call('POST', '/api/requests', {'title': title, 'tasks': tasks or [], **kw})
        assert status == 201, (status, body)
        return body

    def progress(tid, **body):
        return call('POST', f'/api/tasks/{tid}/progress', body)

    def complete(kind, id_, **body):
        return call('POST', f'/api/{kind}/{id_}/complete', body)

    try:
        check('pidfile holds pid', srv.pidfile.read_text().strip() == str(srv.proc.pid))
        s, h, b = call('GET', '/api/health')
        check('health', s == 200 and b['ok'] is True and b['service'] == 'tasks' and b['version'] == '1'
              and b['pid'] == srv.proc.pid and 'now' in b and 'started_at' in b and b['requests_running'] == 0, b)
        check('json headers', h.get('x-content-type-options') == 'nosniff' and h.get('cache-control') == 'no-store'
              and 'content-length' in h, h)
        s, h, b = call('HEAD', '/api/health')
        check('HEAD health: 200, no body, Content-Length', s == 200 and b == '' and int(h['content-length']) > 0, (s, b))
        s, h, b = call('GET', '/favicon.ico')
        check('favicon 204', s == 204 and h.get('content-length') == '0' and h.get('x-content-type-options') == 'nosniff', (s, h))

        s, h, b = call('GET', '/api/skill/taskctl.py', host=CRAFTED)
        check('real taskctl.py: crafted host not baked', s == 200 and 'rm -rf' not in b
              and f'BAKED_URL = "http://127.0.0.1:{P}"' in b, b[:200])
        s, h, b = call('GET', '/api/skill/taskctl.py')
        check('real taskctl.py baked exactly once', s == 200 and b.count(f'BAKED_URL = "http://127.0.0.1:{P}"') == 1
              and '{{BASE_URL}}' not in b, s)
        s, h, b = call('GET', '/nope')
        check('route 404 hint', s == 404 and b['hint'] == f'GET http://127.0.0.1:{P}/api/usage', b)
        s, h, b = call('PUT', '/api/requests', {})
        check('405 Allow on /api/requests', s == 405 and h.get('allow') == 'GET, POST', (s, h.get('allow'), b))
        s, h, b = call('POST', '/api/requests/abcdef', {})
        check('405 Allow on /api/requests/<rid>', s == 405 and h.get('allow') == 'GET, DELETE', (s, h.get('allow')))

        S.section('create + validation')
        r = new_request('  Build\t\tthe\nthing\x00 now ', tasks=['a', ' b ', 'c'], origin='host:repo', precent=4)
        check('request keys, in order', list(r)[:len(REQ_KEYS)] == REQ_KEYS and 'tasks' in r and 'now' in r, list(r))
        check('title cleaned', r['title'] == 'Build the thing now', repr(r['title']))
        check('create aggregates', r['origin'] == 'host:repo' and r['status'] == 'running' and r['tasks_total'] == 3
              and r['tasks_pending'] == 3, r)
        check('unknown field ignored with a warning', r.get('warnings') == ["unknown field 'precent' ignored"], r.get('warnings'))
        check('task ids', [t['id'] for t in r['tasks']] == [f"{r['id']}-{i}" for i in (1, 2, 3)], r['tasks'])
        check('task keys, in order', all(list(t) == TASK_KEYS for t in r['tasks']), list(r['tasks'][0]))
        check('task create', r['tasks'][1]['title'] == 'b' and r['tasks'][0]['status'] == 'pending', r['tasks'][1])
        check('urls', r['url'] == f"http://127.0.0.1:{P}/r/{r['id']}" and r['tasks'][0]['url'] == f"{r['url']}#{r['id']}-1", r['url'])
        check('rid alphabet', len(r['id']) == 6 and all(c in '23456789abcdefghjkmnpqrstuvwxyz' for c in r['id']), r['id'])
        rid = r['id']
        long = new_request('x' * 250, tasks=['y' * 300])
        check('truncation to 200 with an ellipsis + warnings', len(long['title']) == 200 and long['title'].endswith('…')
              and len(long['tasks'][0]['title']) == 200 and len(long['warnings']) == 2, long.get('warnings'))
        for label, kw in (('empty title', {'body': {'title': '   '}}), ('missing title', {'body': {}}),
                          ('non-str title', {'body': {'title': 5}})):
            s, _, b = call('POST', '/api/requests', **kw)
            check(label, s == 400 and b.get('field') == 'title', b)
        s, _, b = call('POST', '/api/requests', raw=b'{"title": ')
        check('bad json', s == 400 and 'invalid JSON' in b['error'], b)
        s, _, b = call('POST', '/api/requests', raw=b'[1, 2]')
        check('non-object json', s == 400 and 'object' in b['error'], b)
        for label, tasks in (('tasks not a list', 'abc'), ('tasks item not a string', ['ok', 3]), ('tasks > 200', ['x'] * 201)):
            s, _, b = call('POST', '/api/requests', {'title': 't', 'tasks': tasks})
            check(label, s == 400 and b.get('field') == 'tasks', b)
        s, _, b = call('POST', '/api/requests', raw=b'{"title": "form"}',
                       headers={'Content-Type': 'application/x-www-form-urlencoded'})
        check('form content-type still parsed as json', s == 201 and b['title'] == 'form', b)
        s, _, b = call('POST', '/api/requests', raw=b'5\r\n{"tit\r\n0\r\n\r\n', headers={'Transfer-Encoding': 'chunked'})
        check('chunked 411', s == 411, (s, b))
        s, _, b = call('POST', '/api/requests', raw=b'{"title": "' + b'x' * 70000 + b'"}')
        check('body > 64 KiB 413', s == 413, (s, b))
        s, _, b = call('POST', '/api/requests', raw='{"title": "\\ud800 lone"}'.encode())
        check('lone surrogate survives', s == 201 and 'lone' in b['title'], (s, b))

        S.section('get + id validation')
        s, _, b = call('GET', f'/api/requests/{rid.upper()}')
        check('get request (upper case id)', s == 200 and b['id'] == rid and b['stale_after'] == 2
              and [t['seq'] for t in b['tasks']] == [1, 2, 3] and 'now' in b, b)
        s, _, b = call('GET', f'/api/requests/{rid}-3')
        check('task id on a request route: 400 + hint', s == 400 and b.get('hint') == f'{rid}-3 is a task id; use /api/tasks/{rid}-3', b)
        s, _, b = call('POST', f'/api/requests/{rid}-3/complete', {})
        check('task id on request complete: hint keeps the suffix',
              s == 400 and b.get('hint') == f'{rid}-3 is a task id; use /api/tasks/{rid}-3/complete', b)
        s, _, b = call('GET', f'/api/tasks/{rid}')
        check('request id on a task route: 400 + hint', s == 400 and b.get('hint') == f'{rid} is a request id; use /api/requests/{rid}', b)
        s, _, b = call('POST', f'/api/tasks/{rid}/progress', {'message': 'x'})
        check('request id on progress: 400 + hint', s == 400 and 'hint' in b, b)
        s, _, b = call('GET', '/api/requests/bogus!')
        check('malformed id', s == 400 and 'malformed' in b['error'], b)
        s, _, b = call('GET', '/api/requests/zzzzzz')
        check('unknown request 404', s == 404 and b == {'error': "unknown request 'zzzzzz'"}, b)
        s, _, b = call('GET', '/api/tasks/zzzzzz-3')
        check('unknown task 404', s == 404 and b == {'error': "unknown task 'zzzzzz-3' (request may have been deleted)"}, b)
        s, _, b = call('GET', f'/api/tasks/{rid.upper()}-1')
        check('get task (upper case id)', s == 200 and b['id'] == f'{rid}-1' and 'now' in b, b)

        S.section('progress')
        t1, t2, t3 = (f'{rid}-{i}' for i in (1, 2, 3))
        s, _, b = progress(t1, message='compiling', percent='45%', eta_seconds=90)
        check('progress starts a pending task', s == 200 and b['status'] == 'running' and b['started_at'] is not None
              and b['percent'] == 45.0 and abs(b['eta_at'] - (b['now'] + 90)) < 0.01 and 89 < b['eta_remaining'] <= 90, b)
        s, _, b = progress(t1, message='  line one  \n\n  line   two\r\nthree\x1b[0m ')
        check('progress keeps percent, clears eta, keeps newlines', s == 200 and b['percent'] == 45.0 and b['eta_at'] is None
              and b['eta_remaining'] is None and b['message'] == 'line one\n\nline two\nthree[0m', repr(b['message']))
        s, _, b = progress(t1, message='x', percent=150)
        check('percent clamped high + warning', s == 200 and b['percent'] == 100.0 and b.get('warnings'), b)
        s, _, b = progress(t1, message='x', percent=-3)
        check('percent clamped low + warning', s == 200 and b['percent'] == 0.0 and b.get('warnings'), b)
        s, _, b = progress(t1, message='x', percent=33.333)
        check('percent rounded to 1 decimal', s == 200 and b['percent'] == 33.3, b)
        for bad in (True, 'abc', 'NaN', 'inf', [1], {'a': 1}):
            s, _, b = progress(t1, message='x', percent=bad)
            check(f'bad percent {bad!r}', s == 400 and b.get('field') == 'percent', b)
        s, _, b = call('POST', f'/api/tasks/{t1}/progress', raw=b'{"message": "x", "percent": NaN}')
        check('JSON NaN percent', s == 400 and b.get('field') == 'percent', b)
        s, _, b = call('POST', f'/api/tasks/{t1}/progress', raw=b'{"message": "x", "percent": 1' + b'0' * 400 + b'}')
        check('huge integer percent', s == 400 and b.get('field') == 'percent', b)
        s, _, b = progress(t1, percent=5)
        check('progress message required', s == 400 and b.get('field') == 'message', b)
        s, _, b = progress(t1, message='  ')
        check('progress message not empty', s == 400 and b.get('field') == 'message', b)
        s, _, b = progress(t1, message='x', eta_seconds=-1)
        check('negative eta', s == 400 and b.get('field') == 'eta_seconds', b)
        s, _, b = progress(t1, message='x', eta_seconds='5m')
        check('non-numeric eta string', s == 400 and b.get('field') == 'eta_seconds', b)
        s, _, b = progress(t1, message='x', eta_seconds='120')
        check('numeric eta string', s == 200 and abs(b['eta_at'] - b['now'] - 120) < 0.01, b)
        s, _, b = progress(t1, message='x', eta_seconds=10 ** 7)
        check('eta clamped to 7 days + warning', s == 200 and abs(b['eta_at'] - b['now'] - 604800) < 0.01 and b.get('warnings'), b)
        s, _, b = progress(t1, message='m' * 600)
        check('message truncated to 500', s == 200 and len(b['message']) == 500 and b['message'].endswith('…') and b.get('warnings'), b)
        s, _, b = progress(t1, message='x', precent=5)
        check("misspelled field: warning suggests the known one", s == 200
              and b.get('warnings') == ["unknown field 'precent' ignored (did you mean 'percent'?)"], b.get('warnings'))
        s, _, b = progress(t1, message='x', eta_seconds=None, percent=None)
        check('null eta clears, null percent keeps', s == 200 and b['eta_at'] is None and b['percent'] == 33.3 and 'warnings' not in b, b)

        S.section('current + request eta')
        progress(t2, message='second', eta_seconds=300)
        s, _, b = call('GET', f'/api/requests/{rid}')
        check('current + eta_at', b['current'] == {'id': t2, 'title': 'b', 'message': 'second'} and b['tasks_running'] == 2
              and abs(b['eta_at'] - (b['tasks'][1]['eta_at'])) < 0.001, (b['current'], b['eta_at']))
        progress(t1, message='first again', eta_seconds=60)
        s, _, b = call('GET', f'/api/requests/{rid}')
        check('current follows the latest update, eta_at is the max', b['current']['id'] == t1
              and b['eta_at'] == b['tasks'][1]['eta_at'], b['current'])

        S.section('task complete rules')
        s, _, b = complete('tasks', t3)
        check('pending -> done: start inferred', s == 200 and b['status'] == 'done' and b['percent'] == 100.0
              and b['start_inferred'] is True and b['started_at'] == b['created_at'] and b['completed_at'] is not None
              and b['elapsed'] is not None, b)
        first_completed = b['completed_at']
        s, _, b = complete('tasks', t3, status='ok')
        check('same status (alias ok): no-op', s == 200 and b['completed_at'] == first_completed and b['status'] == 'done', b)
        s, _, b = complete('tasks', t3, status='SUCCESS', message='all good')
        check('same status: message replaced', s == 200 and b['message'] == 'all good' and b['completed_at'] == first_completed, b)
        s, _, b = complete('tasks', t3, status='error', message='late failure')
        check('different status: last write wins', s == 200 and b['status'] == 'failed' and b['completed_at'] == first_completed
              and b['percent'] == 100.0 and b['message'] == 'late failure', b)
        s, _, b = complete('tasks', t3, status='completed')
        check('back to done, message kept', s == 200 and b['status'] == 'done' and b['message'] == 'late failure', b)
        s, _, b = complete('tasks', t3, status='maybe')
        check('bad status', s == 400 and b.get('field') == 'status', b)
        s, _, b = complete('tasks', t3, status=1)
        check('non-string status', s == 400 and b.get('field') == 'status', b)
        s, _, b = progress(t3, message='x')
        check('progress on a closed task: 409', s == 409 and b == {'error': f'task {t3} is already done', 'status': 'done'}, b)
        s, _, b = call('POST', f'/api/tasks/{t1}/complete', raw=b'')
        check('empty body complete = done', s == 200 and b['status'] == 'done' and b['eta_at'] is None and b['start_inferred'] is False, b)
        s, _, b = complete('tasks', t2, status='fail', message='boom')
        check('fail keeps percent', s == 200 and b['status'] == 'failed' and b['percent'] == 0.0 and b['message'] == 'boom', b)
        s, _, b = call('GET', f'/api/requests/{rid}')
        check('aggregates after completes', b['status'] == 'running' and b['tasks_done'] == 2 and b['tasks_failed'] == 1
              and b['percent'] == round((100 + 0 + 100) / 3, 1) and b['current'] is None and b['eta_at'] is None, b)

        S.section('request complete cascade')
        r = new_request('cascade', tasks=['run', 'pend1', 'pend2', 'finished'])
        c1, c2, c3, c4 = (t['id'] for t in r['tasks'])
        progress(c1, message='working', percent=40, eta_seconds=50)
        complete('tasks', c4, message='was done')
        s, _, b = complete('requests', r['id'], message='wrapped up')
        check('cascade done: auto_closed + cancelled', s == 200 and b['auto_closed'] == [c1] and b['cancelled'] == [c2, c3]
              and b['status'] == 'done' and b['message'] == 'wrapped up' and b['completed_at'] is not None, b)
        tk = {t['id']: t for t in b['tasks']}
        check('running -> done 100%', tk[c1]['status'] == 'done' and tk[c1]['percent'] == 100.0 and tk[c1]['eta_at'] is None
              and tk[c1]['completed_at'] == b['completed_at'], tk[c1])
        check('pending -> cancelled', tk[c2]['status'] == 'cancelled' and tk[c2]['completed_at'] is not None
              and tk[c2]['started_at'] is None and tk[c2]['elapsed'] is None, tk[c2])
        check('aggregates exclude cancelled', b['tasks_total'] == 2 and b['tasks_done'] == 2 and b['tasks_cancelled'] == 2
              and b['percent'] == 100.0, b)
        req_completed = b['completed_at']
        s, _, b = complete('requests', r['id'], status='done')
        check('request same status: no-op', s == 200 and b['auto_closed'] == [] and b['cancelled'] == []
              and b['completed_at'] == req_completed and b['message'] == 'wrapped up', b)
        s, _, b = complete('requests', r['id'], status='done', message='new msg')
        check('request same status: message replaced', s == 200 and b['message'] == 'new msg' and b['completed_at'] == req_completed, b)
        s, _, b = complete('requests', r['id'], status='failed', message='actually failed')
        check('request different status: overwrite, completed_at kept', s == 200 and b['status'] == 'failed'
              and b['completed_at'] == req_completed and b['auto_closed'] == [] and tk[c1]['status'] == 'done', b)
        s, _, b = complete('tasks', c2, status='failed', message='ran after all')
        check('cancelled -> failed overwrite', s == 200 and b['status'] == 'failed' and b['completed_at'] == tk[c2]['completed_at']
              and b['start_inferred'] is True and b['started_at'] == b['created_at'], b)
        s, _, b = call('GET', f"/api/requests/{r['id']}")
        check('completing a task never reopens its request', b['status'] == 'failed' and b['completed_at'] == req_completed
              and b['tasks_total'] == 3 and b['percent'] == round((100 + 100 + 0) / 3, 1), b)

        r = new_request('cascade fail', tasks=['run', 'pend'])
        f1, f2 = (t['id'] for t in r['tasks'])
        progress(f1, message='going', percent=30)
        s, _, b = complete('requests', r['id'], status='failed', message='gave up')
        tk = {t['id']: t for t in b['tasks']}
        check('cascade failed keeps the running percent', b['auto_closed'] == [f1] and b['cancelled'] == [f2]
              and tk[f1]['status'] == 'failed' and tk[f1]['percent'] == 30.0 and b['percent'] == 30.0 and b['tasks_total'] == 1, b)

        S.section('adding tasks, auto-reopen')
        s, _, b = call('POST', f"/api/requests/{r['id']}/tasks", {'title': 'retry'})
        check('single add to a closed request: reopened, started', s == 201 and b['reopened'] is True and b['status'] == 'running'
              and b['started_at'] is not None and b['id'] == f"{r['id']}-3" and 'now' in b
              and list(b)[:len(TASK_KEYS)] == TASK_KEYS, b)
        s, _, b = call('GET', f"/api/requests/{r['id']}")
        check('reopened request state', b['status'] == 'running' and b['completed_at'] is None and b['message'] is None
              and b['tasks_total'] == 2 and b['tasks_cancelled'] == 1 and b['percent'] == round((30 + 0) / 2, 1), b)
        s, _, b = call('POST', f"/api/requests/{r['id']}/tasks", {'title': 'later', 'start': False})
        check('start false: pending', s == 201 and b['status'] == 'pending' and b['started_at'] is None and b['reopened'] is False, b)
        s, _, b = call('POST', f"/api/requests/{r['id']}/tasks", {'title': 'x', 'start': 'yes'})
        check('bad start', s == 400 and b.get('field') == 'start', b)
        s, _, b = call('POST', f"/api/requests/{r['id']}/tasks", {'title': 'x', 'titles': ['y']})
        check('title and titles together: 400', s == 400, b)
        s, _, b = call('POST', f"/api/requests/{r['id']}/tasks", {'titles': []})
        check('empty titles', s == 400 and b.get('field') == 'titles', b)
        complete('requests', r['id'])
        s, _, b = call('POST', f"/api/requests/{r['id']}/tasks", {'titles': ['p1', 'p2']})
        check('bulk add reopens, all pending, next seqs', s == 201 and b['reopened'] is True and b['request_id'] == r['id']
              and set(b) >= {'now', 'tasks'} and [t['status'] for t in b['tasks']] == ['pending', 'pending']
              and [t['seq'] for t in b['tasks']] == [5, 6], b)
        s, _, b = call('POST', f"/api/requests/{r['id']}/tasks", {})
        check('add without a title', s == 400 and b.get('field') == 'title', b)
        s, _, b = call('POST', '/api/requests/zzzzzz/tasks', {'title': 'x'})
        check('add to an unknown request: 404', s == 404, b)

        S.section('zero-task requests, 500-task cap')
        z = new_request('empty')
        check('empty running request: 0%', z['percent'] == 0.0 and z['tasks_total'] == 0, z)
        s, _, b = complete('requests', z['id'])
        check('empty done request: 100%', b['percent'] == 100.0, b)
        z2 = new_request('empty fail')
        s, _, b = complete('requests', z2['id'], status='failed')
        check('empty failed request: 0%', b['percent'] == 0.0, b)
        big = new_request('big', tasks=[f't{i}' for i in range(200)])
        s, _, b = call('POST', f"/api/requests/{big['id']}/tasks", {'titles': [f'u{i}' for i in range(200)]})
        check('add 200 more', s == 201, s)
        s, _, b = call('POST', f"/api/requests/{big['id']}/tasks", {'titles': [f'v{i}' for i in range(101)]})
        check('exceeding 500 tasks: 409', s == 409, (s, b))
        s, _, b = call('POST', f"/api/requests/{big['id']}/tasks", {'titles': [f'v{i}' for i in range(100)]})
        check('exactly 500 ok', s == 201, s)
        s, _, b = call('POST', f"/api/requests/{big['id']}/tasks", {'title': 'one more'})
        check('single add beyond 500: 409', s == 409, s)

        S.section('Origin guard')
        s, _, b = call('POST', '/api/requests', {'title': 'x'}, headers={'Origin': 'http://evil.example'})
        check('foreign Origin 403', s == 403 and b == {'error': 'cross-origin write refused'}, b)
        s, _, b = call('POST', '/api/requests', {'title': 'x'}, headers={'Origin': f'http://evil.example:{P}'},
                       host=f'evil.example:{P}')
        check('DNS rebinding (Origin == Host, foreign name) 403', s == 403, b)
        s, _, b = call('POST', '/api/requests', {'title': 'x'}, headers={'Origin': 'null'})
        check('null Origin 403', s == 403, b)
        s, _, b = call('POST', '/api/requests', {'title': 'same origin'}, headers={'Origin': f'http://127.0.0.1:{P}'})
        check('same-origin IP ok', s == 201, (s, b))
        s, _, b = call('POST', '/api/requests', {'title': 'same origin'}, headers={'Origin': f'http://localhost:{P}'},
                       host=f'localhost:{P}')
        check('same-origin localhost ok (and its url)', s == 201 and b['url'].startswith(f'http://localhost:{P}/r/'), (s, b))
        hn = socket.gethostname()
        s, _, b = call('POST', '/api/requests', {'title': 'hn'}, headers={'Origin': f'http://{hn}:{P}'}, host=f'{hn}:{P}')
        check('same-origin machine hostname ok', s == 201, (s, b))
        s, _, b = call('DELETE', f"/api/requests/{z['id']}", headers={'Origin': 'http://evil.example'})
        check('DELETE with foreign Origin 403', s == 403, b)
        s, _, b = call('GET', '/api/requests', headers={'Origin': 'http://evil.example'})
        check('GET with foreign Origin ok', s == 200, s)
        s, _, b = call('GET', '/api/health', host=CRAFTED)
        check('crafted Host GET ok', s == 200, s)
        s, _, b = call('GET', '/nope', host=CRAFTED)
        check('crafted Host 404 hint falls back to the socket address', s == 404 and 'rm -rf' not in json.dumps(b)
              and b['hint'] == f'GET http://127.0.0.1:{P}/api/usage', b)
        s, _, b = call('POST', '/api/requests', {'title': 'crafted'}, host=CRAFTED)
        check('crafted Host url fallback', s == 201 and b['url'].startswith(f'http://127.0.0.1:{P}/r/'), b['url'])

        S.section('delete')
        s, _, b = call('DELETE', f"/api/requests/{z['id']}")
        check('delete', s == 200 and b['deleted'] == z['id'] and 'now' in b, b)
        s, _, b = call('DELETE', f"/api/requests/{z['id']}")
        check('delete again 404', s == 404, b)
        s, _, b = call('DELETE', f'/api/requests/{z["id"]}-1')
        check('delete with a task id 400', s == 400 and 'hint' in b, b)
        dr = new_request('to delete', tasks=['a'])
        call('DELETE', f"/api/requests/{dr['id']}")
        s, _, b = progress(dr['tasks'][0]['id'], message='x')
        check('task gone after its request is deleted', s == 404 and 'may have been deleted' in b['error'], b)

        S.section('list')
        for i in range(2):
            complete('requests', new_request(f'done {i}')['id'])
        s, _, b = call('GET', '/api/requests')
        reqs = b['requests']
        running = [x for x in reqs if x['status'] == 'running']
        finished = [x for x in reqs if x['status'] != 'running']
        check('list keys', s == 200 and set(b) >= {'now', 'stale_after', 'counts', 'requests'} and b['stale_after'] == 2, list(b))
        check('running first', reqs == running + finished, [x['status'] for x in reqs])
        check('running by created_at desc', [x['created_at'] for x in running] == sorted((x['created_at'] for x in running), reverse=True))
        check('finished by completed_at desc', [x['completed_at'] for x in finished]
              == sorted((x['completed_at'] for x in finished), reverse=True))
        check('list item keys', all(list(x) == REQ_KEYS for x in reqs), list(reqs[0]))
        with sqlite3.connect(srv.db) as db:
            dbcounts = dict(db.execute('select status, count(*) from requests group by status').fetchall())
        check('counts over the whole table', b['counts']['running'] == dbcounts.get('running', 0)
              and b['counts']['done'] == dbcounts.get('done', 0) and b['counts']['failed'] == dbcounts.get('failed', 0)
              and 'stale' in b['counts'], (b['counts'], dbcounts))
        s, _, b = call('GET', '/api/requests?status=done&limit=1')
        check('status filter + limit', s == 200 and len(b['requests']) == 1 and b['requests'][0]['status'] == 'done'
              and b['counts']['running'] == dbcounts['running'], b['requests'])
        s, _, b = call('GET', '/api/requests?limit=1')
        check('limit caps finished only', len([x for x in b['requests'] if x['status'] == 'running']) == dbcounts['running']
              and len([x for x in b['requests'] if x['status'] != 'running']) == 1)
        s, _, b = call('GET', '/api/requests?status=running')
        check('status=running', all(x['status'] == 'running' for x in b['requests']) and len(b['requests']) == dbcounts['running'])
        s, _, b = call('GET', '/api/requests?status=bogus')
        check('bad status filter', s == 400 and b.get('field') == 'status', b)
        s, _, b = call('GET', '/api/requests?limit=abc')
        check('bad limit', s == 400 and b.get('field') == 'limit', b)
        s, _, b = call('GET', '/api/requests?limit=0')
        check('limit clamped + warning', s == 200 and b.get('warnings'), b.get('warnings'))

        S.section('stale')
        st = new_request('stale me', tasks=['a', 'b'])
        sa, sb = (t['id'] for t in st['tasks'])
        progress(sa, message='quiet')
        progress(sb, message='with eta', eta_seconds=30)
        st2 = new_request('stale me too', tasks=['a'])
        progress(st2['tasks'][0]['id'], message='quiet')
        time.sleep(2.6)
        s, _, b = call('GET', f"/api/requests/{st['id']}")
        tk = {t['id']: t for t in b['tasks']}
        check('a running eta keeps its task and request fresh', tk[sa]['stale'] is True and tk[sb]['stale'] is False
              and b['stale'] is False, (tk[sa]['stale'], tk[sb]['stale'], b['stale']))
        s, _, b = call('GET', f"/api/requests/{st2['id']}")
        check('silent request and task are stale', b['stale'] is True and b['tasks'][0]['stale'] is True, b['stale'])
        s, _, b = call('GET', '/api/requests')
        check('counts.stale', b['counts']['stale'] >= 1 and b['counts']['stale'] == sum(1 for x in b['requests'] if x['stale']), b['counts'])

        S.section('503 while another process holds the write lock')
        locker = sqlite3.connect(srv.db, isolation_level=None)
        locker.execute('BEGIN IMMEDIATE')
        t0 = time.time()
        s, h, b = call('POST', '/api/requests', {'title': 'locked'})
        check('503 database busy + Retry-After', s == 503 and b == {'error': 'database busy'} and h.get('retry-after') == '1', (s, b))
        check('busy timeout waited', time.time() - t0 >= 1.3, time.time() - t0)
        s, _, b = call('GET', '/api/requests')
        check('reads still work while locked', s == 200, s)
        locker.execute('ROLLBACK')
        locker.close()

        S.section('expiry (maintenance every 2s)')
        ex = new_request('expire me', tasks=['run', 'pend'])
        progress(ex['tasks'][0]['id'], message='going quiet')
        keep = new_request('eta keeps alive', tasks=['a'])
        progress(keep['tasks'][0]['id'], message='long step', eta_seconds=60)
        b = wait_until(lambda: (lambda d: d if d['status'] != 'running' else None)(call('GET', f"/api/requests/{ex['id']}")[2]), 12, 0.3)
        b = b or call('GET', f"/api/requests/{ex['id']}")[2]
        check('silent request expires as failed, running -> failed, pending -> cancelled', b['status'] == 'failed'
              and b['message'] == 'expired: no activity for 4s' and [t['status'] for t in b['tasks']] == ['failed', 'cancelled'],
              (b['status'], b['message'], [t['status'] for t in b['tasks']]))
        s, _, b = call('GET', f"/api/requests/{keep['id']}")
        check('a running eta keeps a request from expiring', b['status'] == 'running', b['status'])

        S.section('EADDRINUSE')
        other_db, other_pid = TMP / 'other.db', TMP / 'other.pid'
        second = subprocess.run([sys.executable, str(SERVER), '--port', str(P), '--db', str(other_db),
                                 '--pidfile', str(other_pid)], capture_output=True, text=True, timeout=10)
        check('second server on the port: exit 2, clear error', second.returncode == 2
              and second.stderr.startswith(f'error: port {P} already in use'), (second.returncode, second.stderr))
        check('the loser leaves the pidfile alone and writes none', srv.pidfile.read_text().strip() == str(srv.proc.pid)
              and not other_pid.exists())
    finally:
        code = srv.stop()
    check('SIGTERM: clean exit, pidfile removed', code == 0 and not srv.pidfile.exists(), (code, srv.pidfile.exists()))
    log = srv.log_text()
    check('log levels without --verbose', 'created request' in log and 'shutting down' in log and ' DEBUG ' not in log
          and 'GET /api/health 200' not in log, log[-800:])
    check('log records expiry and delete', 'expired' in log and 'deleted request' in log)


def fixture_tree(name: str) -> Path:
    """A copy of server.py next to the fixture templates (tests/fixtures/fake_tasks)."""
    tree = TMP / name
    shutil.copytree(FIXTURES / 'fake_tasks', tree)
    shutil.copy2(SERVER, tree / 'server.py')
    return tree


def phase_templates():
    S.section('rendering, against fixture templates')
    tree = fixture_tree('fixture')
    with ScratchServer(TMP, script=tree / 'server.py', name='fixture') as srv:
        P, call = srv.port, srv.call
        s, h, b = call('GET', '/', headers={'Accept': 'text/html,application/xhtml+xml'})
        check('GET / with text/html: the page, no-cache, Vary', s == 200 and h['content-type'] == 'text/html; charset=utf-8'
              and h.get('cache-control') == 'no-cache' and h.get('vary') == 'Accept' and '<p>ui</p>' in b, (s, h))
        s, h, b = call('GET', '/', headers={'Accept': '*/*'})
        check('GET / otherwise: usage text, no-store, Vary', s == 200 and h['content-type'] == 'text/plain; charset=utf-8'
              and h.get('vary') == 'Accept' and h.get('cache-control') == 'no-store' and f'curl http://127.0.0.1:{P}/api/usage' in b, b)
        check('render is a literal replace of the three tokens only', f'Dashboard: http://127.0.0.1:{P}\n' in b and 'Version 1' in b
              and '{{UNKNOWN}}' in b and '{"json": {"braces": 1}}' in b and '${SHELL}' in b, b)
        for path in ('/api', '/api/usage', '/api/'):
            s, h, b2 = call('GET', path)
            check(f'usage at {path}', s == 200 and b2 == b, s)
        s, h, b = call('GET', '/api/rule')
        check('rule rendered', s == 200 and b.startswith('<!-- task-status:begin -->') and f'http://127.0.0.1:{P}' in b, b)
        s, h, b = call('GET', '/api/skill/SKILL.md')
        check('SKILL.md served unrendered', s == 200 and '{{BASE_URL}}' in b and h['content-type'] == 'text/plain; charset=utf-8', b)
        s, h, b = call('GET', '/api/skill/taskctl')
        check('taskctl wrapper served unrendered', s == 200 and '{{BASE_URL}}' in b, b)
        s, h, b = call('GET', '/api/skill/taskctl.py')
        check('taskctl.py rendered', s == 200 and f'BAKED_URL = "http://127.0.0.1:{P}"' in b, b)
        for evil in (CRAFTED, 'evil.example:80/"', 'a b', 'a\tb:1', 'x`id`', '$(id)', "a'b", 'a:99999999', '[::1"]',
                     'ex ample.com'):
            s, h, b = call('GET', '/api/skill/taskctl.py', host=evil)
            check(f'crafted Host {evil!r} never baked', s == 200 and evil not in b
                  and f'BAKED_URL = "http://127.0.0.1:{P}"' in b, b)
        s, h, b = call('GET', '/api/skill/taskctl.py', host='board.lan:9000')
        check('valid Host baked', 'BAKED_URL = "http://board.lan:9000"' in b, b)
        s, h, b = call('GET', '/api/usage', host=f'[::1]:{P}')
        check('IPv6 literal Host baked', f'curl http://[::1]:{P}/api/usage' in b, b)
        s, h, b = call('GET', '/r/k3m9qa')
        check('/r/<rid> is the page', s == 200 and '<p>ui</p>' in b and h.get('cache-control') == 'no-cache', s)
        s, h, b = call('GET', '/r/anything/deeper')
        check('/r/<anything> is the page', s == 200 and '<p>ui</p>' in b, s)
        usage = tree / 'templates' / 'usage.md'
        usage.write_text('edited {{BASE_URL}}\n')
        s, h, b = call('GET', '/api/usage')
        check('files are re-read on every request', b == f'edited http://127.0.0.1:{P}\n', b)


def phase_missing():
    S.section('every file route with its file missing (server.py alone in a directory)')
    tree = TMP / 'bare'
    tree.mkdir()
    shutil.copy2(SERVER, tree / 'server.py')
    with ScratchServer(TMP, script=tree / 'server.py', name='bare') as srv:
        for path, rel, accept in FILE_ROUTES:
            s, h, b = srv.call('GET', path, headers={'Accept': accept} if accept else None)
            check(f'GET {path}{" (Accept " + accept + ")" if accept else ""}: 500 template missing: {rel}',
                  s == 500 and b == {'error': f'template missing: {rel}'} and h.get('x-content-type-options') == 'nosniff'
                  and h.get('content-type') == 'application/json', (s, b))
        s, _, b = srv.call('GET', '/api/health')
        check('the API still works without any template', s == 200 and b['ok'] is True, b)
    check('the real tree still has every served file', all((TASKS / rel).is_file() for _, rel, _ in FILE_ROUTES))


def phase_public_url():
    S.section('--public-url, retention, pidfile ownership')
    tree = fixture_tree('public')
    public = 'http://board.example:8123'
    # retention 0.00003 days = 2.6s; --expire-after 2 runs maintenance every second
    srv = ScratchServer(TMP, '--public-url', public + '/', '--retention-days', '0.00003', '--expire-after', '2',
                        script=tree / 'server.py', name='public').start()
    P, call = srv.port, srv.call
    try:
        r = call('POST', '/api/requests', {'title': 'public', 'tasks': ['a']})[2]
        check('public url in every url field (trailing slash stripped)', r['url'] == f"{public}/r/{r['id']}"
              and r['tasks'][0]['url'].startswith(f'{public}/r/'), r['url'])
        s, h, b = call('GET', '/api/usage')
        check('BASE_URL from the Host, PUBLIC_URL from --public-url', f'curl http://127.0.0.1:{P}/api/usage' in b
              and f'Dashboard: {public}\n' in b, b)
        s, h, b = call('GET', '/api/skill/taskctl.py', host=CRAFTED)
        check('crafted Host falls back to the public url', f'BAKED_URL = "{public}"' in b, b)
        s, h, b = call('POST', '/api/requests', {'title': 'x'}, headers={'Origin': public}, host='board.example:8123')
        check('the public url host counts as this machine for the Origin guard', s == 201, (s, b))
        done_req = call('POST', '/api/requests', {'title': 'old', 'tasks': ['a']})[2]
        call('POST', f"/api/requests/{done_req['id']}/complete", {})
        gone = wait_until(lambda: call('GET', f"/api/requests/{done_req['id']}")[0] == 404, 10, 0.3)
        check('retention prunes a finished request', gone)
        s, _, b = call('GET', '/api/tasks/' + done_req['tasks'][0]['id'])
        check('retention cascades to its tasks', s == 404, s)
        s, _, b = call('GET', f"/api/requests/{r['id']}")
        check('a running request is not pruned before it expires (200) or is expired then pruned (404)', s in (200, 404), s)
        srv.pidfile.write_text('999999\n')  # another server took the pidfile over
    finally:
        srv.stop()
    check('a pidfile naming another pid is left alone at exit', srv.pidfile.exists() and srv.pidfile.read_text().strip() == '999999')


def phase_bad_args():
    S.section('argument errors')
    bad_db = TMP / 'bad.db'
    port = str(free_port())  # given first, so a regression that accepts the bad value never reaches the default port
    for extra in (['--public-url', 'ftp://x'], ['--public-url', 'http://a"b'], ['--port', '0'], ['--stale-after', '-1']):
        res = subprocess.run([sys.executable, str(SERVER), '--db', str(bad_db), '--pidfile', str(TMP / 'bad.pid'),
                              '--port', port, *extra],
                             capture_output=True, text=True, timeout=10)
        check(f'bad args {extra}: exit 2 with an error', res.returncode == 2 and 'error' in res.stderr, (res.returncode, res.stderr[-200:]))
    check('bad args create no database', not bad_db.exists())


if __name__ == '__main__':
    phase_api()
    phase_templates()
    phase_missing()
    phase_public_url()
    phase_bad_args()
    sys.exit(S.finish())
