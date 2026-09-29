#!/usr/bin/env python3
"""Shared helpers for the Python test suites. Standard library only, Python 3.10+.

    from harness import Suite, ScratchServer, ...     (each suite puts tests/lib on sys.path)
    python3 tests/lib/harness.py free-port             (prints a free TCP port, for the shell suites)

Every suite runs against throwaway state: a scratch server on a free port (never the real board's
8765) with a temp --db, --pidfile and --config, a temp HOME / CLAUDE_CONFIG_DIR, and a temp taskctl
offline queue (TASKS_SPOOL_DIR), all removed at exit.
"""
from __future__ import annotations

import atexit
import hashlib
import http.client
import json
import os
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.dont_write_bytecode = True  # never leave __pycache__ in the repo (tasks/ is imported by some suites)

LIB = Path(__file__).resolve().parent
TESTS = LIB.parent
REPO = TESTS.parent
TASKS = REPO / 'tasks'
SERVER = TASKS / 'server.py'
WRAPPER = TASKS / 'taskctl'
TASKCTL_PY = TASKS / 'taskctl.py'
FIXTURES = TESTS / 'fixtures'
REAL_PORT = 8765   # the real board's default port; no test ever binds it
# The agent instructions the board versions (X-Tasks-Docs, docs_version), in the contract's order.
DOCS_FILES = ('skill/SKILL.md', 'templates/usage.md', 'templates/rule.md', 'templates/changelog.md', 'taskctl', 'taskctl.py')
SKIP_EXIT = 77     # a suite that cannot run here (missing optional dependency) exits with this

_TEMP_DIRS: list[Path] = []
_SERVERS: list['ScratchServer'] = []


# ---------------------------------------------------------------- results

class Suite:
    """Counts checks. check() prints FAIL lines always and PASS lines with TESTS_VERBOSE=1."""

    def __init__(self, name: str):
        self.name = name
        self.passed = 0
        self.failed: list[str] = []
        self.skipped = 0
        self.verbose = os.environ.get('TESTS_VERBOSE') == '1'
        self.t0 = time.monotonic()

    def check(self, label: str, cond, detail='') -> bool:
        if cond:
            self.passed += 1
            if self.verbose:
                print(f'  ok   {label}', flush=True)
        else:
            self.failed.append(label)
            text = detail if isinstance(detail, str) else repr(detail)
            print(f'  FAIL {label}' + (f'\n       {text[:1500]}' if text else ''), flush=True)
        return bool(cond)

    def skip(self, label: str, reason: str) -> None:
        self.skipped += 1
        print(f'  SKIP {label} ({reason})', flush=True)

    @staticmethod
    def section(title: str) -> None:
        print(f'== {title}', flush=True)

    def finish(self) -> int:
        took = time.monotonic() - self.t0
        print(f'\n{self.name}: {self.passed} passed, {len(self.failed)} failed, {self.skipped} skipped ({took:.1f}s)')
        for label in self.failed:
            print(f'  FAILED: {label}')
        print(f'RESULT: passed={self.passed} failed={len(self.failed)} skipped={self.skipped}', flush=True)
        return 1 if self.failed else 0


def skip_suite(reason: str) -> None:
    """The whole suite cannot run here: say why and exit with SKIP_EXIT."""
    print(f'SKIP: {reason}')
    print('RESULT: passed=0 failed=0 skipped=1', flush=True)
    sys.exit(SKIP_EXIT)


def docs_version(root: Path = TASKS) -> str:
    """The contract's docs_version of a tasks/ tree, computed independently of server.py."""
    digest = hashlib.sha256()
    for rel in DOCS_FILES:
        digest.update(rel.encode() + b'\0')
        path = Path(root) / rel
        if path.is_file():
            digest.update(path.read_bytes() + b'\0')
    return digest.hexdigest()[:12]


# ---------------------------------------------------------------- temp state

def tempdir(prefix: str) -> Path:
    path = Path(tempfile.mkdtemp(prefix=f'tasks-test-{prefix}-'))
    _TEMP_DIRS.append(path)
    return path


def scratch_home(tmp: Path) -> Path:
    """A temp HOME with its own CLAUDE_CONFIG_DIR, exported to this process and everything it runs."""
    home = tmp / 'home'
    (home / '.claude').mkdir(parents=True, exist_ok=True)
    os.environ['HOME'] = str(home)
    os.environ['CLAUDE_CONFIG_DIR'] = str(home / '.claude')
    return home


_SPOOL: list[Path] = []


def spool_dir() -> Path:
    """This process's scratch taskctl offline queue (TASKS_SPOOL_DIR in clean_env), made on first use."""
    if not _SPOOL:
        _SPOOL.append(tempdir('spool') / 'spool')
    return _SPOOL[0]


def clean_env(**extra) -> dict:
    """os.environ without anything that points taskctl or the server somewhere else. taskctl's offline
    queue goes to a temp dir (TASKS_SPOOL_DIR), never the user's cache; pass TASKS_SPOOL_DIR=None to
    drop it (then HOME, a temp one in every suite, decides)."""
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('TASKS_') and k not in ('CDPATH', 'XDG_CACHE_HOME')}
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    env['TASKS_SPOOL_DIR'] = str(spool_dir())
    for key, value in extra.items():
        if value is None:
            env.pop(key, None)
        else:
            env[key] = str(value)
    return env


def tasks_copy(dest: Path, without=()) -> Path:
    """A copy of tasks/ (never its data/) at dest, minus the named entries. Returns dest."""
    dest.mkdir(parents=True, exist_ok=True)
    for entry in ('server.py', 'taskctl', 'taskctl.py', 'skill', 'static', 'templates'):
        if entry in without:
            continue
        src = TASKS / entry
        if src.is_dir():
            shutil.copytree(src, dest / entry)
        else:
            shutil.copy2(src, dest / entry)
    return dest


def _cleanup() -> None:
    for server in list(_SERVERS):
        server.stop()
    for path in _TEMP_DIRS:
        reap(path)
        # A test may have left a file unreadable on purpose.
        for root, dirs, files in os.walk(path):
            for name in dirs + files:
                full = os.path.join(root, name)
                if not os.path.islink(full):
                    try:
                        os.chmod(full, 0o700)
                    except OSError:
                        pass
        shutil.rmtree(path, ignore_errors=True)


atexit.register(_cleanup)


def _terminate(signum, frame):
    raise SystemExit(128 + signum)


signal.signal(signal.SIGTERM, _terminate)


def reap(marker: Path) -> None:
    """Kill any process whose command line mentions marker (a temp dir): the safety net that stops
    a server or child a failed test left behind."""
    if not os.path.isdir('/proc'):
        return
    needle = str(marker).encode()
    victims = []
    for pid in os.listdir('/proc'):
        if not pid.isdigit() or int(pid) == os.getpid():
            continue
        try:
            with open(f'/proc/{pid}/cmdline', 'rb') as f:
                if needle in f.read():
                    victims.append(int(pid))
        except OSError:
            pass
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for pid in victims:
            try:
                os.kill(pid, sig)
            except OSError:
                pass
        if victims and sig == signal.SIGTERM:
            time.sleep(0.5)


# ---------------------------------------------------------------- network

def has_ipv6() -> bool:
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
            s.bind(('::1', 0))
        return True
    except OSError:
        return False


def free_port() -> int:
    """A TCP port free on IPv4 and IPv6 (bind to port 0 dual-stack), never the real board's."""
    for _ in range(50):
        if has_ipv6():
            s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
            s.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
            s.bind(('::', 0))
        else:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.bind(('0.0.0.0', 0))
        port = s.getsockname()[1]
        s.close()
        if port != REAL_PORT:
            return port
    raise RuntimeError('no free port')


def connects(addr: str, port: int, timeout: float = 2) -> bool:
    try:
        fam = socket.AF_INET6 if ':' in addr else socket.AF_INET
        with socket.socket(fam, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(socket.getaddrinfo(addr, port, fam, socket.SOCK_STREAM)[0][4])
        return True
    except OSError:
        return False


def link_local() -> tuple[str, str] | None:
    """(address, interface) of one IPv6 link-local address on this machine, or None."""
    try:
        with open('/proc/net/if_inet6') as f:
            rows = [line.split() for line in f]
    except OSError:
        return None
    for row in rows:
        hexaddr, scope, iface = row[0], row[3], row[5]
        if scope == '20' and hexaddr.startswith('fe80'):
            addr = ':'.join(hexaddr[i:i + 4] for i in range(0, 32, 4))
            addr = socket.inet_ntop(socket.AF_INET6, socket.inet_pton(socket.AF_INET6, addr))
            try:
                with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
                    s.bind(socket.getaddrinfo(f'{addr}%{iface}', 0, socket.AF_INET6, socket.SOCK_STREAM)[0][4])
                return addr, iface
            except OSError:
                continue
    return None


def call(port: int, method: str, path: str, body=None, headers=None, raw: bytes | None = None,
         host: str | None = None, addr: str = '127.0.0.1', timeout: float = 15, tls: bool = False):
    """One HTTP request with full control of Host and the body. Returns (status, headers, body):
    headers lower-cased, body parsed JSON for application/json, else text. tls=True: HTTPS, without
    verifying the (self-signed) certificate."""
    if tls:
        conn = http.client.HTTPSConnection(addr, port, timeout=timeout, context=ssl._create_unverified_context())
    else:
        conn = http.client.HTTPConnection(addr, port, timeout=timeout)
    hdrs = dict(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    conn.putrequest(method, path, skip_host=host is not None, skip_accept_encoding=True)
    if host is not None:
        conn.putheader('Host', host)
    for key, value in hdrs.items():
        conn.putheader(key, value)
    if data is not None and 'Transfer-Encoding' not in hdrs:
        conn.putheader('Content-Length', str(len(data)))
    conn.endheaders(data)
    resp = conn.getresponse()
    payload = resp.read()
    conn.close()
    ctype = resp.getheader('Content-Type') or ''
    parsed = (json.loads(payload) if ctype.startswith('application/json') and payload
              else payload.decode('utf-8', 'replace'))
    return resp.status, {k.lower(): v for k, v in resp.getheaders()}, parsed


def api(base: str, method: str, path: str, body=None, headers=None, raw: bool = False):
    """urllib (no proxies) against base. Returns (status, parsed JSON or None | raw bytes, headers)."""
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(base + path, data=data, method=method, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=10) as r:
            payload, code, hdrs = r.read(), r.status, dict(r.headers)
    except urllib.error.HTTPError as e:
        payload, code, hdrs = e.read(), e.code, dict(e.headers)
    if raw:
        return code, payload, hdrs
    return code, (json.loads(payload) if payload else None), hdrs


def self_signed_cert(tmp: Path):
    """(cert, key, encrypted key) PEM files for localhost / 127.0.0.1, made with openssl; None when
    openssl is not installed or cannot make them (the TLS checks are then skipped)."""
    openssl = shutil.which('openssl')
    if not openssl:
        return None
    cert, key, enc = tmp / 'cert.pem', tmp / 'key.pem', tmp / 'key-encrypted.pem'
    base = [openssl, 'req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-keyout', str(key), '-out', str(cert),
            '-days', '2', '-subj', '/CN=localhost']
    for argv in (base + ['-addext', 'subjectAltName=DNS:localhost,IP:127.0.0.1'], base):  # -addext: OpenSSL 1.1.1+
        if subprocess.run(argv, capture_output=True, timeout=60).returncode == 0:
            break
    else:
        return None
    made = subprocess.run([openssl, 'pkey', '-in', str(key), '-out', str(enc), '-aes-256-cbc', '-passout', 'pass:secret'],
                          capture_output=True, timeout=60)
    return cert, key, (enc if made.returncode == 0 else None)


def wait_until(fn, timeout: float, interval: float = 0.2):
    """Polls fn until it returns something truthy or timeout passes; returns the last value."""
    deadline = time.monotonic() + timeout
    while True:
        value = fn()
        if value or time.monotonic() >= deadline:
            return value
        time.sleep(interval)


# ---------------------------------------------------------------- scratch server

class ScratchServer:
    """tasks/server.py (or a copy, or a wrapper around it) on a free port with a temp db, pidfile and log.

        with ScratchServer(tmp, '--stale-after', '2') as srv:
            srv.call('GET', '/api/health')
    """

    def __init__(self, tmp: Path, *extra: str, script: Path = SERVER, port: int | None = None,
                 name: str = 'server', wrapper: Path | None = None, env: dict | None = None,
                 fresh_db: bool = True, own_config: bool = True):
        self.tmp = Path(tmp)
        self.extra = [str(x) for x in extra]
        self.script = Path(script)
        self.port = port or free_port()
        self.name = name
        self.wrapper = wrapper
        self.env = env
        self.db = self.tmp / f'{name}.db'
        self.pidfile = self.tmp / f'{name}.pid'
        self.log = self.tmp / f'{name}.log'
        # A settings file of its own (the default, <db dir>/settings.json, is shared by every server in tmp);
        # own_config=False leaves --config to the caller or the server's default.
        given = any(x == '--config' or x.startswith('--config=') for x in self.extra)
        self.config = self.tmp / f'{name}.settings.json' if own_config and not given else None
        self.proc: subprocess.Popen | None = None
        self.fresh_db = fresh_db

    @property
    def url(self) -> str:
        return f'http://127.0.0.1:{self.port}'

    def argv(self) -> list[str]:
        return ([sys.executable] + ([str(self.wrapper)] if self.wrapper else []) +
                [str(self.script), '--port', str(self.port), '--db', str(self.db), '--pidfile', str(self.pidfile),
                 *(['--config', str(self.config)] if self.config else []), *self.extra])

    def start(self, wait: bool = True) -> 'ScratchServer':
        if self.fresh_db:
            for suffix in ('', '-wal', '-shm'):
                Path(str(self.db) + suffix).unlink(missing_ok=True)
        self.log.write_text('')
        with open(self.log, 'a') as log:
            self.proc = subprocess.Popen(self.argv(), stdout=log, stderr=subprocess.STDOUT,
                                         env=self.env or clean_env())
        _SERVERS.append(self)
        if wait:
            # The server logs "listening on" after the bind; connections made after that queue until
            # serve_forever picks them up, so this works for every --host.
            ok = wait_until(lambda: self.proc.poll() is not None or 'tasks server listening on' in self.log_text(), 10, 0.05)
            if not ok or self.proc.poll() is not None:
                raise RuntimeError(f'{self.name} did not start (rc {self.proc.poll()}):\n{self.log_text()[-2000:]}')
        return self

    def log_text(self) -> str:
        try:
            return self.log.read_text(errors='replace')
        except OSError:
            return ''

    def call(self, *args, **kw):
        return call(self.port, *args, **kw)

    def events(self, since=0, limit=1000):
        """GET /api/events?since=N (since=None: the baseline)."""
        query = '' if since is None else f'?since={since}&limit={limit}'
        status, _, body = self.call('GET', '/api/events' + query)
        assert status == 200, (status, body)
        return body

    def api(self, method, path, body=None, **kw):
        return api(self.url, method, path, body, **kw)

    def stop(self):
        """SIGTERM, then SIGKILL after 10s. Returns the exit code (None if never started)."""
        if self in _SERVERS:
            _SERVERS.remove(self)
        if not self.proc:
            return None
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(5)
        return self.proc.returncode

    def __enter__(self):
        return self.start() if self.proc is None else self

    def __exit__(self, *exc):
        self.stop()


if __name__ == '__main__':
    if sys.argv[1:] == ['free-port']:
        print(free_port())
    elif sys.argv[1:] == ['link-local']:
        found = link_local()
        print('%s %s' % found if found else '')
    else:
        sys.exit('usage: harness.py free-port | link-local')
