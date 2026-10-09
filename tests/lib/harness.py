#!/usr/bin/env python3
"""Shared helpers for the Python test suites. Standard library only, Python 3.10+.

    from harness import Suite, ScratchServer, ...     (each suite puts tests/lib on sys.path)
    python3 tests/lib/harness.py free-port             (prints a free TCP port, for the shell suites)

Every suite runs against throwaway state: a scratch server on a free port (never the real board's
8765) with a temp --db, --pidfile and --config, a temp HOME / CLAUDE_CONFIG_DIR, and a temp taskctl
offline queue (TASKS_SPOOL_DIR), all removed at exit.

The live stream's helpers (see "streams" below): fake_ffmpeg_dir (tests/lib/fake_ffmpeg.py as the board's
ffmpeg), real_ffmpeg_dir, FFMPEG / FFPROBE (None when absent), make_ts / ts_from / ts_without_audio (MPEG-TS
sources made with ffmpeg), probe (ffprobe), split_boxes (the boxes of an MP4), stream_get (a media response
read in the background), FakeUpstream (a source like the real one), children_of / find_procs.
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
import struct
import subprocess
import sys
import tempfile
import threading
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
        self.lock = threading.Lock()  # a suite may run scenarios in threads

    def check(self, label: str, cond, detail='') -> bool:
        with self.lock:
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
        with self.lock:
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


# ---------------------------------------------------------------- streams (ffmpeg, a fake source, media readers)

FFMPEG = os.environ.get('FFMPEG') or shutil.which('ffmpeg')       # the real ones, for the checks that need them;
FFPROBE = os.environ.get('FFPROBE') or shutil.which('ffprobe')    # None when absent (those checks are then skipped)
TS_PACKET = 188


def install_fake_ffmpeg(directory: Path) -> Path:
    """directory/ffmpeg: tests/lib/fake_ffmpeg.py with an absolute interpreter on its first line (so a PATH of this
    directory alone runs it, where `#!/usr/bin/env python3` would not be found). Written under another name and
    renamed, so a board looking for ffmpeg never meets half a file."""
    body = (LIB / 'fake_ffmpeg.py').read_text().split('\n', 1)[1]
    directory = Path(directory)
    temp, target = directory / '.ffmpeg.new', directory / 'ffmpeg'
    temp.write_text(f'#!{sys.executable}\n{body}')
    temp.chmod(0o755)
    os.replace(temp, target)
    return target


def fake_ffmpeg_dir(tmp: Path, name: str = 'fakebin', install: bool = True) -> Path:
    """tmp/fakebin holding the fake ffmpeg: put it first on the board's PATH (clean_env(PATH=...)), and set
    FAKE_FFMPEG_LOG for the record of every run. The directory is under tmp, so reap() finds the fake by its command
    line. install=False makes it empty (install_fake_ffmpeg adds the fake later: ffmpeg turning up while the board runs)."""
    directory = Path(tmp) / name
    directory.mkdir(parents=True, exist_ok=True)
    if install:
        install_fake_ffmpeg(directory)
    return directory


def real_ffmpeg_dir(tmp: Path) -> Path | None:
    """tmp/realbin holding `ffmpeg`, a link to FFMPEG, for a board's PATH; None without ffmpeg. The link is under
    tmp, so reap() finds the real ffmpeg a failed test left behind by its command line."""
    if not FFMPEG:
        return None
    directory = Path(tmp) / 'realbin'
    directory.mkdir(parents=True, exist_ok=True)
    link = directory / 'ffmpeg'
    if not link.is_symlink():
        link.symlink_to(FFMPEG)
    return directory


_ENCODERS: list = []


def has_encoder(name: str) -> bool:
    """Whether FFMPEG can encode with name (libx264, say)."""
    if not FFMPEG:
        return False
    if not _ENCODERS:
        try:
            out = subprocess.run([FFMPEG, '-hide_banner', '-encoders'], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            out = ''
        _ENCODERS.append({line.split()[1] for line in out.splitlines() if line.startswith(' ') and len(line.split()) > 1})
    return name in _ENCODERS[0]


class TsData(bytes):
    """MPEG-TS bytes that know how long they play (.seconds): what FakeUpstream paces itself by."""
    seconds = 6.0


def _ts(data: bytes, seconds: float) -> TsData:
    out = TsData(data)
    out.seconds = seconds
    return out


_TS_CACHE: dict = {}


def make_ts(kind: str = 'video', seconds: float = 6, *, gop: int = 30, size: str = '320x180', rate: int = 30) -> TsData | None:
    """MPEG-TS made from ffmpeg's test sources: kind 'video' (H.264 baseline with a keyframe every gop frames, like
    today's real source), 'av' (that and AAC stereo at 48 kHz) or 'audio' (MP2). None without ffmpeg or libx264."""
    key = (kind, seconds, gop, size, rate)
    if key not in _TS_CACHE:
        _TS_CACHE[key] = _make_ts(*key)
    return _TS_CACHE[key]


def _make_ts(kind, seconds, gop, size, rate):
    video, audio = kind in ('video', 'av'), kind in ('av', 'audio')
    if not FFMPEG or (video and not has_encoder('libx264')):
        return None
    argv = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostdin']
    if video:
        argv += ['-f', 'lavfi', '-i', f'testsrc2=size={size}:rate={rate}']
    if audio:
        argv += ['-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000']
    argv += ['-t', str(seconds)]
    if video:
        argv += ['-c:v', 'libx264', '-profile:v', 'baseline', '-pix_fmt', 'yuv420p', '-g', str(gop), '-sc_threshold', '0']
    if audio:
        argv += (['-c:a', 'aac', '-b:a', '64k'] if video else ['-c:a', 'mp2', '-b:a', '128k']) + ['-ac', '2']
    try:
        done = subprocess.run(argv + ['-f', 'mpegts', 'pipe:1'], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    return _ts(done.stdout, float(seconds)) if done.returncode == 0 and done.stdout else None


def probe(data: bytes, timeout: float = 60) -> dict | None:
    """ffprobe's -show_streams -show_format of data (MPEG-TS, MP4...) as parsed JSON; None without ffprobe or when it
    cannot read the data."""
    if not FFPROBE:
        return None
    try:
        done = subprocess.run([FFPROBE, '-v', 'error', '-show_streams', '-show_format', '-of', 'json', '-i', 'pipe:0'],
                              input=bytes(data), capture_output=True, timeout=timeout)
        return json.loads(done.stdout) if done.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


def ts_from(data: bytes, seconds: float) -> TsData | None:
    """data from `seconds` into it (at the first video packet from then, on a packet boundary): a source joined
    mid-way, mid-GOP when the GOP is long. None without ffprobe."""
    if not FFPROBE:
        return None
    with tempfile.NamedTemporaryFile(suffix='.ts') as f:
        f.write(data)
        f.flush()
        try:
            done = subprocess.run([FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'packet=pos,pts_time', '-of', 'json',
                                   '-i', f.name], capture_output=True, timeout=60)
            packets = [(float(p['pts_time']), int(p['pos'])) for p in json.loads(done.stdout)['packets']]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError):
            return None
    if not packets:
        return None
    wanted = packets[0][0] + seconds
    cut = next((pos for pts, pos in packets if pts >= wanted), None)
    if cut is None:
        return None
    cut = cut // TS_PACKET * TS_PACKET
    return _ts(data[cut:], getattr(data, 'seconds', 6.0) * (len(data) - cut) / max(len(data), 1))


def ts_without_audio(data: bytes) -> TsData | None:
    """data with every audio packet removed and the program table left as it was: a source that announces a sound track
    and never sends any. None without ffprobe."""
    info = probe(data)
    if not info:
        return None
    pids = {int(s['id'], 16) for s in info['streams'] if s.get('codec_type') == 'audio' and s.get('id')}
    if not pids:
        return None
    kept = bytearray()
    for pos in range(0, len(data) - TS_PACKET + 1, TS_PACKET):
        if ((data[pos + 1] & 0x1f) << 8 | data[pos + 2]) not in pids:
            kept += data[pos:pos + TS_PACKET]
    return _ts(kept, getattr(data, 'seconds', 6.0))


def split_boxes(data: bytes, start: int = 0, end: int | None = None) -> list:
    """The complete top-level MP4 boxes of data as (type, start, end); stops at a truncated or impossible one. Understands
    64-bit sizes; a size of 0 means "to the end"."""
    end = len(data) if end is None else end
    out, pos = [], start
    while pos + 8 <= end:
        size, kind = struct.unpack_from('>I4s', data, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                break
            size, header = struct.unpack_from('>Q', data, pos + 8)[0], 16
        elif size == 0:
            size = end - pos
        if size < header or pos + size > end:
            break
        out.append((kind.decode('latin-1'), pos, pos + size))
        pos += size
    return out


class StreamReader:
    """One HTTP response read in the background, for bodies that do not end (the media route). Records when the headers,
    the first byte and the end came, and keeps the body so far.

        r = stream_get(srv.port, '/api/stream/media?id=...')
        r.wait_headers(5); r.status; r.headers           headers lower-cased
        r.wait_bytes(50000, 10); r.body; r.boxes()       what arrived (bytes; split_boxes of it)
        r.wait_closed(5); r.error                        the server ended it (error: what the socket said, if it broke)
        r.pause(); r.resume(); r.close()                 stop calling recv (the socket's buffers fill); hang up
    rcvbuf shrinks the receive buffer before connecting (a reader that cannot take much); paused starts without reading
    the body."""

    def __init__(self, port: int, path: str, *, tls: bool = False, addr: str = '127.0.0.1', method: str = 'GET', headers=None,
                 host: str | None = None, rcvbuf: int | None = None, paused: bool = False, timeout: float = 60):
        self.status: int | None = None
        self.headers: dict = {}
        self.error: BaseException | None = None
        self.t0 = time.monotonic()
        self.t_headers = self.t_first = self.t_closed = None
        self._data = bytearray()
        self._cond = threading.Condition()
        self._done = False
        self._sock: socket.socket | None = None
        self._go = threading.Event()
        if not paused:
            self._go.set()
        self._thread = threading.Thread(target=self._run, args=(port, path, tls, addr, method, headers or {}, host, rcvbuf, timeout), daemon=True)
        self._thread.start()

    def _run(self, port, path, tls, addr, method, headers, host, rcvbuf, timeout):
        sock = None
        try:
            sock = socket.socket(socket.AF_INET6 if ':' in addr else socket.AF_INET, socket.SOCK_STREAM)
            if rcvbuf:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, rcvbuf)
            sock.settimeout(timeout)
            sock.connect((addr, port))
            if tls:
                sock = ssl._create_unverified_context().wrap_socket(sock, server_hostname=addr)
            self._sock = sock
            lines = [f'{method} {path} HTTP/1.1', f'Host: {host or f"{addr}:{port}"}', 'Connection: close',
                     *(f'{k}: {v}' for k, v in headers.items())]
            sock.sendall(('\r\n'.join(lines) + '\r\n\r\n').encode())
            buf = b''
            while b'\r\n\r\n' not in buf:
                chunk = sock.recv(65536)
                if not chunk:
                    raise ConnectionError('closed before the headers were complete')
                buf += chunk
            head, _, rest = buf.partition(b'\r\n\r\n')
            status_line, *header_lines = head.decode('latin-1').split('\r\n')
            with self._cond:
                self.status = int(status_line.split()[1])
                self.headers = {k.strip().lower(): v.strip() for k, _, v in (line.partition(':') for line in header_lines)}
                self.t_headers = time.monotonic()
                self._cond.notify_all()
            if rest:
                self._add(rest)
            while method != 'HEAD':
                self._go.wait()
                chunk = sock.recv(65536)
                if not chunk:
                    break
                self._add(chunk)
        except (OSError, ValueError, IndexError) as exc:  # a timeout, a reset, a TLS error, a malformed reply
            self.error = exc
        finally:
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass
            with self._cond:
                self.t_closed = time.monotonic()
                self._done = True
                self._cond.notify_all()

    def _add(self, chunk: bytes) -> None:
        with self._cond:
            if self.t_first is None:
                self.t_first = time.monotonic()
            self._data += chunk
            self._cond.notify_all()

    def _wait(self, cond, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._cond:
            while not cond():
                left = deadline - time.monotonic()
                if left <= 0:
                    return False
                self._cond.wait(left)
        return True

    def wait_headers(self, timeout: float = 10) -> bool:
        return self._wait(lambda: self.status is not None or self._done, timeout) and self.status is not None

    def wait_bytes(self, n: int, timeout: float = 10) -> bool:
        return self._wait(lambda: len(self._data) >= n or self._done, timeout) and len(self._data) >= n

    def wait_closed(self, timeout: float = 10) -> bool:
        return self._wait(lambda: self._done, timeout)

    @property
    def body(self) -> bytes:
        with self._cond:
            return bytes(self._data)

    @property
    def closed(self) -> bool:
        return self._done

    def boxes(self) -> list:
        return split_boxes(self.body)

    def json(self, timeout: float = 10):
        """The body parsed as JSON (an error reply's), once it has all come; None if it is not JSON."""
        self.wait_closed(timeout)
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    def pause(self) -> None:
        self._go.clear()

    def resume(self) -> None:
        self._go.set()

    def close(self) -> None:
        """Hang up. The socket is only shut down here, which wakes the reader thread, and that thread closes it: closing
        a socket another thread is blocked on can give its number to a new socket, which the blocked thread then reads."""
        self._go.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.wait_closed(5)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def stream_get(port: int, path: str, tls: bool = False, **kw) -> StreamReader:
    """Start reading GET path from the board in the background; see StreamReader for what the result offers."""
    return StreamReader(port, path, tls=tls, **kw)


class FakeUpstream:
    """A stream source like the real one: on 127.0.0.1, HTTP/1.0, `video/mp2t`, no Content-Length, no CORS headers,
    the data (MPEG-TS) paced to real time and looping.

        up = FakeUpstream(make_ts('av'))          up.url, up.connections (all so far), up.active (open now), up.requests
        up.mode = 'blackhole'; up.close()

    mode: serve; refuse (nothing listens there: connection refused, until serve() is called); blackhole (accepts, never
    answers); 404; 500; html (200 text/html); redirect (302 to `location`, file:///etc/hostname); close (sends
    `close_after` bytes, then hangs up). data is TsData from make_ts (it knows its duration), else seconds says how long
    it plays. marker (a path, tmp) goes into the URL's path so that reap() finds an ffmpeg left reading it."""

    def __init__(self, data: bytes = b'', mode: str = 'serve', *, seconds: float | None = None, loop: bool = True,
                 location: str = 'file:///etc/hostname', close_after: int = 4096, marker: str | Path = ''):
        self.data = bytes(data)
        self.seconds = float(seconds or getattr(data, 'seconds', 6.0))
        self.mode, self.loop, self.location, self.close_after = mode, loop, location, close_after
        self.connections = self.active = 0
        self.requests: list = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._socks: list = []
        self._listener: socket.socket | None = None
        self._acceptor: threading.Thread | None = None
        self.port = 0
        if mode == 'refuse':   # reserve a port and leave it unbound: connecting is refused
            with socket.socket() as s:
                s.bind(('127.0.0.1', 0))
                self.port = s.getsockname()[1]
        else:
            self._listen()
        self.url = f'http://127.0.0.1:{self.port}{str(marker).rstrip("/")}/stream'

    def _listen(self) -> None:
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', self.port))
        sock.listen(64)
        sock.settimeout(0.2)
        self.port = sock.getsockname()[1]
        self._listener = sock
        self._acceptor = threading.Thread(target=self._accept, args=(sock,), daemon=True)
        self._acceptor.start()

    def serve(self) -> None:
        """Start serving on the port that was refusing (the source coming back)."""
        self.mode = 'serve'
        if self._listener is None:
            self._listen()

    def _accept(self, sock: socket.socket) -> None:
        # The thread that accepts is the one that closes the listening socket (see close()).
        try:
            while not self._stop.is_set():
                try:
                    conn, _ = sock.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                with self._lock:
                    self.connections += 1
                    self.active += 1
                    self._socks.append(conn)
                threading.Thread(target=self._handle, args=(conn,), daemon=True).start()
        finally:
            sock.close()

    def _handle(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(10)
            head = b''
            while b'\r\n\r\n' not in head:
                chunk = conn.recv(4096)
                if not chunk:
                    return
                head += chunk
            with self._lock:
                self.requests.append(head.decode('latin-1').split('\r\n\r\n')[0])
            mode = self.mode
            if mode == 'blackhole':
                self._stop.wait()
            elif mode in ('404', '500'):
                reason = 'Not Found' if mode == '404' else 'Internal Server Error'
                conn.sendall(f'HTTP/1.0 {mode} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'.encode())
            elif mode == 'html':
                body = b'<html><body>not a video stream</body></html>'
                conn.sendall(b'HTTP/1.0 200 OK\r\nContent-Type: text/html\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s' % (len(body), body))
            elif mode == 'redirect':
                conn.sendall(f'HTTP/1.0 302 Found\r\nLocation: {self.location}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'.encode())
            else:
                conn.sendall(b'HTTP/1.0 200 OK\r\nContent-Type: video/mp2t\r\nConnection: close\r\n\r\n')
                if mode == 'close':
                    conn.sendall((self.data * (self.close_after // max(len(self.data), 1) + 1))[:self.close_after])
                else:
                    self._pace(conn)
        except OSError:
            pass
        finally:
            try:
                conn.close()
            except OSError:
                pass
            with self._lock:
                self.active -= 1

    def _pace(self, conn: socket.socket) -> None:
        """The data at the speed it was made for, from its start, again and again."""
        if not self.data:
            self._stop.wait()
            return
        rate = len(self.data) / self.seconds
        chunk, pos, sent, t0 = TS_PACKET * 7, 0, 0, time.monotonic()
        while not self._stop.is_set():
            piece = self.data[pos:pos + chunk]
            conn.sendall(piece)
            pos += len(piece)
            sent += len(piece)
            if pos >= len(self.data):
                if not self.loop:
                    return
                pos = 0
            self._stop.wait(max(0.0, t0 + sent / rate - time.monotonic()))

    def close(self) -> None:
        """Stop serving: connections are shut down (each handler thread then closes its own socket, and the accepting
        thread the listener, so no socket is closed under a thread that is using it)."""
        self._stop.set()
        with self._lock:
            socks, self._socks = self._socks, []
        for conn in socks:
            try:
                conn.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        acceptor = getattr(self, '_acceptor', None)
        if acceptor is not None:
            acceptor.join(2)
        self._listener = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def children_of(pid: int, alive: bool = True) -> list:
    """The pids of pid's children, from /proc (zombies, which are only waiting to be reaped, left out unless alive=False)."""
    out = []
    for name in os.listdir('/proc'):
        if not name.isdigit():
            continue
        try:
            with open(f'/proc/{name}/stat') as f:
                stat = f.read()
        except OSError:
            continue
        state, ppid = stat.rsplit(')', 1)[1].split()[:2]  # the name may hold spaces and parentheses
        if int(ppid) == pid and not (alive and state == 'Z'):
            out.append(int(name))
    return sorted(out)


def find_procs(needle: str) -> list:
    """The pids of the processes (other than this one) whose command line mentions needle: an ffmpeg left reading a
    source after its board was killed, say."""
    out, want = [], needle.encode()
    for name in os.listdir('/proc'):
        if name.isdigit() and int(name) != os.getpid():
            try:
                with open(f'/proc/{name}/cmdline', 'rb') as f:
                    if want in f.read():
                        out.append(int(name))
            except OSError:
                pass
    return sorted(out)


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
        self.stream_file = self.db.with_suffix('.stream.json')   # where the board keeps its open stream (0600)
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
            self.stream_file.unlink(missing_ok=True)   # a stream a previous run left open must not come back
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
