#!/usr/bin/env python3
"""taskctl with no board listening: usage errors (exit 2), soft failures (exit 0 + a warning), --strict
(3 = unreachable, 1 for the setup commands install-rule and update), the offline sentinel, new's
client-made ids (queued for replay), run without a board, TASKS_TIMEOUT/--timeout, unusable URLs, -h,
and the sh wrapper (CDPATH, symlinks, no Python). The offline queue itself is the spool suite's."""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the checkout, even when run without run_all.sh
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from harness import TASKS, WRAPPER, Suite, clean_env, free_port, scratch_home, tempdir  # noqa: E402

S = Suite('cli_offline')
check = S.check
TMP = tempdir('cli-offline')
scratch_home(TMP)
DEAD = f'http://127.0.0.1:{free_port()}'  # nothing listens here
ENV = clean_env()
UNREACHABLE = f'taskctl: warning: board unreachable at {DEAD} ('
OFFLINE_NOTE = "taskctl: id is 'offline' (the board was unreachable when it was created); nothing to report"
QUEUED = '; queued for replay ('
RID_RE = re.compile('[23456789abcdefghjkmnpqrstuvwxyz]{6}')


def real_ids(out, tasks):
    """new's stdout while the board is down: its client-made request id, then rid-1..rid-N."""
    ids = out.split('\n')
    return (len(ids) == tasks + 2 and ids[-1] == '' and bool(RID_RE.fullmatch(ids[0]))
            and ids[1:-1] == [f'{ids[0]}-{i}' for i in range(1, tasks + 1)])


def tc(*argv, env=None, cwd=TMP, exe=WRAPPER, timeout=60):
    p = subprocess.run([str(exe), *argv], capture_output=True, text=True, cwd=cwd, env=env or ENV, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def no_trace(err):
    return 'Traceback' not in err and 'Exception in thread' not in err


def expect(label, want_rc, argv, out=None, err_has=(), env=None, exe=WRAPPER):
    rc, o, e = tc(*argv, env=env, exe=exe)
    ok = rc == want_rc and no_trace(e)
    if out is not None:
        ok = ok and o == out
    for needle in ([err_has] if isinstance(err_has, str) else err_has):
        ok = ok and needle in e
    check(label, ok, (rc, o, e[-600:]))
    return rc, o, e


S.section('usage errors: exit 2, nothing on stdout, even with the offline id')
for label, argv, needle in (
        ('no arguments', [], 'the following arguments are required: COMMAND'),
        ('unknown subcommand', ['--url', DEAD, 'bogus'], "invalid choice: 'bogus'"),
        ('progress bad percent', ['--url', DEAD, 'progress', 'k3m9qa-1', 'abc', 'hint'],
         "percent must be a number such as 45, 45.5 or 45%, not 'abc'"),
        ('progress bad eta', ['--url', DEAD, 'progress', 'k3m9qa-1', '50', 'hint', '--eta', '5x'], "cannot read duration '5x'"),
        ('progress missing hint', ['--url', DEAD, 'progress', 'k3m9qa-1', '50'], 'required: hint'),
        ('start given a request id', ['--url', DEAD, 'start', 'k3m9qa'], 'k3m9qa is a request id; this command needs a task id'),
        ('add given a task id', ['--url', DEAD, 'add', 'k3m9qa-1', 'title'], 'k3m9qa-1 is a task id; this command needs its request id'),
        ('add --start with two titles', ['--url', DEAD, 'add', '--start', 'k3m9qa', 'a', 'b'], 'add --start takes exactly one TITLE'),
        ('fail without a message', ['--url', DEAD, 'fail', 'k3m9qa-1'], 'required: message'),
        ('done with a malformed id', ['--url', DEAD, 'done', 'ZZZ'], "'ZZZ' is not a request id"),
        ('run without --', ['--url', DEAD, 'run', 'k3m9qa-1', 'echo', 'hi'], 'run needs -- and then the command'),
        ('run with nothing after --', ['--url', DEAD, 'run', 'k3m9qa-1', '--'], 'run needs -- and then the command'),
        ('run with no id', ['--url', DEAD, 'run'], 'run needs --'),
        ('run with a bad regex', ['--url', DEAD, 'run', 'k3m9qa-1', '--percent-regex', '(', '--', 'true'], 'bad --percent-regex'),
        ('run regex with 0 groups', ['--url', DEAD, 'run', 'k3m9qa-1', '--percent-regex', 'x', '--', 'true'], 'needs 1 group'),
        ('run --every 0', ['--url', DEAD, 'run', 'k3m9qa-1', '--every', '0', '--', 'true'], '--every must be more than 0'),
        ('ask without a question', ['--url', DEAD, 'ask', 'k3m9qa'], 'required: question'),
        ('ask with a blank question', ['--url', DEAD, 'ask', 'k3m9qa-2', '  '], 'ask needs the QUESTION'),
        ('ask with a malformed id', ['--url', DEAD, 'ask', 'k3m9q', 'why?'], "'k3m9q' is not a request id"),
        ('resume without an id', ['--url', DEAD, 'resume'], 'required: id'),
        ('resume with a malformed id', ['--url', DEAD, 'resume', 'l0l0l0'], "'l0l0l0' is not a request id"),
        ('api without a path', ['--url', DEAD, 'api', 'GET'], 'required: PATH'),
        ('api with a path outside /api/', ['--url', DEAD, 'api', 'GET', '/health'], 'PATH must start with /api/'),
        ('api with whitespace in the path', ['--url', DEAD, 'api', 'GET', '/api/a b'], 'PATH must start with /api/'),
        ('api with a bad method', ['--url', DEAD, 'api', 'G-T', '/api/health'], 'METHOD must be an HTTP method'),
        ('api with bad JSON', ['--url', DEAD, 'api', 'POST', '/api/requests', '{bad'], 'JSON is not valid JSON'),
        ('update with an extra argument', ['--url', DEAD, 'update', 'now'], 'unrecognized arguments: now'),
        ('--timeout abc', ['--timeout', 'abc', '--url', DEAD, 'ping'], '--timeout must be a positive number'),
        ('--timeout 0 (no offline printed)', ['--timeout', '0', '--url', DEAD, 'new', 'x'], '--timeout must be a positive number'),
        ('--url without a value', ['ping', '--url'], '--url needs a value'),
        ('a usage error beats the offline id', ['progress', 'offline', 'abc', 'hint'], 'percent must be a number')):
    expect(f'{label}: exit 2', 2, argv, out='', err_has=needle)

S.section('unreachable board, soft: exit 0 + one warning; new queues its create and prints real ids')
t0 = time.monotonic()
rc, out, err = expect('new with 2 tasks -> its own rid + rid-1, rid-2', 0,
                      ['--url', DEAD, 'new', 'Build it', '-t', 'step one', '-t', 'step two'], err_has=(UNREACHABLE, QUEUED))
check('an unreachable board answers fast (connection refused)', time.monotonic() - t0 < 3, time.monotonic() - t0)
check('new offline: rid, then rid-1 and rid-2 (deterministic)', real_ids(out, 2), out)
check('new offline: one warning, and a created note that says queued', err.count('warning:') == 1
      and f"taskctl: created {out.split()[0]} 'Build it' (queued until the board is back)  page: {DEAD}/r/{out.split()[0]}" in err, err)
rc, out2, err = expect('new, global options after the subcommand', 0, ['new', 'Build it', '-t', 'a', '--url', DEAD])
check('new, global options after the subcommand: 2 real ids, a fresh rid', real_ids(out2, 1) and out2.split()[0] != out.split()[0], out2)
expect('add 2 titles -> 2x offline', 0, ['--url', DEAD, 'add', 'k3m9qa', 'a', 'b'], out='offline\n' * 2)
expect('alias task --start -> 1x offline', 0, ['--url', DEAD, 'task', '--start', 'k3m9qa', 'a'], out='offline\n')
for label, argv in (('start', ['start', 'k3m9qa-1']), ('progress', ['progress', 'k3m9qa-1', '45%', 'compiling', '--eta', '5m']),
                    ('alias finish (done a task)', ['finish', 'k3m9qa-1', 'ok']), ('alias failed (fail a request)', ['failed', 'k3m9qa', 'broke']),
                    ('alias status (show)', ['status', 'k3m9qa']), ('alias ls --all', ['ls', '--all'])):
    expect(f'{label}: exit 0, warning, no stdout', 0, ['--url', DEAD, *argv], out='', err_has=UNREACHABLE)
expect('ping prints the URL (trailing slash stripped)', 0, ['--url', DEAD + '/', 'ping'], out=DEAD + '\n', err_has=UNREACHABLE)
expect('TASKS_URL used, scheme added', 0, ['ping'], out=DEAD + '\n', env=dict(ENV, TASKS_URL=DEAD.removeprefix('http://')))

S.section('--strict: 3 = unreachable; install-rule is a setup command: 1')
rc, out, err = expect('--strict new: 3 (unreachable), though queued', 3, ['--url', DEAD, '--strict', 'new', 'T', '-t', 'a'],
                      err_has=(f'taskctl: error: board unreachable at {DEAD}', QUEUED))
check('--strict new: the queued ids are still printed, never offline', real_ids(out, 1), out)
expect('--strict after the subcommand', 3, ['progress', 'k3m9qa-1', '5', 'x', '--url', DEAD, '--strict'])
expect('TASKS_STRICT=1 ping: 3, still prints the URL', 3, ['--url', DEAD, 'ping'], out=DEAD + '\n', env=dict(ENV, TASKS_STRICT='1'))
never = TMP / 'never.md'
expect('install-rule unreachable: 1, no file', 1, ['--url', DEAD, 'install-rule', '--file', str(never)],
       err_has=f'cannot install the rule in {never}')
check('install-rule unreachable wrote nothing', not never.exists())
expect('update from the repo copy: 1, not an installed skill', 1, ['--url', DEAD, 'update'],
       out='', err_has='not an installed skill')
for sub in ('usage', 'changelog'):
    expect(f'{sub} unreachable (soft): 0, a warning, no stdout', 0, ['--url', DEAD, sub], out='', err_has=UNREACHABLE)
    expect(f'{sub} unreachable (--strict): 3', 3, ['--url', DEAD, '--strict', sub], out='')
expect('api unreachable (soft): 0, a warning', 0, ['--url', DEAD, 'api', 'GET', '/api/health'], out='', err_has=UNREACHABLE)
expect('api unreachable (--strict): 3', 3, ['--url', DEAD, '--strict', 'api', 'GET', '/api/health'], out='')

S.section('the offline sentinel is a no-op even with --strict')
for label, argv in (('start', ['start', 'offline']), ('progress', ['progress', 'offline', '50', 'hint', '--eta', '1:30']),
                    ('done (any case)', ['done', 'OFFLINE']), ('fail', ['fail', 'offline', 'x']), ('show', ['show', 'offline']),
                    ('ask', ['ask', 'offline', 'which one?']), ('resume', ['resume', 'Offline'])):
    expect(f'{label} offline: exit 0, note', 0, ['--url', DEAD, '--strict', *argv], out='', err_has=OFFLINE_NOTE)
expect('add offline a b -> 2x offline', 0, ['--url', DEAD, '--strict', 'add', 'offline', 'a', 'b'], out='offline\n' * 2)

S.section('run with the board unreachable: the command is unaffected')
noexec = TMP / 'not-executable'
noexec.write_text('#!/bin/sh\necho no\n')
noexec.chmod(0o644)
expect('run exit 7, output passed through', 7, ['--url', DEAD, 'run', 'k3m9qa-1', '--', 'sh', '-c', 'echo 5/10; exit 7'],
       out='5/10\n', err_has='the command is unaffected')
expect('run offline id', 7, ['--url', DEAD, 'run', 'offline', '--', 'sh', '-c', 'echo 5/10; exit 7'], out='5/10\n',
       err_has="taskctl: id is 'offline'; running the command without status reporting")
expect('run a missing command: 127', 127, ['--url', DEAD, 'run', 'k3m9qa-1', '--', str(TMP / 'nonexistent'), 'arg'],
       err_has='cannot run')
expect('run a non-executable file: 126', 126, ['--url', DEAD, 'run', 'k3m9qa-1', '--', str(noexec)], err_has='Permission denied')
expect('run a child killed by SIGTERM: 143', 143, ['--url', DEAD, 'run', 'k3m9qa-1', '--', 'sh', '-c', 'kill -TERM $$'])
expect("run keeps the child's own --url/--strict args", 0, ['--url', DEAD, 'run', 'k3m9qa-1', '--', 'echo', '--url', '--strict', 'x'],
       out='--url --strict x\n')

S.section('TASKS_TIMEOUT: a bad value warns once and uses 3s; --timeout is strict')
BAD_TIMEOUT = "warning: TASKS_TIMEOUT='abc' is not a positive number of seconds; using 3s"
rc, out, err = tc('--url', DEAD, 'ping', env=dict(ENV, TASKS_TIMEOUT='abc'))
check('TASKS_TIMEOUT=abc ping: soft, one warning', rc == 0 and out == DEAD + '\n' and err.count(BAD_TIMEOUT) == 1, (rc, out, err))
rc, out, err = tc('--url', DEAD, 'new', 'x', '-t', 'a', env=dict(ENV, TASKS_TIMEOUT='abc'))
check('TASKS_TIMEOUT=abc new: 2 queued ids, one timeout warning', rc == 0 and real_ids(out, 1) and err.count(BAD_TIMEOUT) == 1, (rc, out, err))
rc, out, err = tc('--url', DEAD, 'new', 'x', '-t', 'a', env=dict(ENV, TASKS_TIMEOUT='5s'))
check('TASKS_TIMEOUT=5s new: 2 queued ids, no timeout warning', rc == 0 and real_ids(out, 1) and 'TASKS_TIMEOUT' not in err, (rc, out, err))
rc, out, err = tc('--url', DEAD, 'run', 'offline', '--', 'sh', '-c', 'echo CHILD-RAN; exit 7', env=dict(ENV, TASKS_TIMEOUT='abc'))
check('TASKS_TIMEOUT=abc run offline: the child still runs', rc == 7 and out == 'CHILD-RAN\n' and BAD_TIMEOUT in err, (rc, out, err))
rc, out, err = tc('--url', DEAD, 'run', 'k3m9qa-1', '--', 'sh', '-c', 'echo CHILD-RAN; exit 7', env=dict(ENV, TASKS_TIMEOUT='bogus'))
check('TASKS_TIMEOUT=bogus run TID: the child still runs, warns', rc == 7 and out == 'CHILD-RAN\n' and 'TASKS_TIMEOUT' in err, (rc, out, err))
rc, out, err = tc('--url', DEAD, 'run', 'offline', '--', 'sh', '-c', 'echo CHILD-RAN; exit 7', env=dict(ENV, TASKS_TIMEOUT='5s'))
check('TASKS_TIMEOUT=5s run: the child runs', rc == 7 and out == 'CHILD-RAN\n', (rc, out, err))
rc, out, err = tc('--url', DEAD, '--strict', 'ping', env=dict(ENV, TASKS_TIMEOUT='abc'))
check('TASKS_TIMEOUT=abc --strict ping: 3 (unreachable), not 2', rc == 3, (rc, err))
rc, out, err = tc('--timeout', '5s', '--url', DEAD, 'ping')
check('--timeout 5s accepted (soft unreachable, 0)', rc == 0 and 'unreachable' in err, (rc, err))

S.section('unusable board URLs are "unreachable", never a traceback, and nothing is queued for them')
rc, out, err = expect("--url '://...' new: 2x offline, one warning", 0, ['--url', '://127.0.0.1:1', 'new', 'x', '-t', 'a'],
                      out='offline\noffline\n', err_has='warning: board unreachable')
check("--url '://...' new: not queued (no connection was ever tried)", 'queued' not in err and err.count('warning:') == 1, err)
expect("TASKS_URL='://x' ping: soft", 0, ['ping'], env=dict(ENV, TASKS_URL='://x'), err_has='warning')
expect("--url 'http://[bad' new: offline", 0, ['--url', 'http://[bad', 'new', 'x'], out='offline\n')
rc, out, err = tc('--url', '://127.0.0.1:1', 'run', 'k3m9qa-1', '--', 'sh', '-c', 'echo child; exit 7')
check("--url '://...' run TID: the child runs, one warning", rc == 7 and out == 'child\n' and no_trace(err)
      and err.count('warning:') == 1, (rc, out, err))
expect("--strict --url '://x' ping: 3", 3, ['--strict', '--url', '://x', 'ping'])
expect("install-rule with '://x': 1 (setup command)", 1, ['--url', '://x', 'install-rule', '--file', str(never)])
check("install-rule with '://x' wrote nothing", not never.exists())

S.section('help')
for argv in (['run', '-h'], ['run', '--help'], ['run', 'k3m9qa-1', '-h'], ['--url', DEAD, 'run', '--help', '--', 'x'],
             ['run', '-h', 'k3m9qa-1', 'echo', 'hi']):
    rc, out, err = tc(*argv)
    check(f"taskctl {' '.join(argv)}: 0, run's help on stdout", rc == 0 and out.startswith('usage: taskctl run')
          and 'TID -- CMD...' in out and '--percent-regex' in out and err == '', (rc, out, err))
expect('-h after -- belongs to the command', 0, ['--url', DEAD, 'run', 'offline', '--', 'sh', '-c', 'echo "[$1]"', '_', '-h'],
       out='[-h]\n')
expect('--help after -- belongs to the command', 0, ['--url', DEAD, 'run', 'offline', '--', 'sh', '-c', 'echo "[$1]"', '_', '--help'],
       out='[--help]\n')
rc, out, err = tc('list', '-h')
check('list -h describes --all accurately', rc == 0 and 'every running request' in out and '20 most recently finished' in out, out)
rc, out, err = tc('-h')
check('top-level -h', rc == 0 and 'COMMAND' in out, (rc, err))
for sub in ('new', 'add', 'start', 'progress', 'done', 'fail', 'show', 'list', 'ping', 'run', 'install-rule',
            'ask', 'resume', 'flush', 'update', 'usage', 'changelog', 'api'):
    rc, out, err = tc(sub, '-h')
    check(f'taskctl {sub} -h', rc == 0 and out.startswith('usage: taskctl'), (rc, err))

S.section('the sh wrapper: CDPATH exported, relative invocation, symlinks')
work, decoy = TMP / 'cdpath_work', TMP / 'cdpath_decoy'
(work / 'tasks').mkdir(parents=True)
(decoy / 'tasks').mkdir(parents=True)
for name in ('taskctl', 'taskctl.py'):
    shutil.copy2(TASKS / name, work / 'tasks' / name)
(decoy / 'tasks' / 'taskctl.py').write_text('print("DECOY")\n')
for cdpath in ('.:/nonexistent', str(decoy), f'{decoy}:.'):
    env = dict(ENV, CDPATH=cdpath)
    for how in ('tasks/taskctl', 'sh tasks/taskctl', 'bash --posix tasks/taskctl'):
        p = subprocess.run(how.split() + ['--url', DEAD, 'ping'], capture_output=True, text=True, cwd=work, env=env)
        check(f'CDPATH={cdpath} {how} ping: 0, prints the URL', p.returncode == 0 and p.stdout == DEAD + '\n',
              (p.returncode, p.stdout, p.stderr))
    p = subprocess.run(['tasks/taskctl', '--url', DEAD, 'new', 'x', '-t', 'a'], capture_output=True, text=True, cwd=work, env=env)
    check(f'CDPATH={cdpath} tasks/taskctl new: 2 queued ids', p.returncode == 0 and real_ids(p.stdout, 1),
          (p.returncode, p.stdout, p.stderr))
# Control: the same wrapper without its CDPATH guard must fail here, or the cases above prove nothing.
fixed = 'here=$(CDPATH= cd -- "$(dirname -- "$self")" >/dev/null 2>&1 && pwd)'
wrapper_text = (TASKS / 'taskctl').read_text()
if fixed in wrapper_text:
    (work / 'old' / 'tasks').mkdir(parents=True)
    (work / 'old' / 'tasks' / 'taskctl').write_text(
        wrapper_text.replace(fixed, 'here=$(cd -- "$(dirname -- "$self")" 2>/dev/null && pwd)'))
    (work / 'old' / 'tasks' / 'taskctl').chmod(0o755)
    shutil.copy2(TASKS / 'taskctl.py', work / 'old' / 'tasks' / 'taskctl.py')
    p = subprocess.run(['tasks/taskctl', '--url', DEAD, 'ping'], capture_output=True, text=True, cwd=work / 'old',
                       env=dict(ENV, CDPATH='.:/nonexistent'))
    check('control: without the guard, an exported CDPATH breaks the wrapper', p.returncode != 0 or p.stdout != DEAD + '\n',
          (p.returncode, p.stdout))
else:
    S.skip('CDPATH control case', "the wrapper's cd line changed; update the control")
(work / 'link-taskctl').symlink_to(work / 'tasks' / 'taskctl')
p = subprocess.run(['./link-taskctl', '--url', DEAD, 'ping'], capture_output=True, text=True, cwd=work, env=dict(ENV, CDPATH='.:/nonexistent'))
check('a symlink to the wrapper, with CDPATH', p.returncode == 0 and p.stdout == DEAD + '\n', (p.returncode, p.stderr))
(work / 'bin').mkdir()
(work / 'bin' / 'rel-link').symlink_to('../tasks/taskctl')
p = subprocess.run(['bin/rel-link', '--url', DEAD, 'ping'], capture_output=True, text=True, cwd=work, env=ENV)
check('a relative symlink to the wrapper', p.returncode == 0 and p.stdout == DEAD + '\n', (p.returncode, p.stderr))

S.section('the sh wrapper without a usable Python: warns, offline ids, run still runs its command')
nopy = TMP / 'nopy'
nopy.mkdir()
for tool in ('dirname', 'readlink'):
    found = shutil.which(tool)
    if found:
        (nopy / tool).symlink_to(found)
oldpy = TMP / 'oldpy'
oldpy.mkdir()
for tool in ('dirname', 'readlink'):
    found = shutil.which(tool)
    if found:
        (oldpy / tool).symlink_to(found)
(oldpy / 'python3').write_text('#!/bin/sh\ncase "$*" in *version_info*) exit 1;; esac\necho "old python ran taskctl"; exit 99\n')
(oldpy / 'python3').chmod(0o755)
sh = shutil.which('sh')
NOPY_WARN = 'taskctl: warning: no Python 3.8+ found; status reporting disabled'
for label, path in (('no python on PATH', nopy), ('only a too-old python3', oldpy)):
    env = dict(ENV, PATH=str(path))
    for what, argv, want_out in (('new with 2 tasks', ['--url', DEAD, 'new', 'T', '-t', 'a', '--task=b'], 'offline\n' * 3),
                                 ('add 2 titles', ['add', 'k3m9qa', 'a', 'b', '--strict'], 'offline\n' * 2),
                                 ('progress', ['--timeout', '2', 'progress', 'k3m9qa-1', '5', 'x'], ''),
                                 ('ping', ['ping'], '')):
        p = subprocess.run([sh, str(WRAPPER), *argv], capture_output=True, text=True, cwd=TMP, env=env)
        check(f'{label}: {what} -> exit 0, {want_out.count("offline")}x offline, warning', p.returncode == 0
              and p.stdout == want_out and NOPY_WARN in p.stderr, (p.returncode, p.stdout, p.stderr))
    p = subprocess.run([sh, str(WRAPPER), 'run', 'k3m9qa-1', '--', sh, '-c', 'echo still ran; exit 6'], capture_output=True,
                       text=True, cwd=TMP, env=env)
    check(f'{label}: run still runs its command and exits with its code', p.returncode == 6 and p.stdout == 'still ran\n',
          (p.returncode, p.stdout, p.stderr))

sys.exit(S.finish())
