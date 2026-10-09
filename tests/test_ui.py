#!/usr/bin/env python3
"""Dashboard UI in headless Firefox (skipped when Firefox is not installed).

A scratch board runs tests/ui/harness.py (the real server plus test hooks), seeded through the API
with a request in every state, then backdated in its scratch database where a state needs age
(stale). Each case loads a page with one tests/ui/*.js script injected; the script checks the page
from inside and posts a JSON result, asserted on here. Firefox's --screenshot mode loads the page and
exits once it has loaded; a slow <img> holds the load event until the script is done.

The v2 cases (needs input, alerts, settings) run on boards of their own, one per case that writes, so
no case sees another's events: the alert engine with Notification and AudioContext replaced by
recorders, the detail view, an outage, the reminder, the settings panel, the permission flow, reduced
motion, and the guidance on an insecure page (with a link to the https listener when openssl can make
a certificate for one).

The live stream's cases (stream_cases) run on boards of their own too (a board has one stream), each with
tests/lib/fake_ffmpeg.py first on its PATH as `ffmpeg`, so a stream's URL (http://fake.invalid/live, /hang,
/audio, /hevc?audio=0, /live?die=30...) says how the "source" behaves. They load the boards in iframes of a
/__frame page (the window each board sees is the iframe's, so any size and orientation can be had), with
the media stub (__media=stub) standing in for the browser's MediaSource and media element: the page's real
player code runs, against a stub that models what it depends on. The layout of the pane in every state and
orientation, the sound the browser holds back and the click that releases it, the settings block, a
replaced stream, a source that ends and restarts, an unplayable codec, the player's seek, jump, trim and
failure paths, parking, going to a request and Back, and an outage. The streamplay case (when ffmpeg can
encode H.264, and this Firefox can decode it) plays a real source with the real MediaSource. The existing
list and detail checks are repeated with a stream open (stream_check_*).

    python3 tests/test_ui.py [base] [v2] [stream]   only those groups of cases (all of them without arguments)
    FIREFOX=/path/to/firefox   use that binary
    UI_SHOTS_DIR=DIR           keep every case's screenshot there, for a human look (the stream cases then
                               leave their frames and stream open, so the screenshot shows them)
    UI_STREAM_URL=URL          also play this real source (a camera's, say) through a scratch board with the real
                               ffmpeg, in a case of its own (streamreal); off by default, so the suite reaches no
                               other host
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import quote

sys.dont_write_bytecode = True  # no __pycache__ in the checkout, even when run without run_all.sh
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from harness import (TESTS, FakeUpstream, ScratchServer, Suite, api, clean_env, fake_ffmpeg_dir, find_procs, free_port,  # noqa: E402
                     make_ts, real_ffmpeg_dir, scratch_home, self_signed_cert, skip_suite, tempdir)

S = Suite('ui')
check = S.check
FIREFOX = os.environ.get('FIREFOX') or shutil.which('firefox')
if not FIREFOX:
    skip_suite('firefox not installed (set FIREFOX=/path/to/firefox to use another binary)')
TMP = tempdir('ui')
HOME = scratch_home(TMP)
RESULTS = TMP / 'results'
SHOTS = TMP / 'shots'
SHOTS.mkdir()
UI = TESTS / 'ui'
STALE_AFTER = 300
BG = {'light': 'rgb(245, 246, 248)', 'dark': 'rgb(14, 17, 22)'}
XSS_IMG = '<img src=x onerror=alert(1)>'
XSS_SCRIPT = '</script><script>alert(1)</script>'
RUNNING: set[subprocess.Popen] = set()   # the Firefox instances running now


STOPPING = []


def _terminate(signum, frame):
    # Stop the browsers and start no more, so the case threads finish; then the servers and temp
    # dirs are cleaned up as usual.
    STOPPING.append(signum)
    for proc in list(RUNNING):
        proc.kill()
    raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, _terminate)


# ---------------------------------------------------------------- fixture

def seed(base: str, db_path: Path) -> dict:
    def call(method, path, body=None):
        code, data, _ = api(base, method, path, body, headers={'Content-Type': 'application/json'})
        assert code in (200, 201), (method, path, code, data)
        return data

    def new(title, tasks=(), origin='verify-host:ui'):
        r = call('POST', '/api/requests', {'title': title, 'origin': origin, 'tasks': list(tasks)})
        return r['id'], [t['id'] for t in r['tasks']]

    def progress(tid, msg, pct=None, eta=None):
        body = {'message': msg}
        if pct is not None:
            body['percent'] = pct
        if eta is not None:
            body['eta_seconds'] = eta
        call('POST', f'/api/tasks/{tid}/progress', body)

    def tdone(tid, status='done', msg=None):
        call('POST', f'/api/tasks/{tid}/complete', {'status': status, **({'message': msg} if msg else {})})

    def rdone(rid, status='done', msg=None):
        call('POST', f'/api/requests/{rid}/complete', {'status': status, **({'message': msg} if msg else {})})

    ids = {}
    for i in range(12):   # finished ones first, so the running ones are the newest
        status = 'failed' if i % 5 == 3 else 'done'
        rid, tids = new(f'Nightly batch job number {i + 1:02d}', [f'step {j + 1}' for j in range(1 + i % 4)], f'buildbox-{i % 3}:nightly')
        for j, tid in enumerate(tids):
            progress(tid, f'working on step {j + 1}', 50)
            tdone(tid, 'failed' if status == 'failed' and j == len(tids) - 1 else 'done', f'step {j + 1} finished')
        rdone(rid, status, None if status == 'done' else 'one step failed')

    rid, tids = new('Refactor the settings loader', ['read old config', 'write migration', 'update docs', 'cleanup'])
    progress(tids[0], 'reading', 30)
    tdone(tids[0], 'done', 'read 14 keys')
    progress(tids[1], 'migrating', 80)
    rdone(rid, 'done', 'merged; docs deferred')                      # running -> done, pending -> cancelled
    ids['done_cancelled'] = rid
    rid, tids = new('Upgrade the image processing library', ['fetch the release', 'rebuild', 'run regression', 'publish'])
    progress(tids[0], 'downloading', 10)
    tdone(tids[0])
    progress(tids[1], 'compiling module 12 of 20', 60)
    tdone(tids[1], 'failed', 'linker error:\n  undefined reference to img_init\n  1 error generated')
    progress(tids[2], 'queued run', 5)
    rdone(rid, 'failed', 'build failed at the link step\nsee the rebuild task for details')
    ids['failed'] = rid
    rid, _ = new('Quick answer with no tasks')
    rdone(rid, 'done', 'answered inline')
    rid, tids = new('Close out without starting', ['lint', 'format'])
    tdone(tids[0], 'done', 'no issues')
    tdone(tids[1])
    rdone(rid)
    ids['inferred_done'] = rid
    rid, tids = new('Abandoned exploration', ['look around'])
    progress(tids[0], 'poking at the codebase', 20)
    rdone(rid, 'failed', 'expired: no activity for 24h')

    rid, tids = new('Mixed request with an inferred start', ['already finished', 'later'])
    tdone(tids[0], 'done', 'finished without a start call')
    rid, tids = new('Waiting for a worker slot', ['shard 1', 'shard 2', 'shard 3'])
    ids['queued'] = rid
    rid, _ = new('Thinking about the problem (no tasks yet)')
    ids['zero_running'] = rid
    rid, tids = new('Long render with a generous ETA', ['render frames'])
    progress(tids[0], 'frame 120 of 900', 13, eta=600)
    ids['eta_fresh'] = rid
    rid, tids = new('Agent went quiet', ['crawl logs', 'summarise'])
    progress(tids[0], 'reading logs/2026-09-27.log', 42)
    ids['stale'] = rid
    rid, tids = new(XSS_IMG, [XSS_SCRIPT, XSS_IMG, 'plain'], origin='<b>bold</b>' + XSS_IMG)
    progress(tids[0], XSS_SCRIPT + '\n' + XSS_IMG, 50, eta=120)
    tdone(tids[1], 'failed', XSS_SCRIPT)
    ids['xss'] = rid
    long_title = ('Investigate why the export job drops records when the input buffer rolls over during a long '
                  'batch run with forty workers and retries enabled on every one of the upstream connections')
    long_title = (long_title + ' ' + 'x' * 190)[:190]
    long_origin = ('a-very-long-hostname-without-any-spaces-at-all.example.internal:' + 'repo_' * 10)[:100]
    rid, tids = new(long_title, ['one very long task title ' * 7], origin=long_origin)
    progress(tids[0], ('scanning /var/lib/export/2026/09/28/batch-000123/records/chunk-0004567.bin at offset '
                       '0x7fffffff; ') * 4, 71, eta=900)
    ids['long'] = rid
    rid, tids = new('Request for the Mark done button', ['running step', 'queued step'])
    progress(tids[0], 'working on it', 40)
    ids['actions'] = rid
    ids['deleted'] = new('Request deleted behind the page', ['a'])[0]
    ids['delbutton'] = new('Request for the Delete button', ['a'])[0]
    rid, tids = new('Deploy the web service to staging', ['build image', 'push image', 'migrate database', 'smoke test', 'canary'])
    progress(tids[0], 'building', 50)
    tdone(tids[0], 'done', 'image 3f9c2a built')
    progress(tids[1], 'pushed 312 MB of 690 MB\nlayer 7 of 12\nregistry: staging.local', 45, eta=300)
    progress(tids[2], 'waiting for the lock on schema_migrations', 20, eta=0)   # overdue from now on
    progress(tids[4], 'canary at 5%', 37)
    tdone(tids[4], 'failed', 'error rate 4.1% above the 2% threshold')
    ids['main'], ids['main_tasks'] = rid, tids

    # Age what must look old: the stale request goes silent past --stale-after, and the ETA one too
    # (its running ETA keeps it fresh anyway).
    now = time.time()
    with sqlite3.connect(db_path, timeout=5) as db:
        for rid, eta in ((ids['stale'], None), (ids['eta_fresh'], now + 480)):
            db.execute('UPDATE requests SET created_at = ?, updated_at = ? WHERE id = ?', (now - 1900, now - STALE_AFTER - 100, rid))
            db.execute('UPDATE tasks SET created_at = ?, updated_at = ?, started_at = CASE WHEN started_at IS NULL THEN NULL ELSE ? END,'
                       ' eta_at = COALESCE(?, eta_at) WHERE request_id = ?', (now - 1900, now - STALE_AFTER - 100, now - 1800, eta, rid))
        db.execute('UPDATE requests SET created_at = ? WHERE id = ?', (now - 725, ids['main']))
    return ids


# ---------------------------------------------------------------- firefox

def firefox(name: str, base: str, path: str, width: int, theme: str, script: str, wait: int, extra: str = '',
            prefs: tuple = (), height: int = 900) -> dict | None:
    if STOPPING:
        return None
    prof = TMP / f'profile-{name}'
    prof.mkdir()
    dark = theme == 'dark'
    (prof / 'user.js').write_text('\n'.join((*prefs,
        f'user_pref("ui.systemUsesDarkTheme", {int(dark)});',
        f'user_pref("layout.css.prefers-color-scheme.content-override", {0 if dark else 1});',
        'user_pref("network.proxy.type", 0);',
        'user_pref("browser.shell.checkDefaultBrowser", false);',
        'user_pref("datareporting.policy.dataSubmissionEnabled", false);',
        'user_pref("toolkit.telemetry.reportingpolicy.firstRun", false);',
        'user_pref("browser.startup.homepage_override.mstone", "ignore");', '')))
    page, _, frag = path.partition('#')
    sep = '&' if '?' in page else '?'
    url = f'{base}{page}{sep}__test={script}&__name={name}&__wait={wait}{extra}' + (f'#{frag}' if frag else '')
    env = clean_env(HOME=HOME, MOZ_CRASHREPORTER_DISABLE=1, XDG_CACHE_HOME=HOME / '.cache', XDG_CONFIG_HOME=HOME / '.config')
    shot = SHOTS / f'{name}.png'
    proc = subprocess.Popen([FIREFOX, '--headless', '--no-remote', '--profile', str(prof), f'--window-size={width},{height}',
                             '--screenshot', str(shot), url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    RUNNING.add(proc)
    try:
        proc.wait(wait / 1000 + 60)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    finally:
        RUNNING.discard(proc)
    shutil.rmtree(prof, ignore_errors=True)
    result = RESULTS / f'{name}.json'
    try:
        return json.loads(result.read_text())
    except (OSError, ValueError):
        return None


def in_parallel(cases, workers=6) -> dict:
    """Runs firefox(*case) for each case, several at a time; returns {name: result}."""
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {case[0]: pool.submit(firefox, *case) for case in cases}
        return {name: f.result() for name, f in futures.items()}


def got(name, r) -> bool:
    return check(f'{name}: the page script posted a result', r is not None and 'crashed' not in r, r)


# ---------------------------------------------------------------- assertions

def assert_check(name, r, theme, rows=None, running=True):
    if not got(name, r):
        return
    check(f'{name}: no problems (overflow, separators, numbers vs the API, XSS, JS errors)', r['problems'] == [], r['problems'][:10])
    check(f'{name}: no horizontal scroll', r['scrollWidth'] <= r['viewport'], (r['scrollWidth'], r['viewport']))
    check(f'{name}: {theme} theme', r['theme'] == theme and r['bodyBg'] == BG[theme], (r['theme'], r['bodyBg']))
    if rows is not None:
        check(f'{name}: every item rendered ({rows})', r['checks']['rows'] == rows, r['checks'])
    if running:
        check(f'{name}: the running-time ticker advances', len(r.get('tickSamples') or []) >= 2, r.get('tickSamples'))
        check(f'{name}: connection live', r['conn'] == 'live', r['conn'])


def read_only_cases(base, empty_base, ids):
    main = ids['main']
    lst = api(base, 'GET', '/api/requests')[1]
    n_requests = len(lst['requests'])
    detail_rows = {rid: len(api(base, 'GET', f'/api/requests/{rid}')[1]['tasks']) for rid in (main, ids['xss'], ids['failed'], ids['long'])}
    cases = [
        ('list_390_light', base, '/', 390, 'light'), ('list_390_dark', base, '/', 390, 'dark'),
        ('list_1280_light', base, '/', 1280, 'light'),
        ('main_390_light', base, f'/r/{main}#{main}-3', 390, 'light'), ('main_390_dark', base, f'/r/{main}', 390, 'dark'),
        ('main_1280_light', base, f'/r/{main}', 1280, 'light'), ('xss_390', base, f"/r/{ids['xss']}", 390, 'light'),
        ('failed_390_dark', base, f"/r/{ids['failed']}", 390, 'dark'), ('long_390', base, f"/r/{ids['long']}", 390, 'light'),
        ('zero_390', base, f"/r/{ids['zero_running']}", 390, 'light'), ('stale_390', base, f"/r/{ids['stale']}", 390, 'light'),
        ('taskpath', base, f'/r/{main.upper()}-3', 390, 'light'), ('unknown', base, '/r/zzzzzz', 390, 'light'),
        ('invalid', base, '/r/bogus!', 390, 'light'), ('nopath', base, '/r/a/b', 390, 'light'),
        ('empty_board', empty_base, '/', 390, 'light'),
    ]
    res = in_parallel([(name, b, path, width, theme, 'check', 7000) for name, b, path, width, theme in cases])

    S.section('list view')
    for name in ('list_390_light', 'list_390_dark', 'list_1280_light'):
        theme = name.rsplit('_', 1)[1]
        assert_check(name, res[name], theme, rows=n_requests)
        r = res[name]
        if r:
            check(f'{name}: tab title counts the running requests', r['title'] == f"({lst['counts']['running']} running) Task Status", r['title'])
            check(f'{name}: one stale request in the summary', r['counts']['stale'] == 1 and 'stale' in r['summary'], r['summary'])
            check(f'{name}: not the empty state', r['empty'] is False and r['view'] == 'list')
    S.section('detail view')
    for name, key, theme in (('main_390_light', main, 'light'), ('main_390_dark', main, 'dark'), ('main_1280_light', main, 'light'),
                             ('xss_390', ids['xss'], 'light'), ('failed_390_dark', ids['failed'], 'dark'),
                             ('long_390', ids['long'], 'light')):
        assert_check(name, res[name], theme, rows=detail_rows[key], running=key != ids['failed'])
    r = res['main_390_light']
    if r:
        check('main: the #tid deep link highlights that task', r['target'] == [f'{main}-3'], r['target'])
        check('main: header shows X/Y and an ETA', r['header']['tasks'] == '1/5 tasks' and r['header']['eta'].endswith(' left'), r['header'])
        check('main: tab title is percent + title', r['title'].endswith(' · Deploy the web service to staging — Task Status'), r['title'])
    r = res['xss_390']
    if r:
        check('xss: agent text shown as text (title intact, no <img>, no alert)', r['imgs'] == 0 and r['alerts'] == []
              and r['header']['origin'] == '<b>bold</b>' + XSS_IMG and r['title'].endswith(XSS_IMG + ' — Task Status'), r['header'])
    r = res['failed_390_dark']
    if r:
        check('failed: no Mark buttons on a closed request, message shown', r['header']['markDoneHidden'] is True
              and r['header']['message'].startswith('build failed at the link step'), r['header'])
        check('failed: the cancelled task is noted', r['header']['cancelled'] == '1 cancelled', r['header'])
    assert_check('zero_390', res['zero_390'], 'light', rows=0)
    r = res['zero_390']
    if r:
        check('zero tasks: "no tasks", no percent, the empty-task hint', r['header']['tasks'] == 'no tasks' and r['header']['pct'] == '—'
              and r['taskEmpty'] is True, r['header'])
    assert_check('stale_390', res['stale_390'], 'light', rows=2, running=False)
    r = res['taskpath']
    if got('taskpath', r):
        check('a task id in the path shows its request with that task highlighted', r['path'] == f'/r/{main}'
              and r['hash'] == f'#{main}-3' and r['target'] == [f'{main}-3'] and r['view'] == 'detail', (r['path'], r['hash'], r['target']))
    S.section('not-found views')
    for name, want in (('unknown', 'Request not found'), ('invalid', 'Not a request id'), ('nopath', 'Page not found')):
        r = res[name]
        if got(name, r):
            check(f'{name}: "{want}"', r['view'] == 'notfound' and r['notFound'] == want and r['title'] == 'Not found — Task Status'
                  and r['problems'] == [], (r.get('notFound'), r.get('problems')))
    r = res['unknown']
    if r:
        check('unknown: the text names the id', 'zzzzzz' in r['nfText'], r['nfText'])
    r = res['empty_board']
    if got('empty_board', r):
        check('empty board: the empty state with a quickstart for this board', r['empty'] is True and r['problems'] == []
              and f"-X POST {empty_base}/api/requests" in r['quickstart'] and r['title'] == 'Task Status', (r.get('problems'), r.get('quickstart')))


def stateful_cases(base, srv, ids):
    # Three rounds: pages that change nothing on the server (reconcile only updates one task, which
    # moves no row); then the ones that close or delete their own request; then Clear, alone.
    res = in_parallel([('reconcile', base, '/', 390, 'light', 'reconcile', 6000, f"&__tid={ids['main_tasks'][1]}"),
                       ('hidden', base, '/', 390, 'light', 'hidden', 17000),
                       ('banner', base, '/', 390, 'light', 'banner', 13000)])
    api(base, 'GET', '/__log?clear=1')
    res.update(in_parallel([('actions', base, f"/r/{ids['actions']}", 390, 'light', 'actions', 7000),
                            ('deleted', base, f"/r/{ids['deleted']}", 390, 'light', 'deleted', 11000,
                             '&__stub=1&__local=' + json.dumps({'sound_enabled': True, 'notify_enabled': True, 'remind_every': 0})),
                            ('delbutton', base, f"/r/{ids['delbutton']}", 390, 'light', 'delbutton', 6000)]))
    log = api(base, 'GET', '/__log')[1]
    res.update(in_parallel([('clear', base, '/', 390, 'light', 'clear', 7000)]))

    S.section('keyed reconcile (a progress update arrives by poll)')
    r = res['reconcile']
    if got('reconcile', r):
        check('reconcile: the progress POST worked', r['post'] == 200, r['post'])
        check('reconcile: every row element survives', r['rowsBefore'] == r['rowsAfter'] == r['sameObjects'] > 0, r)
        check('reconcile: no childList churn in the row containers', r['mutations'] == 0, r['mutations'])
        check('reconcile: the updated row changed in place', r['bumped'] and r['bumpedSame']
              and 'bumped by the reconcile test' in r['bumpedHint'], (r['bumped'], r['bumpedSame'], r['bumpedHint']))
        check('reconcile: elapsed ticks every second', r['textChanges'] >= 3, r['textChanges'])
        check('reconcile: the favicon is a drawn progress ring', r['favicon'].startswith('data:image/svg+xml,'), r['favicon'][:60])
        check('reconcile: no JS errors', r['errors'] == [], r['errors'])

    S.section('a hidden tab keeps polling, every 5s, and polls at once when shown')
    r = res['hidden']
    if got('hidden', r):
        cycles = [t for t, url in r['polls'] if url.startswith('/api/events')]
        during = [t for t in cycles if r['hiddenAt'] + 100 < t < r['shownAt']]
        gaps = [b - a for a, b in zip(during, during[1:])]
        after = [t for t in cycles if r['shownAt'] <= t < r['shownAt'] + 600]
        later = [t for t in cycles if t >= r['shownAt']]
        check('hidden: it keeps polling (each cycle reads /api/events, then the view)', len(during) >= 2
              and any(url.startswith('/api/requests') and r['hiddenAt'] < t < r['shownAt'] for t, url in r['polls']), r['polls'])
        check('hidden: about every 5s, not every 1.5s', gaps and all(4400 <= g <= 7000 for g in gaps)
              and during[0] - r['hiddenAt'] >= 1200, (r['hiddenAt'], during, gaps))
        check('hidden: polls again as soon as it is shown', len(after) >= 1, (r['shownAt'], cycles))
        check('hidden: then every 1.5s again', len(later) >= 2 and all(b - a < 3000 for a, b in zip(later, later[1:])), later)
        check('hidden: no JS errors', r['errors'] == [], r['errors'])

    S.section('the offline banner')
    r = res['banner']
    if got('banner', r):
        check('banner: live -> reconnecting -> offline', r['states'] == ['live', 'reconnecting', 'offline'], r['states'])
        check('banner: shown with the error and the retry countdown', (r['bannerText'] or '').startswith("Can't reach the task server (network error). ")
              and 'last update' in r['bannerText'] and r['bannerHidden'] is False, r['bannerText'])
        check('banner: the page dims (body.offline)', r['offlineClass'] is True)
        check('banner: exponential backoff between attempts', r['gaps'] and r['gaps'][0] >= 2500, r['gaps'])

    S.section('Mark done')
    r = res['actions']
    if got('actions', r):
        rid = ids['actions']
        check('actions: the button shows while running', r['hiddenBefore'] is False)
        check('actions: confirm asked first', any(a.startswith('confirm:Mark "Request for the Mark done button" as done?') for a in r['alerts']), r['alerts'])
        check('actions: closed done on the server, with the UI message', r['status'] == 'done' and r['message'] == 'marked done from the web UI', r)
        check('actions: running -> done, queued -> cancelled', r['tasks'] == [f'{rid}-1:done', f'{rid}-2:cancelled'], r['tasks'])
        check('actions: the page follows (badge, buttons hidden)', r['badge'] == 'done' and r['headStatus'] == 'done'
              and r['markDoneHidden'] and r['markFailedHidden'], r)
        check('actions: task badges and counts', r['taskBadges'] == ['done', 'cancelled'] and r['header'] == '1/1 tasks | 1 cancelled', r)
        check('actions: no alerts beyond the confirm, no JS errors', len(r['alerts']) == 1 and r['errors'] == [], r)

    S.section('deleted behind the page')
    r = res['deleted']
    if got('deleted', r):
        check('deleted: the DELETE worked', r['deleteStatus'] == 200, r['deleteStatus'])
        check('deleted: the next poll shows the deleted view', r['view'] == 'notfound' and r['nfTitle'] == 'This request was deleted.'
              and r['title'] == 'Not found — Task Status', r)
        check("deleted: the request's own polls stop", r['polls'] and not [u for u in r['polls'] if u.startswith('/api/requests')],
              r['polls'])
        check('deleted: the events polls go on (every 1.5s)', len([u for u in r['polls'] if u.startswith('/api/events?since=')]) >= 2,
              r['polls'])
        check('deleted: a question asked elsewhere afterwards still notifies and chimes; the view stays', r['view'] == r['viewAfter'] == 'notfound'
              and 'Needs input: Asked after the deletion' in r['notes'] and 'attention' in r['chimes']
              and "r='15' fill='#c026d3'" in r['favicon'] and r['errors'] == [], r)

    S.section('the Delete button')
    rid = ids['delbutton']
    r = res['delbutton']
    if got('delbutton', r):
        methods = [m + ' ' + p.split('?')[0] for m, p in log]
        deleted_at = methods.index(f'DELETE /api/requests/{rid}') if f'DELETE /api/requests/{rid}' in methods else -1
        check('delete button: DELETE sent', deleted_at >= 0, methods)
        check('delete button: then back to the list', 'GET /' in methods[deleted_at + 1:], methods)
        check('delete button: the request is gone', api(base, 'GET', f'/api/requests/{rid}')[0] == 404)

    S.section('Clear finished (last: it deletes every finished request)')
    r = res['clear']
    if got('clear', r):
        check('clear: confirm asked with the count', any(a.startswith(f"confirm:Delete {r['finishedBefore']} finished requests?") for a in r['alerts']), r['alerts'])
        check('clear: every finished request deleted on the server', r['finishedBefore'] > 0 and r['apiFinished'] == 0, r)
        check('clear: the Finished group empties and hides', r['finishedAfter'] == 0 and r['groupHidden'] is True, r)
        check('clear: the running requests stay', r['activeAfter'] == r['activeBefore'] == r['apiRunning'] > 0, r)
        check('clear: the click did not toggle the group', r['openAfter'] == r['openBefore'], r)
        check('clear: no JS errors', r['errors'] == [], r['errors'])
    check('the harness server logged no tracebacks', 'Traceback' not in srv.log_text(), srv.log_text()[-1500:])


# ---------------------------------------------------------------- v2: needs input, alerts, settings

V2_WAIT = 'The staging database has 3 unmigrated rows.\nDrop them, or keep them and migrate by hand?'


def seed_v2(base: str) -> dict:
    """A board with every kind of waiting: a task question (multi-line), a request's own question, two
    questions in one request, beside the usual running, failed and finished requests."""
    def call(method, path, body=None):
        code, data, _ = api(base, method, path, body, headers={'Content-Type': 'application/json'})
        assert code in (200, 201), (method, path, code, data)
        return data

    def new(title, tasks=(), origin='laptop:demo'):
        r = call('POST', '/api/requests', {'title': title, 'origin': origin, 'tasks': list(tasks)})
        return r['id'], [t['id'] for t in r['tasks']]

    ids = {}
    for i in range(3):
        rid, tids = new(f'Nightly report {i + 1}', ['collect', 'render'])
        for t in tids:
            call('POST', f'/api/tasks/{t}/progress', {'message': 'working', 'percent': 50})
            call('POST', f'/api/tasks/{t}/complete', {'status': 'done', 'message': 'ok'})
        call('POST', f'/api/requests/{rid}/complete', {'status': 'done', 'message': 'report published'})
    rid, tids = new('Upgrade the image library', ['fetch', 'rebuild'])
    call('POST', f'/api/tasks/{tids[1]}/progress', {'message': 'compiling', 'percent': 60})
    call('POST', f'/api/requests/{rid}/complete', {'status': 'failed', 'message': 'build failed at the link step'})
    rid, tids = new('Render the product video', ['encode'])
    call('POST', f'/api/tasks/{tids[0]}/progress', {'message': 'frame 300 of 900', 'percent': 33, 'eta_seconds': 600})
    ids['plain'] = rid
    rid, tids = new('Migrate the settings loader', ['read old config', 'write migration', 'update docs'])
    call('POST', f'/api/tasks/{tids[0]}/complete', {'status': 'done', 'message': 'read 14 keys'})
    call('POST', f'/api/tasks/{tids[1]}/progress', {'message': 'writing the migration', 'percent': 40})
    call('POST', f'/api/tasks/{tids[1]}/attention', {'message': V2_WAIT})
    ids['task_wait'], ids['task_wait_tid'] = rid, tids[1]
    rid, tids = new('Clean up the release branch', ['list stale branches', 'delete them'])
    call('POST', f'/api/tasks/{tids[0]}/complete', {'status': 'done', 'message': '14 stale branches'})
    call('POST', f'/api/requests/{rid}/attention', {'message': 'OK to delete 14 remote branches older than 90 days?'})
    ids['req_wait'] = rid
    rid, tids = new('Two questions at once', ['left', 'right', 'middle'])
    call('POST', f'/api/tasks/{tids[0]}/attention', {'message': 'Left first?'})
    call('POST', f'/api/tasks/{tids[1]}/attention', {'message': 'Right too?'})
    call('POST', f'/api/tasks/{tids[2]}/progress', {'message': 'going', 'percent': 10})
    ids['two_wait'] = rid
    return ids


def one_request(base: str, title: str, n: int, start: bool = True):
    code, r, _ = api(base, 'POST', '/api/requests', {'title': title, 'tasks': [f'step {chr(97 + i)}' for i in range(n)]},
                     headers={'Content-Type': 'application/json'})
    assert code == 201, r
    tids = [t['id'] for t in r['tasks']]
    for t in tids if start else ():
        api(base, 'POST', f'/api/tasks/{t}/progress', {'message': 'started', 'percent': 5}, headers={'Content-Type': 'application/json'})
    return r['id'], tids


def chime_gaps(chimes):
    return [b[0] - a[0] for a, b in zip(chimes, chimes[1:])]


def v2_cases(env):
    made = self_signed_cert(TMP)
    tls_port = free_port() if made else None
    tls = ['--tls-port', str(tls_port), '--tls-cert', str(made[0]), '--tls-key', str(made[1])] if made else []
    boards = {name: ScratchServer(TMP, '--host', '127.0.0.1', '--stale-after', '600', *(tls if name == 'v2' else []),
                                  script=UI / 'harness.py', name=f'ui-{name}', env=env).start()
              for name in ('v2', 'eng', 'engd', 'engo', 'engr', 'engh', 'panel')}
    try:
        _v2_cases(boards, tls_port)
    finally:
        for srv in boards.values():
            srv.stop()
        for name, srv in boards.items():
            check(f'the {name} board logged no tracebacks', 'Traceback' not in srv.log_text(), srv.log_text()[-1500:])


def _v2_cases(boards, tls_port):
    v2 = boards['v2'].url
    ids = seed_v2(v2)
    eng_rid, eng_tids = one_request(boards['eng'].url, 'Engine test request', 6)
    engd_rid, engd_tids = one_request(boards['engd'].url, 'Render the launch video', 2)
    engo_rid, engo_tids = one_request(boards['engo'].url, 'Offline test request', 5)
    engr_rid, engr_tids = one_request(boards['engr'].url, 'Reminder test request', 1)
    engh_rid, engh_tids = one_request(boards['engh'].url, 'Held audio test request', 2)
    panel_rid, panel_tids = one_request(boards['panel'].url, 'Panel test request', 1)
    api(boards['panel'].url, 'POST', f'/api/tasks/{panel_tids[0]}/attention', {'message': 'Waiting while the panel is tested?'},
        headers={'Content-Type': 'application/json'})
    stub = '&__stub=1&__local=' + json.dumps({'sound_enabled': True, 'notify_enabled': True, 'remind_every': 0})
    lst = api(v2, 'GET', '/api/requests')[1]
    rows = {rid: len(api(v2, 'GET', f'/api/requests/{rid}')[1]['tasks']) for rid in (ids['task_wait'], ids['req_wait'], ids['two_wait'])}
    cases = [
        ('v2_list_390_light', v2, '/', 390, 'light', 'check', 7000), ('v2_list_390_dark', v2, '/', 390, 'dark', 'check', 7000),
        ('v2_list_1280_light', v2, '/', 1280, 'light', 'check', 7000),
        ('v2_task_wait_390', v2, f"/r/{ids['task_wait']}", 390, 'light', 'check', 7000),
        ('v2_task_wait_1280_dark', v2, f"/r/{ids['task_wait']}", 1280, 'dark', 'check', 7000),
        ('v2_req_wait_390_dark', v2, f"/r/{ids['req_wait']}", 390, 'dark', 'check', 7000),
        ('v2_two_wait_390', v2, f"/r/{ids['two_wait']}", 390, 'light', 'check', 7000),
        ('v2_motion', v2, '/', 390, 'light', 'motion', 4000, '', ('user_pref("ui.prefersReducedMotion", 1);',)),
        ('v2_motion_detail', v2, f"/r/{ids['req_wait']}", 390, 'light', 'motion', 4000, '', ('user_pref("ui.prefersReducedMotion", 1);',)),
        ('v2_insecure', v2, f"/r/{ids['plain']}", 390, 'light', 'insecure', 5000, '&__insecure=1&__stub=1'),
        ('v2_perm', v2, '/', 390, 'light', 'perm', 5000, '&__stub=1&__perm=default'),
        ('v2_engine', boards['eng'].url, '/', 1280, 'light', 'engine', 70000, stub + f"&__rid={eng_rid}&__tids={','.join(eng_tids)}"),
        ('v2_detail', boards['engd'].url, f'/r/{engd_rid}', 390, 'light', 'detailengine', 25000,
         stub + f"&__rid={engd_rid}&__tids={','.join(engd_tids)}"),
        ('v2_offline', boards['engo'].url, '/', 1280, 'light', 'offline', 40000, stub + f"&__rid={engo_rid}&__tids={','.join(engo_tids)}"),
        ('v2_remind', boards['engr'].url, '/', 390, 'light', 'remind', 50000,
         '&__stub=1&__insecure=1&__local=' + json.dumps({'sound_enabled': True, 'notify_enabled': True, 'remind_every': 3})
         + f"&__tids={','.join(engr_tids)}"),
        ('v2_panel', boards['panel'].url, '/', 1280, 'light', 'panel', 35000, '&__stub=1'),
        ('v2_held', boards['engh'].url, '/', 390, 'light', 'held', 24000,
         '&__stub=1&__suspended=1&__local=' + json.dumps({'sound_enabled': True, 'notify_enabled': False, 'remind_every': 2})
         + f"&__tids={','.join(engh_tids)}"),
    ]
    res = in_parallel(cases)
    RESULTS_V2.update(res)

    S.section('v2: needs input on the list: marked, first in Active, in the summary, the question shown')
    for name in ('v2_list_390_light', 'v2_list_390_dark', 'v2_list_1280_light'):
        theme = name.rsplit('_', 1)[1]
        assert_check(name, res[name], theme, rows=len(lst['requests']))
        r = res[name]
        if r:
            check(f'{name}: the three waiting requests lead Active', set(r['activeOrder'][:3]) == {ids['task_wait'], ids['req_wait'], ids['two_wait']}
                  and len(r['waitingRows']) == 3, r['activeOrder'])
            check(f'{name}: the summary leads with "3 waiting for you"', r['summary'].startswith('3 waiting for you'), r['summary'])
            w = {x['id']: x for x in r['waitingRows']}
            check(f"{name}: a request's own question; a task's question; two questions: the first and +1 more",
                  w[ids['req_wait']]['ask'] == 'OK to delete 14 remote branches older than 90 days?' and w[ids['task_wait']]['ask'] == V2_WAIT
                  and w[ids['two_wait']]['ask'] == 'Left first?' and w[ids['two_wait']]['more'] == '+1 more', w)
    S.section('v2: needs input in the detail view')
    for name, key, theme in (('v2_task_wait_390', 'task_wait', 'light'), ('v2_task_wait_1280_dark', 'task_wait', 'dark'),
                             ('v2_req_wait_390_dark', 'req_wait', 'dark'), ('v2_two_wait_390', 'two_wait', 'light')):
        assert_check(name, res[name], theme, rows=rows[ids[key]])
    r = res['v2_task_wait_390']
    if r:
        check("a task waits: the header says so (data-waiting=task), the task row carries the question", r['header']['waiting'] == 'task'
              and r['header']['ask'] is None and r['header']['badge'] == 'needs input'
              and r['waitingTasks'] == [{'id': ids['task_wait_tid'], 'ask': V2_WAIT}], (r['header'], r['waitingTasks']))
    r = res['v2_req_wait_390_dark']
    if r:
        check("the request waits: the header's own question (data-waiting=request)", r['header']['waiting'] == 'request'
              and r['header']['ask'] == 'OK to delete 14 remote branches older than 90 days?' and r['waitingTasks'] == [], r['header'])
    r = res['v2_two_wait_390']
    if r:
        check('two tasks wait: both rows carry their question', [x['ask'] for x in r['waitingTasks']] == ['Left first?', 'Right too?'], r['waitingTasks'])

    S.section('v2: reduced motion: a static strong outline instead of the pulse')
    for name in ('v2_motion', 'v2_motion_detail'):
        r = res[name]
        if got(name, r):
            check(f'{name}: the browser reports reduced motion', r['reduced'] is True, r)
            check(f'{name}: every waiting row: no animation, an outline shadow', r['rows'] and all(x['animation'] == 'none'
                  and x['shadow'] not in ('none', '') for x in r['rows']), r['rows'])

    S.section('v2: an insecure page explains notifications, and links to the https listener')
    r = res['v2_insecure']
    if got('v2_insecure', r):
        check('insecure: the guidance shows in the open panel', r['open'] and r['insecure'] is True and r['errors'] == [], r)
        if tls_port:
            want = f"https://127.0.0.1:{tls_port}/r/{ids['plain']}"
            check('insecure: this board has https: a link to the same page on its tls_port', r['tlsShown'] is True
                  and r['link'] == want and r['linkText'].startswith(f'https://127.0.0.1:{tls_port}'), (r['link'], r['linkText'], want))
        else:
            check('insecure: no https listener, no link', r['tlsShown'] is False, r)
            S.skip('the https link on an insecure page', 'openssl could not make a certificate')

    S.section('v2: the notification permission flow, keyboard tabs')
    r = res['v2_perm']
    if got('v2_perm', r):
        check('perm: before, notifications are off with an Enable button', r['before'][1] == 'Enable notifications' and r['before'][2] is False
              and r['before'][3] is True, r['before'])
        check('perm: Enable asks the browser once, turns them on, sends a test', r['after'][3] == 1 and r['after'][5] is True
              and r['after'][2] is False and len(r['after'][4]) == 1, r['after'])
        check('perm: and Turn off', r['off'][2] is False, r['off'])
        check('perm: ArrowRight moves to the All viewers tab', r['arrow'] == ['true', False, 'tab-global'], r['arrow'])
        check('perm: the close button closes the dialog; no JS errors', r['closed'] is True and r['errors'] == [], r)

    assert_engine(res['v2_engine'], eng_rid, eng_tids)
    assert_detail(res['v2_detail'], engd_rid, engd_tids)
    assert_offline(res['v2_offline'])
    assert_remind(res['v2_remind'])
    assert_held(res['v2_held'])
    assert_panel(res['v2_panel'], boards['panel'])


RESULTS_V2: dict = {}


def assert_engine(r, rid, tids):
    S.section('v2: the alert engine on the list view (stubbed Notification and AudioContext)')
    if not got('v2_engine', r):
        return
    st = {x['label'].split(':')[0]: x for x in r['steps']}
    notes, chimes = r['notes'], r['chimes']
    tag = lambda key: f'taskstatus:{rid}:{key}'  # noqa: E731
    check('engine: no JS errors, no alerts', r['errors'] == [] and r['alerts'] == [], (r['errors'], r['alerts']))
    b = st['baseline']
    check('baseline: what was on the board already alerts nobody', b['notes'] == 0 and b['chimes'] == 0
          and b['row']['waiting'] is None and b['summary'] == '1 running', b)
    a = st['A']
    check('A: the asking task marks its request: needs input, the question and who asked, the pulse', a['status'] == 200
          and a['row']['waiting'] == 'yes' and a['row']['badge'] == 'needs input' and a['row']['ask'] == 'Which region should I deploy to?'
          and a['row']['askWho'].startswith('#1') and a['row']['animation'] == 'pulse', a['row'])
    check('A: "1 waiting for you" leads the summary; the favicon turns to the alert variant', a['summary'].startswith('1 waiting for you')
          and "r='15' fill='#c026d3'" in a['favicon'], (a['summary'], a['favicon'][:160]))
    n0 = notes[0] if notes else {}
    check('A: one notification: "Needs input: <request>", the task and question, tagged per request, sticky',
          n0.get('title') == 'Needs input: Engine test request' and n0.get('body') == '#1 step a: Which region should I deploy to?'
          and n0.get('tag') == tag('waiting') and n0.get('requireInteraction') is True, notes[:1])
    check('A: the attention chime', chimes[:1] and chimes[0][1] == 'attention', chimes[:2])
    bh = st['B']
    check('B: while hidden, a failure notifies (not sticky) and chimes failure', any(n['title'] == 'Task failed: #2 step b'
          and n['tag'] == tag('task_failed') and n['requireInteraction'] is False and n['body'] == 'Engine test request\nsegfault in the renderer'
          for n in notes) and [c[1] for c in chimes[:2]] == ['attention', 'failure'], (notes[:3], chimes[:3]))
    check('B: the tab title counts it, "(1) ...", and alternates with "❗ Needs input — <request>"',
          any(t.startswith('(1) ') for t in bh['titles']) and any(t == '(1) ❗ Needs input — Engine test request' for t in bh['titles'])
          and any(t.startswith('(1) (') and 'running' in t for t in bh['titles']), bh['titles'])
    check('B: shown again, the count clears', st['B2']['title'] == '(1 running) Task Status', st['B2']['title'])
    c = st['C']
    c_chimes = [k for t, k in chimes if st['B']['at'] < t <= c['at']]
    check('C: both alerts chime, the question\'s included (a chime asked for too soon waits; it is not dropped)',
          'attention' in c_chimes and set(c_chimes) <= {'failure', 'attention'} and c['row']['askMore'] == '+1 more', (c_chimes, c['row']))
    check('chimes never start less than 2s apart', all(g >= 1950 for g in chime_gaps(chimes)), chime_gaps(chimes))
    d = st['D']
    d_notes = [n for n in notes if st['C']['at'] < n['at'] <= d['at']]
    d_chimes = [k for t, k in chimes if st['C']['at'] < t <= d['at']]
    d_flash = [f for f in r['flashes'] if st['C']['at'] < f[0] <= d['at'] and f[2] == 'failed']
    check('D: a replayed failure only highlights: its row flashes failed, no notification, no chime', d['status'] == 200
          and d_notes == [] and d_chimes == [] and d_flash, (d_notes, d_chimes, r['flashes']))
    e = st['E']
    check('E: both answered: the row stops waiting, the summary drops it, the favicon is a progress ring again',
          e['row']['waiting'] is None and e['row']['ask'] is None and not e['summary'].startswith('1 waiting')
          and 'waiting for you' not in e['summary'] and "fill='#c026d3'" not in e['favicon'], e)
    check("E: the answered question's notification is closed", tag('waiting') in [t for _, t in r['closes']], r['closes'])
    f = st['F']
    check('F: the page refetches the settings after the board changes them', any(t > st['E']['at'] and m == 'GET' for t, m in r['settingsFetches']),
          r['settingsFetches'])
    f_notes = [n for n in notes if st['E']['at'] < n['at'] <= f['at']]
    f_chimes = [k for t, k in chimes if st['E']['at'] < t <= f['at']]
    check('F: task_done now notifies and chimes success (the board\'s new settings)', [n['title'] for n in f_notes] == ['Task done: #6 step f']
          and f_notes[0]['tag'] == tag('task_done') and f_chimes == ['success'], (f_notes, f_chimes))
    g = st['G']
    g_notes = [n for n in notes if f['at'] < n['at'] <= g['at']]
    check('G: a This-device cell cycles inherit -> on -> off, saved in localStorage', g['cellStates'] == ['inherit', 'on', 'off']
          and g['localAfter']['events'] == {'task_done': {'sound': False}}, (g['cellStates'], g['localAfter']))
    check('G: the device override wins: the next task_done notifies but plays no chime', [n['title'] for n in g_notes] == ['Task done: #5 step e']
          and g['newChimes'] == [], (g_notes, g['newChimes']))
    h = st['H']
    check('H: going quiet while hidden: the row turns stale and flashes, the title counts it', h['row']['status'] == 'stale'
          and any(fl[2] == 'stale' and fl[0] > g['at'] for fl in r['flashes']) and h['title'].startswith('(1) '), (h['row'], h['title']))
    check('H: stale notifies nobody by default (sound and notify off)', not [n for n in notes if g['at'] < n['at'] <= h['at']]
          and not [k for t, k in chimes if g['at'] < t <= h['at']])
    i = st['I']
    i_notes = [n for n in notes if h['at'] < n['at'] <= i['at']]
    check('I: the request finishes: "Done: <request>", tagged request_done, the success chime, the row done',
          [n['title'] for n in i_notes] == ['Done: Engine test request'] and i_notes[0]['tag'] == tag('request_done')
          and i_notes[0]['body'] == 'all good' and [k for t, k in chimes if h['at'] < t] == ['success'] and i['row']['status'] == 'done',
          (i_notes, chimes[-2:], i['row']))
    check('engine: the notifications, in all', len(notes) == 7, [n['title'] for n in notes])


def assert_detail(r, rid, tids):
    S.section('v2: the detail view: questions on the header and the task rows, flashes, a notification click')
    if not got('v2_detail', r):
        return
    sn = {x['label']: x for x in r['snaps']}
    check('detail: no JS errors', r['errors'] == [], r['errors'])
    b = sn['baseline']
    check('baseline: nothing waits', b['head']['waiting'] is None and b['task']['waiting'] is None and b['notes'] == 0, b)
    a = sn['a task asks']
    check('a task asks: its row waits and pulses with the question; the header says a task waits', a['task']['waiting'] == 'yes'
          and a['task']['badge'] == 'needs input' and a['task']['askQ'] == 'Keep 4K or drop to 1080p?' and a['task']['animation'] == 'pulse'
          and a['head']['waiting'] == 'task' and a['head']['askQ'] is None, a)
    q = sn['the request asks too']
    check("the request asks too: the header shows its own question and pulses", q['head']['waiting'] == 'request'
          and q['head']['askQ'] == 'Also upload it when done?' and q['head']['animation'] == 'pulse' and q['task']['waiting'] == 'yes', q)
    pa = sn['progress answers the task']
    check('progress on the task clears its question; the request still waits on its own', pa['task']['waiting'] is None
          and pa['task']['askQ'] is None and pa['head']['waiting'] == 'request', pa)
    tf = sn['a task fails']
    check('a task failure flashes that task row', any(f[1] == tids[1] and f[2] == 'failed' for f in r['flashes']), r['flashes'])
    check("one waiting notification per request (the request's question replaced the task's), kept while the request waits",
          [n['tag'] for n in r['notes'][:2]] == [f'taskstatus:{rid}:waiting'] * 2 and tf['head']['waiting'] == 'request'
          and f'taskstatus:{rid}:waiting' not in [t for _, t in r['closes']], (r['notes'], r['closes']))
    clicked = sn['its notification clicked']
    check('clicking a notification opens its task (#tid in this view)', r['clicked'] and clicked['hash'] == '#' + tids[1], (r['clicked'], clicked['hash']))
    titles = [n['title'] for n in r['notes']]
    check('detail: the notifications: both questions, then the failure', titles == ['Needs input: Render the launch video',
                                                                                   'Needs input: Render the launch video',
                                                                                   'Task failed: #2 step b'], titles)


def assert_offline(r):
    S.section('v2: the board out of reach for 10s while agents write; then back')
    if not got('v2_offline', r):
        return
    sn = {x['label']: x for x in r['snaps']}
    d, back = sn['during the outage'], sn['back']
    check('offline: no JS errors', r['errors'] == [], r['errors'])
    check('during: the conn state is offline, the title starts "offline · ", the favicon is the offline variant', d['conn'] == 'offline'
          and d['title'].startswith('offline · ') and "stroke-dasharray='7 4.6'" in d['favicon'], d)
    check('during: nothing is announced', d['notes'] == 0 and d['chimes'] == 0, d)
    titles = [n['title'] for n in r['notes']]
    check('back: one summary notification for what happened, plus the question asked meanwhile', len(r['notes']) == 2
          and 'Needs input: Offline test request' in titles
          and any(t.startswith('While the board was out of reach: ') for t in titles), r['notes'])
    summary = next((n for n in r['notes'] if n['title'].startswith('While')), {})
    check('back: the summary counts each kind that notifies (task_done does not by default), and names the request',
          summary.get('body') == 'Task failed ×2' and summary.get('title') == 'While the board was out of reach: 2 updates on Offline test request'
          and summary.get('tag') == 'taskstatus:summary', summary)
    check('back: one chime, the most urgent (attention)', [k for _, k in r['chimes']] == ['attention'], r['chimes'])
    check('back: live again, the title and favicon too', back['conn'] == 'live' and not back['title'].startswith('offline')
          and "stroke-dasharray='7 4.6'" not in back['favicon'], back)


def assert_remind(r):
    S.section('v2: the needs-input reminder (every 3s here), on an insecure page')
    if not got('v2_remind', r):
        return
    chimes = [t for t, k in r['chimes'] if k == 'attention']
    waiting = [t for t in chimes if r['asked'] < t < r['offlineAt']]
    check('remind: no JS errors, no notifications (insecure page), sounds still play', r['errors'] == [] and r['notes'] == 0 and chimes, r)
    check('remind: the attention chime, then a reminder about every 3s while it waits', len(waiting) >= 3
          and all(2500 <= g <= 6500 for g in chime_gaps([[t] for t in waiting])), waiting)
    offline = [t for t, s in r['conn'] if s == 'offline']
    live_again = [t for t, s in r['conn'] if s == 'live' and t > r['offlineAt']]
    if offline and live_again:
        quiet = [t for t in chimes if offline[0] + 200 < t < live_again[0]]
        check('remind: paused while the board is out of reach', quiet == [], (offline, live_again, chimes))
    else:
        check('remind: the page saw the board go offline and come back', False, r['conn'])
    after = [t for t in chimes if t > r['resumed'] + 2500]
    check('remind: stops once the question is answered', after == [], (r['resumed'], chimes))


def assert_held(r):
    S.section('v2: audio held by the browser (no click yet) while a question waits and a task fails')
    if not got('v2_held', r):
        return
    check('held: no JS errors; the bell shows the audio held', r['errors'] == [] and r['bellBefore'] == 'yes', r)
    check('held: nothing is scheduled while it is held (no tones, no nodes), the reminders included', r['whileHeld'] == {'tones': 0, 'nodes': 0},
          r['whileHeld'])
    check('held: the first click plays one chime, the most urgent (attention over failure), not a stacked burst',
          [k for _, k in r['onClick']] == ['attention'] and r['onClick'][0][0] < 700, r['onClick'])
    after = r['all']
    check('held: then the reminders go on, attention only, never less than 2s apart', len(after) >= 2 and all(k == 'attention' for _, k in after)
          and all(g >= 1950 for g in chime_gaps(after)), after)
    check('held: the bell is not held any more', r['bellAfter'] is None, r['bellAfter'])


def assert_panel(r, srv):
    S.section('v2: the settings panel: both tabs, save, the board changing it, offline, reset')
    if not got('v2_panel', r):
        return
    st = {x['label']: x for x in r['steps']}
    check('panel: no JS errors', r['errors'] == [], r['errors'])
    check('the bell turns device sound on (saved on this device) and plays a chime', r['bell'][0] == 'false' and r['bell'][1] == 'true'
          and r['bell'][2] is True and r['bell'][3] == ['info'], r['bell'])
    check('needs-input highlight off on this device: body.calm, no pulse; back to inherit: the pulse again', r['calm']['cls'] is True
          and r['calm']['anim'] == 'none' and r['calm']['state'] == 'off' and r['calm']['after'] == [False, 'pulse'], r['calm'])
    g = st['global loaded']
    check('All viewers: the settings file path, not saved yet, Save disabled', g['path'] == str(srv.config) and g['save'] is False
          and 'not' in g['exists'].lower(), g)
    check('a changed checkbox enables Save and Discard', st['dirty']['save'] is True and st['dirty']['discard'] is True, st['dirty'])
    sv = st['saved']
    check('Save: PUT to the board, the file exists', sv['server'] == {'highlight': True, 'title': False, 'sound': True, 'notify': False}
          and sv['exists'] is True and sv['save'] is False, sv)
    ch = st['changed on the board (clean form)']
    check('changed on the board while the form is clean: the new values shown, a notice', ch['staleSound'] is True and ch['notice'], ch)
    dirty = st['changed on the board (dirty form)']
    check('changed on the board while editing: a notice says so, the edits are kept', dirty['notice'] and 'while' in dirty['notice']
          and dirty['save'] is True, dirty)
    off = st['save while offline']
    check("Save while the board is out of reach: not saved, the edits kept", "can't reach the board" in off['status']
          and off['taskStartedTitle'] is True, off)
    again = st['saved after reconnect']
    check('Save after it is back: the volume and the kept edit reach the board; what the board changed meanwhile is overwritten '
          'with what the form showed', again['server']['volume'] == 0.3 and again['server']['task_started']['title'] is True
          and again['server']['request_created']['title'] is False, again)
    rs = st['reset']
    check('Reset asks first, then DELETE: the file is gone', rs['exists'] is False
          and any(a.startswith('confirm:') for a in rs['alerts']), rs)
    br = st['broken file on the board']
    noticed = (br['notice'] == 'The settings file changed on the board; showing the new values.'
               or (br['notice'] or '').startswith('The settings file is not valid ('))
    check('a hand edit that breaks the file: noticed, the defaults shown, and Save offered with nothing changed',
          noticed and br['volume'] == '60' and br['save'] is True and br['discard'] is False, br)
    fixed = st['saved over the broken file']
    check('Save over it: the file is valid again (the defaults written out)', fixed['serverError'] is None and fixed['exists'] is True
          and fixed['notice'] is None, fixed)


# ---------------------------------------------------------------- the live stream

STREAM_JSON = {'Content-Type': 'application/json'}
STREAM_NAMES = ('layout', 'audio', 'held', 'swap', 'retry', 'multi', 'panel', 'hevc', 'player', 'fault', 'life', 'nav', 'offline', 'odd', 'check')
RESULTS_STREAM: dict = {}


def secs(timer) -> int | None:
    """The seconds a pane's timer shows ("12s"; the cases stay under a minute)."""
    match = re.fullmatch(r'(\d+)s', timer or '')
    return int(match.group(1)) if match else None


def at(p, x, y, w, h, tol=1.0) -> bool:
    """Whether the pane's box is (x, y, w, h), within tol pixels."""
    r = p['rect']
    return all(abs(r[k] - v) <= tol for k, v in (('x', x), ('y', y), ('w', w), ('h', h)))


def stream_cases():
    """The live stream's cases: a board each (a board has one stream), whose ffmpeg is tests/lib/fake_ffmpeg.py."""
    fakebin = fake_ffmpeg_dir(TMP)
    boards, play = {}, None
    try:
        for name in STREAM_NAMES:
            env = clean_env(UI_RESULTS_DIR=RESULTS, PATH=f'{fakebin}{os.pathsep}{os.environ["PATH"]}', FAKE_FFMPEG_LOG=TMP / f'fake-{name}.log')
            boards[name] = ScratchServer(TMP, '--host', '127.0.0.1', '--stale-after', '600', script=UI / 'harness.py', name=f'ui-s{name}', env=env).start()
        play = streamplay_setup()
        if os.environ.get('UI_STREAM_URL'):     # a real source, only when asked for: a board of its own, the real ffmpeg
            real = real_ffmpeg_dir(TMP)
            if real:
                play = dict(play or {}, real=ScratchServer(TMP, '--host', '127.0.0.1', script=UI / 'harness.py', name='ui-sreal',
                            env=clean_env(UI_RESULTS_DIR=RESULTS, PATH=f'{real}{os.pathsep}{os.environ["PATH"]}')).start())
            else:
                S.skip('streamreal', 'UI_STREAM_URL needs ffmpeg')
        _stream_cases(boards, play)
    finally:
        for srv in boards.values():
            srv.stop()
        for name, srv in boards.items():
            check(f'the stream {name} board logged no tracebacks', 'Traceback' not in srv.log_text(), srv.log_text()[-1500:])
        check('no ffmpeg (the fake) is left running by a stream board', not find_procs(str(fakebin)), find_procs(str(fakebin)))
        for key, board in (('board', play and play.get('board')), ('real', play and play.get('real'))):
            if board:
                board.stop()
                check(f'the real-ffmpeg board ({key}) logged no tracebacks', 'Traceback' not in board.log_text(), board.log_text()[-1500:])
        if play:
            if play.get('upstream'):
                play['upstream'].close()
            left = find_procs(str(real_ffmpeg_dir(TMP)))
            check('no ffmpeg is left running by a real-ffmpeg board', not left, left)


def streamplay_setup():
    """What the real-playback case needs: a source made from ffmpeg's test patterns (an MPEG-TS served like the real
    one) and a board of its own whose ffmpeg is the real one; None (the case is skipped) without ffmpeg and libx264."""
    data = make_ts('av', 40)
    if not data:
        return None
    real = real_ffmpeg_dir(TMP)
    env = clean_env(UI_RESULTS_DIR=RESULTS, PATH=f'{real}{os.pathsep}{os.environ["PATH"]}')
    board = ScratchServer(TMP, '--host', '127.0.0.1', script=UI / 'harness.py', name='ui-splay', env=env).start()
    return {'board': board, 'upstream': FakeUpstream(data)}


def _stream_cases(boards, play):
    keep = '&__shot=1' if os.environ.get('UI_SHOTS_DIR') else ''
    check_board = boards['check'].url
    ids = seed_v2(check_board)
    api(check_board, 'POST', '/api/stream', {'url': 'http://fake.invalid/hang'}, headers=STREAM_JSON)
    n_requests = len(api(check_board, 'GET', '/api/requests')[1]['requests'])
    wait_rows = len(api(check_board, 'GET', f"/api/requests/{ids['task_wait']}")[1]['tasks'])

    def frame_case(name, wait):
        return (f'stream{name}', boards[name].url, '/__frame', 1280, 'light', f'stream{name}', wait, keep, (), 900)

    cases = [frame_case('fault', 75000), frame_case('retry', 40000), frame_case('player', 40000), frame_case('offline', 40000),
             frame_case('layout', 36000), frame_case('nav', 30000), frame_case('life', 24000), frame_case('held', 30000),
             frame_case('swap', 22000), frame_case('hevc', 20000), frame_case('odd', 32000), frame_case('audio', 14000),
             frame_case('multi', 14000), frame_case('panel', 14000),
             ('stream_check_390', check_board, '/', 390, 'light', 'check', 7000, '', (), 900),
             ('stream_check_1280_dark', check_board, '/', 1280, 'dark', 'check', 7000, '', (), 900),
             ('stream_check_844x390', check_board, '/', 844, 'light', 'check', 7000, '', (), 390),
             ('stream_check_detail_390', check_board, f"/r/{ids['task_wait']}", 390, 'light', 'check', 7000, '', (), 900)]
    if play and play.get('board'):    # real ffmpeg, the real MediaSource: the same pool, a board of its own
        cases.insert(4, ('streamplay', play['board'].url, '/__frame', 1280, 'light', 'streamplay', 16000, f'&__src={quote(play["upstream"].url, safe="")}{keep}', (), 900))
    if play and play.get('real'):     # UI_STREAM_URL: the same script, a real source
        cases.insert(5, ('streamreal', play['real'].url, '/__frame', 1280, 'light', 'streamplay', 40000,
                         f'&__src={quote(os.environ["UI_STREAM_URL"], safe="")}{keep}', (), 900))
    res = in_parallel(cases)
    RESULTS_STREAM.update(res)

    assert_streamlayout(res['streamlayout'])
    assert_streamaudio(res['streamaudio'])
    assert_streamheld(res['streamheld'])
    assert_streamswap(res['streamswap'])
    assert_streamretry(res['streamretry'])
    assert_streammulti(res['streammulti'])
    assert_streampanel(res['streampanel'])
    assert_streamhevc(res['streamhevc'])
    assert_streamplayer(res['streamplayer'])
    assert_streamfault(res['streamfault'])
    assert_streamlife(res['streamlife'])
    assert_streamnav(res['streamnav'])
    assert_streamoffline(res['streamoffline'])
    assert_streamodd(res['streamodd'])
    S.section('stream: the list and detail checks, repeated with a stream open')
    for name, theme, width, height, rows in (('stream_check_390', 'light', 390, 900, n_requests), ('stream_check_1280_dark', 'dark', 1280, 900, n_requests),
                                             ('stream_check_844x390', 'light', 844, 390, n_requests), ('stream_check_detail_390', 'light', 390, 900, wait_rows)):
        r = res[name]
        assert_check(name, r, theme, rows=rows)
        if r:
            pane = r.get('pane') or {}
            vh = r.get('innerHeight') or 0
            if width > height:     # landscape: the pane on the right, half the width
                ok = r['streaming'] and at({'rect': pane}, width / 2, 0, width / 2, vh) if pane else False
            else:                  # portrait: the pane at the bottom, half the height
                ok = r['streaming'] and at({'rect': pane}, 0, vh / 2, width, vh / 2) if pane else False
            check(f'{name}: streaming, the pane is half the window ({"right" if width > height else "bottom"}); the board scrolls on its own, not sideways',
                  ok and r['boardScroll'][0] <= r['boardScroll'][1], (r.get('streaming'), pane, vh, r.get('boardScroll')))
            check(f'{name}: the phone layout follows the board\'s own width ({"yes" if width < 1000 else "no"})',
                  r['setupWideHidden'] is (width < 1000), (width, r['setupWideHidden']))
            check(f'{name}: the pane is black with light text in the {theme} theme', r['paneColors'] == ['rgb(0, 0, 0)', 'rgb(230, 237, 243)'], r.get('paneColors'))
    S.section('stream: real playback (real ffmpeg, no stub)')
    if play and play.get('board'):
        assert_streamplay(res['streamplay'], play['upstream'])
    else:
        S.skip('streamplay', 'ffmpeg with libx264 is needed to make a source')
    if play and play.get('real'):
        assert_streamplay(res['streamreal'], None, name='streamreal')


# ---- assertions

def assert_streamlayout(r):
    S.section('stream: where the pane goes, how big it is, and what it says')
    if not got('streamlayout', r):
        return
    n, p, land, rel = r['none'], r['hangPortrait'], r['hangLandscape'], r['reloaded']
    check('layout: with no stream there is no pane and no .streaming class, and the board has its old layout', n['hidden'] is True and n['streaming'] is False
          and r['noneDoc'] == {'cls': '|', 'scrollW': 390, 'clientW': 390}, (n['hidden'], r['noneDoc']))
    check('layout: a stream that does not connect: a black pane, half the window, below the board in portrait (390x844: 390x422)',
          p['streaming'] and p['state'] == 'connecting' and at(p, 0, 422, 390, 422) and p['bg'] == 'rgb(0, 0, 0)' and p['board']['h'] == 422, p)
    check('layout: the pane says exactly "connecting to <url>...", the timer on a line of its own, and nothing else',
          p['line'] == 'connecting to http://fake.invalid/hang...' and secs(p['timer']) is not None and p['title'] is None and not p['chip'], (p['line'], p['timer'], p['title']))
    check('layout: the timer counts (a status and a timer role, and a label on the pane)', None not in map(secs, r['hangTimer']) and secs(r['hangTimer'][1]) - secs(r['hangTimer'][0]) >= 1
          and r['hangRoles'] == ['status', 'timer', 'Live stream'], (r['hangTimer'], r['hangRoles']))
    check('layout: landscape (844x390): the pane is on the right, half the width, the board beside it (a phone layout in a 422px board)',
          at(land, 422, 0, 422, 390) and land['bg'] == 'rgb(0, 0, 0)' and land['board']['w'] == 422 and land['setupWide'] == 'none'
          and land['docScroll'] == 0 and land['boardScroll'] == 0, land)
    check('layout: a reloaded board goes on from the server\'s clock: its timer has not started over',
          rel['streaming'] and secs(rel['timer']) is not None and secs(rel['timer']) >= secs(land['timer']) and secs(rel['timer']) >= 4, (land['timer'], rel['timer']))
    vl, vp, vw, vt = r['videoLandscape'], r['videoPortrait'], r['videoWide'], r['videoTall']
    check('layout: the video, 16:9, landscape 844x390: half the width (the picture is letterboxed inside: object-fit contain)',
          vl['state'] == 'video' and at(vl, 422, 0, 422, 390) and vl['fit'] == 'contain' and vl['line'] == '' and vl['videos'] == 1 and vl['video']['opacity'] == '1', vl)
    check('layout: the video, 16:9, portrait 390x844: the whole width, the height the ratio gives (219.4), at the bottom',
          at(vp, 0, 844 - 390 * 9 / 16, 390, 390 * 9 / 16) and abs(vp['board']['h'] + vp['rect']['h'] - 844) < 0.01 and vp['ar'] == '1.7778', vp)
    check('layout: the video, 16:9, landscape 1280x800: half the width (640)', at(vw, 640, 0, 640, 800), vw)
    check('layout: the video, 16:9, portrait 800x1280: the ratio gives 450 of the 640 allowed', at(vt, 0, 1280 - 450, 800, 450), vt)
    tp, tw = r['sourceTallPortrait'], r['sourceTallWide']
    check('layout: the video, 9:16, portrait 390x844: capped at half the window (422), the width of the window', tp['state'] == 'video' and at(tp, 0, 422, 390, 422) and tp['ar'] == '0.5625', tp)
    check('layout: the video, 9:16, landscape 1280x800: as wide as the ratio asks (450 of the 640 allowed)', at(tw, 830, 0, 450, 800), tw)
    mk = r['markup']
    check('layout: markup in the URL is shown as text: the pane has the URL\'s characters, no <img>, no alert', mk['line'] == f"connecting to {mk['want']}..."
          and mk['imgs'] == 0 and mk['alerts'] == [], (mk['line'], mk['imgs'], mk['alerts']))
    sc = r['secret']
    check('layout: a password in the URL is shown as ***@, in the pane and its tooltip', sc['line'] == 'connecting to http://***@fake.invalid/hang...'
          and 'pw' not in sc['text'].replace('\n', ' ').split('http://***@')[-1].replace('fake.invalid/hang', ''), sc['text'])
    c = r['closed']
    check('layout: closed, the pane is gone and the board has the window again; no media request follows', r['deleteStatus'] == 200 and c['streaming'] is False and c['hidden'] is True
          and c['doc'] == {'cls': '|', 'scrollW': 390, 'clientW': 390} and c['mediaRequests'] == 0, (r['deleteStatus'], c['doc'], c['mediaRequests']))
    check('layout: no JS errors', r['errors'] == [[], []], r['errors'])


def assert_streamaudio(r):
    S.section('stream: audio only')
    if not got('streamaudio', r):
        return
    p, l, ph, w = r['portrait'], r['landscape'], r['phone'], r['wide']
    check('audio: an audio-only stream is "audio connected" (exactly), a black pane, no timer; the player asked for the audio MIME',
          all(x['state'] == 'audio' and x['line'] == 'audio connected' and x['bg'] == 'rgb(0, 0, 0)' for x in (p, l, ph, w)) and r['timerShown'] == 'none'
          and r['type'] == 'audio/mp4; codecs="mp4a.40.2"' and r['plays'] == [[False, 'ok']], (p['line'], r['timerShown'], r['type'], r['plays']))
    check('audio: 15% of the window: the height in portrait 390x844 (126.6) at the bottom', at(p, 0, 844 - 126.6, 390, 126.6), p)
    check('audio: 15% of the window: the width in landscape 844x390 (126.6) on the right', at(l, 844 - 126.6, 0, 126.6, 390), l)
    check('audio: 15% of the window: 100 wide at 667x375, 192 at 1280x800', at(ph, 667 - 100.05, 0, 100.05, 375) and at(w, 1280 - 192, 0, 192, 800), (ph['rect'], w['rect']))
    held = [r['heldPortrait'], r['heldLandscape'], r['heldPhone'], r['heldWide']]
    check('audio: held by the browser: the chip says "click for sound" in every size', all(x['held'] == 'sound' and x['chip']['label'] == 'click for sound' for x in held), held)

    def inside(x):
        c, k = x['chip']['rect'], x['rect']
        return c['x'] >= k['x'] - 0.5 and c['y'] >= k['y'] - 0.5 and c['x'] + c['w'] <= k['x'] + k['w'] + 0.5 and c['y'] + c['h'] <= k['y'] + k['h'] + 0.5
    check('audio: the chip fits the pane in every size, as text where there is room (portrait, 1280 wide) and as its icon in the narrow ones',
          all(inside(x) for x in held) and [x['chip']['textShown'] for x in held] == [True, False, False, True], [(x['chip']['rect'], x['rect'], x['chip']['textShown']) for x in held])
    check('audio: no JS errors', r['errors'] == [[]], r['errors'])


def assert_streamheld(r):
    S.section('stream: sound the browser holds back')
    if not got('streamheld', r):
        return
    m, c = r['muted'], r['clicked']
    check('held: where only muted playback may start, the video plays muted and the chip says "click for sound"; the unmuted try came first',
          m['pane']['state'] == 'video' and m['pane']['held'] == 'sound' and m['pane']['chip']['text'] == 'click for sound' and m['pane']['video']['muted'] is True
          and m['pane']['video']['paused'] is False and m['plays'] == [[False, 'NotAllowedError'], [True, 'ok']], m)
    check('held: Escape does not let the sound out (it is no activation)', r['escape']['pane']['held'] == 'sound' and r['escape']['unmutes'] == 0, r['escape'])
    check('held: nor does a click or a key with no activation (it would pause the video)', r['noActivation']['pane']['held'] == 'sound' and r['noActivation']['unmutes'] == 0
          and r['noActivation']['pane']['video']['paused'] is False, r['noActivation'])
    check('held: a click with activation lets it out: unmuted, playing, no chip, one unmute, and a play (unmuted, allowed) asked for with it', c['pane']['held'] is None
          and c['pane']['chip'] is None and c['pane']['video']['muted'] is False and c['pane']['video']['paused'] is False and c['unmutes'] == [[True, False]]
          and c['plays'] == [[False, 'NotAllowedError'], [True, 'ok'], [False, 'ok']], c)
    check('held: so does a key (not Escape)', r['keyed']['pane']['held'] is None and r['keyed']['pane']['video']['muted'] is False and r['keyed']['unmutes'] == 1, r['keyed'])
    b, bc = r['blocked'], r['blockedClicked']
    check('held: where nothing may start, the video waits and the chip says "click to play" (unmuted try, muted try, both refused)',
          b['pane']['held'] == 'play' and b['pane']['chip']['text'] == 'click to play' and b['pane']['video']['paused'] is True
          and b['plays'] == [[False, 'NotAllowedError'], [True, 'NotAllowedError']], b)
    check('held: the click starts it, with sound, and the chip goes', bc['pane']['held'] is None and bc['pane']['chip'] is None and bc['pane']['video']['paused'] is False
          and bc['pane']['video']['muted'] is False and bc['plays'][-1] == [False, 'ok'], bc)
    so, soc = r['soundOff'], r['soundOffClicked']
    check('held: with the stream\'s sound off on this device: muted from the start, no unmuted try, no chip; a click changes nothing',
          so['pane']['held'] is None and so['pane']['chip'] is None and so['pane']['video']['muted'] is True and so['plays'] == [[True, 'ok']]
          and soc['unmutes'] == 0 and soc['pane']['video']['muted'] is True, (so, soc))
    ob, obc = r['offBlocked'], r['offBlockedClicked']
    check('held: sound off and nothing may start: "click to play", and the click plays it muted', ob['pane']['held'] == 'play' and ob['pane']['chip']['text'] == 'click to play'
          and obc['pane']['held'] is None and obc['pane']['video']['paused'] is False and obc['pane']['video']['muted'] is True and obc['unmutes'] == 0, (ob, obc))
    vo, vb, vc = r['videoOnly'], r['videoOnlyBlocked'], r['videoOnlyClicked']
    check('held: a stream with no audio track (a camera\'s) is played muted at once: no unmuted try, no "click for sound" chip, nothing held', vo['pane']['state'] == 'video'
          and vo['pane']['held'] is None and vo['pane']['chip'] is None and vo['pane']['video']['muted'] is True and vo['plays'] == [[True, 'ok']], vo)
    check('held: nor is there any sound to click for where nothing may start: "click to play", and the click plays it muted', vb['pane']['held'] == 'play'
          and vb['pane']['chip']['text'] == 'click to play' and vb['plays'] == [[True, 'NotAllowedError']] and vc['pane']['held'] is None and vc['pane']['video']['paused'] is False
          and vc['pane']['video']['muted'] is True and vc['unmutes'] == 0, (vb, vc))
    rf, rl = r['refused'], r['refusedLater']
    check('held: a browser that pauses the video after an unmute it refused: muted again, playing again, held again, the chip back',
          rf['pane']['held'] == 'sound' and rf['pane']['video']['muted'] is True and rf['pane']['video']['paused'] is False and rf['pane']['chip']['text'] == 'click for sound'
          and rf['unmutes'] == 1, rf)
    check('held: and it does not go on trying by itself', rl['unmutes'] == 1 and rl['plays'] == rf['plays'].__len__() and rl['held'] == 'sound', (rf['plays'], rl))
    check('held: no JS errors (no unhandled play() rejection)', r['errors'] == [[]], r['errors'])


def assert_streamswap(r):
    S.section('stream: a newer URL replaces the stream')
    if not got('streamswap', r):
        return
    a, b, c = r['a'], r['b'], r['c']
    ids = r['ids']
    check('swap: stream A plays in both boards, from fragments marked A; the board counts two viewers', a['f']['state'] == 'video' and a['g']['state'] == 'video'
          and a['markers'] == ['A', 'A'] and a['viewers'] == 2, a['markers'])
    check('swap: told of B (which never connects): both panes are "connecting to B" again, the timer started over, the video element gone, no viewer left',
          all(x['state'] == 'connecting' and x['line'] == 'connecting to http://fake.invalid/hang...' and x['videos'] == 0 and secs(x['timer']) <= 4 for x in (b['f'], b['g']))
          and b['viewers'] == 0, b)
    check('swap: then C: both boards play it, one video element each, from C only after the swap (the fragments before were A\'s), two viewers',
          c['f']['state'] == 'video' and c['g']['state'] == 'video' and c['f']['videos'] == c['g']['videos'] == 1 and c['markers'] == ['AC', 'AC']
          and c['last'] == ['CCC', 'CCC'] and c['viewers'] == 2 and c['srcs'] == [2, 2] and c['loads'] == [1, 1], c)
    log = r['log']
    check('swap: the media requests are for A (one per board) and C (one per board): none for B, none for A after it was replaced',
          log.count(ids['a']) == 2 and log.count(ids['c']) == 2 and ids['b'] not in log and len(log) == 4, (log, ids))
    check('swap: no JS errors', r['errors'] == [[], []], r['errors'])


def assert_streamretry(r):
    S.section('stream: a source that ends every few seconds')
    if not got('streamretry', r):
        return
    s = r['samples']
    states = [x['state'] for x in s]
    cycles = [i for i in range(1, len(states)) if states[i] == 'video' and states[i - 1] == 'connecting']
    check('retry: the board plays, goes back to "connecting" when the media ends, and plays again: at least two cycles', len(cycles) >= 3 and 'video' in states[:3], states)
    flips = [x for i, x in enumerate(s) if x['state'] == 'connecting' and i and s[i - 1]['state'] == 'video']
    check('retry: where the media ended, the timer counts from the server\'s `since` (the end of the run) and the tooltip is the server\'s reason (the page\'s own, "the stream '
          'ended", only until the board answers: the page asks at once)', flips and all(secs(x['timer']) <= 2 for x in flips)
          and all(x['title'] in ('the source ended', 'the stream ended') for x in flips) and any(x['title'] == 'the source ended' for x in flips), flips)
    fetches = [t for t, status in r['fetches']]
    gaps = [b - a for a, b in zip(fetches, fetches[1:])]
    check('retry: the page asks for the media again only when the board says live: every request 200, never two within a second', len(fetches) >= 3 and all(st == 200 for _, st in r['fetches'])
          and all(g >= 1000 for g in gaps) and r['srcs'] == len(fetches), (r['fetches'], gaps))
    check('retry: no JS errors', r['errors'] == [[]], r['errors'])


def assert_streammulti(r):
    S.section('stream: two boards, one stream')
    if not got('streammulti', r):
        return
    f, g = r['f'], r['g']
    check('multi: a phone upright and one on its side: the same stream, each laid out for its own window (16:9: 390 wide, 219 high at the bottom; 422 wide on the right)',
          f['state'] == g['state'] == 'video' and at(f, 0, 844 - 390 * 9 / 16, 390, 390 * 9 / 16) and at(g, 422, 0, 422, 390), (f['rect'], g['rect']))
    check('multi: both are viewers of the one stream (one media request each); one going away leaves one', r['both']['id'] == r['id'] and r['both']['viewers'] == 2
          and r['srcs'] == [1, 1] and r['one']['viewers'] == 1, (r['both'].get('viewers'), r['srcs'], r['one'].get('viewers')))
    check('multi: no JS errors', r['errors'] == [[]], r['errors'])


def assert_streampanel(r):
    S.section('stream: the settings panel\'s Live stream block (This device)')
    if not got('streampanel', r):
        return
    st, o = r['start'], r['open']
    check('panel: two boards play muted (held), nothing saved yet', st['a']['held'] == st['b']['held'] == 'sound' and st['stored'] == {}, st)
    check('panel: the dialog is titled Settings (the gear too), has a Live stream block after Sound with the switch on, the volume at 100%, and the "muted by the browser" note',
          o['dialog'] and o['title'] == 'Settings' and o['gear'] == 'Settings' and o['heading'][:2] == ['Sound', 'Live stream'] and o['checked'] is True and o['volume'] == '100'
          and o['volumeText'] == '100%' and o['heldNote'] is True, o)
    off = r['off']
    check('panel: the switch off: saved as stream_audio false; both boards muted with nothing held; the slider and the note go', off['stored']['stream_audio'] is False and off['checkbox'] is False
          and off['a']['held'] is None and off['b']['held'] is None and off['a']['video']['muted'] and off['b']['video']['muted'] and off['heldNote'] is False and off['sliderDisabled'] is True, off)
    on = r['on']
    check('panel: on again, with a click: this board\'s sound is out (one unmute, with a gesture); the other board, which got no gesture, holds it back with its chip',
          on['stored']['stream_audio'] is True and on['a']['held'] is None and on['a']['video']['muted'] is False and on['unmutes'] == [[True], 0]
          and on['b']['held'] == 'sound' and on['b']['video']['muted'] is True and on['b']['chip']['label'] == 'click for sound', on)
    vol = r['volume']
    check('panel: the volume slider: saved as stream_volume 0.4, shown as 40%, the volume of this board\'s video and of the other one\'s (by the storage event)',
          vol['stored']['stream_volume'] == 0.4 and vol['text'] == '40%' and vol['a']['video']['volume'] == 0.4 and vol['b']['video']['volume'] == 0.4, vol)
    junk = r['junk']
    check('panel: a hand-edited, invalid saved value is ignored (the defaults: sound on, volume 1)', junk['b']['video']['volume'] == 1 and junk['b']['held'] == 'sound', junk)
    check('panel: no JS errors', r['errors'] == [[], []], r['errors'])


def assert_streamhevc(r):
    S.section('stream: a codec the browser cannot play')
    if not got('streamhevc', r):
        return
    e, late = r['early'], r['late']
    check('hevc: the board has it live as hev1; the page makes no video, the pane stays "connecting" at half the window with the reason as its tooltip only',
          r['server'] == {'state': 'live', 'video': 'hev1', 'audio': None} and e['state'] == 'connecting' and at(e, 0, 422, 390, 422) and e['videos'] == 0
          and e['title'] == 'this browser cannot play this stream (hev1)' and e['line'] == 'connecting to http://fake.invalid/hevc?audio=0...' and r['srcs'] == 0, (r['server'], e))
    check('hevc: the timer goes on, and the page asked once for the media and never again (no retry loop)', secs(late['timer']) >= secs(e['timer']) + 6
          and r['requests'] == [1, 1] and r['supports'] == ['video/mp4; codecs="hev1"'], (e['timer'], late['timer'], r['requests'], r['supports']))
    check('hevc: no JS errors', r['errors'] == [[]], r['errors'])


def assert_streamplayer(r):
    S.section('stream: the player\'s seeks, jump and trim (stub MediaSource)')
    if not got('streamplayer', r):
        return
    first = r['first']
    s0 = first['seeks'][0] if first['seeks'] else None
    inside = bool(s0 and s0['buffered'] and s0['buffered'][0][0] <= s0['to'] <= s0['buffered'][0][1])
    check('player: a board that joins a running stream starts inside what the first append buffered, not at 0 (the GOP does not start there)',
          len(first['seeks']) == 1 and inside and s0['to'] > 0 and s0['buffered'][0][0] > 0 and not s0['paused'], first['seeks'])
    check('player: steady playback needs no further seek', len(first['seeks']) == 1 and first['snap']['paused'] is False and first['snap']['ready'] >= 3, first['snap'])
    j = r['jump']['seeks']
    lag = (j[1]['buffered'][0][1] - j[1]['from']) if len(j) > 1 else None
    check('player: a playhead that falls behind (held) is brought to the live edge, 0.4s behind the newest frame, by one jump, when it was more than 1.5s behind',
          len(j) == 2 and lag is not None and 1.5 < lag < 2.0 and abs(j[1]['to'] - (j[1]['buffered'][0][1] - 0.4)) < 0.05, j)
    ch = r['chase']['seeks']
    gaps = [b - a for a, b in zip(ch[1:], ch[2:])]
    check('player: while it stays behind it jumps again only after two seconds, never faster', len(ch) >= 3 and all(g >= 1900 for g in gaps) and ch[1] - ch[0] > 0, gaps)
    check('player: playhead caught up: no more jumps', r['steady']['after'] == r['steady']['before'], r['steady'])
    p, pc = r['paused'], r['pausedClicked']
    check('player: a video the browser would not start (paused) is never chased: one seek (the first) while the buffer grew by seconds', p['seeks'] == 1 and p['held'] == 'play'
          and p['snap']['paused'] is True and p['snap']['buffered'][-1][1] - p['snap']['ct'] > 3, p)
    pj = pc['seeks'][1] if len(pc['seeks']) > 1 else None
    check('player: its click starts it, and it jumps to the live edge at once (one more seek)', len(pc['seeks']) == 2 and pc['held'] is None and pc['snap']['paused'] is False
          and abs(pj['to'] - (pj['buffered'][0][1] - 0.4)) < 0.05, pc)
    h, sh = r['hidden'], r['shown']
    check('player: a hidden tab is not chased (no seek in 3.5s with the playhead held), and when it is shown it jumps once', h['to'] == h['from']
          and len(sh['seeks']) == h['from'] + 1 and sh['seeks'][-1]['hidden'] is False, (h, [x['hidden'] for x in sh['seeks']]))

    def removals(x):
        return x['removes']
    t = r['trim']
    first_rm = removals(t)[0] if removals(t) else None
    check('player: far behind the playhead (more than the trim threshold) it removes buffered media up to the playhead minus the margin, and the playing GOP is intact',
          first_rm and abs(first_rm['end'] - (first_rm['ct'] - 3)) < 0.1 and first_rm['after'] and first_rm['after'][0][0] <= first_rm['ct'] <= first_rm['after'][0][1]
          and first_rm['after'][0][0] >= first_rm['end'], removals(t)[:2])
    check('player: and goes on playing through it (the playhead is in the buffer), once per 4s of playback rather than constantly', t['state'] == 'video'
          and t['snap']['buffered'][0][0] <= t['snap']['ct'] <= t['snap']['buffered'][-1][1] and t['srcs'] == 1 and 1 <= len(removals(t)) <= 6, t)
    cut = r['cut']
    cut_rm = removals(cut)[0] if removals(cut) else None
    check('player: the stub can tell: with no margin the same trim cuts the playing GOP (its removal runs on to a keyframe after the playhead)',
          cut_rm and not (cut_rm['after'] and cut_rm['after'][0][0] <= cut_rm['ct'] <= cut_rm['after'][0][1]), removals(cut)[:1])
    check('player: no JS errors', r['errors'] == [[], [], [], []], r['errors'])


def assert_streamfault(r):
    S.section('stream: faults (stub MediaSource)')
    if not got('streamfault', r):
        return
    q = r['quota']
    check('fault: a full browser buffer (QuotaExceededError): the pane is "connecting" with that as its tooltip, the page asks again after about a second, and plays again',
          q['seen'] == ['video|', 'connecting|the browser buffer is full', 'video|'] and q['srcs'] == 2 and q['loads'] == 1 and q['after']['videos'] == 1
          and 900 <= q['fetches'][1] <= 2500, q)
    o = r['open']
    gaps = [b - a for a, b in zip(o['fetches'], o['fetches'][1:])]
    check('fault: a MediaSource that never opens: reconnect after the wait, again and again, slower each time (backoff), with the reason as the tooltip',
          o['pane']['title'] == 'the browser did not open the media source' and o['srcs'] >= 3 and o['opens'] == 0 and all(a < b for a, b in zip(gaps, gaps[1:])), (o['srcs'], gaps, o['pane']['title']))
    n = r['noMse']
    check('fault: no MediaSource at all: "this browser cannot play live streams" as the tooltip, no request for the media, no element', n['pane']['title'] == 'this browser cannot play live streams'
          and n['fetches'] == 0 and n['srcs'] == 0 and n['pane']['videos'] == 0 and n['pane']['state'] == 'connecting', n)
    s = r['stall']
    check('fault: a relay gone silent: reconnect, with "no data from the stream for Ns" as the tooltip', any(x.startswith('connecting|no data from the stream for') for x in s['seen'])
          and s['srcs'] >= 2 and s['fetches'] >= 2, s)
    fz, fl = r['frozen'], r['frozenLater']
    check('fault: bytes arrive but the buffered end stands still (the audio stopped): reconnect, with "the video froze" as the tooltip; and it plays again',
          fz['seen'][-1] == 'connecting|the video froze' and fl['seen'][-1] == 'video|', (fz, fl['seen']))
    m, e, mr = r['mms'], r['mmsEnded'], r['mmsResumed']
    check('fault: ManagedMediaSource only: it opens (the element has disableRemotePlayback), plays', m['pane']['state'] == 'video' and m['opens'] == 1 and m['srcs'] == 1 and m['fetches'] == 1 and m['viewers'] == 1, m)
    check('fault: endstreaming: the page stops reading (the board loses its viewer), the element stays', e['fetches'] == 1 and e['viewers'] == 0 and e['loads'] == 0 and e['state'] == 'video', e)
    check('fault: startstreaming: it reads again (a new request, a new init segment appended), without a new element, and the playhead moves on past the old end',
          mr['fetches'] == 2 and mr['viewers'] == 1 and mr['loads'] == 0 and mr['state'] == 'video' and mr['inits'] == 2 and mr['snap']['ct'] > e['snap']['buffered'][-1][1] + 1, (e['snap'], mr))
    check('fault: no JS errors', r['errors'] == [[]], r['errors'])


def assert_streamlife(r):
    S.section('stream: parking, a page kept for Back, the browser coming back online')
    if not got('streamlife', r):
        return
    check('life: two boards play; nothing parks early (1.2s hidden)', r['start'] == {'viewers': 2, 'm': 1, 's': 1} and r['early'] == {'m': 1, 'mLoads': 0, 'viewers': 2}, (r['start'], r['early']))
    pk = r['parked']
    check('life: hidden and muted past the park time, the tab lets its media connection go (the element gone, the board loses the viewer); the one with sound on goes on',
          pk['m']['videos'] == 0 and pk['mLoads'] == 1 and pk['viewers'] == 1 and pk['s'] == 1 and pk['sLoads'] == 0, pk)
    b = r['back']
    check('life: shown again, the parked board takes the stream up again (a new request, a new element); the other one is as it was',
          b['m']['state'] == 'video' and b['mSrcs'] == 2 and b['mFetches'] == 2 and b['s'] == 1 and b['sSrcs'] == 1 and b['viewers'] == 2, b)
    check('life: a page kept for Back (pagehide persisted) stops its player; shown again (pageshow) it attaches again', r['pagehide'] == {'videos': 0, 'loads': 1}
          and r['pageshow'] == {'state': 'video', 'srcs': 2}, (r['pagehide'], r['pageshow']))
    on = r['online']
    check('life: the browser coming back online cuts a retry\'s wait short (the third try came when it did, 0.3s after the second failed; its wait was 2s)',
          250 <= on['waited'] <= 900 and 250 <= on['fetches'][-1] <= 900, on)
    check('life: no JS errors', r['errors'] == [[], [], []], r['errors'])


def assert_streamnav(r):
    S.section('stream: reading position, deep links, Back, the deleted view')
    if not got('streamnav', r):
        return
    check('nav: opening a stream keeps the reading position (the document at 300: the board at 300, the document at 0); closing it moves it back',
          r['before'] == {'doc': 300, 'board': 0} and r['streaming']['doc'] == 0 and r['streaming']['board'] == 300 and r['streaming']['boardScrollH'] > r['streaming']['boardH']
          and r['closed'] == {'doc': 300, 'board': 0}, (r['before'], r['streaming'], r['closed']))
    h = r['hash']
    check('nav: a #task deep link scrolls the task into view inside the board (not the document) and marks it', h['streaming'] and h['doc'] == 0 and h['board'] > 0 and h['target']
          and h['row']['y'] >= h['boardRect']['y'] - 1 and h['row']['y'] + h['row']['h'] <= h['boardRect']['y'] + h['boardRect']['h'] + 1, h)
    check('nav: to a request and Back: the board\'s scroll position is back (400), the stream still on', r['listScroll'] == 400 and r['detail']['path'].startswith('/r/')
          and r['detail']['streaming'] and r['back']['path'] == '/' and r['back']['board'] == 400 and r['back']['streaming'] and r['back']['doc'] == 0, (r['listScroll'], r['back']))
    d = r['deleted']
    check('nav: a request deleted behind the page: the deleted view keeps the pane, and its timer counts on the board\'s clock (the browser\'s is an hour fast)',
          d['title'] == 'This request was deleted.' and d['streaming'] and d['line'] == 'connecting to http://fake.invalid/hang...' and secs(d['later']) is not None
          and secs(d['later']) - secs(d['first']) >= 1 and secs(d['later']) < 60, d)
    u = r['unknown']
    check('nav: a request that never existed: the same, and the timer is right with that clock too', u['title'] == 'Request not found' and u['streaming'] and secs(u['timer']) is not None
          and secs(u['timer']) < 60, u)
    check('nav: no JS errors', r['errors'] == [[]], r['errors'])


def assert_streamoffline(r):
    S.section('stream: the board out of reach while a stream plays and is replaced')
    if not got('streamoffline', r):
        return
    s = r['samples']
    check('offline: the old media ends when the stream is replaced; the pane says so, then that the stream cannot be reached; the board shows offline',
          r['playing']['state'] == 'video' and any(x['title'] == 'the stream ended' for x in s) and any(x['title'] == "can't reach the stream" for x in s)
          and any(x['conn'] == 'offline' for x in s), s)
    a = [x for x in r['fetches'] if x[1] == 'A']
    check('offline: the retries meanwhile fail quietly and slowly (dropped like the polls, never more than one per second), and the one after is told the stream was replaced (410)',
          2 <= len(a) <= 6 and [x[2] for x in a][1:3] == [-1, -1] and a[-1][2] == 410 and all(b[0] - c[0] >= 1000 for c, b in zip(a, a[1:])), r['fetches'])
    check('offline: back in reach, the page takes the new stream (the new id, fragments marked B) without a reload', r['final']['state'] == 'video' and r['final']['videos'] == 1
          and r['last'] == 'BBB' and r['markers'] == 'AB' and r['fetches'][-1][1] == 'B' and r['fetches'][-1][2] == 200 and r['viewers'] == 1, (r['final'], r['markers'], r['fetches']))
    check('offline: no JS errors', r['errors'] == [[]], r['errors'])


def assert_streamodd(r):
    S.section('stream: a board that answers oddly')
    if not got('streamodd', r):
        return
    none = ('start', 'missing', 'string', 'noId')
    check('odd: no stream (and so no pane): none in the events, a value that is not an object, an object without an id', all(r[k]['streaming'] is False and r[k]['hidden'] is True for k in none), {k: r[k]['streaming'] for k in none})
    mn = r['minimal']
    check('odd: only an id and a url: the pane is "connecting to <url>..." with a timer that starts at 0 (no `since` to count from)', mn['streaming'] and mn['state'] == 'connecting'
          and mn['line'] == 'connecting to http://odd.invalid/s...' and at(mn, 0, 422, 390, 422) and secs(mn['timer']) is not None and secs(mn['timer']) <= 4, mn)
    bg = r['bogus']
    check('odd: an unknown state counts as connecting; its error is the tooltip, as text; a `since` an hour ahead gives a timer of 0s, never a negative or odd one',
          bg['streaming'] and bg['state'] == 'connecting' and bg['title'] == '<img src=x onerror=alert(1)>' and bg['imgs'] == 0 and bg['alerts'] == [] and bg['timer'] == '0s', bg)
    lv = r['live']
    check('odd: a media path that is not this board\'s is ignored: the page asks this board\'s route, and plays', lv['state'] == 'video' and r['fetches'] == ['/api/stream/media?id=abc125'], (lv['state'], r['fetches']))
    check('odd: a codec this browser cannot play blocks the stream (tooltip, no video); the same stream with codecs it can play lifts the block',
          r['blocked']['state'] == 'connecting' and r['blocked']['title'] == 'this browser cannot play this stream (hev1)' and r['blocked']['videos'] == 0
          and r['unblocked']['state'] == 'video' and r['unblocked']['videos'] == 1, (r['blocked'], r['unblocked']['state']))
    check('odd: the key gone again: the pane goes, the video with it', r['gone']['streaming'] is False and r['gone']['videos'] == 0, r['gone'])
    check('odd: no JS errors', r['errors'] == [[]], r['errors'])


def assert_streamplay(r, up, name='streamplay'):
    """up: the generated source (FakeUpstream); None for a real one (UI_STREAM_URL), whose size and frame rate are not known here."""
    if not got(name, r):
        return
    if not r['live']:
        check(f'{name}: the board got the stream live', False, r['server'])
        return
    if r.get('skip'):
        S.skip(f'{name}: real playback', r['skip'])
        return
    pre, play, srv = r['pre'], r['play'], r['server']
    check(f'{name}: the board got the stream live (ffmpeg remuxed it: an H.264 video, and audio if the source has it), and a preflight page decodes what the board relays',
          r['live'] and srv['video'].startswith('avc1.') and pre['decoded'] is True and pre['size'] == [srv['width'], srv['height']]
          and (up is None or ((srv['width'], srv['height']) == (320, 180) and srv['audio'] == 'mp4a.40.2')), (srv, pre))
    check(f'{name}: the board plays it with the real MediaSource: the picture has the source\'s size, the pane is 16:9 at the bottom, muted with the chip (autoplay is refused)',
          r['pane']['state'] == 'video' and r['pane']['held'] == ('sound' if srv['audio'] else None) and r['pane']['videos'] == 1 and (play['w'], play['h']) == (srv['width'], srv['height'])
          and play['muted'] is True and abs(r['pane']['rect']['h'] - 390 * srv['height'] / srv['width']) < 1.0, (r['pane'], play))
    check(f'{name}: it moves: the playhead advances in real time, the frames are decoded (a frame rate of at least 10, none dropped for good), no error',
          abs(play['advanced'] - play['seconds']) < 0.6 and play['frames'] > 10 * play['seconds'] and play['readyState'] >= 2 and play['error'] is None, play)
    check(f'{name}: the buffer is one range, seconds long, not far from the live edge (lag under 2s)', play['ranges'] == 1 and play['span'] < 15 and 0 <= play['lag'] < 2, play)
    if up is not None:
        check(f'{name}: the source was read once, whatever the number of viewers', up.connections == 1, up.connections)
    check(f'{name}: no JS errors', r['errors'] == [[]], r['errors'])


if __name__ == '__main__':
    # python3 tests/test_ui.py [base] [v2] [stream]: only those groups of cases (every one without arguments)
    groups = set(sys.argv[1:]) or {'base', 'v2', 'stream'}
    if groups - {'base', 'v2', 'stream'}:
        sys.exit(f'usage: test_ui.py [base] [v2] [stream]   (no argument: all); not a group: {sorted(groups - {"base", "v2", "stream"})}')
    env = clean_env(UI_RESULTS_DIR=RESULTS)
    board = ScratchServer(TMP, '--host', '127.0.0.1', '--stale-after', str(STALE_AFTER), script=UI / 'harness.py',
                          name='ui', env=env).start()
    empty = ScratchServer(TMP, '--host', '127.0.0.1', script=UI / 'harness.py', name='ui-empty', env=env).start()
    try:
        S.section('preflight')
        r = firefox('ping', empty.url, '/', 390, 'light', 'ping', 500)
        if not check('firefox loads a page and runs the injected test script', r and r.get('ok'), r):
            sys.exit(S.finish())
        if 'base' in groups:
            ids = seed(board.url, board.db)
            read_only_cases(board.url, empty.url, ids)
            stateful_cases(board.url, board, ids)
        if 'v2' in groups:
            v2_cases(env)
        if 'stream' in groups:
            stream_cases()
    finally:
        board.stop()
        empty.stop()
        keep = os.environ.get('UI_SHOTS_DIR')
        if keep:
            Path(keep).mkdir(parents=True, exist_ok=True)
            for shot in SHOTS.glob('*.png'):
                shutil.copy2(shot, keep)
            (Path(keep) / 'v2_results.json').write_text(json.dumps(RESULTS_V2, indent=1))
            (Path(keep) / 'stream_results.json').write_text(json.dumps(RESULTS_STREAM, indent=1))
            print(f'screenshots (and the v2 and stream results) kept in {keep}')
    sys.exit(S.finish())
