#!/usr/bin/env python3
"""Dual-stack listening, the Origin/Host guard and base URLs with IPv6 literals, --host variants, the
IPv4-only fallback, and http.server's own error paths (as JSON) over IPv4 and IPv6.

Needs ::1 for the IPv6 parts (skipped otherwise); the link-local cases use this machine's first
fe80:: address and are skipped when it has none; the curl cases are skipped without curl."""
from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the checkout, even when run without run_all.sh
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from harness import (SERVER, ScratchServer, Suite, call, connects, free_port, has_ipv6, link_local,  # noqa: E402
                     scratch_home, tempdir)

S = Suite('ipv6')
check = S.check
TMP = tempdir('ipv6')
scratch_home(TMP)
V6 = has_ipv6()
LL = link_local()          # (address, interface) or None
CURL = shutil.which('curl')
ADDRS = ['127.0.0.1'] + (['::1'] if V6 else [])


def raw(data: bytes, port: int, addr='127.0.0.1', rst=False):
    """Sends bytes on a bare socket; returns (status, headers, parsed body) or None after an RST."""
    fam = socket.AF_INET6 if ':' in addr else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.settimeout(5)
    s.connect(socket.getaddrinfo(addr, port, fam, socket.SOCK_STREAM)[0][4])
    s.sendall(data)
    if rst:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b'\x01\x00\x00\x00\x00\x00\x00\x00')
        s.close()
        return None
    out = b''
    while True:
        try:
            chunk = s.recv(65536)
        except (ConnectionResetError, TimeoutError):
            break
        if not chunk:
            break
        out += chunk
    s.close()
    if not out.startswith(b'HTTP/'):
        return None, {}, json.loads(out) if out else None
    head, _, body = out.partition(b'\r\n\r\n')
    lines = head.decode('latin-1').split('\r\n')
    status = int(lines[0].split()[1])
    hdrs = {line.split(':', 1)[0].lower(): line.split(':', 1)[1].strip() for line in lines[1:] if ':' in line}
    try:
        payload = json.loads(body) if body else None
    except ValueError:
        payload = body
    return status, hdrs, payload


def listening_line(srv):
    m = re.search(r'tasks server listening on (.*?) \(db ', srv.log_text())
    return m and m.group(1)


def baked(text):
    return re.findall('BAKED_URL = .*', text)


def phase_dual_stack():
    S.section('dual-stack default host')
    srv = ScratchServer(TMP, '--verbose', name='dual').start()
    P = srv.port
    try:
        check('the startup line names both stacks', listening_line(srv) == f'http://0.0.0.0:{P} and http://[::]:{P}', listening_line(srv))
        for addr in ADDRS:
            s, h, b = call(P, 'GET', '/api/health', addr=addr)
            check(f'health over {addr}', s == 200 and b['service'] == 'tasks', (s, b))
        if CURL:
            for url in (f'http://[::1]:{P}/api/health', f'http://127.0.0.1:{P}/api/health'):
                res = subprocess.run([CURL, '-sS', '-g', '--noproxy', '*', url], capture_output=True, text=True, timeout=10)
                check(f'curl {url}', '"service": "tasks"' in res.stdout, res)
            if LL:
                res = subprocess.run([CURL, '-sS', '-g', '--noproxy', '*', f'http://[{LL[0]}%25{LL[1]}]:{P}/api/health'],
                                     capture_output=True, text=True, timeout=10)
                check('curl over the link-local literal', '"service": "tasks"' in res.stdout, res)
            else:
                S.skip('curl over a link-local literal', 'no fe80:: address on this machine')
        else:
            S.skip('curl cases', 'curl not installed')

        S.section('the Origin/Host guard with IPv6 literals')
        s, _, b = call(P, 'POST', '/api/requests', {'title': 'v6'}, addr='::1', host=f'[::1]:{P}', headers={'Origin': f'http://[::1]:{P}'})
        check('Origin == Host [::1]: allowed, url on [::1]', s == 201 and b['url'].startswith(f'http://[::1]:{P}/r/'), (s, b))
        s, _, b = call(P, 'POST', '/api/requests', {'title': 'x'}, addr='::1', host=f'[::1]:{P}', headers={'Origin': 'http://evil.example'})
        check('foreign Origin over IPv6: 403', s == 403 and b == {'error': 'cross-origin write refused'}, (s, b))
        s, _, b = call(P, 'POST', '/api/requests', {'title': 'x'}, addr='::1', host=f'evil.example:{P}',
                       headers={'Origin': f'http://evil.example:{P}'})
        check('DNS rebinding over IPv6: 403', s == 403, (s, b))
        s, _, b = call(P, 'POST', '/api/requests', {'title': 'x'}, host=f'[::1]:{P}', headers={'Origin': f'http://[::2]:{P}'})
        check('Origin [::2] vs Host [::1]: 403', s == 403, (s, b))
        s, _, b = call(P, 'POST', '/api/requests', {'title': 'll'}, host=f'[fe80::1]:{P}', headers={'Origin': f'http://[fe80::1]:{P}'})
        check('link-local literal Origin == Host: allowed, url on it', s == 201 and b['url'].startswith(f'http://[fe80::1]:{P}/r/'), (s, b))
        s, _, b = call(P, 'POST', '/api/requests', {'title': 'llz'}, host=f'[fe80::1%25eth0]:{P}',
                       headers={'Origin': f'http://[fe80::1%25eth0]:{P}'})
        check('zoned link-local Origin == Host: allowed (zone ignored for the IP check)', s == 201, (s, b))
        s, _, b = call(P, 'POST', '/api/requests', {'title': 'mapped'}, host=f'[::ffff:127.0.0.1]:{P}',
                       headers={'Origin': f'http://[::ffff:127.0.0.1]:{P}'})
        check('IPv4-mapped literal: allowed', s == 201, (s, b))
        hn = socket.gethostname()
        s, _, b = call(P, 'POST', '/api/requests', {'title': 'hn'}, addr='::1', host=f'{hn}:{P}', headers={'Origin': f'http://{hn}:{P}'})
        check('machine hostname over IPv6: allowed', s == 201 and b['url'].lower().startswith(f'http://{hn.lower()}:{P}/r/'), (s, b))
        s, _, b = call(P, 'POST', '/api/requests', {'title': 'x'}, headers={'Origin': 'http://[abc'})
        check('malformed IPv6 Origin: 403, not 500', s == 403 and b == {'error': 'cross-origin write refused'}, (s, b))
        s, _, b = call(P, 'POST', '/api/requests', {'title': 'x'}, host='[abc', headers={'Origin': 'http://[abc'})
        check('malformed Host and Origin: 403', s == 403, (s, b))

        S.section('base URLs baked from IPv6 addresses')
        s, _, b = call(P, 'GET', '/api/usage', addr='::1', host=f'[::1]:{P}')
        check('usage via Host [::1] bakes it', s == 200 and f'http://[::1]:{P} is a task-status board' in b and '{{BASE_URL}}' not in b, b[:300])
        s, _, b = call(P, 'GET', '/api/skill/taskctl.py', host='x"y')
        check('socket fallback over IPv4: the ::ffff: prefix stripped', f'BAKED_URL = "http://127.0.0.1:{P}"' in b, baked(b))
        s, _, b = call(P, 'GET', '/api/skill/taskctl.py', addr='::1', host='x"y')
        check('socket fallback over IPv6: bracketed', f'BAKED_URL = "http://[::1]:{P}"' in b, baked(b))
        if LL:
            addr, iface = LL
            s, _, b = call(P, 'GET', '/api/skill/taskctl.py', addr=f'{addr}%{iface}', host='x"y')
            check('socket fallback over link-local: zone dropped', f'BAKED_URL = "http://[{addr}]:{P}"' in b, baked(b))
            s, _, b = call(P, 'GET', '/api/skill/taskctl.py', addr=f'{addr}%{iface}', host=f'[{addr}%25{iface}]:{P}')
            check('a zoned Host is never baked', f'BAKED_URL = "http://[{addr}]:{P}"' in b and '%' not in baked(b)[0], baked(b))
        else:
            S.skip('link-local socket fallback', 'no fe80:: address on this machine')

        S.section('client addresses in the log')
        call(P, 'GET', '/api/nope-v4')
        call(P, 'GET', '/api/nope-v6', addr='::1')
        raw(b'GET / HTTP/1.0\r\nX-Partial: y', P, rst=True)
        time.sleep(0.3)
        log = srv.log_text()
        check('IPv4 client logged as 127.0.0.1', re.search(r' INFO 127\.0\.0\.1 GET /api/nope-v4 404 ', log), log[-600:])
        check('IPv6 client logged as ::1', re.search(r' INFO ::1 GET /api/nope-v6 404 ', log))
        check('IPv4 success line (--verbose) plain', re.search(r' DEBUG 127\.0\.0\.1 GET /api/health 200 ', log))
        check('no ::ffff: anywhere in the log', '::ffff:' not in log,
              [line for line in log.splitlines() if '::ffff:' in line][:3])
        check('a connection error names the plain address (if one was logged)',
              re.search(r'connection error from 127\.0\.0\.1: ', log) or 'connection error' not in log)
    finally:
        check('dual-stack server exits 0 on SIGTERM', srv.stop() == 0)


def phase_errors():
    S.section("http.server's own rejections are JSON, over " + ' and '.join(ADDRS))
    srv = ScratchServer(TMP, name='errors').start()
    P = srv.port
    try:
        for addr in ADDRS:
            for method in ('OPTIONS', 'TRACE', 'CONNECT'):
                s, h, b = raw(f'{method} /api/requests HTTP/1.1\r\nHost: x\r\n\r\n'.encode(), P, addr)
                check(f'{method} -> JSON 405 + Allow ({addr})', s == 405 and h.get('allow') == 'GET, POST'
                      and h.get('content-type') == 'application/json' and h.get('x-content-type-options') == 'nosniff'
                      and h.get('cache-control') == 'no-store' and b == {'error': f'{method} is not allowed on /api/requests'}, (s, h, b))
            s, h, b = raw(b'OPTIONS /api/tasks/k3m9qa-1/progress HTTP/1.0\r\n\r\n', P, addr)
            check(f'OPTIONS on progress: Allow POST ({addr})', s == 405 and h.get('allow') == 'POST', (s, h))
            for data, code, error in (
                    (b'FOO /api/requests HTTP/1.0\r\n\r\n', 501, "Unsupported method ('FOO')"),
                    (b'GARBAGE\r\n\r\n', 400, "Bad request syntax ('GARBAGE')"),
                    (b'GET / HTTP/x.y\r\n\r\n', 400, "Bad request version ('HTTP/x.y')"),
                    (b'POST /\r\n\r\n', 400, "Bad HTTP/0.9 request type ('POST')"),
                    (b'GET / HTTP/2.0\r\n\r\n', 505, 'Invalid HTTP version (2.0)'),
                    (b'GET /' + b'a' * 70000 + b' HTTP/1.0\r\n\r\n', 414, 'URI Too Long'),
                    (b'GET / HTTP/1.0\r\nX: ' + b'a' * 70000 + b'\r\n\r\n', 431, 'Line too long'),
                    (b'GET / HTTP/1.0\r\n' + b''.join(b'X%d: y\r\n' % i for i in range(120)) + b'\r\n', 431, 'Too many headers'),
                    (b'GET http://[x/api/health HTTP/1.0\r\n\r\n', 400, 'malformed request target')):
                s, h, b = raw(data, P, addr)
                check(f'{code} {error} as JSON ({addr})', s == code and b == {'error': error}
                      and h.get('content-type') == 'application/json' and h.get('x-content-type-options') == 'nosniff'
                      and h.get('cache-control') == 'no-store'
                      and int(h.get('content-length', -1)) == len(json.dumps({'error': error}).encode()), (s, h, b))
            s, h, b = raw(b'HEAD http://[x/api/health HTTP/1.0\r\n\r\n', P, addr)
            check(f'HEAD with a malformed target: 400, no body ({addr})', s == 400 and b is None and int(h['content-length']) > 0, (s, h, b))
            s, h, b = raw(b'GET http://127.0.0.1/api/health HTTP/1.0\r\n\r\n', P, addr)
            check(f'absolute-form target still served ({addr})', s == 200 and b['service'] == 'tasks', (s, b))
        s, h, b = raw(b'GET /api/health\r\n\r\n', P)
        check('a genuine HTTP/0.9 GET is answered with a bare body', s is None and b['service'] == 'tasks', (s, b))

        S.section('log injection')
        raw(b'GET /api/requests/abc%0A2026-01-01%20INFO%20forged HTTP/1.0\r\n\r\n', P)
        raw(b'GET /x%0D%0A2026-01-01%2014:00:00,000%20INFO%20created%20request%20FORGED HTTP/1.0\r\n\r\n', P)
        raw(b'FOO\x01 / HTTP/1.0\r\n\r\n', P)
        time.sleep(0.3)
        log = srv.log_text()
        check('no forged log lines', not any(line.startswith('2026-01-01') for line in log.splitlines()), log[-800:])
        check('the error note is sanitised in the log', "malformed request id 'abc?2026-01-01 info forged'" in log)
        check('every log line is well-formed', all(re.match(r'\d{4}-\d\d-\d\d \d\d:\d\d:\d\d,\d{3} (INFO|DEBUG|WARNING|ERROR) ', line)
                                                    for line in log.splitlines() if line),
              [line for line in log.splitlines() if not re.match(r'\d{4}-', line)][:3])
        check('no tracebacks in the log', 'Traceback' not in log)
        check('an early 400 is logged with placeholders', ' - - 400 ' in log and "Bad request syntax ('GARBAGE')" in log)
    finally:
        srv.stop()


def phase_hosts():
    S.section('--host variants')
    for host in ('::', '[::]', '*', ''):
        with ScratchServer(TMP, '--host', host, name='host') as srv:
            P = srv.port
            check(f'--host {host!r} is dual-stack', listening_line(srv) == f'http://0.0.0.0:{P} and http://[::]:{P}', listening_line(srv))
            check(f'--host {host!r} answers on both', connects('127.0.0.1', P) and connects('::1', P))
    with ScratchServer(TMP, '--host', '127.0.0.1', name='host') as srv:
        P = srv.port
        check('--host 127.0.0.1: startup line', listening_line(srv) == f'http://127.0.0.1:{P}', listening_line(srv))
        check('--host 127.0.0.1 is IPv4 only', connects('127.0.0.1', P) and not connects('::1', P))
    with ScratchServer(TMP, '--host', '::1', name='host') as srv:
        P = srv.port
        check('--host ::1: startup line', listening_line(srv) == f'http://[::1]:{P}', listening_line(srv))
        check('--host ::1 is IPv6 only', connects('::1', P) and not connects('127.0.0.1', P))
        s, _, b = call(P, 'GET', '/api/skill/taskctl.py', addr='::1', host='x"y')
        check('--host ::1: socket fallback', f'BAKED_URL = "http://[::1]:{P}"' in b, baked(b))
    with ScratchServer(TMP, '--host', '[::1]', name='host') as srv:
        check('--host [::1] (bracketed) binds ::1', listening_line(srv) == f'http://[::1]:{srv.port}', srv.log_text()[-300:])
    if LL:
        addr, iface = LL
        with ScratchServer(TMP, '--host', f'{addr}%{iface}', name='host') as srv:
            P = srv.port
            check('--host link-local%zone binds only that address', srv.proc.poll() is None and connects(f'{addr}%{iface}', P)
                  and not connects('127.0.0.1', P) and not connects('::1', P), srv.log_text()[-300:])
    else:
        S.skip('--host link-local%zone', 'no fe80:: address on this machine')
    srv = ScratchServer(TMP, '--host', '192.0.2.1', name='unbindable').start(wait=False)
    code = srv.proc.wait(10)
    check('an unbindable IPv4 host exits 2', code == 2 and 'cannot listen on 192.0.2.1' in srv.log_text(), srv.log_text())


def phase_fallback():
    S.section('IPv6 unavailable -> IPv4 fallback; EADDRINUSE')
    wrapper = TMP / 'nov6_wrapper.py'
    wrapper.write_text(
        'import errno, runpy, socket, sys\n'
        'sys.dont_write_bytecode = True\n'
        '_Orig = socket.socket\n'
        'class NoV6(_Orig):\n'
        '    def __init__(self, family=-1, *a, **k):\n'
        '        if family == socket.AF_INET6:\n'
        "            raise OSError(errno.EAFNOSUPPORT, 'Address family not supported by protocol')\n"
        '        super().__init__(family, *a, **k)\n'
        'socket.socket = NoV6\n'
        'sys.argv = sys.argv[1:]\n'
        "runpy.run_path(sys.argv[0], run_name='__main__')\n")
    with ScratchServer(TMP, wrapper=wrapper, name='nov6') as srv:
        P = srv.port
        log = srv.log_text()
        check('fallback INFO line', 'INFO IPv6 is unavailable (Address family not supported by protocol); listening on IPv4 only' in log, log)
        check('fallback listens on 0.0.0.0', listening_line(srv) == f'http://0.0.0.0:{P}', listening_line(srv))
        check('fallback serves IPv4', call(P, 'GET', '/api/health')[0] == 200)
        check('fallback has no IPv6', not connects('::1', P))
    with ScratchServer(TMP, name='first') as srv:
        P = srv.port
        second = subprocess.run([sys.executable, str(SERVER), '--port', str(P), '--db', str(TMP / 'other.db'),
                                 '--pidfile', str(TMP / 'other.pid')], capture_output=True, text=True, timeout=10)
        check('dual-stack EADDRINUSE: exit 2, no silent IPv4 fallback', second.returncode == 2
              and second.stderr.startswith(f'error: port {P} already in use') and 'IPv6 is unavailable' not in second.stderr, second.stderr)
    busy = free_port()
    blocker = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    blocker.bind(('127.0.0.1', busy))
    blocker.listen()
    try:
        res = subprocess.run([sys.executable, str(SERVER), '--port', str(busy), '--db', str(TMP / 'other.db'),
                              '--pidfile', str(TMP / 'other.pid')], capture_output=True, text=True, timeout=10)
        check('an IPv4-only listener on the port also blocks the dual-stack bind', res.returncode == 2
              and 'already in use' in res.stderr and 'IPv6 is unavailable' not in res.stderr, res.stderr)
    finally:
        blocker.close()


if __name__ == '__main__':
    if V6:
        phase_dual_stack()
    else:
        S.skip('dual-stack phase', 'no IPv6 loopback (::1) on this machine')
    phase_errors()
    if V6:
        phase_hosts()
    else:
        S.skip('--host variants', 'no IPv6 loopback (::1) on this machine')
    phase_fallback()
    sys.exit(S.finish())
