#!/usr/bin/env python3
"""Task-status board: a small HTTP service that agents push request and task progress to.

    python3 tasks/server.py [--host 0.0.0.0] [--port 8765] [--db tasks/data/tasks.db] [--public-url URL]
        [--stale-after 600] [--expire-after 86400] [--retention-days 30] [--pidfile PATH] [--verbose]

Env fallbacks: TASKS_HOST, TASKS_PORT, TASKS_DB, TASKS_PUBLIC_URL. The pidfile defaults to
<db dir>/server-<port>.pid. Standard library only; needs Python >= 3.10 and SQLite >= 3.37.
A wildcard --host (0.0.0.0, ::, * or empty) listens dual-stack, IPv4 and IPv6 on one socket, so a
hostname that resolves to an IPv6 address works too; without IPv6 it falls back to IPv4 only. A
specific address is bound as given.

Frozen wire contract (every component is built against it)

IDs: a request id is 6 chars of 23456789abcdefghjkmnpqrstuvwxyz (k3m9qa); a task id is {rid}-{seq}
(k3m9qa-3). Case-insensitive, returned lowercase. The wrong kind of id on a route is a 400 with a hint.

Routes (JSON unless noted; bodies are parsed as JSON whatever the Content-Type; empty body = {})
  GET    /                         static/index.html if Accept has text/html, else usage (Vary: Accept)
  GET    /r/<anything>             static/index.html
  GET    /api, /api/usage          templates/usage.md, rendered, text/plain
  GET    /api/rule                 templates/rule.md, rendered, text/plain
  GET    /api/skill/SKILL.md       skill/SKILL.md, text/plain
  GET    /api/skill/taskctl        taskctl (sh wrapper), text/plain
  GET    /api/skill/taskctl.py     taskctl.py, rendered, text/plain
  GET    /favicon.ico              204
  GET    /api/health               {ok, service: "tasks", version: "1", pid, now, started_at, requests_running}
  POST   /api/requests             {title, origin?, tasks?: [str]} -> 201 Request + tasks + now
  GET    /api/requests             ?status=running|done|failed&limit=1..500 (100) -> {now, stale_after,
                                   counts: {running, stale, done, failed}, requests}: running (created_at
                                   DESC, max 500) then finished (completed_at DESC, limited); counts = all
  GET    /api/requests/{rid}       Request + tasks (by seq) + now + stale_after
  DELETE /api/requests/{rid}       {deleted, now}
  POST   /api/requests/{rid}/complete  {status?: done|failed, message?}
                                   -> Request + tasks + now + auto_closed: [tid] + cancelled: [tid]
  POST   /api/requests/{rid}/tasks {title, start?: true} -> 201 Task + now + reopened
                                   {titles: [str]} -> 201 {now, request_id, tasks (pending), reopened}
  GET    /api/tasks/{tid}          Task + now
  POST   /api/tasks/{tid}/progress {message, percent?, eta_seconds?} -> Task + now
  POST   /api/tasks/{tid}/complete {status?: done|failed, message?} -> Task + now
Rendering literally replaces {{BASE_URL}}, {{PUBLIC_URL}} and {{VERSION}}. Files are re-read per
request; a missing one is a 500 {"error": "template missing: <path>"}.

Shapes (every key always present, null when absent; times are float epoch seconds, server clock)
  Task     id request_id seq title status percent message eta_at eta_remaining created_at started_at
           start_inferred updated_at completed_at elapsed stale url
  Request  id title origin status message created_at updated_at completed_at elapsed percent tasks_total
           tasks_done tasks_failed tasks_running tasks_pending tasks_cancelled current eta_at stale url
  Task status: pending|running|done|failed|cancelled. Request status: running|done|failed.
  tasks_total counts NON-cancelled tasks (Y), tasks_done is X. Request percent = mean over non-cancelled
  tasks, done counting 100 (none: 100 if the request is done, else 0). current = {id, title, message} of
  the latest-updated running task; eta_at = max running eta_at. elapsed runs from started_at (task) or
  created_at (request) to completed_at or now. stale = running, silent > --stale-after, and not within
  60s past a running eta_at. url = <public>/r/<rid>, plus #<tid> for a task.

Errors are {error, field?, hint?}: 400 validation, 403 cross-origin write, 404 unknown id/route, 405
(+Allow), 409 state conflict, 411 chunked, 413 body > 64 KiB, 500, 503 database busy (+Retry-After: 1).
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
  - Maintenance: running requests silent for --expire-after (and not inside a running eta) close as
    failed "expired: no activity for 24h"; finished requests older than --retention-days are deleted.
Writes (POST, DELETE) carrying an Origin header are refused unless Origin equals Host and Host names
this machine (IP literal, [IPv6] included, localhost, its hostname, the --public-url host). No auth in v1.
"""

import argparse
import difflib
import errno
import ipaddress
import json
import logging
import math
import os
import re
import secrets
import signal
import socket
import sqlite3
import sys
import threading
import time
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit


VERSION = '1'
SCHEMA_VERSION = 1
TASKS_DIR = Path(__file__).resolve().parent

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

# Aggregates live in SQL so the list and detail views can never disagree. {where} filters the
# inner r.* rows; {order} sorts and limits the outer g.* rows.
REQUEST_SELECT = """
SELECT g.*, c.title AS current_title, c.message AS current_message FROM (
    SELECT r.id, r.title, r.origin, r.status, r.message, r.created_at, r.updated_at, r.completed_at,
        COUNT(t.id) - IFNULL(SUM(t.status = 'cancelled'), 0) AS tasks_total,
        IFNULL(SUM(t.status = 'done'), 0) AS tasks_done,
        IFNULL(SUM(t.status = 'failed'), 0) AS tasks_failed,
        IFNULL(SUM(t.status = 'running'), 0) AS tasks_running,
        IFNULL(SUM(t.status = 'pending'), 0) AS tasks_pending,
        IFNULL(SUM(t.status = 'cancelled'), 0) AS tasks_cancelled,
        AVG(CASE t.status WHEN 'done' THEN 100.0 WHEN 'cancelled' THEN NULL ELSE t.percent END) AS avg_percent,
        MAX(CASE WHEN t.status = 'running' THEN t.eta_at END) AS eta_at,
        (SELECT id FROM tasks WHERE request_id = r.id AND status = 'running'
         ORDER BY updated_at DESC, seq DESC LIMIT 1) AS current_id
    FROM requests r LEFT JOIN tasks t ON t.request_id = r.id
    {where} GROUP BY r.id
) g LEFT JOIN tasks c ON c.id = g.current_id {order}
"""

STALE_COUNT = """
SELECT COUNT(*) FROM requests r WHERE r.status = 'running' AND ? - r.updated_at > ?
    AND NOT EXISTS (SELECT 1 FROM tasks t WHERE t.request_id = r.id AND t.status = 'running'
                    AND t.eta_at IS NOT NULL AND ? < t.eta_at + 60)
"""

# Shared by task complete and the request-complete cascade; params: status, status, message, now, now.
CLOSE_TASK_SET = """
    status = ?, percent = CASE WHEN ? = 'done' THEN 100.0 ELSE percent END,
    message = COALESCE(?, message), eta_at = NULL,
    completed_at = COALESCE(completed_at, ?), updated_at = ?,
    start_inferred = CASE WHEN started_at IS NULL THEN 1 ELSE start_inferred END,
    started_at = COALESCE(started_at, created_at)
"""


class Store:
    """One SQLite connection behind one lock: every step is atomic without busy-retry code."""

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
            if version < 1:
                for statement in SCHEMA:
                    db.execute(statement)
                db.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

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
    def _insert_tasks(db, rid, first_seq, titles, status, now):
        started = now if status == 'running' else None
        db.executemany(
            'INSERT INTO tasks (id, request_id, seq, title, status, created_at, started_at, updated_at)'
            ' VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            [(f'{rid}-{seq}', rid, seq, title, status, now, started, now)
             for seq, title in enumerate(titles, first_seq)])

    def create_request(self, title, origin, titles, now):
        with self._write() as db:
            for _ in range(20):
                rid = ''.join(secrets.choice(ID_ALPHABET) for _ in range(6))
                if db.execute('SELECT 1 FROM requests WHERE id = ?', (rid,)).fetchone() is None:
                    break
            else:
                raise RuntimeError('could not allocate a free request id')
            db.execute(
                'INSERT INTO requests (id, title, origin, status, created_at, updated_at, next_seq)'
                " VALUES (?, ?, ?, 'running', ?, ?, ?)", (rid, title, origin, now, now, len(titles) + 1))
            self._insert_tasks(db, rid, 1, titles, 'pending', now)
            return self._detail(db, rid)

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
            counts = {'running': 0, 'stale': 0, 'done': 0, 'failed': 0}
            counts.update(db.execute('SELECT status, COUNT(*) FROM requests GROUP BY status').fetchall())
            counts['stale'] = db.execute(STALE_COUNT, (now, stale_after, now)).fetchone()[0]
            return rows, counts

    def running_count(self):
        with self._read() as db:
            return db.execute("SELECT COUNT(*) FROM requests WHERE status = 'running'").fetchone()[0]

    def delete_request(self, rid):
        with self._write() as db:
            if db.execute('DELETE FROM requests WHERE id = ?', (rid,)).rowcount == 0:
                raise ApiError(404, f"unknown request '{rid}'")

    def complete_request(self, rid, status, message, now):
        with self._write() as db:
            request = self._request(db, rid)
            auto_closed, cancelled = self._close_request(db, request, status, message, now)
            return (*self._detail(db, rid), auto_closed, cancelled, request['status'])

    @staticmethod
    def _close_request(db, request, status, message, now):
        rid, open_tasks = request['id'], []
        if request['status'] == 'running':
            open_tasks = db.execute("SELECT id, status FROM tasks WHERE request_id = ?"
                                    " AND status IN ('pending', 'running') ORDER BY seq", (rid,)).fetchall()
            db.execute("UPDATE tasks SET status = 'cancelled', completed_at = ?, updated_at = ?"
                       " WHERE request_id = ? AND status = 'pending'", (now, now, rid))
            db.execute(f"UPDATE tasks SET {CLOSE_TASK_SET} WHERE request_id = ? AND status = 'running'",
                       (status, status, None, now, now, rid))
        elif request['status'] == status and message is None:
            return [], []
        # A closed request always has completed_at, so COALESCE keeps the original close time.
        db.execute('UPDATE requests SET status = ?, message = COALESCE(?, message), completed_at = COALESCE('
                   'completed_at, ?), updated_at = ? WHERE id = ?', (status, message, now, now, rid))
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
            db.execute('UPDATE requests SET next_seq = next_seq + ?, updated_at = ? WHERE id = ?',
                       (len(titles), now, rid))
            self._insert_tasks(db, rid, request['next_seq'], titles, 'running' if start else 'pending', now)
            return self._tasks(db, rid, request['next_seq']), reopened

    def task(self, tid):
        with self._read() as db:
            return self._task(db, tid)

    def progress(self, tid, message, percent, eta_at, now):
        with self._write() as db:
            task = self._task(db, tid)
            if task['status'] not in ('pending', 'running'):
                raise ApiError(409, f"task {tid} is already {task['status']}", status=task['status'])
            db.execute("UPDATE tasks SET status = 'running', started_at = COALESCE(started_at, ?),"
                       ' percent = COALESCE(?, percent), message = ?, eta_at = ?, updated_at = ? WHERE id = ?',
                       (now, percent, message, eta_at, now, tid))
            db.execute('UPDATE requests SET updated_at = ? WHERE id = ?', (now, task['request_id']))
            return self._task(db, tid)

    def complete_task(self, tid, status, message, now):
        with self._write() as db:
            task = self._task(db, tid)
            # On a task already closed with this status CLOSE_TASK_SET changes only message and updated_at.
            if task['status'] != status or message is not None:
                db.execute(f'UPDATE tasks SET {CLOSE_TASK_SET} WHERE id = ?', (status, status, message, now, now, tid))
                db.execute('UPDATE requests SET updated_at = ? WHERE id = ?', (now, task['request_id']))
            return self._task(db, tid), task['status']

    def expire(self, cutoff, message, now):
        with self._write() as db:
            rows = db.execute(
                "SELECT * FROM requests r WHERE status = 'running' AND updated_at < ? AND NOT EXISTS"
                " (SELECT 1 FROM tasks t WHERE t.request_id = r.id AND t.status = 'running' AND t.eta_at > ?)",
                (cutoff, now)).fetchall()
            return [(row['id'], *self._close_request(db, row, 'failed', message, now)) for row in rows]

    def prune(self, cutoff):
        with self._write() as db:
            return db.execute("DELETE FROM requests WHERE status != 'running' AND completed_at < ?",
                              (cutoff,)).rowcount


# Serialization

def is_stale(updated_at, eta_at, now, stale_after):
    return now - updated_at > stale_after and not (eta_at is not None and now < eta_at + 60)


def task_json(row, now, public, stale_after):
    running = row['status'] == 'running'
    eta_at, started = row['eta_at'], row['started_at']
    return {
        'id': row['id'], 'request_id': row['request_id'], 'seq': row['seq'], 'title': row['title'],
        'status': row['status'], 'percent': row['percent'], 'message': row['message'], 'eta_at': eta_at,
        'eta_remaining': round(max(0.0, eta_at - now), 3) if running and eta_at is not None else None,
        'created_at': row['created_at'], 'started_at': started,
        'start_inferred': bool(row['start_inferred']), 'updated_at': row['updated_at'],
        'completed_at': row['completed_at'],
        'elapsed': None if started is None else round((row['completed_at'] or now) - started, 3),
        'stale': running and is_stale(row['updated_at'], eta_at, now, stale_after),
        'url': f"{public}/r/{row['request_id']}#{row['id']}",
    }


def request_json(row, now, public, stale_after):
    percent = row['avg_percent']
    if percent is None:
        percent = 100.0 if row['status'] == 'done' else 0.0
    current = None
    if row['current_id'] is not None:
        current = {'id': row['current_id'], 'title': row['current_title'], 'message': row['current_message']}
    return {
        'id': row['id'], 'title': row['title'], 'origin': row['origin'], 'status': row['status'],
        'message': row['message'], 'created_at': row['created_at'], 'updated_at': row['updated_at'],
        'completed_at': row['completed_at'],
        'elapsed': round((row['completed_at'] or now) - row['created_at'], 3),
        'percent': round(percent, 1),
        **{key: row[key] for key in ('tasks_total', 'tasks_done', 'tasks_failed', 'tasks_running',
                                     'tasks_pending', 'tasks_cancelled')},
        'current': current, 'eta_at': row['eta_at'],
        'stale': row['status'] == 'running' and is_stale(row['updated_at'], row['eta_at'], now, stale_after),
        'url': f"{public}/r/{row['id']}",
    }


# HTTP

def _route(method, pattern, handler, **fixed):
    return method, re.compile(pattern, re.IGNORECASE), handler, fixed


ROUTES = (
    _route('GET', r'/', 'index'),
    _route('GET', r'/r(?:/.*)?', 'file', rel='static/index.html'),
    _route('GET', r'/favicon\.ico', 'favicon'),
    _route('GET', r'/api(?:/usage)?', 'file', rel='templates/usage.md', render=True),
    _route('GET', r'/api/rule', 'file', rel='templates/rule.md', render=True),
    _route('GET', r'/api/skill/SKILL\.md', 'file', rel='skill/SKILL.md'),
    _route('GET', r'/api/skill/taskctl', 'file', rel='taskctl'),
    _route('GET', r'/api/skill/taskctl\.py', 'file', rel='taskctl.py', render=True),
    _route('GET', r'/api/health', 'health'),
    _route('GET', r'/api/requests', 'list_requests'),
    _route('POST', r'/api/requests', 'create_request'),
    _route('GET', r'/api/requests/(?P<rid>[^/]+)', 'get_request'),
    _route('DELETE', r'/api/requests/(?P<rid>[^/]+)', 'delete_request'),
    _route('POST', r'/api/requests/(?P<rid>[^/]+)/complete', 'complete_request'),
    _route('POST', r'/api/requests/(?P<rid>[^/]+)/tasks', 'add_tasks'),
    _route('GET', r'/api/tasks/(?P<tid>[^/]+)', 'get_task'),
    _route('POST', r'/api/tasks/(?P<tid>[^/]+)/progress', 'progress'),
    _route('POST', r'/api/tasks/(?P<tid>[^/]+)/complete', 'complete_task'),
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


class Handler(BaseHTTPRequestHandler):
    server_version = f'tasks/{VERSION}'
    sys_version = ''
    timeout = 30

    def _dispatch(self):
        method = 'GET' if self.command == 'HEAD' else self.command
        self._t0 = time.monotonic()
        self._error_note = ''
        self.warnings = []
        path = self.path
        try:
            try:
                split = urlsplit(self.path)
            except ValueError:  # e.g. an absolute-form target such as http://[x/
                raise ApiError(400, 'malformed request target') from None
            self.query = parse_qs(split.query)
            path = unquote(split.path).rstrip('/') or '/'
            handler, params = self._match(method, path)
            if method in ('POST', 'DELETE'):
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
                if suffix in ('', '/complete') and method != 'DELETE':
                    hint = f'{tid} is a task id; use /api/tasks/{tid}{suffix}'
                else:
                    hint = f'{tid} is a task id; its request is {parent}'
                raise ApiError(400, f"'{tid}' is a task id, not a request id", hint=hint)
            raise ApiError(400, f"malformed request id '{value[:40]}'", hint='request ids look like k3m9qa')
        if tid_match:
            return f'{tid_match[1]}-{int(tid_match[2])}'
        if rid_match:
            hint = (f'{value} is a request id; use /api/requests/{value}{suffix}' if suffix in ('', '/complete')
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

    def base_url(self):
        host = (self.headers.get('Host') or '').strip()
        if HOST_RE.fullmatch(host):
            return 'http://' + host
        if self.server.public_url:
            return self.server.public_url
        ip, port = self.connection.getsockname()[:2]
        ip = plain_ip(ip).partition('%')[0]  # a link-local address's %zone is only meaningful here
        return f'http://[{ip}]:{port}' if ':' in ip else f'http://{ip}:{port}'

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
            base = self.base_url()
            data = (data.decode('utf-8').replace('{{BASE_URL}}', base)
                    .replace('{{PUBLIC_URL}}', self.server.public_url or base)
                    .replace('{{VERSION}}', VERSION).encode('utf-8'))
        html = rel.endswith('.html')
        self._send(200, data, 'text/html; charset=utf-8' if html else 'text/plain; charset=utf-8',
                   'no-cache' if html else 'no-store', {'Vary': 'Accept'} if vary else None)

    def h_favicon(self):
        self._send(204, b'', None, None)

    def h_health(self):
        self._reply(200, {'ok': True, 'service': 'tasks', 'version': VERSION, 'pid': os.getpid(), 'now': ts(),
                          'started_at': self.server.started_at,
                          'requests_running': self.server.store.running_count()})

    def _query(self, name):
        values = self.query.get(name)
        return values[-1].strip().lower() if values else None

    def h_list_requests(self):
        status = self._query('status')
        if status is not None and status not in ('running', 'done', 'failed'):
            raise ApiError(400, 'status must be running, done or failed', field='status')
        limit = 100
        if self._query('limit') is not None:
            try:
                limit = int(self._query('limit'))
            except ValueError:
                raise ApiError(400, 'limit must be an integer', field='limit') from None
            if not 1 <= limit <= 500:
                self.warnings.append(f'limit {limit} clamped to 1..500')
                limit = min(500, max(1, limit))
        now, stale_after, public = ts(), self.server.stale_after, self.public_url()
        rows, counts = self.server.store.list_requests(status, limit, now, stale_after)
        self._reply(200, {'now': now, 'stale_after': stale_after, 'counts': counts,
                          'requests': [request_json(row, now, public, stale_after) for row in rows]})

    def h_create_request(self):
        body, w = self._body({'title', 'origin', 'tasks'}), self.warnings
        title = clean_text(body.get('title'), 'title', TITLE_MAX, w, required=True)
        origin = clean_text(body.get('origin'), 'origin', ORIGIN_MAX, w)
        titles = [] if body.get('tasks') is None else clean_titles(body['tasks'], 'tasks', w)
        now = ts()
        row, tasks = self.server.store.create_request(title, origin, titles, now)
        log.info("created request %s '%s' with %d task(s) from %s", row['id'], title, len(tasks), origin or '?')
        self._reply(201, self._detail(row, tasks, now))

    def h_get_request(self, rid):
        now = ts()
        payload = self._detail(*self.server.store.request_detail(rid), now)
        self._reply(200, {**payload, 'stale_after': self.server.stale_after})

    def h_delete_request(self, rid):
        self.server.store.delete_request(rid)
        log.info('deleted request %s', rid)
        self._reply(200, {'deleted': rid, 'now': ts()})

    def h_complete_request(self, rid):
        body, w = self._body({'status', 'message'}), self.warnings
        status = parse_status(body.get('status'))
        message = clean_text(body.get('message'), 'message', MESSAGE_MAX, w, multiline=True)
        now = ts()
        row, tasks, auto_closed, cancelled, previous = self.server.store.complete_request(rid, status, message, now)
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

    def h_get_task(self, tid):
        now = ts()
        self._reply(200, {**self._task(self.server.store.task(tid), now), 'now': now})

    def h_progress(self, tid):
        body, w = self._body({'message', 'percent', 'eta_seconds'}), self.warnings
        message = clean_text(body.get('message'), 'message', MESSAGE_MAX, w, required=True, multiline=True)
        percent = None if body.get('percent') is None else parse_percent(body['percent'], w)
        eta = None if body.get('eta_seconds') is None else parse_eta(body['eta_seconds'], w)
        now = ts()
        row = self.server.store.progress(tid, message, percent, None if eta is None else round(now + eta, 3), now)
        log.debug('progress %s %s%% %s', tid, row['percent'], _printable(message))
        self._reply(200, {**self._task(row, now), 'now': now})

    def h_complete_task(self, tid):
        body, w = self._body({'status', 'message'}), self.warnings
        status = parse_status(body.get('status'))
        message = clean_text(body.get('message'), 'message', MESSAGE_MAX, w, multiline=True)
        now = ts()
        row, previous = self.server.store.complete_task(tid, status, message, now)
        if previous != status:
            log.info('task %s %s (was %s)', tid, status, previous)
        self._reply(200, {**self._task(row, now), 'now': now})


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


def listen(host, port):
    # A wildcard host listens dual-stack; without IPv6 it falls back to IPv4 only. Any other host is
    # bound as given (an IPv6 one is IPv6 only).
    host = host.strip('[]')
    if host in WILDCARD_HOSTS:
        try:
            return TasksServer('::', port, Handler, dual_stack=True)
        except OSError as exc:
            if exc.errno in (errno.EADDRINUSE, errno.EACCES):
                raise
            log.info('IPv6 is unavailable (%s); listening on IPv4 only', exc.strerror or exc)
        host = '0.0.0.0'
    return TasksServer(host, port, Handler)


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

    try:
        httpd = listen(args.host, args.port)
    except OSError as exc:
        if exc.errno == errno.EADDRINUSE:
            print(f'error: port {args.port} already in use (is the tasks server already running? '
                  f'try: curl -s http://127.0.0.1:{args.port}/api/health)', file=sys.stderr)
        else:
            print(f'error: cannot listen on {args.host}:{args.port}: {exc.strerror or exc}', file=sys.stderr)
        return 2
    try:
        store = Store(args.db)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        httpd.server_close()
        print(f'error: cannot open database {args.db}: {exc}', file=sys.stderr)
        return 2
    pidfile = args.pidfile or args.db.parent / f'server-{args.port}.pid'
    try:
        write_pidfile(pidfile)
    except OSError as exc:
        httpd.server_close()
        store.close()
        print(f'error: cannot write pidfile {pidfile}: {exc}', file=sys.stderr)
        return 2

    httpd.store = store
    httpd.public_url = args.public_url
    httpd.stale_after = args.stale_after
    httpd.started_at = ts()
    httpd.local_names = frozenset(name.lower() for name in (
        'localhost', socket.gethostname(), urlsplit(args.public_url or '').hostname) if name)

    stop = threading.Event()
    threading.Thread(target=maintenance_loop, name='maintenance', daemon=True,
                     args=(store, args.expire_after, args.retention_days, stop)).start()
    signal.signal(signal.SIGTERM, _interrupt)
    host, port = httpd.server_address[:2]
    if httpd.dual_stack:
        shown = f'http://0.0.0.0:{port} and http://[::]:{port}'
    else:
        shown = f'http://[{host}]:{port}' if ':' in host else f'http://{host}:{port}'
    log.info('tasks server listening on %s (db %s, pid %d, public url %s)', shown,
             args.db, os.getpid(), args.public_url or 'from Host header')
    try:
        httpd.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        log.info('shutting down')
    finally:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        stop.set()
        httpd.server_close()
        store.close()
        remove_pidfile(pidfile)
    return 0


if __name__ == '__main__':
    sys.exit(main())
