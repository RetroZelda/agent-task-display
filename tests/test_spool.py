#!/usr/bin/env python3
"""taskctl's offline queue, end to end through the sh wrapper: writes that could not connect are kept in
the spool file (its path, format and mode, one per board URL, the private temp fallback) and replayed in
order with their original times (X-Tasks-Replay: 1 + at) by the next command that reaches the board or
by `flush`; superseded writes are coalesced; the exact warnings; --strict exit codes; a live write goes
after the backlog and is not a replay; 8 parallel processes share the queue under its lock; a connect
failure (refused, DNS, connect timeout) is queued while a read timeout is not; a 5xx stops a replay and
keeps the rest, a 4xx drops that write (and a refused create drops its request's writes); an idempotent
create counts as replayed; `run` against a dead board; `add` is never queued."""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import socket
import stat
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the checkout, even when run without run_all.sh
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from harness import WRAPPER, ScratchServer, Suite, api, clean_env, free_port, scratch_home, tempdir  # noqa: E402

S = Suite('spool')
check = S.check
TMP = tempdir('spool')
HOME = scratch_home(TMP)
SPOOL = TMP / 'queue'
ENV = clean_env(TASKS_SPOOL_DIR=SPOOL, CLAUDE_CONFIG_DIR=HOME / '.claude')
RID_CHARS = set('23456789abcdefghjkmnpqrstuvwxyz')


def tc(*argv, env=None, timeout=60):
    p = subprocess.run([str(WRAPPER), *map(str, argv)], capture_output=True, text=True, env=env or ENV, timeout=timeout, cwd=TMP)
    return p.returncode, p.stdout, p.stderr


def spool_file(url, directory=SPOOL):
    return Path(directory) / ('spool-%s.jsonl' % hashlib.sha1(url.encode()).hexdigest()[:10])


def records(url, directory=SPOOL):
    f = spool_file(url, directory)
    return [json.loads(line) for line in f.read_text().splitlines() if line.strip()] if f.exists() else []


def board(port, name, fresh=False):
    return ScratchServer(TMP, '--host', '127.0.0.1', port=port, name=name, fresh_db=fresh).start()


def events_for(url, rid):
    code, body, _ = api(url, 'GET', '/api/events?since=0&limit=1000')
    assert code == 200, body
    return [e for e in body['events'] if e['request_id'] == rid]


def near(a, b, tol=0.35):
    return a is not None and b is not None and abs(a - b) <= tol


def offline_then_replay():
    S.section('offline: new prints real ids, every write is queued with its time')
    P = free_port()
    URL = f'http://127.0.0.1:{P}'
    times = {'new': time.time()}
    rc, out, err = tc('--url', URL, 'new', 'Offline job', '-t', 'alpha', '-t', 'beta')
    ids = out.split()
    check('new: rc 0, its rid + rid-1, rid-2', rc == 0 and len(ids) == 3 and set(ids[0]) <= RID_CHARS and len(ids[0]) == 6
          and ids[1:] == [ids[0] + '-1', ids[0] + '-2'], (rc, out, err))
    rid, ta, tb = ids
    check('new: the exact queued warning (no "continuing without")', f'taskctl: warning: board unreachable at {URL} (' in err
          and err.rstrip().endswith('; queued for replay (1 queued)') and 'continuing without' not in err, err)
    check('new: the created note says queued, with the page link, and lists its tasks',
          f"taskctl: created {rid} 'Offline job' (queued until the board is back)  page: {URL}/r/{rid}\n" in err
          and f'taskctl:   {ta}  alpha\n' in err and f'taskctl:   {tb}  beta\n' in err, err)
    recs = records(URL)
    check('the spool: one JSON line {method, path, body, at, queued_at, qid}, the body carries the id', len(recs) == 1
          and list(recs[0]) == ['method', 'path', 'body', 'at', 'queued_at', 'qid'] and recs[0]['method'] == 'POST'
          and re.fullmatch('[0-9a-f]{16}', recs[0]['qid'])
          and recs[0]['path'] == '/api/requests' and recs[0]['body']['id'] == rid and recs[0]['body']['title'] == 'Offline job'
          and recs[0]['body']['tasks'] == ['alpha', 'beta'] and near(recs[0]['at'], times['new'], 1), recs)
    f = spool_file(URL)
    check('spool-<sha1(url)[:10]>.jsonl in TASKS_SPOOL_DIR, mode 0600, its lock file beside it', f.exists()
          and stat.S_IMODE(f.stat().st_mode) == 0o600 and Path(str(f) + '.lock').exists(), sorted(os.listdir(SPOOL)))
    check('the spool dir is private (0700)', stat.S_IMODE(SPOOL.stat().st_mode) == 0o700, oct(SPOOL.stat().st_mode))
    time.sleep(1.1)
    times['start_a'] = time.time()
    rc, out, err = tc('--url', URL, 'start', ta, 'go')
    check('start: rc 0, no stdout, 2 queued', rc == 0 and out == '' and '(2 queued)' in err, (rc, out, err))
    time.sleep(1.1)
    rc, out, err = tc('--url', URL, 'progress', tb, '30', 'b working', '--eta', '5m')
    check('progress: 3 queued', rc == 0 and '(3 queued)' in err, err)
    time.sleep(1.1)
    times['done_a'] = time.time()
    rc, out, err = tc('--url', URL, 'done', ta, 'ok')
    check("done: the start it supersedes is dropped (still 3 queued)", rc == 0 and '(3 queued)' in err, err)
    time.sleep(1.1)
    rc, out, err = tc('--url', URL, 'ask', rid, 'Which branch?')
    check('ask: 4 queued, no "waiting for input" note (it did not reach the board)', rc == 0 and '(4 queued)' in err
          and 'waiting for input' not in err, err)
    rc, out, err = tc('--url', URL, 'resume', rid)
    check('resume: supersedes the ask (still 4 queued)', rc == 0 and '(4 queued)' in err, err)
    time.sleep(1.1)
    times['strict_b'] = time.time()
    rc, out, err = tc('--strict', '--url', URL, 'progress', tb, '70', 'b later')
    check('--strict: exit 3, yet queued (superseding the older progress: 4 queued)', rc == 3
          and 'taskctl: error: board unreachable' in err and '(4 queued)' in err, (rc, err))
    rc, out, err = tc('--url', URL, 'ping')
    check('ping: the URL on stdout, "N queued" and the spool path on stderr, one warning', rc == 0 and out == URL + '\n'
          and f'taskctl: 4 queued (writes waiting for the board, in {f}; taskctl flush replays them)' in err
          and err.count('warning:') == 1, (rc, out, err))
    rc, out, err = tc('--url', URL, 'show', rid)
    check('show: soft, one warning (the replay attempt and the call share it), the spool untouched', rc == 0 and out == ''
          and err.count('warning:') == 1 and len(records(URL)) == 4, (rc, out, err))
    rc, out, err = tc('--url', URL, 'add', rid, 'late one')
    check('add is never queued: offline per title', rc == 0 and out == 'offline\n' and len(records(URL)) == 4, (rc, out, err))
    rc, out, err = tc('--url', URL, 'flush')
    check('flush with the board down: soft, "4 still queued"', rc == 0 and '4 still queued' in err and err.count('warning:') == 1, (rc, err))
    rc, out, err = tc('--strict', '--url', URL, 'flush')
    check('flush --strict with the board down: exit 3', rc == 3, (rc, err))
    queued = records(URL)
    order = [(r['method'], r['path']) for r in queued]
    check('the queue, in order, coalesced', order == [('POST', '/api/requests'), ('POST', f'/api/tasks/{ta}/complete'),
                                                        ('DELETE', f'/api/requests/{rid}/attention'), ('POST', f'/api/tasks/{tb}/progress')], order)
    at = {r['path']: r['at'] for r in queued}
    check("each record's at is when its command ran", near(at['/api/requests'], times['new'], 1.5)
          and near(at[f'/api/tasks/{ta}/complete'], times['done_a'], 1.5) and near(at[f'/api/tasks/{tb}/progress'], times['strict_b'], 1.5)
          and at['/api/requests'] < at[f'/api/tasks/{ta}/complete'] < at[f'/api/tasks/{tb}/progress'], (at, times))

    S.section('the board is back: the next command replays the queue first, in order, at the original times')
    time.sleep(1)
    srv = board(P, 'p', fresh=True)
    replay_time = time.time()
    rc, out, err = tc('--url', URL, 'list')
    check('"replayed 4 queued writes", then the command itself', rc == 0 and 'taskctl: replayed 4 queued writes\n' in err
          and rid in out, (rc, out, err))
    check('the spool file is deleted once empty', not f.exists())
    code, r, _ = api(URL, 'GET', f'/api/requests/{rid}')
    check('the request exists under its client-made id, with its title and tasks', code == 200 and r['title'] == 'Offline job'
          and [t['id'] for t in r['tasks']] == [ta, tb], r)
    check("created_at is the queued create's at (the offline new), not the replay's time", near(r['created_at'], at['/api/requests'], 0.002)
          and r['created_at'] < replay_time - 3, (r['created_at'], at, replay_time))
    a = next(t for t in r['tasks'] if t['id'] == ta)
    b = next(t for t in r['tasks'] if t['id'] == tb)
    check('task a: done at the offline done time (its start inferred, as the start was coalesced away)', a['status'] == 'done'
          and a['message'] == 'ok' and near(a['completed_at'], at[f'/api/tasks/{ta}/complete'], 0.002) and a['start_inferred'] is True, a)
    check('task b: the last queued progress, at its time', b['status'] == 'running' and b['percent'] == 70
          and b['message'] == 'b later' and near(b['updated_at'], at[f'/api/tasks/{tb}/progress'], 0.002)
          and b['started_at'] == b['updated_at'], b)
    check('ask then resume: no attention left', r['attention'] is None and r['waiting'] is False, r)
    ev = events_for(URL, rid)
    kinds = [e['type'] for e in ev]
    check('events: one create, task a done, task b started; all replayed', kinds == ['request_created', 'task_done', 'task_started']
          and all(e['replayed'] for e in ev), ev)
    check('event times are the queued times', near(ev[0]['ts'], at['/api/requests'], 0.002)
          and near(ev[1]['ts'], at[f'/api/tasks/{ta}/complete'], 0.002) and near(ev[2]['ts'], at[f'/api/tasks/{tb}/progress'], 0.002),
          ([e['ts'] for e in ev], at))
    rc, out, err = tc('--url', URL, 'flush')
    check('flush with nothing queued', rc == 0 and err == f'taskctl: nothing queued for {URL}\n', err)
    rc, out, err = tc('--url', URL, 'list')
    check('no second replay, no duplicates', 'replayed' not in err and len(events_for(URL, rid)) == 3, err)
    rc, out, err = tc('--url', URL, 'progress', tb, '80', 'live now')
    check('a live write is not a replay', rc == 0 and api(URL, 'GET', f'/api/tasks/{tb}')[1]['percent'] == 80
          and events_for(URL, rid)[-1]['replayed'] is False, events_for(URL, rid)[-1])

    S.section('a write while writes are queued goes after them; the live one is not a replay')
    srv.stop()
    rc, out, err = tc('--url', URL, 'progress', tb, '85', 'queued while down')
    check('queued while down', rc == 0 and '(1 queued)' in err, err)
    srv = board(P, 'p')
    rc, out, err = tc('--url', URL, 'ask', tb, 'Deploy now?')
    t = api(URL, 'GET', f'/api/tasks/{tb}')[1]
    ev = events_for(URL, rid)
    check('the backlog replayed first, then the ask sent live', rc == 0 and 'replayed 1 queued writes' in err
          and f'taskctl: {tb} waiting for input: Deploy now?' in err and t['attention']['message'] == 'Deploy now?'
          and t['percent'] == 85, (err, t))
    check('the backlog event is replayed, the ask is not (the page chimes for it)', ev[-1]['type'] == 'attention'
          and ev[-1]['replayed'] is False and ev[-2]['type'] == 'task_progress' and ev[-2]['replayed'] is True
          and ev[-2]['percent'] == 85, ev[-3:])

    S.section('run against a dead board: its reports are queued, and land later')
    srv.stop()
    rc, out, err = tc('--url', URL, 'run', tb, '--every', '0.2', '--', 'sh', '-c', 'echo 1/2; sleep 0.5; echo 2/2')
    check('run: the command ran, its exit code, one warning ("the command is unaffected")', rc == 0 and out == '1/2\n2/2\n'
          and err.count('warning:') == 1 and 'the command is unaffected' in err, (rc, out, err))
    recs = records(URL)
    check("run: its reports are queued, and its close supersedes its progress (one write left)", len(recs) == 1
          and recs[0]['path'] == f'/api/tasks/{tb}/complete' and recs[0]['body']['status'] == 'done'
          and recs[0]['body']['message'].startswith('exit 0 after'), recs)
    srv = board(P, 'p')
    rc, out, err = tc('--url', URL, 'flush')
    t = api(URL, 'GET', f'/api/tasks/{tb}')[1]
    check('flush: replayed; the task is done with run\'s message', rc == 0 and 'replayed' in err and t['status'] == 'done'
          and t['message'].startswith('exit 0 after'), (err, t))
    srv.stop()


def parallel():
    S.section('8 parallel taskctl processes share the queue under its lock')
    Q = free_port()
    URL = f'http://127.0.0.1:{Q}'
    rc, out, err = tc('--url', URL, 'new', 'Parallel', *sum([['-t', f'job {i}'] for i in range(1, 9)], []))
    ids = out.split()
    prid = ids[0]
    check('offline new printed 9 ids', rc == 0 and len(ids) == 9, (rc, out, err))
    procs = [subprocess.Popen([str(WRAPPER), '--url', URL, 'progress', f'{prid}-{i}', str(10 * i), f'job {i} going'],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV, cwd=TMP) for i in range(1, 9)]
    results = [(p.wait(60), p.stderr.read()) for p in procs]
    recs = records(URL)
    check('all 8 exit 0', all(rc == 0 for rc, _ in results), results)
    check('9 records: none lost, none duplicated', len(recs) == 9 and recs[0]['path'] == '/api/requests'
          and sorted(r['path'] for r in recs[1:]) == sorted(f'/api/tasks/{prid}-{i}/progress' for i in range(1, 9)), recs)
    srv = board(Q, 'q', fresh=True)
    procs = [subprocess.Popen([str(WRAPPER), '--url', URL, 'ping'], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV,
                              cwd=TMP) for _ in range(8)]
    results = [(p.wait(60), p.stderr.read()) for p in procs]
    replayed = [line for _, e in results for line in e.splitlines() if 'replayed' in line]
    check('8 parallel pings: exactly one of them replayed all 9', replayed == ['taskctl: replayed 9 queued writes'], replayed)
    r = api(URL, 'GET', f'/api/requests/{prid}')[1]
    ev = events_for(URL, prid)
    check('every task started once, at its percent; one create', all(t['status'] == 'running' and t['percent'] == 10 * t['seq']
                                                                     for t in r['tasks'])
          and [e['type'] for e in ev].count('task_started') == 8 and [e['type'] for e in ev].count('request_created') == 1,
          ([t['percent'] for t in r['tasks']], [e['type'] for e in ev]))
    check('the spool is gone', not spool_file(URL).exists())
    srv.stop()
    tc('--url', URL, 'progress', f'{prid}-1', '15', 'backlog')
    srv = board(Q, 'q')
    procs = [subprocess.Popen([str(WRAPPER), '--strict', '--url', URL, 'progress', f'{prid}-{i}', str(10 * i + 5), f'live {i}'],
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=ENV, cwd=TMP) for i in range(2, 9)]
    results = [(p.wait(60), p.stderr.read()) for p in procs]
    r = api(URL, 'GET', f'/api/requests/{prid}')[1]
    replayers = [e for _, e in results if 'replayed' in e]
    behind = [e for _, e in results if 'queued behind them and goes with them' in e]
    check('7 live writers (--strict) with a backlog of 1: all exit 0; one of them replays the backlog, plus any write'
          ' queued behind it meanwhile', all(rc == 0 for rc, _ in results) and len(replayers) == 1
          and f'replayed {1 + len(behind)} queued writes' in replayers[0], results)
    check('...and every write lands: the backlog, then each live value', all(t['percent'] == 10 * t['seq'] + 5 for t in r['tasks'])
          and not spool_file(URL).exists(), ([t['percent'] for t in r['tasks']], records(URL)))
    srv.stop()


def connect_vs_read():
    S.section('connect failures are queued; a read timeout is not (the board may have got it)')
    silent = socket.socket()
    silent.bind(('127.0.0.1', 0))
    silent.listen(8)
    URL = f'http://127.0.0.1:{silent.getsockname()[1]}'
    held, stop = [], threading.Event()

    def acceptor():
        silent.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(silent.accept()[0])  # read nothing, answer nothing
            except OSError:
                pass
    threading.Thread(target=acceptor, daemon=True).start()
    try:
        rc, out, err = tc('--timeout', '1', '--url', URL, 'progress', 'k3m9qa-1', '5', 'x')
        check('a read timeout: soft, "no reply within 1s; not queued"', rc == 0 and 'no reply within 1s' in err
              and 'not queued, as the board may have got it' in err and not spool_file(URL).exists(), (rc, err))
        rc, out, err = tc('--timeout', '1', '--url', URL, 'new', 'Maybe created', '-t', 'a')
        check('a read timeout on new: offline ids (it may exist; nothing queued)', rc == 0 and out == 'offline\noffline\n'
              and not spool_file(URL).exists(), (rc, out, err))
        rc, out, err = tc('--strict', '--timeout', '1', '--url', URL, 'done', 'k3m9qa-1')
        check('a read timeout, --strict: exit 3, nothing queued', rc == 3 and not spool_file(URL).exists(), (rc, err))
    finally:
        stop.set()
        silent.close()
        for c in held:
            c.close()
    full = socket.socket()
    full.bind(('127.0.0.1', 0))
    full.listen(0)
    URL = f'http://127.0.0.1:{full.getsockname()[1]}'
    fillers = []
    for _ in range(4):  # a full accept queue drops the SYN, so connect() itself times out
        s = socket.socket()
        s.setblocking(False)
        with contextlib.suppress(BlockingIOError, OSError):
            s.connect(full.getsockname())
        fillers.append(s)
    time.sleep(0.3)
    rc, out, err = tc('--timeout', '1', '--url', URL, 'start', 'k3m9qa-1', 'x')
    if 'no connection within 1s' in err:
        check('a connect timeout: "no connection within 1s", queued', rc == 0 and '(1 queued)' in err and spool_file(URL).exists(), (rc, err))
    else:
        S.skip('a connect timeout is queued', 'this kernel accepted the connection anyway: ' + err.strip()[-120:])
    full.close()
    for s in fillers:
        s.close()
    rc, out, err = tc('--url', 'http://no-such-host.invalid:1', 'start', 'k3m9qa-1', 'x')
    check('a DNS failure: queued', rc == 0 and 'queued for replay (1 queued)' in err
          and spool_file('http://no-such-host.invalid:1').exists(), (rc, err))
    rc, out, err = tc('--url', 'http://127.0.0.1:1', 'start', 'k3m9qa-1', 'x')
    check('connection refused: queued', rc == 0 and 'queued for replay' in err, (rc, err))
    rc, out, err = tc('--url', '://127.0.0.1:1', 'start', 'k3m9qa-1', 'x')
    check('an unusable URL: not queued, one warning', rc == 0 and 'queued' not in err and err.count('warning:') == 1, (rc, err))


class Refusing(BaseHTTPRequestHandler):
    """A board that answers every write with 503 (its database busy, say)."""
    seen = []

    def do_POST(self):
        self.seen.append((self.path, self.headers.get('X-Tasks-Replay')))
        self.rfile.read(int(self.headers.get('Content-Length') or 0))
        body = b'{"error": "database busy"}'
        self.send_response(503)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST

    def log_message(self, *args):
        pass


def replay_outcomes():
    S.section('how a replay ends: a 5xx keeps the rest, a 4xx drops a write, an existing create is fine')
    stub = ThreadingHTTPServer(('127.0.0.1', 0), Refusing)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    URL = f'http://127.0.0.1:{stub.server_address[1]}'
    try:
        spool_file(URL).parent.mkdir(parents=True, exist_ok=True)
        now = time.time()
        lines = [{'method': 'POST', 'path': f'/api/tasks/k3m9qa-{i}/progress', 'body': {'message': f'm{i}'}, 'at': now, 'queued_at': now}
                 for i in (1, 2)]
        spool_file(URL).write_text(''.join(json.dumps(x) + '\n' for x in lines))
        rc, out, err = tc('--url', URL, 'flush')
        check('a 503: the replay stops at once and keeps both writes', rc == 0 and len(records(URL)) == 2 and len(Refusing.seen) == 1
              and '2 still queued' in err and 'replayed' not in err, (rc, err, Refusing.seen))
        check('the replay carried X-Tasks-Replay: 1', Refusing.seen == [('/api/tasks/k3m9qa-1/progress', '1')], Refusing.seen)
    finally:
        stub.shutdown()
        stub.server_close()

    P = free_port()
    URL = f'http://127.0.0.1:{P}'
    srv = board(P, 'outcomes', fresh=True)
    try:
        code, made, _ = api(URL, 'POST', '/api/requests', {'id': 'zz22zz', 'title': 'Theirs', 'tasks': ['their task']})
        check('someone else made request zz22zz', code == 201, made)
        code, own, _ = api(URL, 'POST', '/api/requests', {'title': 'Ours', 'tasks': ['a', 'b']})
        own_1, own_2 = (t['id'] for t in own['tasks'])
        code, same, _ = api(URL, 'POST', '/api/requests', {'id': 'yy33yy', 'title': 'Same', 'tasks': ['x']})
        spool_file(URL).parent.mkdir(parents=True, exist_ok=True)
        now = time.time()

        def rec(method, path, body):
            return json.dumps({'method': method, 'path': path, 'body': body, 'at': now, 'queued_at': now})
        spool_file(URL).write_text('\n'.join([
            rec('POST', '/api/requests', {'id': 'zz22zz', 'title': 'Mine', 'tasks': ['my task']}),
            '{not json',
            rec('POST', '/api/tasks/zz22zz-1/progress', {'message': 'mine', 'percent': 50}),
            rec('POST', '/api/requests', {'id': 'yy33yy', 'title': 'Same', 'tasks': ['x']}),
            rec('POST', '/api/tasks/yy33yy-1/progress', {'message': 'after the existing create'}),
            rec('POST', f'/api/tasks/{own_1}/progress', {'message': 'kept', 'percent': 33}),
            rec('POST', f'/api/tasks/{own_2}/complete', {'status': 'done'}),
            rec('POST', f'/api/tasks/{own_2}/attention', {'message': 'too late: 409'}),
        ]) + '\n')
        rc, out, err = tc('--url', URL, 'flush')
        theirs = api(URL, 'GET', '/api/requests/zz22zz')[1]
        t = api(URL, 'GET', f'/api/tasks/{own_2}')[1]
        t1 = api(URL, 'GET', f'/api/tasks/{own_1}')[1]
        y = api(URL, 'GET', '/api/tasks/yy33yy-1')[1]
        check('an unreadable line is skipped with a warning', 'skipped 1 unreadable queued writes' in err, err)
        check("a refused create (409 id clash) drops it and every write for that request: theirs is untouched",
              '409' in err and 'which was never created' in err and theirs['title'] == 'Theirs'
              and theirs['tasks'][0]['status'] == 'pending', (err, theirs))
        check('an existing create (the same id and title) counts as replayed; the writes after it land', y['message'] == 'after the existing create'
              and y['status'] == 'running', y)
        check('a 4xx on a later write (attention on a done task) drops just that one', t['status'] == 'done' and t['attention'] is None
              and t1['message'] == 'kept' and t1['percent'] == 33, (t, t1))
        check('"replayed 4 queued writes, dropped 3"; the spool is gone', rc == 0 and 'replayed 4 queued writes, dropped 3' in err
              and not spool_file(URL).exists(), err)

        S.section('a refused create stays refused: no later command sends a write for that request')
        dead = spool_file(URL).with_suffix('.dead')
        lines = dead.read_text().split() if dead.exists() else []
        check('the refused id is remembered beside the queue (spool-<hash>.dead: id and time, mode 0600)', lines[:1] == ['zz22zz']
              and abs(float(lines[1]) - time.time()) < 60 and stat.S_IMODE(dead.stat().st_mode) == 0o600, lines)
        for argv in (('done', 'zz22zz-1', 'ok'), ('progress', 'zz22zz-1', '50', 'mine'), ('ask', 'zz22zz', 'Mine?'),
                     ('fail', 'zz22zz', 'mine failed')):
            rc, out, err = tc('--url', URL, *argv)
            check(f'{argv[0]} {argv[1]}: not sent, one warning, exit 0', rc == 0 and err.count('warning:') == 1
                  and 'request zz22zz was never created' in err, (rc, err))
        rc, out, err = tc('--url', URL, 'add', 'zz22zz', 'my extra step')
        check('add zz22zz: not sent either (offline for its id)', rc == 0 and out == 'offline\n' and 'never created' in err, (rc, out, err))
        rc, out, err = tc('--strict', '--url', URL, 'done', 'zz22zz-1')
        check('--strict: exit 1 (refused, not unreachable)', rc == 1 and 'never created' in err, (rc, err))
        theirs = api(URL, 'GET', '/api/requests/zz22zz')[1]
        check('theirs is still untouched: one pending task, running, no question', theirs['status'] == 'running'
              and [t['status'] for t in theirs['tasks']] == ['pending'] and theirs['attention'] is None, theirs)
        rc, out, err = tc('--url', URL, 'show', 'zz22zz')
        check('reading it is fine', rc == 0 and 'zz22zz' in out and 'warning' not in err, (rc, out, err))

        code, _, _ = api(URL, 'POST', '/api/requests', {'id': 'ww44ww', 'title': 'Theirs too', 'tasks': ['their step']})
        spool_file(URL).write_text(rec('POST', '/api/requests', {'id': 'ww44ww', 'title': 'Mine too', 'tasks': ['my step']}) + '\n')
        rc, out, err = tc('--url', URL, 'done', 'ww44ww-1', 'ok')
        w = api(URL, 'GET', '/api/tasks/ww44ww-1')[1]
        check("the command whose replay found the clash does not send its own write either", code == 201 and rc == 0
              and 'dropped 1' in err and 'request ww44ww was never created' in err and w['status'] == 'pending', (err, w))
        check('both refused ids are remembered', sorted(line.split()[0] for line in dead.read_text().splitlines()) == ['ww44ww', 'zz22zz'],
              dead.read_text())
        old = time.time() - 8 * 86400
        dead.write_text(f'zz22zz {old:.0f}\nww44ww {time.time():.0f}\n')
        rc, out, err = tc('--strict', '--url', URL, 'progress', 'zz22zz-1', '10', 'a week later')
        check('after 7 days a refused id is forgotten (the write goes out; the board decides)', rc == 0
              and api(URL, 'GET', '/api/tasks/zz22zz-1')[1]['percent'] == 10, (rc, err))
    finally:
        srv.stop()


class SlowReplays(BaseHTTPRequestHandler):
    """A board that takes REPLAY_DELAY to answer each replayed write and answers everything else at once."""
    seen = []
    lock = threading.Lock()

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get('Content-Length') or 0)) or b'{}')
        replay = self.headers.get('X-Tasks-Replay') == '1'
        if replay:
            time.sleep(REPLAY_DELAY)
        with self.lock:
            self.seen.append((self.path, body.get('percent'), replay, time.time()))
        self.answer({'id': self.path.split('/')[3], 'status': 'running', 'percent': body.get('percent')})

    def do_GET(self):
        self.answer({'id': self.path.rsplit('/', 1)[-1], 'title': 'slow', 'status': 'running', 'tasks': [], 'now': time.time()})

    def answer(self, payload):
        data = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args):
        pass


REPLAY_DELAY = 1.5


@contextlib.contextmanager
def holding(path):
    """Another process's lock on `path` (this test process stands in for it)."""
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        yield
    finally:
        os.close(fd)


def timed(*argv, env=None):
    t0 = time.monotonic()
    rc, out, err = tc(*argv, env=env)
    return rc, out, err, time.monotonic() - t0


def nobody_waits():
    S.section('one process replays; nobody waits for it: reads go direct, writes queue behind the replay')
    stub = ThreadingHTTPServer(('127.0.0.1', 0), SlowReplays)
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    URL = f'http://127.0.0.1:{stub.server_address[1]}'
    rid = 'k7m7qa'
    try:
        spool_file(URL).parent.mkdir(parents=True, exist_ok=True)
        now = time.time() - 60
        spool_file(URL).write_text(json.dumps({'method': 'POST', 'path': f'/api/tasks/{rid}-1/progress',
                                               'body': {'message': 'backlog', 'percent': 10}, 'at': now, 'queued_at': now}) + '\n')
        plan = {1: 60, 2: 20, 3: 30, 4: 40, 5: 50, 6: 70}
        t0 = time.monotonic()
        procs = {i: subprocess.Popen([str(WRAPPER), '--url', URL, 'progress', f'{rid}-{i}', str(pct), f'job {i}'], stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, text=True, env=ENV, cwd=TMP) for i, pct in plan.items()}
        took, errs = {}, {}
        while len(took) < len(procs) and time.monotonic() - t0 < 60:
            for i, p in procs.items():
                if i not in took and p.poll() is not None:
                    took[i], errs[i] = time.monotonic() - t0, p.stderr.read()
            time.sleep(0.02)
        replayer = [i for i, e in errs.items() if 'replayed' in e]
        others = sorted(t for i, t in took.items() if i not in replayer)
        check('6 parallel writers while the backlog replays slowly (1.5s a write): one replays, and it takes that long',
              len(replayer) == 1 and took[replayer[0]] >= REPLAY_DELAY, (took, errs))
        check('the other 5 never wait for it: each is done in well under one replayed write', len(others) == 5
              and others[-1] < REPLAY_DELAY * 0.8 and all(procs[i].returncode == 0 for i in took), (took, errs))
        behind = [i for i, e in errs.items() if 'queued behind them and goes with them' in e]
        check('...those that found the replay going queued behind it (a note, not a warning)', behind
              and all('warning' not in errs[i] for i in behind), errs)
        paths = [(path.split('/')[3], pct, replay) for path, pct, replay, _ in SlowReplays.seen]
        check('every write reached the board once: the backlog and all 6', sorted(p[0] for p in paths) == sorted(
              [f'{rid}-1'] + [f'{rid}-{i}' for i in plan]), paths)
        last = [pct for tid, pct, _ in paths if tid == f'{rid}-1']
        check('in order: the backlog (10) before the live value for the same task (60)', last == [10, 60], paths)
        check('the queue is empty afterwards', not spool_file(URL).exists(), records(URL))

        S.section('a stuck replay (its lock held): nothing blocks')
        spool_file(URL).write_text(json.dumps({'method': 'POST', 'path': f'/api/tasks/{rid}-2/progress',
                                               'body': {'message': 'old', 'percent': 5}, 'at': now, 'queued_at': now}) + '\n')
        SlowReplays.seen.clear()
        with holding(str(spool_file(URL)) + '.replay.lock'):
            rc, out, err, dt = timed('--url', URL, 'show', rid)
            check('show: straight to the board, at once, no warning', rc == 0 and out.startswith(rid) and 'warning' not in err
                  and dt < 1.5, (rc, out, err, dt))
            rc, out, err, dt = timed('--strict', '--url', URL, 'progress', f'{rid}-3', '33', 'behind a stuck replay')
            check('a write: queued behind it at once, exit 0 even with --strict, a note', rc == 0 and dt < 1.5
                  and 'taskctl: another taskctl is replaying the queued writes' in err and '(2 queued)' in err
                  and 'warning' not in err, (rc, err, dt))
            rc, out, err, dt = timed('--url', URL, 'flush')
            check('flush: says another taskctl is replaying, exit 0', rc == 0 and dt < 1.5
                  and f'another taskctl is replaying the queue for {URL} right now (2 queued)' in err, (rc, err))
            check('nothing was sent meanwhile', SlowReplays.seen == [], SlowReplays.seen)
        rc, out, err = tc('--url', URL, 'flush')
        check('once it lets go, flush replays both, in order', rc == 0 and 'replayed 2 queued writes' in err
              and [(p.split('/')[3], pct) for p, pct, _, _ in SlowReplays.seen] == [(f'{rid}-2', 5), (f'{rid}-3', 33)], (err, SlowReplays.seen))

        S.section('a stuck queue file lock: waited for about a second, then the board directly')
        spool_file(URL).write_text(json.dumps({'method': 'POST', 'path': f'/api/tasks/{rid}-4/progress',
                                               'body': {'message': 'old', 'percent': 5}, 'at': now, 'queued_at': now}) + '\n')
        SlowReplays.seen.clear()
        with holding(str(spool_file(URL)) + '.lock'):
            rc, out, err, dt = timed('--url', URL, 'show', rid)
            check('show: one warning (cannot replay), then its answer, within a few seconds', rc == 0 and out.startswith(rid)
                  and err.count('warning:') == 1 and 'cannot replay the queued writes' in err and dt < 4, (rc, out, err, dt))
            rc, out, err, dt = timed('--url', URL, 'progress', f'{rid}-5', '55', 'past a stuck lock')
            check('a write: one warning, then sent live, within a few seconds', rc == 0 and dt < 4 and err.count('warning:') == 1
                  and (f'/api/tasks/{rid}-5/progress', 55, False) in [s[:3] for s in SlowReplays.seen], (rc, err, dt))
        check('the queued write waited for a replay that could lock the queue', len(records(URL)) == 1, records(URL))
    finally:
        stub.shutdown()
        stub.server_close()


def paths():
    S.section('where the queue lives: TASKS_SPOOL_DIR, XDG_CACHE_HOME, ~/.cache, the private temp fallback')
    URL = f'http://127.0.0.1:{free_port()}'
    xdg = TMP / 'xdg'
    rc, out, err = tc('--url', URL, 'start', 'k3m9qa-1', 'x', env=dict(ENV, TASKS_SPOOL_DIR='', XDG_CACHE_HOME=str(xdg)))
    check('no TASKS_SPOOL_DIR: ${XDG_CACHE_HOME}/taskctl', rc == 0 and spool_file(URL, xdg / 'taskctl').exists(), (err, list(xdg.rglob('*'))))
    env = {k: v for k, v in ENV.items() if k not in ('TASKS_SPOOL_DIR', 'XDG_CACHE_HOME')}
    rc, out, err = tc('--url', URL, 'start', 'k3m9qa-1', 'x', env=env)
    check('neither: ~/.cache/taskctl', rc == 0 and spool_file(URL, HOME / '.cache' / 'taskctl').exists(), err)
    other = f'http://127.0.0.1:{free_port()}'
    tc('--url', other, 'start', 'k3m9qa-1', 'x')
    check('one spool file per board URL', spool_file(other).exists() and not spool_file(URL).exists()
          and len(records(other)) == 1, sorted(os.listdir(SPOOL)))
    ro = TMP / 'ro'
    ro.mkdir()
    tmpd = TMP / 'tmpd'
    tmpd.mkdir()
    fb_env = dict(ENV, TASKS_SPOOL_DIR=str(ro / 'spool'), TMPDIR=str(tmpd))
    ro.chmod(0o555)
    try:
        rc, out, err = tc('--url', URL, 'start', 'k3m9qa-1', 'x', env=fb_env)
        fb = tmpd / f'taskctl-{os.getuid()}'
        check('an unwritable TASKS_SPOOL_DIR: queued in <tmp>/taskctl-<uid>, a private dir', rc == 0 and '(1 queued)' in err
              and spool_file(URL, fb).exists() and stat.S_IMODE(fb.stat().st_mode) == 0o700, (rc, err, list(tmpd.rglob('*'))))
        rc, out, err = tc('--url', URL, 'ping', env=fb_env)
        check('ping finds the fallback queue', f'taskctl: 1 queued (writes waiting for the board, in {spool_file(URL, fb)}' in err, err)
    finally:
        ro.chmod(0o755)
    evil = TMP / 'tmpd2'
    evil.mkdir()
    (evil / f'taskctl-{os.getuid()}').symlink_to(TMP / 'elsewhere')
    (TMP / 'elsewhere').mkdir()
    ro.chmod(0o555)
    try:
        rc, out, err = tc('--url', URL, 'start', 'k3m9qa-1', 'x', env=dict(fb_env, TMPDIR=str(evil)))
    finally:
        ro.chmod(0o755)
    check('a planted symlink as the fallback dir is refused: nothing written through it, one warning', rc == 0
          and not any((TMP / 'elsewhere').iterdir()) and 'could not queue it' in err and err.count('warning:') == 1, (rc, err))


if __name__ == '__main__':
    offline_then_replay()
    parallel()
    connect_vs_read()
    replay_outcomes()
    nobody_waits()
    paths()
    sys.exit(S.finish())
