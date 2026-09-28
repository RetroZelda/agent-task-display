#!/usr/bin/env python3
"""Concurrency smoke: 40 reader threads (the list plus one detail page, as open dashboard tabs poll)
for CONC_SECONDS (default 5) against a seeded scratch board, while one writer posts progress every
0.2s. Every read and write must succeed with a well-formed body, and the last write must win.
Latencies are printed for information, not asserted."""
from __future__ import annotations

import json
import os
import statistics
import sys
import threading
import time
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the checkout, even when run without run_all.sh
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from harness import ScratchServer, Suite, scratch_home, tempdir  # noqa: E402

S = Suite('concurrency')
check = S.check
TMP = tempdir('conc')
scratch_home(TMP)
DURATION = float(os.environ.get('CONC_SECONDS', '5'))
READERS = 40
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def main(base):
    def call(method, path, body=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(base + path, data=data, method=method, headers={'Content-Type': 'application/json'})
        with OPENER.open(req, timeout=10) as r:
            return r.status, json.loads(r.read())

    S.section('seed: 30 requests x 5 tasks, a third finished; a 10-task detail target')
    for i in range(30):
        s, r = call('POST', '/api/requests', {'title': f'seed {i}', 'tasks': [f't{k}' for k in range(5)]})
        call('POST', f"/api/tasks/{r['tasks'][0]['id']}/progress", {'message': 'working', 'percent': 30})
        if i % 3 == 0:
            call('POST', f"/api/requests/{r['id']}/complete", {'status': 'done'})
    s, target = call('POST', '/api/requests', {'title': 'detail target', 'tasks': [f'w{k}' for k in range(10)]})
    rid, tid = target['id'], target['tasks'][0]['id']

    S.section(f'{READERS} readers + 1 writer for {DURATION:g}s')
    lat = {'list': [], 'detail': []}
    errs, lock = [], threading.Lock()
    writes = {'n': 0, 'lat': [], 'errs': [], 'last': None}
    stop_at = time.time() + DURATION

    def reader():
        while time.time() < stop_at:
            for kind, path in (('list', '/api/requests'), ('detail', '/api/requests/' + rid)):
                t0 = time.perf_counter()
                try:
                    s, body = call('GET', path)
                    good = s == 200 and (('requests' in body and body['counts']['running'] == 21) if kind == 'list'
                                         else body.get('id') == rid and len(body['tasks']) == 10)
                    with lock:
                        if good:
                            lat[kind].append((time.perf_counter() - t0) * 1000)
                        else:
                            errs.append(f'{kind}: bad body or status {s}')
                except Exception as e:  # noqa: BLE001
                    with lock:
                        errs.append(f'{kind}: {e!r}')

    def writer():
        pct = 0.0
        while time.time() < stop_at:
            pct = (pct + 1) % 100
            msg = f"step {writes['n']}"
            t0 = time.perf_counter()
            try:
                s, body = call('POST', f'/api/tasks/{tid}/progress', {'message': msg, 'percent': pct})
                if s != 200 or body.get('status') != 'running' or body.get('message') != msg:
                    writes['errs'].append(f"progress: {s} {body.get('status')} {body.get('message')!r}")
                writes['lat'].append((time.perf_counter() - t0) * 1000)
                writes['n'] += 1
                writes['last'] = (msg, round(pct, 1))
            except Exception as e:  # noqa: BLE001
                writes['errs'].append(repr(e))
            time.sleep(0.2)

    threads = [threading.Thread(target=reader) for _ in range(READERS)] + [threading.Thread(target=writer)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    def q(v, frac):
        v = sorted(v)
        return v[min(len(v) - 1, int(len(v) * frac))] if v else float('nan')

    reads = lat['list'] + lat['detail']
    print(f"  reads={len(reads)} (list {len(lat['list'])}, detail {len(lat['detail'])}) read_errors={len(errs)}"
          f"  writes={writes['n']} write_errors={len(writes['errs'])}")
    for name, v in (('all reads', reads), ('list', lat['list']), ('detail', lat['detail']), ('progress writes', writes['lat'])):
        if v:
            print(f'  {name:16} p50={statistics.median(v):.1f}ms p95={q(v, .95):.1f}ms p99={q(v, .99):.1f}ms max={max(v):.1f}ms')
    check('every read succeeded with a well-formed body', not errs, errs[:5])
    check('every progress write succeeded', not writes['errs'], writes['errs'][:5])
    check(f'the readers made real progress ({len(reads)} reads)', len(reads) >= READERS * 2)
    check(f"the writer kept its pace ({writes['n']} writes)", writes['n'] >= max(1, int(DURATION / 0.2 * 0.5)))
    s, final = call('GET', '/api/tasks/' + tid)
    check('the last write won', (final['message'], final['percent']) == writes['last'], (final['message'], final['percent'], writes['last']))
    s, lst = call('GET', '/api/requests')
    check('the counts are unchanged by the load', lst['counts'] == {'running': 21, 'stale': 0, 'done': 10, 'failed': 0}, lst['counts'])


if __name__ == '__main__':
    with ScratchServer(TMP, '--host', '127.0.0.1', name='conc') as srv:
        main(srv.url)
        log = srv.log_text()
        check('no errors or tracebacks in the server log', 'Traceback' not in log and ' ERROR ' not in log, log[-800:])
    sys.exit(S.finish())
