#!/usr/bin/env python3
"""taskctl <-> server: every subcommand and alias through the real sh wrapper against a scratch
server (--stale-after 3, --expire-after 6), with every aggregate recomputed from the task list.
Also: run (output pass-through, percent regex, signals, a background grandchild that outlives it),
install-rule, the served and downloaded taskctl, the offline sentinel, ask/resume (waiting in show and
list), usage, changelog, api and flush."""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the checkout, even when run without run_all.sh
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from harness import (TASKCTL_PY, TASKS, WRAPPER, ScratchServer, Suite, api, clean_env, docs_version,  # noqa: E402
                     scratch_home, tasks_copy, tempdir, wait_until)

S = Suite('cli')
check = S.check
TMP = tempdir('cli')
scratch_home(TMP)
WORK = TMP / 'agent-work'   # taskctl's cwd: the request origin is <hostname>:agent-work
WORK.mkdir()
STALE_AFTER = 3
EXPIRE_AFTER = 6
CC = TMP / 'cc'
ENV = clean_env(CLAUDE_CONFIG_DIR=CC)
URL = ''

TASK_KEYS = {'id', 'request_id', 'seq', 'title', 'status', 'percent', 'message', 'eta_at', 'eta_remaining',
             'created_at', 'started_at', 'start_inferred', 'updated_at', 'completed_at', 'elapsed', 'stale', 'url',
             'attention'}
REQUEST_KEYS = {'id', 'title', 'origin', 'status', 'message', 'created_at', 'updated_at', 'completed_at',
                'elapsed', 'percent', 'tasks_total', 'tasks_done', 'tasks_failed', 'tasks_running',
                'tasks_pending', 'tasks_cancelled', 'current', 'eta_at', 'stale', 'url', 'attention', 'waiting',
                'tasks_waiting'}
NUDGE = ('taskctl: task was never started; its running time is approximate - run `taskctl start TID` '
         'when you begin a task')


def tc(*args, strict=True, url='default', exe=WRAPPER, env=None, timeout=60):
    url = URL if url == 'default' else url
    argv = [str(exe)] + (['--strict'] if strict else []) + (['--url', url] if url else []) + list(args)
    p = subprocess.run(argv, capture_output=True, text=True, cwd=WORK, env=env or ENV, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def get(path):
    return api(URL, 'GET', path)[1]


def get_req(rid):
    code, body, _ = api(URL, 'GET', '/api/requests/' + rid)
    assert code == 200, (code, body)
    return body


def near(a, b, tol=0.05):
    return a is not None and b is not None and abs(a - b) <= tol


def lines(text):
    return [line for line in text.splitlines() if line.strip()]


def task_of(r, tid):
    return next(t for t in r['tasks'] if t['id'] == tid)


def verify_task(t, now, where):
    check(f"{where}: task {t['id']} has exactly the contract keys", set(t) - {'now', 'reopened', 'warnings'} == TASK_KEYS,
          set(t) ^ TASK_KEYS)
    running = t['status'] == 'running'
    exp_rem = max(0.0, t['eta_at'] - now) if running and t['eta_at'] is not None else None
    check(f"{where}: {t['id']} eta_remaining", (exp_rem is None and t['eta_remaining'] is None)
          or near(exp_rem, t['eta_remaining']), (exp_rem, t['eta_remaining']))
    exp_el = None if t['started_at'] is None else (t['completed_at'] or now) - t['started_at']
    check(f"{where}: {t['id']} elapsed", (exp_el is None and t['elapsed'] is None) or near(exp_el, t['elapsed']),
          (exp_el, t['elapsed']))
    exp_stale = (running and t['attention'] is None and now - t['updated_at'] > STALE_AFTER
                 and not (t['eta_at'] and now < t['eta_at'] + 60))
    check(f"{where}: {t['id']} stale", t['stale'] == exp_stale, (t['stale'], exp_stale))
    check(f"{where}: {t['id']} attention is null or {{message, since}}", t['attention'] is None
          or (set(t['attention']) == {'message', 'since'} and t['attention']['message']), t['attention'])
    check(f"{where}: {t['id']} url", t['url'] == f"{URL}/r/{t['request_id']}#{t['id']}", t['url'])
    check(f"{where}: {t['id']} percent in range, 1 decimal", 0 <= t['percent'] <= 100
          and round(t['percent'], 1) == t['percent'], t['percent'])
    check(f"{where}: {t['id']} start_inferred is a bool", isinstance(t['start_inferred'], bool))


def verify_request(r, where):
    """Recomputes every aggregate from the task list and compares it with what the server reported."""
    now = r['now']
    check(f'{where}: request has exactly the contract keys',
          set(r) - {'tasks', 'now', 'stale_after', 'auto_closed', 'cancelled', 'warnings'} == REQUEST_KEYS,
          set(r) ^ REQUEST_KEYS)
    tasks = r['tasks']
    check(f'{where}: tasks ordered by seq', [t['seq'] for t in tasks] == sorted(t['seq'] for t in tasks))
    for t in tasks:
        verify_task(t, now, where)
    live = [t for t in tasks if t['status'] != 'cancelled']
    check(f'{where}: tasks_total = non-cancelled ({len(live)})', r['tasks_total'] == len(live), r['tasks_total'])
    for s in ('done', 'failed', 'running', 'pending', 'cancelled'):
        n = sum(1 for t in tasks if t['status'] == s)
        check(f'{where}: tasks_{s}', r['tasks_' + s] == n, (r['tasks_' + s], n))
    if live:
        exp_pct = round(sum(100.0 if t['status'] == 'done' else t['percent'] for t in live) / len(live), 1)
    else:
        exp_pct = 100.0 if r['status'] == 'done' else 0.0
    check(f'{where}: percent {exp_pct:.1f}', near(r['percent'], exp_pct, 0.051), (r['percent'], exp_pct))
    running = [t for t in tasks if t['status'] == 'running']
    if running:
        cur = max(running, key=lambda t: (t['updated_at'], t['seq']))
        exp_cur = {'id': cur['id'], 'title': cur['title'], 'message': cur['message']}
    else:
        exp_cur = None
    check(f'{where}: current', r['current'] == exp_cur, (r['current'], exp_cur))
    etas = [t['eta_at'] for t in running if t['eta_at'] is not None]
    check(f'{where}: eta_at = max running eta', r['eta_at'] == (max(etas) if etas else None), (r['eta_at'], etas))
    exp_tw = sum(1 for t in running if t['attention'] is not None)
    exp_waiting = r['status'] == 'running' and (r['attention'] is not None or exp_tw > 0)
    check(f'{where}: tasks_waiting = running tasks with attention ({exp_tw})', r['tasks_waiting'] == exp_tw, r['tasks_waiting'])
    check(f'{where}: waiting={exp_waiting}', r['waiting'] is exp_waiting, (r['waiting'], r['attention']))
    exp_stale = (r['status'] == 'running' and not exp_waiting and now - r['updated_at'] > STALE_AFTER
                 and not any(t['eta_at'] and now < t['eta_at'] + 60 for t in running))
    check(f'{where}: stale={exp_stale}', r['stale'] == exp_stale, r['stale'])
    check(f'{where}: url', r['url'] == f"{URL}/r/{r['id']}", r['url'])
    check(f'{where}: elapsed', near(r['elapsed'], (r['completed_at'] or now) - r['created_at']), r['elapsed'])
    if 'stale_after' in r:
        check(f'{where}: stale_after', r['stale_after'] == STALE_AFTER, r['stale_after'])
    return r


# ---------------------------------------------------------------- the main flow

def main_flow():
    host = socket.gethostname()
    S.section('ping, new, add')
    rc, out, err = tc('ping')
    check('ping: rc 0', rc == 0, (rc, err))
    check('ping: stdout is the base URL only', out == URL + '\n', out)
    check('ping: stderr says ok', err.startswith('taskctl: ok'), err)

    rc, out, err = tc('new', 'Integration test', '-t', 'alpha', '-t', 'beta', '-t', 'gamma')
    ids = lines(out)
    check('new: rc 0', rc == 0, (rc, err))
    check('new: stdout = rid + 3 tids in order', len(ids) == 4 and re.fullmatch(r'[23456789a-hjkmnp-z]{6}', ids[0])
          and ids[1:] == [f'{ids[0]}-{i}' for i in (1, 2, 3)], out)
    rid = ids[0]
    t1, t2, t3 = ids[1:]
    errl = err.splitlines()
    check('new: stderr created line', errl and errl[0] == f"taskctl: created {rid} 'Integration test'  page: {URL}/r/{rid}", err)
    check('new: stderr task lines', errl[1:] == [f'taskctl:   {t}  {n}' for t, n in zip(ids[1:], ('alpha', 'beta', 'gamma'), strict=False)], err)
    r = verify_request(get_req(rid), 'after new')
    check('new: origin = hostname:cwd basename', r['origin'] == f'{host}:{WORK.name}', r['origin'])
    check('new: request running, 0%, tasks pending', r['status'] == 'running' and r['percent'] == 0
          and all(t['status'] == 'pending' and t['started_at'] is None for t in r['tasks']))

    rc, out, err = tc('add', rid, 'delta', 'epsilon')
    check('add: rc 0, two tids', rc == 0 and lines(out) == [rid + '-4', rid + '-5'], (rc, out, err))
    check('add: stderr notes, not reopened', f'added {rid}-4' in err and 'reopened' not in err, err)
    t4, t5 = rid + '-4', rid + '-5'
    r = verify_request(get_req(rid), 'after add')
    check('add: new tasks pending', task_of(r, t4)['status'] == 'pending' and task_of(r, t5)['status'] == 'pending')
    rc, out, err = tc('add', rid, 'zeta', '--start')
    t6 = rid + '-6'
    check('add --start: rc 0, one tid', rc == 0 and lines(out) == [t6], (rc, out, err))
    r = verify_request(get_req(rid), 'after add --start')
    check('add --start: task running with started_at', task_of(r, t6)['status'] == 'running' and task_of(r, t6)['started_at'])
    check('add --start: current is it', r['current'] and r['current']['id'] == t6, r['current'])
    rc, out, err = tc('task', rid, 'theta')
    t7 = rid + '-7'
    check('alias task: rc 0, pending tid', rc == 0 and lines(out) == [t7], (rc, out, err))
    rc, out, err = tc('add', '--start', rid, 'a', 'b')
    check('add --start with two titles: usage exit 2', rc == 2 and out == '', (rc, out, err))

    S.section('start, progress')
    rc, out, err = tc('start', t1, 'beginning', 'alpha')
    check('start with hint: rc 0, no stdout', rc == 0 and out == '', (rc, out, err))
    rc, out, err = tc('start', t2)
    check('start without hint: rc 0', rc == 0, (rc, err))
    r = verify_request(get_req(rid), 'after start')
    check('start: t1 running, hint words joined', task_of(r, t1)['status'] == 'running'
          and task_of(r, t1)['message'] == 'beginning alpha' and task_of(r, t1)['started_at'], task_of(r, t1))
    check('start: t2 message "started"', task_of(r, t2)['message'] == 'started', task_of(r, t2)['message'])
    check('start: current = latest updated running (t2)', r['current'] and r['current']['id'] == t2, r['current'])
    for pct, dur, exp_pct, exp_eta in (('45', '90', 45.0, 90), ('45.5', '90s', 45.5, 90), ('46%', '5m', 46.0, 300),
                                       ('47', '5min', 47.0, 300), ('48', '~5m', 48.0, 300), ('49', '1h30m', 49.0, 5400),
                                       ('50', '2h', 50.0, 7200), ('51', '1:30', 51.0, 90), ('52', '1:30:00', 52.0, 5400)):
        rc, out, err = tc('progress', t1, pct, 'compiling', 'shaders', '--eta', dur)
        t = get('/api/tasks/' + t1)
        check(f'progress {pct} --eta {dur}: rc 0, percent {exp_pct}, eta ~{exp_eta}s',
              rc == 0 and out == '' and t['percent'] == exp_pct and near(t['eta_at'] - t['updated_at'], exp_eta, 0.01)
              and t['message'] == 'compiling shaders', (rc, out, err, t['percent'], t['eta_at'] and t['eta_at'] - t['updated_at']))
    r = verify_request(get_req(rid), 'after progress --eta')
    check('progress: request eta_at is t1 eta', r['eta_at'] == task_of(r, t1)['eta_at'])
    rc, out, err = tc('progress', t2, '150', 'over', 'the', 'top')
    t = get('/api/tasks/' + t2)
    check('progress 150: client clamps + warns, rc 0', rc == 0 and t['percent'] == 100 and 'warning' in err, (rc, err, t['percent']))
    rc, out, err = tc('progress', t2, '30', 'no eta now')
    t = get('/api/tasks/' + t2)
    check('progress without --eta: eta cleared, percent 30', rc == 0 and t['eta_at'] is None and t['percent'] == 30, t)
    rc, out, err = tc('progress', t2, '--eta', '5m', '35', 'hint', 'text')
    t = get('/api/tasks/' + t2)
    check('progress with --eta before PCT', rc == 0 and t['percent'] == 35 and t['eta_at'], (rc, err, t['percent']))
    rc, out, err = tc('progress', t2, '36', 'hint', '--eta=90')
    t = get('/api/tasks/' + t2)
    check('progress --eta=90', rc == 0 and near(t['eta_at'] - t['updated_at'], 90, 0.01), (rc, err))
    rc, out, err = tc('progress', t2, '-5', 'neg')
    t = get('/api/tasks/' + t2)
    check('progress -5 clamps to 0', rc == 0 and t['percent'] == 0, (rc, err, t['percent']))
    rc, out, err = tc('progress', t1, 'abc', 'x')
    check('progress bad percent: exit 2', rc == 2, (rc, err))
    rc, out, err = tc('progress', t1, '10', 'x', '--eta', 'soon')
    check('progress bad eta: exit 2', rc == 2, (rc, err))
    rc, out, err = tc('progress', rid, '10', 'x')
    check('progress on a request id: exit 2', rc == 2, (rc, err))
    verify_request(get_req(rid), 'after progress')

    S.section('done / fail on tasks, the start nudge, aliases, last write wins')
    rc, out, err = tc('done', t2, 'beta', 'ok')
    check('done task: rc 0, no nudge', rc == 0 and out == '' and 'never started' not in err, (rc, out, err))
    t = get('/api/tasks/' + t2)
    check('done task: done, 100%, message, eta cleared', t['status'] == 'done' and t['percent'] == 100
          and t['message'] == 'beta ok' and t['eta_at'] is None and t['completed_at'], t)
    first_completed = t['completed_at']
    rc, out, err = tc('done', t4)
    check('done a never-started task: nudge on stderr', rc == 0 and NUDGE in err, err)
    t = get('/api/tasks/' + t4)
    check('done a never-started task: start_inferred, started_at = created_at',
          t['start_inferred'] is True and t['started_at'] == t['created_at'] and t['status'] == 'done', t)
    rc, out, err = tc('fail', t5, 'broke', 'badly')
    t = get('/api/tasks/' + t5)
    check('fail task: failed, percent unchanged, message, nudge', rc == 0 and t['status'] == 'failed'
          and t['percent'] == 0 and t['message'] == 'broke badly' and NUDGE in err, (rc, err, t))
    rc, out, err = tc('fail', t5)
    check('fail without a message: exit 2', rc == 2, (rc, err))
    rc, out, err = tc('finish', t2, 'again')
    t = get('/api/tasks/' + t2)
    check('alias finish (same status): no-op, message replaced', rc == 0 and t['status'] == 'done'
          and t['message'] == 'again' and t['completed_at'] == first_completed, (rc, err, t))
    rc, out, err = tc('failed', t2, 'flip')
    t = get('/api/tasks/' + t2)
    check('alias failed (different status): overwrite, completed_at kept', rc == 0 and t['status'] == 'failed'
          and t['completed_at'] == first_completed and t['message'] == 'flip', (rc, err, t))
    rc, out, err = tc('complete', t2, 'flip back')
    t = get('/api/tasks/' + t2)
    check('alias complete: back to done, 100%', rc == 0 and t['status'] == 'done' and t['percent'] == 100
          and t['completed_at'] == first_completed, t)
    rc, out, err = tc('progress', t2, '50', 'late update')
    check('progress on a done task (strict): exit 1, 409 warning', rc == 1 and '409' in err and 'already done' in err, (rc, err))
    rc, out, err = tc('progress', t2, '50', 'late update', strict=False)
    check('progress on a done task (soft): exit 0 + warning', rc == 0 and 'taskctl: warning:' in err, (rc, err))
    r = verify_request(get_req(rid), 'after task closes')

    S.section('show, status, list, ls')
    rc, out, err = tc('show', rid)
    ol = out.splitlines()
    check('show request: rc 0', rc == 0, (rc, err))
    head = ol[0] if ol else ''
    check('show request: header has id, status, percent, X/Y, title, origin',
          head.startswith(rid) and 'running' in head and f"{r['tasks_done']}/{r['tasks_total']}" in head
          and 'Integration test' in head and r['origin'] in head and '%' in head, head)
    check('show request: page line', len(ol) > 1 and ol[1] == f'page: {URL}/r/{rid}', ol[1:2])
    check('show request: one line per task', len(ol) == 2 + len(r['tasks'])
          and all(t['id'] in ol[2 + i] for i, t in enumerate(r['tasks'])), out)
    t1line = next((line for line in ol if f' {t1} ' in line), '')
    check('show request: task line has status, percent, title: message, eta',
          'running' in t1line and '52%' in t1line and 'alpha: compiling shaders' in t1line and 'eta' in t1line, t1line)
    rc, out2, err = tc('status', rid)
    check('alias status == show', rc == 0 and lines(out2)[0].split()[:2] == lines(out)[0].split()[:2], out2)
    rc, out, err = tc('show', rid.upper())
    check('upper-case id works', rc == 0 and lines(out)[0].startswith(rid), (rc, out, err))
    rc, out, err = tc('show', t4)
    check('show task: one line, ~ for an inferred start', rc == 0 and len(lines(out)) == 1 and t4 in out
          and 'done' in out and '~' in out, out)
    rc, out, err = tc('show', 'zzzzzz')
    check('show unknown request (strict): exit 1, 404', rc == 1 and '404' in err and out == '', (rc, out, err))
    rc, out, err = tc('show', rid + '-99')
    check('show unknown task (strict): exit 1', rc == 1 and '404' in err, (rc, err))
    rc, out, err = tc('show', 'l0l0l0')
    check('show malformed id: exit 2', rc == 2, (rc, err))

    rc, out, err = tc('new', "user's C:\\path request", '-t', 'one', '-t', 'two')
    rid2, s1, s2 = lines(out)
    check('new: stderr quotes the title literally', err.splitlines()[0]
          == f"taskctl: created {rid2} 'user's C:\\path request'  page: {URL}/r/{rid2}", err)
    tc('start', s2, 'working')
    rc, out, err = tc('list')
    ll = lines(out)
    check('list: both running requests, newest first, one line each', rc == 0 and len(ll) == 2
          and ll[0].startswith(rid2) and ll[1].startswith(rid), out)
    rc, out2, err = tc('ls')
    check('alias ls == list', rc == 0 and [x.split()[0] for x in lines(out2)] == [x.split()[0] for x in ll], out2)
    code, lst, _ = api(URL, 'GET', '/api/requests')
    check('GET /api/requests counts', lst['counts'] == {'running': 2, 'stale': 0, 'done': 0, 'failed': 0, 'waiting': 0},
          lst['counts'])

    S.section('fail on a request: running -> failed (auto_closed), pending -> cancelled')
    rc, out, err = tc('fail', rid2, 'gave', 'up')
    check('fail request: rc 0, no stdout', rc == 0 and out == '', (rc, out, err))
    check('fail request: stderr lists auto_closed and cancelled', s2 in err and s1 in err and 'cancelled' in err
          and 'failed' in err, err)
    r2 = verify_request(get_req(rid2), 'after fail request')
    check('fail request: failed with its message', r2['status'] == 'failed' and r2['message'] == 'gave up' and r2['completed_at'])
    check('fail request: s2 failed, s1 cancelled', task_of(r2, s2)['status'] == 'failed' and task_of(r2, s1)['status'] == 'cancelled')
    check('fail request: tasks_total excludes cancelled', r2['tasks_total'] == 1 and r2['tasks_cancelled'] == 1)
    rc, out, err = tc('progress', s1, '5', 'x')
    check('progress on a cancelled task (strict): exit 1, 409', rc == 1 and 'already cancelled' in err, (rc, err))

    S.section('run')
    seen = []
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            try:
                t = get('/api/tasks/' + t6)
                seen.append((t['status'], t['percent'], t['message']))
            except Exception:  # noqa: BLE001
                pass
            time.sleep(0.25)

    poller = threading.Thread(target=poll, daemon=True)
    poller.start()
    t0 = time.monotonic()
    rc, out, err = tc('run', t6, '--every', '1', '--percent-regex', r'(\d+)/(\d+)', '--',
                      'sh', '-c', 'for i in 1 2 3 4; do echo "$i/4"; sleep 1; done')
    took = time.monotonic() - t0
    stop.set()
    poller.join()
    check('run: rc 0, output passed through unchanged', rc == 0 and out == '1/4\n2/4\n3/4\n4/4\n', (rc, out, err))
    check('run: no warnings on stderr', 'warning' not in err, err)
    check('run: took ~4s (not blocked by reporting)', 3.5 < took < 7, took)
    mids = sorted({p for s, p, m in seen if s == 'running' and p in (25.0, 50.0, 75.0)})
    check('run: intermediate percents from the regex', len(mids) >= 2, seen)
    check('run: the hint is the latest line', any(m in ('2/4', '3/4') for s, p, m in seen if s == 'running'), seen)
    t = get('/api/tasks/' + t6)
    check('run: task done, 100%, "exit 0 after Ns"', t['status'] == 'done' and t['percent'] == 100
          and re.fullmatch(r'exit 0 after [45]s', t['message'] or ''), t)
    verify_request(get_req(rid), 'after run success')

    rc, out, err = tc('run', t7, '--', 'sh', '-c', 'echo working; sleep 0.3; echo "boom: disk full" >&2; exit 3')
    check('run failing: rc 3, stdout and stderr passed through', rc == 3 and out == 'working\n' and 'boom: disk full' in err, (rc, out, err))
    t = get('/api/tasks/' + t7)
    check('run failing: task failed "exit 3 after 0s: boom: disk full"', t['status'] == 'failed'
          and t['message'] == 'exit 3 after 0s: boom: disk full', t)
    check('run failing: the pending task was started by run (not inferred)', t['start_inferred'] is False and t['started_at'])
    rc, out, err = tc('run', t7, '--', 'definitely-not-a-command-xyz')
    check('run a missing command: rc 127', rc == 127, (rc, err))
    rc, out, err = tc('run', t7, 'sh', '-c', 'true')
    check('run without --: exit 2', rc == 2, (rc, err))
    verify_request(get_req(rid), 'after run failing')

    S.section('done on the request: t1 running -> auto_closed, t3 pending -> cancelled')
    before = get_req(rid)
    exp_auto = [t['id'] for t in before['tasks'] if t['status'] == 'running']
    exp_canc = [t['id'] for t in before['tasks'] if t['status'] == 'pending']
    rc, out, err = tc('done', rid, 'all', 'wrapped', 'up')
    check('done request: rc 0, no stdout', rc == 0 and out == '', (rc, out, err))
    check('done request: the expected sets', exp_auto == [t1] and exp_canc == [t3], (exp_auto, exp_canc))
    check('done request: stderr lists auto_closed', f'still-running tasks closed as done: {t1}' in err, err)
    check('done request: stderr lists cancelled', f'never-started tasks cancelled: {t3}' in err, err)
    r = verify_request(get_req(rid), 'after done request')
    check('done request: done with its message', r['status'] == 'done' and r['message'] == 'all wrapped up' and r['completed_at'])
    check('done request: t1 done 100%, eta cleared', task_of(r, t1)['status'] == 'done' and task_of(r, t1)['percent'] == 100
          and task_of(r, t1)['eta_at'] is None)
    check('done request: t3 cancelled with completed_at', task_of(r, t3)['status'] == 'cancelled' and task_of(r, t3)['completed_at'])
    check('done request: current and eta_at are null', r['current'] is None and r['eta_at'] is None)
    completed = r['completed_at']
    code, body, _ = api(URL, 'POST', f'/api/requests/{rid}/complete', {'status': 'done', 'message': 'again'})
    check('complete the request again (same status): lists empty, message replaced', code == 200
          and body['auto_closed'] == [] and body['cancelled'] == [] and body['message'] == 'again'
          and body['completed_at'] == completed, body)
    rc, out, err = tc('done', rid, 'third')
    check('done request again via the CLI: no auto/cancel lines', rc == 0 and 'still-running' not in err
          and 'cancelled:' not in err, err)
    rc, out, err = tc('show', rid)
    check('show done request: header says done', rc == 0 and lines(out)[0].split()[1] == 'done', out)

    rc, out, err = tc('add', rid, 'follow-up')
    t8 = rid + '-8'
    check('add to a closed request: tid printed, reopened noted', rc == 0 and lines(out) == [t8] and 'reopened' in err, (rc, out, err))
    r = verify_request(get_req(rid), 'after reopen')
    check('reopen: running, completed_at and message cleared', r['status'] == 'running' and r['completed_at'] is None
          and r['message'] is None)

    rc, out, err = tc('list', '--all')
    la = [x.split()[0] for x in lines(out)]
    check('list --all: running and finished requests', rc == 0 and rid in la and rid2 in la, out)
    rc, out, err = tc('ls', '-a')
    check('alias ls -a', rc == 0 and [x.split()[0] for x in lines(out)] == la, out)

    S.section('offline sentinel, unreachable board, URL and option placement')
    for args in (('progress', 'offline', '5', 'x'), ('done', 'offline'), ('start', 'offline'), ('show', 'offline'),
                 ('fail', 'offline', 'x')):
        rc, out, err = tc(*args)
        check(f'offline id: {args[0]} is a no-op, exit 0', rc == 0 and out == '' and 'offline' in err, (rc, out, err))
    rc, out, err = tc('add', 'offline', 'a', 'b')
    check('add offline: prints offline per title', rc == 0 and lines(out) == ['offline', 'offline'], (rc, out, err))
    rc, out, err = tc('run', 'offline', '--', 'sh', '-c', 'echo ran; exit 4')
    check('run offline: runs the command, exits with its code', rc == 4 and out == 'ran\n', (rc, out, err))
    rc, out, err = tc('new', 'x', '-t', 'a', strict=False, url='http://127.0.0.1:1')
    ids = lines(out)
    check('new unreachable (soft): exit 0, its own rid + rid-1, queued', rc == 0 and len(ids) == 2
          and re.fullmatch(r'[23456789a-hjkmnp-z]{6}', ids[0]) and ids[1] == ids[0] + '-1'
          and 'queued for replay' in err, (rc, out, err))
    rc, out, err = tc('new', 'x', '-t', 'a', url='http://127.0.0.1:1')
    check('new unreachable (strict): exit 3, the queued ids still printed', rc == 3 and len(lines(out)) == 2
          and 'offline' not in out and 'queued for replay' in err, (rc, out, err))
    rc, out, err = tc('ping', url='http://127.0.0.1:1')
    check('ping unreachable (strict): exit 3, stdout URL', rc == 3 and out == 'http://127.0.0.1:1\n' and 'unreachable' in err, (rc, out, err))
    rc, out, err = tc('ping', strict=False, url='http://127.0.0.1:1')
    check('ping unreachable (soft): exit 0, says unreachable', rc == 0 and 'unreachable' in err, (rc, out, err))
    rc, out, err = tc('--strict', 'show', rid, strict=False, url=None, env=dict(ENV, TASKS_URL=URL))
    check('TASKS_URL used when there is no --url', rc == 0 and lines(out)[0].startswith(rid), (rc, out, err))
    rc, out, err = tc('show', rid, '--strict', '--url', URL, strict=False, url=None)
    check('global options after the subcommand', rc == 0 and lines(out)[0].startswith(rid), (rc, out, err))
    rc, out, err = tc('ping', env=dict(ENV, TASKS_TIMEOUT='0'))
    check('bad TASKS_TIMEOUT: warning + 3s default, still works', rc == 0
          and "warning: TASKS_TIMEOUT='0' is not a positive number of seconds; using 3s" in err, (rc, err))
    rc, out, err = tc('ping', env=dict(ENV, TASKS_TIMEOUT='5s'))
    check('TASKS_TIMEOUT=5s accepted (duration syntax)', rc == 0 and 'TASKS_TIMEOUT' not in err, (rc, err))

    S.section('stale, then expiry')
    rc, out, err = tc('new', 'Kept alive by eta', '-t', 'long')
    rid3, l1 = lines(out)
    tc('progress', l1, '10', 'long haul', '--eta', '60')
    time.sleep(STALE_AFTER + 0.6)
    r = verify_request(get_req(rid), 'stale check')
    check('stale: a request silent > stale_after is stale', r['stale'] is True, r['stale'])
    r3 = verify_request(get_req(rid3), 'eta keeps fresh')
    check('stale: a running eta keeps its request and task fresh', r3['stale'] is False and task_of(r3, l1)['stale'] is False)
    code, lst, _ = api(URL, 'GET', '/api/requests')
    check('list counts.stale', lst['counts']['stale'] == 1 and lst['counts']['running'] == 2, lst['counts'])
    wait_until(lambda: get_req(rid)['status'] != 'running', EXPIRE_AFTER + EXPIRE_AFTER / 2 + 3, 0.3)
    r = verify_request(get_req(rid), 'after expiry')
    check('expiry: failed, "expired: no activity for 6s"', r['status'] == 'failed'
          and r['message'] == 'expired: no activity for 6s', (r['status'], r['message']))
    check('expiry: the pending task is cancelled', task_of(r, t8)['status'] == 'cancelled')
    check('expiry: a request inside a running eta is not expired', get_req(rid3)['status'] == 'running')
    tc('done', rid3, 'finished')

    S.section('DELETE')
    code, body, _ = api(URL, 'DELETE', '/api/requests/' + rid2)
    check('DELETE request', code == 200 and body['deleted'] == rid2)
    rc, out, err = tc('show', s1)
    check('a task of a deleted request: 404 (strict exit 1)', rc == 1 and 'request may have been deleted' in err, err)

    S.section('ask and resume: a request or a task waiting for input')
    rc, out, err = tc('new', 'Needs an answer', '-t', 'one', '-t', 'two')
    arid, a1, a2 = lines(out)
    rc, out, err = tc('ask', arid, 'Which', 'environment?')
    r = verify_request(get_req(arid), 'after ask RID')
    check('ask RID: rc 0, no stdout, the exact note', rc == 0 and out == '' and err == f'taskctl: {arid} waiting for input: Which environment?\n',
          (rc, out, err))
    check('ask RID: request-level attention, waiting, no task waits', r['attention'] and r['attention']['message'] == 'Which environment?'
          and r['waiting'] is True and r['tasks_waiting'] == 0, r)
    rc, out, err = tc('show', arid)
    ol = out.splitlines()
    check('show a waiting request: status waiting, the question under the header, then the page',
          rc == 0 and ol[0].split()[1] == 'waiting' and ol[1] == 'waiting: Which environment?' and ol[2] == f'page: {URL}/r/{arid}'
          and len(ol) == 5, out)
    rc, out, err = tc('list')
    line = next((x for x in lines(out) if x.startswith(arid)), '')
    check('list marks the waiting request', rc == 0 and line.split()[1] == 'waiting', out)
    code, lst, _ = api(URL, 'GET', '/api/requests')
    check('counts.waiting counts it', lst['counts']['waiting'] == 1, lst['counts'])
    rc, out, err = tc('ask', a1, 'Need', 'a', 'token')
    t = get('/api/tasks/' + a1)
    check('ask TID: starts the pending task, sets its attention', rc == 0 and err == f'taskctl: {a1} waiting for input: Need a token\n'
          and t['status'] == 'running' and t['started_at'] and t['attention']['message'] == 'Need a token', (err, t))
    rc, out, err = tc('show', arid)
    ol = out.splitlines()
    i = next((i for i, x in enumerate(ol) if f' {a1} ' in x), 0)
    check('show: the waiting task line, then its question indented', ol[i].split()[0] == 'waiting'
          and ol[i + 1] == '    waiting: Need a token', out)
    rc, out, err = tc('show', a1)
    check('show a waiting task: its line, then the question', rc == 0 and lines(out)[1:] == ['waiting: Need a token']
          and lines(out)[0].split()[0] == 'waiting', out)
    rc, out, err = tc('resume', arid)
    r = verify_request(get_req(arid), 'after resume RID')
    check("resume RID: the exact note; the request's own question cleared, its task still waits",
          rc == 0 and err == f'taskctl: {arid} resumed\n' and r['attention'] is None and r['waiting'] is True
          and r['tasks_waiting'] == 1, (err, r))
    rc, out, err = tc('resume', arid)
    check('resume again: idempotent, rc 0', rc == 0 and err == f'taskctl: {arid} resumed\n', (rc, err))
    rc, out, err = tc('progress', a1, '40', 'got', 'the', 'token')
    r = verify_request(get_req(arid), 'after progress on the waiting task')
    check('progress on a waiting task clears its question', task_of(r, a1)['attention'] is None and r['waiting'] is False, r)
    tc('ask', a1, 'One more thing?')
    rc, out, err = tc('resume', a1)
    t = get('/api/tasks/' + a1)
    check('resume TID', rc == 0 and err == f'taskctl: {a1} resumed\n' and t['attention'] is None and t['status'] == 'running', (err, t))
    tc('ask', arid, 'Ship it?')
    tc('ask', a2, 'Also this?')
    rc, out, err = tc('done', arid, 'answered')
    r = verify_request(get_req(arid), 'after done on a waiting request')
    check('closing the request clears its question and its tasks\'', r['attention'] is None and r['waiting'] is False
          and all(t['attention'] is None for t in r['tasks']), r)
    rc, out, err = tc('ask', a1, 'too late?')
    check('ask on a closed task (strict): exit 1, 409', rc == 1 and '409' in err and 'already done' in err, (rc, err))
    rc, out, err = tc('ask', arid, 'too late?', strict=False)
    check('ask on a closed request (soft): exit 0, a warning with the hint', rc == 0 and 'taskctl: warning:' in err
          and 'the request is closed' in err, (rc, err))
    rc, out, err = tc('resume', a1)
    check('resume on a closed task: idempotent, rc 0', rc == 0, (rc, err))

    S.section('usage, changelog, api, flush')
    code, usage_text, _ = api(URL, 'GET', '/api/usage', raw=True)
    rc, out, err = tc('usage')
    check("usage: the board's usage text on stdout, verbatim", rc == 0 and out == usage_text.decode() and err == '', (rc, err))
    code, changelog, _ = api(URL, 'GET', '/api/changelog', raw=True)
    rc, out, err = tc('changelog')
    check("changelog: the board's changelog on stdout, newest first", rc == 0 and out == changelog.decode()
          and out.startswith('## v3 — '), (rc, out[:80], err))
    rc, out, err = tc('api', 'GET', '/api/health')
    check('api GET /api/health: the JSON body on stdout', rc == 0 and json.loads(out)['version'] == '3'
          and json.loads(out)['docs_version'] == docs_version(TASKS), (rc, out, err))
    rc, out, err = tc('api', 'get', f'/api/requests/{arid}')
    check('api: the method is case-insensitive, the body is the route\'s', rc == 0 and json.loads(out)['id'] == arid, (rc, err))
    rc, out, err = tc('api', 'POST', f'/api/tasks/{t6}/progress', '{"message": "via api"}')
    check('api POST with a JSON body: a 409 is printed and exits 1 (strict)', rc == 1 and json.loads(out)['status'] == 'done'
          and '409' in err, (rc, out, err))
    rc, out, err = tc('api', 'GET', '/api/requests/zzzzzz', strict=False)
    check('api 404 (soft): the error body on stdout, a warning, exit 0', rc == 0
          and json.loads(out) == {'error': "unknown request 'zzzzzz'"} and 'taskctl: warning:' in err and '404' in err, (rc, out, err))
    rc, out, err = tc('api', 'PUT', '/api/settings', '{"settings": {"sound": {"volume": 0.4}}}')
    check('api PUT: any method and route', rc == 0 and json.loads(out)['settings']['sound']['volume'] == 0.4, (rc, out, err))
    tc('api', 'DELETE', '/api/settings')
    rc, out, err = tc('flush')
    check('flush with nothing queued: rc 0, says so', rc == 0 and out == '' and err == f'taskctl: nothing queued for {URL}\n', (rc, err))
    rc, out, err = tc('ping')
    check('ping with nothing queued: no queue note', rc == 0 and 'queued' not in err, err)


def run_signals():
    S.section('run: signals and a request closed mid-run')
    rc, out, err = tc('new', 'Signals', '-t', 'a', '-t', 'b', '-t', 'c')
    rid, a, b, c = lines(out)
    seen = []
    stop = threading.Event()

    def poll():
        while not stop.is_set():
            t = get('/api/tasks/' + b)
            seen.append((t['status'], t['message']))
            time.sleep(0.05)

    th = threading.Thread(target=poll, daemon=True)
    th.start()
    rc, out, err = tc('run', b, '--every', '5', '--', 'sh', '-c', 'sleep 1.5; echo hi')
    stop.set()
    th.join()
    msgs = [m for s, m in seen if s == 'running']
    check('run: the first report is "running: CMD" (shell-quoted)', msgs and msgs[0] == "running: sh -c 'sleep 1.5; echo hi'", msgs[:3])
    check('run: the closing message', get('/api/tasks/' + b)['message'] == 'exit 0 after 2s', get('/api/tasks/' + b)['message'])

    p = subprocess.Popen([str(WRAPPER), '--strict', '--url', URL, 'run', a, '--', 'sh', '-c', 'echo up; exec sleep 30'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV, text=True, cwd=WORK)
    wait_until(lambda: get('/api/tasks/' + a)['status'] == 'running', 5, 0.1)
    time.sleep(0.5)
    p.send_signal(signal.SIGTERM)
    out, err = p.communicate(timeout=15)
    t = get('/api/tasks/' + a)
    check('run SIGTERM: forwarded, exit 143, task failed "killed by signal 15"', p.returncode == 143
          and t['status'] == 'failed' and t['message'] == 'killed by signal 15', (p.returncode, out, err, t['status'], t['message']))

    rc, out, err = tc('add', rid, 'late', '--start')
    late = lines(out)[0]
    p = subprocess.Popen([str(WRAPPER), '--strict', '--url', URL, 'run', late, '--', 'sh', '-c', 'sleep 1.5; echo nope >&2; exit 5'],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=ENV, text=True, cwd=WORK)
    time.sleep(0.6)
    rc2, out2, err2 = tc('done', rid, 'closing early')
    check('done RID mid-run: the running task auto-closed, the pending one cancelled', late in err2 and c in err2, err2)
    out, err = p.communicate(timeout=15)
    t = get('/api/tasks/' + late)
    check('run after its request closed: exit 5, the task overwritten to failed', p.returncode == 5 and t['status'] == 'failed'
          and t['message'].startswith('exit 5 after') and t['message'].endswith(': nope'), (p.returncode, err, t))
    r = verify_request(get_req(rid), 'late fail')
    check('late fail: the request stays done (completing a task never reopens it)', r['status'] == 'done', r['status'])

    S.section('run: the heartbeat re-sends an unchanged line (heartbeat shortened to 1.5s)')
    hb = TMP / 'heartbeat.py'
    hb.write_text('import importlib.util, sys\nsys.dont_write_bytecode = True\n'
                  "spec = importlib.util.spec_from_file_location('taskctl', sys.argv.pop(1))\n"
                  'tc = importlib.util.module_from_spec(spec)\nspec.loader.exec_module(tc)\n'
                  'tc.HEARTBEAT_SECONDS = 1.5\nsys.exit(tc.main(sys.argv[1:]))\n')
    rc, out, err = tc('new', 'Heartbeat', '-t', 'quiet step')
    hrid, htid = lines(out)
    beats, stop = set(), threading.Event()

    def watch():
        while not stop.is_set():
            t = get('/api/tasks/' + htid)
            if t['status'] == 'running' and t['message'] == 'one':
                beats.add(t['updated_at'])
            time.sleep(0.1)

    th = threading.Thread(target=watch, daemon=True)
    th.start()
    p = subprocess.run([sys.executable, str(hb), str(TASKCTL_PY), '--strict', '--url', URL, 'run', htid, '--',
                        'sh', '-c', 'echo one; sleep 5'], capture_output=True, text=True, env=ENV, cwd=WORK, timeout=30)
    stop.set()
    th.join()
    check('heartbeat: the unchanged line was reported at least twice', p.returncode == 0 and len(beats) >= 2,
          (p.returncode, p.stderr, sorted(beats)))
    tc('done', hrid)


# ---------------------------------------------------------------- a background grandchild outlives run

LOOP = 'while :; do echo tick; echo tock >&2; sleep 1; done'


def copiers_writing_to(path):
    found = []
    for pid in os.listdir('/proc'):
        if not pid.isdigit():
            continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                cmd = f.read()
            if b'os.read(0, 65536)' in cmd and os.readlink(f'/proc/{pid}/fd/1') == str(path):
                found.append(int(pid))
        except OSError:
            pass
    return found


def alive(pid):
    try:
        os.kill(pid, 0)
        with open(f'/proc/{pid}/stat') as f:
            return f.read().split(')')[-1].split()[0] != 'Z'
    except OSError:
        return False


def grandchild_case(label, tid, url, pipe_to_cat=False, code=5):
    d = TMP / 'grandchild'
    shutil.rmtree(d, ignore_errors=True)
    d.mkdir()
    out_path, err_path, pidf = d / 'out.txt', d / 'err.txt', d / 'pid'
    script = f'sh -c "{LOOP}" & echo $! > {pidf}; sleep 0.5; exit {code}'
    gpid = cat = None
    argv = [str(WRAPPER), '--url', url, 'run', tid, '--', 'sh', '-c', script]
    try:
        with open(out_path, 'wb') as out, open(err_path, 'wb') as err:
            t0 = time.monotonic()
            if pipe_to_cat:
                proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=err, env=ENV, cwd=WORK)
                cat = subprocess.Popen(['cat'], stdin=proc.stdout, stdout=out)
                proc.stdout.close()
            else:
                proc = subprocess.Popen(argv, stdout=out, stderr=err, env=ENV, cwd=WORK)
            rc = proc.wait(30)
            took = time.monotonic() - t0
        gpid = int(pidf.read_text().strip())
        ticks0, tocks0 = out_path.read_text().count('tick'), err_path.read_text().count('tock')
        time.sleep(3)
        ticks1, tocks1 = out_path.read_text().count('tick'), err_path.read_text().count('tock')
        err_text = err_path.read_text()
        check(f'{label}: taskctl exits with the child code {code}', rc == code, rc)
        check(f'{label}: taskctl exit not delayed by the grandchild ({took:.1f}s)', took < 6, took)
        check(f'{label}: the grandchild is alive 3s after taskctl exited', alive(gpid))
        check(f'{label}: its stdout kept flowing ({ticks0} -> {ticks1} ticks)', ticks1 >= ticks0 + 2)
        check(f'{label}: its stderr kept flowing ({tocks0} -> {tocks1} tocks)', tocks1 >= tocks0 + 2, err_text[-300:])
        check(f'{label}: no traceback on stderr', 'Traceback' not in err_text and 'Exception in thread' not in err_text, err_text[-300:])
        if not pipe_to_cat:
            copiers = copiers_writing_to(out_path) + copiers_writing_to(err_path)
            check(f'{label}: one copier per pipe, each detached into its own session',
                  len(copiers_writing_to(out_path)) == 1 and len(copiers_writing_to(err_path)) == 1
                  and all(os.getsid(c) == c for c in copiers), copiers)
        os.kill(gpid, signal.SIGTERM)
        wait_until(lambda: not (copiers_writing_to(out_path) or copiers_writing_to(err_path)), 5, 0.1)
        if pipe_to_cat:
            try:
                cat.wait(5)
                cat_done = True
            except subprocess.TimeoutExpired:
                cat_done = False
            check(f'{label}: the downstream reader sees EOF once the grandchild is gone', cat_done)
        check(f'{label}: the copiers exit when the grandchild is killed',
              not copiers_writing_to(out_path) and not copiers_writing_to(err_path))
    finally:
        if gpid and alive(gpid):
            os.kill(gpid, signal.SIGKILL)
        if cat and cat.poll() is None:
            cat.kill()
        for c in copiers_writing_to(out_path) + copiers_writing_to(err_path):
            os.kill(c, signal.SIGKILL)


def grandchildren():
    S.section('run: a background grandchild outlives taskctl and keeps its output')
    if not os.path.isdir('/proc'):
        S.skip('grandchild cases', 'no /proc')
        return
    dead = 'http://127.0.0.1:1'
    grandchild_case('offline id, output to files', 'offline', dead)
    grandchild_case('unreachable board, real-looking tid', 'k3m9qa-1', dead, code=0)
    grandchild_case('offline id, stdout piped to cat', 'offline', dead, pipe_to_cat=True)
    rc, out, err = tc('new', 'Grandchild check', '-t', 'run it')
    rid, tid = lines(out)
    grandchild_case('live board', tid, URL, code=0)
    t = get('/api/tasks/' + tid)
    check('live board: task closed done by run', t['status'] == 'done' and t['message'].startswith('exit 0 after'),
          (t['status'], t['message']))
    tc('done', rid, 'checked')


# ---------------------------------------------------------------- install-rule, served files

def install_rule():
    S.section('install-rule')
    CC.mkdir(exist_ok=True)
    target = CC / 'CLAUDE.md'
    target.write_text('# My notes\n\nkeep me\n')
    rc, out, err = tc('install-rule', '--file', str(target))
    check('install-rule: installed', rc == 0 and out == '' and err.strip() == f'taskctl: rule installed in {target}', (rc, out, err))
    rc, out, err = tc('install-rule', '--file', str(target))
    check('install-rule twice: unchanged', rc == 0 and 'rule unchanged in' in err, (rc, err))
    text = target.read_text()
    check('install-rule: one block, prior content kept, blank-line separator',
          text.count('<!-- task-status:begin -->') == 1 and text.count('<!-- task-status:end -->') == 1
          and text.startswith('# My notes\n\nkeep me\n\n<!-- task-status:begin -->'), text)
    target.write_text(text.replace('<!-- task-status:begin -->\n', '<!-- task-status:begin -->\nold text\n') + 'after\n')
    rc, out, err = tc('install-rule', '--file', str(target))
    text2 = target.read_text()
    check('install-rule: stale block updated, trailing content kept', rc == 0 and 'rule updated in' in err
          and 'old text' not in text2 and text2.endswith('<!-- task-status:end -->\nafter\n'), (err, text2))
    rc, out, err = tc('install-rule', env=dict(ENV, CLAUDE_CONFIG_DIR=str(CC / 'fresh')))
    check('install-rule: defaults to $CLAUDE_CONFIG_DIR/CLAUDE.md and creates it', rc == 0
          and (CC / 'fresh' / 'CLAUDE.md').exists(), (rc, err))

    tree = tasks_copy(TMP / 'no-templates', without=('templates',))
    with ScratchServer(TMP, '--host', '127.0.0.1', script=tree / 'server.py', name='notemplates') as srv:
        f = CC / 'notemplate.md'
        f.write_text('keep\n')
        rc, out, err = tc('install-rule', '--file', str(f), strict=False, url=srv.url)
        check('install-rule with the rule template missing: exit 1 even without --strict, file untouched',
              rc == 1 and f.read_text() == 'keep\n' and 'template missing' in err, (rc, err))


def served_files():
    S.section('the served taskctl: rendered, runnable after download')
    code, data, hdrs = api(URL, 'GET', '/api/skill/taskctl.py', raw=True)
    text = data.decode()
    baked = [line for line in text.splitlines() if line.startswith('BAKED_URL')]
    check('served taskctl.py: 200 text/plain no-store', code == 200 and hdrs.get('Content-Type') == 'text/plain; charset=utf-8'
          and hdrs.get('Cache-Control') == 'no-store', hdrs)
    check('served taskctl.py: BAKED_URL is the real base', baked == [f'BAKED_URL = "{URL}"'], baked)
    check('served taskctl.py: no other render tokens left', '{{' not in text.replace("BAKED_URL.startswith('{{')", ''),
          [line for line in text.splitlines() if '{{' in line])
    check('served taskctl.py: BAKED_DOCS is the docs version, BAKED_API the API version',
          [line for line in text.splitlines() if line.startswith(('BAKED_DOCS', 'BAKED_API'))]
          == [f'BAKED_DOCS = "{docs_version(TASKS)}"', 'BAKED_API = "3"'] and hdrs.get('X-Tasks-Docs') == docs_version(TASKS),
          [line for line in text.splitlines() if line.startswith('BAKED_')])
    code, wrapper, _ = api(URL, 'GET', '/api/skill/taskctl', raw=True)
    check('served wrapper is byte-identical (unrendered)', wrapper == WRAPPER.read_bytes())
    code, crafted, _ = api(URL, 'GET', '/api/skill/taskctl.py', headers={'Host': 'x";rm -rf /'}, raw=True)
    cb = [line for line in crafted.decode().splitlines() if line.startswith('BAKED_URL')]
    check('crafted Host falls back to the socket address', cb == [f'BAKED_URL = "{URL}"'] and b'rm -rf' not in crafted, cb)
    dl = TMP / 'dl'
    dl.mkdir()
    (dl / 'taskctl.py').write_bytes(data)
    (dl / 'taskctl').write_bytes(wrapper)
    os.chmod(dl / 'taskctl', 0o755)
    os.chmod(dl / 'taskctl.py', 0o755)
    rc, out, err = tc('ping', url=None, exe=dl / 'taskctl')
    check('downloaded taskctl pings its BAKED_URL', rc == 0 and out == URL + '\n' and 'ok' in err, (rc, out, err))
    check('the repo taskctl.py keeps its three unrendered BAKED_ lines',
          [line for line in TASKCTL_PY.read_text().splitlines() if line.startswith('BAKED_')]
          == ['BAKED_URL = "{{BASE_URL}}"', 'BAKED_DOCS = "{{DOCS_VERSION}}"', 'BAKED_API = "{{VERSION}}"'])


if __name__ == '__main__':
    server = ScratchServer(TMP, '--host', '127.0.0.1', '--stale-after', str(STALE_AFTER), '--expire-after', str(EXPIRE_AFTER),
                           name='cli').start()
    URL = server.url
    try:
        main_flow()
        run_signals()
        grandchildren()
        install_rule()
        served_files()
    finally:
        code = server.stop()
    check('server exits 0 on SIGTERM and removes its pidfile', code == 0 and not server.pidfile.exists(), code)
    sys.exit(S.finish())
