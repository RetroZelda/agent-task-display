#!/usr/bin/env python3
"""taskctl: report progress on long-running work to the task-status board.

Reporting is a side channel and must never break the real work. stdout carries only machine values
(IDs, URLs; tables for show/list), everything else goes to stderr as "taskctl: ...". Board errors
(an unusable board URL or TASKS_TIMEOUT included) are warnings that exit 0 (--strict / TASKS_STRICT=1:
1 = rejected, 3 = unreachable); usage errors always exit 2. When new/add cannot get IDs they print
`offline` per expected ID, and any command given the ID `offline` does nothing, so captured IDs keep
working in scripts.

Board URL: --url, $TASKS_URL, the address baked in when the board served this file, then
http://127.0.0.1:8765. Python 3.8+ syntax and the standard library only: this runs on remote machines.
"""
from __future__ import annotations

import argparse
import http.client
import json
import math
import os
import re
import shlex
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Optional

# The board replaces this placeholder with its own address when it serves the file.
BAKED_URL = "{{BASE_URL}}"
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
    """A failed board call; `unreachable` separates network trouble from a rejection."""

    def __init__(self, message: str, unreachable: bool):
        super().__init__(message)
        self.unreachable = unreachable


def note(message: str) -> None:
    print('taskctl: ' + message, file=sys.stderr, flush=True)


def warn(message: str) -> None:
    note('warning: ' + message)


def emit(values) -> None:
    for value in values:
        print(value, flush=True)


class Board:
    def __init__(self, base: str, timeout: float, strict: bool):
        self.base = base
        self.timeout = timeout
        self.strict = strict
        # A LAN service: a proxy configured in the environment would only get in the way.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def call(self, method: str, path: str, body: Optional[dict] = None, text: bool = False):
        data = None if body is None else json.dumps(body).encode('utf-8')
        try:
            # Inside the try: an unusable URL (ValueError) is board config, reported like a dead board.
            request = urllib.request.Request(self.base + path, data=data, method=method)
            request.add_header('Accept', 'text/plain' if text else 'application/json')
            if data is not None:
                request.add_header('Content-Type', 'application/json')
            with self._opener.open(request, timeout=self.timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as e:
            raise BoardError('board rejected %s %s: %s' % (method, path, describe_http_error(e)), False)
        except (urllib.error.URLError, OSError, http.client.HTTPException, ValueError) as e:
            reason = getattr(e, 'reason', None) or e
            raise BoardError('board unreachable at %s (%s)' % (self.base, reason), True)
        if text:
            return raw.decode('utf-8', 'replace')
        try:
            result = json.loads(raw.decode('utf-8'))
        except ValueError:
            result = None
        if not isinstance(result, dict):
            raise BoardError('unexpected reply to %s %s (is %s a task-status board?)'
                             % (method, path, self.base), False)
        for message in result.get('warnings') or []:
            warn('board: %s' % message)
        return result


def describe_http_error(e: urllib.error.HTTPError) -> str:
    try:
        payload = json.loads(e.read().decode('utf-8'))
        message = payload.get('error') or e.reason
        if payload.get('hint'):
            message = '%s (hint: %s)' % (message, payload['hint'])
    except (ValueError, OSError, AttributeError, http.client.HTTPException):
        message = e.reason
    return '%d %s' % (e.code, message)


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
    status = 'stale' if t.get('stale') else t.get('status')
    return '%-9s %6s %8s  %s  %s' % (status, fmt_pct(t.get('percent')), elapsed, t.get('id'), text)


def request_line(r: dict) -> str:
    text = r.get('title') or ''
    if r.get('message'):
        text += ': ' + one_line(r['message'])
    if r.get('origin'):
        text += '  (%s)' % r['origin']
    status = 'stale' if r.get('stale') else r.get('status')
    return '%s  %-7s %6s  %s/%s done  %8s  %s' % (r.get('id'), status, fmt_pct(r.get('percent')),
                                                  r.get('tasks_done'), r.get('tasks_total'),
                                                  fmt_dur(r.get('elapsed')), text)


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


def cmd_new(board: Board, args) -> int:
    title = ' '.join(args.title)
    try:
        cwd = os.path.basename(os.getcwd())
    except OSError:
        cwd = '?'
    body = {'title': title, 'origin': '%s:%s' % (socket.gethostname(), cwd), 'tasks': args.tasks}
    r = post_for_ids(board, '/api/requests', body, 1 + len(args.tasks))
    tasks = r.get('tasks') or []
    emit_ids([r.get('id')] + [t.get('id') for t in tasks], 1 + len(args.tasks))
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
    board.call('POST', '/api/tasks/%s/progress' % tid, {'message': ' '.join(args.hint).strip() or 'started'})
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
    t = board.call('POST', '/api/tasks/%s/progress' % tid, body)
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
        t = board.call('POST', '/api/tasks/%s/complete' % v, body)
        note('%s %s' % (v, t.get('status')))
        if t.get('start_inferred'):
            note('task was never started; its running time is approximate'
                 ' - run `taskctl start TID` when you begin a task')
        return 0
    r = board.call('POST', '/api/requests/%s/complete' % v, body)
    note('request %s %s (%s/%s tasks done)' % (v, r.get('status'), r.get('tasks_done'), r.get('tasks_total')))
    if r.get('auto_closed'):
        note('still-running tasks closed as %s: %s' % (args.status, ', '.join(r['auto_closed'])))
    if r.get('cancelled'):
        note('never-started tasks cancelled: %s' % ', '.join(r['cancelled']))
    return 0


def cmd_show(board: Board, args) -> int:
    v, kind = classify(args.id)
    if skip_offline(v):
        return 0
    if kind == 'task':
        t = board.call('GET', '/api/tasks/%s' % v)
        emit([task_line(t, t.get('now'))])
        return 0
    r = board.call('GET', '/api/requests/%s' % v)
    emit([request_line(r), 'page: %s' % r.get('url')])
    emit(['  ' + task_line(t, r.get('now')) for t in r.get('tasks') or []])
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
    health = board.call('GET', '/api/health')
    if not health.get('ok') or health.get('service') != 'tasks':
        raise BoardError('%s answered, but not as a task-status board' % board.base, False)
    note('ok: board at %s (pid %s, %s running)'
         % (board.base, health.get('pid'), health.get('requests_running')))
    return 0


def cmd_install_rule(board: Board, args) -> int:
    config_dir = os.environ.get('CLAUDE_CONFIG_DIR') or '~/.claude'
    path = os.path.expanduser(args.file or os.path.join(config_dir, 'CLAUDE.md'))
    # A setup command, not a status report: every failure is a real error (exit 1), never soft.
    try:
        rule = board.call('GET', '/api/rule', text=True).strip()
        if not (rule.startswith(RULE_BEGIN) and rule.endswith(RULE_END)):
            raise BoardError('%s/api/rule did not return a task-status block' % board.base, False)
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


def write_atomic(path: str, text: str) -> None:
    directory = os.path.dirname(path) or '.'
    os.makedirs(directory, exist_ok=True)
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
            self.board.call('POST', '/api/tasks/%s/%s' % (self.tid, action), body)
        except Exception as e:  # BoardError, or anything unforeseen: the command must be unaffected
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
    command('show', cmd_show, 'show a request with its tasks, or one task').add_argument('id')
    p = command('list', cmd_list, 'list every running request (--all: plus the 20 most recently finished)')
    p.add_argument('-a', '--all', action='store_true', help='also list the 20 most recently finished requests')
    command('ping', cmd_ping, 'check the board; prints its URL')
    p = command('run', cmd_run, 'run CMD and report its latest output line: run TID -- CMD...')
    p.usage = '%(prog)s [-h] [--every SEC] [--percent-regex RE] TID -- CMD...'
    p.add_argument('tid')
    p.add_argument('--every', type=float, default=10.0, metavar='SEC', help='min seconds between updates')
    p.add_argument('--percent-regex', metavar='RE', help='1 group = percent, 2 groups = done/total')
    p = command('install-rule', cmd_install_rule, 'add the task-status rule block to CLAUDE.md')
    p.add_argument('--file', metavar='PATH', help='default: ${CLAUDE_CONFIG_DIR:-~/.claude}/CLAUDE.md')
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
        return args.func(board, args)
    except UsageError as e:
        note('error: %s' % e)
        return 2
    except BoardError as e:
        if board is not None and board.strict:
            note('error: %s' % e)
            return 3 if e.unreachable else 1
        warn('%s; continuing without status reporting' % e if e.unreachable else str(e))
        return 0
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
