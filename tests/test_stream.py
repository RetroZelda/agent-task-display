#!/usr/bin/env python3
"""The live stream's server side: POST /api/stream plays a network stream on every open board page, through one
ffmpeg that remuxes it to fragmented MP4 (GET /api/stream/media). Four phases:

  units    server.py's helpers on their own: the URL rules, redaction, the ffmpeg command line per scheme and
           probe level, ffmpeg's error text against stderr it really wrote, and the MP4 parsing (box sizes,
           every trun/tfhd/trex path for "is this a keyframe fragment", the splitter fed a byte at a time)
  api      the routes with the fake ffmpeg: shapes and key order, validation, replace/keep/close, the Origin
           guard, 405/413, headers, no password anywhere, 8 racing POSTs
  fake     the media route and the supervisor against tests/lib/fake_ffmpeg.py (it plays what a URL asks
           for): body order, late joiners, audio-lead, HEAD, the 16-viewer cap, a reader that stops reading,
           replace/close leaving no process, the backoff and its reset, stalls, stubborn processes, garbage,
           the probe ladder, audio that is silent or stops, no ffmpeg installed yet, persistence across a
           restart (mode 0600, a corrupt file), SIGTERM with viewers attached, SIGKILL of the board, polls
           with 16 viewers
  real     the same against the real ffmpeg (skipped without ffmpeg and libx264), over loopback only: a
           paced MPEG-TS source (video, audio+video, audio, a long GOP joined mid-way, announced audio that
           never comes), refusals and a black hole, a redirect to file:, five viewers on one upstream
           connection, rtmp/tcp/srt/rtsp through ffmpeg's own listeners, the HTTPS listener, orphans after a
           SIGKILL

Slow scenarios (a watchdog that needs 15 s of silence, say) run side by side in threads, each on a board of its own.
"""
from __future__ import annotations

import json
import os
import re
import signal
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the checkout, even when run without run_all.sh
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / 'lib'))
sys.path.insert(0, str(HERE.parent / 'tasks'))
import fake_ffmpeg as fk  # noqa: E402
import server  # noqa: E402
from harness import (FFMPEG, FFPROBE, FakeUpstream, ScratchServer, Suite, children_of, clean_env, docs_version, fake_ffmpeg_dir,  # noqa: E402
                     find_procs, free_port, has_encoder, install_fake_ffmpeg, make_ts, probe, real_ffmpeg_dir,
                     scratch_home, self_signed_cert, split_boxes, stream_get, tempdir, ts_from, ts_without_audio,
                     wait_until)

S = Suite('stream')
check = S.check
TMP = tempdir('stream')
scratch_home(TMP)
PATH = os.environ.get('PATH', '')
EVIL = {'Origin': 'http://evil.example'}
ID_RE = re.compile(r'[0-9a-f]{12}')
STREAM_KEYS = ['id', 'url', 'opened_at', 'state', 'since', 'error', 'video', 'audio', 'width', 'height', 'viewers', 'media']
FAKE = 'http://fake.invalid'   # fake_ffmpeg.py reads its mode and parameters from such a URL (the host is never resolved)
WHITELIST = 'http,https,tls,tcp,udp,rtp,rtsp,rtsps,rtmp,rtmps,srt,crypto'


# ---------------------------------------------------------------- helpers

def alive(pid: int) -> bool:
    try:
        with open(f'/proc/{pid}/stat') as f:
            return f.read().rsplit(')', 1)[1].split()[0] != 'Z'
    except OSError:
        return False


class Board(ScratchServer):
    """A scratch board whose ffmpeg is the fake ('fake'), nothing ('none': a PATH with no ffmpeg on it, and no /usr/bin)
    or the real one ('real'). The fake logs every run to <name>.ffmpeg.log."""

    def __init__(self, name: str, *extra, ffmpeg: str = 'fake', version: str | None = None, env: dict | None = None, **kw):
        self.ffmpeg = ffmpeg
        self.fake_log = TMP / f'{name}.ffmpeg.log'
        if ffmpeg == 'real':
            self.bin = real_ffmpeg_dir(TMP)
            path = f'{self.bin}{os.pathsep}{PATH}'
        else:
            self.bin = fake_ffmpeg_dir(TMP, f'{name}-bin', install=ffmpeg == 'fake')
            path = f'{self.bin}{os.pathsep}{PATH}' if ffmpeg == 'fake' else str(self.bin)
        super().__init__(TMP, *extra, name=name, env=clean_env(PATH=path, FAKE_FFMPEG_LOG=self.fake_log, FAKE_FFMPEG_VERSION=version, **(env or {})),
                         **kw)

    def stream(self):
        """The open stream's object from GET /api/stream, or None."""
        status, _, body = self.call('GET', '/api/stream')
        return body['stream'] if status == 200 else None

    def post(self, url: str, **kw):
        return self.call('POST', '/api/stream', {'url': url}, **kw)

    def wait_stream(self, pred, timeout: float = 10, interval: float = 0.1):
        """The stream object once pred(object) holds, else None."""
        def look():
            stream = self.stream()
            return stream if stream is not None and pred(stream) else None
        return wait_until(look, timeout, interval)

    def go_live(self, url: str, timeout: float = 10):
        status, _, body = self.post(url)
        check(f'{url}: opened', status in (200, 201), (status, body))
        return self.wait_stream(lambda s: s['state'] == 'live', timeout)

    def spawns(self) -> list:
        """The fake's runs so far, from its log: dicts with pid, argv, env, t, url, mode."""
        out = []
        try:
            for line in self.fake_log.read_text().splitlines():
                try:
                    out.append(json.loads(line))
                except ValueError:
                    pass
        except OSError:
            pass
        return out

    def kids(self) -> list:
        return children_of(self.proc.pid) if self.proc else []

    def reader(self, **kw):
        """A background reader of the open stream's media (see harness.StreamReader)."""
        return stream_get(self.port, self.stream()['media'], **kw)


def analyze_of(spawn: dict) -> int:
    argv = spawn['argv']
    return int(argv[argv.index('-analyzeduration') + 1])


def gaps(spawns: list) -> list:
    return [round(b['t'] - a['t'], 2) for a, b in zip(spawns, spawns[1:])]


def no_traceback(board: Board, label: str = '') -> None:
    text = board.log_text()
    check(f'{label or board.name}: no traceback in the board log', 'Traceback' not in text and 'Exception in thread' not in text, text[-1500:])


# ---- an MP4 reader of our own (the oracle for the server's, written from the standard rather than from server.py)

def kids(data: bytes, start: int, end: int):
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from('>I4s', data, pos)
        header = 8
        if size == 1:
            size, header = struct.unpack_from('>Q', data, pos + 8)[0], 16
        elif size == 0:
            size = end - pos
        yield kind.decode('latin-1'), pos + header, pos + size
        pos += size


def trex_flags(moov: bytes) -> dict:
    out = {}
    for kind, a, b in kids(moov, 8, len(moov)):
        if kind == 'mvex':
            for sub, c, d in kids(moov, a, b):
                if sub == 'trex':
                    track, _, _, _, flags = struct.unpack_from('>5I', moov, c + 4)
                    out[track] = flags
    return out


def moof_sync(moof: bytes, trex: dict) -> dict:
    """{track id: whether its first sample is a sync sample} of a moof, by the standard's rules."""
    out = {}
    for kind, a, b in kids(moof, 8, len(moof)):
        if kind != 'traf':
            continue
        default, track, first, count, trun_flags = None, None, None, 0, 0
        for sub, c, d in kids(moof, a, b):
            if sub == 'tfhd':
                bits = struct.unpack_from('>I', moof, c)[0] & 0xffffff
                track = struct.unpack_from('>I', moof, c + 4)[0]
                pos = c + 8
                for bit, size in ((0x1, 8), (0x2, 4), (0x8, 4), (0x10, 4)):
                    pos += size if bits & bit else 0
                default = struct.unpack_from('>I', moof, pos)[0] if bits & 0x20 else None
            elif sub == 'trun':
                trun_flags = struct.unpack_from('>I', moof, c)[0] & 0xffffff
                count = struct.unpack_from('>I', moof, c + 4)[0]
                pos = c + 8 + (4 if trun_flags & 1 else 0)
                if trun_flags & 4:
                    first = struct.unpack_from('>I', moof, pos)[0]
                elif trun_flags & 0x400:
                    first = struct.unpack_from('>I', moof, pos + (4 if trun_flags & 0x100 else 0) + (4 if trun_flags & 0x200 else 0))[0]
        if first is None:
            first = default if default is not None else trex.get(track, 0)
        out[track] = count > 0 and not first & 0x10000
    return out


def parse_media(body: bytes) -> dict:
    """The boxes of a media body: {'types': [...], 'init': ftyp+moov bytes, 'frags': [{'seq', 'sync': {track: bool}, 'tracks',
    'fake': (K, marker, pid, seq) | None, 'bytes'}]}. Stops at an incomplete box."""
    boxes = split_boxes(body)
    out = {'types': [b[0] for b in boxes], 'init': b'', 'frags': []}
    trex, pending = {}, None
    for kind, a, b in boxes:
        if kind == 'moov':
            trex = trex_flags(body[a:b])
        if kind in ('ftyp', 'moov'):
            out['init'] += body[a:b]
        elif kind == 'moof':
            pending = (a, b)
        elif kind == 'mdat' and pending:
            moof = body[pending[0]:pending[1]]
            sync = moof_sync(moof, trex)
            seq = struct.unpack_from('>I', moof, 8 + 8 + 4)[0]
            payload = body[a + 8:a + 8 + 18]
            fake = None
            if payload[:4] == b'FAKE':
                fake = (payload[4:5] == b'K', payload[5:6].decode(), *struct.unpack('>II', payload[6:14]))
            out['frags'].append({'seq': seq, 'sync': sync, 'tracks': list(sync), 'fake': fake, 'bytes': body[pending[0]:b]})
            pending = None
    return out


# ---------------------------------------------------------------- units: URLs, redaction, argv, error text

STDERR_NOISE = ['[h264 @ 0x55ae9ed64640] non-existing PPS 0 referenced', '    Last message repeated 1 times',
                '[h264 @ 0x55ae9ed64640] no frame!'] * 15
# What ffmpeg 8 really wrote (loopback runs of the board's command line), the cause at the end or the start.
STDERR = {
    'refused': ['[tcp @ 0x55dd56408e80] Connection to tcp://127.0.0.1:1 failed: Connection refused',
                '[in#0 @ 0x55dd56405700] Error opening input: Connection refused',
                'Error opening input file http://user:secret@127.0.0.1:1/x.', 'Error opening input files: Connection refused'],
    'dns': ['[tcp @ 0x564c84e02e00] Failed to resolve hostname no-such-host.invalid: Name or service not known',
            '[in#0 @ 0x564c84dff700] Error opening input: Input/output error',
            'Error opening input file http://no-such-host.invalid:8554/.', 'Error opening input files: Input/output error'],
    '404': ['[in#0 @ 0x5577d92e0700] Error opening input: Server returned 404 Not Found',
            'Error opening input file http://127.0.0.1:41693/404.', 'Error opening input files: Server returned 404 Not Found'],
    'invalid': ['[in#0 @ 0x560d09436700] Error opening input: Invalid data found when processing input',
                'Error opening input file http://127.0.0.1:41693/garbage.', 'Error opening input files: Invalid data found when processing input'],
    'whitelist': [f"[http @ 0x55e84a471040] Protocol 'file' not on whitelist '{WHITELIST}'!",
                  '[in#0 @ 0x55e84a470700] Error opening input: Invalid argument',
                  'Error opening input file http://127.0.0.1:41693/redir.', 'Error opening input files: Invalid argument'],
    'timeout': ['[http @ 0x565199d33040] Error reading HTTP response: Connection timed out',
                '[in#0 @ 0x565199d32700] Error opening input: Connection timed out',
                'Error opening input file http://127.0.0.1:41693/hang.', 'Error opening input files: Connection timed out'],
    'option': ['Option rw_timeout not found.', 'Error opening input file testsrc2=size=64x64:rate=10.',
               'Error opening input files: Option not found'],
    'probe': ['[mp4 @ 0x55ae9edca580] dimensions not set',
              '[out#0/mp4 @ 0x55ae9edad800] Could not write header (incorrect codec parameters ?): Invalid argument',
              '[af#0:1 @ 0x55ae9ed65c00] Error sending frames to consumers: Invalid argument',
              '[af#0:1 @ 0x55ae9ed65c00] Task finished with error code: -22 (Invalid argument)',
              '[out#0/mp4 @ 0x55ae9edad800] Nothing was written into output file, because at least one of its streams received no packets.'],
}


def attempt(fn, *args):
    """(result, None) or (None, the ApiError)."""
    try:
        return fn(*args), None
    except server.ApiError as exc:
        return None, exc


def phase_units_urls():
    S.section('clean_stream_url: what may be opened')
    accept = {
        'http://dell-cachyos:8554/': 'http://dell-cachyos:8554/',
        # The scheme is lower-cased (ffmpeg is case-sensitive about it), nothing else.
        'HTTP://Host:8554/Path?Q=1': 'http://Host:8554/Path?Q=1',
        '  rtsp://cam.lan/stream1  ': 'rtsp://cam.lan/stream1',             # stripped
        'RtSpS://cam.lan:322/x': 'rtsps://cam.lan:322/x',
        'https://[::1]:8554/': 'https://[::1]:8554/',
        'http://[fe80::1]/x': 'http://[fe80::1]/x',
        'udp://@239.0.0.1:1234': 'udp://@239.0.0.1:1234',                   # the multicast form: an empty userinfo
        'udp://239.0.0.1:1234?pkt_size=1316': 'udp://239.0.0.1:1234?pkt_size=1316',
        'srt://h:9000?mode=caller&latency=200': 'srt://h:9000?mode=caller&latency=200',
        'rtmp://h/app/key': 'rtmp://h/app/key', 'rtmps://h:443/app/key': 'rtmps://h:443/app/key',
        'tcp://127.0.0.1:9?listen=1': 'tcp://127.0.0.1:9?listen=1',
        'http://user:p%40ss@host/x#frag': 'http://user:p%40ss@host/x#frag',
        'http://h/' + 'a' * (server.STREAM_URL_MAX - len('http://h/')): 'http://h/' + 'a' * (server.STREAM_URL_MAX - len('http://h/')),
        'http://h:65535/': 'http://h:65535/', 'http://h:1/': 'http://h:1/',
        'http://h/%20space%20encoded': 'http://h/%20space%20encoded',
        'http://h/ünïcode': 'http://h/ünïcode',
    }
    for given, want in accept.items():
        got, exc = attempt(server.clean_stream_url, given)
        check(f'accepted: {given[:60]!r}', got == want and exc is None, (got, exc and exc.payload))
    reject = [None, 5, 5.5, True, [], ['http://h/'], {}, {'url': 'http://h/'}, '', '   ', '\t\n',
              'ftp://h/x', 'file:///etc/passwd', 'FILE:///etc/passwd', 'javascript:alert(1)', 'data:text/plain,hi', 'concat:a|b',
              'pipe:0', 'fd:1', 'subfile,,start,0,end,0,,:/etc/passwd', 'cache:http://h/', 'async:http://h/', 'httpproxy://h/', 'gopher://h/',
              'http', 'http:', 'http:/h', 'host:8554', 'h:8554/x', '//h/x', '/etc/passwd', 'localhost',
              'http://', 'http:///x', 'http://:80/', 'udp://:1234', 'http://@/',
              'http://h:0/', 'http://h:99999/', 'http://h:65536/', 'http://h:abc/', 'http://h:-1/', 'http://[::1/', 'http://::1/',
              'http://h/a b', 'http://h/a\tb', 'http://h/a\nb', 'http://h/a\x00b', 'http://h/a\x1fb', 'http://h/a\x7fb', 'http://h/a\x85b',
              'http://h/a b', 'http://h/a\xa0b', ' http://h/a b',
              'http://h/' + 'a' * (server.STREAM_URL_MAX - len('http://h/') + 1)]
    for given in reject:
        got, exc = attempt(server.clean_stream_url, given)
        check(f'rejected: {str(given)[:50]!r}', got is None and exc is not None and exc.code == 400 and exc.payload.get('field') == 'url'
              and isinstance(exc.payload.get('error'), str) and exc.payload['error'], (got, exc and exc.payload))
    # An unusable value never reaches ffmpeg as an option: a leading dash is not a scheme.
    got, exc = attempt(server.clean_stream_url, '-i http://h/')
    check('rejected: an ffmpeg option', exc is not None and exc.code == 400, got)


def phase_units_redaction():
    S.section('redact_url / redact_text: a password is shown to no one')
    cases = {
        'http://user:secret@host:80/p?q=1': 'http://***@host:80/p?q=1',
        'http://user@host/': 'http://***@host/',
        'http://:pw@host/': 'http://***@host/',
        'rtsp://admin:12345@[::1]:554/Streaming/Channels/101': 'rtsp://***@[::1]:554/Streaming/Channels/101',
        'http://user:p@ss@host/x': 'http://***@host/x',                    # an @ in the password: all of it goes
        'http://user:p%40ss@host/x': 'http://***@host/x',
        'udp://@239.0.0.1:1234': 'udp://@239.0.0.1:1234',                  # no userinfo: untouched
        'http://host/a@b': 'http://host/a@b',
        'http://host/?token=a@b': 'http://host/?token=a@b',
        'http://host/': 'http://host/',
        'srt://h:9000?passphrase=secret': 'srt://h:9000?passphrase=secret',  # a query is shown: documented, not redacted
    }
    for given, want in cases.items():
        check(f'redact_url({given!r})', server.redact_url(given) == want, server.redact_url(given))
    check('redact_url on a URL urlsplit rejects still hides the userinfo', 'secret' not in server.redact_url('http://user:secret@[::1/x'),
          server.redact_url('http://user:secret@[::1/x'))
    url = 'http://user:secret@127.0.0.1:1/x'
    text = f'Error opening input file {url}.'
    check('redact_text: the stream URL as ffmpeg echoes it', server.redact_text(text, url) == 'Error opening input file http://***@127.0.0.1:1/x.',
          server.redact_text(text, url))
    check('redact_text: another URL with credentials (a redirect)', server.redact_text('redirected to https://other:pw@h2/y now', 'http://x/') ==
          'redirected to https://***@h2/y now', server.redact_text('redirected to https://other:pw@h2/y now', 'http://x/'))
    got = server.redact_text('see http://user:p@ss@host/x and more', 'http://elsewhere/')
    check('redact_text: user:p@ss@host leaks no part of the password', got == 'see http://***@host/x and more'
          and 'ss' not in got.replace('host', ''), got)
    check('redact_text: a multicast URL is not damaged',
          server.redact_text('udp://@239.0.0.1:1234 failed', 'udp://@239.0.0.1:1234') == 'udp://@239.0.0.1:1234 failed')
    check('redact_text: text without credentials is unchanged', server.redact_text('Connection refused', url) == 'Connection refused')
    check('redact_text: the URL twice', server.redact_text(f'{url} {url}', url) == 'http://***@127.0.0.1:1/x http://***@127.0.0.1:1/x')


def option_value(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def phase_units_argv():
    S.section('ffmpeg_argv: one command line per scheme and probe level')
    exe = '/x/ffmpeg'
    argv = server.ffmpeg_argv(exe, 'http://user:secret@h:8554/a?b=1', 0, True, 7)
    check('argv: a list of strings, the executable first, the URL one item (no shell)', isinstance(argv, list)
          and all(isinstance(a, str) for a in argv)
          and argv[0] == exe and argv.count('http://user:secret@h:8554/a?b=1') == 1
          and argv[argv.index('-i') + 1] == 'http://user:secret@h:8554/a?b=1', argv)
    check('argv: quiet, no stdin, no stats',
          argv[1:8] == ['-hide_banner', '-nostdin', '-nostats', '-loglevel', 'error', '-protocol_whitelist', server.STREAM_PROTOCOLS], argv[:9])
    allowed = option_value(argv, '-protocol_whitelist').split(',')
    check('argv: the whitelist is the network protocols',
          set(allowed) == {'http', 'https', 'tls', 'tcp', 'udp', 'rtp', 'rtsp', 'rtsps', 'rtmp', 'rtmps', 'srt', 'crypto'}, allowed)
    for banned in ('file', 'pipe', 'fd', 'concat', 'concatf', 'subfile', 'data', 'cache', 'async', 'httpproxy', 'unix', 'ftp', 'sftp', 'gopher',
                   'bluray', 'ipfs'):
        check(f'argv: {banned} is not whitelisted', banned not in allowed, allowed)
    check('argv: the whitelist is an input option (before -i)', argv.index('-protocol_whitelist') < argv.index('-i'))
    check('argv: video copied, audio to AAC stereo at 128k, a short interleave delta', option_value(argv, '-c:v') == 'copy'
          and option_value(argv, '-c:a') == 'aac'
          and option_value(argv, '-b:a') == '128k' and option_value(argv, '-ac') == '2' and option_value(argv, '-max_interleave_delta') == '1000000',
          argv)
    check('argv: maps one video and one optional audio stream', [argv[i + 1] for i, a in enumerate(argv) if a == '-map'] == ['0:v:0?', '0:a:0?'],
          argv)
    check('argv: fragmented MP4 in 200 ms pieces on stdout', option_value(argv, '-f') == 'mp4'
          and option_value(argv, '-movflags') == '+frag_keyframe+empty_moov+default_base_moof'
          and option_value(argv, '-frag_duration') == '200000' and argv[-1] == 'pipe:1', argv)
    check('argv: no -y, no -re, no -stream_loop', not {'-y', '-re', '-stream_loop'} & set(argv), argv)
    check('argv: -nobuffer', option_value(argv, '-fflags') == '+nobuffer')

    micros = str(server.STREAM_IO_TIMEOUT * 1000000)
    for scheme in ('http', 'https', 'rtmp', 'rtmps', 'srt', 'udp', 'tcp'):
        a = server.ffmpeg_argv(exe, f'{scheme}://h:1/x', 0, True, 7)
        check(f'{scheme}: -rw_timeout {micros}, and no rtsp or listen options', option_value(a, '-rw_timeout') == micros
              and '-timeout' not in a and '-stimeout' not in a and '-rtsp_transport' not in a and '-listen' not in a, a)
    for scheme in ('rtsp', 'rtsps', 'RTSP'):
        for major, flag in ((7, '-timeout'), (8, '-timeout'), (5, '-timeout'), (None, '-timeout'), (4, '-stimeout'), (3, '-stimeout')):
            a = server.ffmpeg_argv(exe, f'{scheme}://h:554/x', 0, True, major)
            check(f'{scheme} on ffmpeg {major}: -rtsp_transport tcp {flag} {micros}, and no -rw_timeout (rtsp is a demuxer: "Option not found")',
                  option_value(a, flag) == micros and option_value(a, '-rtsp_transport') == 'tcp' and '-rw_timeout' not in a
                  and (flag == '-stimeout' or '-stimeout' not in a) and (flag == '-timeout' or '-timeout' not in a), a)
    a = server.ffmpeg_argv(exe, 'rtmp://h/app/key', 0, True, 4)
    check('rtmp on ffmpeg 4: still no -timeout (for rtmp it makes ffmpeg listen)', '-timeout' not in a and '-stimeout' not in a, a)

    S.section('the probe ladder and the audio-less command line')
    ladder = [(1000000, 500000), (5000000, 5000000), (15000000, 20000000)]
    for level, (analyze, size) in enumerate(ladder):
        a = server.ffmpeg_argv(exe, 'http://h/', level, True, 7)
        check(f'level {level}: -analyzeduration {analyze} -probesize {size}, before -i', option_value(a, '-analyzeduration') == str(analyze)
              and option_value(a, '-probesize') == str(size) and a.index('-analyzeduration') < a.index('-i'), a)
    check('the ladder is the constant the server says', list(server.STREAM_PROBES) == ladder, server.STREAM_PROBES)
    for level, want in ((-1, 0), (3, 2), (99, 2)):
        a = server.ffmpeg_argv(exe, 'http://h/', level, True, 7)
        check(f'level {level} is clamped to {want}', option_value(a, '-analyzeduration') == str(ladder[want][0]), a)
    a = server.ffmpeg_argv(exe, 'http://h/', 0, False, 7)
    check('audio=False: -an, no audio mapping, no AAC options', '-an' in a and '-map' in a
          and [a[i + 1] for i, x in enumerate(a) if x == '-map'] == ['0:v:0?']
          and '-c:a' not in a and '-b:a' not in a and '-ac' not in a, a)
    a = server.ffmpeg_argv(exe, 'http://h/', 0, True, 7)
    check('audio=True: no -an', '-an' not in a, a)
    same = server.ffmpeg_argv(exe, 'http://h/', 0, True, 7)
    check('the command line is deterministic', same == server.ffmpeg_argv(exe, 'http://h/', 0, True, 7))


def phase_units_errors():
    S.section('ffmpeg_error: why a run failed, from stderr it really wrote')
    url = 'http://user:secret@127.0.0.1:1/x'
    want = {'refused': 'Connection refused', 'dns': 'Name or service not known', '404': 'Server returned 404 Not Found',
            'invalid': 'Invalid data found when processing input', 'whitelist': 'not on whitelist', 'timeout': 'Connection timed out',
            'option': 'Option rw_timeout not found', 'probe': 'dimensions not set'}
    for name, needle in want.items():
        for label, lines in ((name, STDERR[name]), (name + ' behind decoder noise', STDERR_NOISE + STDERR[name]),
                             (name + ' with noise after it', STDERR[name] + STDERR_NOISE)):
            got = server.ffmpeg_error(lines, url, 1)
            check(f'{label}: says {needle!r}, one clean line', isinstance(got, str) and needle in got and '\n' not in got and len(got) <= 200
                  and 'secret' not in got and not re.search(r'@ 0x[0-9a-f]+\]', got) and 'non-existing PPS' not in got and 'no frame' not in got, got)
    check('refused: the cause is the connection line, not the URL echo', server.ffmpeg_error(STDERR['refused'], url, 145) ==
          'Connection to tcp://127.0.0.1:1 failed: Connection refused', server.ffmpeg_error(STDERR['refused'], url, 145))
    check('only decoder noise (the real source, then a clean end): nothing to show', server.ffmpeg_error(STDERR_NOISE, url, 0) is None
          and server.ffmpeg_error(STDERR_NOISE, url, None) is None, server.ffmpeg_error(STDERR_NOISE, url, None))
    check('a clean end (status 0) has no error text, whatever stderr held', server.ffmpeg_error(STDERR['refused'], url, 0) is None)
    check('no lines: nothing', server.ffmpeg_error([], url, 1) is None and server.ffmpeg_error([''], url, 1) is None
          and server.ffmpeg_error(['   '], url, 1) is None)
    for noisy in ('aac', 'mp2', 'mp3', 'hevc', 'mpeg2video', 'h264'):
        check(f'chatter from the {noisy} decoder is dropped',
              server.ffmpeg_error([f'[{noisy} @ 0x5555] something odd happened', 'Connection refused'], url, 1) == 'Connection refused',
              server.ffmpeg_error([f'[{noisy} @ 0x5555] something odd happened', 'Connection refused'], url, 1))
    got = server.ffmpeg_error(['Something unusual happened', 'Then another thing'], url, 2)
    check('no known cause: the first line that is not noise', got == 'Something unusual happened', got)
    got = server.ffmpeg_error(['Something unusual happened', '[tcp @ 0x1] Connection to tcp://h:1 failed: Connection refused'], url, 1)
    check('a known cause wins over an earlier unknown line', got == 'Connection to tcp://h:1 failed: Connection refused', got)
    long_line = 'Error: ' + 'x' * 400 + ' refused'
    got = server.ffmpeg_error([long_line], url, 1)
    check('a long line is cut to 200 characters', len(got) <= 200 and got.startswith('Error: xxx'), len(got))
    got = server.ffmpeg_error(['Connection\trefused\x07 by\x00 host\r'], url, 1)
    check('control characters are squashed', got == 'Connection refused by host', repr(got))
    got = server.ffmpeg_error([f'Server returned 401 Unauthorized for {url}'], url, 1)
    check('the stream URL is redacted wherever it appears', 'secret' not in got and 'http://***@127.0.0.1:1/x' in got, got)
    got = server.ffmpeg_error(['redirected to http://u2:pw2@other/z: Connection refused'], url, 1)
    check('...and so is any other URL with credentials', 'pw2' not in got and '***@other' in got, got)
    check('accepts bytes-decoded lines with a trailing newline', server.ffmpeg_error(['Connection refused\n'], url, 1) == 'Connection refused')


# ---------------------------------------------------------------- units: MP4

def init_parts(**kw):
    """(init bytes, moov box bytes) of a fake init segment."""
    init = fk.build_init(**kw)
    return init, init[struct.unpack('>I', init[:4])[0]:]


def moov_of(*traks, trex=None, version=None):
    return fk.box(b'moov', fk.mvhd(len(traks) + 1), *traks, *([fk.mvex(trex)] if trex is not None else []))


def child(data: bytes, kind: str) -> bytes:
    for k, a, b in kids(data, 8, len(data)):
        if k == kind:
            return data[a - 8:b]
    raise KeyError(kind)


def trak_with_tkhd_v1(track_id: int, width: int, height: int, kind: str = 'vide') -> bytes:
    """A trak whose tkhd is the 64-bit (version 1) form."""
    entry = fk.visual_entry(b'avc1', width, height, fk.avcc('640028')) if kind == 'vide' else fk.audio_entry(2, 48000, fk.esds())
    donor = fk.trak(track_id, kind, entry, 90000, width, height)
    tkhd = fk.fbox(b'tkhd', 1, 3, struct.pack('>QQIIQ', 0, 0, track_id, 0, 0), bytes(8), struct.pack('>HHHH', 0, 0, 0, 0), fk.MATRIX,
                   struct.pack('>II', width << 16, height << 16))
    return fk.box(b'trak', tkhd, child(donor, 'mdia'))


def fragment_flags(moof_bytes, trex=None):
    return server.mp4_fragment_info(moof_bytes, trex)


def phase_units_mp4():
    S.section('mp4_boxes')
    data = fk.box(b'ftyp', b'isom', bytes(4)) + fk.box(b'free', b'abc') + fk.box64(b'big ', b'0123456789') + fk.box(b'moov', fk.box(b'trak', b'xy'))
    got = list(server.mp4_boxes(data))
    check('mp4_boxes: (type, payload start, box end) for each box, a 64-bit size included',
          [(k, data[a:b][:4]) for k, a, b in got][:3] == [('ftyp', b'isom'), ('free', b'abc'), ('big ', b'0123')]
          and [k for k, _, _ in got] == ['ftyp', 'free', 'big ', 'moov']
          and got[-1][2] == len(data) and got[2][1] == 16 + 11 + 16, got)
    inner = list(server.mp4_boxes(data, got[3][1], got[3][2]))
    check('mp4_boxes: start and end walk the boxes inside one', [k for k, _, _ in inner] == ['trak'], inner)
    check('mp4_boxes: a truncated last box ends the walk quietly', [k for k, _, _ in server.mp4_boxes(data[:-3])] == ['ftyp', 'free', 'big '])
    check('mp4_boxes: a header cut in half ends it too', list(server.mp4_boxes(b'\x00\x00\x00\x10mo')) == [] and list(server.mp4_boxes(b'')) == [])
    open_ended = fk.box(b'free', b'x') + struct.pack('>I4s', 0, b'mdat') + b'tail'
    check('mp4_boxes: size 0 means to the end', [(k, b) for k, _, b in server.mp4_boxes(open_ended)] == [('free', 9), ('mdat', 9 + 12)])
    for bad, what in ((struct.pack('>I4s', 4, b'free') + bytes(16), 'a size below its own header'),
                      (struct.pack('>I4s', 1, b'free') + struct.pack('>Q', 9) + bytes(8), 'a 64-bit size below 16')):
        try:
            list(server.mp4_boxes(bad))
            check(f'mp4_boxes: {what} is an error', False, 'no error')
        except ValueError:
            check(f'mp4_boxes: {what} is a ValueError', True)

    S.section('mp4_init_info: tracks, codecs, sizes, trex')
    init, moov = init_parts()
    info = server.mp4_init_info(moov)
    want = {'tracks': {1: {'kind': 'vide', 'codec': 'avc1.42C01F', 'width': 320, 'height': 180},
                       2: {'kind': 'soun', 'codec': 'mp4a.40.2', 'width': 0, 'height': 0}}, 'trex': {1: 0, 2: 0}}
    check('video + audio: ids 1 and 2, avc1 and mp4a codecs, 320x180, trex flags 0', info == want, info)
    info = server.mp4_init_info(init_parts(video=(1920, 1080), codec='640028')[1])
    check('1920x1080 high profile: avc1.640028', info['tracks'][1]['codec'] == 'avc1.640028'
          and (info['tracks'][1]['width'], info['tracks'][1]['height']) == (1920, 1080), info)
    info = server.mp4_init_info(init_parts(video=(1280, 720), codec='4d4028')[1])
    check('main profile: avc1.4D4028, the hex in capitals', info['tracks'][1]['codec'] == 'avc1.4D4028', info)
    info = server.mp4_init_info(init_parts(audio=False)[1])
    check('video only: one track', list(info['tracks']) == [1] and info['tracks'][1]['kind'] == 'vide' and info['trex'] == {1: 0}, info)
    info = server.mp4_init_info(init_parts(video=None)[1])
    check('audio only: the audio is track 1', list(info['tracks']) == [1]
          and info['tracks'][1] == {'kind': 'soun', 'codec': 'mp4a.40.2', 'width': 0, 'height': 0}, info)
    info = server.mp4_init_info(init_parts(fourcc='hev1')[1])
    check('hev1: named bare (a browser will most likely refuse it)', info['tracks'][1]['codec'] == 'hev1', info)
    for aot in (1, 5, 29):
        info = server.mp4_init_info(init_parts(aot=aot)[1])
        check(f'AAC audio object type {aot}: mp4a.40.{aot}', info['tracks'][2]['codec'] == f'mp4a.40.{aot}', info)
    for length_bytes in (1, 4):
        info = server.mp4_init_info(init_parts(esds_length=length_bytes)[1])
        check(f'esds with {length_bytes}-byte descriptor lengths (ffmpeg writes 4)', info['tracks'][2]['codec'] == 'mp4a.40.2', info)
    # Audio object type 42 (xHE-AAC): the type is 31, then 6 more bits (42 - 32 = 10) in the AudioSpecificConfig.
    config = bytes([0xF9, 0x40 | 3 << 1])
    decoder = bytes([0x40, 0x15]) + bytes(3) + struct.pack('>II', 1, 1) + fk.descriptor(5, config)
    esds_escape = fk.fbox(b'esds', 0, 0, fk.descriptor(3, struct.pack('>HB', 2, 0) + fk.descriptor(4, decoder)))
    trak = fk.trak(2, 'soun', fk.audio_entry(2, 48000, esds_escape), 48000)
    info = server.mp4_init_info(moov_of(trak, trex={2: 0}))
    check('audio object type 31 + 6 bits: mp4a.40.42', info['tracks'][2]['codec'] == 'mp4a.40.42', info)
    # An ES descriptor with the optional fields (a URL, dependsOn, OCR) in front of the decoder config.
    es = (struct.pack('>H', 2) + bytes([0x80 | 0x40 | 0x20]) + struct.pack('>H', 7) + bytes([3]) + b'url' + struct.pack('>H', 9)
          + fk.descriptor(4, decoder) + fk.descriptor(6, b'\x02'))
    trak = fk.trak(2, 'soun', fk.audio_entry(2, 48000, fk.fbox(b'esds', 0, 0, fk.descriptor(3, es))), 48000)
    info = server.mp4_init_info(moov_of(trak, trex={2: 0}))
    check('an ES descriptor with dependsOn, URL and OCR fields before the config', info['tracks'][2]['codec'] == 'mp4a.40.42', info)
    info = server.mp4_init_info(moov_of(trak_with_tkhd_v1(7, 1280, 720), fk.trak(9, 'soun', fk.audio_entry(2, 48000, fk.esds()), 48000),
                                        trex={7: 0x10000, 9: 0}))
    check('tkhd version 1 (64-bit times): the track id and the size are found; other trex flags are kept',
          info == {'tracks': {7: {'kind': 'vide', 'codec': 'avc1.640028', 'width': 1280, 'height': 720},
                              9: {'kind': 'soun', 'codec': 'mp4a.40.2', 'width': 0, 'height': 0}},
                   'trex': {7: 0x10000, 9: 0}}, info)
    info = server.mp4_init_info(init_parts(trex=False)[1])
    check('a moov without mvex: no trex', info['trex'] == {} and set(info['tracks']) == {1, 2}, info)
    info = server.mp4_init_info(moov_of())
    check('a moov without tracks: an empty answer', info == {'tracks': {}, 'trex': {}}, info)
    info = server.mp4_init_info(fk.box(b'moov', fk.mvhd(2), fk.box(b'trak', fk.box(b'free', b'x')), fk.box(b'udta', b'whatever'),
                                       fk.box(b'meta', b'x')))
    check('a trak with no tkhd or mdia is skipped; unknown boxes are ignored', info == {'tracks': {}, 'trex': {}}, info)
    info = server.mp4_init_info(init_parts()[1] + b'trailing junk')
    check('bytes after the moov are ignored', set(info['tracks']) == {1, 2}, info)
    for what, blob in (('a box that is no moov', fk.box(b'free', b'x')), ('nothing', b''), ('a few bytes', b'\x00\x00')):
        try:
            server.mp4_init_info(blob)
            check(f'mp4_init_info of {what}: ValueError', False, 'no error')
        except ValueError:
            check(f'mp4_init_info of {what}: ValueError', True)
    bad = 0
    for cut in range(8, len(moov)):
        for outer_ok in (True, False):
            blob = (struct.pack('>I', cut) + moov[4:cut]) if outer_ok else moov[:cut]
            try:
                server.mp4_init_info(blob)
            except ValueError:
                pass
            except Exception as exc:  # anything else would kill the supervisor
                bad += 1
                check(f'mp4_init_info of a moov cut at {cut} (outer size {"fixed" if outer_ok else "left"})', False, repr(exc))
    check(f'mp4_init_info: a moov cut at any of {len(moov) - 8} points gives an answer or a ValueError, never another exception', bad == 0)

    S.section('stream_mime')
    tracks = server.mp4_init_info(moov)['tracks']
    check('video + audio: video/mp4 with both codecs, the video first', server.stream_mime(tracks) == 'video/mp4; codecs="avc1.42C01F,mp4a.40.2"',
          server.stream_mime(tracks))
    check('...whatever order the tracks come in', server.stream_mime({2: tracks[2], 1: tracks[1]}) == 'video/mp4; codecs="avc1.42C01F,mp4a.40.2"')
    check('audio only: audio/mp4', server.stream_mime({1: tracks[2]}) == 'audio/mp4; codecs="mp4a.40.2"')
    check('video only: video/mp4', server.stream_mime({1: tracks[1]}) == 'video/mp4; codecs="avc1.42C01F"')
    check('hev1 is named bare',
          server.stream_mime({1: {'kind': 'vide', 'codec': 'hev1', 'width': 1, 'height': 1}, 2: tracks[2]}) == 'video/mp4; codecs="hev1,mp4a.40.2"')

    S.section('mp4_fragment_info: is the first sample a keyframe? (every path the standard allows)')
    SYNC, DELTA = fk.SYNC, fk.DELTA

    def frag(*trafs, seq=1):
        return fk.moof(fk.mfhd(seq), *trafs)

    def one(track, *, tfhd_kw=None, trun_kw=None, count=3, extra=()):
        return fk.traf(fk.tfhd(track, **(tfhd_kw or {})), fk.tfdt(0), *extra, fk.trun(count, **(trun_kw or {})))
    check('trun first_sample_flags (0x4): a keyframe', fragment_flags(frag(one(1, trun_kw={'first_flags': SYNC}))) == {1: True})
    check('...a difference frame', fragment_flags(frag(one(1, trun_kw={'first_flags': DELTA}))) == {1: False})
    check('...with a data offset in front of it (0x1)', fragment_flags(frag(one(1, trun_kw={'data_offset': 100, 'first_flags': DELTA}))) == {1: False}
          and fragment_flags(frag(one(1, trun_kw={'data_offset': 100, 'first_flags': SYNC}))) == {1: True})
    check('first_sample_flags wins over the tfhd default',
          fragment_flags(frag(one(1, tfhd_kw={'flags': DELTA}, trun_kw={'first_flags': SYNC}))) == {1: True}
          and fragment_flags(frag(one(1, tfhd_kw={'flags': SYNC}, trun_kw={'first_flags': DELTA}))) == {1: False})
    for label, kw in (('flags only', {'flags': [SYNC, DELTA, DELTA]}),
                      ('durations and flags', {'durations': [3, 3, 3], 'flags': [SYNC, DELTA, DELTA]}),
                      ('sizes and flags', {'sizes': [9, 9, 9], 'flags': [SYNC, DELTA, DELTA]}),
                      ('durations, sizes, flags', {'durations': [3, 3, 3], 'sizes': [9, 9, 9], 'flags': [SYNC, DELTA, DELTA]}),
                      ('all four fields, a data offset',
                       {'data_offset': 40, 'durations': [3, 3, 3], 'sizes': [9, 9, 9], 'flags': [SYNC, DELTA, DELTA], 'offsets': [0, 0, 0]})):
        flipped = dict(kw, flags=[DELTA, SYNC, SYNC])
        check(f'per-sample flags (0x400), {label}: the first sample decides', fragment_flags(frag(one(1, trun_kw=kw))) == {1: True}
              and fragment_flags(frag(one(1, trun_kw=flipped))) == {1: False})
    import itertools
    for picked in itertools.product((0, 1), repeat=4):
        kw = {k: v for use, (k, v) in zip(picked, (('base_offset', 5), ('description', 1), ('duration', 3), ('size', 7))) if use}
        check(f'tfhd default flags (0x20) behind the optional fields {sorted(kw) or "none"}',
              fragment_flags(frag(one(1, tfhd_kw=dict(kw, flags=SYNC)))) == {1: True}
              and fragment_flags(frag(one(1, tfhd_kw=dict(kw, flags=DELTA)))) == {1: False})
    check('no flags anywhere: the trex default decides', fragment_flags(frag(one(1)), {1: DELTA}) == {1: False}
          and fragment_flags(frag(one(1)), {1: SYNC}) == {1: True}
          and fragment_flags(frag(one(1)), {1: 0}) == {1: True})
    check('...for its own track only', fragment_flags(frag(one(2)), {1: DELTA, 2: SYNC}) == {2: True}
          and fragment_flags(frag(one(1)), {2: DELTA}) == {1: True})
    check('...and none given at all: sync (trex flags 0)', fragment_flags(frag(one(1))) == {1: True}
          and fragment_flags(frag(one(1)), None) == {1: True})
    check('the tfhd default wins over the trex default', fragment_flags(frag(one(1, tfhd_kw={'flags': SYNC})), {1: DELTA}) == {1: True})
    check('the sync bit is the only one that counts (other flag bits are ignored)',
          fragment_flags(frag(one(1, trun_kw={'first_flags': 0x0200_0000 | 0x0001_0000 * 0 | 0x4000})), None) == {1: True}
          and fragment_flags(frag(one(1, trun_kw={'first_flags': 0x10000})), None) == {1: False})
    check('a track fragment that is not the first: both are reported, each by its own flags',
          fragment_flags(frag(one(2, trun_kw={'first_flags': SYNC}), one(1, trun_kw={'first_flags': DELTA}))) == {2: True, 1: False}
          and fragment_flags(frag(one(2, tfhd_kw={'flags': SYNC}), one(1, trun_kw={'first_flags': SYNC}))) == {2: True, 1: True})
    check('the track ids come in the order of the trafs', list(fragment_flags(frag(one(2), one(1)))) == [2, 1]
          and list(fragment_flags(frag(one(1), one(2)))) == [1, 2])
    check('a run of no samples is no keyframe', fragment_flags(frag(fk.traf(fk.tfhd(1), fk.tfdt(0), fk.trun(0, first_flags=SYNC)))) == {1: False})
    check('a traf without a trun is no keyframe', fragment_flags(frag(fk.traf(fk.tfhd(1, flags=SYNC), fk.tfdt(0)))) == {1: False})
    check('a traf without a tfhd is skipped; a moof without trafs says nothing',
          fragment_flags(frag(fk.traf(fk.tfdt(0), fk.trun(2, first_flags=SYNC)))) == {} and fragment_flags(frag()) == {})
    check('boxes of the 64-bit size form inside a moof',
          fragment_flags(fk.box64(b'moof', fk.mfhd(1), fk.box64(b'traf', fk.tfhd(1), fk.tfdt(0), fk.trun(2, first_flags=SYNC)))) == {1: True})
    check('unknown boxes between the others are skipped',
          fragment_flags(frag(fk.box(b'free', b'xx'),
                              fk.traf(fk.box(b'sdtp', b'xx'), fk.tfhd(1), fk.tfdt(0), fk.box(b'subs', b'x'),
                                      fk.trun(2, first_flags=SYNC)))) == {1: True})
    full = frag(fk.traf(fk.tfhd(1, duration=3, size=7, flags=DELTA), fk.tfdt(5), fk.trun(2, data_offset=9, first_flags=SYNC, sizes=[7, 7])),
                fk.traf(fk.tfhd(2, flags=SYNC), fk.tfdt(5), fk.trun(2, data_offset=30, sizes=[5, 5])))
    check('a whole moof of two tracks', fragment_flags(full) == {1: True, 2: True})
    bad = 0
    for cut in range(8, len(full)):
        for outer_ok in (True, False):
            blob = (struct.pack('>I', cut) + full[4:cut]) if outer_ok else full[:cut]
            try:
                server.mp4_fragment_info(blob)
            except ValueError:
                pass
            except Exception as exc:
                bad += 1
                check(f'mp4_fragment_info of a moof cut at {cut}', False, repr(exc))
    check(f'mp4_fragment_info: a moof cut at any of {len(full) - 8} points gives an answer or a ValueError', bad == 0)
    # A trun that says it has more samples than its bytes hold, and a tfhd cut off inside its fields.
    short_trun = fk.box(b'moof', fk.mfhd(1), fk.box(b'traf', fk.tfhd(1), fk.fbox(b'trun', 0, 0x405, struct.pack('>I', 5))))
    try:
        got = server.mp4_fragment_info(short_trun)
        check('a trun whose fields stop short: a ValueError (or an answer), never a struct.error', isinstance(got, dict))
    except ValueError:
        check('a trun whose fields stop short: a ValueError (or an answer), never a struct.error', True)
    except Exception as exc:
        check('a trun whose fields stop short: a ValueError (or an answer), never a struct.error', False, repr(exc))
    try:
        server.mp4_fragment_info(fk.box(b'free', b'x'))
        check('mp4_fragment_info of a box that is no moof: ValueError', False, 'no error')
    except ValueError:
        check('mp4_fragment_info of a box that is no moof: ValueError', True)


def feed_all(reader, data: bytes, chunk: int | None = None) -> list:
    """Everything the reader makes of data, fed in pieces of chunk bytes (one at a time when 1; None: whole)."""
    events = []
    if chunk is None:
        return list(reader.feed(data))
    for i in range(0, len(data), chunk):
        events += reader.feed(data[i:i + chunk])
    return events


def phase_units_reader():
    S.section('BoxSplitter: top-level boxes out of a byte stream')
    stream = fk.sample_stream(14, f'{FAKE}/live?gop=5')
    whole = b''.join(stream)
    boxes = server.BoxSplitter().feed(whole)
    check('ftyp, moov, then a moof and an mdat per fragment', [k for k, _ in boxes] == ['ftyp', 'moov'] + ['moof', 'mdat'] * 14,
          [k for k, _ in boxes][:6])
    check('...and the boxes are exactly the bytes that were fed', b''.join(b for _, b in boxes) == whole)
    out, splitter = [], server.BoxSplitter()
    for i in range(len(whole)):
        out += splitter.feed(whole[i:i + 1])
    check('fed one byte at a time: the same boxes', out == boxes)
    for chunk in (2, 3, 7, 64, 1000, 4096):
        check(f'fed {chunk} bytes at a time: the same boxes', feed_all(server.BoxSplitter(), whole, chunk) == boxes)
    splitter = server.BoxSplitter()
    first = splitter.feed(whole[:len(stream[0]) + 10])
    check('an incomplete box is held back: nothing for it yet', [k for k, _ in first] == ['ftyp', 'moov'] and splitter.feed(b'') == [])
    check('...and comes out when the rest does', [k for k, _ in splitter.feed(whole[len(stream[0]) + 10:])] == ['moof', 'mdat'] * 14)
    got = server.BoxSplitter().feed(fk.box64(b'free', b'x' * 100) + fk.box(b'mdat', b'y'))
    check('a 64-bit size box is cut right', [(k, len(b)) for k, b in got] == [('free', 116), ('mdat', 9)], got)
    for label, blob in (('size 0 (to the end of the file: not for a live stream)', struct.pack('>I4s', 0, b'mdat') + bytes(20)),
                        ('size 4 (smaller than its own header)', struct.pack('>I4s', 4, b'free') + bytes(20)),
                        ('size 7', struct.pack('>I4s', 7, b'free') + bytes(20)),
                        ('a 64-bit size of 10', struct.pack('>I4sQ', 1, b'free', 10) + bytes(20)),
                        (f'size STREAM_BOX_MAX + 1 ({server.STREAM_BOX_MAX + 1})', struct.pack('>I4s', server.STREAM_BOX_MAX + 1, b'mdat')),
                        ('a 64-bit size of 1 TiB', struct.pack('>I4sQ', 1, b'mdat', 1 << 40)), ('text', b'This is not an MP4 file at all.'),
                        ('an HTTP error page', b'HTTP/1.0 404 Not Found\r\n\r\n')):
        try:
            server.BoxSplitter().feed(blob)
            check(f'BoxSplitter: {label} is a ValueError', False, 'no error')
        except ValueError:
            check(f'BoxSplitter: {label} is a ValueError', True)
    check('a header claiming exactly STREAM_BOX_MAX is waited for, not refused',
          server.BoxSplitter().feed(struct.pack('>I4s', server.STREAM_BOX_MAX, b'mdat') + bytes(100)) == [])
    splitter = server.BoxSplitter()
    splitter.feed(struct.pack('>I4s', server.STREAM_BOX_MAX, b'mdat') + bytes(1000))
    check('...only what has arrived is held', len(splitter.buf) <= 1100, len(splitter.buf))

    S.section('FragmentReader: one init segment, then fragments, each with its keyframe flag and tracks')

    def run(url, count, chunk=None, argv=None):
        data = fk.sample_stream(count, url, argv)
        events = feed_all(server.FragmentReader(), b''.join(data), chunk)
        return data, events, [e for e in events if e[0] == 'fragment']
    data, events, frags = run(f'{FAKE}/live?gop=5', 14, 1)
    check('one init event, first: the ftyp + moov bytes, the MIME type, the tracks',
          [e[0] for e in events].count('init') == 1 and events[0][0] == 'init' and events[0][1] == data[0]
          and events[0][2] == 'video/mp4; codecs="avc1.42C01F,mp4a.40.2"'
          and events[0][3][1]['kind'] == 'vide' and events[0][3][2]['kind'] == 'soun', events[0][2:])
    check('every fragment is the moof + mdat bytes, in order', [e[1] for e in frags] == data[1:] and len(frags) == 14)
    check('a fragment is sync when the video starts with a keyframe: every 5th here', [e[2] for e in frags] == [i % 5 == 0 for i in range(14)],
          [e[2] for e in frags])
    check('...with the ids of the tracks it carries', all(e[3] == (1, 2) for e in frags), [e[3] for e in frags][:3])
    _, _, frags = run(f'{FAKE}/audio-lead?lead=4&gop=3', 12)
    check('audio-lead: audio-only fragments first, none of them a start point', [(e[2], e[3]) for e in frags[:4]] == [(False, (2,))] * 4,
          [(e[2], e[3]) for e in frags[:5]])
    check('...then the first video fragment is sync, and the GOPs go on from it', [e[2] for e in frags[4:]] == [i % 3 == 0 for i in range(8)]
          and frags[4][3] == (1, 2),
          [(e[2], e[3]) for e in frags[4:7]])
    _, _, frags = run(f'{FAKE}/audio', 6)
    check('an audio-only stream: every fragment is a start point', [e[2] for e in frags] == [True] * 6 and all(e[3] == (1,) for e in frags),
          [(e[2], e[3]) for e in frags])
    _, _, frags = run(f'{FAKE}/live?audio=0&gop=4', 9)
    check('video only', [e[2] for e in frags] == [i % 4 == 0 for i in range(9)] and all(e[3] == (1,) for e in frags))
    _, _, frags = run(f'{FAKE}/drop-audio?n=3&gop=4', 8)
    check('drop-audio: the audio track leaves the fragments after the third', [e[3] for e in frags] == [(1, 2)] * 3 + [(1,)] * 5,
          [e[3] for e in frags])
    data = fk.sample_stream(4, f'{FAKE}/live')
    mfra = fk.build_mfra({1: [(0, 1249)], 2: [(0, 1249)]})
    events = feed_all(server.FragmentReader(), b''.join(data) + fk.box(b'free', b'padding') + mfra + fk.box(b'skip', b'x'), 5)
    check('mfra, free and the like make no event', [e[0] for e in events] == ['init'] + ['fragment'] * 4, [e[0] for e in events])
    events = feed_all(server.FragmentReader(), b''.join(data[1:3]) + b''.join(data), 100)
    check('fragments before the init segment are not published', [e[0] for e in events] == ['init'] + ['fragment'] * 4)
    events = feed_all(server.FragmentReader(), data[0] + data[1][:data[1].index(b'mdat') - 4] + data[1] + data[2])
    check('a moof without its mdat is replaced by the next moof', [e[0] for e in events] == ['init', 'fragment', 'fragment']
          and events[1][1] == data[1])
    for label, blob in (('text', b'This is not an MP4 file at all.' * 4), ('a moov without tracks', fk.box(b'ftyp', b'iso5', bytes(8)) + moov_of()),
                        ('a moov cut inside its first trak', fk.box(b'ftyp', b'iso5', bytes(8)) + init_parts()[1][:208])):
        try:
            events = feed_all(server.FragmentReader(), blob + bytes(200))
            check(f'FragmentReader: {label}: a ValueError (or no init)', not events, events)
        except ValueError:
            check(f'FragmentReader: {label}: a ValueError (or no init)', True)
    reader = server.FragmentReader()
    feed_all(reader, data[0])
    try:
        feed_all(reader, fk.box(b'moof', b'\x00' * 3) + fk.box(b'mdat', b'x'))
        check('FragmentReader: a moof that is mostly nothing makes a fragment of no tracks, or a ValueError', True)
    except ValueError:
        check('FragmentReader: a moof that is mostly nothing makes a fragment of no tracks, or a ValueError', True)
    except Exception as exc:
        check('FragmentReader: a moof that is mostly nothing makes a fragment of no tracks, or a ValueError', False, repr(exc))


def remux(ts: bytes, level: int = 0, audio: bool = True) -> bytes | None:
    """The real ffmpeg's fragmented MP4 for a TS, with the board's own command line (its input and whitelist swapped for a file)."""
    if not FFMPEG:
        return None
    path = TMP / f'remux-{len(ts)}-{level}.ts'
    path.write_bytes(ts)
    argv = server.ffmpeg_argv(FFMPEG, 'http://h/x', level, audio, 7)
    for flag in ('-protocol_whitelist', '-rw_timeout'):
        del argv[argv.index(flag):argv.index(flag) + 2]
    argv[argv.index('-i') + 1] = str(path)
    done = subprocess.run(argv, capture_output=True, timeout=120)
    return done.stdout if done.returncode == 0 and done.stdout else None


def key_packets(mp4: bytes) -> int | None:
    """How many video packets ffprobe calls keyframes in this MP4."""
    if not FFPROBE:
        return None
    done = subprocess.run([FFPROBE, '-v', 'error', '-select_streams', 'v:0', '-show_entries', 'packet=flags', '-of', 'csv=p=0', '-i', 'pipe:0'],
                          input=mp4, capture_output=True, timeout=60)
    return sum('K' in line for line in done.stdout.decode().splitlines()) if done.returncode == 0 else None


def phase_units_real_mp4():
    S.section("the parsers on real ffmpeg's output (the board's command line, a TS made by ffmpeg)")
    if not FFMPEG or not FFPROBE or not has_encoder('libx264'):
        S.skip('parsers on real ffmpeg output', 'ffmpeg, ffprobe or libx264 not installed')
        return
    sources = {'video': (make_ts('video', 6), 0, True), 'av': (make_ts('av', 6), 0, True), 'audio': (make_ts('audio', 4), 0, True),
               'av joined mid-GOP': (ts_from(make_ts('av', 8, gop=60), 0.7), 1, True)}
    for name, (ts, level, audio) in sources.items():
        if ts is None:
            S.skip(f'real output: {name}', 'could not make the source')
            continue
        out = remux(ts, level, audio)
        if not check(f'real output ({name}): ffmpeg made fragmented MP4', out is not None and out[4:8] == b'ftyp'):
            continue
        info = probe(out)
        events = feed_all(server.FragmentReader(), out, 1 if name == 'audio' else 4093)
        inits = [e for e in events if e[0] == 'init']
        frags = [e for e in events if e[0] == 'fragment']
        check(f'real output ({name}): one init event and {len(frags)} fragments', len(inits) == 1 and events[0][0] == 'init' and len(frags) >= 4,
              [e[0] for e in events][:5])
        if not inits:
            continue
        tracks = inits[0][3]
        streams = {s['codec_type']: s for s in info['streams']}
        want = {'vide': streams.get('video'), 'soun': streams.get('audio')}
        for track in tracks.values():
            ref = want[track['kind']]
            check(f'real output ({name}): the {track["kind"]} codec string is the one ffprobe gives ({ref and ref["mime_codec_string"]})',
                  ref is not None and track['codec'].lower() == ref['mime_codec_string'].lower(), (track, ref and ref['mime_codec_string']))
            if track['kind'] == 'vide':
                check(f'real output ({name}): the size is ffprobe\'s', (track['width'], track['height']) == (ref['width'], ref['height']),
                      (track, ref['width'], ref['height']))
        check(f'real output ({name}): the same tracks as ffprobe lists',
              sorted(t['kind'] for t in tracks.values()) == sorted({'video': 'vide', 'audio': 'soun'}[k] for k in streams), (tracks, list(streams)))
        mime = inits[0][2]
        check(f'real output ({name}): the MIME type', mime == ('audio' if 'video' not in streams else 'video') + '/mp4; codecs="' + ','.join(
            t['codec'] for t in sorted(tracks.values(), key=lambda t: t['kind'] != 'vide')) + '"', mime)
        check(f'real output ({name}): init + fragments are the bytes ffmpeg wrote, but for the trailing mfra',
              inits[0][1] + b''.join(e[1] for e in frags) == out[:len(inits[0][1]) + sum(len(e[1]) for e in frags)]
              and out[len(inits[0][1]) + sum(len(e[1]) for e in frags):][4:8] in (b'mfra', b''), len(out))
        flags = [e[2] for e in frags]
        if 'video' in streams:
            keys = key_packets(out)
            check(f'real output ({name}): as many sync fragments as ffprobe finds keyframes ({keys})', flags.count(True) == keys and keys >= 1,
                  (flags.count(True), keys))
            ours = [moof_sync(e[1][:struct.unpack('>I', e[1][:4])[0]],
                              trex_flags(inits[0][1][struct.unpack('>I', inits[0][1][:4])[0]:])) for e in frags]
            vid = next(i for i, t in tracks.items() if t['kind'] == 'vide')
            check(f'real output ({name}): the flags agree with an independent reading of the standard', [o.get(vid, False) for o in ours] == flags,
                  (flags[:8], [o.get(vid) for o in ours][:8]))
            first_video = next(i for i, e in enumerate(frags) if vid in e[3])
            check(f'real output ({name}): the first fragment with video is a keyframe', flags[first_video] is True and not any(flags[:first_video]),
                  (first_video, flags[:first_video + 1]))
            if name == 'av joined mid-GOP':
                check('real output (joined mid-GOP): audio-only fragments come first, as on the real source', first_video >= 1
                      and all(e[3] == tuple(i for i, t in tracks.items() if t['kind'] == 'soun') for e in frags[:first_video]),
                      (first_video, [e[3] for e in frags[:first_video + 1]]))
        else:
            check(f'real output ({name}): audio only, every fragment a start point', all(flags) and len(flags) == len(frags), flags[:6])


# ---------------------------------------------------------------- api: the routes

def has_headers(h: dict, docs: str) -> bool:
    return h.get('x-tasks-version') == '3' and h.get('x-tasks-docs') == docs


def phase_api():
    S.section('the routes with the fake ffmpeg: shapes, replace / keep / close')
    srv = Board('api').start()
    call, docs = srv.call, docs_version()
    try:
        s, h, b = call('GET', '/api/stream')
        check('GET /api/stream, nothing open: {now, stream: null, ffmpeg: the path of ffmpeg}', s == 200 and list(b) == ['now', 'stream', 'ffmpeg']
              and b['stream'] is None
              and b['ffmpeg'] == str(srv.bin / 'ffmpeg') and abs(b['now'] - time.time()) < 5, b)
        check('...JSON, with both X-Tasks headers', h.get('content-type', '').startswith('application/json') and has_headers(h, docs), h)
        s, h, b = call('GET', '/api/events')
        check('/api/events ends in stream: null', s == 200 and list(b)[-1] == 'stream' and b['stream'] is None
              and list(b) == ['now', 'cursor', 'events', 'truncated', 'waiting', 'stale', 'settings_version', 'stream'], list(b))
        s, h, b = call('GET', '/api/events?since=0')
        check('...with a cursor too', s == 200 and list(b)[-1] == 'stream', list(b))

        t0, t = time.time(), time.monotonic()
        s, h, b = srv.post(f'{FAKE}/hang')
        took = time.monotonic() - t
        st = b.get('stream') or {}
        check('POST /api/stream: 201 at once, whatever the source does', s == 201 and took < 0.5, (s, took))
        check('...the reply is {now, stream, ffmpeg, existing: false}; the stream has the twelve keys in order',
              list(b) == ['now', 'stream', 'ffmpeg', 'existing']
              and b['existing'] is False and list(st) == STREAM_KEYS, (list(b), list(st)))
        check('...12 hex digits of id, the URL, connecting, the timer base, nothing known yet, no viewers, the media path',
              bool(ID_RE.fullmatch(st.get('id', ''))) and st['url'] == f'{FAKE}/hang' and st['state'] == 'connecting'
              and t0 - 1 <= st['opened_at'] <= time.time() + 1
              and abs(st['since'] - st['opened_at']) < 1 and st['error'] is None and st['video'] is None and st['audio'] is None
              and st['width'] is None
              and st['height'] is None and st['viewers'] == 0 and st['media'] == f'/api/stream/media?id={st["id"]}', st)
        check('...and X-Tasks headers', has_headers(h, docs), h)
        s, h, b = call('GET', '/api/stream')
        check('GET /api/stream shows the same object', s == 200 and b['stream'] == st, b)
        s, h, b = call('GET', '/api/events')
        check('/api/events carries it, as the last key', s == 200 and list(b)[-1] == 'stream' and b['stream'] == st, b.get('stream'))
        check('...and a hand-written spelling of the same URL is the same stream',
              srv.post('HTTP://fake.invalid/hang')[2]['stream']['id'] == st['id'])

        check('(the first ffmpeg is started)', wait_until(lambda: len(srv.spawns()) == 1, 5), srv.spawns())
        s, h, b = srv.post(f'{FAKE}/hang')
        time.sleep(0.3)
        check('the same URL again: 200, existing true, the same stream, nothing restarted', s == 200 and b['existing'] is True
              and b['stream']['id'] == st['id']
              and list(b) == ['now', 'stream', 'ffmpeg', 'existing'] and len(srv.spawns()) == 1 and srv.kids() == [srv.spawns()[0]['pid']],
              (s, b.get('existing'), srv.spawns()))
        s, h, b = srv.post(f'{FAKE}/hang?other=1')
        new = b.get('stream') or {}
        check('a newer URL replaces it: 201, a new id, existing false', s == 201 and b['existing'] is False and new['id'] != st['id']
              and new['url'] == f'{FAKE}/hang?other=1', b)
        s, h, b = call('GET', f'/api/stream/media?id={st["id"]}')
        check('the replaced stream\'s media: 410 {error}', s == 410 and list(b) == ['error'] and has_headers(h, docs), (s, b))
        check('exactly one ffmpeg is left, the new one\'s',
              wait_until(lambda: len(srv.kids()) == 1 and srv.spawns()[-1]['url'] == new['url'] and srv.kids() == [srv.spawns()[-1]['pid']], 5),
              (srv.kids(), srv.spawns()))

        s, h, b = call('DELETE', '/api/stream')
        check('DELETE: 200 {now, stream: null, ffmpeg, closed: true}', s == 200 and list(b) == ['now', 'stream', 'ffmpeg', 'closed']
              and b['stream'] is None and b['closed'] is True and has_headers(h, docs), b)
        s, h, b = call('DELETE', '/api/stream')
        check('DELETE again: 200, closed false (idempotent)', s == 200 and b['closed'] is False and b['stream'] is None, b)
        s, h, b = call('GET', f'/api/stream/media?id={new["id"]}')
        check('media of a closed stream: 404 {error}', s == 404 and 'error' in b and has_headers(h, docs), (s, b))
        s, h, b = call('GET', '/api/stream/media')
        check('media with no stream and no id: 404', s == 404 and 'error' in b, (s, b))
        check('no ffmpeg is left', wait_until(lambda: srv.kids() == [], 5), srv.kids())
        check('/api/events says null again', call('GET', '/api/events')[2]['stream'] is None)
        s, h, b = srv.post(f'{FAKE}/hang')
        check('a stream opened after a close has a new id again', s == 201 and b['stream']['id'] not in (st['id'], new['id']))
        call('DELETE', '/api/stream')

        S.section('validation, the Origin guard, methods, sizes')
        bad = [({}, 'no url'), ({'url': None}, 'url null'), ({'url': 5}, 'a number'), ({'url': ['http://h/']}, 'a list'), ({'url': ''}, 'empty'),
               ({'url': ' '}, 'blank'),
               ({'url': 'ftp://h/x'}, 'ftp'), ({'url': 'file:///etc/passwd'}, 'file'), ({'url': 'http://h/a b'}, 'a space'),
               ({'url': 'http://h:0/'}, 'port 0'),
               ({'url': 'http://h:99999/'}, 'port 99999'), ({'url': 'http:///x'}, 'no host'), ({'url': 'x' * 3000}, 'too long'),
               ({'uri': 'http://h/'}, 'the wrong field')]
        for body, label in bad:
            s, h, b = call('POST', '/api/stream', body)
            check(f'POST {label}: 400 with field url', s == 400 and b.get('field') == 'url' and b.get('error') and has_headers(h, docs), (s, b))
        s, h, b = call('POST', '/api/stream', raw=b'not json')
        check('POST invalid JSON: 400', s == 400 and 'error' in b, (s, b))
        s, h, b = call('POST', '/api/stream', raw=b'["http://h/"]')
        check('POST a JSON list: 400', s == 400 and 'error' in b, (s, b))
        check('nothing was opened by any of these', srv.stream() is None and srv.spawns()[2:] == [])
        s, h, b = call('POST', '/api/stream', {'url': f'{FAKE}/hang', 'urll': 'x'})
        check('an unknown field is a warning with a hint, not an error', s == 201
              and any("unknown field 'urll'" in w and "did you mean 'url'" in w for w in b.get('warnings', [])), b.get('warnings'))
        s, h, b = call('POST', '/api/stream', {'url': f'{FAKE}/hang'})
        check('...and a clean reply has no warnings key (ffmpeg is installed, the stream was saved)', s == 200 and 'warnings' not in b,
              b.get('warnings'))
        call('DELETE', '/api/stream')

        for method, path, allow in (('PUT', '/api/stream', {'GET', 'POST', 'DELETE'}), ('PATCH', '/api/stream', {'GET', 'POST', 'DELETE'}),
                                    ('POST', '/api/stream/media', {'GET'}), ('DELETE', '/api/stream/media', {'GET'}),
                                    ('PUT', '/api/stream/media', {'GET'})):
            s, h, b = call(method, path, {})
            got = {x.strip() for x in h.get('allow', '').split(',')}
            check(f'{method} {path}: 405 with Allow', s == 405 and got >= allow and 'error' in b and has_headers(h, docs), (s, h.get('allow'), b))
        s, h, b = call('POST', '/api/stream', {'url': f'{FAKE}/hang'}, headers=EVIL)
        check('POST with a foreign Origin: 403, nothing opened', s == 403 and 'error' in b and srv.stream() is None and has_headers(h, docs), (s, b))
        srv.post(f'{FAKE}/hang')
        s, h, b = call('DELETE', '/api/stream', headers=EVIL)
        check('DELETE with a foreign Origin: 403, the stream stays', s == 403 and srv.stream() is not None, (s, b))
        s, h, b = call('GET', '/api/stream', headers=EVIL)
        check('GET with a foreign Origin: reads are not guarded', s == 200 and b['stream'] is not None, s)
        s, h, b = call('POST', '/api/stream', {'url': f'{FAKE}/hang?o=1'}, headers={'Origin': f'http://127.0.0.1:{srv.port}'})
        check('POST from the page itself (Origin = Host): fine', s == 201, (s, b))
        s, h, b = call('POST', '/api/stream', raw=b'{"url": "' + b'x' * 70000 + b'"}')
        check('POST a body over 64 KiB: 413', s == 413 and 'error' in b and has_headers(h, docs), (s, b))
        s, h, b = call('POST', '/api/stream', raw=b'{"url": "http://h/"}', headers={'Transfer-Encoding': 'chunked'})
        check('POST chunked: 411', s == 411, (s, b))
        s, h, b = call('HEAD', '/api/stream')
        check('HEAD /api/stream: headers, no body', s == 200 and b == '' and has_headers(h, docs), (s, h))
        call('DELETE', '/api/stream')

        S.section('a password is shown to no one')
        replies = []
        for secret_url, secret, shown in (('http://user:s3cr3t@fake.invalid/refuse', 's3cr3t', 'http://***@fake.invalid/refuse'),
                                          ('http://user:p@ss@fake.invalid/refuse?x=1', 'p@ss', 'http://***@fake.invalid/refuse?x=1'),
                                          ('rtsp://admin:hunter2@fake.invalid:554/refuse', 'hunter2', 'rtsp://***@fake.invalid:554/refuse')):
            s, h, b = srv.post(secret_url)
            replies.append(json.dumps(b))
            check(f'{secret_url.split("@")[0]}@...: the reply shows {shown}', s == 201 and b['stream']['url'] == shown, b)
            st = srv.wait_stream(lambda x: x['error'], 10)
            check('...the run fails, and its error names the cause without the password', st is not None and 'Connection refused' in st['error']
                  and secret not in st['error']
                  and 'user:' not in st['error'], st)
            replies += [json.dumps(call('GET', '/api/stream')[2]), json.dumps(call('GET', '/api/events')[2]), json.dumps(srv.post(secret_url)[2])]
            check('...the state file holds the real URL (it must reopen the stream), readable by its owner only',
                  srv.stream_file.exists() and oct(srv.stream_file.stat().st_mode & 0o777) == '0o600'
                  and json.loads(srv.stream_file.read_text())['url'] == secret_url,
                  srv.stream_file.read_text() if srv.stream_file.exists() else 'no file')
            replies.append(json.dumps(call('DELETE', '/api/stream')[2]))
            check(f'...nowhere else: not in {len(replies)} replies, and not in the board log', all(secret not in text for text in replies)
                  and secret not in srv.log_text(),
                  [t for t in replies if secret in t][:1] or srv.log_text()[-800:])
            replies.clear()
        check('DELETE removed the state file', not srv.stream_file.exists())

        S.section('eight POSTs at once: one stream, one ffmpeg')
        urls = [f'{FAKE}/hang?n={i}' for i in range(8)]
        results = [None] * 8

        def post(i):
            results[i] = srv.post(urls[i])
        threads = [threading.Thread(target=post, args=(i,)) for i in range(8)]
        spawned = len(srv.spawns())
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        check('every POST got an answer, 200 or 201, with a stream in it',
              all(r and r[0] in (200, 201) and isinstance(r[2], dict) and r[2].get('stream') for r in results), results)
        final = srv.stream()
        check('...one stream is left, one of the eight URLs', final is not None and final['url'] in urls, final)
        ids = [r[2]['stream']['id'] for r in results if r[0] == 201]
        check('...each 201 made its own id', len(set(ids)) == len(ids) and len(ids) >= 1, ids)
        check('...and ONE ffmpeg is alive a moment later (the replaced ones were stopped)', wait_until(lambda: len(srv.kids()) == 1, 8), srv.kids())
        check('...the one that runs is the last stream\'s', len(srv.kids()) == 1 and srv.spawns()[-1]['pid'] == srv.kids()[0]
              and srv.spawns()[-1]['url'] == final['url'],
              (srv.kids(), srv.spawns()[-1:]))
        check('...at most eight were started', len(srv.spawns()) - spawned <= 8)
        call('DELETE', '/api/stream')
        check('...DELETE leaves none', wait_until(lambda: srv.kids() == [], 5), srv.kids())
    finally:
        srv.stop()
    no_traceback(srv, 'api')


# ---------------------------------------------------------------- fake: the media route

def keyframe(frag: dict, video: int | None = 1) -> bool:
    """Whether a fragment starts the video with a keyframe (video: its track id; None for an audio-only stream)."""
    return any(frag['sync'].values()) if video is None else frag['sync'].get(video, False)


def frags_of(reader, minimum: int = 1, timeout: float = 10) -> list:
    """The fragments a reader has so far, once there are at least minimum of them."""
    wait_until(lambda: len(parse_media(reader.body)['frags']) >= minimum, timeout, 0.05)
    return parse_media(reader.body)['frags']


def seqs_ok(frags: list) -> bool:
    return all(b['seq'] == a['seq'] + 1 for a, b in zip(frags, frags[1:]))


def viewers(board: Board) -> int | None:
    stream = board.stream()
    return stream['viewers'] if stream else None


def phase_media():
    S.section('the media route: headers, body order, late joiners')
    srv = Board('media').start()
    docs = docs_version()
    readers = []
    try:
        st = srv.go_live(f'{FAKE}/live?pace=100&gop=10&marker=A', 10)
        if not check('the fake stream goes live within 10 s', st is not None, srv.stream()):
            return
        check('...live means a keyframe went out: since is that moment, the error is gone, the codecs and the size are known',
              st['error'] is None and st['video'] == 'avc1.42C01F' and st['audio'] == 'mp4a.40.2' and (st['width'], st['height']) == (320, 180)
              and st['since'] >= st['opened_at']
              and st['viewers'] == 0, st)
        r = srv.reader()
        readers.append(r)
        check('the media request is answered', r.wait_headers(5), r.error)
        h = r.headers
        check('200, video/mp4 with the codecs, X-Content-Type-Options, no-store, X-Tasks-*, Connection: close',
              r.status == 200 and h.get('content-type') == 'video/mp4; codecs="avc1.42C01F,mp4a.40.2"'
              and h.get('x-content-type-options') == 'nosniff' and h.get('cache-control') == 'no-store'
              and has_headers(h, docs) and h.get('connection') == 'close', (r.status, h))
        check('...and no Content-Length (the body does not end)', 'content-length' not in h and 'transfer-encoding' not in h, h)
        frags = frags_of(r, 25)
        m = parse_media(r.body)
        check('the body starts ftyp, moov and goes on with moof, mdat pairs', m['types'][:2] == ['ftyp', 'moov']
              and all(t == ('moof' if i % 2 == 0 else 'mdat') for i, t in enumerate(m['types'][2:])), m['types'][:8])
        check('the init segment is what ffmpeg wrote: ftyp and moov, bytes for bytes', m['init'] == fk.build_init(), len(m['init']))
        check('the first fragment starts at a keyframe (a viewer can start there)', len(frags) >= 25 and keyframe(frags[0]) and frags[0]['fake'][0],
              frags[0]['fake'])
        check('fragment sequence numbers have no gap and no repeat', seqs_ok(frags), [f['seq'] for f in frags])
        check('every fragment carries both tracks, from one ffmpeg, with its marker',
              all(f['tracks'] == [1, 2] and f['fake'][1] == 'A' and f['fake'][2] == srv.spawns()[0]['pid'] for f in frags),
              frags[0])
        check('keyframes come every tenth fragment, as the source made them',
              all(keyframe(f) == ((f['seq'] - 1) % 10 == 0) and f['fake'][0] == keyframe(f) for f in frags),
              [(f['seq'], keyframe(f)) for f in frags][:12])
        check('/api/stream counts the viewer', viewers(srv) == 1, srv.stream())

        late, at_join = [], []
        for delay in (0.35, 0.3, 0.45):
            time.sleep(delay)
            at_join.append(parse_media(r.body)['frags'][-1]['seq'])   # how far the stream had got when this viewer came
            late.append(srv.reader())
            readers.append(late[-1])
        for i, lr in enumerate(late):
            fr = frags_of(lr, 6)
            check(f'late joiner {i + 1}: its first fragment is the latest keyframe (the stream was at {at_join[i]}), not the start nor a later one; no gap',
                  fr and keyframe(fr[0]) and (fr[0]['seq'] - 1) % 10 == 0 and at_join[i] - 11 <= fr[0]['seq'] <= at_join[i] + 1 and fr[0]['seq'] > 1
                  and seqs_ok(fr),
                  [(f['seq'], keyframe(f)) for f in fr][:6])
        reference = {f['seq']: f['bytes'] for f in frags_of(r, 40)}
        check('...they all get the same fragments as the first viewer, byte for byte',
              all(f['bytes'] == reference[f['seq']] for lr in late for f in frags_of(lr, 6)[:6] if f['seq'] in reference))
        check('/api/stream counts four viewers', wait_until(lambda: viewers(srv) == 4, 3), srv.stream())

        S.section('HEAD, a wrong id, and the other ways a stream can sound')
        hd = stream_get(srv.port, srv.stream()['media'], method='HEAD')
        check('HEAD: the same headers, no body', hd.wait_headers(5) and hd.status == 200 and hd.headers.get('content-type') == h.get('content-type')
              and 'content-length' not in hd.headers
              and hd.wait_closed(5) and hd.body == b'', (hd.status, hd.headers, len(hd.body)))
        check('HEAD does not subscribe (still four viewers)', viewers(srv) == 4, viewers(srv))
        s, hh, b = srv.call('GET', '/api/stream/media?id=0123456789ab')
        check('a wrong id: 410 {error}', s == 410 and list(b) == ['error'] and has_headers(hh, docs), (s, b))
        s, hh, b = srv.call('HEAD', '/api/stream/media?id=0123456789ab')
        check('HEAD with a wrong id: 410', s == 410, s)
        no_id = stream_get(srv.port, '/api/stream/media')
        check('no id at all: the open stream is served', no_id.wait_headers(5) and no_id.status == 200 and no_id.wait_bytes(2000, 5),
              (no_id.status, len(no_id.body)))
        readers.append(no_id)
        for r_ in readers:
            r_.close()
        readers.clear()

        for url, mime, video, audio, size in ((f'{FAKE}/audio', 'audio/mp4; codecs="mp4a.40.2"', None, 'mp4a.40.2', (None, None)),
                                              (f'{FAKE}/live?audio=0', 'video/mp4; codecs="avc1.42C01F"', 'avc1.42C01F', None, (320, 180)),
                                              (f'{FAKE}/live?codec=640028&w=1920&h=1080', 'video/mp4; codecs="avc1.640028,mp4a.40.2"', 'avc1.640028',
                                               'mp4a.40.2', (1920, 1080)),
                                              (f'{FAKE}/hevc', 'video/mp4; codecs="hev1,mp4a.40.2"', 'hev1', 'mp4a.40.2', (320, 180))):
            st = srv.go_live(url, 10)
            ok = st is not None and (st['video'], st['audio'], (st['width'], st['height'])) == (video, audio, size)
            check(f'{url.split("fake.invalid")[1]}: live, video {video}, audio {audio}, size {size}', ok, st)
            r = srv.reader()
            check(f'...Content-Type {mime}', r.wait_headers(5) and r.headers.get('content-type') == mime, r.headers)
            fr = frags_of(r, 3)
            check('...every fragment of an audio-only stream is a start point; others start at a keyframe', len(fr) >= 3
                  and (all(keyframe(f, None) for f in fr) if video is None else keyframe(fr[0])),
                  [(f['seq'], f['sync']) for f in fr][:3])
            r.close()
        srv.call('DELETE', '/api/stream')

        S.section('audio-lead: connecting until the keyframe, as on the real source')
        srv.post(f'{FAKE}/audio-lead?lead=8&pace=200&gop=5')
        early = srv.wait_stream(lambda s: s['video'] is not None, 5, 0.02)
        check('the header is out (codecs known) while no keyframe has gone out: connecting', early is not None and early['state'] == 'connecting'
              and early['audio'] == 'mp4a.40.2' and early['error'] is None, early)
        r = srv.reader()
        check('a viewer is let in before the keyframe', r.wait_headers(5) and r.status == 200, (r.status, r.error))
        live = srv.wait_stream(lambda s: s['state'] == 'live', 8)
        check('live follows the first keyframe fragment (about 1.6 s of audio first)', live is not None and live['since'] > early['since']
              and live['since'] - early['opened_at'] >= 1.0, (early, live))
        fr = frags_of(r, 12, 15)
        first_video = next((i for i, f in enumerate(fr) if 1 in f['tracks']), None)
        check('the viewer got init, audio-only fragments, then the keyframe fragment with its video', first_video is not None and first_video >= 1
              and all(f['tracks'] == [2] and not keyframe(f) for f in fr[:first_video]) and keyframe(fr[first_video])
              and fr[first_video]['tracks'] == [1, 2], [(f['seq'], f['tracks'], keyframe(f)) for f in fr][:12])
        check('...with no gap in the sequence', seqs_ok(fr))
        late_r = srv.reader()
        fr2 = frags_of(late_r, 3)
        check('a viewer arriving later starts at a keyframe', keyframe(fr2[0]) and 1 in fr2[0]['tracks'], [(f['seq'], f['tracks']) for f in fr2][:3])
        r.close()
        late_r.close()
        srv.call('DELETE', '/api/stream')

        S.section('replacing or closing a stream with viewers attached')
        srv.go_live(f'{FAKE}/live?pace=100&gop=10&marker=A', 10)
        old_pid = srv.spawns()[-1]['pid']
        olds = [srv.reader() for _ in range(3)]
        check('(three viewers on the old stream)', all(x.wait_headers(5) and x.status == 200 for x in olds)
              and wait_until(lambda: viewers(srv) == 3, 3), viewers(srv))
        for x in olds:
            frags_of(x, 3)
        srv.post(f'{FAKE}/live?pace=100&gop=10&marker=B')
        t = time.monotonic()
        check('replaced: every viewer of the old stream is ended within 2 s', all(x.wait_closed(2) for x in olds), [x.closed for x in olds])
        check('...the old ffmpeg is gone and exactly one runs', wait_until(lambda: not alive(old_pid) and len(srv.kids()) == 1, 4),
              (old_pid, srv.kids()))
        check('...the old viewers saw only the old stream, in order',
              all(parse_media(x.body)['frags'] and all(f['fake'][1] == 'A' for f in parse_media(x.body)['frags'])
                                                                          and seqs_ok(parse_media(x.body)['frags']) for x in olds))
        st = srv.wait_stream(lambda s: s['state'] == 'live', 8)
        newr = srv.reader()
        fr = frags_of(newr, 3)
        check('...and a viewer of the new one gets only the new stream, from a keyframe', st is not None and fr
              and all(f['fake'][1] == 'B' for f in fr) and keyframe(fr[0]), fr and fr[0]['fake'])
        srv.call('DELETE', '/api/stream')
        check('closed: its viewer is ended within 2 s, and no ffmpeg is left', newr.wait_closed(2) and wait_until(lambda: srv.kids() == [], 3),
              (newr.closed, srv.kids()))
        for x in olds + [newr]:
            x.close()

        S.section('16 viewers, a 17th, and a reader that stops reading')
        srv.go_live(f'{FAKE}/live?pace=100&gop=10', 10)
        sixteen = [srv.reader() for _ in range(16)]
        check('sixteen viewers are let in', all(x.wait_headers(5) and x.status == 200 for x in sixteen), [x.status for x in sixteen])
        check('...and counted', wait_until(lambda: viewers(srv) == 16, 3), viewers(srv))
        extra = srv.reader()
        check('the 17th: 503 with Retry-After 10 and a JSON error naming the limit', extra.wait_closed(5) and extra.status == 503
              and extra.headers.get('retry-after') == '10'
              and '16' in (extra.json() or {}).get('error', '') and has_headers(extra.headers, docs), (extra.status, extra.headers, extra.body[:200]))
        sixteen[0].close()
        check('a viewer going away makes room', wait_until(lambda: viewers(srv) == 15, 5), viewers(srv))
        again = srv.reader()
        check('...for the next one', again.wait_headers(5) and again.status == 200, again.status)
        for x in sixteen + [again]:
            x.close()
        check('all gone: no viewers', wait_until(lambda: viewers(srv) == 0, 5), viewers(srv))
        srv.call('DELETE', '/api/stream')

        srv.go_live(f'{FAKE}/live?pace=50&size=200000&ksize=200000&gop=20&audio=0', 10)
        good = srv.reader()
        stuck = srv.reader(rcvbuf=2048, paused=True)
        check('two viewers, one of which will not read', stuck.wait_headers(5) and good.wait_headers(5) and wait_until(lambda: viewers(srv) == 2, 3),
              viewers(srv))
        t0 = time.monotonic()
        dropped = wait_until(lambda: viewers(srv) == 1, 25, 0.1)
        took = time.monotonic() - t0
        check(f'the slow one is dropped once it is 64 fragments behind ({took:.1f}s), and the other goes on', dropped, (viewers(srv), took))
        check('...the board says so in its log', 'dropped a viewer' in srv.log_text(), srv.log_text()[-600:])
        fr = frags_of(good, 20)
        mark = len(good.body)
        check('...the good viewer has an unbroken sequence', seqs_ok(fr) and len(fr) >= 20, [f['seq'] for f in fr][:5])
        time.sleep(0.5)
        check('...and is still being fed', len(good.body) > mark + 100000, (mark, len(good.body)))
        t0 = time.monotonic()
        lat = []
        for _ in range(10):
            t1 = time.monotonic()
            srv.call('GET', '/api/events')
            lat.append(time.monotonic() - t1)
        check('...and polls stay fast meanwhile', max(lat) < 0.5, [round(x, 3) for x in lat])
        stuck.resume()
        check('the dropped reader, reading again, gets what was in flight and then the end of the stream', stuck.wait_closed(15)
              and len(stuck.body) > 0, (stuck.closed, stuck.error, len(stuck.body)))
        good.close()
        stuck.close()
        srv.call('DELETE', '/api/stream')

        S.section('a GOP larger than the cache limit (8 MiB) is not cached: a late viewer gets no history until the next keyframe')
        srv.go_live(f'{FAKE}/live?pace=50&size=400000&ksize=400000&gop=40&audio=0', 10)
        first = srv.reader()
        fr = frags_of(first, 26, 15)   # 26 x 400 KB: past the limit, 14 fragments short of the next keyframe (seq 41)
        latest = fr[-1]['seq']
        joiner = srv.reader()
        jf = frags_of(joiner, 3, 15)
        check(f'the stream was at fragment {latest}, in a GOP of 40: the joiner gets no history (the cache was dropped), only what comes next',
              latest < 41 and jf and jf[0]['seq'] > latest and seqs_ok(jf), (latest, [(f['seq'], keyframe(f)) for f in jf]))
        frags_of(first, 48, 15)
        joiner2 = srv.reader()
        jf2 = frags_of(joiner2, 3, 15)
        check('once the next GOP has begun the cache is back: a viewer then starts at that keyframe (fragment 41), with the fragments since', jf2
              and keyframe(jf2[0]) and jf2[0]['seq'] == 41
              and seqs_ok(jf2), [(f['seq'], keyframe(f)) for f in jf2][:4])
        for x in (first, joiner, joiner2):
            x.close()
    finally:
        for r_ in readers:
            r_.close()
        srv.stop()
    no_traceback(srv, 'media')


# ---------------------------------------------------------------- fake: the supervisor (slow scenarios, side by side)

def tagged(tag: str):
    def ck(label: str, cond, detail=''):
        return check(f'{tag}: {label}', cond, detail)
    return ck


def run_group(title: str, *scenarios, limit: float = 240) -> None:
    """Run scenario functions in threads, each on a board of its own; a scenario that raises is a failed check."""
    S.section(title)

    def wrap(fn):
        try:
            fn()
        except BaseException:  # noqa: BLE001 - whatever it was, say so and go on
            import traceback
            check(f'{fn.__name__}: ran to the end', False, traceback.format_exc()[-1500:])
    threads = [threading.Thread(target=wrap, args=(fn,), name=fn.__name__, daemon=True) for fn in scenarios]
    for t in threads:
        t.start()
    deadline = time.monotonic() + limit
    for t in threads:
        t.join(max(0.0, deadline - time.monotonic()))
        if t.is_alive():
            check(f'{t.name}: finished within {limit:.0f} s', False)


def sc_backoff():
    ck = tagged('refused source')
    srv = Board('backoff').start()
    try:
        srv.post('http://user:s3cr3t@fake.invalid/refuse')
        st = srv.wait_stream(lambda s: s['error'], 5)
        ck('the failure is on the stream: the cause, no password, still connecting', st is not None and st['state'] == 'connecting'
           and 'Connection refused' in st['error']
           and 's3cr3t' not in st['error'] and st['video'] is None and st['viewers'] == 0, st)
        since = st['since'] if st else None
        wait_until(lambda: len(srv.spawns()) >= 3, 9, 0.05)
        g = gaps(srv.spawns()[:3])
        ck('the pauses between runs: 1 s, then 2 s', len(g) == 2 and 0.9 <= g[0] <= 2.6 and 1.9 <= g[1] <= 4.2, g)
        time.sleep(0.7)
        t_kick = time.time()
        s, h, b = srv.post('http://user:s3cr3t@fake.invalid/refuse')
        ck('posting the same URL again keeps the stream (200, existing)', s == 200 and b['existing'] is True and b['stream']['id'] == st['id'],
           (s, b.get('existing')))
        ck('...and ends the waiting: the next run starts at once (within 2.5 s), not after the 4 s pause',
           wait_until(lambda: len(srv.spawns()) >= 4, 4, 0.05)
           and srv.spawns()[3]['t'] - t_kick < 2.5, (len(srv.spawns()), [round(x['t'] - t_kick, 2) for x in srv.spawns()]))
        later = srv.stream()
        ck('the timer base survives the failed retries: since is where the trouble began', later['since'] == since and later['id'] == st['id'],
           (since, later['since']))
        ck('every run was given the same command line but for the probe',
           all(x['argv'][x['argv'].index('-i') + 1] == 'http://user:s3cr3t@fake.invalid/refuse' for x in srv.spawns()))
        ck('...and no proxy variable', all(x['env'] == {} for x in srv.spawns()), [x['env'] for x in srv.spawns()][:1])
        srv.call('DELETE', '/api/stream')
        n = len(srv.spawns())
        time.sleep(1.5)
        ck('after DELETE nothing is started again', len(srv.spawns()) == n and srv.kids() == [], (n, len(srv.spawns()), srv.kids()))
    finally:
        srv.stop()
    no_traceback(srv, 'refused source')


def sc_die():
    ck = tagged('source that ends')
    srv = Board('die').start()
    try:
        srv.post(f'{FAKE}/die=3?noise=46')
        live = srv.wait_stream(lambda s: s['state'] == 'live', 5, 0.02)
        r = srv.reader() if live else None
        ck('it goes live, and a viewer is attached', live is not None and r is not None and r.wait_headers(3), live)
        ended = r.wait_closed(6) if r else False
        ck('the viewer is ended when the run ends (the page will ask again)', ended, r and (r.status, len(r.body)))
        st = srv.wait_stream(lambda s: s['state'] == 'connecting' and s['error'], 5, 0.05)
        ck('back to connecting; the error is "the source ended", not the decoder chatter before it', st is not None
           and st['error'] == 'the source ended', st)
        ck('...the timer base moved to the moment it was lost, and what the header said is forgotten', st is not None and st['since'] > live['since']
           and st['video'] is None
           and st['audio'] is None and st['width'] is None and st['viewers'] == 0, (live and live['since'], st))
        wait_until(lambda: len(srv.spawns()) >= 4, 14, 0.1)
        g = gaps(srv.spawns()[:4])
        ck('short runs keep growing the pause: the gaps between starts grow by about 1 s, then 2 s', len(g) == 3 and 0.4 <= g[1] - g[0] <= 2.6
           and 0.9 <= g[2] - g[1] <= 3.8, g)
        again = srv.wait_stream(lambda s: s['state'] == 'live', 8)
        ck('every run goes live again, with no error left', again is not None and again['error'] is None, again)
        for url, want in ((f'{FAKE}/die=2?rc=1&err=Connection%20reset%20by%20peer', 'Connection reset by peer'),
                          (f'{FAKE}/die=2?rc=3', 'ffmpeg exited with status 3'),
                          (f'{FAKE}/die=2?rc=0', 'the source ended')):
            srv.post(url)
            st = srv.wait_stream(lambda s: s['error'] and s['state'] == 'connecting', 8, 0.05)
            ck(f'{url.split("/", 3)[3]}: the error says {want!r}', st is not None and st['error'] == want, st)
        srv.call('DELETE', '/api/stream')
    finally:
        srv.stop()
    no_traceback(srv, 'source that ends')


def sc_healthy():
    ck = tagged('healthy run')
    srv = Board('healthy').start()
    try:
        srv.post(f'{FAKE}/probefail?fail=3&die=56')
        st = srv.wait_stream(lambda s: s['error'], 4)
        ck('three runs fail at the probe first; the cause is on the stream', st is not None and 'dimensions not set' in st['error'], st)
        live = srv.wait_stream(lambda s: s['state'] == 'live', 16)
        ck('the fourth plays', live is not None and live['error'] is None, live)
        wait_until(lambda: len(srv.spawns()) >= 5, 20, 0.1)
        sp = srv.spawns()
        g = gaps(sp[:5])
        ck('the pauses grew while runs failed: 1 s, 2 s, 4 s', len(g) >= 3 and 0.9 <= g[0] <= 2.6 and 1.9 <= g[1] <= 4.2 and 3.9 <= g[2] <= 7.0, g)
        ck('...and after a run that was live for over 10 s the next one starts after 1 s, not 8 (a healthy run resets it)', len(g) == 4
           and 11.0 <= g[3] <= 15.5, g)
        levels = [analyze_of(x) for x in sp[:5]]
        ck('the probe went up with each failure to the top of the ladder, and a healthy run put it back',
           levels == [1000000, 5000000, 15000000, 15000000, 1000000], levels)
        srv.call('DELETE', '/api/stream')
    finally:
        srv.stop()
    no_traceback(srv, 'healthy run')


def sc_stall():
    ck = tagged('stalled source')
    srv = Board('stall').start()
    try:
        srv.post(f'{FAKE}/stall?n=2')
        live = srv.wait_stream(lambda s: s['state'] == 'live', 5)
        r = srv.reader() if live else None
        ck('live after two fragments', live is not None and r is not None and r.wait_headers(3), live)
        pid = srv.spawns()[0]['pid']
        st = srv.wait_stream(lambda s: s['state'] == 'connecting' and s['error'], 26, 0.2)
        ck('15 s of silence from a live run: it is stopped, with an error that says so', st is not None
           and st['error'] == 'no data from the source for 15s', st)
        ck('...the viewer is ended', r is not None and r.wait_closed(3), r and r.closed)
        ck('...the silent ffmpeg is gone', wait_until(lambda: not alive(pid), 4), pid)
        ck('...and a new one is started', wait_until(lambda: len(srv.spawns()) >= 2 and srv.spawns()[1]['pid'] != pid, 4), srv.spawns())
        srv.call('DELETE', '/api/stream')
    finally:
        srv.stop()
    no_traceback(srv, 'stalled source')


def sc_stubborn():
    ck = tagged('stubborn ffmpeg')
    srv = Board('stubborn').start()
    try:
        ck('(it plays)', srv.go_live(f'{FAKE}/stubborn?pace=100', 8) is not None)
        pid = srv.spawns()[0]['pid']
        t = time.monotonic()
        srv.post(f'{FAKE}/hang?replaced=1')
        died = wait_until(lambda: not alive(pid), 8, 0.05)
        took = time.monotonic() - t
        ck(f'one that ignores SIGTERM is killed when its stream is replaced, after about 2 s ({took:.1f}s)', died and 1.5 <= took <= 5.0,
           (died, took))
        ck('...leaving only the new ffmpeg', wait_until(lambda: len(srv.kids()) == 1, 3), srv.kids())
        srv.post(f'{FAKE}/stubborn?out=0')
        wait_until(lambda: srv.spawns()[-1]['url'].endswith('out=0') and alive(srv.spawns()[-1]['pid']), 5, 0.05)
        pid = srv.spawns()[-1]['pid']
        time.sleep(0.5)
        t = time.monotonic()
        srv.call('DELETE', '/api/stream')
        died = wait_until(lambda: not alive(pid), 8, 0.05)
        took = time.monotonic() - t
        ck(f'...and so is a silent one when the stream is closed ({took:.1f}s)', died and 1.5 <= took <= 5.0 and srv.kids() == [],
           (died, took, srv.kids()))
        srv.post(f'{FAKE}/stubborn?out=0&first=1')   # silent, so it cannot notice by itself that its reader is gone
        wait_until(lambda: srv.spawns() and srv.spawns()[-1]['url'].endswith('first=1'), 5, 0.05)
        replaced = srv.spawns()[-1]['pid']
        srv.post(f'{FAKE}/hang?second=1')   # the new stream waits for the old ffmpeg to be gone, which takes 2 s: it never starts
        time.sleep(0.3)
        t = time.monotonic()
        srv.proc.send_signal(signal.SIGTERM)  # while the replaced one is still being killed
        try:
            code = srv.proc.wait(8)
        except subprocess.TimeoutExpired:
            code = None
        ck(f'SIGTERM right after a replace: the replaced stubborn ffmpeg does not outlive the board either ({time.monotonic() - t:.1f}s)',
           code == 0 and time.monotonic() - t < 5.5 and not alive(replaced) and all(not alive(x['pid']) for x in srv.spawns()),
           (code, [x['pid'] for x in srv.spawns() if alive(x['pid'])]))
        srv.stop()
        srv = Board('stubborn', port=srv.port, fresh_db=False).start()
        srv.post(f'{FAKE}/stubborn?pace=100')
        srv.wait_stream(lambda s: s['state'] == 'live', 5)
        pid = srv.spawns()[-1]['pid']
        r = srv.reader()
        r.wait_headers(3)
        t = time.monotonic()
        srv.proc.send_signal(signal.SIGTERM)
        try:
            code = srv.proc.wait(8)
        except subprocess.TimeoutExpired:
            code = None
        took = time.monotonic() - t
        ck(f'SIGTERM to the board while a stubborn ffmpeg runs with a viewer attached: exits 0 within 5 s ({took:.1f}s), pidfile removed', code == 0
           and took < 5.0 and not srv.pidfile.exists(), (code, took))
        ck('...the viewer is ended and the stubborn ffmpeg is dead', r.wait_closed(2) and not alive(pid), (r.closed, alive(pid)))
        r.close()
    finally:
        srv.stop()
    no_traceback(srv, 'stubborn ffmpeg')


def sc_garbage():
    ck = tagged('unreadable output')
    srv = Board('garbage').start()
    try:
        for query in ('garbage', 'garbage?kind=zero', 'garbage?kind=small', 'garbage?after=2', 'bigbox', 'bigbox?after=2', 'truncated'):
            srv.post(f'{FAKE}/{query}')
            before = len(srv.spawns())
            st = srv.wait_stream(lambda s: s['error'] == 'ffmpeg produced unreadable output', 8, 0.05)
            ck(f'{query}: "ffmpeg produced unreadable output", connecting again', st is not None and st['state'] == 'connecting', srv.stream())
            first = wait_until(lambda: next((x for x in srv.spawns()[before:] if x['url'].endswith(query)), None), 3)
            ck(f'{query}: that ffmpeg is stopped, not left running', first is not None and wait_until(lambda: not alive(first['pid']), 4), first)
            ck(f'{query}: another run follows', wait_until(lambda: len([x for x in srv.spawns() if x['url'].endswith(query)]) >= 2, 4),
               len(srv.spawns()))
        srv.post(f'{FAKE}/truncated?part=eof')
        st = srv.wait_stream(lambda s: s['error'], 6, 0.05)
        ck('the output ending in the middle of a fragment: "the source ended"', st is not None and st['error'] == 'the source ended', st)
        srv.post(f'{FAKE}/truncated?part=moof')
        time.sleep(2.5)
        st = srv.stream()
        ck('a moof cut off inside its first traf: nothing crashes, the board answers, the stream is still there', st is not None
           and srv.call('GET', '/api/health')[0] == 200, st)
        srv.call('DELETE', '/api/stream')
        ck('...and closing it stops its ffmpeg', wait_until(lambda: srv.kids() == [], 5), srv.kids())
    finally:
        srv.stop()
    no_traceback(srv, 'unreadable output')


def sc_probe():
    ck = tagged('probe ladder')
    srv = Board('probe').start()
    try:
        srv.post(f'{FAKE}/probefail')
        live = srv.wait_stream(lambda s: s['state'] == 'live', 10)
        sp = srv.spawns()
        ck('the first run fails at the shortest look, the second looks longer and plays', live is not None and live['error'] is None
           and [analyze_of(x) for x in sp[:2]] == [1000000, 5000000]
           and [x['argv'][x['argv'].index('-probesize') + 1] for x in sp[:2]] == ['500000', '5000000'], (live, [x['argv'] for x in sp][:2]))
        srv.post(f'{FAKE}/probeneed?us=15000000')
        live = srv.wait_stream(lambda s: s['state'] == 'live', 12)
        sp = [x for x in srv.spawns() if 'probeneed' in x['url']]
        ck('a source that needs the longest look gets there in three runs', live is not None
           and [analyze_of(x) for x in sp] == [1000000, 5000000, 15000000]
           and sp[-1]['argv'][sp[-1]['argv'].index('-probesize') + 1] == '20000000', ([analyze_of(x) for x in sp], live))
        srv.post(f'{FAKE}/probeneed?us=99999999')
        wait_until(lambda: len([x for x in srv.spawns() if x['url'].endswith('99999999')]) >= 4, 14, 0.1)
        sp = [x for x in srv.spawns() if x['url'].endswith('99999999')]
        st = srv.stream()
        ck('one that never works stays at the top of the ladder, with the cause on the stream',
           [analyze_of(x) for x in sp[:4]] == [1000000, 5000000, 15000000, 15000000]
           and st['state'] == 'connecting' and 'dimensions not set' in (st['error'] or ''), ([analyze_of(x) for x in sp], st))
        n = len(sp)
        s, h, b = srv.post(f'{FAKE}/probeneed?us=99999999')
        ok = wait_until(lambda: len([x for x in srv.spawns() if x['url'].endswith('99999999')]) > n, 4, 0.05)
        sp = [x for x in srv.spawns() if x['url'].endswith('99999999')]
        ck('posting the same URL again tries at once and starts over at the shortest look', s == 200 and ok and analyze_of(sp[n]) == 1000000,
           (s, ok, [analyze_of(x) for x in sp]))
        srv.call('DELETE', '/api/stream')
    finally:
        srv.stop()
    no_traceback(srv, 'probe ladder')


def sc_silent():
    ck = tagged('silent audio')
    srv = Board('silent').start()
    try:
        srv.post(f'{FAKE}/silent-audio')
        time.sleep(2)
        st = srv.stream()
        ck('(a source that announces audio and sends none: no header after 2 s, still connecting)', st['state'] == 'connecting'
           and st['video'] is None and st['error'] is None, st)
        sp = srv.spawns()
        ck('the first run maps the audio', len(sp) == 1 and '-map' in sp[0]['argv'] and '0:a:0?' in sp[0]['argv'] and '-an' not in sp[0]['argv'],
           sp[:1] and sp[0]['argv'])
        live = srv.wait_stream(lambda s: s['state'] == 'live', 20)
        sp = srv.spawns()
        ck('after about 9 s without a header the next run leaves the audio out, and plays', live is not None and len(sp) == 2
           and '-an' in sp[1]['argv'] and '0:a:0?' not in sp[1]['argv']
           and live['audio'] is None and live['video'] == 'avc1.42C01F' and sp[1]['t'] - sp[0]['t'] >= 8.5, (live, [x['argv'][-14:] for x in sp]))
        ck('...the first ffmpeg was stopped', not alive(sp[0]['pid']))
        r = srv.reader()
        ck('...and the media is video only', r.wait_headers(3) and r.headers.get('content-type') == 'video/mp4; codecs="avc1.42C01F"', r.headers)
        r.close()
        s, h, b = srv.post(f'{FAKE}/silent-audio')
        ck('posting the same URL again keeps the stream and tries the audio once more', s == 200 and b['existing'] is True, (s, b.get('existing')))
        ok = wait_until(lambda: len(srv.spawns()) >= 3, 6, 0.1)
        sp = srv.spawns()
        ck('...the run restarts with the audio mapped again', ok and '0:a:0?' in sp[2]['argv'] and '-an' not in sp[2]['argv'], sp[-1]['argv'])
        srv.call('DELETE', '/api/stream')
    finally:
        srv.stop()
    no_traceback(srv, 'silent audio')


def sc_dropaudio():
    ck = tagged('audio that stops')
    srv = Board('dropaudio').start()
    try:
        srv.post(f'{FAKE}/drop-audio?n=6')
        live = srv.wait_stream(lambda s: s['state'] == 'live', 5)
        r = srv.reader() if live else None
        ck('it plays with audio and video at first', live is not None and live['audio'] == 'mp4a.40.2' and r is not None and r.wait_headers(3), live)
        ck('...then the audio stops, and a few seconds later the viewers are ended (a browser would otherwise freeze on the video)', r is not None
           and r.wait_closed(9), r and (r.closed, len(r.body)))
        sp = wait_until(lambda: srv.spawns() if len(srv.spawns()) >= 2 else None, 5)
        ck('the next run leaves the audio out', sp and '-an' in sp[1]['argv'] and '0:a:0?' not in sp[1]['argv'] and sp[1]['t'] - sp[0]['t'] <= 8, sp
           and [x['t'] for x in sp])
        again = srv.wait_stream(lambda s: s['state'] == 'live' and s['audio'] is None, 8)
        ck('...and plays video only, with nothing wrong on the stream', again is not None and again['error'] is None
           and again['video'] == 'avc1.42C01F', again)
        srv.call('DELETE', '/api/stream')
    finally:
        srv.stop()
    no_traceback(srv, 'audio that stops')


def sc_firstkey():
    ck = tagged('no keyframe')
    srv = Board('firstkey').start()
    try:
        srv.post(f'{FAKE}/audio-lead?lead=1000')
        st = srv.wait_stream(lambda s: s['video'] is not None, 5)
        ck('the header comes, the keyframe does not: connecting, codecs known', st is not None and st['state'] == 'connecting', st)
        err = srv.wait_stream(lambda s: s['error'], 40, 0.25)
        ck('30 s after the header with no keyframe the run is stopped, with an error that says so', err is not None
           and err['error'] == 'no keyframe from the source within 30s', err)
        srv.call('DELETE', '/api/stream')
    finally:
        srv.stop()
    no_traceback(srv, 'no keyframe')


def sc_nofmpeg():
    ck = tagged('no ffmpeg')
    srv = Board('noffmpeg', ffmpeg='none').start()
    try:
        s, h, b = srv.call('GET', '/api/stream')
        ck('GET /api/stream: ffmpeg is null', s == 200 and b['ffmpeg'] is None and b['stream'] is None, b)
        s, h, b = srv.post(f'{FAKE}/live')
        ck('POST still opens the stream (201), with a warning that says what to install', s == 201 and b['ffmpeg'] is None
           and any('ffmpeg is not installed' in w and 'apt install ffmpeg' in w for w in b.get('warnings', [])), b)
        st = srv.wait_stream(lambda s: s['error'], 5)
        ck('the stream is connecting, and its error says so', st is not None and st['state'] == 'connecting'
           and st['error'] == 'ffmpeg is not installed on the board host (sudo apt install ffmpeg)', st)
        r = srv.reader()
        ck('a media request has nothing to wait for: after 10 s, 503 with the reason as the hint', r.wait_closed(14) and r.status == 503
           and r.headers.get('retry-after') == '2'
           and (r.json() or {}).get('hint') == 'ffmpeg is not installed on the board host (sudo apt install ffmpeg)', (r.status, r.body[:200]))
        install_fake_ffmpeg(srv.bin)
        t = time.monotonic()
        live = srv.wait_stream(lambda s: s['state'] == 'live', 16, 0.2)
        ck(f'ffmpeg appears: the stream goes live by itself, no restart ({time.monotonic() - t:.1f}s)', live is not None and live['error'] is None,
           srv.stream())
        s, h, b = srv.call('GET', '/api/stream')
        ck('GET /api/stream now names ffmpeg', b['ffmpeg'] == str(srv.bin / 'ffmpeg'), b['ffmpeg'])
        srv.call('DELETE', '/api/stream')
    finally:
        srv.stop()
    no_traceback(srv, 'no ffmpeg')
    # An ffmpeg that cannot even start.
    ck = tagged('broken ffmpeg')
    srv = Board('broken').start()
    try:
        broken = srv.bin / 'ffmpeg'
        broken.write_text('#!/nonexistent/interpreter\n')
        broken.chmod(0o755)
        srv.post(f'{FAKE}/live')
        st = srv.wait_stream(lambda s: s['error'], 5)
        ck('an ffmpeg that cannot be run: "cannot run <path>: <why>"', st is not None and st['error'].startswith(f'cannot run {broken}: ')
           and st['state'] == 'connecting', st)
        install_fake_ffmpeg(srv.bin)
        live = srv.wait_stream(lambda s: s['state'] == 'live', 12, 0.2)
        ck('...once it is replaced by one that works, the stream goes live', live is not None, srv.stream())
        srv.call('DELETE', '/api/stream')
    finally:
        srv.stop()
    no_traceback(srv, 'broken ffmpeg')


def sc_media503():
    ck = tagged('not connected yet')
    srv = Board('m503').start()
    try:
        srv.post(f'{FAKE}/hang')
        r = srv.reader()
        r.wait_headers(2)
        time.sleep(1.0)
        ck('(a media request is waiting for a header that does not come)', not r.closed and r.status is None, (r.status, r.closed))
        srv.post(f'{FAKE}/hang?replaced=1')
        ck('a request waiting for the header when the stream is replaced gets 410 at once', r.wait_closed(2) and r.status == 410
           and 'error' in (r.json() or {}), (r.status, r.body[:100]))
        t = time.monotonic()
        r = srv.reader()
        r.wait_closed(14)
        body = r.json() or {}
        ck('with no header after 10 s: 503 {error}, Retry-After 2 (and no hint: the run has not been given up yet)', r.closed and r.status == 503
           and r.headers.get('retry-after') == '2'
           and body.get('error') == 'the stream is not connected yet' and 'hint' not in body and 9.0 <= time.monotonic() - t <= 13.5,
           (r.status, r.headers, r.body[:100], time.monotonic() - t))
        st = srv.wait_stream(lambda s: s['error'], 5)
        ck('...then a source that sends nothing is given up after the look and 10 s more, and the next run leaves the audio out', st is not None
           and st['error'].startswith('no output after 11s') and 'trying again without audio' in st['error'], st)
        srv.post('http://user:s3cr3t@fake.invalid/refuse')
        st = srv.wait_stream(lambda s: s['error'], 5)
        r = srv.reader()
        ck('with an error to give, the 503 carries it as the hint, redacted', r.wait_closed(14) and r.status == 503
           and (r.json() or {}).get('hint') == (st or {}).get('error')
           and 'Connection refused' in (r.json() or {}).get('hint', '') and 's3cr3t' not in r.body.decode(), (r.status, r.body[:200]))
        srv.call('DELETE', '/api/stream')
    finally:
        srv.stop()
    no_traceback(srv, 'not connected yet')


def sc_schemes():
    ck = tagged('schemes')
    srv = Board('schemes',
                env={'http_proxy': 'http://proxy.invalid:3128', 'HTTPS_PROXY': 'http://proxy.invalid:3128', 'all_proxy': 'socks://proxy.invalid',
                     'NO_PROXY': 'x'}).start()
    try:
        micros = str(server.STREAM_IO_TIMEOUT * 1000000)
        for url in ('http://fake.invalid:8554/live', 'https://fake.invalid/live', 'rtsp://fake.invalid:554/live', 'rtsps://fake.invalid:322/live',
                    'rtmp://fake.invalid/app/live',
                    'rtmps://fake.invalid/app/live', 'srt://fake.invalid:9000?mode=caller', 'udp://239.0.0.1:1234/live', 'tcp://127.0.0.1:9000/live',
                    'HTTP://Fake.invalid/live',
                    'udp://@239.0.0.1:1234/live'):
            st = srv.go_live(url, 8)
            argv = srv.spawns()[-1]['argv']
            scheme = url.partition(':')[0].lower()
            flags_ok = ((option_value(argv, '-timeout') == micros and option_value(argv, '-rtsp_transport') == 'tcp'
                         and '-rw_timeout' not in argv) if scheme in ('rtsp', 'rtsps')
                        else (option_value(argv, '-rw_timeout') == micros and '-timeout' not in argv))
            ck(f'{url}: goes live through the fake, which refuses what ffmpeg would (an unwhitelisted protocol, rw_timeout on rtsp, timeout on rtmp)',
               st is not None and flags_ok, (st and st['error'], argv[:14]))
        ck('the ffmpegs got no proxy variable in any case', all(x['env'] == {} for x in srv.spawns()),
           [x['env'] for x in srv.spawns() if x['env']][:1])
        srv.call('DELETE', '/api/stream')
    finally:
        srv.stop()
    no_traceback(srv, 'schemes')
    for version, flag, other in (('4.4.2-0ubuntu0.22.04.1', '-stimeout', '-timeout'), ('5.1.6-0+deb12u1', '-timeout', '-stimeout'),
                                 ('N-117000-gabcdef', '-timeout', '-stimeout')):
        ck = tagged(f'ffmpeg {version}')
        srv = Board('v' + re.sub(r'\W', '', version)[:6], version=version).start()
        try:
            st = srv.go_live('rtsp://fake.invalid:554/live', 8)
            argv = srv.spawns()[-1]['argv'] if srv.spawns() else []
            ck(f'rtsp gets {flag} (the fake hangs, as ffmpeg 4 does, if it is {other}) and goes live', st is not None
               and option_value(argv, flag) == str(server.STREAM_IO_TIMEOUT * 1000000) and other not in argv, (st and st['error'], argv[:12]))
            srv.call('DELETE', '/api/stream')
        finally:
            srv.stop()
        no_traceback(srv, f'ffmpeg {version}')


# ---------------------------------------------------------------- fake: restarts, shutdown, load, HTTPS

def phase_persistence():
    S.section('persistence: the open stream is kept and reopened by the next run of the board')
    srv = Board('persist').start()
    port = srv.port
    url = 'http://user:s3cr3t@fake.invalid/hang?keep=1'
    try:
        s, h, b = srv.post(url)
        first = b['stream']
        stray = lambda: sorted(p.name for p in TMP.iterdir() if p.name.startswith('.persist') or p.name.endswith('.tmp'))  # noqa: E731
        check('a saved stream: <db>.stream.json holds {id, url (the real one), opened_at}, mode 0600, nothing else beside it',
              srv.stream_file.exists()
              and json.loads(srv.stream_file.read_text()) == {'id': first['id'], 'url': url, 'opened_at': first['opened_at']}
              and oct(srv.stream_file.stat().st_mode & 0o777) == '0o600' and stray() == [],
              (srv.stream_file.read_text() if srv.stream_file.exists() else None, stray()))
        check('...it is named after the database (server.db -> server.stream.json)', srv.stream_file.name == 'persist.stream.json'
              and srv.stream_file.parent == srv.db.parent, srv.stream_file)
        s, h, b = srv.post(url + '&second=1')
        second = b['stream']
        check('a replacing stream rewrites it, still 0600', json.loads(srv.stream_file.read_text())['id'] == second['id']
              and oct(srv.stream_file.stat().st_mode & 0o777) == '0o600'
              and stray() == [], srv.stream_file.read_text())
        s, h, b = srv.post(url + '&second=1')
        check('the same URL again: the file is as it was', json.loads(srv.stream_file.read_text())['id'] == second['id'])
        pid = wait_until(lambda: (srv.spawns() or [{}])[-1].get('pid'), 5)
        before_log = srv.log_text()
        code = srv.stop()
        check('SIGTERM: exit 0, no pidfile, no ffmpeg left', code == 0 and not srv.pidfile.exists() and pid and wait_until(lambda: not alive(pid), 3),
              (code, pid))
        check('...and the file is still there (only DELETE removes it)', srv.stream_file.exists()
              and json.loads(srv.stream_file.read_text())['id'] == second['id'])
        check('...nothing in the log about a failure', 'Traceback' not in before_log)

        again = Board('persist', port=port, fresh_db=False).start()
        st = again.stream()
        check('the next run of the board has the same stream: id, URL (redacted), opened_at', st is not None and st['id'] == second['id']
              and st['url'] == 'http://***@fake.invalid/hang?keep=1&second=1'
              and st['opened_at'] == second['opened_at'] and st['state'] == 'connecting', st)
        check('...with its ffmpeg running, and the log saying it was reopened (no password)', wait_until(lambda: len(again.kids()) == 1, 5)
              and 'reopened after a restart' in again.log_text()
              and 's3cr3t' not in again.log_text(), again.log_text()[-500:])
        check('...and a newer URL still replaces it', again.post(url + '&third=1')[2]['stream']['id'] != second['id'])
        s, h, b = again.call('DELETE', '/api/stream')
        check('DELETE removes the file', b['closed'] is True and not again.stream_file.exists())
        again.stop()
        later = Board('persist', port=port, fresh_db=False).start()
        check('...and the next run starts with no stream', later.stream() is None and later.call('GET', '/api/events')[2]['stream'] is None)
        later.stop()
        no_traceback(again, 'persist (second run)')
        no_traceback(later, 'persist (third run)')
    finally:
        srv.stop()

    S.section('persistence: a file that cannot be used is ignored with a warning, the board starts')
    good_url = f'{FAKE}/hang'
    cases = [('not JSON', b'\x00\xff this is { not json', 'ignored'), ('empty', b'', 'ignored'), ('a JSON list', b'[1, 2]', 'ignored'),
             ('a JSON string', b'"http://fake.invalid/hang"', 'ignored'), ('no url', json.dumps({'id': '0123456789ab'}).encode(), 'ignored'),
             ('a url of the wrong type', json.dumps({'url': 5, 'id': '0123456789ab'}).encode(), 'ignored'),
             ('a file: URL', json.dumps({'url': 'file:///etc/passwd', 'id': '0123456789ab', 'opened_at': 1.0}).encode(), 'ignored'),
             ('a URL with a space', json.dumps({'url': 'http://h/a b', 'id': '0123456789ab'}).encode(), 'ignored'),
             ('a URL over 2048 characters', json.dumps({'url': 'http://h/' + 'a' * 3000}).encode(), 'ignored')]
    srv = Board('corrupt')
    for label, data, warn in cases:
        srv.stream_file.write_bytes(data)
        srv.fresh_db = False
        srv.start()
        try:
            check(f'{label}: no stream, the board is up, a warning in the log', srv.stream() is None and srv.call('GET', '/api/health')[0] == 200
                  and 'WARNING' in srv.log_text() and 'stream file' in srv.log_text() and 'Traceback' not in srv.log_text() and srv.kids() == [],
                  srv.log_text()[-400:])
        finally:
            srv.stop()
    srv.stream_file.write_text(json.dumps({'url': good_url, 'id': 'zzz', 'opened_at': 'yesterday'}))
    srv.start()
    try:
        st = srv.stream()
        check('an id that is not 12 hex digits and an opened_at that is no number: the stream opens with a new id, opened_at now', st is not None
              and ID_RE.fullmatch(st['id']) and st['id'] != 'zzz'
              and abs(st['opened_at'] - time.time()) < 30 and st['url'] == good_url, st)
    finally:
        srv.stop()
    srv.stream_file.write_text(json.dumps({'url': good_url, 'id': 'AB' * 6, 'opened_at': 1234.5}))
    srv.start()
    try:
        st = srv.stream()
        check('an id in capitals is not an id (lower-case hex only): a new one; a good opened_at is kept', st is not None and st['id'] != 'AB' * 6
              and ID_RE.fullmatch(st['id']) and st['opened_at'] == 1234.5, st)
    finally:
        srv.stop()
    srv.stream_file.write_text(json.dumps({'url': good_url, 'id': '0123456789ab', 'opened_at': 1234.5}))
    srv.stream_file.chmod(0o000)
    srv.start()
    try:
        check('an unreadable file: ignored with a warning (the board runs as its owner, so this needs a non-root user)', os.geteuid() == 0
              or (srv.stream() is None and 'cannot read it' in srv.log_text()), srv.log_text()[-300:])
    finally:
        srv.stop()
    srv.stream_file.chmod(0o600)
    nof = Board('nofile', ffmpeg='none', port=srv.port, fresh_db=False)
    nof.stream_file.write_text(json.dumps({'url': good_url, 'id': '0123456789ab', 'opened_at': 1234.5}))
    nof.start()
    try:
        st = nof.stream()
        check('a stream reopened with no ffmpeg installed: it is there, connecting, and the log says what is missing', st is not None
              and st['id'] == '0123456789ab' and wait_until(lambda: nof.stream()['error'], 4)
              and 'ffmpeg is not installed, so the stream cannot play' in nof.log_text(), nof.log_text()[-400:])
    finally:
        nof.stop()


def phase_shutdown():
    S.section('shutdown with viewers attached, a SIGKILLed board, polls with 16 viewers')
    srv = Board('shutdown').start()
    try:
        srv.go_live(f'{FAKE}/live?pace=100', 10)
        pid = srv.spawns()[0]['pid']
        readers = [srv.reader() for _ in range(3)]
        check('(three viewers attached)', all(r.wait_headers(3) for r in readers) and wait_until(lambda: viewers(srv) == 3, 3), viewers(srv))
        t = time.monotonic()
        srv.proc.send_signal(signal.SIGTERM)
        try:
            code = srv.proc.wait(8)
        except subprocess.TimeoutExpired:
            code = None
        took = time.monotonic() - t
        check(f'SIGTERM: exit 0 within 5 s ({took:.2f}s), the pidfile is removed', code == 0 and took < 5 and not srv.pidfile.exists(), (code, took))
        check('...every viewer is ended, ffmpeg is gone, the stream stays saved', all(r.wait_closed(2) for r in readers) and not alive(pid)
              and srv.stream_file.exists(),
              ([r.closed for r in readers], alive(pid)))
        check('...the log shows a clean shutdown', 'shutting down' in srv.log_text() and 'Traceback' not in srv.log_text(), srv.log_text()[-500:])
        for r in readers:
            r.close()
    finally:
        srv.stop()

    srv = Board('polls').start()
    try:
        srv.go_live(f'{FAKE}/live?pace=50&size=100000&ksize=100000&gop=20', 10)
        readers = [srv.reader() for _ in range(16)]
        check('(16 viewers attached to a stream of 2 MB/s each)', all(r.wait_headers(3) for r in readers)
              and wait_until(lambda: viewers(srv) == 16, 3), viewers(srv))
        lat = {'events': [], 'stream': [], 'health': []}
        for _ in range(25):
            for path, key in (('/api/events', 'events'), ('/api/stream', 'stream'), ('/api/health', 'health')):
                t = time.monotonic()
                s = srv.call('GET', path)[0]
                lat[key].append(time.monotonic() - t if s == 200 else 99)
            time.sleep(0.05)
        for key, values in lat.items():
            values.sort()
            worst, median = values[-1] * 1000, values[len(values) // 2] * 1000
            check(f'polls of {key} with 16 viewers attached: worst {worst:.0f} ms, median {median:.0f} ms (limits 600 ms and 150 ms)',
                  values[-1] < 0.6 and values[len(values) // 2] < 0.15, [round(x, 3) for x in values])
        check('...and the viewers kept receiving', all(r.wait_bytes(500000, 8) for r in readers), [len(r.body) for r in readers])
        for r in readers:
            r.close()
    finally:
        srv.stop()
    no_traceback(srv, 'polls')

    srv = Board('killed').start()
    try:
        srv.go_live(f'{FAKE}/live?pace=100', 10)
        pid = srv.spawns()[0]['pid']
        os.kill(srv.proc.pid, signal.SIGKILL)
        srv.proc.wait(5)
        check('SIGKILL of the board while ffmpeg writes: ffmpeg notices its reader is gone and exits by itself (within 3 s)',
              wait_until(lambda: not alive(pid), 3), pid)
        check('...and the saved stream is still there for the next run', srv.stream_file.exists())
    finally:
        srv.stop()
        if alive(pid):
            os.kill(pid, signal.SIGKILL)


def phase_https():
    S.section('the media route over the HTTPS listener')
    made = self_signed_cert(TMP)
    if made is None:
        S.skip('media over https', 'openssl not installed (or it could not make a self-signed certificate)')
        return
    cert, key, _ = made
    tp = free_port()
    srv = Board('https', '--tls-port', str(tp), '--tls-cert', str(cert), '--tls-key', str(key)).start()
    try:
        st = srv.go_live(f'{FAKE}/live?pace=100', 10)
        r = stream_get(tp, st['media'], tls=True)
        check('200 over TLS, the same Content-Type and headers', r.wait_headers(5) and r.status == 200
              and r.headers.get('content-type') == 'video/mp4; codecs="avc1.42C01F,mp4a.40.2"'
              and r.headers.get('cache-control') == 'no-store' and 'content-length' not in r.headers and has_headers(r.headers, docs_version()),
              (r.status, r.headers, r.error))
        fr = frags_of(r, 10)
        check('...and the same fragments, from a keyframe, without a gap', len(fr) >= 10 and keyframe(fr[0]) and seqs_ok(fr),
              [f['seq'] for f in fr][:5])
        plain = srv.reader()
        check('...next to a plain-http viewer of the same stream', plain.wait_headers(5) and viewers(srv) == 2, viewers(srv))
        s, h, b = call_tls(tp, 'GET', '/api/stream')
        check('GET /api/stream over TLS shows both', s == 200 and b['stream']['viewers'] == 2, b)
        s, h, b = call_tls(tp, 'POST', '/api/stream', {'url': f'{FAKE}/hang?tls=1'})
        check('POST over TLS opens a stream (and ends the viewers)', s == 201 and r.wait_closed(3) and plain.wait_closed(3), (s, b))
        r.close()
        plain.close()
    finally:
        srv.stop()
    no_traceback(srv, 'https')


def call_tls(port, method, path, body=None):
    from harness import call
    return call(port, method, path, body, tls=True)


# ---------------------------------------------------------------- real ffmpeg, over loopback

def whole_boxes(body: bytes) -> bytes:
    """body cut after its last complete top-level box."""
    boxes = split_boxes(body)
    return body[:boxes[-1][2]] if boxes else b''


def listener(output: list, url: str, seconds: int = 30) -> subprocess.Popen:
    """A real ffmpeg that serves a test picture and sound to the first client to connect to url (rtmp, tcp, srt...).
    Its command line mentions TMP, so reap() finds it if a test dies."""
    argv = [FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostdin', '-re', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30', '-f', 'lavfi',
            '-i',
            'sine=frequency=440:sample_rate=48000', '-t', str(seconds), '-c:v', 'libx264', '-profile:v', 'baseline', '-pix_fmt', 'yuv420p', '-g',
            '30',
            '-c:a', 'aac', '-b:a', '64k', '-ac', '2', '-metadata', f'comment={TMP}', *output, url]
    return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


_PROTOCOLS: list = []


def has_protocol(name: str) -> bool:
    """Whether FFMPEG was built with the protocol (srt needs libsrt, which not every build has)."""
    if not _PROTOCOLS:
        try:
            out = subprocess.run([FFMPEG, '-hide_banner', '-protocols'], capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            out = ''
        _PROTOCOLS.append({line.strip() for line in out.splitlines() if line.startswith('  ')})
    return name in _PROTOCOLS[0]


def stop_proc(proc) -> None:
    if proc is not None and proc.poll() is None:
        proc.kill()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            pass


def rc_sources():
    ck = tagged('real source')
    made = self_signed_cert(TMP)
    tp = free_port()
    extra = ['--tls-port', str(tp), '--tls-cert', str(made[0]), '--tls-key', str(made[1])] if made else []
    srv = Board('r-src', *extra, ffmpeg='real').start()
    try:
        for kind in ('video', 'av', 'audio'):
            ts = make_ts(kind, 6)
            if ts is None:
                S.skip(f'real source: {kind}', 'could not make the test stream')
                continue
            want_video, want_audio = kind != 'audio', kind != 'video'
            up = FakeUpstream(ts, marker=TMP)
            try:
                t0 = time.monotonic()
                srv.post(up.url)
                st = srv.wait_stream(lambda s: s['state'] == 'live', 30)
                ck(f'{kind}: live within 30 s ({time.monotonic() - t0:.1f}s)', st is not None, srv.stream())
                if st is None:
                    continue
                ck(f'{kind}: the header tells the codecs and the size', (st['video'] or '').startswith('avc1.') == want_video
                   and (st['audio'] == 'mp4a.40.2') == want_audio
                   and ((st['width'], st['height']) == (320, 180)) == want_video and st['error'] is None, st)
                r = srv.reader()
                mime = f'{"video" if want_video else "audio"}/mp4; codecs="' + ','.join(c for c in (st['video'], st['audio']) if c) + '"'
                ck(f'{kind}: Content-Type {mime}', r.wait_headers(5) and r.headers.get('content-type') == mime, r.headers)
                frags = frags_of(r, 20, 20)
                data = whole_boxes(r.body)
                info = probe(data)
                streams = {x['codec_type']: x for x in (info or {}).get('streams', [])}
                ck(f'{kind}: ffprobe reads the relayed bytes: ' + ', '.join(f'{k} {v["codec_name"]}' for k, v in streams.items()),
                   info is not None and ('video' in streams) == want_video and ('audio' in streams) == want_audio
                   and (not want_video
                        or (streams['video']['codec_name'], streams['video']['width'], streams['video']['height']) == ('h264', 320, 180))
                   and (not want_audio or (streams['audio']['codec_name'], streams['audio']['channels']) == ('aac', 2)), info
                   and [(x['codec_type'], x['codec_name']) for x in info['streams']])
                ck(f'{kind}: the codec strings are ffprobe\'s',
                   all(streams[k]['mime_codec_string'].lower() == c.lower() for k, c in (('video', st['video']), ('audio', st['audio'])) if c),
                   streams and {k: v['mime_codec_string'] for k, v in streams.items()})
                ck(f'{kind}: it starts with a start point and has no gap in its fragment numbers', len(frags) >= 20 and seqs_ok(frags)
                   and keyframe(frags[0], 1 if want_video else None), [(f['seq'], f['sync']) for f in frags][:4])
                if want_video:
                    keys = [f['seq'] for f in frags if keyframe(f)]
                    ck(f'{kind}: a keyframe fragment about every second (every fifth fragment): {keys[:5]}', len(keys) >= 3
                       and all(3 <= b - a <= 7 for a, b in zip(keys, keys[1:])), keys)
                else:
                    ck(f'{kind}: every fragment of an audio-only stream is a start point', all(keyframe(f, None) for f in frags))
                ck(f'{kind}: viewers are served from one upstream connection; one ffmpeg', up.connections == 1 and len(srv.kids()) == 1
                   and srv.stream()['viewers'] == 1, (up.connections, srv.kids()))
                if kind == 'av' and made:
                    rt = stream_get(tp, st['media'], tls=True)
                    ck('av: the same over the HTTPS listener', rt.wait_headers(5) and rt.status == 200 and rt.headers.get('content-type') == mime
                       and frags_of(rt, 5) and keyframe(frags_of(rt, 5)[0]), (rt.status, rt.headers))
                    rt.close()
                r.close()
                srv.call('DELETE', '/api/stream')
                ck(f'{kind}: closing the stream closes the upstream connection and ends ffmpeg',
                   wait_until(lambda: up.active == 0 and srv.kids() == [], 6), (up.active, srv.kids()))
            finally:
                up.close()
    finally:
        srv.stop()
    no_traceback(srv, 'real source')


def rc_midgop():
    ck = tagged('real source joined mid-GOP')
    srv = Board('r-mid', ffmpeg='real').start()
    try:
        ts = make_ts('video', 12, gop=300)
        mid = ts_from(ts, 8.5) if ts else None
        if mid is None:
            S.skip('real source joined mid-GOP', 'could not make the test stream')
            return
        up = FakeUpstream(mid, marker=TMP)
        try:
            t0 = time.monotonic()
            srv.post(up.url)
            seen = srv.wait_stream(lambda s: s['error'] and 'dimensions not set' in s['error'], 12)
            ck('a keyframe 1.5 s away is more than the first look (1 s) sees: the run fails, and the error is the cause, not the decoder chatter',
               seen is not None and seen['state'] == 'connecting' and 'PPS' not in seen['error'] and 'no frame' not in seen['error'], seen)
            st = srv.wait_stream(lambda s: s['state'] == 'live', 45)
            ck(f'the next run looks longer and goes live ({time.monotonic() - t0:.1f}s)', st is not None and st['error'] is None
               and (st['width'], st['height']) == (320, 180), srv.stream())
            ck('...after at least two connections to the source', up.connections >= 2, up.connections)
            r = srv.reader()
            fr = frags_of(r, 10, 15)
            ck('...and the viewer gets a keyframe first', fr and keyframe(fr[0]) and seqs_ok(fr), fr and [(f['seq'], f['sync']) for f in fr][:3])
            r.close()
            srv.call('DELETE', '/api/stream')
        finally:
            up.close()
    finally:
        srv.stop()
    no_traceback(srv, 'real source joined mid-GOP')


def rc_silent():
    ck = tagged('real source with announced audio that never comes')
    srv = Board('r-silent', ffmpeg='real').start()
    try:
        silent = ts_without_audio(make_ts('av', 8))
        if silent is None:
            S.skip('announced audio that never comes', 'could not make the test stream')
            return
        up = FakeUpstream(silent, marker=TMP)
        try:
            t0 = time.monotonic()
            srv.post(up.url)
            time.sleep(3)
            ck('(ffmpeg waits for the audio, writes nothing: still connecting, no error)', srv.stream()['state'] == 'connecting'
               and srv.stream()['video'] is None, srv.stream())
            st = srv.wait_stream(lambda s: s['state'] == 'live', 40)
            ck(f'about 9 s without a header, the next run leaves the audio out and plays the video ({time.monotonic() - t0:.1f}s)', st is not None
               and st['audio'] is None
               and (st['video'] or '').startswith('avc1.') and st['error'] is None, srv.stream())
            ck('...after two connections to the source', up.connections == 2, up.connections)
            r = srv.reader()
            ck('...and the media is video only', r.wait_headers(5) and r.headers.get('content-type', '').startswith('video/mp4; codecs="avc1.')
               and ',' not in r.headers['content-type'], r.headers)
            r.close()
            srv.call('DELETE', '/api/stream')
        finally:
            up.close()
    finally:
        srv.stop()
    no_traceback(srv, 'announced audio that never comes')


def rc_failures():
    ck = tagged('real ffmpeg, failing sources')
    srv = Board('r-fail', ffmpeg='real').start()
    ts = make_ts('av', 6)
    try:
        up = FakeUpstream(ts, 'refuse', marker=TMP)
        try:
            srv.post(up.url)
            st = srv.wait_stream(lambda s: s['error'], 8)
            ck('a refused connection: connecting, with ffmpeg\'s reason', st is not None and st['state'] == 'connecting'
               and 'Connection refused' in st['error'] and '0x' not in st['error'], st)
            time.sleep(2)
            up.serve()
            live = srv.wait_stream(lambda s: s['state'] == 'live', 25)
            ck('...and when the source comes back the stream goes live by itself', live is not None and live['error'] is None, srv.stream())
            srv.call('DELETE', '/api/stream')
        finally:
            up.close()
        for mode, needle in (('404', 'Server returned 404'), ('500', 'Server returned 5XX'), ('html', 'Invalid data found'),
                             ('redirect', 'not on whitelist')):
            up = FakeUpstream(ts, mode, marker=TMP, location='file:///etc/hostname')
            try:
                srv.post(up.url)
                st = srv.wait_stream(lambda s: s['error'], 10)
                ck(f'{mode}: connecting, and the error says {needle!r}', st is not None and st['state'] == 'connecting' and needle in st['error'], st)
                time.sleep(0.5)
                ck(f'{mode}: never live', srv.stream()['state'] == 'connecting' and up.connections >= 1, srv.stream())
            finally:
                up.close()
        up = FakeUpstream(ts, 'close', marker=TMP, close_after=3000)
        try:
            srv.post(up.url)
            st = srv.wait_stream(lambda s: s['error'], 10)
            ck('a source that hangs up after 3 KB of garbage-to-ffmpeg: connecting with some error, not live', st is not None
               and st['state'] == 'connecting' and st['error'], st)
        finally:
            up.close()
        up = FakeUpstream(ts, 'blackhole', marker=TMP)
        try:
            t0 = time.monotonic()
            srv.post(up.url)
            time.sleep(2)
            ck('a black hole: accepted, never answered: connecting, nothing wrong yet', up.connections >= 1 and srv.stream()['state'] == 'connecting'
               and srv.stream()['error'] is None, (up.connections, srv.stream()))
            st = srv.wait_stream(lambda s: s['error'], 18)
            ck(f'...after the I/O timeout (10 s) the run fails with a timeout ({time.monotonic() - t0:.1f}s)', st is not None
               and 9 <= time.monotonic() - t0 <= 17 and 'timed out' in st['error'].lower(), st)
        finally:
            srv.call('DELETE', '/api/stream')
            up.close()
        ck('after all this ffmpeg is not left running', wait_until(lambda: srv.kids() == [], 5), srv.kids())
    finally:
        srv.stop()
    no_traceback(srv, 'real ffmpeg, failing sources')


def rc_fanout():
    ck = tagged('real source, five viewers')
    srv = Board('r-fan', ffmpeg='real').start()
    try:
        up = FakeUpstream(make_ts('av', 6), marker=TMP)
        try:
            srv.post(up.url)
            st = srv.wait_stream(lambda s: s['state'] == 'live', 30)
            readers = [srv.reader() for _ in range(5)] if st else []
            ck('five viewers are served', len(readers) == 5 and all(r.wait_headers(5) and r.status == 200 for r in readers),
               [r.status for r in readers])
            time.sleep(9)  # past the end of the 6 s source: it starts over, and the stream must not notice
            ck('...from one connection to the source and one ffmpeg', up.connections == 1 and len(srv.kids()) == 1, (up.connections, srv.kids()))
            ck('...and they are 5 viewers on the board', viewers(srv) == 5, viewers(srv))
            parsed = [frags_of(r, 30, 15) for r in readers]
            ck('each got an unbroken sequence from a keyframe, across the source starting over',
               all(len(fr) >= 30 and keyframe(fr[0]) and seqs_ok(fr) for fr in parsed), [[f['seq'] for f in fr][:3] for fr in parsed])
            tail = [fr[-1]['seq'] for fr in parsed]
            ck('...they all have the same fragments, byte for byte (compared by number)',
               all(f['bytes'] == next((g['bytes'] for g in parsed[0] if g['seq'] == f['seq']), f['bytes']) for fr in parsed[1:] for f in fr)
               and max(tail) - min(tail) <= 8, tail)
            ck('...and the stream never left live', srv.stream()['state'] == 'live' and srv.stream()['error'] is None, srv.stream())
            for r in readers:
                r.close()
            srv.call('DELETE', '/api/stream')
        finally:
            up.close()
    finally:
        srv.stop()
    no_traceback(srv, 'real source, five viewers')


def rc_protocols():
    ck = tagged('real ffmpeg, other protocols')
    srv = Board('r-proto', ffmpeg='real').start()
    procs = []
    try:
        for name, output, template in (('rtmp', ['-f', 'flv', '-listen', '1'], 'rtmp://127.0.0.1:{p}/live/x'),
                                       ('tcp', ['-f', 'mpegts'], 'tcp://127.0.0.1:{p}?listen=1'),
                                       ('srt', ['-f', 'mpegts'], 'srt://127.0.0.1:{p}?mode=listener')):
            if not has_protocol(name):
                S.skip(f'real ffmpeg, {name}', f'this ffmpeg was built without {name}')
                continue
            port = free_port()
            listen_url = template.format(p=port)
            procs.append(listener(output, listen_url))
            time.sleep(0.8)
            url = listen_url.replace('?listen=1', '').replace('mode=listener', 'mode=caller')
            t0 = time.monotonic()
            srv.post(url)
            st = srv.wait_stream(lambda s: s['state'] == 'live', 30)
            ck(f'{name}: {url} goes live ({time.monotonic() - t0:.1f}s)', st is not None and (st['video'] or '').startswith('avc1.')
               and st['audio'] == 'mp4a.40.2' and st['error'] is None, srv.stream())
            r = srv.reader()
            fr = frags_of(r, 8, 15)
            ck(f'{name}: and its media is fMP4 from a keyframe', len(fr) >= 8 and keyframe(fr[0]) and seqs_ok(fr), fr
               and [(f['seq'], f['sync']) for f in fr][:3])
            r.close()
            srv.call('DELETE', '/api/stream')
            ck(f'{name}: closing ends ffmpeg', wait_until(lambda: srv.kids() == [], 5), srv.kids())
            stop_proc(procs[-1])
    finally:
        for p in procs:
            stop_proc(p)
        srv.stop()
    no_traceback(srv, 'real ffmpeg, other protocols')


def rc_rtsp():
    ck = tagged('real ffmpeg, rtsp')
    port = free_port()
    url = f'rtsp://127.0.0.1:{port}/live'
    pubs = []

    def run_once(drop_option: bool):
        argv = server.ffmpeg_argv(FFMPEG, url, 0, True, 8)
        i = argv.index('-i')
        argv[i:i] = ['-rtsp_flags', 'listen', '-listen_timeout', '10']
        if drop_option is False:
            argv[argv.index('-rtsp_transport'):argv.index('-rtsp_transport')] = ['-rw_timeout', str(server.STREAM_IO_TIMEOUT * 1000000)]
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        pubs.append(proc)
        out, err = bytearray(), bytearray()

        def rd(f, buf):
            while True:
                c = os.read(f.fileno(), 65536)
                if not c:
                    break
                buf += c
        readers = [threading.Thread(target=rd, args=(f, buf), daemon=True) for f, buf in ((proc.stdout, out), (proc.stderr, err))]
        for t in readers:
            t.start()
        time.sleep(0.8)
        pub = subprocess.Popen([FFMPEG, '-hide_banner', '-loglevel', 'error', '-nostdin', '-re', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30',
                                '-f', 'lavfi', '-i',
                                'sine=frequency=440:sample_rate=48000', '-t', '4', '-c:v', 'libx264', '-profile:v', 'baseline', '-pix_fmt', 'yuv420p',
                                '-g', '30', '-c:a', 'aac',
                                '-b:a', '64k', '-metadata', f'comment={TMP}', '-f', 'rtsp', '-rtsp_transport', 'tcp', url], stdin=subprocess.DEVNULL,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               start_new_session=True)
        pubs.append(pub)
        try:
            proc.wait(25)
        except subprocess.TimeoutExpired:
            proc.kill()
        stop_proc(pub)
        for t in readers:
            t.join(3)
        return proc.returncode, bytes(out), bytes(err).decode('utf-8', 'replace')
    try:
        rc, out, err = run_once(True)
        events = feed_all(server.FragmentReader(), out, 4096)
        ck('the board\'s rtsp command line (+ -rtsp_flags listen) takes a stream from a publisher: ftyp, moov, fragments, exit 0', rc == 0 and events
           and events[0][0] == 'init'
           and len([e for e in events if e[0] == 'fragment']) >= 5 and 'not found' not in err, (rc, len(out), err[-300:]))
        ck('...codecs avc1 and mp4a.40.2', events and events[0][2].startswith('video/mp4; codecs="avc1.') and events[0][2].endswith(',mp4a.40.2"'),
           events and events[0][2])
        rc, out, err = run_once(False)
        ck('control: the same with -rw_timeout (what the first draft passed) fails as the review found it would, "Option rw_timeout not found"',
           rc != 0 and 'Option rw_timeout not found' in err and not out, (rc, len(out), err[-200:]))
    finally:
        for p in pubs:
            stop_proc(p)


def rc_orphans():
    ck = tagged('real ffmpeg, a board that dies')
    # SIGKILL while live: ffmpeg's next write fails, it exits by itself.
    srv = Board('r-orph1', ffmpeg='real').start()
    pid = None
    try:
        up = FakeUpstream(make_ts('av', 6), marker=TMP)
        try:
            srv.post(up.url)
            ok = srv.wait_stream(lambda s: s['state'] == 'live', 30)
            pid = (srv.kids() or [None])[0]
            ck('(live, with one ffmpeg child)', ok is not None and pid is not None, srv.kids())
            if pid:
                os.kill(srv.proc.pid, signal.SIGKILL)
                srv.proc.wait(5)
                t = time.monotonic()
                gone = wait_until(lambda: not alive(pid), 12, 0.1)
                ck(f'SIGKILL of the board while live: ffmpeg is gone by itself, within a few seconds ({time.monotonic() - t:.1f}s)', gone
                   and time.monotonic() - t < 6, alive(pid))
                ck('...and the source\'s connection with it', wait_until(lambda: up.active == 0, 3), up.active)
        finally:
            up.close()
            if pid and alive(pid):
                os.kill(pid, signal.SIGKILL)
    finally:
        srv.stop()
    # SIGKILL while connecting to a source that never answers: ffmpeg's own I/O timeout ends it.
    srv = Board('r-orph2', ffmpeg='real').start()
    pid = None
    try:
        up = FakeUpstream(make_ts('av', 6), 'blackhole', marker=TMP)
        try:
            srv.post(up.url)
            pid = wait_until(lambda: (find_procs(up.url) or [None])[0], 5)   # the ffmpeg reading it (not the `ffmpeg -version` before)
            time.sleep(1)
            ck('(connecting to a black hole, one ffmpeg child)', pid is not None and up.connections >= 1 and srv.kids() == [pid],
               (pid, srv.kids(), up.connections))
            if pid:
                os.kill(srv.proc.pid, signal.SIGKILL)
                srv.proc.wait(5)
                t = time.monotonic()
                limit = server.STREAM_IO_TIMEOUT + 2
                gone = wait_until(lambda: not alive(pid), limit + 5, 0.2)
                took = time.monotonic() - t
                ck(f'SIGKILL of the board while connecting: ffmpeg gives up by its own timeout, within {limit} s of the start ({took:.1f}s after the kill)',
                   gone and took <= limit, (alive(pid), took))
        finally:
            up.close()
            if pid and alive(pid):
                os.kill(pid, signal.SIGKILL)
    finally:
        srv.stop()
    # SIGTERM with viewers attached.
    srv = Board('r-orph3', ffmpeg='real').start()
    try:
        up = FakeUpstream(make_ts('av', 6), marker=TMP)
        try:
            srv.post(up.url)
            srv.wait_stream(lambda s: s['state'] == 'live', 30)
            pid = (srv.kids() or [None])[0]
            readers = [srv.reader() for _ in range(3)]
            [r.wait_headers(5) for r in readers]
            t = time.monotonic()
            srv.proc.send_signal(signal.SIGTERM)
            try:
                code = srv.proc.wait(8)
            except subprocess.TimeoutExpired:
                code = None
            took = time.monotonic() - t
            ck(f'SIGTERM with three viewers on a live stream: exit 0 within 5 s ({took:.1f}s), no pidfile', code == 0 and took < 5
               and not srv.pidfile.exists(), (code, took))
            ck('...ffmpeg is gone, the viewers are ended, the stream is kept for the next run', pid is not None and not alive(pid)
               and all(r.wait_closed(2) for r in readers) and srv.stream_file.exists(),
               (pid, [r.closed for r in readers]))
            for r in readers:
                r.close()
        finally:
            up.close()
    finally:
        srv.stop()
    no_traceback(srv, 'real ffmpeg, a board that dies')


def phase_real():
    if not (FFMPEG and FFPROBE and has_encoder('libx264')):
        S.skip('the real-ffmpeg phase', 'ffmpeg, ffprobe or libx264 not installed (set FFMPEG / FFPROBE to use others)')
        return
    real_ffmpeg_dir(TMP)
    for kind in ('video', 'av', 'audio'):
        make_ts(kind, 6)
    make_ts('av', 8)
    run_group('real ffmpeg over loopback: sources, joining mid-GOP, silent audio, failures, five viewers, rtmp/tcp/srt/rtsp, a board that dies',
              rc_sources, rc_midgop, rc_silent, rc_failures, rc_fanout, rc_protocols, rc_rtsp, rc_orphans, limit=300)


# ---------------------------------------------------------------- units: the classes behind the route

def phase_units_channel():
    S.section('StreamViewer: a bounded queue the supervisor never waits for')
    v = server.StreamViewer()
    check('empty: next() times out with b"" (not None, which means closed)', v.next(0) == b'' and v.next(0.05) == b'')
    check('offer queues in order; next hands them out in order', all(v.offer(bytes([i])) for i in range(3))
          and [v.next(0) for _ in range(3)] == [b'\x00', b'\x01', b'\x02'])
    v = server.StreamViewer()
    check(f'{server.STREAM_QUEUE_MAX} fragments may wait', all(v.offer(b'x' * 10) for _ in range(server.STREAM_QUEUE_MAX))
          and len(v.items) == server.STREAM_QUEUE_MAX and not v.closed)
    check('one more: offer says no, the viewer is closed and dropped, its queue cleared', v.offer(b'y') is False and v.closed and v.dropped
          and not v.items and v.next(0) is None)
    check('...and a closed viewer refuses more', v.offer(b'z') is False)
    v = server.StreamViewer()
    v.offer(b'a')
    v.end()
    check('end(): closed but not dropped, the queue cleared, next() says closed, offer says no', v.closed and not v.dropped and not v.items
          and v.next(0) is None and v.offer(b'b') is False)
    v, got = server.StreamViewer(), []
    waiter = threading.Thread(target=lambda: got.append(v.next(5)))
    waiter.start()
    time.sleep(0.15)
    t = time.monotonic()
    v.offer(b'data')
    waiter.join(2)
    check('a waiting next() wakes when a fragment is offered', got == [b'data'] and time.monotonic() - t < 1, got)
    got.clear()
    waiter = threading.Thread(target=lambda: got.append(v.next(5)))
    waiter.start()
    time.sleep(0.15)
    t = time.monotonic()
    v.end()
    waiter.join(2)
    check('...and when the viewer is ended (within a moment, not the 5 s it asked to wait)', got == [None] and time.monotonic() - t < 1,
          (got, time.monotonic() - t))

    S.section('StreamChannel: snapshot, attach and stop (no ffmpeg involved)')
    chan = server.StreamChannel('0123456789ab', 'http://user:s3cr3t@h:8554/x', 1234.5)
    snap = chan.snapshot()
    check('a new channel: connecting, nothing known, the URL redacted, the media path', list(snap) == STREAM_KEYS and snap['id'] == '0123456789ab'
          and snap['url'] == 'http://***@h:8554/x'
          and snap['opened_at'] == 1234.5 and snap['state'] == 'connecting' and snap['error'] is None
          and snap['video'] is snap['audio'] is snap['width'] is snap['height'] is None
          and snap['viewers'] == 0 and snap['media'] == '/api/stream/media?id=0123456789ab', snap)
    check('...it keeps the real URL for ffmpeg', chan.url == 'http://user:s3cr3t@h:8554/x' and chan.shown == 'http://***@h:8554/x')
    _, moov = init_parts()
    info = server.mp4_init_info(moov)
    chan.init, chan.mime, chan.tracks = b'INIT', 'video/mp4; codecs="x"', info['tracks']
    snap = chan.snapshot()
    check('once a header is in, the snapshot has its codecs and size',
          (snap['video'], snap['audio'], snap['width'], snap['height']) == ('avc1.42C01F', 'mp4a.40.2', 320, 180), snap)
    chan.gop = [b'k', b'p1']
    viewer, init, gop, mime = chan.attach(0.1)
    check('attach: the viewer, the init segment, a copy of the GOP cache, the MIME type', init == b'INIT' and gop == [b'k', b'p1']
          and gop is not chan.gop and mime == 'video/mp4; codecs="x"'
          and viewer in chan.viewers and chan.snapshot()['viewers'] == 1)
    chan.detach(viewer)
    chan.detach(viewer)
    check('detach is idempotent', viewer not in chan.viewers and chan.snapshot()['viewers'] == 0)
    chan.gop = None
    check('no GOP cache (it grew past its limit): attach gives an empty list', chan.attach(0.1)[2] == [])
    chan.viewers.clear()
    chan.viewers.update(server.StreamViewer() for _ in range(server.STREAM_VIEWERS_MAX))
    try:
        chan.attach(0.1)
        check('attach with 16 viewers: 503', False, 'no error')
    except server.ApiError as exc:
        check('attach with 16 viewers: 503, Retry-After 10, the limit in the message', exc.code == 503 and exc.headers == {'Retry-After': '10'}
              and '16' in exc.payload['error'], (exc.code, exc.headers, exc.payload))
    chan.viewers.clear()
    chan.init = chan.mime = None
    for error, hint in ((None, None), ('Connection refused', 'Connection refused')):
        chan.error = error
        t = time.monotonic()
        try:
            chan.attach(0.2)
            check('attach with no header yet: 503', False, 'no error')
        except server.ApiError as exc:
            check(f'attach with no header yet, error {error!r}: 503 after the wait, Retry-After 2, the hint only when there is an error',
                  exc.code == 503 and exc.headers == {'Retry-After': '2'}
                  and exc.payload.get('hint') == hint and ('hint' in exc.payload) == (hint is not None)
                  and exc.payload['error'] == 'the stream is not connected yet' and 0.15 <= time.monotonic() - t < 2, (exc.payload, exc.headers))
    outcome = []

    def wait_in_attach():
        try:
            chan.attach(5)
            outcome.append('attached')
        except server.ApiError as exc:
            outcome.append(exc.code)
    waiter = threading.Thread(target=wait_in_attach)
    waiter.start()
    time.sleep(0.2)
    check('a request waiting for the header is counted as a reservation', chan.waiting == 1, chan.waiting)
    t = time.monotonic()
    chan.stop()
    waiter.join(2)
    check('stop() wakes it with a 410, at once', outcome == [410] and time.monotonic() - t < 1 and chan.waiting == 0, (outcome, chan.waiting))
    ended = [server.StreamViewer(), server.StreamViewer()]
    chan2 = server.StreamChannel('ba9876543210', 'http://h/x', 1.0)
    chan2.viewers.update(ended)
    chan2.stop()
    check('stop() ends every viewer and forgets them', all(x.closed and not x.dropped for x in ended) and not chan2.viewers and chan2.stopped)
    try:
        chan2.attach(0.1)
        check('attach on a stopped channel: 410', False, 'no error')
    except server.ApiError as exc:
        check('attach on a stopped channel: 410 {error}', exc.code == 410, exc.payload)
    chan2.stop()
    check('stop() twice is fine', chan2.stopped and not chan2.viewers and chan2.stopping.is_set())


if __name__ == '__main__':
    phase_units_urls()
    phase_units_redaction()
    phase_units_argv()
    phase_units_errors()
    phase_units_mp4()
    phase_units_reader()
    phase_units_real_mp4()
    phase_units_channel()
    phase_api()
    phase_media()
    run_group('the supervisor: backoff and its reset, stalls, stubborn processes, garbage, the probe ladder, audio, no ffmpeg', sc_backoff, sc_die,
              sc_healthy, sc_stall,
              sc_stubborn, sc_garbage, sc_probe, sc_silent, sc_dropaudio, sc_firstkey, sc_nofmpeg, sc_media503, sc_schemes)
    phase_persistence()
    phase_shutdown()
    phase_https()
    phase_real()
    sys.exit(S.finish())
