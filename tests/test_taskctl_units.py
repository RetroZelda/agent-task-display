#!/usr/bin/env python3
"""taskctl.py unit tests: parsers, formatters, id handling, option splitting, the rule merge, the run
monitor, timeout/URL resolution and error handling, imported straight from the repo (no bytecode)."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import random
import re
import sys
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

sys.exit(S.finish())
