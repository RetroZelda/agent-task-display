#!/usr/bin/env python3
"""Task-status board: a small HTTP service that agents push request and task progress to.

    python3 tasks/server.py [--host 0.0.0.0] [--port 8765] [--db tasks/data/tasks.db] [--public-url URL]
        [--config PATH] [--tls-port P --tls-cert FILE --tls-key FILE]
        [--stale-after 600] [--expire-after 86400] [--retention-days 30] [--pidfile PATH] [--verbose]

Env fallbacks: TASKS_HOST, TASKS_PORT, TASKS_DB, TASKS_PUBLIC_URL, TASKS_CONFIG, TASKS_TLS_PORT,
TASKS_TLS_CERT, TASKS_TLS_KEY. The pidfile defaults to <db dir>/server-<port>.pid and the settings file
to <db dir>/settings.json. Standard library only; needs Python >= 3.10 and SQLite >= 3.37 (and ffmpeg, only
for live streams).
A wildcard --host (0.0.0.0, ::, * or empty) listens dual-stack, IPv4 and IPv6 on one socket, so a
hostname that resolves to an IPv6 address works too; without IPv6 it falls back to IPv4 only. A
specific address is bound as given. --tls-port, --tls-cert and --tls-key (all three or none) add an
HTTPS listener on the same host, by the same rules: the same routes over the same database, for
browsers, which only allow notifications in a secure context.

Wire contract, version 3 (every component is built against it; v1 and v2 clients keep working)

IDs: a request id is 6 chars of 23456789abcdefghjkmnpqrstuvwxyz (k3m9qa); a task id is {rid}-{seq}
(k3m9qa-3). Case-insensitive, returned lowercase. The wrong kind of id on a route is a 400 with a hint.

Every response, success or error (http.server's own included), carries X-Tasks-Version: 3 and
X-Tasks-Docs: <docs_version>. docs_version = sha256 over skill/SKILL.md, templates/usage.md,
templates/rule.md, templates/changelog.md, taskctl and taskctl.py, in that order, each as relpath + NUL +
bytes + NUL (a missing file: relpath + NUL only), hex[:12]: it changes whenever the agent instructions do.

Routes (JSON unless noted; bodies are parsed as JSON whatever the Content-Type; empty body = {})
  GET    /                         static/index.html if Accept has text/html, else usage (Vary: Accept)
  GET    /r/<anything>             static/index.html
  GET    /api, /api/usage          templates/usage.md, rendered, text/plain
  GET    /api/rule                 templates/rule.md, rendered, text/plain
  GET    /api/changelog            templates/changelog.md, rendered, text/plain: newest first, each entry a
                                   line "## v<N> — <YYYY-MM-DD>" and its bullet lines
  GET    /api/skill/SKILL.md       skill/SKILL.md, text/plain
  GET    /api/skill/taskctl        taskctl (sh wrapper), text/plain
  GET    /api/skill/taskctl.py     taskctl.py, rendered, text/plain
  GET    /favicon.ico              204
  GET    /api/health               {ok, service: "tasks", version: "3", pid, now, started_at, requests_running,
                                   docs_version, tls_port (int | null), config_path}
  POST   /api/requests             {title, origin?, tasks?: [str], id?} -> 201 Request + tasks + now + existing
                                   (false). id is a client-made request id (else the server makes one; not an
                                   id -> 400): if it exists with the same title -> 200 that request + tasks +
                                   now + existing: true (an idempotent replay, nothing written); with another
                                   title -> 409 {"error": "request id already exists"}
  GET    /api/requests             ?status=running|done|failed&limit=1..500 (100) -> {now, stale_after,
                                   counts: {running, stale, done, failed, waiting}, requests}: running
                                   (created_at DESC, max 500) then finished (completed_at DESC, limited);
                                   counts = all; waiting = running requests with waiting true
  GET    /api/requests/{rid}       Request + tasks (by seq) + now + stale_after
  DELETE /api/requests/{rid}       {deleted, now}
  POST   /api/requests/{rid}/complete  {status?: done|failed, message?}
                                   -> Request + tasks + now + auto_closed: [tid] + cancelled: [tid]
  POST   /api/requests/{rid}/tasks {title, start?: true} -> 201 Task + now + reopened
                                   {titles: [str]} -> 201 {now, request_id, tasks (pending), reopened}
  POST   /api/requests/{rid}/attention  {message} -> Request + tasks + now; a closed request is a 409
                                   {error, status, hint: "the request is closed"}
  DELETE /api/requests/{rid}/attention  -> Request + tasks + now (idempotent; the request's own only)
  GET    /api/tasks/{tid}          Task + now
  POST   /api/tasks/{tid}/progress {message, percent?, eta_seconds?} -> Task + now
  POST   /api/tasks/{tid}/complete {status?: done|failed, message?} -> Task + now
  POST   /api/tasks/{tid}/attention  {message} -> Task + now; a closed task is a 409 {error, status}
  DELETE /api/tasks/{tid}/attention  -> Task + now (idempotent)
  GET    /api/events               ?since=N&limit=1..1000 (500) -> {now, cursor, events, truncated, waiting,
                                   stale, settings_version, stream} (see Events and Stream)
  GET    /api/settings             {now, path, version, exists, error, settings, defaults} (see Settings)
  PUT    /api/settings             {settings: {...}, partial} -> the GET shape
  DELETE /api/settings             removes the file (the defaults again) -> the GET shape
  GET    /api/stream               {now, stream, ffmpeg} (see Stream)
  POST   /api/stream               {url} -> 201 the GET shape + existing: false; the url already open: 200, existing: true
  DELETE /api/stream               -> the GET shape + closed: bool (idempotent)
  GET    /api/stream/media?id=<id> the open stream as fragmented MP4, an endless body (see Stream)
Rendering literally replaces {{BASE_URL}}, {{PUBLIC_URL}}, {{VERSION}} and {{DOCS_VERSION}}. Files are
re-read per request; a missing one is a 500 {"error": "template missing: <path>"}. On the HTTPS listener
BASE_URL is http://<the Host's hostname>:<http port> (agents stay on plain http, which a self-signed
certificate cannot break), while PUBLIC_URL and every url field keep the https address.

Shapes (every key always present, null when absent; times are float epoch seconds, server clock)
  Task     id request_id seq title status percent message eta_at eta_remaining created_at started_at
           start_inferred updated_at completed_at elapsed stale url attention
  Request  id title origin status message created_at updated_at completed_at elapsed percent tasks_total
           tasks_done tasks_failed tasks_running tasks_pending tasks_cancelled current eta_at stale url
           attention waiting tasks_waiting
  Event    id ts type request_id task_id request_title task_title status percent message replayed
  Task status: pending|running|done|failed|cancelled. Request status: running|done|failed.
  tasks_total counts NON-cancelled tasks (Y), tasks_done is X. Request percent = mean over non-cancelled
  tasks, done counting 100 (none: 100 if the request is done, else 0). current = {id, title, message} of
  the latest-updated running task; eta_at = max running eta_at. elapsed runs from started_at (task) or
  created_at (request) to completed_at or now. attention = {message, since} | null: a question for the
  user (a Request's is its own, not its tasks'). waiting = running and (the request's attention or a
  running task's); tasks_waiting = running tasks with attention. stale = running, silent > --stale-after,
  not within 60s past a running eta_at, and never while it has attention (task) or is waiting (request).
  url = <public>/r/<rid>, plus #<tid> for a task.

Errors are {error, field?, hint?}: 400 validation, 403 cross-origin write, 404 unknown id/route, 405
(+Allow), 409 state conflict, 410 a stream that was replaced or closed, 411 chunked, 413 body > 64 KiB, 500,
503 database busy (+Retry-After: 1), or a stream not connected yet, full or shutting down (+Retry-After).
http.server's own rejections (malformed request line 400, 414, 431, unknown method 501) are JSON too.
Success bodies gain "warnings": [str] only when something was truncated, clamped or ignored.

State rules
  - Progress starts a pending task (running, started_at = now); on a closed task it is a 409.
    Omitted percent keeps the old value; omitted eta_seconds clears the ETA.
  - Task complete: done sets percent 100; ETA cleared; completed_at set once; a never-started task gets
    started_at = created_at, start_inferred = true. Same status again: no-op. Different status:
    overwrite (last write wins). Never reopens the request.
  - Request complete (one transaction): pending tasks -> cancelled, running tasks -> the request's
    status. Same status again: no-op. Different status: overwrite, completed_at kept.
  - Adding tasks to a done/failed request reopens it. Every task write bumps the request's updated_at.
  - Attention: setting it on a pending task starts it; setting it again replaces message and since; it
    bumps updated_at like any write. Progress on a task or its completion clears the task's attention; a
    request's completion clears its own and all its tasks'.
  - Maintenance: running requests silent for --expire-after (not waiting, not inside a running eta) close
    as failed "expired: no activity for 24h"; finished requests older than --retention-days are deleted;
    events older than 7 days are deleted, then all but the newest 10000.

Events: every change writes events in its own transaction; they outlive deletes. status and percent
are the entity's after the change (the request's for request-level events), times the write's.
  request_created; request_reopened (a task added to a closed request); request_done, request_failed (a
  status change by request complete or expiry; message = the request's; the cascade writes no task
  events); request_deleted; task_added (each task added to an existing request); task_started (pending ->
  running by progress or attention, or a single add with start); task_progress (progress on a running
  task); task_done, task_failed (a status change by task complete); attention (message = the question;
  task_id null at request level); attention_cleared (message = the question that was cleared).
  GET /api/events without since: events [] and cursor = the last event id (0 if none), a baseline. With
  since: the events with id > since, ascending, at most limit; truncated = more remain (then cursor = the
  last one returned) or events after since were pruned; otherwise cursor = the last event id. waiting =
  [{request_id, request_title, task_id | null, task_title | null, message, since}] for every attention
  open in a running request, oldest first; stale = [{request_id, request_title}] of running requests
  stale now; settings_version = the settings' version; stream = the open stream's object, or null (see Stream).

Replays: taskctl queues writes while the board is unreachable and replays them later with the header
X-Tasks-Replay: 1. On create request, request and task complete, progress and attention set/clear such a
request may carry a numeric body field "at": the write's own time, clamped to [now - 86400, now] and
never before the entity's updated_at. The write's times (an ETA's base included) use it, and its events
get ts = it and replayed = true. Without the header "at" is an unknown field.

Settings: the notification defaults for every viewer, a JSON file (--config), re-read whenever its
mtime or size changes:
  {"events": {KEY: {"highlight": bool, "title": bool, "sound": bool, "notify": bool}}, "sound": {"volume": 0..1}}
  KEY: waiting task_failed request_failed request_done task_done task_started task_progress request_created
  stale. settings = the defaults deep-merged with the file; a file that is not valid JSON or breaks the
  schema counts as absent (the defaults) and says why in error. PUT validates its partial settings
  (unknown key, channel or field, non-bool, volume outside 0..1 -> 400 with field), merges them onto the
  current settings and writes the full result atomically. version = sha256 of the settings as compact
  JSON with sorted keys, hex[:12].

Stream: one network stream can be open at a time, shown beside the board on every open page. POST /api/stream
  {url} opens it: a newer url replaces the open one, the url already open is kept and retried at once. url:
  http, https, rtsp, rtsps, rtmp, rtmps, srt, udp or tcp, up to 2048 characters, no spaces or controls (else
  400 with field url). DELETE closes it. The board runs one ffmpeg for it (found on PATH at each start, so
  installing it later needs no restart; until then error says so): video copied, audio made AAC stereo,
  written as fragmented MP4 in pieces of about 200 ms. Only network protocols are read (no file:, pipe:,
  concat:). A run that ends or fails starts again after 1, 2, 4, 8, then 10 s of waiting.
  The object (in /api/events "stream", GET /api/stream and the replies): {id (12 hex, new for every
  stream), url (a user:password@ is shown as ***@ everywhere), opened_at, state: "connecting" | "live",
  since, error, video, audio, width, height, viewers, media}. live = a keyframe has gone out; since = when it
  went live, or while connecting when the trouble began (failed retries keep it); error = why the last run
  ended (null while live); video and audio = codec strings (avc1.42C028, mp4a.40.2), width and height those
  of the video; all null until ffmpeg has written its header, or when there is no such track. media = the
  path of GET /api/stream/media?id=<id>: 200, Content-Type video/mp4 (audio/mp4 without video) with the
  codecs, no Content-Length, fragmented MP4: the header, the fragments since the last keyframe, then live
  ones. It ends whenever a run ends and when the stream is replaced or closed; a page then asks again. 404 no
  stream, 410 another id, 503 (+Retry-After) not connected yet, 16 viewers or shutting down. A viewer more
  than 64 fragments behind is dropped. HEAD sends the headers only. The open stream is kept in
  <db>.stream.json (mode 600; --db tasks/data/tasks.db gives tasks/data/tasks.stream.json) and reopened
  after a restart; only DELETE removes it.

Writes (POST, PUT, DELETE) carrying an Origin header are refused unless Origin equals Host and Host names
this machine (IP literal, [IPv6] included, localhost, its hostname, the --public-url host). No auth.
"""

import argparse
import collections
import copy
import difflib
import errno
import hashlib
import ipaddress
import json
import logging
import math
import os
import queue
import re
import secrets
import shutil
import signal
import socket
import sqlite3
import ssl
import struct
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit


VERSION = '3'
SCHEMA_VERSION = 2
TASKS_DIR = Path(__file__).resolve().parent
# The agent-facing files; X-Tasks-Docs hashes them so an installed skill can tell it is out of date.
DOCS_FILES = ('skill/SKILL.md', 'templates/usage.md', 'templates/rule.md', 'templates/changelog.md',
              'taskctl', 'taskctl.py')

ID_ALPHABET = '23456789abcdefghjkmnpqrstuvwxyz'
RID_RE = re.compile(f'[{ID_ALPHABET}]{{6}}')
TID_RE = re.compile(f'([{ID_ALPHABET}]{{6}})-(\\d{{1,6}})')
# The Host header is baked into scripts agents execute, so only plain host[:port] shapes pass.
HOST_RE = re.compile(
    r'^(localhost|\d{1,3}(\.\d{1,3}){3}|\[[0-9A-Fa-f:.]+\]|[A-Za-z0-9]([A-Za-z0-9-]{0,62})'
    r'(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}))*)(:\d{1,5})?$', re.ASCII)

TITLE_MAX = 200
MESSAGE_MAX = 500
ORIGIN_MAX = 100
BODY_MAX = 64 * 1024
ETA_MAX = 7 * 86400
TASKS_PER_CALL = 200
TASKS_PER_REQUEST = 500
RUNNING_LIST_MAX = 500
REPLAY_WINDOW = 86400
EVENTS_MAX_AGE = 7 * 86400
EVENTS_KEEP = 10000
EVENTS_PER_CALL = 1000
# Live stream (the Stream paragraph of the docstring): what the board may open, and how patient it is with it.
STREAM_URL_MAX = 2048
STREAM_SCHEMES = ('http', 'https', 'rtsp', 'rtsps', 'rtmp', 'rtmps', 'srt', 'udp', 'tcp')
# What ffmpeg may open for an input: network protocols only, never file, pipe, fd, concat, subfile, data or cache.
STREAM_PROTOCOLS = 'http,https,tls,tcp,udp,rtp,rtsp,rtsps,rtmp,rtmps,srt,crypto'
STREAM_IO_TIMEOUT = 10          # s of silence before ffmpeg gives up on a source (-rw_timeout; rtsp -timeout)
# (analyzeduration in microseconds, probesize in bytes): how long ffmpeg looks at a source before it writes its
# header. A run moves to the next pair when the source needs a longer look (a long GOP joined mid-way).
STREAM_PROBES = ((1000000, 500000), (5000000, 5000000), (15000000, 20000000))
STREAM_NO_INIT = 10             # s past the analysis time without a header: audio that is announced but never sent. With the
                                # analysis time it must outlast STREAM_IO_TIMEOUT, or a source that takes the connection and
                                # then says nothing (busy, single-client) is given up on, and its audio dropped, before ffmpeg's own timeout
STREAM_FIRST_DATA = 30          # s from the header to the first keyframe
STREAM_STALL = 15               # s without output from a live run
STREAM_AUDIO_GAP = 3            # s of video without any audio before the run restarts without audio
STREAM_BACKOFF = (1, 2, 4, 8, 10)  # s between failed runs in a row
STREAM_HEALTHY = 10             # s live: the run counts as healthy and the backoff starts over
STREAM_KILL_AFTER = 2           # s from SIGTERM to SIGKILL for ffmpeg
STREAM_VIEWERS_MAX = 16
STREAM_QUEUE_MAX = 64           # fragments a viewer may lag behind (about 13 s) before it is dropped
STREAM_GOP_MAX = 8 << 20        # bytes of fragments kept for late joiners
STREAM_BOX_MAX = 64 << 20       # a box claiming more than this is corrupt output
STREAM_INIT_WAIT = 10           # s the media route waits for a restarting ffmpeg's header
STREAM_ERROR_LINES = 8          # lines of ffmpeg's stderr kept from the start and from the end of a run
STATUS_ALIASES = {
    'done': 'done', 'ok': 'done', 'success': 'done', 'complete': 'done', 'completed': 'done',
    'failed': 'failed', 'fail': 'failed', 'error': 'failed',
}

log = logging.getLogger('tasks')


def ts():
    return round(time.time(), 3)


class ApiError(Exception):
    def __init__(self, code, error, headers=None, **extra):
        super().__init__(error)
        self.code = code
        self.payload = {'error': error, **extra}
        self.headers = headers or {}


# Validation

_CONTROL_RE = re.compile(r'[\x00-\x1f\x7f-\x9f]')


def _squash(line):
    # Whitespace controls (tab, CR...) become spaces so words don't run together; others vanish.
    return ' '.join(_CONTROL_RE.sub(lambda m: ' ' if m.group().isspace() else '', line).split())


def clean_text(value, field, max_len, warnings, *, required=False, multiline=False, label=None):
    label = label or field
    if value is None:
        if required:
            raise ApiError(400, f'{label} is required', field=field)
        return None
    if not isinstance(value, str):
        raise ApiError(400, f'{label} must be a string', field=field)
    # Lone surrogates from JSON escapes would make SQLite and the UTF-8 encoder fail later.
    text = value.encode('utf-8', 'replace').decode('utf-8')
    text = '\n'.join(map(_squash, re.split(r'\r\n?|\n', text))).strip() if multiline else _squash(text)
    if not text:
        if required:
            raise ApiError(400, f'{label} must not be empty', field=field)
        return None
    if len(text) > max_len:
        text = text[:max_len - 1].rstrip() + '…'
        warnings.append(f'{label} truncated to {max_len} characters')
    return text


def clean_titles(value, field, warnings):
    if not isinstance(value, list):
        raise ApiError(400, f'{field} must be a list of strings', field=field)
    if len(value) > TASKS_PER_CALL:
        raise ApiError(400, f'{field} has {len(value)} items; at most {TASKS_PER_CALL} per call', field=field)
    return [clean_text(item, field, TITLE_MAX, warnings, required=True, label=f'{field}[{i}]')
            for i, item in enumerate(value)]


def clean_rid(value):
    # A client-made request id (taskctl makes its own so a create can be queued offline and replayed).
    if value is None:
        return None
    rid = value.strip().lower() if isinstance(value, str) else ''
    if not RID_RE.fullmatch(rid):
        raise ApiError(400, f'id must be 6 characters of {ID_ALPHABET}', field='id',
                       hint='request ids look like k3m9qa')
    return rid


def parse_number(value, field, allow_percent_sign=False):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ApiError(400, f'{field} must be a number', field=field)
    if isinstance(value, str):
        value = value.strip()
        if allow_percent_sign:
            value = value.removesuffix('%').strip()
    try:
        number = float(value)
    except (ValueError, OverflowError):
        raise ApiError(400, f'{field} must be a number', field=field) from None
    if not math.isfinite(number):
        raise ApiError(400, f'{field} must be a finite number', field=field)
    return number


def parse_percent(value, warnings):
    percent = parse_number(value, 'percent', allow_percent_sign=True)
    if not 0 <= percent <= 100:
        warnings.append(f'percent {percent:g} clamped to 0..100')
        percent = min(100.0, max(0.0, percent))
    return round(percent, 1)


def parse_eta(value, warnings):
    seconds = parse_number(value, 'eta_seconds')
    if seconds < 0:
        raise ApiError(400, 'eta_seconds must be >= 0', field='eta_seconds')
    if seconds > ETA_MAX:
        warnings.append(f'eta_seconds {seconds:g} clamped to {ETA_MAX} (7 days)')
        seconds = ETA_MAX
    return seconds


def parse_status(value):
    if value is None:
        return 'done'
    status = STATUS_ALIASES.get(value.strip().lower()) if isinstance(value, str) else None
    if status is None:
        raise ApiError(400, "status must be 'done' or 'failed'", field='status')
    return status


def check_fields(body, known, warnings):
    for key in body:
        if key not in known:
            guess = difflib.get_close_matches(key, sorted(known), n=1)
            hint = f" (did you mean '{guess[0]}'?)" if guess else ''
            warnings.append(f"unknown field '{key[:40]}' ignored{hint}")


def humanise(seconds):
    hours, rest = divmod(int(seconds), 3600)
    minutes, secs = divmod(rest, 60)
    return ''.join(f'{n}{unit}' for n, unit in ((hours, 'h'), (minutes, 'm'), (secs, 's')) if n) or '0s'


# Agent docs version

_docs_lock = threading.Lock()
_docs_cache = {'key': None, 'value': None}


def docs_version():
    """sha256 over DOCS_FILES (see the docstring), recomputed only when a file's mtime or size changes."""
    key = []
    for rel in DOCS_FILES:
        try:
            st = os.stat(TASKS_DIR / rel)
            key.append((st.st_mtime_ns, st.st_size))
        except OSError:
            key.append(None)
    with _docs_lock:
        if _docs_cache['key'] == key:
            return _docs_cache['value']
    digest, complete = hashlib.sha256(), True
    for rel, stamp in zip(DOCS_FILES, key):
        digest.update(rel.encode() + b'\0')
        if stamp is not None:
            try:
                digest.update((TASKS_DIR / rel).read_bytes() + b'\0')
            except OSError:  # counts as missing, and is looked at again next time
                complete = False
    value = digest.hexdigest()[:12]
    with _docs_lock:
        _docs_cache.update(key=key if complete else None, value=value)
    return value


# Settings

CHANNELS = ('highlight', 'title', 'sound', 'notify')
# Each event KEY's default for the four channels, in CHANNELS order (T = on).
_DEFAULT_EVENTS = {
    'waiting': 'TTTT', 'task_failed': 'TTTT', 'request_failed': 'TTTT', 'request_done': 'TTTT',
    'task_done': 'TFFF', 'task_started': 'TFFF', 'task_progress': 'TFFF', 'request_created': 'TFFF',
    'stale': 'TTFF',
}
EVENT_KEYS = tuple(_DEFAULT_EVENTS)
DEFAULT_SETTINGS = {
    'events': {key: {channel: flag == 'T' for channel, flag in zip(CHANNELS, flags)}
               for key, flags in _DEFAULT_EVENTS.items()},
    'sound': {'volume': 0.6},
}


def _unknown(what, key, known, where):
    guess = difflib.get_close_matches(key, known, n=1)
    hint = f"did you mean '{guess[0]}'?" if guess else f"expected one of: {', '.join(known)}"
    path = f'{where}.{key[:40]}' if where else key[:40]
    return ApiError(400, f"unknown {what} '{key[:40]}' in {where or 'the settings'}", field=path, hint=hint)


def check_settings(value, where):
    """A validated copy of (partial) settings; the first problem is an ApiError 400 whose field is its path."""
    def section(value, path):
        if not isinstance(value, dict):
            raise ApiError(400, f'{path or "the settings"} must be a JSON object', field=path or 'settings')
        return value

    def join(path, key):
        return f'{path}.{key}' if path else key

    out = {}
    for key, part in section(value, where).items():
        path = join(where, key)
        if key == 'events':
            out['events'] = {}
            for name, channels in section(part, path).items():
                if name not in EVENT_KEYS:
                    raise _unknown('event', name, EVENT_KEYS, path)
                out['events'][name] = {}
                for channel, flag in section(channels, join(path, name)).items():
                    if channel not in CHANNELS:
                        raise _unknown('channel', channel, CHANNELS, join(path, name))
                    if not isinstance(flag, bool):
                        field = join(join(path, name), channel)
                        raise ApiError(400, f'{field} must be true or false', field=field)
                    out['events'][name][channel] = flag
        elif key == 'sound':
            out['sound'] = {}
            for name, volume in section(part, path).items():
                if name != 'volume':
                    raise _unknown('field', name, ('volume',), path)
                bad = ApiError(400, f'{join(path, name)} must be a number from 0 to 1', field=join(path, name))
                if isinstance(volume, bool) or not isinstance(volume, (int, float)):
                    raise bad
                try:
                    volume = float(volume)  # a JSON integer can be too large for a float
                except OverflowError:
                    raise bad from None
                if not math.isfinite(volume) or not 0 <= volume <= 1:
                    raise bad
                out['sound']['volume'] = volume
        else:
            raise _unknown('field', key, ('events', 'sound'), where)
    return out


def merge_settings(base, patch):
    merged = copy.deepcopy(base)
    for name, channels in patch.get('events', {}).items():
        merged['events'][name].update(channels)
    merged['sound'].update(patch.get('sound', {}))
    return merged


def settings_version(settings):
    return hashlib.sha256(json.dumps(settings, sort_keys=True, separators=(',', ':')).encode()).hexdigest()[:12]


class Settings:
    """The global settings file, re-read when its (mtime_ns, size) changes; writes are atomic."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._stamp = False  # not read yet
        self.effective, self.version = DEFAULT_SETTINGS, settings_version(DEFAULT_SETTINGS)
        self.error, self.exists = None, False

    def _file_stamp(self):
        try:
            st = os.stat(self.path)
        except OSError:
            return None
        return st.st_mtime_ns, st.st_size

    def _load(self, stamp):
        self._stamp, self.exists, self.error = stamp, stamp is not None, None
        effective = DEFAULT_SETTINGS
        if stamp is not None:
            try:
                effective = merge_settings(DEFAULT_SETTINGS, check_settings(
                    json.loads(self.path.read_text(encoding='utf-8-sig')), ''))
            except OSError as exc:
                self.error = f'cannot read the file: {exc.strerror or exc}'
            except ApiError as exc:
                self.error = f'{exc} ({exc.payload["hint"]})' if exc.payload.get('hint') else str(exc)
            except (ValueError, RecursionError) as exc:  # JSON and UTF-8 errors are ValueErrors
                self.error = f'invalid JSON: {exc}'
            except Exception as exc:  # anything unforeseen: still the defaults and an error, never a crash
                self.error = f'invalid settings: {exc}'
            if self.error:
                log.warning('settings file %s: %s; using the defaults', self.path, _printable(self.error))
        self.effective, self.version = effective, settings_version(effective)

    def _refresh(self):
        stamp = self._file_stamp()
        if stamp != self._stamp:
            if self._stamp is not False:
                log.info('settings file %s changed; reloaded', self.path)
            self._load(stamp)

    def snapshot(self, now):
        with self._lock:
            self._refresh()
            return {'now': now, 'path': str(self.path), 'version': self.version, 'exists': self.exists,
                    'error': self.error, 'settings': self.effective, 'defaults': DEFAULT_SETTINGS}

    def current_version(self):
        with self._lock:
            self._refresh()
            return self.version

    def update(self, patch):
        with self._lock:
            self._refresh()
            text = json.dumps(merge_settings(self.effective, patch), indent=2) + '\n'
            temp = self.path.with_name(f'.{self.path.name}.{os.getpid()}.tmp')
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with open(temp, 'w', encoding='utf-8') as f:
                    f.write(text)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(temp, self.path)
            except OSError as exc:
                temp.unlink(missing_ok=True)
                raise ApiError(500, f'cannot write {self.path}: {exc.strerror or exc}') from None
            self._load(self._file_stamp())

    def reset(self):
        with self._lock:
            try:
                self.path.unlink(missing_ok=True)
            except OSError as exc:
                raise ApiError(500, f'cannot remove {self.path}: {exc.strerror or exc}') from None
            self._load(self._file_stamp())


# Live stream

# One ffmpeg per open stream remuxes the source to fragmented MP4 and the pages play it (see the Stream paragraph
# of the docstring). Nothing here waits for a viewer: they are fed through bounded queues.

STREAM_BAD_URL_RE = re.compile(r'[\s\x00-\x1f\x7f-\x9f]')
_USERINFO_RE = re.compile(r'(://)[^/\s]+@')
STREAM_PREFIX_RE = re.compile(r'^\[([^\]@]*?)\s*@ 0x[0-9a-f]+\]\s*')
# ffmpeg's decoders chatter while a copy joins a stream mid-way; none of it says why a run failed.
STREAM_NOISY_TAGS = frozenset(('h264', 'hevc', 'mpeg2video', 'mpeg4', 'aac', 'mp2', 'mp3'))
STREAM_NOISE_RE = re.compile(
    r'^(?:no frame!|non-existing PPS|Last message repeated|Error opening input file|Conversion failed|'
    r'decode_slice_header|co located POCs|Missing reference|error while decoding)', re.IGNORECASE)
STREAM_CAUSE_RE = re.compile(
    r'refused|timed out|timeout|resolve|Server returned|HTTP error|not on whitelist|Protocol not found|'
    r'Option .* not found|dimensions not set|Could not find codec parameters|Invalid data found|'
    r'does not contain any stream|Connection reset|unreachable|No route to host|End of file|'
    r'Input/output error|Permission denied|Unauthorized|Forbidden', re.IGNORECASE)
# Errors that mean the source needs a longer look before ffmpeg knows its streams.
STREAM_PROBE_RE = re.compile(
    r'dimensions not set|Could not find codec parameters|unspecified size|Could not write header', re.IGNORECASE)


def clean_stream_url(value):
    """The URL of a stream to open: one of ffmpeg's network schemes, returned with the scheme in lower case."""
    if value is None:
        raise ApiError(400, 'url is required', field='url', hint='send {"url": "http://camera.lan:8554/"}')
    if not isinstance(value, str):
        raise ApiError(400, 'url must be a string', field='url')
    url = value.strip()
    if not url:
        raise ApiError(400, 'url must not be empty', field='url')
    if len(url) > STREAM_URL_MAX:
        raise ApiError(400, f'url is longer than {STREAM_URL_MAX} characters', field='url')
    if STREAM_BAD_URL_RE.search(url):
        raise ApiError(400, 'url must not contain spaces or control characters', field='url',
                       hint='percent-encode them')
    try:
        parts = urlsplit(url)
        host, port = parts.hostname, parts.port
    except ValueError:
        raise ApiError(400, 'url has an invalid address or port', field='url') from None
    if parts.scheme not in STREAM_SCHEMES:  # urlsplit lower-cases the scheme
        raise ApiError(400, f'url scheme must be one of {", ".join(STREAM_SCHEMES)}', field='url')
    if not host:
        raise ApiError(400, 'url needs a host', field='url')
    if port == 0:
        raise ApiError(400, 'url has an invalid address or port', field='url')
    return parts.scheme + url[len(parts.scheme):]


def redact_url(url):
    """The URL with any user:password@ shown as ***@: the board shows it to everyone who can open the page."""
    try:
        parts = urlsplit(url)
        if not (parts.username or parts.password):
            return url
        return urlunsplit((parts.scheme, '***@' + parts.netloc.rpartition('@')[2], parts.path, parts.query,
                           parts.fragment))
    except ValueError:
        return _USERINFO_RE.sub(r'\1***@', url)


def redact_text(text, url):
    """text (ffmpeg's stderr, say) with the stream's URL redacted and any other user:password@ hidden."""
    return _USERINFO_RE.sub(r'\1***@', text.replace(url, redact_url(url)))


def find_ffmpeg():
    return shutil.which('ffmpeg')


def _ffmpeg_env():
    # The board is for a LAN: no proxy from the environment, whatever case it is spelled in.
    return {key: value for key, value in os.environ.items() if not key.lower().endswith('_proxy')}


_ffmpeg_majors = {}


def ffmpeg_major(exe):
    """ffmpeg's major version (5 for 5.1.6); None when it cannot be told (a git build), which counts as new."""
    try:
        st = os.stat(exe)
    except OSError:
        return None
    key = (exe, st.st_mtime_ns, st.st_size)
    if key not in _ffmpeg_majors:
        major = None
        try:
            out = subprocess.run([exe, '-version'], stdin=subprocess.DEVNULL, capture_output=True, timeout=10,
                                 env=_ffmpeg_env()).stdout
            match = re.search(rb'version n?(\d+)', out)
            major = int(match.group(1)) if match else None
        except (OSError, subprocess.SubprocessError):
            pass
        _ffmpeg_majors[key] = major
    return _ffmpeg_majors[key]


def ffmpeg_argv(exe, url, level=0, audio=True, major=None):
    """The ffmpeg command line for a stream: the video copied, the audio made AAC stereo, fragmented MP4 on stdout.
    level picks how long ffmpeg looks at the source first (a long GOP joined mid-way needs more); audio=False
    leaves the audio out."""
    scheme = url.partition(':')[0].lower()
    analyze, probe = STREAM_PROBES[min(max(level, 0), len(STREAM_PROBES) - 1)]
    micros = str(STREAM_IO_TIMEOUT * 1000000)
    argv = [exe, '-hide_banner', '-nostdin', '-nostats', '-loglevel', 'error', '-protocol_whitelist', STREAM_PROTOCOLS]
    if scheme in ('rtsp', 'rtsps'):
        # RTSP is a demuxer, not a protocol, so -rw_timeout would be an unused option and ffmpeg refuses those.
        # Before ffmpeg 5, -timeout meant "listen for a connection"; the socket timeout was -stimeout.
        argv += ['-rtsp_transport', 'tcp', '-stimeout' if major is not None and major < 5 else '-timeout', micros]
    else:
        argv += ['-rw_timeout', micros]  # never -timeout here: for rtmp it makes ffmpeg listen
    argv += ['-fflags', '+nobuffer', '-analyzeduration', str(analyze), '-probesize', str(probe), '-i', url,
             '-map', '0:v:0?']
    argv += ['-map', '0:a:0?', '-c:a', 'aac', '-b:a', '128k', '-ac', '2'] if audio else ['-an']
    # A short interleave delta keeps one silent track from holding the other back.
    argv += ['-c:v', 'copy', '-max_interleave_delta', '1000000', '-f', 'mp4',
             '-movflags', '+frag_keyframe+empty_moov+default_base_moof', '-frag_duration', '200000', 'pipe:1']
    return argv


def _stderr_text(raw):
    """One stderr line without its [module @ 0x...] prefix; None when it is empty or decoder noise."""
    line = raw.strip()
    tag = ''
    match = STREAM_PREFIX_RE.match(line)
    if match:
        tag, line = match.group(1), line[match.end():]
    line = _squash(line)
    if not line or tag in STREAM_NOISY_TAGS or STREAM_NOISE_RE.search(line):
        return None
    return line


def ffmpeg_error(lines, url, rc=None):
    """One line saying why a run failed, from ffmpeg's stderr: the first line that names a known cause, else the
    first that is not noise. Redacted and shortened. None when there is nothing to show, or the run ended cleanly."""
    if rc == 0:
        return None
    cleaned = [line for line in map(_stderr_text, lines) if line]
    pick = next((line for line in cleaned if STREAM_CAUSE_RE.search(line)), cleaned[0] if cleaned else None)
    if pick is None:
        return None
    pick = redact_text(pick, url)
    return pick if len(pick) <= 200 else pick[:199] + '…'


# Fragmented MP4, as much as the board needs of it

def mp4_boxes(data, start=0, end=None):
    """The boxes in data[start:end] as (type, payload_start, box_end). Stops at a truncated box; a size that is
    smaller than the box's own header raises ValueError."""
    end = len(data) if end is None else end
    pos = start
    while pos + 8 <= end:
        size, kind = struct.unpack_from('>I4s', data, pos)
        header = 8
        if size == 1:
            if pos + 16 > end:
                return
            size, header = struct.unpack_from('>Q', data, pos + 8)[0], 16
        elif size == 0:
            size = end - pos
        if size < header:
            raise ValueError('a box smaller than its header')
        if pos + size > end:
            return
        yield kind.decode('latin-1'), pos + header, pos + size
        pos += size


def _mp4_find(data, start, end, *path):
    """The (payload_start, box_end) of the first box named by the last item of path, looked up level by level."""
    for name in path:
        for kind, payload, stop in mp4_boxes(data, start, end):
            if kind == name:
                start, end = payload, stop
                break
        else:
            return None
    return start, end


def _mp4_esds_codec(data, pos, end):
    """mp4a.<object type>.<audio object type> from an esds box's descriptors (pos is after its version and flags)."""
    oti = aot = None
    while pos < end:
        tag = data[pos]
        pos += 1
        length = 0
        for _ in range(4):
            byte = data[pos]
            pos += 1
            length = (length << 7) | (byte & 0x7f)
            if not byte & 0x80:
                break
        if tag == 0x03:  # ES_Descriptor: ES_ID and flags, then optional fields, then the descriptors inside
            flags = data[pos + 2]
            pos += 3
            if flags & 0x80:
                pos += 2
            if flags & 0x40:
                pos += 1 + data[pos]
            if flags & 0x20:
                pos += 2
        elif tag == 0x04:  # DecoderConfigDescriptor: object type, then 12 bytes before the descriptors inside
            oti = data[pos]
            pos += 13
        elif tag == 0x05:  # DecoderSpecificInfo: the AudioSpecificConfig starts with the audio object type
            aot = data[pos] >> 3
            if aot == 31:
                aot = 32 + (((data[pos] & 7) << 3) | (data[pos + 1] >> 5))
            break
        else:
            pos += length
    if oti is None:
        return 'mp4a'
    return f'mp4a.{oti:x}.{aot}' if aot is not None else f'mp4a.{oti:x}'


def _mp4_codec(data, kind, payload, stop):
    if kind in ('avc1', 'avc3'):
        found = _mp4_find(data, payload + 78, stop, 'avcC')  # a video sample entry has 78 bytes before its boxes
        if found:
            return '%s.%02X%02X%02X' % (kind, *data[found[0] + 1:found[0] + 4])
    elif kind == 'mp4a':
        found = _mp4_find(data, payload + 28, stop, 'esds')  # an audio sample entry has 28
        if found:
            return _mp4_esds_codec(data, found[0] + 4, found[1])
    return kind  # hev1, hvc1, av01...: named bare, which a browser will most likely refuse


def _mp4_track(data, start, end):
    tkhd = _mp4_find(data, start, end, 'tkhd')
    hdlr = _mp4_find(data, start, end, 'mdia', 'hdlr')
    stsd = _mp4_find(data, start, end, 'mdia', 'minf', 'stbl', 'stsd')
    if not (tkhd and hdlr and stsd):
        return None
    track_id = struct.unpack_from('>I', data, tkhd[0] + (20 if data[tkhd[0]] == 1 else 12))[0]
    width, height = (value >> 16 for value in struct.unpack_from('>II', data, tkhd[1] - 8))  # 16.16 fixed point
    entries = list(mp4_boxes(data, stsd[0] + 8, stsd[1]))
    return track_id, {'kind': bytes(data[hdlr[0] + 8:hdlr[0] + 12]).decode('latin-1'),
                      'codec': _mp4_codec(data, *entries[0]) if entries else '?', 'width': width, 'height': height}


def mp4_init_info(moov):
    """What a moov box (with its header) says: {'tracks': {id: {'kind', 'codec', 'width', 'height'}}, 'trex':
    {id: default sample flags}}. kind is the handler (vide, soun) and codec an RFC 6381 string (avc1.42C028,
    mp4a.40.2), or the bare sample entry code. ValueError for a moov it cannot read."""
    try:
        root = _mp4_find(moov, 0, len(moov), 'moov')
        if root is None:
            raise ValueError('not a moov box')
        tracks, trex = {}, {}
        for kind, payload, stop in mp4_boxes(moov, *root):
            if kind == 'mvex':
                for sub, start, _ in mp4_boxes(moov, payload, stop):
                    if sub == 'trex':
                        track_id, _, _, _, flags = struct.unpack_from('>5I', moov, start + 4)
                        trex[track_id] = flags
            elif kind == 'trak':
                track = _mp4_track(moov, payload, stop)
                if track:
                    tracks[track[0]] = track[1]
        return {'tracks': tracks, 'trex': trex}
    except (struct.error, IndexError) as exc:
        raise ValueError(f'unreadable moov: {exc}') from None


def _first_sample_sync(data, pos, default):
    """Whether the first sample of a trun box (pos is its payload) is a sync sample; default is the flags the
    track fragment header or the trex gave."""
    flags = int.from_bytes(data[pos + 1:pos + 4], 'big')
    count = struct.unpack_from('>I', data, pos + 4)[0]
    pos += 8 + (4 if flags & 0x1 else 0)  # the data offset
    if count == 0:
        return False
    if flags & 0x4:  # first_sample_flags
        first = struct.unpack_from('>I', data, pos)[0]
    elif flags & 0x400:  # per-sample flags, after the sample's duration and size
        first = struct.unpack_from('>I', data, pos + (4 if flags & 0x100 else 0) + (4 if flags & 0x200 else 0))[0]
    else:
        first = default
    return not first & 0x10000  # sample_is_non_sync_sample


def mp4_fragment_info(moof, trex=None):
    """The tracks a moof box (with its header) carries: {track_id: whether its first sample is a sync sample,
    that is a keyframe}. ValueError for a moof it cannot read."""
    try:
        root = _mp4_find(moof, 0, len(moof), 'moof')
        if root is None:
            raise ValueError('not a moof box')
        out = {}
        for kind, payload, stop in mp4_boxes(moof, *root):
            if kind != 'traf':
                continue
            tfhd = _mp4_find(moof, payload, stop, 'tfhd')
            if tfhd is None:
                continue
            flags = int.from_bytes(moof[tfhd[0] + 1:tfhd[0] + 4], 'big')
            track_id = struct.unpack_from('>I', moof, tfhd[0] + 4)[0]
            pos = tfhd[0] + 8
            for bit, size in ((0x1, 8), (0x2, 4), (0x8, 4), (0x10, 4)):  # fields before default_sample_flags
                if flags & bit:
                    pos += size
            default = struct.unpack_from('>I', moof, pos)[0] if flags & 0x20 else (trex or {}).get(track_id, 0)
            trun = _mp4_find(moof, payload, stop, 'trun')
            out[track_id] = trun is not None and _first_sample_sync(moof, trun[0], default)
        return out
    except (struct.error, IndexError) as exc:
        raise ValueError(f'unreadable moof: {exc}') from None


def stream_mime(tracks):
    """The MIME type, with codecs, that a MediaSource needs for these tracks (video first)."""
    ordered = sorted(tracks.values(), key=lambda track: track['kind'] != 'vide')
    kind = 'video' if any(track['kind'] == 'vide' for track in ordered) else 'audio'
    return f'{kind}/mp4; codecs="{",".join(track["codec"] for track in ordered)}"'


class BoxSplitter:
    """Cuts a byte stream into top-level MP4 boxes."""

    def __init__(self):
        self.buf = bytearray()

    def feed(self, chunk):
        """The boxes now complete, as (type, bytes); ValueError for a size that cannot be right."""
        buf = self.buf
        buf += chunk
        out, pos = [], 0
        while len(buf) - pos >= 8:
            size, kind = struct.unpack_from('>I4s', buf, pos)
            header = 8
            if size == 1:
                if len(buf) - pos < 16:
                    break
                size, header = struct.unpack_from('>Q', buf, pos + 8)[0], 16
            if size < header or size > STREAM_BOX_MAX:
                raise ValueError(f'a box of {size} bytes')
            if len(buf) - pos < size:
                break
            out.append((kind.decode('latin-1'), bytes(buf[pos:pos + size])))
            pos += size
        del buf[:pos]
        return out


class FragmentReader:
    """Turns ffmpeg's fragmented MP4 into one init segment (ftyp and moov) and then fragments (a moof and its mdat)."""

    def __init__(self):
        self.splitter = BoxSplitter()
        self.ftyp = b''
        self.moof = None
        self.init = self.mime = None
        self.tracks, self.trex = {}, {}
        self.video = self.audio = None  # track ids

    def feed(self, chunk):
        """-> [('init', init_bytes, mime, tracks) or ('fragment', bytes, sync, track_ids)]. A fragment is sync when
        the video track's first sample is a keyframe (every fragment of an audio-only stream is). ValueError for
        output that is not fragmented MP4 as ffmpeg writes it."""
        events = []
        for kind, box in self.splitter.feed(chunk):
            if kind == 'ftyp':
                self.ftyp = box
            elif kind == 'moov':
                info = mp4_init_info(box)
                if not info['tracks']:
                    raise ValueError('a moov without tracks')
                self.tracks, self.trex = info['tracks'], info['trex']
                self.video = next((i for i, track in self.tracks.items() if track['kind'] == 'vide'), None)
                self.audio = next((i for i, track in self.tracks.items() if track['kind'] == 'soun'), None)
                self.init, self.mime = self.ftyp + box, stream_mime(self.tracks)
                events.append(('init', self.init, self.mime, self.tracks))
            elif kind == 'moof':
                self.moof = box
            elif kind == 'mdat' and self.moof is not None and self.init is not None:
                info = mp4_fragment_info(self.moof, self.trex)
                sync = True if self.video is None else info.get(self.video, False)
                events.append(('fragment', self.moof + box, sync, tuple(info)))
                self.moof = None
            # mfra, free and the like are not needed
        return events


def _stop_process(proc):
    """Ask ffmpeg to stop and kill it if it has not by STREAM_KILL_AFTER seconds; -> its exit status."""
    if proc.poll() is None:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(STREAM_KILL_AFTER)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            proc.wait()
    return proc.returncode


def _close_pipes(proc):
    for pipe in (proc.stdout, proc.stderr):
        try:
            pipe.close()
        except OSError:
            pass


class StreamViewer:
    """One media connection's queue of fragments. The supervisor offers to it and never waits for it."""

    def __init__(self):
        self.cond = threading.Condition()
        self.items = collections.deque()
        self.closed = self.dropped = False

    def offer(self, data):
        """False once the viewer is closed, or when it fell too far behind and was closed for it."""
        with self.cond:
            if self.closed:
                return False
            if len(self.items) >= STREAM_QUEUE_MAX:
                self.closed = self.dropped = True
                self.items.clear()
                self.cond.notify_all()
                return False
            self.items.append(data)
            self.cond.notify()
            return True

    def end(self):
        with self.cond:
            self.closed = True
            self.items.clear()
            self.cond.notify_all()

    def next(self, timeout):
        """The next fragment; b'' when none came within timeout seconds; None once the viewer is closed."""
        with self.cond:
            if not self.items and not self.closed:
                self.cond.wait(timeout)
            if self.items:
                return self.items.popleft()
            return None if self.closed else b''


class StreamChannel:
    """One open stream. A supervisor thread keeps an ffmpeg running for its URL, cuts the output into fragments and
    hands them to the viewers (one per page). It lives until stop() and is never reused."""

    def __init__(self, sid, url, opened_at):
        self.id, self.url, self.shown, self.opened_at = sid, url, redact_url(url), opened_at
        self.cond = threading.Condition()  # guards what follows, down to stopping
        self.state, self.since, self.error = 'connecting', ts(), None
        self.init = self.mime = self.gop = None  # gop: the fragments since the last keyframe fragment
        self.gop_bytes = 0
        self.tracks = {}
        self.viewers = set()
        self.waiting = 0  # media requests waiting in attach() for an init segment
        self.proc = None
        self.stopped = False
        self.audio = True  # False once the source proved to have no usable audio
        self.level = 0  # how long ffmpeg looks at the source first (STREAM_PROBES)
        self.restart = False  # the same URL was posted again to try the audio again
        self.stopping = threading.Event()
        self.wake = threading.Event()  # cuts a backoff wait short
        self.thread = self.after = None

    def start(self, after=None):
        """Start the supervisor. With after (the channel this one replaces) it first waits for that one's ffmpeg to
        be gone, so a source that serves a single client does not turn the new connection away."""
        self.after = after
        self.thread = threading.Thread(target=self._supervise, name=f'stream-{self.id}', daemon=True)
        self.thread.start()

    def stop(self):
        """Ask the channel to end; does not wait for it. Every viewer ends now."""
        self.stopping.set()
        self.wake.set()
        with self.cond:
            self.stopped = True
            proc, viewers = self.proc, list(self.viewers)
            self.viewers.clear()
            self.cond.notify_all()
        for viewer in viewers:
            viewer.end()
        if proc is not None:
            try:
                proc.terminate()
            except OSError:
                pass

    def kick(self):
        """The same URL was posted again: try now if it is not live; if it is live without the audio it lost, try
        the audio again."""
        with self.cond:
            if self.state == 'live':
                if self.audio:
                    return
                self.audio, self.restart = True, True
            else:
                self.audio, self.level = True, 0
        self.wake.set()

    def snapshot(self):
        with self.cond:
            tracks = self.tracks.values()
            video = next((track for track in tracks if track['kind'] == 'vide'), None)
            audio = next((track for track in tracks if track['kind'] == 'soun'), None)
            return {'id': self.id, 'url': self.shown, 'opened_at': self.opened_at, 'state': self.state,
                    'since': self.since, 'error': self.error, 'video': video and video['codec'],
                    'audio': audio and audio['codec'], 'width': video and video['width'],
                    'height': video and video['height'], 'viewers': len(self.viewers),
                    'media': f'/api/stream/media?id={self.id}'}

    def attach(self, timeout):
        """Register a viewer once there is an init segment (waiting up to timeout seconds for a restarting ffmpeg's).
        -> (viewer, init segment, the cached fragments, mime). Taking the cache and registering happen under one
        lock, so the viewer sees every fragment once and in order."""
        with self.cond:
            if len(self.viewers) + self.waiting >= STREAM_VIEWERS_MAX:
                raise ApiError(503, f'the stream has {STREAM_VIEWERS_MAX} viewers already', {'Retry-After': '10'})
            self.waiting += 1
            try:
                self.cond.wait_for(lambda: self.stopped or self.init is not None, timeout)
            finally:
                self.waiting -= 1
            if self.stopped:
                raise ApiError(410, 'the stream was replaced or closed')
            if self.init is None:
                extra = {'hint': self.error} if self.error else {}
                raise ApiError(503, 'the stream is not connected yet', {'Retry-After': '2'}, **extra)
            viewer = StreamViewer()
            self.viewers.add(viewer)
            return viewer, self.init, list(self.gop or ()), self.mime

    def detach(self, viewer):
        with self.cond:
            self.viewers.discard(viewer)

    def _supervise(self):
        after, self.after = self.after, None
        if after is not None and after.thread is not None:
            deadline = time.monotonic() + STREAM_KILL_AFTER + 1
            while after.thread.is_alive() and time.monotonic() < deadline and not self.stopping.is_set():
                time.sleep(0.05)
        attempt = 0
        while not self.stopping.is_set():
            self.wake.clear()
            try:
                exe = find_ffmpeg()
                if exe is None:
                    self._end_run('ffmpeg is not installed on the board host (sudo apt install ffmpeg)')
                    wait = 10
                else:
                    attempt = 0 if self._run(exe) else attempt + 1
                    wait = STREAM_BACKOFF[min(max(attempt - 1, 0), len(STREAM_BACKOFF) - 1)]
            except Exception:  # whatever it was, it counts as a failed run: the supervisor itself must not die
                if self.stopping.is_set():
                    break
                log.exception('stream %s: the supervisor failed', self.id)
                self._end_run('internal error (see the board log)')
                attempt += 1
                wait = STREAM_BACKOFF[min(attempt - 1, len(STREAM_BACKOFF) - 1)]
            self.wake.wait(wait)

    def _run(self, exe):
        """One ffmpeg process, from its start to its end. True when it was live for at least STREAM_HEALTHY seconds."""
        analyze = STREAM_PROBES[min(self.level, len(STREAM_PROBES) - 1)][0] / 1e6
        argv = ffmpeg_argv(exe, self.url, self.level, self.audio, ffmpeg_major(exe))
        log.debug('stream %s: %s', self.id, redact_text(' '.join(argv), self.url))
        self.restart = False
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    bufsize=0, env=_ffmpeg_env(), start_new_session=True)
        except OSError as exc:
            self._end_run(f'cannot run {exe}: {exc.strerror or exc}')
            return False
        with self.cond:
            stopped = self.stopped
            if not stopped:
                self.proc = proc
        if stopped:  # stop() came in before the process was registered, so it could not stop it
            _stop_process(proc)
            _close_pipes(proc)
            return False

        chunks = queue.Queue()  # unbounded on purpose: the pump must never wait, or it would never see the end
        head, tail = [], collections.deque(maxlen=STREAM_ERROR_LINES)  # the first and the last lines of stderr

        def pump():
            try:
                while True:
                    data = os.read(proc.stdout.fileno(), 65536)
                    if not data:
                        break
                    chunks.put(data)
            except OSError:
                pass
            finally:
                chunks.put(None)

        def drain():
            rest = b''
            try:
                while True:
                    data = os.read(proc.stderr.fileno(), 4096)
                    if not data:
                        break
                    *lines, rest = (rest + data).split(b'\n')
                    rest = rest[-2000:]
                    for raw in lines:
                        keep(raw)
            except OSError:
                pass
            keep(rest)

        def keep(raw):
            line = _stderr_text(raw.decode('utf-8', 'replace'))
            if line:
                (head if len(head) < STREAM_ERROR_LINES else tail).append(line)

        threads = [threading.Thread(target=pump, daemon=True), threading.Thread(target=drain, daemon=True)]
        for thread in threads:
            thread.start()

        reader = FragmentReader()
        started = last_data = time.monotonic()
        init_at = live_at = last_audio = last_video = None
        error, rc = None, None
        try:
            while not self.stopping.is_set():
                try:
                    chunk = chunks.get(timeout=1)
                except queue.Empty:
                    chunk = b''
                now = time.monotonic()
                if chunk is None:
                    break
                if chunk:
                    last_data = now
                    for event in reader.feed(chunk):
                        if event[0] == 'init':
                            init_at = now
                            self._set_init(event[1], event[2], event[3])
                            continue
                        _, data, sync, ids = event
                        if sync and live_at is None:
                            live_at = last_audio = last_video = now
                        if reader.audio in ids:
                            last_audio = now
                        if reader.video in ids:
                            last_video = now
                        self._publish(data, sync)
                if self.restart:
                    error = 'restarting to try the audio again'
                elif init_at is None and now - started > analyze + STREAM_NO_INIT:
                    # Nothing came out: a source that announces audio it never sends keeps ffmpeg from writing the
                    # header. Look at the video alone next time.
                    error = f'no output after {int(now - started)}s' + ('; trying again without audio' if self.audio else '')
                    self.audio = False
                elif init_at is not None and live_at is None and now - init_at > STREAM_FIRST_DATA:
                    error = f'no keyframe from the source within {STREAM_FIRST_DATA}s'
                elif live_at is not None and now - last_data > STREAM_STALL:
                    error = f'no data from the source for {STREAM_STALL}s'
                elif (live_at is not None and reader.audio is not None and reader.video is not None
                      and now - last_audio > STREAM_AUDIO_GAP and now - last_video <= 1):
                    # The browser plays only what both tracks have buffered, so audio that stops would freeze the video.
                    self.audio = False
                    error = 'the source stopped sending audio; showing video only'
                if error:
                    break
        except ValueError as exc:
            error = 'ffmpeg produced unreadable output'
            log.info('stream %s: %s (%s)', self.id, error, exc)
        finally:
            rc = _stop_process(proc)
            for thread in threads:
                thread.join(1)
            _close_pipes(proc)

        if error is None and not self.stopping.is_set():
            error = (ffmpeg_error(head + list(tail), self.url, rc)
                     or ('the source ended' if rc == 0 else f'ffmpeg exited with status {rc}'))
        healthy = live_at is not None and time.monotonic() - live_at >= STREAM_HEALTHY
        if healthy:
            self.level = 0
        elif error and STREAM_PROBE_RE.search(' '.join(head + list(tail))):
            self.level = min(self.level + 1, len(STREAM_PROBES) - 1)
        self._end_run(error)
        return healthy

    def _set_init(self, init, mime, tracks):
        with self.cond:
            if self.stopped:
                return
            self.init, self.mime, self.tracks = init, mime, tracks
            self.gop, self.gop_bytes = None, 0
            self.cond.notify_all()

    def _publish(self, frag, sync):
        with self.cond:
            if self.stopped:
                return
            if sync:
                self.gop, self.gop_bytes = [frag], len(frag)
                if self.state != 'live':
                    self.state, self.since, self.error = 'live', ts(), None
                    log.info('stream %s live: %s (%s)', self.id, self.shown,
                             ', '.join(track['codec'] for track in self.tracks.values()))
            elif self.gop is not None:
                self.gop.append(frag)
                self.gop_bytes += len(frag)
                if self.gop_bytes > STREAM_GOP_MAX:
                    self.gop = None  # a late joiner then starts at the next keyframe
            for viewer in list(self.viewers):
                if not viewer.offer(frag):
                    self.viewers.discard(viewer)
                    if viewer.dropped:
                        log.info('stream %s: dropped a viewer that fell %d fragments behind', self.id, STREAM_QUEUE_MAX)

    def _end_run(self, error):
        """A run is over: its viewers end (their pages reconnect to the next one) and the channel is connecting again."""
        was_live = changed = False
        with self.cond:
            self.proc = self.init = self.mime = self.gop = None
            self.gop_bytes = 0
            self.tracks = {}
            viewers = list(self.viewers)
            self.viewers.clear()
            if not self.stopped:
                was_live = self.state == 'live'
                if was_live:
                    self.state, self.since = 'connecting', ts()
                changed = error != self.error
                self.error = error
            self.cond.notify_all()
        for viewer in viewers:
            viewer.end()
        if not self.stopped and error:
            (log.info if was_live or changed else log.debug)('stream %s: %s', self.id, error)


class StreamHub:
    """The board's one open stream (or none), saved to a file so a restart reopens it."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()  # guards the fields below; held only for quick, non-blocking work
        self._chan = None
        self._retired = []  # replaced or closed channels, until their threads are gone
        self._closing = False

    def snapshot(self):
        chan = self._chan  # no lock: a poll never waits behind a file write
        return chan.snapshot() if chan else None

    def channel(self, sid):
        chan = self._chan
        if chan is None:
            raise ApiError(404, 'no stream is open', hint='POST /api/stream {"url": "..."} opens one')
        if sid and sid != chan.id:
            raise ApiError(410, f'stream {sid} was replaced or closed')
        return chan

    def open(self, url):
        """Open url, replacing any stream. -> (channel, existing, warnings); the same URL as the open one is kept."""
        with self._lock:
            if self._closing:
                raise ApiError(503, 'the board is shutting down', {'Retry-After': '5'})
            old = self._chan
            if old is not None and old.url == url:
                old.kick()
                return old, True, []
            chan = StreamChannel(secrets.token_hex(6), url, ts())
            warnings = self._save(chan)
            self._chan = chan
            if old is not None:
                old.stop()
                self._retire(old)
            chan.start(after=old)  # under the lock, so no one can see a channel that was not started
        log.info('stream %s opened: %s%s', chan.id, chan.shown, f' (replacing {old.id})' if old else '')
        return chan, False, warnings

    def close(self):
        """-> (whether a stream was open, warnings)."""
        with self._lock:
            chan, self._chan = self._chan, None
            warnings = self._forget()
            if chan is not None:
                chan.stop()
                self._retire(chan)
        if chan is not None:
            log.info('stream %s closed', chan.id)
        return chan is not None, warnings

    def _retire(self, chan):
        self._retired = [c for c in self._retired if c.thread is not None and c.thread.is_alive()] + [chan]

    def _save(self, chan):
        text = json.dumps({'id': chan.id, 'url': chan.url, 'opened_at': chan.opened_at}) + '\n'
        temp = self.path.with_name(f'.{self.path.name}.{os.getpid()}.tmp')
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with os.fdopen(os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w', encoding='utf-8') as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp, self.path)
        except OSError as exc:
            temp.unlink(missing_ok=True)
            return [f'the stream was not saved for restarts: {exc.strerror or exc}']
        return []

    def _forget(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError as exc:
            return [f'could not remove {self.path}: {exc.strerror or exc}']
        return []

    def restore(self):
        """Reopen the stream a previous run left in the file; anything wrong with the file is logged and ignored."""
        try:
            raw = self.path.read_bytes()  # decoded by json.loads, whose ValueError (UnicodeDecodeError too) is caught below
        except FileNotFoundError:
            return
        except OSError as exc:
            log.warning('stream file %s: cannot read it: %s', self.path, exc.strerror or exc)
            return
        try:
            data = json.loads(raw)
            url = clean_stream_url(data['url'])
            sid = data.get('id')
            opened_at = data.get('opened_at')
        except (ValueError, KeyError, TypeError, RecursionError, ApiError) as exc:  # RecursionError: JSON nested too deep
            log.warning('stream file %s ignored: %s', self.path, exc)
            return
        if not (isinstance(sid, str) and re.fullmatch(r'[0-9a-f]{12}', sid)):
            sid = secrets.token_hex(6)
        if isinstance(opened_at, bool) or not isinstance(opened_at, (int, float)) or not math.isfinite(opened_at):
            opened_at = ts()
        with self._lock:
            if self._closing or self._chan is not None:
                return
            chan = self._chan = StreamChannel(sid, url, opened_at)
            chan.start()
        log.info('stream %s reopened after a restart: %s', chan.id, chan.shown)
        if find_ffmpeg() is None:
            log.warning('ffmpeg is not installed, so the stream cannot play (sudo apt install ffmpeg)')

    def shutdown(self, timeout=4):
        """Stop every channel, ffmpeg included, and wait for them; the saved file stays, for the next start."""
        try:
            with self._lock:
                self._closing = True
                chans = ([self._chan] if self._chan is not None else []) + list(self._retired)
            for chan in chans:
                chan.stop()
            deadline = time.monotonic() + timeout
            for chan in chans:
                try:
                    chan.thread.join(max(0.0, deadline - time.monotonic()))
                except (AttributeError, RuntimeError):  # never started
                    pass
        except Exception:
            log.exception('stopping the stream failed')


# Storage

SCHEMA = (
    """CREATE TABLE requests (
        id TEXT PRIMARY KEY, title TEXT NOT NULL, origin TEXT,
        status TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')), message TEXT,
        created_at REAL NOT NULL, updated_at REAL NOT NULL, completed_at REAL,
        next_seq INTEGER NOT NULL DEFAULT 1
    ) STRICT""",
    'CREATE INDEX requests_status ON requests (status, completed_at)',
    """CREATE TABLE tasks (
        id TEXT PRIMARY KEY, request_id TEXT NOT NULL REFERENCES requests (id) ON DELETE CASCADE,
        seq INTEGER NOT NULL, title TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
        percent REAL NOT NULL DEFAULT 0 CHECK (percent BETWEEN 0 AND 100), message TEXT, eta_at REAL,
        created_at REAL NOT NULL, started_at REAL, start_inferred INTEGER NOT NULL DEFAULT 0,
        updated_at REAL NOT NULL, completed_at REAL,
        UNIQUE (request_id, seq)
    ) STRICT""",
)


def _migrate_2(db):
    # Attention columns and the event log. Checked step by step, so running it twice changes nothing.
    for table in ('requests', 'tasks'):
        columns = {row[1] for row in db.execute(f'PRAGMA table_info({table})')}
        for column, kind in (('attention_message', 'TEXT'), ('attention_at', 'REAL')):
            if column not in columns:
                db.execute(f'ALTER TABLE {table} ADD COLUMN {column} {kind}')
    db.execute("""CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, type TEXT NOT NULL, request_id TEXT NOT NULL,
        task_id TEXT, request_title TEXT, task_title TEXT, status TEXT, percent REAL, message TEXT,
        replayed INTEGER NOT NULL DEFAULT 0
    ) STRICT""")  # no foreign key: events outlive the requests they describe
    db.execute('CREATE INDEX IF NOT EXISTS events_ts ON events (ts)')


# user_version N-1 -> N. A fresh database is created at 1 and then runs them all, so a fresh and an
# upgraded database are the same.
MIGRATIONS = {2: _migrate_2}

# Aggregates live in SQL so the list and detail views can never disagree. {where} filters the
# inner r.* rows; {order} sorts and limits the outer g.* rows.
REQUEST_SELECT = """
SELECT g.*, c.title AS current_title, c.message AS current_message FROM (
    SELECT r.id, r.title, r.origin, r.status, r.message, r.created_at, r.updated_at, r.completed_at,
        r.attention_message, r.attention_at,
        COUNT(t.id) - IFNULL(SUM(t.status = 'cancelled'), 0) AS tasks_total,
        IFNULL(SUM(t.status = 'done'), 0) AS tasks_done,
        IFNULL(SUM(t.status = 'failed'), 0) AS tasks_failed,
        IFNULL(SUM(t.status = 'running'), 0) AS tasks_running,
        IFNULL(SUM(t.status = 'pending'), 0) AS tasks_pending,
        IFNULL(SUM(t.status = 'cancelled'), 0) AS tasks_cancelled,
        IFNULL(SUM(t.status = 'running' AND t.attention_at IS NOT NULL), 0) AS tasks_waiting,
        AVG(CASE t.status WHEN 'done' THEN 100.0 WHEN 'cancelled' THEN NULL ELSE t.percent END) AS avg_percent,
        MAX(CASE WHEN t.status = 'running' THEN t.eta_at END) AS eta_at,
        (SELECT id FROM tasks WHERE request_id = r.id AND status = 'running'
         ORDER BY updated_at DESC, seq DESC LIMIT 1) AS current_id
    FROM requests r LEFT JOIN tasks t ON t.request_id = r.id
    {where} GROUP BY r.id
) g LEFT JOIN tasks c ON c.id = g.current_id {order}
"""

# A running request r is waiting while it, or one of its running tasks, has a question for the user.
WAITING = """(r.attention_at IS NOT NULL OR EXISTS (SELECT 1 FROM tasks w WHERE w.request_id = r.id
                  AND w.status = 'running' AND w.attention_at IS NOT NULL))"""

# params: now, stale_after, now
STALE = f"""r.status = 'running' AND ? - r.updated_at > ? AND NOT {WAITING}
    AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.request_id = r.id AND t.status = 'running'
                    AND t.eta_at IS NOT NULL AND ? < t.eta_at + 60)"""

STALE_COUNT = f'SELECT COUNT(*) FROM requests r WHERE {STALE}'
STALE_LIST = f'SELECT r.id AS request_id, r.title AS request_title FROM requests r WHERE {STALE} ORDER BY r.created_at'
WAITING_COUNT = f"SELECT COUNT(*) FROM requests r WHERE r.status = 'running' AND {WAITING}"
WAITING_LIST = """
SELECT * FROM (
    SELECT r.id AS request_id, r.title AS request_title, NULL AS task_id, NULL AS task_title,
        r.attention_message AS message, r.attention_at AS since, 0 AS seq
    FROM requests r WHERE r.status = 'running' AND r.attention_at IS NOT NULL
    UNION ALL
    SELECT r.id, r.title, t.id, t.title, t.attention_message, t.attention_at, t.seq
    FROM requests r JOIN tasks t ON t.request_id = r.id
    WHERE r.status = 'running' AND t.status = 'running' AND t.attention_at IS NOT NULL
) ORDER BY since, request_id, seq
"""

# Shared by task complete and the request-complete cascade; params: status, status, message, now, now.
CLOSE_TASK_SET = """
    status = ?, percent = CASE WHEN ? = 'done' THEN 100.0 ELSE percent END,
    message = COALESCE(?, message), eta_at = NULL, attention_message = NULL, attention_at = NULL,
    completed_at = COALESCE(completed_at, ?), updated_at = ?,
    start_inferred = CASE WHEN started_at IS NULL THEN 1 ELSE start_inferred END,
    started_at = COALESCE(started_at, created_at)
"""

# The titles are copied in, so an event still reads well after its request is gone.
EVENT_INSERT = """
INSERT INTO events (ts, type, request_id, task_id, request_title, task_title, status, percent, message, replayed)
VALUES (?, ?, ?, ?, (SELECT title FROM requests WHERE id = ?), (SELECT title FROM tasks WHERE id = ?), ?, ?, ?, ?)
"""


class Store:
    """One SQLite connection behind one lock: every step is atomic without busy-retry code.

    Writes take now (the server clock) and, for a replayed write, at (its own time, already clamped to
    the replay window); the effective time and the replayed flag travel together as a stamp."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute('PRAGMA journal_mode=WAL')
        self._db.execute('PRAGMA synchronous=NORMAL')
        self._db.execute('PRAGMA foreign_keys=ON')
        self._db.execute('PRAGMA busy_timeout=1500')
        with self._write() as db:
            version = db.execute('PRAGMA user_version').fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(f'schema version {version} is newer than this server supports ({SCHEMA_VERSION})')
            fresh = version < 1
            if fresh:
                for statement in SCHEMA:
                    db.execute(statement)
                version = 1
            for target in range(version + 1, SCHEMA_VERSION + 1):
                MIGRATIONS[target](db)
            if version != SCHEMA_VERSION:
                db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')
                if not fresh:
                    log.info('database %s upgraded from schema %d to %d', path, version, SCHEMA_VERSION)

    @contextmanager
    def _write(self):
        with self._lock:
            self._db.execute('BEGIN IMMEDIATE')
            try:
                yield self._db
                self._db.execute('COMMIT')
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute('ROLLBACK')
                raise

    @contextmanager
    def _read(self):
        with self._lock:
            yield self._db

    def close(self):
        with self._lock:
            try:
                self._db.execute('PRAGMA optimize')
            except sqlite3.Error:
                pass
            self._db.close()

    @staticmethod
    def _stamp(now, at, floor=None):
        # (effective time, replayed): a replayed write's own time, never before the entity's last write.
        if at is None:
            return now, False
        return (at if floor is None else max(at, floor)), True

    @staticmethod
    def _requests(db, where, params=(), order=''):
        return db.execute(REQUEST_SELECT.format(where=where, order=order), params).fetchall()

    @staticmethod
    def _tasks(db, rid, first_seq=1):
        return db.execute('SELECT * FROM tasks WHERE request_id = ? AND seq >= ? ORDER BY seq',
                          (rid, first_seq)).fetchall()

    def _detail(self, db, rid):
        rows = self._requests(db, 'WHERE r.id = ?', (rid,))
        if not rows:
            raise ApiError(404, f"unknown request '{rid}'")
        return rows[0], self._tasks(db, rid)

    @staticmethod
    def _request(db, rid):
        row = db.execute('SELECT * FROM requests WHERE id = ?', (rid,)).fetchone()
        if row is None:
            raise ApiError(404, f"unknown request '{rid}'")
        return row

    @staticmethod
    def _task(db, tid):
        row = db.execute('SELECT * FROM tasks WHERE id = ?', (tid,)).fetchone()
        if row is None:
            raise ApiError(404, f"unknown task '{tid}' (request may have been deleted)")
        return row

    @staticmethod
    def _touch(db, rid, when):
        # A task write bumps its request; MAX keeps an older replayed write from moving it back.
        db.execute('UPDATE requests SET updated_at = MAX(updated_at, ?) WHERE id = ?', (when, rid))

    @staticmethod
    def _event(db, stamp, kind, rid, task=None, status=None, message=None):
        # A task event carries the task's status and percent, a request event the request's.
        if task is not None:
            tid, status, percent = task['id'], task['status'], task['percent']
        else:
            tid = None
            percent = db.execute("SELECT AVG(CASE status WHEN 'done' THEN 100.0 WHEN 'cancelled' THEN NULL"
                                 ' ELSE percent END) FROM tasks WHERE request_id = ?', (rid,)).fetchone()[0]
            percent = round(percent, 1) if percent is not None else 100.0 if status == 'done' else 0.0
        db.execute(EVENT_INSERT, (stamp[0], kind, rid, tid, rid, tid, status, percent, message, int(stamp[1])))

    @staticmethod
    def _insert_tasks(db, rid, first_seq, titles, status, now):
        started = now if status == 'running' else None
        db.executemany(
            'INSERT INTO tasks (id, request_id, seq, title, status, created_at, started_at, updated_at)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            [(f'{rid}-{seq}', rid, seq, title, status, now, started, now)
             for seq, title in enumerate(titles, first_seq)])

    def create_request(self, rid, title, origin, titles, now, at=None):
        """-> (row, tasks, existing). A known rid with the same title is an idempotent replay."""
        with self._write() as db:
            if rid is None:
                for _ in range(20):
                    rid = ''.join(secrets.choice(ID_ALPHABET) for _ in range(6))
                    if db.execute('SELECT 1 FROM requests WHERE id = ?', (rid,)).fetchone() is None:
                        break
                else:
                    raise RuntimeError('could not allocate a free request id')
            else:
                known = db.execute('SELECT title FROM requests WHERE id = ?', (rid,)).fetchone()
                if known is not None:
                    if known['title'] != title:
                        raise ApiError(409, 'request id already exists')
                    return (*self._detail(db, rid), True)
            stamp = self._stamp(now, at)
            db.execute(
                'INSERT INTO requests (id, title, origin, status, created_at, updated_at, next_seq)'
                " VALUES (?, ?, ?, 'running', ?, ?, ?)", (rid, title, origin, stamp[0], stamp[0], len(titles) + 1))
            self._insert_tasks(db, rid, 1, titles, 'pending', stamp[0])
            self._event(db, stamp, 'request_created', rid, status='running')
            return (*self._detail(db, rid), False)

    def request_detail(self, rid):
        with self._read() as db:
            return self._detail(db, rid)

    def list_requests(self, status, limit, now, stale_after):
        with self._read() as db:
            rows = []
            if status in (None, 'running'):
                rows += self._requests(db, "WHERE r.status = 'running'", (RUNNING_LIST_MAX,),
                                       'ORDER BY g.created_at DESC LIMIT ?')
            if status != 'running':
                where, params = ('WHERE r.status = ?', (status, limit)) if status else \
                    ("WHERE r.status != 'running'", (limit,))
                rows += self._requests(db, where, params, 'ORDER BY g.completed_at DESC, g.created_at DESC LIMIT ?')
            counts = {'running': 0, 'stale': 0, 'done': 0, 'failed': 0, 'waiting': 0}
            counts.update(db.execute('SELECT status, COUNT(*) FROM requests GROUP BY status').fetchall())
            counts['stale'] = db.execute(STALE_COUNT, (now, stale_after, now)).fetchone()[0]
            counts['waiting'] = db.execute(WAITING_COUNT).fetchone()[0]
            return rows, counts

    def running_count(self):
        with self._read() as db:
            return db.execute("SELECT COUNT(*) FROM requests WHERE status = 'running'").fetchone()[0]

    def delete_request(self, rid, now):
        with self._write() as db:
            request = self._request(db, rid)
            self._event(db, (now, False), 'request_deleted', rid, status=request['status'])
            db.execute('DELETE FROM requests WHERE id = ?', (rid,))

    def complete_request(self, rid, status, message, now, at=None):
        with self._write() as db:
            request = self._request(db, rid)
            stamp = self._stamp(now, at, request['updated_at'])
            auto_closed, cancelled = self._close_request(db, request, status, message, stamp)
            return (*self._detail(db, rid), auto_closed, cancelled, request['status'])

    def _close_request(self, db, request, status, message, stamp):
        rid, when, open_tasks, waiting = request['id'], stamp[0], [], []
        if request['status'] == 'running':
            open_tasks = db.execute("SELECT id, status FROM tasks WHERE request_id = ?"
                                    " AND status IN ('pending', 'running') ORDER BY seq", (rid,)).fetchall()
            waiting = db.execute('SELECT id, attention_message FROM tasks WHERE request_id = ?'
                                 ' AND attention_at IS NOT NULL ORDER BY seq', (rid,)).fetchall()
            db.execute("UPDATE tasks SET status = 'cancelled', completed_at = ?, updated_at = ?"
                       " WHERE request_id = ? AND status = 'pending'", (when, when, rid))
            db.execute(f"UPDATE tasks SET {CLOSE_TASK_SET} WHERE request_id = ? AND status = 'running'",
                       (status, status, None, when, when, rid))
        elif request['status'] == status and message is None:
            return [], []
        # A closed request always has completed_at, so COALESCE keeps the original close time.
        db.execute('UPDATE requests SET status = ?, message = COALESCE(?, message), completed_at = COALESCE('
                   'completed_at, ?), updated_at = ?, attention_message = NULL, attention_at = NULL WHERE id = ?',
                   (status, message, when, when, rid))
        for task in waiting:
            self._event(db, stamp, 'attention_cleared', rid, self._task(db, task['id']),
                        message=task['attention_message'])
        if request['attention_at'] is not None:
            self._event(db, stamp, 'attention_cleared', rid, status=status, message=request['attention_message'])
        if request['status'] != status:
            self._event(db, stamp, f'request_{status}', rid, status=status,
                        message=request['message'] if message is None else message)
        return ([t['id'] for t in open_tasks if t['status'] == 'running'],
                [t['id'] for t in open_tasks if t['status'] == 'pending'])

    def add_tasks(self, rid, titles, start, now):
        with self._write() as db:
            request = self._request(db, rid)
            count = db.execute('SELECT COUNT(*) FROM tasks WHERE request_id = ?', (rid,)).fetchone()[0]
            if count + len(titles) > TASKS_PER_REQUEST:
                raise ApiError(409, f'request {rid} has {count} tasks; adding {len(titles)} would exceed '
                                    f'the limit of {TASKS_PER_REQUEST}')
            reopened = request['status'] != 'running'
            if reopened:
                db.execute("UPDATE requests SET status = 'running', completed_at = NULL, message = NULL"
                           ' WHERE id = ?', (rid,))
            db.execute('UPDATE requests SET next_seq = next_seq + ?, updated_at = MAX(updated_at, ?) WHERE id = ?',
                       (len(titles), now, rid))
            self._insert_tasks(db, rid, request['next_seq'], titles, 'running' if start else 'pending', now)
            tasks, stamp = self._tasks(db, rid, request['next_seq']), (now, False)
            if reopened:
                self._event(db, stamp, 'request_reopened', rid, status='running')
            for task in tasks:
                self._event(db, stamp, 'task_added', rid, task)
            if start:
                self._event(db, stamp, 'task_started', rid, tasks[0])
            return tasks, reopened

    def task(self, tid):
        with self._read() as db:
            return self._task(db, tid)

    def progress(self, tid, message, percent, eta_seconds, now, at=None):
        with self._write() as db:
            task = self._task(db, tid)
            if task['status'] not in ('pending', 'running'):
                raise ApiError(409, f"task {tid} is already {task['status']}", status=task['status'])
            stamp = self._stamp(now, at, task['updated_at'])
            when = stamp[0]
            eta_at = None if eta_seconds is None else round(when + eta_seconds, 3)
            db.execute("UPDATE tasks SET status = 'running', started_at = COALESCE(started_at, ?),"
                       ' percent = COALESCE(?, percent), message = ?, eta_at = ?, attention_message = NULL,'
                       ' attention_at = NULL, updated_at = ? WHERE id = ?', (when, percent, message, eta_at, when, tid))
            self._touch(db, task['request_id'], when)
            row = self._task(db, tid)
            if task['attention_at'] is not None:
                self._event(db, stamp, 'attention_cleared', row['request_id'], row, message=task['attention_message'])
            self._event(db, stamp, 'task_started' if task['status'] == 'pending' else 'task_progress',
                        row['request_id'], row, message=message)
            return row

    def complete_task(self, tid, status, message, now, at=None):
        with self._write() as db:
            task = self._task(db, tid)
            stamp = self._stamp(now, at, task['updated_at'])
            # On a task already closed with this status CLOSE_TASK_SET changes only message and updated_at.
            if task['status'] != status or message is not None:
                db.execute(f'UPDATE tasks SET {CLOSE_TASK_SET} WHERE id = ?',
                           (status, status, message, stamp[0], stamp[0], tid))
                self._touch(db, task['request_id'], stamp[0])
            row = self._task(db, tid)
            if task['attention_at'] is not None:
                self._event(db, stamp, 'attention_cleared', row['request_id'], row, message=task['attention_message'])
            if task['status'] != status:
                self._event(db, stamp, f'task_{status}', row['request_id'], row, message=row['message'])
            return row, task['status']

    def task_attention(self, tid, message, now, at=None):
        with self._write() as db:
            task = self._task(db, tid)
            if task['status'] not in ('pending', 'running'):
                raise ApiError(409, f"task {tid} is already {task['status']}", status=task['status'])
            stamp = self._stamp(now, at, task['updated_at'])
            db.execute("UPDATE tasks SET status = 'running', started_at = COALESCE(started_at, ?),"
                       ' attention_message = ?, attention_at = ?, updated_at = ? WHERE id = ?',
                       (stamp[0], message, stamp[0], stamp[0], tid))
            self._touch(db, task['request_id'], stamp[0])
            row = self._task(db, tid)
            if task['status'] == 'pending':
                self._event(db, stamp, 'task_started', row['request_id'], row, message=row['message'])
            self._event(db, stamp, 'attention', row['request_id'], row, message=message)
            return row

    def task_resume(self, tid, now, at=None):
        with self._write() as db:
            task = self._task(db, tid)
            if task['attention_at'] is None:
                return task, False
            stamp = self._stamp(now, at, task['updated_at'])
            db.execute('UPDATE tasks SET attention_message = NULL, attention_at = NULL, updated_at = ? WHERE id = ?',
                       (stamp[0], tid))
            self._touch(db, task['request_id'], stamp[0])
            row = self._task(db, tid)
            self._event(db, stamp, 'attention_cleared', row['request_id'], row, message=task['attention_message'])
            return row, True

    def request_attention(self, rid, message, now, at=None):
        with self._write() as db:
            request = self._request(db, rid)
            if request['status'] != 'running':
                raise ApiError(409, f"request {rid} is already {request['status']}", status=request['status'],
                               hint='the request is closed')
            stamp = self._stamp(now, at, request['updated_at'])
            db.execute('UPDATE requests SET attention_message = ?, attention_at = ?, updated_at = ? WHERE id = ?',
                       (message, stamp[0], stamp[0], rid))
            self._event(db, stamp, 'attention', rid, status='running', message=message)
            return self._detail(db, rid)

    def request_resume(self, rid, now, at=None):
        with self._write() as db:
            request = self._request(db, rid)
            cleared = request['attention_at'] is not None
            if cleared:
                stamp = self._stamp(now, at, request['updated_at'])
                db.execute('UPDATE requests SET attention_message = NULL, attention_at = NULL, updated_at = ?'
                           ' WHERE id = ?', (stamp[0], rid))
                self._event(db, stamp, 'attention_cleared', rid, status=request['status'],
                            message=request['attention_message'])
            return (*self._detail(db, rid), cleared)

    def events(self, since, limit, now, stale_after):
        with self._read() as db:
            # The last id ever handed out (sqlite_sequence), so a cursor never moves back after pruning.
            first, top, seq = db.execute("SELECT MIN(id), MAX(id), (SELECT seq FROM sqlite_sequence"
                                         " WHERE name = 'events') FROM events").fetchone()
            last = max(top or 0, seq or 0)
            events, truncated, cursor = [], False, last
            if since is not None:
                rows = db.execute('SELECT * FROM events WHERE id > ? ORDER BY id LIMIT ?',
                                  (since, limit + 1)).fetchall()
                events = rows[:limit]
                more = len(rows) > limit
                pruned = since < (last + 1 if first is None else first) - 1
                truncated = more or pruned
                if more:
                    cursor = events[-1]['id']
            waiting = db.execute(WAITING_LIST).fetchall()
            stale = db.execute(STALE_LIST, (now, stale_after, now)).fetchall()
        return {'cursor': cursor, 'events': [event_json(row) for row in events], 'truncated': truncated,
                'waiting': [{key: row[key] for key in ('request_id', 'request_title', 'task_id', 'task_title',
                                                       'message', 'since')} for row in waiting],
                'stale': [dict(row) for row in stale]}

    def expire(self, cutoff, message, now):
        with self._write() as db:
            rows = db.execute(
                f"SELECT * FROM requests r WHERE status = 'running' AND updated_at < ? AND NOT {WAITING} AND NOT EXISTS"
                " (SELECT 1 FROM tasks t WHERE t.request_id = r.id AND t.status = 'running' AND t.eta_at > ?)",
                (cutoff, now)).fetchall()
            return [(row['id'], *self._close_request(db, row, 'failed', message, (now, False))) for row in rows]

    def prune(self, cutoff):
        with self._write() as db:
            return db.execute("DELETE FROM requests WHERE status != 'running' AND completed_at < ?",
                              (cutoff,)).rowcount

    def prune_events(self, cutoff, keep):
        with self._write() as db:
            pruned = db.execute('DELETE FROM events WHERE ts < ?', (cutoff,)).rowcount
            return pruned + db.execute('DELETE FROM events WHERE id <= (SELECT id FROM events ORDER BY id DESC'
                                       ' LIMIT 1 OFFSET ?)', (keep,)).rowcount


# Serialization

def is_stale(updated_at, eta_at, now, stale_after):
    return now - updated_at > stale_after and not (eta_at is not None and now < eta_at + 60)


def attention_json(message, since):
    return None if since is None else {'message': message, 'since': since}


def task_json(row, now, public, stale_after):
    running = row['status'] == 'running'
    eta_at, started = row['eta_at'], row['started_at']
    attention = attention_json(row['attention_message'], row['attention_at'])
    return {
        'id': row['id'], 'request_id': row['request_id'], 'seq': row['seq'], 'title': row['title'],
        'status': row['status'], 'percent': row['percent'], 'message': row['message'], 'eta_at': eta_at,
        'eta_remaining': round(max(0.0, eta_at - now), 3) if running and eta_at is not None else None,
        'created_at': row['created_at'], 'started_at': started,
        'start_inferred': bool(row['start_inferred']), 'updated_at': row['updated_at'],
        'completed_at': row['completed_at'],
        'elapsed': None if started is None else round((row['completed_at'] or now) - started, 3),
        'stale': running and attention is None and is_stale(row['updated_at'], eta_at, now, stale_after),
        'url': f"{public}/r/{row['request_id']}#{row['id']}",
        'attention': attention,
    }


def request_json(row, now, public, stale_after):
    percent = row['avg_percent']
    if percent is None:
        percent = 100.0 if row['status'] == 'done' else 0.0
    current = None
    if row['current_id'] is not None:
        current = {'id': row['current_id'], 'title': row['current_title'], 'message': row['current_message']}
    running = row['status'] == 'running'
    attention = attention_json(row['attention_message'], row['attention_at'])
    waiting = running and (attention is not None or row['tasks_waiting'] > 0)
    return {
        'id': row['id'], 'title': row['title'], 'origin': row['origin'], 'status': row['status'],
        'message': row['message'], 'created_at': row['created_at'], 'updated_at': row['updated_at'],
        'completed_at': row['completed_at'],
        'elapsed': round((row['completed_at'] or now) - row['created_at'], 3),
        'percent': round(percent, 1),
        **{key: row[key] for key in ('tasks_total', 'tasks_done', 'tasks_failed', 'tasks_running',
                                     'tasks_pending', 'tasks_cancelled')},
        'current': current, 'eta_at': row['eta_at'],
        'stale': running and not waiting and is_stale(row['updated_at'], row['eta_at'], now, stale_after),
        'url': f"{public}/r/{row['id']}",
        'attention': attention, 'waiting': waiting, 'tasks_waiting': row['tasks_waiting'],
    }


def event_json(row):
    return {**{key: row[key] for key in ('id', 'ts', 'type', 'request_id', 'task_id', 'request_title', 'task_title',
                                         'status', 'percent', 'message')},
            'replayed': bool(row['replayed'])}


# HTTP

def _route(method, pattern, handler, **fixed):
    return method, re.compile(pattern, re.IGNORECASE), handler, fixed


ROUTES = (
    _route('GET', r'/', 'index'),
    _route('GET', r'/r(?:/.*)?', 'file', rel='static/index.html'),
    _route('GET', r'/favicon\.ico', 'favicon'),
    _route('GET', r'/api(?:/usage)?', 'file', rel='templates/usage.md', render=True),
    _route('GET', r'/api/rule', 'file', rel='templates/rule.md', render=True),
    _route('GET', r'/api/changelog', 'file', rel='templates/changelog.md', render=True),
    _route('GET', r'/api/skill/SKILL\.md', 'file', rel='skill/SKILL.md'),
    _route('GET', r'/api/skill/taskctl', 'file', rel='taskctl'),
    _route('GET', r'/api/skill/taskctl\.py', 'file', rel='taskctl.py', render=True),
    _route('GET', r'/api/health', 'health'),
    _route('GET', r'/api/events', 'events'),
    _route('GET', r'/api/settings', 'get_settings'),
    _route('PUT', r'/api/settings', 'put_settings'),
    _route('DELETE', r'/api/settings', 'delete_settings'),
    _route('GET', r'/api/stream', 'get_stream'),
    _route('POST', r'/api/stream', 'open_stream'),
    _route('DELETE', r'/api/stream', 'close_stream'),
    _route('GET', r'/api/stream/media', 'stream_media'),
    _route('GET', r'/api/requests', 'list_requests'),
    _route('POST', r'/api/requests', 'create_request'),
    _route('GET', r'/api/requests/(?P<rid>[^/]+)', 'get_request'),
    _route('DELETE', r'/api/requests/(?P<rid>[^/]+)', 'delete_request'),
    _route('POST', r'/api/requests/(?P<rid>[^/]+)/complete', 'complete_request'),
    _route('POST', r'/api/requests/(?P<rid>[^/]+)/tasks', 'add_tasks'),
    _route('POST', r'/api/requests/(?P<rid>[^/]+)/attention', 'request_attention'),
    _route('DELETE', r'/api/requests/(?P<rid>[^/]+)/attention', 'request_resume'),
    _route('GET', r'/api/tasks/(?P<tid>[^/]+)', 'get_task'),
    _route('POST', r'/api/tasks/(?P<tid>[^/]+)/progress', 'progress'),
    _route('POST', r'/api/tasks/(?P<tid>[^/]+)/complete', 'complete_task'),
    _route('POST', r'/api/tasks/(?P<tid>[^/]+)/attention', 'task_attention'),
    _route('DELETE', r'/api/tasks/(?P<tid>[^/]+)/attention', 'task_resume'),
)


def host_name(hostport):
    # '[fe80::1%25eth0]:8765' -> 'fe80::1' (brackets and any zone dropped); 'box:8765' -> 'box'.
    if hostport.startswith('['):
        return hostport[1:].partition(']')[0].partition('%')[0]
    return hostport.rsplit(':', 1)[0]


def is_ip(name):
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def plain_ip(ip):
    # The dual-stack socket reports IPv4 peers as ::ffff:a.b.c.d; show them as a.b.c.d.
    return ip[7:] if ip.lower().startswith('::ffff:') and '.' in ip else ip


WILDCARD_HOSTS = ('0.0.0.0', '::', '', '*')


class TasksServer(ThreadingHTTPServer):
    request_queue_size = 64
    allow_reuse_port = False  # never let a second server silently share the port
    tls = False

    def __init__(self, host, port, handler, dual_stack=False):
        # dual_stack: one IPv6 socket on '::' that also takes IPv4 clients, so a hostname that resolves
        # to this machine's IPv6 (e.g. link-local) address reaches the board as well as its IPv4 one.
        self.address_family = socket.AF_INET6 if ':' in host else socket.AF_INET
        self.dual_stack = dual_stack
        address = (host, port)
        if ':' in host:  # the full sockaddr keeps a link-local address's %zone; (host, port) would drop it
            address = socket.getaddrinfo(host, port, socket.AF_INET6, socket.SOCK_STREAM)[0][4]
        super().__init__(address, handler)

    def server_bind(self):
        if self.dual_stack:
            self.socket.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, 0)
        super().server_bind()

    def handle_error(self, request, client_address):
        log.info('connection error from %s: %s', plain_ip(client_address[0]), sys.exc_info()[1])


class TLSTasksServer(TasksServer):
    """The HTTPS listener. Accepted sockets are wrapped here without I/O; the handshake runs in the
    request's own thread (Handler.setup), so a slow client never holds up accepting others."""
    tls = True

    def __init__(self, host, port, handler, dual_stack=False, context=None):
        self.tls_context = context
        super().__init__(host, port, handler, dual_stack)

    def get_request(self):
        sock, address = super().get_request()
        try:
            return self.tls_context.wrap_socket(sock, server_side=True, do_handshake_on_connect=False), address
        except OSError:
            sock.close()
            raise


class Handler(BaseHTTPRequestHandler):
    server_version = f'tasks/{VERSION}'
    sys_version = ''
    timeout = 30

    def setup(self):
        if self.server.tls:
            self.request.settimeout(self.timeout)
            self.request.do_handshake()  # a failure ends up in TasksServer.handle_error
        super().setup()

    def _dispatch(self):
        method = 'GET' if self.command == 'HEAD' else self.command
        self._t0 = time.monotonic()
        self._error_note = ''
        self.warnings = []
        self.replay = False
        path = self.path
        try:
            try:
                split = urlsplit(self.path)
            except ValueError:  # e.g. an absolute-form target such as http://[x/
                raise ApiError(400, 'malformed request target') from None
            self.query = parse_qs(split.query)
            path = unquote(split.path).rstrip('/') or '/'
            handler, params = self._match(method, path)
            if method in ('POST', 'PUT', 'DELETE'):
                self._check_origin()
            getattr(self, 'h_' + handler)(**params)
        except ApiError as exc:
            self._send_json(exc.code, exc.payload, exc.headers)
        except Exception as exc:
            if isinstance(exc, sqlite3.OperationalError) and ('locked' in str(exc) or 'busy' in str(exc)):
                self._send_json(503, {'error': 'database busy'}, {'Retry-After': '1'})
                return
            log.exception('%s %s failed', method, _printable(path))
            self._send_json(500, {'error': 'internal error'})

    # Unrouted methods still get a JSON 405 instead of http.server's HTML 501.
    do_GET = do_HEAD = do_POST = do_DELETE = do_PUT = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = _dispatch

    def send_error(self, code, message=None, explain=None):
        # http.server's own rejections (bad request line, 414, 431, an unknown method's 501) get the
        # JSON error shape and headers too. They can come before a path was parsed, and before a
        # version was, which would leave HTTP/0.9's bare body without a status line or headers.
        self._t0 = time.monotonic()
        self.command, self.path = self.command or '-', getattr(self, 'path', '-')
        self.request_version = 'HTTP/1.0'
        self.close_connection = True
        self._send_json(code, {'error': message or self.responses.get(code, ('error',))[0]}, {'Connection': 'close'})

    def _match(self, method, path):
        allowed = []
        for route_method, pattern, handler, fixed in ROUTES:
            match = pattern.fullmatch(path)
            if match is None:
                continue
            if route_method != method:
                allowed.append(route_method)
                continue
            params = dict(fixed)
            for key, raw in match.groupdict().items():
                params[key] = self._check_id(key, raw.lower(), path[match.end(key):], method)
            return handler, params
        if allowed:
            raise ApiError(405, f'{method} is not allowed on {path}', {'Allow': ', '.join(allowed)})
        raise ApiError(404, f'no route for {method} {path}', hint=f'GET {self.base_url()}/api/usage')

    @staticmethod
    def _check_id(kind, value, suffix, method):
        rid_match, tid_match = RID_RE.fullmatch(value), TID_RE.fullmatch(value)
        if kind == 'rid':
            if rid_match:
                return value
            if tid_match:
                tid, parent = f'{tid_match[1]}-{int(tid_match[2])}', tid_match[1]
                if suffix == '/attention' or (suffix in ('', '/complete') and method != 'DELETE'):
                    hint = f'{tid} is a task id; use /api/tasks/{tid}{suffix}'
                else:
                    hint = f'{tid} is a task id; its request is {parent}'
                raise ApiError(400, f"'{tid}' is a task id, not a request id", hint=hint)
            raise ApiError(400, f"malformed request id '{value[:40]}'", hint='request ids look like k3m9qa')
        if tid_match:
            return f'{tid_match[1]}-{int(tid_match[2])}'
        if rid_match:
            hint = (f'{value} is a request id; use /api/requests/{value}{suffix}'
                    if suffix in ('', '/complete', '/attention')
                    else f'{value} is a request id; task ids look like {value}-1')
            raise ApiError(400, f"'{value}' is a request id, not a task id", hint=hint)
        raise ApiError(400, f"malformed task id '{value[:40]}'", hint='task ids look like k3m9qa-3')

    def _check_origin(self):
        origin = self.headers.get('Origin')
        if origin is None:
            return
        host = (self.headers.get('Host') or '').strip().lower()
        name = host_name(host)
        try:
            netloc = urlsplit(origin.strip()).netloc.lower()
        except ValueError:  # e.g. Origin: http://[abc
            raise ApiError(403, 'cross-origin write refused') from None
        if not host or netloc != host or not (is_ip(name) or name in self.server.local_names):
            raise ApiError(403, 'cross-origin write refused')

    def _body(self, known):
        if 'chunked' in self.headers.get('Transfer-Encoding', '').lower():
            raise ApiError(411, 'chunked request bodies are not supported; send Content-Length')
        try:
            length = int(self.headers.get('Content-Length', 0))
            if length < 0:
                raise ValueError
        except ValueError:
            raise ApiError(400, 'invalid Content-Length') from None
        if length > BODY_MAX:
            self.close_connection = True
            raise ApiError(413, f'request body is larger than {BODY_MAX // 1024} KiB')
        try:
            data = self.rfile.read(length)
        except OSError:
            raise ApiError(400, 'could not read the request body') from None
        if len(data) < length:
            raise ApiError(400, 'request body is shorter than Content-Length')
        try:
            text = data.decode('utf-8-sig')
            body = json.loads(text) if text.strip() else {}
        except UnicodeDecodeError:
            raise ApiError(400, 'request body is not UTF-8') from None
        except (ValueError, RecursionError) as exc:
            raise ApiError(400, f'invalid JSON: {exc}', hint='send a JSON object, e.g. {"title": "..."}') from None
        if not isinstance(body, dict):
            raise ApiError(400, 'request body must be a JSON object')
        check_fields(body, known, self.warnings)
        return body

    def _write_body(self, known):
        # The writes taskctl may queue offline: a replay (X-Tasks-Replay: 1) may also carry "at".
        self.replay = self.headers.get('X-Tasks-Replay', '').strip() == '1'
        return self._body(known | {'at'} if self.replay else known)

    def _at(self, body, now):
        """A replayed write's own time, clamped to [now - REPLAY_WINDOW, now]; None when not a replay."""
        if not self.replay:
            return None
        if body.get('at') is None:
            return now
        try:
            at = parse_number(body['at'], 'at')
        except ApiError:
            self.warnings.append('at ignored: it must be a number (epoch seconds)')
            return now
        if at > now:
            self.warnings.append(f'at clamped to now ({at - now:.3g}s in the future)')
        elif at < now - REPLAY_WINDOW:
            self.warnings.append(f'at clamped to {REPLAY_WINDOW // 3600}h ago')
        return round(min(now, max(now - REPLAY_WINDOW, at)), 3)

    def base_url(self):
        scheme = 'https' if self.server.tls else 'http'
        host = (self.headers.get('Host') or '').strip()
        if HOST_RE.fullmatch(host):
            return f'{scheme}://{host}'
        if self.server.public_url:
            return self.server.public_url
        ip, port = self.connection.getsockname()[:2]
        ip = plain_ip(ip).partition('%')[0]  # a link-local address's %zone is only meaningful here
        return f'{scheme}://[{ip}]:{port}' if ':' in ip else f'{scheme}://{ip}:{port}'

    def agent_url(self):
        # BASE_URL of the rendered agent files. On the HTTPS listener it is plain http on the main port:
        # agents stay on http, where a self-signed certificate cannot break urllib.
        if not self.server.tls:
            return self.base_url()
        match = HOST_RE.fullmatch((self.headers.get('Host') or '').strip())
        if match:
            name = match.group(1)
        else:
            name = plain_ip(self.connection.getsockname()[0]).partition('%')[0]
            name = f'[{name}]' if ':' in name else name
        return f'http://{name}:{self.server.http_port}'

    def public_url(self):
        return self.server.public_url or self.base_url()

    def _send(self, code, body, content_type, cache, headers=None):
        try:
            self.send_response(code)
            if content_type:
                self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('X-Content-Type-Options', 'nosniff')
            if cache:
                self.send_header('Cache-Control', cache)
            self.send_header('X-Tasks-Version', VERSION)
            self.send_header('X-Tasks-Docs', docs_version())
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            if body and self.command != 'HEAD':
                self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True
        line = f'{self.address_string()} {self.command} {_printable(self.path)} {code} ' \
               f'{(time.monotonic() - self._t0) * 1000:.0f}ms'
        if code >= 400:
            log.info('%s %s', line, _printable(self._error_note))
        else:
            log.debug('%s', line)

    def _send_json(self, code, payload, headers=None):
        if code >= 400:
            self._error_note = payload.get('error', '')
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8', 'replace')
        self._send(code, body, 'application/json', 'no-store', headers)

    def _reply(self, code, payload):
        if self.warnings:
            payload['warnings'] = self.warnings
        self._send_json(code, payload)

    def log_request(self, code='-', size='-'):
        pass  # _send logs every response with its timing

    def address_string(self):
        return plain_ip(self.client_address[0])

    def log_message(self, format, *args):
        log.info('%s %s', self.address_string(), _printable(format % args))

    def _task(self, row, now):
        return task_json(row, now, self.public_url(), self.server.stale_after)

    def _detail(self, row, tasks, now):
        public, stale_after = self.public_url(), self.server.stale_after
        return {**request_json(row, now, public, stale_after),
                'tasks': [task_json(task, now, public, stale_after) for task in tasks], 'now': now}

    def h_index(self):
        if 'text/html' in self.headers.get('Accept', '').lower():
            self.h_file('static/index.html', vary=True)
        else:
            self.h_file('templates/usage.md', render=True, vary=True)

    def h_file(self, rel, render=False, vary=False):
        try:
            data = (TASKS_DIR / rel).read_bytes()
        except OSError:
            raise ApiError(500, f'template missing: {rel}') from None
        if render:
            data = (data.decode('utf-8').replace('{{BASE_URL}}', self.agent_url())
                    .replace('{{PUBLIC_URL}}', self.public_url()).replace('{{VERSION}}', VERSION)
                    .replace('{{DOCS_VERSION}}', docs_version()).encode('utf-8'))
        html = rel.endswith('.html')
        self._send(200, data, 'text/html; charset=utf-8' if html else 'text/plain; charset=utf-8',
                   'no-cache' if html else 'no-store', {'Vary': 'Accept'} if vary else None)

    def h_favicon(self):
        self._send(204, b'', None, None)

    def h_health(self):
        self._reply(200, {'ok': True, 'service': 'tasks', 'version': VERSION, 'pid': os.getpid(), 'now': ts(),
                          'started_at': self.server.started_at,
                          'requests_running': self.server.store.running_count(),
                          'docs_version': docs_version(), 'tls_port': self.server.tls_port,
                          'config_path': str(self.server.settings.path)})

    def _query(self, name):
        values = self.query.get(name)
        return values[-1].strip().lower() if values else None

    def _int_query(self, name, default, low, high):
        if self._query(name) is None:
            return default
        try:
            value = int(self._query(name))
        except ValueError:
            raise ApiError(400, f'{name} must be an integer', field=name) from None
        if not low <= value <= high:
            self.warnings.append(f'{name} {value} clamped to {low}..{high}')
            value = min(high, max(low, value))
        return value

    def h_list_requests(self):
        status = self._query('status')
        if status is not None and status not in ('running', 'done', 'failed'):
            raise ApiError(400, 'status must be running, done or failed', field='status')
        limit = self._int_query('limit', 100, 1, 500)
        now, stale_after, public = ts(), self.server.stale_after, self.public_url()
        rows, counts = self.server.store.list_requests(status, limit, now, stale_after)
        self._reply(200, {'now': now, 'stale_after': stale_after, 'counts': counts,
                          'requests': [request_json(row, now, public, stale_after) for row in rows]})

    def h_create_request(self):
        body, w = self._write_body({'title', 'origin', 'tasks', 'id'}), self.warnings
        title = clean_text(body.get('title'), 'title', TITLE_MAX, w, required=True)
        origin = clean_text(body.get('origin'), 'origin', ORIGIN_MAX, w)
        titles = [] if body.get('tasks') is None else clean_titles(body['tasks'], 'tasks', w)
        rid = clean_rid(body.get('id'))
        now = ts()
        row, tasks, existing = self.server.store.create_request(rid, title, origin, titles, now, self._at(body, now))
        if existing:
            log.info('request %s already exists (a replayed create)', row['id'])
        else:
            log.info("created request %s '%s' with %d task(s) from %s%s", row['id'], title, len(tasks), origin or '?',
                     ' (replayed)' if self.replay else '')
        self._reply(200 if existing else 201, {**self._detail(row, tasks, now), 'existing': existing})

    def h_get_request(self, rid):
        now = ts()
        payload = self._detail(*self.server.store.request_detail(rid), now)
        self._reply(200, {**payload, 'stale_after': self.server.stale_after})

    def h_delete_request(self, rid):
        now = ts()
        self.server.store.delete_request(rid, now)
        log.info('deleted request %s', rid)
        self._reply(200, {'deleted': rid, 'now': now})

    def h_complete_request(self, rid):
        body, w = self._write_body({'status', 'message'}), self.warnings
        status = parse_status(body.get('status'))
        message = clean_text(body.get('message'), 'message', MESSAGE_MAX, w, multiline=True)
        now = ts()
        row, tasks, auto_closed, cancelled, previous = self.server.store.complete_request(
            rid, status, message, now, self._at(body, now))
        if previous != status:
            log.info('request %s %s (was %s; auto-closed %d, cancelled %d)', rid, status, previous,
                     len(auto_closed), len(cancelled))
        self._reply(200, {**self._detail(row, tasks, now), 'auto_closed': auto_closed, 'cancelled': cancelled})

    def h_add_tasks(self, rid):
        body, w = self._body({'title', 'titles', 'start'}), self.warnings
        bulk = body.get('titles') is not None
        if bulk:
            if body.get('title') is not None:
                raise ApiError(400, 'send either title or titles, not both', field='titles')
            titles = clean_titles(body['titles'], 'titles', w)
            if not titles:
                raise ApiError(400, 'titles must not be empty', field='titles')
            if body.get('start') is not None:
                w.append('start is ignored with titles; the tasks are created pending')
            start = False
        else:
            titles = [clean_text(body.get('title'), 'title', TITLE_MAX, w, required=True)]
            start = True if body.get('start') is None else body['start']
            if not isinstance(start, bool):
                raise ApiError(400, 'start must be true or false', field='start')
        now = ts()
        tasks, reopened = self.server.store.add_tasks(rid, titles, start, now)
        log.info('added %s to request %s%s', ', '.join(t['id'] for t in tasks), rid, ' (reopened)' if reopened else '')
        if bulk:
            self._reply(201, {'now': now, 'request_id': rid, 'tasks': [self._task(t, now) for t in tasks],
                              'reopened': reopened})
        else:
            self._reply(201, {**self._task(tasks[0], now), 'now': now, 'reopened': reopened})

    def h_request_attention(self, rid):
        body, w = self._write_body({'message'}), self.warnings
        message = clean_text(body.get('message'), 'message', MESSAGE_MAX, w, required=True, multiline=True)
        now = ts()
        row, tasks = self.server.store.request_attention(rid, message, now, self._at(body, now))
        log.info('request %s is waiting for input', rid)
        self._reply(200, self._detail(row, tasks, now))

    def h_request_resume(self, rid):
        body = self._write_body(set())
        now = ts()
        row, tasks, cleared = self.server.store.request_resume(rid, now, self._at(body, now))
        if cleared:
            log.info('request %s resumed', rid)
        self._reply(200, self._detail(row, tasks, now))

    def h_get_task(self, tid):
        now = ts()
        self._reply(200, {**self._task(self.server.store.task(tid), now), 'now': now})

    def h_progress(self, tid):
        body, w = self._write_body({'message', 'percent', 'eta_seconds'}), self.warnings
        message = clean_text(body.get('message'), 'message', MESSAGE_MAX, w, required=True, multiline=True)
        percent = None if body.get('percent') is None else parse_percent(body['percent'], w)
        eta = None if body.get('eta_seconds') is None else parse_eta(body['eta_seconds'], w)
        now = ts()
        row = self.server.store.progress(tid, message, percent, eta, now, self._at(body, now))
        log.debug('progress %s %s%% %s', tid, row['percent'], _printable(message))
        self._reply(200, {**self._task(row, now), 'now': now})

    def h_complete_task(self, tid):
        body, w = self._write_body({'status', 'message'}), self.warnings
        status = parse_status(body.get('status'))
        message = clean_text(body.get('message'), 'message', MESSAGE_MAX, w, multiline=True)
        now = ts()
        row, previous = self.server.store.complete_task(tid, status, message, now, self._at(body, now))
        if previous != status:
            log.info('task %s %s (was %s)', tid, status, previous)
        self._reply(200, {**self._task(row, now), 'now': now})

    def h_task_attention(self, tid):
        body, w = self._write_body({'message'}), self.warnings
        message = clean_text(body.get('message'), 'message', MESSAGE_MAX, w, required=True, multiline=True)
        now = ts()
        row = self.server.store.task_attention(tid, message, now, self._at(body, now))
        log.info('task %s is waiting for input', tid)
        self._reply(200, {**self._task(row, now), 'now': now})

    def h_task_resume(self, tid):
        body = self._write_body(set())
        now = ts()
        row, cleared = self.server.store.task_resume(tid, now, self._at(body, now))
        if cleared:
            log.info('task %s resumed', tid)
        self._reply(200, {**self._task(row, now), 'now': now})

    def h_events(self):
        since = self._query('since')
        if since is not None:
            try:
                since = int(since)
                if not 0 <= since < 2 ** 63:
                    raise ValueError
            except ValueError:
                raise ApiError(400, 'since must be an event id (an integer >= 0)', field='since',
                               hint="omit since for a baseline; each reply's cursor is the next since") from None
        limit = self._int_query('limit', 500, 1, EVENTS_PER_CALL)
        version = self.server.settings.current_version()
        now = ts()
        self._reply(200, {'now': now, **self.server.store.events(since, limit, now, self.server.stale_after),
                          'settings_version': version, 'stream': self.server.stream.snapshot()})

    def h_get_settings(self):
        self._reply(200, self.server.settings.snapshot(ts()))

    def h_put_settings(self):
        body = self._body({'settings'})
        if body.get('settings') is None:
            raise ApiError(400, 'settings is required', field='settings',
                           hint='send {"settings": {"events": {"task_done": {"sound": true}}}}')
        self.server.settings.update(check_settings(body['settings'], 'settings'))
        log.info('settings saved to %s', self.server.settings.path)
        self._reply(200, self.server.settings.snapshot(ts()))

    def h_delete_settings(self):
        self.server.settings.reset()
        log.info('settings reset to the defaults (%s removed)', self.server.settings.path)
        self._reply(200, self.server.settings.snapshot(ts()))

    def _stream_payload(self, **extra):
        return {'now': ts(), 'stream': self.server.stream.snapshot(), 'ffmpeg': find_ffmpeg(), **extra}

    def h_get_stream(self):
        self._reply(200, self._stream_payload())

    def h_open_stream(self):
        body = self._body({'url'})
        _, existing, warnings = self.server.stream.open(clean_stream_url(body.get('url')))
        self.warnings.extend(warnings)
        if find_ffmpeg() is None:
            self.warnings.append('ffmpeg is not installed on the board host; the stream starts once it is '
                                 '(sudo apt install ffmpeg)')
        self._reply(200 if existing else 201, self._stream_payload(existing=existing))

    def h_close_stream(self):
        closed, warnings = self.server.stream.close()
        self.warnings.extend(warnings)
        self._reply(200, self._stream_payload(closed=closed))

    def _stream_headers(self, mime):
        self.send_response(200)
        self.send_header('Content-Type', mime)
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Tasks-Version', VERSION)
        self.send_header('X-Tasks-Docs', docs_version())
        self.send_header('Connection', 'close')
        self.end_headers()

    def h_stream_media(self):
        """The open stream as fragmented MP4: no Content-Length, the body ends when the channel's run does."""
        chan = self.server.stream.channel(self._query('id'))
        if self.command == 'HEAD':  # routed as GET; it only says what a GET would send
            if chan.mime is None:
                raise ApiError(503, 'the stream is not connected yet', {'Retry-After': '2'})
            self._stream_headers(chan.mime)
            return
        viewer, init, gop, mime = chan.attach(STREAM_INIT_WAIT)
        sent, started = 0, time.monotonic()
        try:
            self._stream_headers(mime)
            for part in (init, *gop):
                self.wfile.write(part)
                sent += len(part)
            while True:
                part = viewer.next(5)
                if part is None:
                    break
                if part:
                    self.wfile.write(part)
                    sent += len(part)
        except OSError:
            pass  # the page went away, or stopped reading for Handler.timeout seconds
        except Exception:  # _dispatch would write a JSON 500 into the middle of the media
            log.exception('stream %s: a media request failed', chan.id)
        finally:
            chan.detach(viewer)
            self.close_connection = True
            log.info('%s GET /api/stream/media %s: %d bytes in %s%s', self.address_string(), chan.id, sent,
                     humanise(time.monotonic() - started), ' (dropped: too slow)' if viewer.dropped else '')


def _printable(text):
    return _CONTROL_RE.sub('?', text)


# Startup

def maintenance_loop(store, expire_after, retention_days, stop):
    interval = 600 if expire_after == 0 else min(600, expire_after / 2)
    note = f'expired: no activity for {humanise(expire_after)}'
    while True:
        try:
            now = ts()
            if expire_after:
                for rid, auto_closed, cancelled in store.expire(now - expire_after, note, now):
                    log.info('request %s expired (auto-closed %d, cancelled %d)', rid, len(auto_closed), len(cancelled))
            if retention_days:
                pruned = store.prune(now - retention_days * 86400)
                if pruned:
                    log.info('pruned %d finished request(s) older than %g days', pruned, retention_days)
            pruned = store.prune_events(now - EVENTS_MAX_AGE, EVENTS_KEEP)
            if pruned:
                log.debug('pruned %d event(s)', pruned)
        except sqlite3.OperationalError as exc:
            log.warning('maintenance pass skipped: %s', exc)
        except Exception:
            if stop.is_set():
                return
            log.exception('maintenance pass failed')
        if stop.wait(interval):
            return


def _seconds(text):
    value = float(text)
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f'expected a number >= 0, got {text!r}')
    return int(value) if value.is_integer() else value


def parse_args(argv):
    env = os.environ.get
    parser = argparse.ArgumentParser(description='Task-status board server; the API is in the module docstring.')
    parser.add_argument('--host', default=env('TASKS_HOST') or '0.0.0.0')
    parser.add_argument('--port', type=int, default=env('TASKS_PORT') or '8765')
    parser.add_argument('--db', type=Path, default=env('TASKS_DB') or str(TASKS_DIR / 'data' / 'tasks.db'))
    parser.add_argument('--public-url', default=env('TASKS_PUBLIC_URL') or None,
                        help='base for human links (e.g. http://<lan-ip>:8765); default: from the Host header')
    parser.add_argument('--config', type=Path, default=env('TASKS_CONFIG') or None,
                        help='global settings file (default: <db dir>/settings.json)')
    parser.add_argument('--tls-port', type=int, default=env('TASKS_TLS_PORT') or None,
                        help='also serve HTTPS on this port (with --tls-cert and --tls-key)')
    parser.add_argument('--tls-cert', type=Path, default=env('TASKS_TLS_CERT') or None,
                        help='PEM certificate (chain) for --tls-port')
    parser.add_argument('--tls-key', type=Path, default=env('TASKS_TLS_KEY') or None,
                        help='PEM private key (unencrypted) for --tls-port')
    parser.add_argument('--stale-after', type=_seconds, default=600,
                        help='seconds of silence before running work is stale')
    parser.add_argument('--expire-after', type=_seconds, default=86400,
                        help='seconds of silence before a running request is failed (0 disables)')
    parser.add_argument('--retention-days', type=_seconds, default=30,
                        help='days to keep finished requests (0 keeps forever)')
    parser.add_argument('--pidfile', type=Path, help='default: <db dir>/server-<port>.pid')
    parser.add_argument('--verbose', action='store_true', help='also log progress calls and successful GETs')
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error(f'--port must be 1..65535, got {args.port}')
    tls = {'--tls-port': args.tls_port, '--tls-cert': args.tls_cert, '--tls-key': args.tls_key}
    missing = [name for name, value in tls.items() if value is None]
    if 0 < len(missing) < 3:
        given = [name for name in tls if name not in missing]
        parser.error(f'{" and ".join(given)} also need{"s" if len(given) == 1 else ""} {" and ".join(missing)}: '
                     'give all three of --tls-port, --tls-cert and --tls-key, or none')
    if args.tls_port is not None:
        if not 1 <= args.tls_port <= 65535:
            parser.error(f'--tls-port must be 1..65535, got {args.tls_port}')
        if args.tls_port == args.port:
            parser.error(f'--tls-port must differ from --port ({args.port})')
    if args.public_url:
        args.public_url = args.public_url.strip().rstrip('/')
        parts = urlsplit(args.public_url)
        # The value is baked into served scripts, so reject anything that could break out of a string.
        if parts.scheme not in ('http', 'https') or not parts.hostname or re.search(r'[\s"\'\\`<>{}]', args.public_url):
            parser.error(f'--public-url must look like http://HOST:PORT, got {args.public_url!r}')
    return args


def write_pidfile(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f'{path.name}.{os.getpid()}.tmp')
    temp.write_text(f'{os.getpid()}\n')
    os.replace(temp, path)


def remove_pidfile(path):
    # A newer server may have replaced the file; only remove it while it still names us.
    try:
        if path.read_text().strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        pass


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def listen(host, port, server_class=TasksServer, **extra):
    # A wildcard host listens dual-stack; without IPv6 it falls back to IPv4 only. Any other host is
    # bound as given (an IPv6 one is IPv6 only).
    host = host.strip('[]')
    if host in WILDCARD_HOSTS:
        try:
            return server_class('::', port, Handler, dual_stack=True, **extra)
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                raise
            log.info('IPv6 is unavailable (%s); listening on IPv4 only', exc.strerror or exc)
        host = '0.0.0.0'
    return server_class(host, port, Handler, **extra)


def _listen_error(exc, host, port, flag=''):
    # flag names the option for the second listener's errors (--tls-port).
    if exc.errno == errno.EADDRINUSE:
        return (f'error: {flag}port {port} already in use (is the tasks server already running? '
                f'try: curl -s http://127.0.0.1:{port}/api/health)')
    return f'error: {flag}cannot listen on {host}:{port}: {exc.strerror or exc}'


def _shown(httpd, scheme):
    host, port = httpd.server_address[:2]
    if httpd.dual_stack:
        return f'{scheme}://0.0.0.0:{port} and {scheme}://[::]:{port}'
    return f'{scheme}://[{host}]:{port}' if ':' in host else f'{scheme}://{host}:{port}'


def main(argv=None):
    if sys.version_info < (3, 10):
        print(f'error: Python 3.10+ is required (this is {sys.version.split()[0]})', file=sys.stderr)
        return 2
    if sqlite3.sqlite_version_info < (3, 37, 0):
        print(f'error: SQLite 3.37+ is required for STRICT tables (this is {sqlite3.sqlite_version})', file=sys.stderr)
        return 2
    args = parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, stream=sys.stderr,
                        format='%(asctime)s %(levelname)s %(message)s')

    context = None
    if args.tls_port is not None:
        try:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            # An encrypted key would prompt on the terminal; an empty password makes it an error instead.
            context.load_cert_chain(args.tls_cert, args.tls_key, password=lambda: b'')
        except (OSError, ValueError) as exc:  # ssl.SSLError is an OSError
            print(f'error: cannot load the TLS certificate {args.tls_cert} and key {args.tls_key}: '
                  f'{getattr(exc, "strerror", None) or exc}', file=sys.stderr)
            return 2
    try:
        httpd = listen(args.host, args.port)
    except OSError as exc:
        print(_listen_error(exc, args.host, args.port), file=sys.stderr)
        return 2
    servers = [httpd]
    if context is not None:
        try:
            servers.append(listen(args.host, args.tls_port, TLSTasksServer, context=context))
        except OSError as exc:
            httpd.server_close()
            print(_listen_error(exc, args.host, args.tls_port, '--tls-port: '), file=sys.stderr)
            return 2
    try:
        store = Store(args.db)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        for server in servers:
            server.server_close()
        print(f'error: cannot open database {args.db}: {exc}', file=sys.stderr)
        return 2
    pidfile = args.pidfile or args.db.parent / f'server-{args.port}.pid'
    try:
        write_pidfile(pidfile)
    except OSError as exc:
        for server in servers:
            server.server_close()
        store.close()
        print(f'error: cannot write pidfile {pidfile}: {exc}', file=sys.stderr)
        return 2

    settings = Settings(Path(os.path.abspath(args.config or args.db.parent / 'settings.json')))
    settings.current_version()  # read it now, so a broken file is reported at startup
    streams = StreamHub(Path(os.path.abspath(args.db)).with_suffix('.stream.json'))
    local_names = frozenset(name.lower() for name in (
        'localhost', socket.gethostname(), urlsplit(args.public_url or '').hostname) if name)
    started_at = ts()
    for server in servers:
        server.store, server.settings, server.stream = store, settings, streams
        server.public_url = args.public_url
        server.stale_after = args.stale_after
        server.started_at = started_at
        server.local_names = local_names
        server.http_port, server.tls_port = args.port, args.tls_port

    stop = threading.Event()
    threading.Thread(target=maintenance_loop, name='maintenance', daemon=True,
                     args=(store, args.expire_after, args.retention_days, stop)).start()
    signal.signal(signal.SIGTERM, _interrupt)
    for server in servers[1:]:
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.5}, name='https', daemon=True).start()
        log.info('https listener on %s (certificate %s)', _shown(server, 'https'), args.tls_cert)
    log.info('tasks server listening on %s (db %s, pid %d, public url %s, settings %s)', _shown(httpd, 'http'),
             args.db, os.getpid(), args.public_url or 'from Host header', settings.path)
    try:
        streams.restore()
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info('shutting down')
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        stop.set()
        streams.shutdown()
        for server in servers[1:]:
            server.shutdown()
        for server in servers:
            server.server_close()
        store.close()
        remove_pidfile(pidfile)
    return 0


if __name__ == '__main__':
    sys.exit(main())
