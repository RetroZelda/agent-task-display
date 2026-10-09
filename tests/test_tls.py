#!/usr/bin/env python3
"""The optional HTTPS listener (--tls-port, --tls-cert, --tls-key; TASKS_TLS_*), with a self-signed
certificate made by openssl (the suite is skipped without it): the same routes over the same database,
dual-stack like the main listener; rendered agent files that keep agents on plain http (BASE_URL =
http://<Host's name>:<http port>) while human links stay https; a taskctl downloaded over https
reporting over http; plain HTTP and idle clients on the TLS port blocking nothing; and every argument,
certificate and port error (exit 2, nothing created).
"""
from __future__ import annotations

import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the checkout, even when run without run_all.sh
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from harness import (FIXTURES, SERVER, ScratchServer, Suite, call, clean_env, free_port, has_ipv6,  # noqa: E402
                     scratch_home, self_signed_cert, skip_suite, tempdir, wait_until)

S = Suite('tls')
check = S.check
TMP = tempdir('tls')
scratch_home(TMP)
made = self_signed_cert(TMP)
if made is None:
    skip_suite('openssl not installed (or it could not make a self-signed certificate)')
CERT, KEY, KEY_ENC = made


def tcall(port, *args, **kw):
    return call(port, *args, tls=True, **kw)


def fixture_tree(name):
    tree = TMP / name
    shutil.copytree(FIXTURES / 'fake_tasks', tree)
    shutil.copy2(SERVER, tree / 'server.py')
    (tree / 'templates' / 'changelog.md').write_text('## v2 — 2026-09-28\n- {{BASE_URL}} {{PUBLIC_URL}}\n')
    return tree


def main_listener():
    S.section('the HTTPS listener beside the http one')
    tp = free_port()
    srv = ScratchServer(TMP, '--tls-port', str(tp), '--tls-cert', str(CERT), '--tls-key', str(KEY), name='tls').start()
    P = srv.port
    try:
        log = srv.log_text()
        want = f'https://0.0.0.0:{tp} and https://[::]:{tp}' if has_ipv6() else f'https://0.0.0.0:{tp}'
        m = re.search(r'https listener on (.+?) \(certificate (.+?)\)', log)
        check('the https listener is logged, dual-stack, with its certificate', m and m.group(1) == want and m.group(2) == str(CERT), log)
        check('...before the listening line', log.find('https listener on') < log.find('tasks server listening on'), log)
        s, h, b = tcall(tp, 'GET', '/api/health')
        check('https health: tls_port, version 3, both headers', s == 200 and b['tls_port'] == tp and b['version'] == '3'
              and h.get('x-tasks-version') == '3' and h.get('x-tasks-docs') == b['docs_version'], (s, b))
        check('the http listener reports tls_port too', srv.call('GET', '/api/health')[2]['tls_port'] == tp)
        if has_ipv6():
            s, h, b = tcall(tp, 'GET', '/api/health', addr='::1')
            check('https over ::1 (dual-stack)', s == 200 and b['service'] == 'tasks', (s, b))
        else:
            S.skip('https over ::1', 'no IPv6 loopback')
        s, h, b = tcall(tp, 'GET', '/', headers={'Accept': 'text/html'})
        check('the page over https', s == 200 and '<html' in b and h['content-type'] == 'text/html; charset=utf-8', s)
        s, h, b = tcall(tp, 'POST', '/api/requests', {'title': 'over tls', 'tasks': ['a']}, host=f'localhost:{tp}',
                        headers={'Origin': f'https://localhost:{tp}'})
        check('a same-origin write over https: 201, its url fields https', s == 201 and b['url'] == f"https://localhost:{tp}/r/{b['id']}"
              and b['tasks'][0]['url'].startswith(f'https://localhost:{tp}/r/'), b)
        rid = b['id']
        s, h, b = srv.call('GET', f'/api/requests/{rid}')
        check('the same database behind both listeners (the http view has http urls)', s == 200
              and b['url'] == f'http://127.0.0.1:{P}/r/{rid}', b)
        s, h, b = tcall(tp, 'POST', f'/api/tasks/{rid}-1/attention', {'message': 'over https?'})
        check('attention over https', s == 200 and b['attention']['message'] == 'over https?', b)
        check('/api/events over https sees it', any(w['task_id'] == f'{rid}-1' for w in tcall(tp, 'GET', '/api/events')[2]['waiting']))
        s, h, b = tcall(tp, 'PUT', '/api/settings', {'settings': {'sound': {'volume': 0.3}}}, host=f'localhost:{tp}',
                        headers={'Origin': f'https://localhost:{tp}'})
        check('a settings save from the https page', s == 200 and srv.call('GET', '/api/settings')[2]['settings']['sound']['volume'] == 0.3, b)
        s, h, b = tcall(tp, 'POST', '/api/requests', {'title': 'x'}, headers={'Origin': 'https://evil.example'})
        check('a foreign Origin over https: 403', s == 403, b)
        s, h, b = tcall(tp, 'GET', '/nope')
        check('a 404 over https: JSON, both headers, the hint on https', s == 404 and h.get('x-tasks-version') == '3'
              and b['hint'] == f'GET https://127.0.0.1:{tp}/api/usage', (s, h, b))

        S.section('agents stay on plain http: what the https listener renders')
        s, h, b = tcall(tp, 'GET', '/api/skill/taskctl.py')
        check('taskctl.py over https: BAKED_URL is http on the main port', f'BAKED_URL = "http://127.0.0.1:{P}"' in b,
              re.findall('BAKED_URL = .*', b))
        s, h, b = tcall(tp, 'GET', '/api/skill/taskctl.py', host=f'board.lan:{tp}')
        check("...by the Host's name", f'BAKED_URL = "http://board.lan:{P}"' in b, re.findall('BAKED_URL = .*', b))
        s, h, b = tcall(tp, 'GET', '/api/usage', host=f'board.lan:{tp}')
        check('usage over https: every curl on http://<name>:<http port>', f'curl -fsS --connect-timeout 5 --noproxy \'*\' http://board.lan:{P}/api/'
              in b and f'https://board.lan:{tp}/api/' not in b, b[:400])
        dl = TMP / 'dl'
        dl.mkdir()
        for name in ('taskctl', 'taskctl.py'):
            (dl / name).write_text(tcall(tp, 'GET', f'/api/skill/{name}')[2])
            (dl / name).chmod(0o755)
        p = subprocess.run([str(dl / 'taskctl'), '--strict', 'ping'], capture_output=True, text=True, timeout=30, env=clean_env())
        check('a taskctl downloaded over https reports over http with its baked URL', p.returncode == 0
              and p.stdout == f'http://127.0.0.1:{P}\n' and 'ok: board at' in p.stderr, (p.returncode, p.stdout, p.stderr))

        S.section('plain HTTP and idle clients on the TLS port block nothing')
        with socket.create_connection(('127.0.0.1', tp), 5) as sk:
            sk.sendall(b'GET /api/health HTTP/1.0\r\n\r\n')
            sk.settimeout(5)
            try:
                junk = sk.recv(200)
            except OSError:
                junk = b''
        check('plain HTTP to the TLS port gets no HTTP answer', not junk.startswith(b'HTTP/'), junk[:80])
        idle = [socket.create_connection(('127.0.0.1', tp), 5) for _ in range(3)]
        t0 = time.monotonic()
        s, h, b = tcall(tp, 'GET', '/api/health')
        check('three idle clients that never handshake do not hold up a new one', s == 200 and time.monotonic() - t0 < 3,
              time.monotonic() - t0)
        check('the http listener is unaffected', srv.call('GET', '/api/health')[0] == 200)
        for sk in idle:
            sk.close()
        ok = wait_until(lambda: 'connection error from 127.0.0.1' in srv.log_text(), 5)
        check('the bad handshake is logged as a connection error (no traceback)', ok and 'Traceback' not in srv.log_text(),
              srv.log_text()[-800:])
    finally:
        code = srv.stop()
    check('SIGTERM: exit 0, pidfile removed, both listeners closed', code == 0 and not srv.pidfile.exists()
          and call_fails(tp) and call_fails(P), code)


def call_fails(port):
    try:
        with socket.create_connection(('127.0.0.1', port), 1):
            return False
    except OSError:
        return True


def rendering():
    S.section('rendering on the https listener, against fixture templates')
    tree = fixture_tree('fixture')
    tp = free_port()
    with ScratchServer(TMP, '--tls-port', str(tp), '--tls-cert', str(CERT), '--tls-key', str(KEY), script=tree / 'server.py',
                       name='fixture') as srv:
        P = srv.port
        s, h, b = tcall(tp, 'GET', '/api/usage', host=f'board.lan:{tp}')
        check('BASE_URL: http, the main port; PUBLIC_URL: https, this one', f'Usage: curl http://board.lan:{P}/api/usage\n' in b
              and f'Dashboard: https://board.lan:{tp}\n' in b, b)
        s, h, b = tcall(tp, 'GET', '/api/usage', host=f'[::1]:{tp}')
        check('an IPv6 Host keeps its brackets', f'curl http://[::1]:{P}/api/usage' in b and f'Dashboard: https://[::1]:{tp}\n' in b, b)
        for evil in ('x"y', 'a b', '$(id)'):
            s, h, b = tcall(tp, 'GET', '/api/usage', host=evil)
            check(f'a crafted Host {evil!r}: the socket address and the http port, never the header', f'curl http://127.0.0.1:{P}/api/usage' in b
                  and f'Dashboard: https://127.0.0.1:{tp}\n' in b and evil not in b, b)
        s, h, b = tcall(tp, 'GET', '/api/changelog', host=f'box:{tp}')
        check('changelog: the agent base and the human base', b == f'## v2 — 2026-09-28\n- http://box:{P} https://box:{tp}\n', b)
        s, h, b = tcall(tp, 'GET', '/api/rule', host=f'box:{tp}')
        check("rule: its link (a human's) stays https", f'board at https://box:{tp}' in b, b)
        s, h, b = tcall(tp, 'GET', '/api/skill/taskctl.py', host=f'box:{tp}')
        check('taskctl.py: http on the main port', f'BAKED_URL = "http://box:{P}"' in b, b)
        s, h, b = srv.call('GET', '/api/usage', host=f'box:{P}')
        check('the http listener renders as before', f'Usage: curl http://box:{P}/api/usage\n' in b and f'Dashboard: http://box:{P}\n' in b, b)
    public = 'http://board.example:8123'
    tp = free_port()
    with ScratchServer(TMP, '--tls-port', str(tp), '--tls-cert', str(CERT), '--tls-key', str(KEY), '--public-url', public,
                       script=tree / 'server.py', name='fixture-public') as srv:
        s, h, b = tcall(tp, 'GET', '/api/usage', host='x"y')
        check('--public-url: PUBLIC_URL is it; a crafted Host still gives agents the socket ip + the http port',
              f'Dashboard: {public}\n' in b and f'curl http://127.0.0.1:{srv.port}/api/usage' in b, b)
        s, h, b = tcall(tp, 'POST', '/api/requests', {'title': 'x'})
        check('--public-url: url fields use it on the https listener too', s == 201 and b['url'].startswith(public + '/r/'), b)


def env_config():
    S.section('TASKS_TLS_PORT, TASKS_TLS_CERT, TASKS_TLS_KEY')
    tp = free_port()
    env = clean_env(TASKS_TLS_PORT=tp, TASKS_TLS_CERT=CERT, TASKS_TLS_KEY=KEY)
    with ScratchServer(TMP, '--host', '127.0.0.1', name='tlsenv', env=env) as srv:
        s, h, b = tcall(tp, 'GET', '/api/health')
        check('the env variables turn it on (a specific --host: that address only)', s == 200 and b['tls_port'] == tp
              and f'https listener on https://127.0.0.1:{tp}' in srv.log_text(), (s, srv.log_text()[-400:]))


def arg_errors():
    S.section('argument, certificate and port errors: exit 2, a clear error, nothing created')
    bad_db, bad_pid = TMP / 'bad.db', TMP / 'bad.pid'

    def run(*extra, env=None, port=None):
        return subprocess.run([sys.executable, str(SERVER), '--db', str(bad_db), '--pidfile', str(bad_pid),
                               '--port', str(port or free_port()), *map(str, extra)], capture_output=True, text=True, timeout=20,
                              env=env or clean_env(), stdin=subprocess.DEVNULL)
    for extra in (['--tls-port', free_port()], ['--tls-cert', CERT], ['--tls-key', KEY], ['--tls-port', free_port(), '--tls-key', KEY],
                  ['--tls-cert', CERT, '--tls-key', KEY]):
        r = run(*extra)
        named = [str(x) for x in extra if str(x).startswith('--')]
        check(f'only {" ".join(named)}: exit 2, "all three"', r.returncode == 2
              and 'give all three of --tls-port, --tls-cert and --tls-key, or none' in r.stderr, r.stderr[-300:])
    r = run(env=clean_env(TASKS_TLS_PORT=free_port()))
    check('only TASKS_TLS_PORT: exit 2', r.returncode == 2 and 'all three' in r.stderr, r.stderr[-300:])
    for value in ('0', '70000'):
        r = run('--tls-port', value, '--tls-cert', CERT, '--tls-key', KEY)
        check(f'--tls-port {value}: exit 2', r.returncode == 2 and '--tls-port must be 1..65535' in r.stderr, r.stderr[-300:])
    r = run('--tls-port', 'abc', '--tls-cert', CERT, '--tls-key', KEY)
    check('--tls-port abc: exit 2', r.returncode == 2 and 'invalid int value' in r.stderr, r.stderr[-300:])
    p = free_port()
    r = run('--tls-port', p, '--tls-cert', CERT, '--tls-key', KEY, port=p)
    check('--tls-port = --port: exit 2', r.returncode == 2 and f'--tls-port must differ from --port ({p})' in r.stderr, r.stderr[-300:])
    r = run('--tls-port', free_port(), '--tls-cert', TMP / 'nope.pem', '--tls-key', KEY)
    check('a missing certificate: exit 2', r.returncode == 2 and r.stderr.startswith('error: cannot load the TLS certificate'), r.stderr[-300:])
    r = run('--tls-port', free_port(), '--tls-cert', KEY, '--tls-key', CERT)
    check('certificate and key swapped: exit 2', r.returncode == 2 and 'cannot load the TLS certificate' in r.stderr, r.stderr[-300:])
    junk = TMP / 'junk.pem'
    junk.write_text('not a pem\n')
    r = run('--tls-port', free_port(), '--tls-cert', junk, '--tls-key', junk)
    check('a file that is not PEM: exit 2', r.returncode == 2 and 'cannot load the TLS certificate' in r.stderr, r.stderr[-300:])
    if KEY_ENC:
        t0 = time.monotonic()
        r = run('--tls-port', free_port(), '--tls-cert', CERT, '--tls-key', KEY_ENC)
        check('an encrypted key: exit 2, no password prompt', r.returncode == 2 and 'cannot load' in r.stderr
              and time.monotonic() - t0 < 15 and 'PEM pass phrase' not in r.stderr + r.stdout, (r.returncode, r.stderr[-300:]))
    else:
        S.skip('an encrypted key', 'openssl could not encrypt the test key')
    busy = socket.socket()
    busy.bind(('127.0.0.1', 0))
    busy.listen()
    bp = busy.getsockname()[1]
    try:
        r = run('--host', '127.0.0.1', '--tls-port', bp, '--tls-cert', CERT, '--tls-key', KEY)
    finally:
        busy.close()
    check('a busy TLS port: exit 2, the error names --tls-port', r.returncode == 2
          and r.stderr.startswith(f'error: --tls-port: port {bp} already in use'), r.stderr[-300:])
    check('no database and no pidfile after any of these', not bad_db.exists() and not bad_pid.exists())


if __name__ == '__main__':
    main_listener()
    rendering()
    env_config()
    arg_errors()
    sys.exit(S.finish())
