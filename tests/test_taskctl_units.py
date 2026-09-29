#!/usr/bin/env python3
"""taskctl.py unit tests: parsers, formatters, id handling, option splitting, the rule merge, the run
monitor, timeout/URL resolution and error handling, and the v2 pieces: the offline queue's records,
coalescing and paths, connect-vs-sent error classification, the changelog's "what's new", the waiting
lines, client-made ids; imported straight from the repo (no bytecode)."""
from __future__ import annotations

import contextlib
import errno
import hashlib
import http.client
import importlib.util
import io
import os
import random
import re
import socket
import sys
import tempfile
import time
import urllib.error
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the checkout, even when run without run_all.sh
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from harness import TASKCTL_PY, Suite  # noqa: E402

S = Suite('taskctl_units')
spec = importlib.util.spec_from_file_location('taskctl', TASKCTL_PY)
tc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tc)


def eq(label, got, want):
    S.check(label, got == want, f'got={got!r} want={want!r}')


def usage(fn, *a):
    try:
        fn(*a)
    except tc.UsageError:
        return 'UsageError'
    return 'no error'


@contextlib.contextmanager
def environ(**values):
    saved = {k: os.environ.get(k) for k in values}
    for k, v in values.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def captured(fn, *a):
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        try:
            value = fn(*a)
        except tc.UsageError:
            value = 'UsageError'
    return value, err.getvalue()


S.section('parse_duration')
for text, want in [('90', 90), ('90s', 90), ('5m', 300), ('5min', 300), ('5 min', 300), ('~5m', 300),
                   ('1h30m', 5400), ('1h 30m', 5400), ('2h', 7200), ('1:30', 90), ('1:30:00', 5400), ('10:05', 605),
                   ('0', 0), ('2.5m', 150), ('45sec', 45), ('3 minutes', 180), ('1d', 86400), ('2hrs', 7200),
                   (' ~ 10m ', 600)]:
    eq(f'duration {text!r}', tc.parse_duration(text), want)
for bad in ['', 'abc', '5x', '1:2:3:4', '-5m', 'm5', '5m foo']:
    eq(f'duration {bad!r} is a usage error', usage(tc.parse_duration, bad), 'UsageError')


def old_clock(s):
    """The original 3-group H:M[:S] parser the clock branch replaced; they must agree."""
    m = re.fullmatch(r'(\d+):(\d{1,2})(?::(\d{1,2}))?', s)
    if not m:
        return None
    seconds = 0
    for part in m.groups():
        seconds = seconds * 60 + int(part) if part is not None else seconds
    return seconds


rng = random.Random(7)
alphabet = '0123456789:: ~٣²m.'
diffs = []
for _ in range(20000):
    s = ''.join(rng.choice(alphabet) for _ in range(rng.randint(1, 9)))
    t = s.strip().lower().lstrip('~').strip()
    if re.fullmatch(r'\d+(?:\.\d+)?', t):
        continue
    old = old_clock(t)
    try:
        new = tc.parse_duration(s)
    except tc.UsageError:
        new = None
    if (old is not None and new != old) or (old is None and new is not None and not tc.DURATION_RE.fullmatch(t)):
        diffs.append((s, old, new))
S.check('clock durations: 20000 random strings agree with the reference parser', not diffs, diffs[:5])

S.section('parse_percent')
for text, want in [('45', 45.0), ('45.5', 45.5), ('45%', 45.0), (' 45 % ', 45.0), ('0', 0.0), ('100', 100.0),
                   ('150', 100.0), ('-5', 0.0), ('33.333', 33.3)]:
    eq(f'percent {text!r}', captured(tc.parse_percent, text)[0], want)  # (out-of-range values warn on stderr)
for bad in ['abc', 'nan', 'inf', '', '%']:
    eq(f'percent {bad!r} is a usage error', usage(tc.parse_percent, bad), 'UsageError')

S.section('formatters')
for sec, want in [(0, '0s'), (41, '41s'), (59.6, '1m00s'), (192, '3m12s'), (3600, '1h00m'), (3725, '1h02m'),
                  (90000, '1d01h'), (None, '0s')]:
    eq(f'fmt_dur {sec!r}', tc.fmt_dur(sec), want)
for p, want in [(45.0, '45%'), (45.5, '45.5%'), (100.0, '100%'), (0, '0%'), (None, '0%'), (10.0, '10%')]:
    eq(f'fmt_pct {p!r}', tc.fmt_pct(p), want)
t = {'id': 'k3m9qa-2', 'status': 'running', 'percent': 45.0, 'title': 'Build', 'message': 'Compiling\nshaders',
     'eta_at': 1000.0, 'eta_remaining': 240.0, 'elapsed': 362.0, 'start_inferred': False, 'stale': False}
eq('task_line running with eta', tc.task_line(t, 760.0), 'running      45%    6m02s  k3m9qa-2  Build: Compiling shaders  (eta ~4m00s)')
t.update(eta_remaining=0.0)
eq('task_line overdue', tc.task_line(t, 1035.0), 'running      45%    6m02s  k3m9qa-2  Build: Compiling shaders  (overdue 35s)')
eq('task_line pending', tc.task_line({'id': 'k3m9qa-3', 'status': 'pending', 'percent': 0.0, 'title': 'Deploy', 'message': None,
                                      'eta_at': None, 'eta_remaining': None, 'elapsed': None}, 1.0),
   'pending       0%        -  k3m9qa-3  Deploy')
eq('request_line stale', tc.request_line({'id': 'k3m9qa', 'status': 'running', 'percent': 38.3, 'tasks_done': 2, 'tasks_total': 5,
                                          'elapsed': 724, 'title': 'Build the app', 'message': None, 'origin': 'host:repo',
                                          'stale': True}),
   'k3m9qa  stale    38.3%  2/5 done    12m04s  Build the app  (host:repo)')

S.section('ids')
eq('classify request', tc.classify(' K3M9QA '), ('k3m9qa', 'request'))
eq('classify task', tc.classify('k3m9qa-12'), ('k3m9qa-12', 'task'))
eq('classify offline', tc.classify('OFFLINE'), ('offline', 'offline'))
for bad in ['k3m9q', 'k3m9qa1', 'k3m9qa-', 'k3m9ql', 'k3m9qa-1-2', 'k3m9 qa']:
    eq(f'classify {bad!r} is a usage error', usage(tc.classify, bad), 'UsageError')
eq('need a task id, given a request id', usage(tc.need_id, 'k3m9qa', 'task'), 'UsageError')
eq('need a request id, given a task id', usage(tc.need_id, 'k3m9qa-1', 'request'), 'UsageError')

S.section('split_globals')
opts, rest = tc.split_globals(['new', 'T', '--url', 'http://x:1', '-t', 'a', '--strict', '--timeout=2'])
eq('global options anywhere', opts, {'url': 'http://x:1', 'timeout': '2', 'strict': True})
eq('the rest in order', rest, ['new', 'T', '-t', 'a'])
opts, rest = tc.split_globals(['run', 'k3m9qa-1', '--', 'cmd', '--strict', '--url', 'u'])
eq('stops at --', (opts['strict'], opts['url'], rest), (False, None, ['run', 'k3m9qa-1', '--', 'cmd', '--strict', '--url', 'u']))
eq('missing value is a usage error', usage(tc.split_globals, ['ping', '--url']), 'UsageError')

S.section('resolve_timeout: --timeout > TASKS_TIMEOUT (bad value warns, uses 3s) > 3s')
for env, flag, want, warns in [(None, None, 3.0, 0), ('5s', None, 5.0, 0), ('0.5', None, 0.5, 0), ('2m', None, 120.0, 0),
                               ('1:30', None, 90.0, 0), ('abc', None, 3.0, 1), ('0', None, 3.0, 1), ('-1', None, 3.0, 1),
                               ('nan', None, 3.0, 1), ('inf', None, 3.0, 1), ('0s', None, 3.0, 1), ('', None, 3.0, 0),
                               (None, '5s', 5.0, 0), (None, '2.5', 2.5, 0), (None, 'abc', 'UsageError', 0),
                               (None, '0', 'UsageError', 0), (None, 'nan', 'UsageError', 0), ('abc', '2', 2.0, 0),
                               ('abc', 'x', 'UsageError', 0)]:
    with environ(TASKS_TIMEOUT=env):
        got, err = captured(tc.resolve_timeout, flag)
    S.check(f'resolve_timeout env={env!r} flag={flag!r} -> {want!r}, {warns} warning(s)',
            got == want and err.count('warning: TASKS_TIMEOUT') == warns, (got, err))

S.section('resolve_url: --url > TASKS_URL > baked > default')
for env, flag, want in [(None, None, tc.DEFAULT_URL), ('  ', None, tc.DEFAULT_URL), (None, ' host:1/ ', 'http://host:1'),
                        ('x:2', None, 'http://x:2'), ('http://a:3/', 'http://b:4', 'http://b:4'), ('', '', tc.DEFAULT_URL)]:
    with environ(TASKS_URL=env):
        eq(f'resolve_url env={env!r} flag={flag!r}', tc.resolve_url(flag), want)
S.check('the repo file is unrendered, so the baked URL is skipped', tc.BAKED_URL.startswith('{{'), tc.BAKED_URL)

S.section('Board.call with an unusable URL is "unreachable", never a crash')
for base in ('://127.0.0.1:1', 'http://[bad', 'nope://x'):
    try:
        tc.Board(base, 1.0, False).call('GET', '/api/health')
        got = 'no error'
    except tc.BoardError as e:
        got = 'unreachable' if e.unreachable else 'rejected'
    except Exception as e:  # noqa: BLE001
        got = type(e).__name__
    eq(f'Board({base!r}).call', got, 'unreachable')

S.section('merge_rule')
BLOCK = '<!-- task-status:begin -->\nrule v1\n<!-- task-status:end -->'
BLOCK2 = '<!-- task-status:begin -->\nrule v2\nline 2\n<!-- task-status:end -->'
eq('into an empty file', tc.merge_rule('', BLOCK), (BLOCK + '\n', False))
eq('appended after one blank line', tc.merge_rule('# mine\nkeep me\n\n\n', BLOCK), ('# mine\nkeep me\n\n' + BLOCK + '\n', False))
text = 'top\n\n' + BLOCK + '\nbottom\n'
eq('replaced in place', tc.merge_rule(text, BLOCK2), ('top\n\n' + BLOCK2 + '\nbottom\n', True))
eq('idempotent', tc.merge_rule(text, BLOCK), (text, True))
eq('duplicate blocks collapse into one', tc.merge_rule('a\n' + BLOCK + '\nb\n' + BLOCK + '\nc\n', BLOCK2),
   ('a\n' + BLOCK2 + '\nb\nc\n', True))
eq('block at EOF without a newline', tc.merge_rule('a\n' + BLOCK, BLOCK), ('a\n' + BLOCK + '\n', True))
crlf = 'x\r\n<!-- task-status:begin -->\r\nold\r\n<!-- task-status:end -->\r\ny\r\n'
eq('CRLF file keeps its other lines', tc.merge_rule(crlf, BLOCK), ('x\r\n' + BLOCK + '\ny\r\n', True))
eq('an inline mention is not a marker', tc.merge_rule('see `<!-- task-status:begin -->`\n', BLOCK)[1], False)
try:
    tc.merge_rule('a\n<!-- task-status:begin -->\nb\n', BLOCK)
    eq('dangling begin marker raises', 'no error', 'ValueError')
except ValueError:
    eq('dangling begin marker raises', 'ValueError', 'ValueError')

S.section('RunMonitor')
m = tc.RunMonitor(None, 'k3m9qa-1', 1, tc.re.compile(r'(\d+)/(\d+)'), 'start')
m.record([b'\x1b[32mBuilding\x1b[0m 3/12 files\t done', b'', b'   '])
eq('record strips ANSI and tabs, skips blank lines', m.line, 'Building 3/12 files done')
eq('record percent from a/b', m.percent, 25.0)
m.record([b'no numbers here'])
eq('record keeps the last percent', (m.line, m.percent), ('no numbers here', 25.0))
m.record([b'x' * 500])
eq('record caps the line at 200', len(m.line), 200)
m.record([b'0/0 weird'])
eq('record ignores a zero total', m.percent, 25.0)
m1 = tc.RunMonitor(None, 't', 1, tc.re.compile(r'(\d+(?:\.\d+)?)%'), 'start')
m1.record([b'progress 45.5% done'])
eq('record percent from 1 group', m1.percent, 45.5)
m1.record([b'progress 450% done'])
eq('record percent clamps', m1.percent, 100.0)
m2 = tc.RunMonitor(None, tc.OFFLINE, 1, None, 'x')
_, err = captured(m2.quietly, 'progress', {'message': 'x'})
eq('quietly with the offline id sends nothing', err, '')


class Exploding:
    def call(self, *a, **k):
        raise RuntimeError('boom')


m3 = tc.RunMonitor(Exploding(), 'k3m9qa-1', 1, None, 'x')
_, err = captured(m3.quietly, 'progress', {'message': 'x'})
_, err2 = captured(m3.quietly, 'complete', {'status': 'done'})
S.check('quietly swallows an unforeseen exception and warns once', 'boom' in err and err2 == '', (err, err2))


S.section('v2 formatters: waiting wins over stale in show and list')
eq('task_line waiting', tc.task_line(dict(t, eta_remaining=None, eta_at=None, stale=True, attention={'message': 'q', 'since': 1.0}), 1.0),
   'waiting      45%    6m02s  k3m9qa-2  Build: Compiling shaders')
eq('request_line waiting', tc.request_line({'id': 'k3m9qa', 'status': 'running', 'percent': 38.3, 'tasks_done': 2, 'tasks_total': 5,
                                            'elapsed': 724, 'title': 'Build the app', 'message': None, 'origin': None,
                                            'stale': True, 'waiting': True}),
   'k3m9qa  waiting  38.3%  2/5 done    12m04s  Build the app')
eq('waiting_lines with a question (one line)', tc.waiting_lines({'attention': {'message': 'Which\nbranch?', 'since': 1}}, '    '),
   ['    waiting: Which branch?'])
eq('waiting_lines without', (tc.waiting_lines({'attention': None}, ''), tc.waiting_lines({}, '')), ([], []))

S.section('client-made ids and baked values')
ids = {tc.new_request_id() for _ in range(300)}
S.check('new_request_id: 6 chars of the alphabet, and random', all(tc.REQUEST_RE.fullmatch(i) for i in ids) and len(ids) > 290, sorted(ids)[:5])
eq('baked: an unrendered value is None', (tc.baked('{{DOCS_VERSION}}'), tc.baked('abc123def456')), (None, 'abc123def456'))
S.check('the repo file carries the three unrendered baked lines', tc.BAKED_URL == '{{BASE_URL}}' and tc.BAKED_DOCS == '{{DOCS_VERSION}}'
        and tc.BAKED_API == '{{VERSION}}', (tc.BAKED_URL, tc.BAKED_DOCS, tc.BAKED_API))

S.section('queued records: valid_record, write_target, coalesce')


def rec(method, path, body=None, at=1.0):
    return {'method': method, 'path': path, 'body': body, 'at': at, 'queued_at': at}


for label, record, want in [('a progress', rec('POST', '/api/tasks/k3m9qa-1/progress', {'message': 'x'}), True),
                            ('a DELETE without a body', rec('DELETE', '/api/requests/k3m9qa/attention'), True),
                            ('a GET', rec('GET', '/api/requests'), False), ('a path outside /api/', rec('POST', '/x'), False),
                            ('a list body', rec('POST', '/api/requests', [1]), False), ('at true', rec('POST', '/api/requests', {}, True), False),
                            ('at NaN', rec('POST', '/api/requests', {}, float('nan')), False), ('at a string', rec('POST', '/api/requests', {}, '1'), False),
                            ('not a dict', ['POST'], False)]:
    eq(f'valid_record: {label}', tc.valid_record(record), want)
for record, want in [(rec('POST', '/api/tasks/k3m9qa-2/progress'), (('tasks', 'k3m9qa-2'), 'progress')),
                     (rec('POST', '/api/requests/k3m9qa/complete'), (('requests', 'k3m9qa'), 'complete')),
                     (rec('DELETE', '/api/tasks/k3m9qa-2/attention'), (('tasks', 'k3m9qa-2'), 'attention')),
                     (rec('POST', '/api/requests', {'id': 'k3m9qa', 'title': 'x'}), (('requests', 'k3m9qa'), 'create')),
                     (rec('POST', '/api/requests', {'title': 'x'}), (None, None)),
                     (rec('POST', '/api/requests/k3m9qa/tasks', {'title': 'x'}), (None, None))]:
    eq(f"write_target {record['path']}", tc.write_target(record), want)
R, T1, T2 = 'k3m9qa', 'k3m9qa-1', 'k3m9qa-2'
recs = [rec('POST', '/api/requests', {'id': R, 'title': 'x'}), rec('POST', f'/api/tasks/{T1}/progress', {'message': 'a'}),
        rec('POST', f'/api/tasks/{T1}/progress', {'message': 'b'}), rec('POST', f'/api/tasks/{T1}/complete', {'status': 'done'}),
        rec('POST', f'/api/tasks/{T2}/progress', {'message': 'c'}), rec('POST', f'/api/requests/{R}/attention', {'message': 'q1'}),
        rec('DELETE', f'/api/requests/{R}/attention'), rec('POST', f'/api/tasks/{T2}/attention', {'message': 'q2'}),
        rec('POST', f'/api/tasks/{T2}/progress', {'message': 'd'}), rec('POST', f'/api/tasks/{T1}/progress', {'message': 'late'}),
        rec('POST', f'/api/requests/{R}/complete', {'status': 'done'})]
got = [(r['method'], r['path'].replace('/api/', ''), (r['body'] or {}).get('message') or (r['body'] or {}).get('status'))
       for r in tc.coalesce(recs)]
eq('coalesce: order kept; a progress superseded by a later write on its task, an attention by a later attention, dropped', got, [
    ('POST', 'requests', None), ('POST', f'tasks/{T1}/complete', 'done'), ('DELETE', f'requests/{R}/attention', None),
    ('POST', f'tasks/{T2}/attention', 'q2'), ('POST', f'tasks/{T2}/progress', 'd'), ('POST', f'tasks/{T1}/progress', 'late'),
    ('POST', f'requests/{R}/complete', 'done')])
eq('coalesce: a request close keeps a task progress before it', len(tc.coalesce([rec('POST', f'/api/tasks/{T1}/progress', {}),
                                                                                  rec('POST', f'/api/requests/{R}/complete', {})])), 2)
eq('coalesce: a progress on another task keeps an attention', len(tc.coalesce([rec('POST', f'/api/tasks/{T1}/attention', {}),
                                                                                rec('POST', f'/api/tasks/{T2}/progress', {})])), 2)
eq('coalesce: closes are all kept (they are not superseded)', len(tc.coalesce([rec('POST', f'/api/tasks/{T1}/complete', {}),
                                                                                rec('POST', f'/api/tasks/{T1}/complete', {})])), 2)
eq('coalesce is idempotent', tc.coalesce(tc.coalesce(recs)), tc.coalesce(recs))

S.section('where the queue lives')
with environ(TASKS_SPOOL_DIR='/x/spool', XDG_CACHE_HOME='/y'):
    eq('spool_home: TASKS_SPOOL_DIR first', tc.spool_home(), '/x/spool')
with environ(TASKS_SPOOL_DIR=None, XDG_CACHE_HOME='/y'):
    eq('spool_home: then $XDG_CACHE_HOME/taskctl', tc.spool_home(), '/y/taskctl')
with environ(TASKS_SPOOL_DIR=None, XDG_CACHE_HOME=None, HOME='/home/someone'):
    eq('spool_home: then ~/.cache/taskctl', tc.spool_home(), '/home/someone/.cache/taskctl')
with tempfile.TemporaryDirectory() as d, environ(TASKS_SPOOL_DIR=d):
    sp = tc.Spool('http://board:8765')
    eq('Spool: spool-<sha1(base url)[:10]>.jsonl', sp.path, os.path.join(d, 'spool-%s.jsonl' % hashlib.sha1(b'http://board:8765').hexdigest()[:10]))
    eq('an empty spool', (sp.pending(), sp.count(), sp.read()), (False, 0, []))
    n = sp.append(rec('POST', f'/api/tasks/{T1}/progress', {'message': 'a'}))
    n = sp.append(rec('POST', f'/api/tasks/{T1}/progress', {'message': 'b'}))
    eq('append coalesces as it goes', (n, sp.count(), sp.read()[0]['body']), (1, 1, {'message': 'b'}))
    with open(sp.path, 'a') as f:
        f.write('{broken\n')
    got, err = captured(sp.read)
    S.check('read skips an unreadable line with one warning', len(got) == 1 and err.count('skipped 1 unreadable queued writes') == 1, err)
    sp.write([])
    eq('writing nothing deletes the file', os.path.exists(sp.path), False)

S.section('Board: connect failures (queue) vs sent calls (do not)')


def classify_exc(exc):
    board = tc.Board('http://127.0.0.1:9', 1.0, False)

    def boom(*a, **k):
        raise exc
    board._opener.open = boom
    try:
        board.exchange('POST', '/api/tasks/k3m9qa-1/progress', {'message': 'x'})
    except tc.BoardError as e:
        return e.unreachable, e.connect, e.sent
    return 'no error'


for label, exc, want in [
        ('refused', urllib.error.URLError(ConnectionRefusedError(errno.ECONNREFUSED, 'refused')), (True, True, False)),
        ('DNS', urllib.error.URLError(socket.gaierror(-2, 'Name or service not known')), (True, True, False)),
        ('network unreachable', urllib.error.URLError(OSError(errno.ENETUNREACH, 'x')), (True, True, False)),
        ('host unreachable', urllib.error.URLError(OSError(errno.EHOSTUNREACH, 'x')), (True, True, False)),
        ('address not available', urllib.error.URLError(OSError(errno.EADDRNOTAVAIL, 'x')), (True, True, False)),
        ('a local firewall: EPERM', urllib.error.URLError(PermissionError(errno.EPERM, 'Operation not permitted')), (True, True, False)),
        ('a local firewall: EACCES', urllib.error.URLError(PermissionError(errno.EACCES, 'Permission denied')), (True, True, False)),
        ('connect timeout', urllib.error.URLError(socket.timeout('timed out')), (True, True, False)),
        ('reset while sending', urllib.error.URLError(ConnectionResetError(errno.ECONNRESET, 'reset')), (True, False, False)),
        ('an unknown scheme', urllib.error.URLError('unknown url type: nope'), (True, False, False)),
        ('read timeout', socket.timeout('timed out'), (True, False, True)),
        ('reset while reading', ConnectionResetError(errno.ECONNRESET, 'reset'), (True, False, True)),
        ('remote disconnected', http.client.RemoteDisconnected('closed'), (True, False, True)),
        ('a bad URL', ValueError('bad'), (True, False, False))]:
    eq(f'{label}: unreachable/connect/sent', classify_exc(exc), want)

with tempfile.TemporaryDirectory() as d, environ(TASKS_SPOOL_DIR=d):
    real_connect = socket.create_connection

    def firewalled(*a, **k):
        raise PermissionError(errno.EPERM, 'Operation not permitted')
    socket.create_connection = firewalled  # what connect() gets from an OUTPUT DROP rule or a VPN kill switch
    try:
        board = tc.Board('http://127.0.0.1:9', 1.0, False)
        try:
            board.call('POST', '/api/tasks/k3m9qa-1/progress', {'message': 'x'}, spool=True)
            got = 'no error'
        except tc.BoardError as e:
            got = (e.queued, e.unreachable, 'Operation not permitted' in str(e))
    finally:
        socket.create_connection = real_connect
    eq('a write blocked by a local firewall (EPERM from connect) is queued', (got, tc.Spool('http://127.0.0.1:9').count()),
       ((True, True, True), 1))

S.section('which board a skill belongs to: norm_url, own_board')
for url, want in [('HTTP://Board.Example:8765/', 'http://board.example:8765'), ('http://board:80', 'http://board'),
                  ('https://board:443/', 'https://board'), ('board.example:8765', 'http://board.example:8765'),
                  ('http://[FE80::1%25eth0]:8765/', 'http://[fe80::1%25eth0]:8765'), ('http://board:8765/x/', 'http://board:8765/x'),
                  ('http://board:99999', 'http://board:99999'), ('  http://board:8765  ', 'http://board:8765')]:
    eq(f'norm_url {url!r}', tc.norm_url(url), want)
saved_url = tc.BAKED_URL
try:
    tc.BAKED_URL = 'http://Board.example:8765'
    eq('own_board: the baked URL, however it is spelled', [tc.own_board(tc.Board(u, 1.0, False)) for u in
       ('http://board.example:8765', 'HTTP://BOARD.EXAMPLE:8765', 'http://board.example:8766', 'http://other.example:8765',
        'https://board.example:8765')], [True, True, False, False, False])
    tc.BAKED_URL = '{{BASE_URL}}'
    eq('own_board: an unrendered copy has no board of its own', tc.own_board(tc.Board('http://board.example:8765', 1.0, False)), False)
finally:
    tc.BAKED_URL = saved_url

S.section('the queue locks: bounded, and one replay at a time; qids; the refused-create list')
try:
    import fcntl
except ImportError:
    fcntl = None
with tempfile.TemporaryDirectory() as d, environ(TASKS_SPOOL_DIR=d):
    sp = tc.Spool('http://board:8765')
    sp.make_dir()
    if fcntl is None:
        S.skip('the lock checks', 'no fcntl here')
    else:
        held = os.open(sp.path + '.lock', os.O_RDWR | os.O_CREAT)
        fcntl.flock(held, fcntl.LOCK_EX)
        t0 = time.monotonic()
        try:
            with sp.locked():
                got = 'locked'
        except tc.QueueBusy as e:
            got = 'QueueBusy'
        took = time.monotonic() - t0
        os.close(held)
        S.check('a queue lock held elsewhere: QueueBusy after about LOCK_WAIT, not a wait forever', got == 'QueueBusy'
                and tc.LOCK_WAIT * 0.9 <= took < tc.LOCK_WAIT + 1, (got, took))
        with sp.locked():
            got = 'locked'
        eq('...and once it is let go, the lock is had', got, 'locked')
        mine = sp.replay_lock()
        S.check('replay_lock: had when free', mine is not None)
        t0 = time.monotonic()
        eq('replay_lock: None at once while another holder has it', (sp.replay_lock(), time.monotonic() - t0 < 0.5), (None, True))
        mine.release()
        mine.release()
        again = sp.replay_lock()
        S.check('replay_lock: free again once released (release is idempotent)', again is not None)
        again.release()
    r1 = tc.Board.record('POST', f'/api/tasks/{T1}/progress', {'message': 'a'}, 1.0)
    r2 = tc.Board.record('POST', f'/api/tasks/{T1}/progress', {'message': 'a'}, 1.0)
    S.check('records carry a random 16-hex qid, their key', re.fullmatch('[0-9a-f]{16}', r1['qid']) and r1['qid'] != r2['qid']
            and tc.record_key(r1) == r1['qid'] and tc.valid_record(r1), r1)
    bare = rec('POST', f'/api/tasks/{T1}/progress', {'message': 'a'})
    S.check('a record without a qid: a key from its content, the same every time', tc.record_key(bare) == tc.record_key(dict(bare))
            and tc.record_key(bare) != tc.record_key(rec('POST', f'/api/tasks/{T2}/progress', {'message': 'a'})), tc.record_key(bare))
    eq('valid_record: a qid that is not a string', tc.valid_record(dict(bare, qid=5)), False)
    eq('no refused creates yet', sp.dead(), set())
    with sp.locked():
        sp.add_dead({'k3m9qa'})
    with open(sp.dead_path, 'a') as f:
        f.write('zzzzzz %d\nnot-an-id 1\nbroken\n' % (time.time() - 8 * 86400))
    eq('dead(): the refused create; one older than 7 days and junk lines ignored', sp.dead(), {'k3m9qa'})
    with sp.locked():
        sp.add_dead({'m2m2m2'})
    eq('add_dead drops the expired ones as it writes', sorted(line.split()[0] for line in open(sp.dead_path).read().splitlines()),
       ['k3m9qa', 'm2m2m2'])
    board = tc.Board('http://board:8765', 1.0, False)
    for method, path in (('POST', '/api/tasks/k3m9qa-2/complete'), ('DELETE', '/api/requests/k3m9qa/attention'),
                         ('POST', '/api/requests/k3m9qa/tasks')):
        try:
            board.refuse_dead(method, path)
            got = 'sent'
        except tc.BoardError as e:
            got = 'never created' in str(e) and not e.unreachable
        eq(f'refuse_dead {method} {path}', got, True)
    for method, path in (('GET', '/api/requests/k3m9qa'), ('POST', '/api/tasks/q3m9qa-1/progress'), ('POST', '/api/requests')):
        board.refuse_dead(method, path)
        S.check(f'refuse_dead lets {method} {path} through', True)

S.section("whats_new: the changelog entries newer than an API version, verbatim")
CL = '## v3 — 2026-10-01\n- three\n\n## v2 — 2026-09-28\n- two a\n- two b\n\n## v1 — 2026-09-01\n- one\n'
eq('since 1: v3 and v2', tc.whats_new(CL, 1), '## v3 — 2026-10-01\n- three\n\n## v2 — 2026-09-28\n- two a\n- two b')
eq('since 3: nothing newer, so the newest', tc.whats_new(CL, 3), '## v3 — 2026-10-01\n- three')
eq('no API version: the newest', tc.whats_new(CL, None), '## v3 — 2026-10-01\n- three')
eq('no headings: the whole text', tc.whats_new('  just text\n', 1), 'just text')
real = (Path(TASKCTL_PY).parent / 'templates' / 'changelog.md').read_text()
S.check("the real changelog: v2's entry is what a v1 CLI is told", tc.whats_new(real, 1).startswith('## v2 — ')
        and '## v1' not in tc.whats_new(real, 1), tc.whats_new(real, 1)[:200])

sys.exit(S.finish())
