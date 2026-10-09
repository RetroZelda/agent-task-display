#!/usr/bin/env python3
"""A stand-in for ffmpeg, for the stream suites. Standard library only, Python 3.10+.

The board looks ffmpeg up on its PATH every time it starts one (shutil.which), so a copy of this file named
`ffmpeg` in a directory put first on the board's PATH is what runs (harness.fake_ffmpeg_dir makes it, with an
absolute `#!<python>` first line, so that PATH needs no /usr/bin). It takes what the board passes (ffmpeg_argv in
server.py: the source URL after `-i`, `-an` or the audio mapping, the probe size, the timeout options), plays
the run the URL asks for as fragmented MP4 on stdout, and writes ffmpeg's own kind of messages on stderr. The
box layouts are those of real ffmpeg 8 output (checked with ffprobe): ftyp + moov (a trex per track, a udta),
a moof + mdat per fragment, a keyframe fragment's trun carrying first_sample_flags 0x2000000 and the others
tfhd default_sample_flags 0x1010000, 4-byte descriptor lengths in esds, an mfra when a run ends.

The URL picks the run: its first path segment is the mode, its query the parameters.

    http://fake.invalid/live?w=320&h=180&pace=200&gop=10&marker=A

Modes (an unknown name plays as `live`):
    live          video and audio (audio=0 or `-an`: video only); the first fragment is a keyframe
    audio         audio only: every fragment is a start point
    audio-lead    as live, but the first `lead` (5) fragments carry audio only, then comes a keyframe fragment
                  whose video track has one sample: what the real source does (ffmpeg drops the video before
                  its first keyframe)
    drop-audio    as live, but the fragments stop carrying the audio track after `n` (8) of them
    silent-audio  nothing is ever written while audio is mapped (an announced track that sends nothing keeps
                  ffmpeg from writing its header); with `-an` it plays as live
    hang          alive, writes nothing
    stubborn      as live, but ignores SIGTERM (only SIGKILL stops it); out=0: writes nothing
    refuse        "Connection refused" on stderr, with the URL (credentials and all) echoed as ffmpeg does; exit 145
    error         the same for another cause: kind=refused|dns|404|500|invalid|timeout|whitelist (refused); the
                  exit status is the one ffmpeg gives for it
    die=N         as live, but the source ends after N (3) fragments: an mfra, then exit status `rc` (0: "the
                  source ended"), after `err` on stderr if given. `die=N` also works as a parameter of any
                  mode that plays
    stall         as live, then silent after `n` (2) fragments, still alive
    garbage       bytes that are no box: kind=text|zero|small, after=<fragments played first> (0)
    bigbox        a box header claiming 512 MiB after `after` (0) fragments, then silence
    truncated     part=moov (default)|moof|eof: a box whose contents are cut off (moov and moof keep a correct
                  outer size), or half a fragment and the end of the output
    hevc          as live with an hev1 sample entry (the board names it bare, which no page will play)
    probefail     the first `fail` (1) runs of this URL print "dimensions not set" and exit 1, as a long GOP
                  joined mid-way does at the shortest probe; later ones play as live
    probeneed     fails while -analyzeduration is below `us` (5000000), whatever the number of runs

Parameters of every mode that plays: w, h (video size, 320x180), fps (30), pace (ms per fragment, 200), gop
(fragments from one keyframe to the next, 10), size and ksize (bytes of video in a plain and in a keyframe
fragment, 3000 and 12000), marker (one character in every fragment's data, A), initdelay (ms before the header,
0), first (ms from the header to the first fragment, 0), codec (profile, constraints and level of avc1 as six hex
digits, 42c01f), rate (audio sample rate, 48000), aot (audio object type, 2), esds (bytes in a descriptor's
length, 4), noise (lines of decoder chatter on stderr first, 0), rc and err (see die).

Every fragment's mdat starts with `FAKE`, `K` (the fragment starts the video with a keyframe) or `.`, the
marker, the process id (4 bytes) and the moof's sequence number (4 bytes), so a reader can tell the runs and the
order apart. The sample data after it is filler: a decoder cannot play it, a demuxer reads it.

Options it checks as ffmpeg does: the URL's scheme must be on -protocol_whitelist ("Protocol 'x' not on
whitelist"); `-rw_timeout` with an rtsp URL fails ("Option rw_timeout not found."); `-timeout` with an rtmp URL
makes it listen for a connection (it hangs); for rtsp, `-stimeout` is only known before ffmpeg 5 and `-timeout`
means listen before it. `-version` prints $FAKE_FFMPEG_VERSION (default 7.1.1-fake).

Every run appends {pid, ppid, argv, env, t, url, mode} as a JSON line to $FAKE_FFMPEG_LOG (env: the proxy
variables it was given). SIGTERM ends it as it ends ffmpeg (exit 255); a closed stdout ends it quietly.

As a library (the tests import it): box, box64, fbox, build_init, build_fragment, build_mfra, sample_stream, and the
pieces mvhd, mvex, trak, tfhd, tfdt, trun, traf, moof, mdat for layouts the fake itself never writes.
"""
from __future__ import annotations

import json
import os
import re
import signal
import struct
import sys
import time
from urllib.parse import parse_qs, unquote, urlsplit

VERSION = '7.1.1-fake'
NEEDS = {  # the protocols each scheme needs on -protocol_whitelist
    'http': ('http', 'tcp'), 'https': ('https', 'tls', 'tcp'), 'rtsp': ('rtsp', 'tcp'), 'rtsps': ('rtsps', 'tls', 'tcp'),
    'rtmp': ('rtmp', 'tcp'), 'rtmps': ('rtmps', 'tls', 'tcp'), 'srt': ('srt',), 'udp': ('udp',), 'tcp': ('tcp',),
}
PORTS = {'http': 80, 'https': 443, 'rtsp': 554, 'rtsps': 322, 'rtmp': 1935, 'rtmps': 443}
FREQUENCIES = (96000, 88200, 64000, 48000, 44100, 32000, 24000, 22050, 16000, 12000, 11025, 8000)
# Sample flags as movenc writes them: SYNC depends on no other sample (a keyframe, or any audio sample), DELTA is
# a difference frame (sample_is_non_sync_sample).
SYNC, DELTA = 0x02000000, 0x01010000
# The SPS and PPS of a 320x180 baseline stream x264 made; the board reads only avcC's three profile bytes.
SPS = bytes.fromhex('6742c00dd901419f9f0110000003001000000303c0f142a480')
PPS = bytes.fromhex('68cb83cb20')
HVCC = bytes([1, 1, 0x60, 0, 0, 0, 0x90, 0, 0, 0, 0, 0, 0x5a, 0xf0, 0, 0xfc, 0xfd, 0xf8, 0xf8, 0, 0, 0x0f, 0])  # no parameter sets
MATRIX = struct.pack('>9I', 0x10000, 0, 0, 0, 0x10000, 0, 0, 0, 0x40000000)


# ---------------------------------------------------------------- boxes

def box(typ: bytes, *parts: bytes) -> bytes:
    payload = b''.join(parts)
    return struct.pack('>I4s', 8 + len(payload), typ) + payload


def box64(typ: bytes, *parts: bytes) -> bytes:
    """A box in the 64-bit size form (size field 1, the real size after the type)."""
    payload = b''.join(parts)
    return struct.pack('>I4sQ', 1, typ, 16 + len(payload)) + payload


def fbox(typ: bytes, version: int, flags: int, *parts: bytes) -> bytes:
    """A full box: version and flags first."""
    return box(typ, struct.pack('>I', version << 24 | flags), *parts)


def descriptor(tag: int, payload: bytes, length_bytes: int = 4) -> bytes:
    """An MPEG-4 descriptor. ffmpeg writes its length in 4 bytes (80 80 80 xx), other muxers in 1."""
    n = len(payload)
    if length_bytes == 1:
        return bytes([tag, n]) + payload
    return bytes([tag, 0x80 | n >> 21 & 0x7f, 0x80 | n >> 14 & 0x7f, 0x80 | n >> 7 & 0x7f, n & 0x7f]) + payload


def visual_entry(fourcc: bytes, width: int, height: int, *children: bytes) -> bytes:
    """A video sample entry: 78 bytes of its own, then its boxes (avcC, pasp...)."""
    return box(fourcc, bytes(6), struct.pack('>H', 1), bytes(16), struct.pack('>HHIIIH', width, height, 0x480000, 0x480000, 0, 1),
               bytes(32), struct.pack('>Hh', 0x18, -1), *children)


def audio_entry(channels: int, rate: int, *children: bytes) -> bytes:
    """An audio sample entry: 28 bytes of its own, then its boxes (esds, btrt)."""
    return box(b'mp4a', bytes(6), struct.pack('>H', 1), bytes(8), struct.pack('>HHHHI', channels, 16, 0, 0, rate << 16), *children)


def avcc(codec: str = '42c01f') -> bytes:
    profile, compat, level = bytes.fromhex(codec)
    sps = SPS[:1] + bytes([profile, compat, level]) + SPS[4:]
    return box(b'avcC', bytes([1, profile, compat, level, 0xff, 0xe1]), struct.pack('>H', len(sps)), sps, b'\x01',
               struct.pack('>H', len(PPS)), PPS)


def esds(rate: int = 48000, channels: int = 2, aot: int = 2, length_bytes: int = 4, extension: bool = True) -> bytes:
    """The esds box of an AAC track: an AudioSpecificConfig of audio object type aot, with the SBR hint ffmpeg's
    encoder appends (which leaves the object type as it is)."""
    config = struct.pack('>H', aot << 11 | FREQUENCIES.index(rate) << 7 | channels << 3) + (b'\x56\xe5\x00' if extension else b'')
    decoder = bytes([0x40, 0x15]) + bytes(3) + struct.pack('>II', 128000, 128000) + descriptor(5, config, length_bytes)
    es = struct.pack('>HB', 2, 0) + descriptor(4, decoder, length_bytes) + descriptor(6, b'\x02', length_bytes)
    return fbox(b'esds', 0, 0, descriptor(3, es, length_bytes))


def trak(track_id: int, kind: str, entry: bytes, timescale: int, width: int = 0, height: int = 0) -> bytes:
    video = kind == 'vide'
    tkhd = fbox(b'tkhd', 0, 3, struct.pack('>IIIIIQHHHH', 0, 0, track_id, 0, 0, 0, 0, 0, 0 if video else 0x100, 0), MATRIX,
                struct.pack('>II', width << 16, height << 16))
    mdhd = fbox(b'mdhd', 0, 0, struct.pack('>IIIIHH', 0, 0, timescale, 0, 0x55c4, 0))
    hdlr = fbox(b'hdlr', 0, 0, bytes(4), kind.encode(), bytes(12), b'VideoHandler\0' if video else b'SoundHandler\0')
    header = fbox(b'vmhd', 0, 1, bytes(8)) if video else fbox(b'smhd', 0, 0, bytes(4))
    dinf = box(b'dinf', fbox(b'dref', 0, 0, struct.pack('>I', 1), fbox(b'url ', 0, 1)))
    stbl = box(b'stbl', fbox(b'stsd', 0, 0, struct.pack('>I', 1), entry), fbox(b'stts', 0, 0, bytes(4)), fbox(b'stsc', 0, 0, bytes(4)),
               fbox(b'stsz', 0, 0, bytes(8)), fbox(b'stco', 0, 0, bytes(4)))
    return box(b'trak', tkhd, box(b'mdia', mdhd, hdlr, box(b'minf', header, dinf, stbl)))


def mvhd(next_track: int = 3) -> bytes:
    return fbox(b'mvhd', 0, 0, struct.pack('>IIII', 0, 0, 1000, 0), struct.pack('>IH', 0x10000, 0x100), bytes(10), MATRIX, bytes(24),
                struct.pack('>I', next_track))


def mvex(flags: dict) -> bytes:
    """The movie extends box: a trex per track from {track id: default sample flags}."""
    return box(b'mvex', *[fbox(b'trex', 0, 0, struct.pack('>IIIII', i, 1, 0, 0, f)) for i, f in flags.items()])


def track_ids(video: bool, audio: bool) -> dict:
    """{'vide': id, 'soun': id} as ffmpeg numbers them: the video is track 1, the audio 2 (1 when it is alone)."""
    ids = {}
    if video:
        ids['vide'] = 1
    if audio:
        ids['soun'] = 2 if video else 1
    return ids


def build_init(video=(320, 180), audio: bool = True, fourcc: str = 'avc1', codec: str = '42c01f', rate: int = 48000,
               channels: int = 2, aot: int = 2, esds_length: int = 4, trex: bool = True) -> bytes:
    """ftyp + moov of a fragmented MP4 (-movflags empty_moov). video is (width, height) or None."""
    ids = track_ids(video is not None, audio)
    traks = []
    if video is not None:
        if fourcc == 'hev1':
            entry = visual_entry(b'hev1', *video, box(b'hvcC', HVCC))
        else:
            entry = visual_entry(fourcc.encode(), *video, avcc(codec), box(b'pasp', struct.pack('>II', 1, 1)))
        traks.append(trak(ids['vide'], 'vide', entry, 90000, *video))
    if audio:
        entry = audio_entry(channels, rate, esds(rate, channels, aot, esds_length), box(b'btrt', struct.pack('>III', 0, 128000, 128000)))
        traks.append(trak(ids['soun'], 'soun', entry, rate))
    udta = box(b'udta', fbox(b'meta', 0, 0, fbox(b'hdlr', 0, 0, bytes(4), b'mdir', b'appl', bytes(9)),
                             box(b'ilst', box(b'\xa9too', fbox(b'data', 0, 1, bytes(4), b'Lavf-fake')))))
    ftyp = box(b'ftyp', b'iso5', struct.pack('>I', 0x200), b'iso5iso6mp41')
    extends = mvex({i: 0 for i in ids.values()}) if trex else b''
    return ftyp + box(b'moov', mvhd(len(ids) + 1), *traks, extends, udta)


# ---------------------------------------------------------------- fragments

def mfhd(seq: int) -> bytes:
    return fbox(b'mfhd', 0, 0, struct.pack('>I', seq))


def tfhd(track_id: int, *, base_offset=None, description=None, duration=None, size=None, flags=None, base_is_moof: bool = True) -> bytes:
    """A track fragment header, with the optional fields that are given, in the order the standard puts them."""
    bits, body = 0x20000 if base_is_moof else 0, b''
    for bit, value, fmt in ((0x1, base_offset, '>Q'), (0x2, description, '>I'), (0x8, duration, '>I'), (0x10, size, '>I'), (0x20, flags, '>I')):
        if value is not None:
            bits |= bit
            body += struct.pack(fmt, value)
    return fbox(b'tfhd', 0, bits, struct.pack('>I', track_id), body)


def tfdt(base: int, version: int = 1) -> bytes:
    return fbox(b'tfdt', version, 0, struct.pack('>Q' if version else '>I', base))


def trun(count=None, *, data_offset=None, first_flags=None, durations=None, sizes=None, flags=None, offsets=None, version: int = 0) -> bytes:
    """A track run. A per-sample list (durations, sizes, flags, offsets) puts that field in every sample."""
    lists = [x for x in (durations, sizes, flags, offsets) if x is not None]
    if count is None:
        count = len(lists[0])
    bits = ((0x1 if data_offset is not None else 0) | (0x4 if first_flags is not None else 0) | (0x100 if durations is not None else 0)
            | (0x200 if sizes is not None else 0) | (0x400 if flags is not None else 0) | (0x800 if offsets is not None else 0))
    body = struct.pack('>I', count)
    if data_offset is not None:
        body += struct.pack('>i', data_offset)
    if first_flags is not None:
        body += struct.pack('>I', first_flags)
    for i in range(count):
        for values, fmt in ((durations, '>I'), (sizes, '>I'), (flags, '>I'), (offsets, '>i' if version else '>I')):
            if values is not None:
                body += struct.pack(fmt, values[i])
    return fbox(b'trun', version, bits, body)


def traf(*children: bytes) -> bytes:
    return box(b'traf', *children)


def moof(*children: bytes) -> bytes:
    return box(b'moof', *children)


def mdat(payload: bytes) -> bytes:
    return box(b'mdat', payload)


def build_fragment(seq: int, parts: list, header: bytes = b'') -> bytes:
    """moof + mdat. parts is one dict per track fragment, in order:
        {'id': track id, 'base': decode time, 'dur': default sample duration, 'sizes': [one per sample],
         'sync': whether the first sample is a keyframe, 'video': False for an audio track (all its samples sync)}
    header is written over the first bytes of the mdat. The layout is movenc's: a keyframe fragment's first sample
    has first_sample_flags (trun 0x4), the others come from tfhd default_sample_flags; per-sample sizes are left
    out of a one-sample run."""
    def trafs(offset: int) -> list:
        out = []
        for part in parts:
            sizes, video = part['sizes'], part.get('video', True)
            out.append(traf(tfhd(part['id'], duration=part['dur'], size=sizes[0], flags=DELTA if video else SYNC), tfdt(part['base']),
                            trun(len(sizes), data_offset=offset, first_flags=SYNC if part['sync'] and video else None,
                                 sizes=None if len(sizes) == 1 else sizes)))
            offset += sum(sizes)
        return out
    head = moof(mfhd(seq), *trafs(0))  # its size does not depend on the offsets, so a second pass fixes them
    head = moof(mfhd(seq), *trafs(len(head) + 8))
    data = bytes(sum(sum(part['sizes']) for part in parts))
    return head + mdat(header + data[len(header):])


def build_mfra(entries: dict) -> bytes:
    """An mfra: a tfra per track from {track id: [(time, moof offset)]}, then the mfro holding its own size."""
    tfras = b''.join(fbox(b'tfra', 1, 0, struct.pack('>III', tid, 0, len(rows)), b''.join(struct.pack('>QQBBB', t, off, 1, 1, 1) for t, off in rows))
                     for tid, rows in entries.items())
    return box(b'mfra', tfras, fbox(b'mfro', 0, 0, struct.pack('>I', 8 + len(tfras) + 16)))


# ---------------------------------------------------------------- what a URL asks for

class Params:
    def __init__(self, query: str):
        self.q = {k: v[-1] for k, v in parse_qs(query, keep_blank_values=True).items()}

    def int(self, name: str, default: int = 0) -> int:
        try:
            return int(self.q.get(name, default))
        except ValueError:
            return default

    def str(self, name: str, default: str = '') -> str:
        return self.q.get(name, default)

    def flag(self, name: str, default: bool = True) -> bool:
        return self.q.get(name, '1' if default else '0').lower() not in ('0', 'false', 'no', 'off', '')


def parse_url(url: str):
    """(mode, Params) of a source URL: the first path segment is the mode, `die=5` meaning mode die with die=5."""
    parts = urlsplit(url)
    segment = unquote(parts.path.strip('/').split('/')[0]).lower()
    params = Params(parts.query)
    if '=' in segment:
        segment, _, value = segment.partition('=')
        params.q.setdefault(segment, value)
    return segment or 'live', params


def major_of(version: str):
    match = re.match(r'n?(\d+)', version)
    return int(match.group(1)) if match else None


class Terminated(BaseException):
    """SIGTERM arrived (the handler raises it, wherever the run was)."""


class Run:
    """The fragments one URL asks for. Without the output methods it is a plain generator of bytes (sample_stream)."""

    def __init__(self, argv: list, url: str, mode: str, p: Params):
        self.argv, self.url, self.mode, self.p = argv, url, mode, p
        self.pos = 0                      # bytes written (the mfra points into them)
        self.seq = 0
        self.marker = (p.str('marker', 'A') or 'A')[:1].encode()
        self.pace = max(p.int('pace', 200), 1) / 1000
        self.has_audio = '-an' not in argv and p.flag('audio', True)
        self.has_video = mode != 'audio'
        self.size = (p.int('w', 320), p.int('h', 180))
        self.fps = max(p.int('fps', 30), 1)
        self.rate = p.int('rate', 48000)
        self.vtime = self.atime = 0       # decode times in track ticks
        self.afrac = 0.0                  # audio samples owed to the next fragment
        self.keys = {}                    # {track id: [(time, offset)]}: where the mfra points
        self.ids = track_ids(self.has_video, self.has_audio)
        self.started = self.writing = False

    # -- what is written
    def init(self) -> bytes:
        return build_init(self.size if self.has_video else None, self.has_audio, fourcc='hev1' if self.mode == 'hevc' else 'avc1',
                          codec=self.p.str('codec', '42c01f'), rate=self.rate, aot=self.p.int('aot', 2), esds_length=self.p.int('esds', 4))

    def fragment(self, *, video: bool, audio: bool, sync: bool, frames: int | None = None) -> bytes:
        """The next fragment: video (frames samples, a keyframe first if sync) and audio for one pace."""
        parts = []
        if video and self.has_video:
            n = frames if frames is not None else max(1, round(self.pace * self.fps))
            total = self.p.int('ksize', 12000) if sync else self.p.int('size', 3000)
            sizes = [max(total // n, 16)] * n
            sizes[0] += max(total - sum(sizes), 0)
            dur = 90000 // self.fps
            parts.append({'id': self.ids['vide'], 'base': self.vtime, 'dur': dur, 'sizes': sizes, 'sync': sync, 'video': True})
            self.vtime += dur * n
        if audio and self.has_audio:
            self.afrac += self.pace * self.rate / 1024
            n = max(int(self.afrac), 1)
            self.afrac = max(self.afrac - n, 0.0)
            parts.append({'id': self.ids['soun'], 'base': self.atime, 'dur': 1024, 'sizes': [340] * n, 'sync': True, 'video': False})
            self.atime += 1024 * n
        self.seq += 1
        keyed = sync and video and self.has_video
        header = b'FAKE' + (b'K' if keyed else b'.') + self.marker + struct.pack('>II', os.getpid(), self.seq)
        for part in parts:
            if part['sync']:
                self.keys.setdefault(part['id'], []).append((part['base'], self.pos))
        return build_fragment(self.seq, parts, header)

    def plan(self) -> dict:
        """How the mode shapes the fragments: audio-only ones first (lead), the audio stopping (drop_after), ..."""
        p, mode = self.p, self.mode
        n = p.int('n', 0)
        return {'lead': p.int('lead', 5) if mode == 'audio-lead' else 0, 'drop_after': (n or 8) if mode == 'drop-audio' else None,
                'die_after': (n or 3) if mode == 'die' else (p.int('die', 0) or None), 'stall_after': (n or 2) if mode == 'stall' else None,
                'garbage_after': p.int('after', 0) if mode == 'garbage' else None, 'bigbox_after': p.int('after', 0) if mode == 'bigbox' else None}

    def schedule(self, k: int, lead: int = 0, drop_after: int | None = None) -> dict:
        """The arguments of fragment() for the k-th fragment."""
        gop = max(self.p.int('gop', 10), 1)
        video = k >= lead
        return {'video': video, 'audio': drop_after is None or k < drop_after, 'sync': video and (k == lead or (k - lead) % gop == 0),
                'frames': 1 if lead and k == lead else None}

    # -- output
    def put(self, data: bytes) -> None:
        self.writing = True
        try:
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
        except (OSError, ValueError):
            os._exit(224)  # the reader is gone (ffmpeg: "Broken pipe", status 224): nothing to tell anyone
        self.writing = False
        self.pos += len(data)

    def say(self, text: str) -> None:
        try:
            sys.stderr.write(text + '\n')
            sys.stderr.flush()
        except (OSError, ValueError):
            pass

    def hang(self) -> None:
        while True:
            time.sleep(3600)

    def addr(self) -> str:
        return f'0x55{os.getpid() * 4096:010x}'

    def noise(self, lines: int) -> None:
        cycle = (f'[h264 @ {self.addr()}] non-existing PPS 0 referenced', '    Last message repeated 1 times', f'[h264 @ {self.addr()}] no frame!')
        for i in range(lines):
            self.say(cycle[i % 3])

    # -- the runs
    def play(self) -> int:
        plan, p = self.plan(), self.p
        time.sleep(max(p.int('initdelay', 0), 0) / 1000)
        self.put(self.init())
        self.started = True
        t = time.monotonic() + p.int('first', 0) / 1000
        k = 0
        while True:
            time.sleep(max(0.0, t - time.monotonic()))
            if plan['garbage_after'] is not None and k >= plan['garbage_after']:
                return self.garbage()
            if plan['bigbox_after'] is not None and k >= plan['bigbox_after']:
                self.put(struct.pack('>I4s', 512 << 20, b'mdat') + bytes(4096))
                self.hang()
            if plan['die_after'] is not None and k >= plan['die_after']:
                return self.finish()
            if plan['stall_after'] is not None and k >= plan['stall_after']:
                self.hang()
            self.put(self.fragment(**self.schedule(k, plan['lead'], plan['drop_after'])))
            k += 1
            t += self.pace

    def finish(self) -> int:
        """The source ended: ffmpeg writes the trailer (an mfra) and leaves."""
        self.write_mfra()
        if self.p.str('err'):
            self.say(self.p.str('err'))
        return self.p.int('rc', 0)

    def write_mfra(self) -> None:
        if self.keys:
            self.put(build_mfra(self.keys))

    def garbage(self) -> int:
        kind = self.p.str('kind', 'text')
        self.put({'zero': bytes(64), 'small': struct.pack('>I4s', 4, b'free') + bytes(32)}.get(kind, b'This is not an MP4 file at all. ' * 8))
        self.hang()
        return 0

    def truncated(self) -> int:
        part = self.p.str('part', 'moov')
        init = self.init()
        ftyp = struct.unpack('>I', init[:4])[0]
        if part == 'moov':  # a correct outer size around the first 200 bytes of the moov: mid-way through the first trak
            moov = init[ftyp:ftyp + 208]
            self.put(init[:ftyp] + struct.pack('>I', len(moov)) + moov[4:])
        else:
            self.put(init)
            self.started = True
            frag = self.fragment(video=True, audio=self.has_audio, sync=True)
            head = struct.unpack('>I', frag[:4])[0]
            if part == 'moof':  # the moof cut off inside its first traf, the mdat intact
                cut = frag[8:head][:44]
                self.put(struct.pack('>I4s', 8 + len(cut), b'moof') + cut + frag[head:])
            else:  # the output ends in the middle of a fragment
                self.put(frag[:head + 20])
                return 0
        self.hang()
        return 0

    def error(self, kind: str) -> int:
        netloc = urlsplit(self.url).netloc.rpartition('@')[2]
        if re.search(r':\d+$', netloc):
            host, port = netloc.rpartition(':')[0], netloc.rpartition(':')[2]
        else:
            host, port = netloc, str(PORTS.get(self.url.partition(':')[0].lower(), 80))
        if kind == 'dns':
            lines, reason, rc = [f'[tcp @ {self.addr()}] Failed to resolve hostname {host}: Name or service not known'], 'Input/output error', 251
        elif kind == '404':
            lines, reason, rc = [], 'Server returned 404 Not Found', 8
        elif kind == '500':
            lines, reason, rc = [], 'Server returned 5XX Server Error reply', 8
        elif kind == 'invalid':
            lines, reason, rc = [], 'Invalid data found when processing input', 183
        elif kind == 'timeout':
            lines, reason, rc = [f'[http @ {self.addr()}] Error reading HTTP response: Connection timed out'], 'Connection timed out', 146
        elif kind == 'whitelist':
            lines, reason, rc = [f"[http @ {self.addr()}] Protocol 'file' not on whitelist '{whitelist(self.argv)}'!"], 'Invalid argument', 234
        else:
            lines, reason, rc = [f'[tcp @ {self.addr()}] Connection to tcp://{host}:{port} failed: Connection refused'], 'Connection refused', 145
        for line in lines + [f'[in#0 @ {self.addr()}] Error opening input: {reason}', f'Error opening input file {self.url}.',
                             f'Error opening input files: {reason}']:
            self.say(line)
        return rc

    def probe_error(self) -> int:
        """What ffmpeg says when the source's streams are not known by the end of the probe: no header is written."""
        for line in (f'[mp4 @ {self.addr()}] dimensions not set',
                     f'[out#0/mp4 @ {self.addr()}] Could not write header (incorrect codec parameters ?): Invalid argument',
                     f'[af#0:1 @ {self.addr()}] Error sending frames to consumers: Invalid argument',
                     f'[out#0/mp4 @ {self.addr()}] Nothing was written into output file, because at least one of its streams received no packets.'):
            self.say(line)
        return 1


def whitelist(argv: list) -> str:
    return argv[argv.index('-protocol_whitelist') + 1] if '-protocol_whitelist' in argv else ''


def sample_stream(count: int = 12, url: str = 'http://fake.invalid/live', argv: list | None = None) -> list:
    """[init segment, fragment, fragment...]: `count` fragments of the run url asks for, with no pacing and no stdout."""
    mode, p = parse_url(url)
    run = Run(list(argv or []), url, mode, p)
    plan = run.plan()
    out = [run.init()]
    for k in range(count):
        out.append(run.fragment(**run.schedule(k, plan['lead'], plan['drop_after'])))
        run.pos += len(out[-1])
    return out


# ---------------------------------------------------------------- the process

def log_spawn(argv: list, url: str, mode: str) -> int:
    """Append this run to $FAKE_FFMPEG_LOG; -> how many runs of this URL it had logged before."""
    path = os.environ.get('FAKE_FFMPEG_LOG')
    if not path:
        return 0
    before = 0
    try:
        with open(path, encoding='utf-8', errors='replace') as f:
            for line in f:
                try:
                    before += json.loads(line).get('url') == url
                except ValueError:
                    pass
    except OSError:
        pass
    env = {k: v for k, v in os.environ.items() if k.lower().endswith('_proxy')}
    record = {'pid': os.getpid(), 'ppid': os.getppid(), 'argv': argv, 'env': env, 't': time.time(), 'url': url, 'mode': mode}
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        os.write(fd, (json.dumps(record) + '\n').encode())
    finally:
        os.close(fd)
    return before


def on_term(signum, frame):
    raise Terminated(signum)


def refusal(run: Run, args: list, major) -> int | None:
    """The exit status when ffmpeg would refuse this command line before reading anything, else None."""
    url = run.url
    scheme = url.partition(':')[0].lower()
    if '-protocol_whitelist' in args:
        allowed = whitelist(args).split(',')
        for need in NEEDS.get(scheme, (scheme,)):
            if need not in allowed:
                run.say(f"[{need} @ {run.addr()}] Protocol '{need}' not on whitelist '{whitelist(args)}'!")
                run.say(f'Error opening input file {url}.')
                return 234
    if scheme in ('rtsp', 'rtsps'):
        # RTSP is a demuxer: an option only a protocol knows is an unused one, and ffmpeg refuses those once the input is open.
        for option in ('-rw_timeout', '-stimeout' if major is None or major >= 5 else ''):
            if option and option in args:
                run.say(f'Option {option[1:]} not found.')
                run.say(f'Error opening input file {url}.')
                run.say('Error opening input files: Option not found')
                return 8
        if '-timeout' in args and major is not None and major < 5:
            run.hang()  # before ffmpeg 5, -timeout made rtsp wait for a connection
    elif scheme in ('rtmp', 'rtmps') and '-timeout' in args:
        run.hang()      # for rtmp, -timeout means "listen"
    return None


def run_mode(run: Run, args: list, before: int) -> int:
    mode, p = run.mode, run.p
    if not (run.has_video or run.has_audio):  # an audio-only source asked to leave its audio out
        run.say(f'[out#0/mp4 @ {run.addr()}] Output file does not contain any stream')
        return 234
    if mode == 'refuse':
        return run.error('refused')
    if mode == 'error':
        return run.error(p.str('kind', 'refused'))
    if mode == 'probefail' and before < p.int('fail', 1):
        return run.probe_error()
    if mode == 'probeneed':
        analyze = int(args[args.index('-analyzeduration') + 1]) if '-analyzeduration' in args else 0
        if analyze < p.int('us', 5000000):
            return run.probe_error()
    if mode == 'hang' or (mode == 'stubborn' and p.str('out') == '0') or (mode == 'silent-audio' and run.has_audio):
        run.hang()
    if mode == 'truncated':
        return run.truncated()
    return run.play()


def main(argv: list) -> int:
    args = argv[1:]
    version = os.environ.get('FAKE_FFMPEG_VERSION', VERSION)
    if '-version' in args:
        print(f'ffmpeg version {version} Copyright (c) 2000-2026 the FFmpeg developers\nbuilt with a fake')
        return 0
    if '-i' not in args or args.index('-i') + 1 >= len(args):
        sys.stderr.write('At least one input file must be specified\n')
        return 1
    url = args[args.index('-i') + 1]
    mode, p = parse_url(url)
    before = log_spawn(args, url, mode)
    run = Run(args, url, mode, p)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    if mode == 'stubborn':
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        run.noise(p.int('noise', 0))
        status = refusal(run, args, major_of(version))
        return run_mode(run, args, before) if status is None else status
    except Terminated:
        run.say('Exiting normally, received signal 15.')
        if run.started and not run.writing:
            try:
                run.write_mfra()
            except Terminated:
                pass
        return 255


if __name__ == '__main__':
    code = main(sys.argv)
    try:
        sys.stdout.flush()
    except (OSError, ValueError):
        code = 224
    os._exit(code)  # no interpreter shutdown: a closed stdout must not print "Exception ignored" at exit
