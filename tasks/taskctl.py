#!/usr/bin/env python3
"""taskctl: report progress on long-running work to the task-status board.

Reporting is a side channel and must never break the real work. stdout carries only machine values
(IDs, URLs; tables for show/list; the board's text for usage/changelog/api), everything else goes to
stderr as "taskctl: ...". Board errors (an unusable board URL or TASKS_TIMEOUT included) are warnings
that exit 0 (--strict / TASKS_STRICT=1: 1 = rejected, 3 = unreachable); usage errors always exit 2.
When add (or new, if the board rejected it or may have missed it) cannot get IDs it prints `offline`
per expected ID, and any command given the ID `offline` does nothing, so captured IDs keep working in
scripts.

Offline queue: a write whose connection failed (so the board certainly never saw it) is kept in a
spool file and replayed in order, at its original time, by the next command that reaches the board or
by `taskctl flush`. new picks the request ID itself, so the IDs it prints are real even offline. One
process replays at a time and nobody waits for it: meanwhile reads go straight to the board and
writes queue behind the replay.

Self-update: an installed skill (SKILL.md beside this file) replaces itself with the board's current
SKILL.md, taskctl and taskctl.py when a reply from its own board (the baked URL, never another one
named by --url or TASKS_URL) says the agent instructions changed, and says so on stderr.
TASKS_NO_UPDATE=1 turns that off; `taskctl update` does it on demand.

Board URL: --url, $TASKS_URL, the address baked in when the board served this file, then
http://127.0.0.1:8765. Python 3.8+ syntax and the standard library only: this runs on remote machines.
"""
from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import http.client
import json
import math
import os
import re
import secrets
import shlex
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import warnings
from typing import Optional

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # everything else
    msvcrt = None

# The board replaces these placeholders when it serves the file: its address, the version of its agent
# instructions (the skill, the templates and this CLI) and its API version.
BAKED_URL = "{{BASE_URL}}"
BAKED_DOCS = "{{DOCS_VERSION}}"
BAKED_API = "{{VERSION}}"
DEFAULT_URL = 'http://127.0.0.1:8765'
DEFAULT_TIMEOUT = 3.0

ID_CHARS = '23456789abcdefghjkmnpqrstuvwxyz'
REQUEST_RE = re.compile('[%s]{6}' % ID_CHARS)
TASK_RE = re.compile('[%s]{6}-[0-9]+' % ID_CHARS)
OFFLINE = 'offline'
ALIASES = {'task': 'add', 'complete': 'done', 'finish': 'done', 'failed': 'fail',
           'status': 'show', 'ls': 'list'}

RULE_BEGIN = '<!-- task-status:begin -->'
RULE_END = '<!-- task-status:end -->'
RULE_BEGIN_RE = re.compile(r'^[ \t]*%s[ \t\r]*$' % re.escape(RULE_BEGIN), re.M)
RULE_BLOCK_RE = re.compile(r'^[ \t]*%s[ \t\r]*$.*?^[ \t]*%s[ \t\r]*$\n?'
                           % (re.escape(RULE_BEGIN), re.escape(RULE_END)), re.M | re.S)

DOCS_HEADER = 'X-Tasks-Docs'
REPLAY_HEADER = 'X-Tasks-Replay'
DOCS_RE = re.compile(r'[0-9a-f]{4,64}')
SERVED_DOCS_RE = re.compile(rb'^BAKED_DOCS = "([^"\r\n]*)"', re.M)
CHANGELOG_ENTRY_RE = re.compile(r'^## v(\d+)\b', re.M)
# The installed skill, as the board serves it: file name, how it must start, mode.
SKILL_FILES = (('SKILL.md', b'---', 0o644), ('taskctl', b'#!', 0o755), ('taskctl.py', None, 0o755))
API_PATH_RE = re.compile(r'/api/[^\s\x00-\x1f\x7f]*')
# A queued write: /api/tasks/<tid>/progress, /api/requests/<rid>/complete and so on.
WRITE_PATH_RE = re.compile(r'/api/(tasks|requests)/([^/?#]+)/(progress|complete|attention)')
# connect() failed: the call certainly never reached the board, so it is safe to queue and replay.
# EPERM and EACCES are a local firewall dropping the connection (a VPN kill switch, say).
CONNECT_ERRNOS = frozenset(getattr(errno, name) for name in (
    'ECONNREFUSED', 'ENETUNREACH', 'EHOSTUNREACH', 'EADDRNOTAVAIL', 'ENETDOWN', 'EPERM', 'EACCES',
    'WSAECONNREFUSED', 'WSAENETUNREACH', 'WSAEHOSTUNREACH', 'WSAEADDRNOTAVAIL', 'WSAENETDOWN', 'WSAEACCES')
    if hasattr(errno, name))
# A lock someone else holds, as flock (EWOULDBLOCK) and msvcrt (EACCES, EDEADLOCK) report it.
LOCK_BUSY_ERRNOS = frozenset(getattr(errno, name) for name in ('EWOULDBLOCK', 'EAGAIN', 'EACCES', 'EDEADLK', 'EDEADLOCK')
                             if hasattr(errno, name))
LOCK_WAIT = 1.0           # seconds to wait for the queue file's lock, which is held for local file work only
REPLAY_ROUNDS = 20        # a replay goes round again for writes queued behind it meanwhile, this many times at most
DEAD_SECONDS = 7 * 86400  # how long a refused create keeps its request's later writes from being sent
ENTITY_PATH_RE = re.compile(r'/api/(?:tasks|requests)/([^/?#]+)')

HEARTBEAT_SECONDS = 60.0
LINE_CAP = 200
LINE_BREAK_RE = re.compile(rb'[\r\n]')
ANSI_RE = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\)|[@-Z\\-_])')
CONTROL_RE = re.compile(r'[\x00-\x1f\x7f]')
# `run` hands each pipe a background grandchild still holds to this copier, which outlives taskctl.
COPIER = '''import os
while True:
    data = os.read(0, 65536)
    if not data:
        break
    while data:
        data = data[os.write(1, data):]
'''

DURATION_UNITS = r'(days?|d|hours?|hrs?|h|minutes?|mins?|m|seconds?|secs?|s)'
DURATION_TOKEN_RE = re.compile(r'(\d+(?:\.\d+)?)\s*' + DURATION_UNITS)
DURATION_RE = re.compile(r'(?:\d+(?:\.\d+)?\s*%s\s*)+' % DURATION_UNITS)
UNIT_SECONDS = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}


class UsageError(Exception):
    pass


class BoardError(Exception):
    """A failed board call. `unreachable` separates network trouble from a rejection; `connect` means
    the connection itself failed (the board never saw the call, so it may be queued); `sent` means the
    call went out but no reply came; `status` is a rejection's HTTP status; `queued` means the write was
    kept for replay; `deferred` means it was queued behind a replay another taskctl is running, which
    sends it (not an error: a note, exit 0)."""

    def __init__(self, message: str, unreachable: bool, connect: bool = False, sent: bool = False,
                 status: Optional[int] = None, queued: bool = False, deferred: bool = False):
        super().__init__(message)
        self.unreachable = unreachable
        self.connect = connect
        self.sent = sent
        self.status = status
        self.queued = queued
        self.deferred = deferred


class QueueBusy(OSError):
    """The queue file stayed locked by another process for longer than LOCK_WAIT."""


def note(message: str) -> None:
    print('taskctl: ' + message, file=sys.stderr, flush=True)


def warn(message: str) -> None:
    note('warning: ' + message)


def emit(values) -> None:
    for value in values:
        print(value, flush=True)


def put(text: str) -> None:
    """Print a body from the board on stdout as it came, ending in exactly one newline."""
    if text:
        sys.stdout.write(text if text.endswith('\n') else text + '\n')
        sys.stdout.flush()


def baked(value: str) -> Optional[str]:
    """A value the board rendered into this file, or None in the unrendered copy of the repo."""
    return None if value.startswith('{') else value


def connect_failed(reason) -> bool:
    """True when urllib's reason says no connection was made (refused, no route, no DNS, timed out)."""
    if isinstance(reason, (socket.timeout, TimeoutError, socket.gaierror, ConnectionRefusedError)):
        return True
    return isinstance(reason, OSError) and reason.errno in CONNECT_ERRNOS


class Board:
    def __init__(self, base: str, timeout: float, strict: bool):
        self.base = base
        self.timeout = timeout
        self.strict = strict
        # A LAN service: a proxy configured in the environment would only get in the way.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.spool = Spool(base)
        self.prepared = False  # the queued writes have had their turn before this command's calls
        self.down = None       # the connect failure that stopped that replay; the next call reuses it
        self.docs = None       # the board's X-Tasks-Docs, from the latest reply that had one
        self.update_tried = False

    def exchange(self, method: str, path: str, body=None, accept: str = 'application/json',
                 replay: bool = False):
        """One round trip: (HTTP status, reply bytes), rejections included. BoardError: no reply."""
        data = None if body is None else json.dumps(body).encode('utf-8')
        try:
            # Inside the try: an unusable URL (ValueError) is board config, reported like a dead board.
            request = urllib.request.Request(self.base + path, data=data, method=method)
            request.add_header('Accept', accept)
            if data is not None:
                request.add_header('Content-Type', 'application/json')
            if replay:
                request.add_header(REPLAY_HEADER, '1')
            try:
                response = self._opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as e:
                self.saw(e.headers)
                try:
                    raw = e.read() if e.fp is not None else b''
                finally:
                    with contextlib.suppress(Exception):
                        e.close()
                return e.code, raw
            with response:
                self.saw(response.headers)
                return response.status, response.read()
        except urllib.error.URLError as e:
            # urllib wraps what fails while connecting and sending; a failed reply comes through bare.
            reason = e.reason
            if isinstance(reason, socket.timeout):
                reason = 'no connection within %gs' % self.timeout
            raise BoardError('board unreachable at %s (%s)' % (self.base, reason), True,
                             connect=connect_failed(e.reason))
        except (OSError, http.client.HTTPException) as e:
            reason = 'no reply within %gs' % self.timeout if isinstance(e, socket.timeout) else str(e)
            raise BoardError('board unreachable at %s (%s)' % (self.base, reason or type(e).__name__), True,
                             sent=True)
        except ValueError as e:
            raise BoardError('board unreachable at %s (%s)' % (self.base, e), True)

    def saw(self, headers) -> None:
        docs = (headers.get(DOCS_HEADER) or '').strip() if headers is not None else ''
        if DOCS_RE.fullmatch(docs):
            self.docs = docs

    def prepare(self) -> None:
        """Before this command's first call, replay what earlier ones queued, so the board sees it in order."""
        if self.prepared:
            return
        self.prepared = True
        if self.spool.pending():
            error, _, _ = self.flush()  # another taskctl replaying them: this command goes on without waiting
            if error is not None and error.connect:
                self.down = error

    def fetch(self, method: str, path: str, body=None, accept: str = 'application/json'):
        """exchange() for a command's own call, after the queued writes had their turn."""
        self.prepare()
        if self.down is not None:
            # The replay just failed to connect: this call would too, and would wait as long again.
            error, self.down = self.down, None
            raise error
        return self.exchange(method, path, body, accept)

    def call(self, method: str, path: str, body: Optional[dict] = None, text: bool = False,
             spool: bool = False):
        """A call whose failure is a BoardError. spool=True: a write that is queued when the board cannot
        be reached (and behind any writes still queued), so it gets there later, in order."""
        if spool:
            return self.write(method, path, body)
        if method != 'GET':
            self.prepare()
            self.refuse_dead(method, path)
        status, raw = self.fetch(method, path, body, 'text/plain' if text else 'application/json')
        return self.parse(method, path, status, raw, text)

    def parse(self, method: str, path: str, status: int, raw: bytes, text: bool = False, quiet: bool = False):
        if status >= 400:
            raise BoardError('board rejected %s %s: %s' % (method, path, describe_error(status, raw)), False,
                             status=status)
        if text:
            return raw.decode('utf-8', 'replace')
        try:
            result = json.loads(raw.decode('utf-8'))
        except ValueError:
            result = None
        if not isinstance(result, dict):
            raise BoardError('unexpected reply to %s %s (is %s a task-status board?)'
                             % (method, path, self.base), False)
        if not quiet:
            for message in result.get('warnings') or []:
                warn('board: %s' % message)
        return result

    def download(self, path: str) -> bytes:
        status, raw = self.exchange('GET', path, accept='text/plain')
        if status != 200:
            raise BoardError('board rejected GET %s: %s' % (path, describe_error(status, raw)), False, status=status)
        return raw

    def write(self, method: str, path: str, body: Optional[dict]):
        at = time.time()
        self.prepared = True  # the replay below is this command's turn for the queue
        self.refuse_dead(method, path)
        if self.spool.pending():
            error, left, busy = self.flush(defer=self.record(method, path, body, at))
            if busy:
                raise BoardError('another taskctl is replaying the queued writes for %s; this one is queued behind'
                                 ' them and goes with them (%d queued)' % (self.base, left), False, queued=True,
                                 deferred=True)
            if left:
                # Stopped by the board, or (rarely) by writes arriving faster than they replay.
                raise self.queue(method, path, body, at, error or BoardError(
                    'the queued writes for %s are still being replayed' % self.base, False, deferred=True))
            self.refuse_dead(method, path)  # the replay may just have found its request was never created
        try:
            status, raw = self.exchange(method, path, body)
        except BoardError as e:
            if e.connect:
                raise self.queue(method, path, body, at, e)
            if e.sent:
                raise BoardError('%s; not queued, as the board may have got it' % e, True)
            raise
        return self.parse(method, path, status, raw)

    def refuse_dead(self, method: str, path: str) -> None:
        """Refuse a write for a request whose queued create the board refused: its id belongs to another
        request (made meanwhile), so the write would land on someone else's work."""
        m = ENTITY_PATH_RE.match(path) if method != 'GET' else None
        rid = m.group(1).split('-')[0] if m else None
        if rid and rid in self.spool.dead():
            raise BoardError('not sent %s %s: request %s was never created (the board refused its queued create,'
                             ' as another request has that id)' % (method, path, rid), False)

    @staticmethod
    def record(method: str, path: str, body: Optional[dict], at: float) -> dict:
        """A write as the queue keeps it; qid tells it apart from every other queued write."""
        return {'method': method, 'path': path, 'body': body, 'at': round(at, 3), 'queued_at': round(time.time(), 3),
                'qid': secrets.token_hex(8)}

    def queue(self, method: str, path: str, body: Optional[dict], at: float, error) -> BoardError:
        """Queue a write for replay; returns the error to report (a warning, or --strict's exit code)."""
        if error is None:
            error = BoardError('board unreachable at %s' % self.base, True)
        try:
            count = self.spool.append(self.record(method, path, body, at))
        except OSError as e:
            return BoardError('%s; could not queue it for replay (%s)' % (error, e), error.unreachable,
                              status=error.status)
        return BoardError('%s; queued for replay (%d queued)' % (error, count), error.unreachable,
                          status=error.status, queued=True, deferred=error.deferred)

    def flush(self, defer: Optional[dict] = None):
        """Replay the queued writes in order: (the error that stopped the replay or None, writes left,
        whether another taskctl is replaying them right now).

        One process at a time replays (it holds the replay lock, which nobody waits for), and the queue
        file is locked only while it is read or rewritten, never while the board is being talked to. So
        while one replays, the others go on: a read goes straight to the board, and a write (`defer`, a
        record) is queued behind the replay, which sends it before it lets go. A connect failure, a
        missing reply or a 5xx stops the replay and keeps the rest; a 4xx drops that write. Every queued
        write is safe to send twice (creates carry their id, closes and attention are idempotent), so a
        write whose reply went missing is kept too."""
        sent = dropped = 0
        error, left, lock = None, 0, None
        try:
            if defer is None:
                lock = self.spool.replay_lock()
            else:
                with self.spool.locked():
                    # A replay lets go only with the queue locked, once it has found nothing left, so it is
                    # either still going (and sends this write too) or over (and this process replays).
                    lock = self.spool.replay_lock()
                    if lock is None:
                        left = self.spool.add(defer)
            if lock is None:
                return None, left or self.spool.count(), True
            refused = set()  # requests whose queued create was refused (an id clash, a bad title)
            for round_ in range(REPLAY_ROUNDS):
                with self.spool.locked():
                    queued = self.spool.read(quiet=round_ > 0)
                    records = coalesce(queued)
                    if len(records) != len(queued):
                        self.spool.write(records)
                    if not records:
                        self.spool.write([])
                        lock.release()
                        break
                    dead = self.spool.dead()
                done, newly_refused = set(), set()
                for record in records:
                    method, path = record['method'], record['path']
                    target, action = write_target(record)
                    rid = target[1].split('-')[0] if target else None
                    if rid is not None and (rid in dead or rid in refused):
                        dropped += 1
                        done.add(record_key(record))
                        warn('dropped a queued write: %s %s is for request %s, which was never created'
                             % (method, path, rid))
                        continue
                    body = dict(record.get('body') or {}, at=record['at'])
                    try:
                        status, raw = self.exchange(method, path, body, replay=True)
                        self.parse(method, path, status, raw, quiet=True)
                    except BoardError as e:
                        if e.status is not None and 400 <= e.status < 500:
                            dropped += 1
                            done.add(record_key(record))
                            warn('dropped a queued write: %s' % e)
                            if action == 'create':
                                refused.add(rid)
                                newly_refused.add(rid)
                            continue
                        error = e
                        break
                    sent += 1
                    done.add(record_key(record))
                # Waited for longer: what was just sent must come off the queue, or it would be sent again.
                with self.spool.locked(LOCK_WAIT * 5):
                    if newly_refused:
                        self.spool.add_dead(newly_refused)
                    # By qid, not by position: a write queued meanwhile may have superseded one of these.
                    remaining = [r for r in self.spool.read(quiet=True) if record_key(r) not in done]
                    self.spool.write(remaining)
                    left = len(remaining)
                    if error is not None or not remaining or round_ == REPLAY_ROUNDS - 1:
                        lock.release()  # with the queue locked (see above)
                        break
        except OSError as e:
            warn('cannot replay the queued writes in %s: %s' % (self.spool.path, e))
        finally:
            if lock is not None:
                lock.release()
        if sent or dropped:
            note('replayed %d queued writes%s' % (sent, ', dropped %d' % dropped if dropped else ''))
        return error, left, False


def describe_error(status: int, raw: bytes) -> str:
    try:
        payload = json.loads(raw.decode('utf-8'))
        message = payload.get('error') or http.client.responses.get(status, 'error')
        if payload.get('hint'):
            message = '%s (hint: %s)' % (message, payload['hint'])
    except (ValueError, AttributeError):
        message = http.client.responses.get(status, 'error')
    return '%d %s' % (status, message)


# The offline queue

def spool_home() -> str:
    """$TASKS_SPOOL_DIR, else ${XDG_CACHE_HOME:-~/.cache}/taskctl."""
    directory = os.environ.get('TASKS_SPOOL_DIR') or os.path.join(
        os.environ.get('XDG_CACHE_HOME') or os.path.join(os.path.expanduser('~'), '.cache'), 'taskctl')
    return os.path.abspath(os.path.expanduser(directory))


def spool_fallback() -> Optional[str]:
    """A private directory in the system temp dir, for when the usual one cannot be written."""
    try:
        if hasattr(os, 'getuid'):
            user = str(os.getuid())
        else:
            import getpass
            user = re.sub(r'[^A-Za-z0-9_.-]', '_', getpass.getuser())
        return os.path.join(tempfile.gettempdir(), 'taskctl-%s' % user)
    except Exception:  # no usable temp dir, or no user name: there is no fallback
        return None


def can_create_in(path: str) -> bool:
    """Whether `path` is a writable directory, or could be created as one."""
    while not os.path.isdir(path):
        parent = os.path.dirname(path)
        if parent == path or os.path.lexists(path):
            return False
        path = parent
    return os.access(path, os.W_OK | os.X_OK)


def private_dir(path: str) -> None:
    """Refuse a shared temp dir entry that is someone else's, or a symlink planted there."""
    if hasattr(os, 'getuid'):
        info = os.lstat(path)
        if stat.S_ISLNK(info.st_mode) or info.st_uid != os.getuid():
            raise PermissionError(errno.EACCES, 'not a private directory of this user', path)
        if info.st_mode & 0o077:
            os.chmod(path, 0o700)


def valid_record(record) -> bool:
    return (isinstance(record, dict) and record.get('method') in ('POST', 'DELETE')
            and isinstance(record.get('path'), str) and record['path'].startswith('/api/')
            and isinstance(record.get('body'), (dict, type(None)))
            and isinstance(record.get('at'), (int, float)) and not isinstance(record['at'], bool)
            and math.isfinite(record['at']) and isinstance(record.get('qid', ''), str))


def record_key(record) -> str:
    """What tells a queued write apart: its qid (or, in a record without one, a hash of it)."""
    if record.get('qid'):
        return record['qid']
    return 'h' + hashlib.sha1(json.dumps(record, sort_keys=True).encode('utf-8')).hexdigest()[:16]


def write_target(record):
    """What a queued write is about and what it does, like (('tasks', 'k3m9qa-2'), 'progress')."""
    m = WRITE_PATH_RE.fullmatch(record['path'])
    if m:
        return (m.group(1), m.group(2)), m.group(3)
    body = record.get('body') or {}
    if record['path'] == '/api/requests' and body.get('id'):
        return ('requests', body['id']), 'create'
    return None, None


def coalesce(records):
    """Drop the writes a later one supersedes: a progress update when a later write is about the same
    task, and an attention set or clear when a later one is about the same request or task."""
    kept, later, later_attention = [], set(), set()
    for record in reversed(records):
        target, action = write_target(record)
        if target is not None:
            if (action == 'progress' and target in later) or (action == 'attention' and target in later_attention):
                continue
            later.add(target)
            if action == 'attention':
                later_attention.add(target)
        kept.append(record)
    kept.reverse()
    return kept


class Spool:
    """Writes that could not reach the board, oldest first, one JSON object per line, in a file per
    board URL. The file is read and rewritten under a lock on the .lock file beside it, which is never
    waited for longer than LOCK_WAIT; the .replay.lock file is held by the one process replaying it; the
    .dead file lists the requests whose queued create the board refused."""

    def __init__(self, base: str):
        self.name = 'spool-%s.jsonl' % hashlib.sha1(base.encode('utf-8', 'replace')).hexdigest()[:10]
        self.path = os.path.join(spool_home(), self.name)
        if not can_create_in(os.path.dirname(self.path)):
            fallback = spool_fallback()
            if fallback:
                self.path = os.path.join(fallback, self.name)

    @property
    def dead_path(self) -> str:
        return os.path.splitext(self.path)[0] + '.dead'

    def pending(self) -> bool:
        try:
            return os.path.getsize(self.path) > 0
        except OSError:
            return False

    def count(self) -> int:
        try:
            with open(self.path, encoding='utf-8', errors='replace') as f:
                return sum(1 for line in f if line.strip())
        except OSError:
            return 0

    def read(self, quiet: bool = False):
        try:
            with open(self.path, encoding='utf-8', errors='replace') as f:
                lines = [line for line in f.read().splitlines() if line.strip()]
        except FileNotFoundError:
            return []
        records = []
        for line in lines:
            try:
                record = json.loads(line)
            except ValueError:
                record = None
            if valid_record(record):
                records.append(record)
        if len(records) < len(lines) and not quiet:
            warn('skipped %d unreadable queued writes in %s' % (len(lines) - len(records), self.path))
        return records

    def write(self, records) -> None:
        if not records:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(self.path)
            return
        write_atomic(self.path, ''.join(json.dumps(r, separators=(',', ':')) + '\n' for r in records), 0o600)

    def append(self, record) -> int:
        """Queue one write after the others (dropping what it supersedes); returns how many are queued."""
        self.make_dir()
        with self.locked():
            return self.add(record)

    def add(self, record) -> int:
        """append() for a caller that holds the lock already."""
        records = coalesce(self.read(quiet=True) + [record])
        self.write(records)
        return len(records)

    def dead(self) -> set:
        """The requests whose queued create the board refused in the last DEAD_SECONDS."""
        return {rid for rid, when in self._dead_entries() if when > time.time() - DEAD_SECONDS}

    def add_dead(self, rids) -> None:
        """Remember refused creates (with the queue locked), forgetting the ones older than DEAD_SECONDS."""
        now = time.time()
        entries = {rid: when for rid, when in self._dead_entries() if when > now - DEAD_SECONDS}
        entries.update((rid, now) for rid in rids)
        write_atomic(self.dead_path, ''.join('%s %d\n' % item for item in sorted(entries.items())), 0o600)

    def _dead_entries(self):
        try:
            with open(self.dead_path, encoding='utf-8', errors='replace') as f:
                lines = f.read().splitlines()
        except OSError:
            return []
        entries = []
        for line in lines:
            rid, _, when = line.partition(' ')
            with contextlib.suppress(ValueError):
                if REQUEST_RE.fullmatch(rid):
                    entries.append((rid, float(when)))
        return entries

    def make_dir(self) -> None:
        fallback = spool_fallback()
        candidates = [self.path]
        if fallback and os.path.dirname(self.path) != fallback:
            candidates.append(os.path.join(fallback, self.name))
        error = None
        for path in candidates:
            directory = os.path.dirname(path)
            try:
                os.makedirs(directory, mode=0o700, exist_ok=True)
                if fallback and directory == fallback:
                    private_dir(directory)
                if not os.access(directory, os.W_OK | os.X_OK):
                    raise PermissionError(errno.EACCES, 'not writable', directory)
            except OSError as e:
                error = error or e
                continue
            self.path = path
            return
        raise error

    @contextlib.contextmanager
    def locked(self, wait: float = LOCK_WAIT):
        """The queue file's lock. It is held only to read or rewrite the files, never while talking to
        the board, so waiting for it longer than that means its holder is stuck: QueueBusy."""
        lock = FileLock(self.path + '.lock')
        if not lock.acquire(wait):
            raise QueueBusy(errno.EWOULDBLOCK, 'another taskctl has held its lock for over %gs' % wait,
                            self.path + '.lock')
        try:
            yield
        finally:
            lock.release()

    def replay_lock(self):
        """The right to replay the queue, a FileLock to release, or None while another process has it."""
        lock = FileLock(self.path + '.replay.lock')
        return lock if lock.acquire(0) else None


class FileLock:
    """An exclusive lock on a lock file, waited for no longer than asked. Best effort: on a filesystem
    without locks it counts as held."""

    def __init__(self, path: str):
        self.path = path
        self.fd = None
        self.real = False

    def acquire(self, wait: float) -> bool:
        """True once it is held; False when another process still holds it after `wait` seconds."""
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        deadline = time.monotonic() + wait
        while True:
            got = try_lock(fd)
            if got is not False:
                self.fd, self.real = fd, got is True
                return True
            if time.monotonic() >= deadline:
                os.close(fd)
                return False
            time.sleep(0.02)

    def release(self) -> None:
        fd, self.fd = self.fd, None
        if fd is None:
            return
        if self.real and fcntl is None and msvcrt is not None:
            with contextlib.suppress(OSError):
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        os.close(fd)  # this also drops an flock


def try_lock(fd: int) -> Optional[bool]:
    """One try for an exclusive lock on fd: True (held), False (another process has it), None (no locks)."""
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        if msvcrt is not None:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
    except OSError as e:
        return False if e.errno in LOCK_BUSY_ERRNOS else None  # otherwise a filesystem without locks
    return None


def note_queued(board: Board) -> None:
    """Tell ping's caller about writes still waiting for the board."""
    queued = board.spool.count()
    if queued:
        note('%d queued (writes waiting for the board, in %s; taskctl flush replays them)'
             % (queued, board.spool.path))


# Self-update of the installed skill

def skill_dir() -> Optional[str]:
    """The directory of this file when it is an installed skill (SKILL.md beside it), else None."""
    here = os.path.dirname(os.path.abspath(__file__))
    return here if os.path.isfile(os.path.join(here, 'SKILL.md')) else None


def claude_md(path: Optional[str] = None) -> str:
    config_dir = os.environ.get('CLAUDE_CONFIG_DIR') or '~/.claude'
    return os.path.expanduser(path or os.path.join(config_dir, 'CLAUDE.md'))


def fetch_rule(board: Board, text: str) -> str:
    rule = text.strip()
    if not (rule.startswith(RULE_BEGIN) and rule.endswith(RULE_END)):
        raise BoardError('%s/api/rule did not return a task-status block' % board.base, False)
    return rule


def refresh_rule(board: Board, path: str) -> None:
    """Upsert the board's rule block in CLAUDE.md, but only where one is installed already."""
    target = os.path.realpath(path)
    try:
        with open(target, encoding='utf-8', newline='') as f:
            text = f.read()
    except FileNotFoundError:
        return
    if RULE_BEGIN_RE.search(text):
        merged, _ = merge_rule(text, fetch_rule(board, board.download('/api/rule').decode('utf-8', 'replace')))
        if merged != text:
            write_atomic(target, merged)


def whats_new(changelog: str, since: Optional[int]) -> str:
    """The changelog entries newer than API version `since`, or the newest one, verbatim."""
    heads = list(CHANGELOG_ENTRY_RE.finditer(changelog))
    if not heads:
        return changelog.strip()
    entries = []
    for i, m in enumerate(heads):
        end = heads[i + 1].start() if i + 1 < len(heads) else len(changelog)
        entries.append((int(m.group(1)), changelog[m.start():end].rstrip()))
    newer = [text for version, text in entries if since is not None and version > since]
    return '\n\n'.join(newer or [entries[0][1]])


def install_skill(board: Board, here: str, old: Optional[str], new: str, force: bool) -> bool:
    """Replace the skill in `here` with the board's copy, refresh the CLAUDE.md rule when one is
    installed, and tell the agent what changed. Raises when the skill itself cannot be updated;
    returns False (changing nothing) when the board serves the version already installed."""
    files = [(name, board.download('/api/skill/' + name), magic, mode) for name, magic, mode in SKILL_FILES]
    for name, data, magic, mode in files:
        if magic is not None and not data.startswith(magic):
            raise ValueError('the board served a %s that does not start with %s' % (name, magic.decode()))
    source = files[2][1]
    if b'BAKED_URL =' not in source:
        raise ValueError('the board served a taskctl.py without its BAKED_URL line')
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('ignore')
            compile(source, 'taskctl.py', 'exec', dont_inherit=True)
    except (SyntaxError, ValueError) as e:
        raise ValueError('the board served a taskctl.py that does not compile here: %s' % e)
    served = SERVED_DOCS_RE.search(source)
    served = served.group(1).decode('ascii', 'replace') if served else ''
    if DOCS_RE.fullmatch(served):
        new = served
        if not force and new == old:
            return False  # the header disagrees with the files; updating would change nothing
    temps = []
    try:
        for name, data, magic, mode in files:
            fd, tmp = tempfile.mkstemp(dir=here, prefix='.%s.' % name, suffix='.tmp')
            temps.append(tmp)
            with os.fdopen(fd, 'wb') as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp, mode)
        for (name, data, magic, mode), tmp in zip(files, temps):
            os.replace(tmp, os.path.join(here, name))
    finally:
        for tmp in temps:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
    path = claude_md()
    try:
        refresh_rule(board, path)
    except (BoardError, OSError, ValueError) as e:
        warn('updated the skill, but not the rule in %s: %s' % (path, e))
    try:
        api = baked(BAKED_API)
        news = whats_new(board.download('/api/changelog').decode('utf-8', 'replace'),
                         int(api) if api and api.isdigit() else None)
    except BoardError as e:
        news = None
        warn('cannot fetch the changelog: %s' % e)
    note("the board's agent instructions changed (docs %s -> %s); updated the skill in %s" % (old or 'none', new, here))
    if news is not None:
        note("what's new:")
        print(news, file=sys.stderr, flush=True)
    note('re-read %s before continuing; the copy loaded in your context is out of date'
         % os.path.join(here, 'SKILL.md'))
    return True


def norm_url(url: str) -> str:
    """A board URL for comparing: scheme and host lowercased, no trailing slash, no default port."""
    text = url.strip().rstrip('/')
    if '://' not in text:
        text = 'http://' + text
    try:
        parts = urllib.parse.urlsplit(text)
        host, port = parts.hostname or '', parts.port
    except ValueError:  # a bad port, say
        return text.lower()
    scheme = parts.scheme.lower()
    if (scheme, port) in (('http', 80), ('https', 443)):
        port = None
    netloc = ('[%s]' % host if ':' in host else host) + ('' if port is None else ':%d' % port)
    return '%s://%s%s' % (scheme, netloc, parts.path.rstrip('/'))


def own_board(board: Board) -> bool:
    """Whether this command talks to the board this skill came from (its baked URL). Only that board
    may replace the skill or its CLAUDE.md rule: --url or TASKS_URL can point anywhere."""
    home = baked(BAKED_URL)
    return home is not None and norm_url(home) == norm_url(board.base)


def auto_update(board: Board) -> None:
    """After the command: when a reply said the board's agent instructions changed, update the skill.
    Never changes the command's stdout or exit status: any failure is one warning."""
    if board is None:
        return
    new, old = board.docs, baked(BAKED_DOCS)
    if not new or new == old or board.update_tried:
        return
    board.update_tried = True
    here = skill_dir()
    if old is None or here is None or os.environ.get('TASKS_NO_UPDATE', '').strip().lower() in ('1', 'true', 'yes'):
        return
    if not own_board(board):
        note('the board at %s has other agent instructions (docs %s) than this skill (docs %s), which follows'
             ' its own board, %s; the skill was not changed' % (board.base, new, old, baked(BAKED_URL)))
        return
    try:
        install_skill(board, here, old, new, force=False)
    except Exception as e:  # BoardError, OSError, anything unforeseen: the command is unaffected
        warn("the board's agent instructions changed (docs %s -> %s), but updating the skill in %s failed: %s;"
             " run %s update" % (old, new, here, e, os.path.join(here, 'taskctl')))


def split_globals(argv):
    """Pull --url/--timeout/--strict out of argv wherever they appear, up to a bare `--`."""
    opts = {'url': None, 'timeout': None, 'strict': False}
    rest = []
    args = iter(argv)
    for arg in args:
        name, eq, value = arg.partition('=')
        if arg == '--':
            rest += [arg] + list(args)
        elif arg == '--strict':
            opts['strict'] = True
        elif name in ('--url', '--timeout'):
            if not eq:
                value = next(args, None)
                if value is None:
                    raise UsageError('%s needs a value' % name)
            opts[name[2:]] = value
        else:
            rest.append(arg)
    return opts, rest


def resolve_url(flag: Optional[str]) -> str:
    baked = None if BAKED_URL.startswith('{{') else BAKED_URL
    url = next(c for c in (flag, os.environ.get('TASKS_URL'), baked, DEFAULT_URL) if c and c.strip())
    url = url.strip().rstrip('/')
    return url if '://' in url else 'http://' + url


def resolve_timeout(flag: Optional[str]) -> float:
    """--timeout, then $TASKS_TIMEOUT, then 3s; each takes seconds (0.5) or a duration (5s)."""
    for source, text in (('--timeout', flag), ('TASKS_TIMEOUT', os.environ.get('TASKS_TIMEOUT'))):
        if text:
            try:
                value = float(text)
            except ValueError:
                try:
                    value = float(parse_duration(text))
                except UsageError:
                    value = math.nan
            if math.isfinite(value) and value > 0:
                return value
            if source == '--timeout':
                raise UsageError('--timeout must be a positive number of seconds such as 3 or 5s, not %r' % text)
            # A bad ambient setting is board config, not a bad command line: it must not stop the work.
            warn('TASKS_TIMEOUT=%r is not a positive number of seconds; using %gs' % (text, DEFAULT_TIMEOUT))
    return DEFAULT_TIMEOUT


def classify(value: str):
    """Return (normalised id, kind), kind being 'request', 'task' or OFFLINE."""
    v = value.strip().lower()
    if v == OFFLINE:
        return v, OFFLINE
    if REQUEST_RE.fullmatch(v):
        return v, 'request'
    if TASK_RE.fullmatch(v):
        return v, 'task'
    raise UsageError('%r is not a request id (like k3m9qa) or a task id (like k3m9qa-3)' % value)


def need_id(value: str, kind: str) -> str:
    v, got = classify(value)
    if got in (kind, OFFLINE):
        return v
    if kind == 'task':
        raise UsageError('%s is a request id; this command needs a task id such as %s-1' % (v, v))
    raise UsageError('%s is a task id; this command needs its request id %s' % (v, v.split('-')[0]))


def skip_offline(v: str) -> bool:
    if v == OFFLINE:
        note("id is 'offline' (the board was unreachable when it was created); nothing to report")
    return v == OFFLINE


def entity_path(v: str, kind: str) -> str:
    return '/api/%s/%s' % ('tasks' if kind == 'task' else 'requests', v)


def parse_percent(text: str) -> float:
    try:
        value = float(text.strip().rstrip('%').strip())
    except ValueError:
        value = math.nan
    if not math.isfinite(value):
        raise UsageError('percent must be a number such as 45, 45.5 or 45%%, not %r' % text)
    if not 0 <= value <= 100:
        value = min(100.0, max(0.0, value))
        warn('percent %s is outside 0-100; sending %g' % (text, value))
    return round(value, 1)


def parse_duration(text: str) -> int:
    """Seconds from 90, 90s, 5m, 5min, ~5m, 1h30m, 2h, 1:30 or 1:30:00."""
    s = text.strip().lower().lstrip('~').strip()
    if re.fullmatch(r'\d+(?:\.\d+)?', s):
        return int(round(float(s)))
    if re.fullmatch(r'\d+(?::\d{1,2}){1,2}', s):
        # The last field is always seconds, as on a timer: 1:30 is 1m30s, 1:30:00 is 1h30m.
        return sum(int(part) * 60 ** i for i, part in enumerate(reversed(s.split(':'))))
    if DURATION_RE.fullmatch(s):
        return int(round(sum(float(n) * UNIT_SECONDS[unit[0]] for n, unit in DURATION_TOKEN_RE.findall(s))))
    raise UsageError('cannot read duration %r (try 90, 90s, 5m, 1h30m or 1:30:00)' % text)


def fmt_dur(seconds) -> str:
    s = int(round(max(0.0, float(seconds or 0))))
    m, s = divmod(s, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d:
        return '%dd%02dh' % (d, h)
    if h:
        return '%dh%02dm' % (h, m)
    return '%dm%02ds' % (m, s) if m else '%ds' % s


def fmt_pct(value) -> str:
    return ('%.1f' % (value or 0)).rstrip('0').rstrip('.') + '%'


def one_line(text: str) -> str:
    return ' '.join(text.split())


def clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit - 1] + '…'


def task_line(t: dict, now) -> str:
    elapsed = '-'
    if t.get('elapsed') is not None:
        elapsed = ('~' if t.get('start_inferred') else '') + fmt_dur(t['elapsed'])
    text = t.get('title') or ''
    if t.get('message'):
        text += ': ' + one_line(t['message'])
    remaining = t.get('eta_remaining')
    if remaining is not None and remaining > 0:
        text += '  (eta ~%s)' % fmt_dur(remaining)
    elif remaining is not None and now is not None and t.get('eta_at') is not None:
        text += '  (overdue %s)' % fmt_dur(now - t['eta_at'])
    status = 'waiting' if t.get('attention') else 'stale' if t.get('stale') else t.get('status')
    return '%-9s %6s %8s  %s  %s' % (status, fmt_pct(t.get('percent')), elapsed, t.get('id'), text)


def request_line(r: dict) -> str:
    text = r.get('title') or ''
    if r.get('message'):
        text += ': ' + one_line(r['message'])
    if r.get('origin'):
        text += '  (%s)' % r['origin']
    status = 'waiting' if r.get('waiting') else 'stale' if r.get('stale') else r.get('status')
    return '%s  %-7s %6s  %s/%s done  %8s  %s' % (r.get('id'), status, fmt_pct(r.get('percent')),
                                                  r.get('tasks_done'), r.get('tasks_total'),
                                                  fmt_dur(r.get('elapsed')), text)


def waiting_lines(item: dict, indent: str):
    """`waiting: QUESTION` for a request or task that is waiting for input, else nothing."""
    attention = item.get('attention')
    if isinstance(attention, dict) and attention.get('message'):
        return [indent + 'waiting: ' + one_line(str(attention['message']))]
    return []


def post_for_ids(board: Board, path: str, body: dict, expected: int) -> dict:
    """POST a create; on a soft failure print `offline` once per expected ID before re-raising."""
    try:
        return board.call('POST', path, body)
    except BoardError:
        if not board.strict:
            emit([OFFLINE] * expected)
        raise


def emit_ids(ids, expected: int) -> None:
    emit([i or OFFLINE for i in ids] + [OFFLINE] * (expected - len(ids)))


def new_request_id() -> str:
    return ''.join(secrets.choice(ID_CHARS) for _ in range(6))


def cmd_new(board: Board, args) -> int:
    title = ' '.join(args.title)
    try:
        cwd = os.path.basename(os.getcwd())
    except OSError:
        cwd = '?'
    expected = 1 + len(args.tasks)
    # The ID is picked here, so a create queued offline still prints the IDs it will have.
    for attempt in range(3):
        rid = new_request_id()
        body = {'id': rid, 'title': title, 'origin': '%s:%s' % (socket.gethostname(), cwd), 'tasks': args.tasks}
        try:
            r = board.call('POST', '/api/requests', body, spool=True)
            break
        except BoardError as e:
            if e.status == 409 and 'already exists' in str(e) and attempt < 2:
                continue  # another request has that ID: draw again
            if e.queued:
                emit([rid] + ['%s-%d' % (rid, i) for i in range(1, expected)])
                note("created %s '%s' (%s)  page: %s/r/%s" % (rid, title, 'queued behind a replay in progress' if e.deferred
                                                            else 'queued until the board is back', board.base, rid))
                for i, task in enumerate(args.tasks, 1):
                    note('  %s-%d  %s' % (rid, i, task))
            elif not board.strict:
                emit([OFFLINE] * expected)
            raise
    tasks = r.get('tasks') or []
    emit_ids([r.get('id')] + [t.get('id') for t in tasks], expected)
    note("created %s '%s'  page: %s" % (r.get('id'), r.get('title') or title, r.get('url')))
    for t in tasks:
        note('  %s  %s' % (t.get('id'), t.get('title')))
    return 0


def cmd_add(board: Board, args) -> int:
    rid = need_id(args.rid, 'request')
    if args.start and len(args.titles) != 1:
        raise UsageError('add --start takes exactly one TITLE (it is created running)')
    if skip_offline(rid):
        emit([OFFLINE] * len(args.titles))
        return 0
    body = {'title': args.titles[0], 'start': True} if args.start else {'titles': args.titles}
    r = post_for_ids(board, '/api/requests/%s/tasks' % rid, body, len(args.titles))
    tasks = r.get('tasks') if 'tasks' in r else [r]
    emit_ids([t.get('id') for t in tasks], len(args.titles))
    for t in tasks:
        note("added %s '%s'%s" % (t.get('id'), t.get('title'), ' (running)' if args.start else ''))
    if r.get('reopened'):
        note('request %s was closed; adding tasks reopened it' % rid)
    return 0


def cmd_start(board: Board, args) -> int:
    tid = need_id(args.tid, 'task')
    if skip_offline(tid):
        return 0
    board.call('POST', '/api/tasks/%s/progress' % tid, {'message': ' '.join(args.hint).strip() or 'started'},
               spool=True)
    note('%s running' % tid)
    return 0


def cmd_progress(board: Board, args) -> int:
    tid = need_id(args.tid, 'task')
    body = {'percent': parse_percent(args.percent), 'message': ' '.join(args.hint).strip()}
    if not body['message']:
        raise UsageError('HINT must say what is happening right now')
    if args.eta is not None:
        body['eta_seconds'] = parse_duration(args.eta)
    if skip_offline(tid):
        return 0
    t = board.call('POST', '/api/tasks/%s/progress' % tid, body, spool=True)
    eta = '  eta ~%s' % fmt_dur(body['eta_seconds']) if 'eta_seconds' in body else ''
    note('%s %s %s%s' % (tid, t.get('status'), fmt_pct(t.get('percent')), eta))
    return 0


def cmd_close(board: Board, args) -> int:
    v, kind = classify(args.id)
    message = ' '.join(args.message).strip()
    if args.status == 'failed' and not message:
        raise UsageError('fail needs a one-line reason: taskctl fail ID MSG')
    if skip_offline(v):
        return 0
    body = {'status': args.status, 'message': message} if message else {'status': args.status}
    if kind == 'task':
        t = board.call('POST', '/api/tasks/%s/complete' % v, body, spool=True)
        note('%s %s' % (v, t.get('status')))
        if t.get('start_inferred'):
            note('task was never started; its running time is approximate'
                 ' - run `taskctl start TID` when you begin a task')
        return 0
    r = board.call('POST', '/api/requests/%s/complete' % v, body, spool=True)
    note('request %s %s (%s/%s tasks done)' % (v, r.get('status'), r.get('tasks_done'), r.get('tasks_total')))
    if r.get('auto_closed'):
        note('still-running tasks closed as %s: %s' % (args.status, ', '.join(r['auto_closed'])))
    if r.get('cancelled'):
        note('never-started tasks cancelled: %s' % ', '.join(r['cancelled']))
    return 0


def cmd_ask(board: Board, args) -> int:
    v, kind = classify(args.id)
    question = one_line(' '.join(args.question))
    if not question:
        raise UsageError('ask needs the QUESTION you are waiting on: taskctl ask ID QUESTION')
    if skip_offline(v):
        return 0
    board.call('POST', entity_path(v, kind) + '/attention', {'message': question}, spool=True)
    note('%s waiting for input: %s' % (v, question))
    return 0


def cmd_resume(board: Board, args) -> int:
    v, kind = classify(args.id)
    if skip_offline(v):
        return 0
    board.call('DELETE', entity_path(v, kind) + '/attention', spool=True)
    note('%s resumed' % v)
    return 0


def cmd_show(board: Board, args) -> int:
    v, kind = classify(args.id)
    if skip_offline(v):
        return 0
    if kind == 'task':
        t = board.call('GET', '/api/tasks/%s' % v)
        emit([task_line(t, t.get('now'))] + waiting_lines(t, ''))
        return 0
    r = board.call('GET', '/api/requests/%s' % v)
    emit([request_line(r)] + waiting_lines(r, '') + ['page: %s' % r.get('url')])
    for t in r.get('tasks') or []:
        emit(['  ' + task_line(t, r.get('now'))] + waiting_lines(t, '    '))
    return 0


def cmd_list(board: Board, args) -> int:
    # The board always returns every running request; `limit` caps only the finished ones, so --all
    # is every running request plus the 20 most recently finished.
    r = board.call('GET', '/api/requests?limit=20' if args.all else '/api/requests?status=running')
    requests = r.get('requests') or []
    if not requests:
        note('no requests' if args.all else 'no running requests')
    emit([request_line(q) for q in requests])
    return 0


def cmd_ping(board: Board, args) -> int:
    emit([board.base])
    try:
        health = board.call('GET', '/api/health')
    finally:
        note_queued(board)
    if not health.get('ok') or health.get('service') != 'tasks':
        raise BoardError('%s answered, but not as a task-status board' % board.base, False)
    note('ok: board at %s (pid %s, %s running)'
         % (board.base, health.get('pid'), health.get('requests_running')))
    return 0


def cmd_flush(board: Board, args) -> int:
    board.prepared = True
    if not board.spool.pending():
        note('nothing queued for %s' % board.base)
        return 0
    error, left, busy = board.flush()
    if busy:
        note('another taskctl is replaying the queue for %s right now (%d queued)' % (board.base, left))
        return 0
    if error is not None:
        raise BoardError('%s; %d still queued' % (error, left), error.unreachable, status=error.status, queued=True)
    return 0


def cmd_text(board: Board, args) -> int:
    put(board.call('GET', args.route, text=True))
    return 0


def cmd_api(board: Board, args) -> int:
    method = args.method.upper()
    if not re.fullmatch('[A-Z]+', method):
        raise UsageError('METHOD must be an HTTP method such as GET or POST, not %r' % args.method)
    if not API_PATH_RE.fullmatch(args.path):
        raise UsageError('PATH must start with /api/ (such as /api/requests/k3m9qa), not %r' % args.path)
    body = None
    if args.json is not None:
        try:
            body = json.loads(args.json)
        except ValueError as e:
            raise UsageError('JSON is not valid JSON: %s' % e)
    status, raw = board.fetch(method, args.path, body)
    put(raw.decode('utf-8', 'replace'))
    if status >= 400:
        raise BoardError('board rejected %s %s: %s' % (method, args.path, describe_error(status, raw)), False,
                         status=status)
    return 0


def cmd_update(board: Board, args) -> int:
    board.update_tried = True
    here = skill_dir()
    # A setup command, not a status report: every failure is a real error (exit 1), never soft.
    try:
        if here is None:
            raise ValueError('%s has no SKILL.md beside it, so it is not an installed skill; install one with'
                             ' the command in %s/api/usage' % (os.path.dirname(os.path.abspath(__file__)), board.base))
        if baked(BAKED_URL) is not None and not own_board(board) and not args.force:
            raise ValueError('this skill belongs to %s; pass --force to move it to %s' % (baked(BAKED_URL), board.base))
        health = board.call('GET', '/api/health')
        current = board.docs or health.get('docs_version')
        if not isinstance(current, str) or not DOCS_RE.fullmatch(current):
            raise ValueError('the board at %s does not version its agent instructions (API version %s)'
                             % (board.base, health.get('version')))
        if current == baked(BAKED_DOCS) and not args.force:
            note('the skill in %s is already current (docs %s)' % (here, current))
            return 0
        install_skill(board, here, baked(BAKED_DOCS), current, force=True)
    except (BoardError, OSError, ValueError) as e:
        note('error: cannot update the skill in %s: %s' % (here or os.path.dirname(os.path.abspath(__file__)), e))
        return 1
    return 0


def cmd_install_rule(board: Board, args) -> int:
    path = claude_md(args.file)
    # A setup command, not a status report: every failure is a real error (exit 1), never soft.
    try:
        # The rule lands in an always-loaded CLAUDE.md, so only the skill's own board may write it.
        if baked(BAKED_URL) is not None and not own_board(board) and not args.force:
            raise ValueError('this skill belongs to %s; pass --force to take the rule from %s'
                             % (baked(BAKED_URL), board.base))
        rule = fetch_rule(board, board.call('GET', '/api/rule', text=True))
        target = os.path.realpath(path)  # write through a symlinked CLAUDE.md instead of replacing the link
        try:
            with open(target, encoding='utf-8', newline='') as f:
                old = f.read()
        except FileNotFoundError:
            old = None
        new, replaced = merge_rule(old or '', rule)
        if new != old:
            write_atomic(target, new)
    except (BoardError, OSError, ValueError) as e:
        note('error: cannot install the rule in %s: %s' % (path, e))
        return 1
    note('rule %s in %s' % ('unchanged' if new == old else 'updated' if replaced else 'installed', path))
    return 0


def merge_rule(text: str, block: str):
    """Replace the begin..end block (inclusive) with `block`, or append it after a blank line.

    Returns (new text, whether an existing block was replaced). Duplicate blocks collapse into one.
    """
    first = RULE_BLOCK_RE.search(text)
    if first:
        rest = RULE_BLOCK_RE.sub('', text[first.end():])
        return text[:first.start()] + block + '\n' + rest, True
    if RULE_BEGIN_RE.search(text):
        raise ValueError('it has %s without a matching %s; fix it by hand' % (RULE_BEGIN, RULE_END))
    existing = text.rstrip('\r\n')
    return (existing + '\n\n' if existing else '') + block + '\n', False


def write_atomic(path: str, text: str, mode: Optional[int] = None) -> None:
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
    if mode is None:
        mode = os.stat(path).st_mode & 0o777 if os.path.exists(path) else 0o644  # mkstemp alone gives 0600
    fd, tmp = tempfile.mkstemp(dir=directory, prefix='.%s.' % os.path.basename(path), suffix='.tmp')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class RunMonitor:
    """State shared by `run`: readers record the child's latest output line, the reporter sends it."""

    def __init__(self, board: Board, tid: str, every: float, pattern, start_message: str):
        self.board, self.tid, self.every, self.pattern = board, tid, every, pattern
        self.start_message = start_message
        self.lock = threading.Lock()
        self.line = None
        self.percent = None
        self.stopping = threading.Event()
        self.warned = False

    def pump(self, pipe, out) -> None:
        """Copy the child's bytes through unchanged while tracking its last non-empty line."""
        pending = b''
        while True:
            try:
                chunk = os.read(pipe.fileno(), 65536)
            except OSError:
                chunk = b''
            if not chunk:
                break
            if out is not None:
                try:
                    out.write(chunk)
                    out.flush()
                except (OSError, ValueError):
                    # Our reader went away (e.g. `| head`): keep draining so the child never blocks, and
                    # point the fd at /dev/null so the flush at interpreter exit doesn't complain.
                    try:
                        os.dup2(os.open(os.devnull, os.O_WRONLY), out.fileno())
                    except (OSError, ValueError):
                        pass
                    out = None
            # Split on \r too, so a progress bar redrawn in place still yields its latest state.
            parts = LINE_BREAK_RE.split(pending + chunk)
            pending = parts.pop()[-4096:]
            self.record(parts)
        self.record([pending])

    def record(self, raw_lines) -> None:
        for raw in raw_lines:
            text = one_line(CONTROL_RE.sub(' ', ANSI_RE.sub('', raw.decode('utf-8', 'replace'))))
            if text:
                percent = self.match_percent(text) if self.pattern else None
                with self.lock:
                    self.line = clip(text, LINE_CAP)
                    self.percent = self.percent if percent is None else percent

    def match_percent(self, text: str) -> Optional[float]:
        m = self.pattern.search(text)
        try:
            if self.pattern.groups == 1:
                value = float((m.group(1) or '').rstrip('%'))
            else:
                value = float(m.group(1)) / float(m.group(2)) * 100
        except (AttributeError, TypeError, ValueError, ZeroDivisionError):
            return None  # no match, an unmatched optional group, or 0 as the total
        return round(min(100.0, max(0.0, value)), 1)

    def report(self) -> None:
        """Start the task, then send the latest line when it changes (throttled), plus a heartbeat."""
        self.quietly('progress', {'message': self.start_message})
        sent, sent_at = (None, None), time.monotonic()
        while not self.stopping.wait(min(1.0, self.every)):
            with self.lock:
                current = (self.line, self.percent)
            waited = time.monotonic() - sent_at
            # The heartbeat keeps a quiet phase (a long link step, a silent download) from looking stale.
            if (current != sent and waited >= self.every) or waited >= HEARTBEAT_SECONDS:
                body = {'message': current[0] or self.start_message}
                if current[1] is not None:
                    body['percent'] = current[1]
                self.quietly('progress', body)
                sent, sent_at = current, time.monotonic()

    def quietly(self, action: str, body: dict) -> None:
        """Reporting errors never touch the child or the exit status: warn once and carry on."""
        if self.tid == OFFLINE:
            return
        try:
            self.board.call('POST', '/api/tasks/%s/%s' % (self.tid, action), body, spool=True)
        except Exception as e:  # BoardError, or anything unforeseen: the command must be unaffected
            if isinstance(e, BoardError) and e.deferred:
                return  # queued behind a replay in progress, which sends it
            if not self.warned:
                self.warned = True
                warn('%s; the command is unaffected' % e)


def keyboard_reaches_child() -> bool:
    """True when a terminal Ctrl-C already goes to our whole process group, the child included."""
    for fd in (0, 1, 2):
        try:
            if os.isatty(fd):
                return os.tcgetpgrp(fd) == os.getpgrp()
        except (OSError, AttributeError):
            return False
    return False


def cmd_run(board: Board, args) -> int:
    tid = need_id(args.tid, 'task')
    if args.every <= 0:
        raise UsageError('--every must be more than 0 seconds')
    pattern = None
    if args.percent_regex is not None:
        try:
            pattern = re.compile(args.percent_regex)
        except re.error as e:
            raise UsageError('bad --percent-regex: %s' % e)
        if pattern.groups not in (1, 2):
            raise UsageError('--percent-regex needs 1 group (the percent) or 2 groups (done, total)')
    command = args.command
    if tid == OFFLINE:  # the command still runs; the monitor just reports nothing
        note("id is 'offline'; running the command without status reporting")
    monitor = RunMonitor(board, tid, args.every, pattern, clip('running: ' + shlex.join(command), LINE_CAP))
    started = time.monotonic()
    try:
        child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
                                 env=dict(os.environ, PYTHONUNBUFFERED='1'))
    except OSError as e:
        code = 127 if isinstance(e, FileNotFoundError) else 126
        reason = 'cannot run %s: %s' % (command[0], e.strerror or e)
        note('error: ' + reason)
        monitor.quietly('complete', {'status': 'failed', 'message': 'exit %d: %s' % (code, reason)})
        return code

    def forward(signum, frame):
        if signum == signal.SIGINT and keyboard_reaches_child():
            return  # the terminal already sent this Ctrl-C to the child; a second one can cut its cleanup short
        try:
            child.send_signal(signum)
        except (OSError, ValueError):
            pass

    for signum in (signal.SIGINT, signal.SIGTERM):
        if signal.getsignal(signum) != signal.SIG_IGN:  # respect nohup / a shell's background-job SIGINT
            signal.signal(signum, forward)
    readers = [threading.Thread(target=monitor.pump, args=(pipe, getattr(out, 'buffer', None)), daemon=True)
               for pipe, out in ((child.stdout, sys.stdout), (child.stderr, sys.stderr))]
    reporter = threading.Thread(target=monitor.report, daemon=True)
    for thread in readers + [reporter]:
        thread.start()
    rc = child.wait()
    took = fmt_dur(time.monotonic() - started)
    # A background grandchild may hold the pipes open; give the output a moment to drain, no more.
    drain_until = time.monotonic() + 2
    for thread in readers:
        thread.join(max(0.0, drain_until - time.monotonic()))
    # Stop the reporter first so a late progress call can't race the final close.
    monitor.stopping.set()
    reporter.join(board.timeout + 1)
    if rc == 0:
        body = {'status': 'done', 'message': 'exit 0 after %s' % took}
    elif rc > 0:
        tail = ': ' + monitor.line if monitor.line else ''
        body = {'status': 'failed', 'message': 'exit %d after %s%s' % (rc, took, tail)}
    else:
        body = {'status': 'failed', 'message': 'killed by signal %d' % -rc}
    monitor.quietly('complete', body)
    # A grandchild still holding a pipe would die of SIGPIPE on its next write once we exit, so reporting
    # would change the real work: a detached copier takes over the pipe (and our pump, until we exit).
    for thread, pipe, out in zip(readers, (child.stdout, child.stderr), (sys.stdout, sys.stderr)):
        if thread.is_alive():
            hand_off(pipe, out)
    return rc if rc >= 0 else 128 - rc


def hand_off(pipe, out) -> None:
    """Copy `pipe` to `out` from now on in a process of its own, which ends when the pipe closes."""
    try:
        dest = out.fileno()
    except (AttributeError, OSError, ValueError):
        dest = subprocess.DEVNULL  # no usable stdout: still drain, so the writer never sees a closed pipe
    try:
        subprocess.Popen([sys.executable, '-c', COPIER], stdin=pipe.fileno(), stdout=dest,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         creationflags=getattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', 0))
    except (OSError, ValueError, TypeError):  # TypeError: sys.executable can be None when embedded
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='taskctl', description='Report progress on long-running work to the task-status board.',
        epilog='Global options also work after the subcommand. '
               'Request IDs look like k3m9qa, task IDs like k3m9qa-3.')
    parser.add_argument('--url', help='board URL (default: $TASKS_URL, baked-in URL, %s)' % DEFAULT_URL)
    parser.add_argument('--timeout', help='seconds per call, such as 3 or 5s (default: $TASKS_TIMEOUT or 3)')
    parser.add_argument('--strict', action='store_true', help='fail on board errors: 1 rejected, 3 unreachable')
    sub = parser.add_subparsers(dest='cmd', metavar='COMMAND')
    sub.required = True

    def command(name, func, help, **defaults):
        p = sub.add_parser(name, help=help, description=help)
        p.set_defaults(func=func, **defaults)
        return p

    p = command('new', cmd_new, 'open a request; prints its ID, then one task ID per -t')
    p.add_argument('title', nargs='+')
    p.add_argument('-t', '--task', dest='tasks', action='append', default=[], metavar='TASK')
    p = command('add', cmd_add, 'add tasks to a request; prints one task ID per title')
    p.add_argument('rid')
    p.add_argument('titles', nargs='+', metavar='title')
    p.add_argument('--start', action='store_true', help='create the (single) task already running')
    p = command('start', cmd_start, 'mark a task running')
    p.add_argument('tid')
    p.add_argument('hint', nargs='*')
    p = command('progress', cmd_progress, 'report percent, what is happening now, and an optional ETA')
    p.add_argument('tid')
    p.add_argument('percent', help='45, 45.5 or 45%%')
    p.add_argument('hint', nargs='+')
    p.add_argument('--eta', metavar='DUR', help='90, 90s, 5m, 1h30m, 1:30:00')
    p = command('done', cmd_close, 'close a task, or a whole request, as done', status='done')
    p.add_argument('id')
    p.add_argument('message', nargs='*')
    p = command('fail', cmd_close, 'close a task, or a whole request, as failed', status='failed')
    p.add_argument('id')
    p.add_argument('message', nargs='+')
    p = command('ask', cmd_ask, 'show a request or task as waiting for your user\'s input: ask ID QUESTION')
    p.add_argument('id')
    p.add_argument('question', nargs='+')
    command('resume', cmd_resume, 'clear the waiting mark once your user has answered').add_argument('id')
    command('show', cmd_show, 'show a request with its tasks, or one task').add_argument('id')
    p = command('list', cmd_list, 'list every running request (--all: plus the 20 most recently finished)')
    p.add_argument('-a', '--all', action='store_true', help='also list the 20 most recently finished requests')
    command('ping', cmd_ping, 'check the board; prints its URL')
    command('flush', cmd_flush, 'replay the writes queued while the board was unreachable')
    p = command('run', cmd_run, 'run CMD and report its latest output line: run TID -- CMD...')
    p.usage = '%(prog)s [-h] [--every SEC] [--percent-regex RE] TID -- CMD...'
    p.add_argument('tid')
    p.add_argument('--every', type=float, default=10.0, metavar='SEC', help='min seconds between updates')
    p.add_argument('--percent-regex', metavar='RE', help='1 group = percent, 2 groups = done/total')
    p = command('install-rule', cmd_install_rule, 'add the task-status rule block to CLAUDE.md')
    p.add_argument('--file', metavar='PATH', help='default: ${CLAUDE_CONFIG_DIR:-~/.claude}/CLAUDE.md')
    p.add_argument('--force', action='store_true', help="also from a board other than the skill's own")
    p = command('update', cmd_update, "replace this installed skill with the board's current one")
    p.add_argument('--force', action='store_true', help='also when it is already current')
    command('usage', cmd_text, "print the board's usage text: every route, field and rule", route='/api/usage')
    command('changelog', cmd_text, "print the board's changelog, newest first", route='/api/changelog')
    p = command('api', cmd_api, 'call any board route and print the reply: api METHOD PATH [JSON]')
    p.add_argument('method', metavar='METHOD', help='GET, POST, PUT or DELETE')
    p.add_argument('path', metavar='PATH', help='starts with /api/, such as /api/requests/k3m9qa')
    p.add_argument('json', metavar='JSON', nargs='?', help="the request body, such as '{\"message\": \"x\"}'")
    return parser


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')  # a non-UTF-8 console must not crash on a title
        except AttributeError:
            pass
    board = None
    try:
        opts, rest = split_globals(sys.argv[1:] if argv is None else argv)
        if rest and rest[0] in ALIASES:
            rest[0] = ALIASES[rest[0]]
        command = None
        if rest and rest[0] == 'run':
            # Split by hand: everything after the first `--` is the command, its options included.
            cut = rest.index('--') if '--' in rest else len(rest)
            rest, command = rest[:cut], rest[cut + 1:]
            if not command and not {'-h', '--help'} & set(rest):  # argparse prints `run -h`
                raise UsageError('run needs -- and then the command: taskctl run TID -- CMD...')
        args = build_parser().parse_args(rest)
        args.command = command
        strict = opts['strict'] or os.environ.get('TASKS_STRICT', '').strip().lower() in ('1', 'true', 'yes')
        board = Board(resolve_url(opts['url']), resolve_timeout(opts['timeout']), strict)
        rc = args.func(board, args)
    except UsageError as e:
        note('error: %s' % e)
        return 2
    except BoardError as e:
        if e.deferred:
            note(str(e))  # queued behind a replay in progress, which sends it: no failure, even with --strict
            rc = 0
        elif board is not None and board.strict:
            note('error: %s' % e)
            rc = 3 if e.unreachable else 1
        else:
            warn('%s; continuing without status reporting' % e if e.unreachable and not e.queued else str(e))
            rc = 0
    except KeyboardInterrupt:
        return 130
    try:
        auto_update(board)
    except KeyboardInterrupt:
        return 130
    return rc


if __name__ == '__main__':
    sys.exit(main())
