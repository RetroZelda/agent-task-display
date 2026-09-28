#!/usr/bin/env python3
"""Dashboard UI in headless Firefox (skipped when Firefox is not installed).

A scratch board runs tests/ui/harness.py (the real server plus test hooks), seeded through the API
with a request in every state, then backdated in its scratch database where a state needs age
(stale). Each case loads a page with one tests/ui/*.js script injected; the script checks the page
from inside and posts a JSON result, asserted on here. Firefox's --screenshot mode loads the page and
exits once it has loaded; a slow <img> holds the load event until the script is done.

    FIREFOX=/path/to/firefox   use that binary
    UI_SHOTS_DIR=DIR           keep every case's screenshot there, for a human look
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the checkout, even when run without run_all.sh
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from harness import TESTS, ScratchServer, Suite, api, clean_env, scratch_home, skip_suite, tempdir  # noqa: E402

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

def firefox(name: str, base: str, path: str, width: int, theme: str, script: str, wait: int, extra: str = '') -> dict | None:
    if STOPPING:
        return None
    prof = TMP / f'profile-{name}'
    prof.mkdir()
    dark = theme == 'dark'
    (prof / 'user.js').write_text('\n'.join((
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
    proc = subprocess.Popen([FIREFOX, '--headless', '--no-remote', '--profile', str(prof), f'--window-size={width},900',
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
                       ('hidden', base, '/', 390, 'light', 'hidden', 9000),
                       ('banner', base, '/', 390, 'light', 'banner', 13000)])
    api(base, 'GET', '/__log?clear=1')
    res.update(in_parallel([('actions', base, f"/r/{ids['actions']}", 390, 'light', 'actions', 7000),
                            ('deleted', base, f"/r/{ids['deleted']}", 390, 'light', 'deleted', 8000),
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

    S.section('a hidden tab stops polling')
    r = res['hidden']
    if got('hidden', r):
        during = [p for p in r['polls'] if r['hiddenAt'] + 100 < p[0] < r['shownAt']]
        after = [p for p in r['polls'] if r['shownAt'] <= p[0] < r['shownAt'] + 400]
        later = [p for p in r['polls'] if p[0] >= r['shownAt'] + 400]
        check('hidden: no polls while hidden', during == [], r['polls'])
        check('hidden: polls again as soon as it is shown', len(after) >= 1, r['polls'])
        check('hidden: then keeps polling', len(later) >= 1, r['polls'])

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
        check('deleted: polling stops', r['pollsAfter'] == 0, r['pollsAfter'])

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


if __name__ == '__main__':
    env = clean_env(UI_RESULTS_DIR=RESULTS)
    board = ScratchServer(TMP, '--host', '127.0.0.1', '--stale-after', str(STALE_AFTER), script=UI / 'harness.py',
                          name='ui', env=env).start()
    empty = ScratchServer(TMP, '--host', '127.0.0.1', script=UI / 'harness.py', name='ui-empty', env=env).start()
    try:
        S.section('preflight')
        r = firefox('ping', empty.url, '/', 390, 'light', 'ping', 500)
        if not check('firefox loads a page and runs the injected test script', r and r.get('ok'), r):
            sys.exit(S.finish())
        ids = seed(board.url, board.db)
        read_only_cases(board.url, empty.url, ids)
        stateful_cases(board.url, board, ids)
    finally:
        board.stop()
        empty.stop()
        keep = os.environ.get('UI_SHOTS_DIR')
        if keep:
            Path(keep).mkdir(parents=True, exist_ok=True)
            for shot in SHOTS.glob('*.png'):
                shutil.copy2(shot, keep)
            print(f'screenshots kept in {keep}')
    sys.exit(S.finish())
