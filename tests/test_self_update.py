#!/usr/bin/env python3
"""taskctl's self-update of an installed skill (SKILL.md, taskctl and taskctl.py in a temp
CLAUDE_CONFIG_DIR/skills/task-status, downloaded from a scratch board): when a reply's X-Tasks-Docs
differs from the baked BAKED_DOCS it replaces the three files atomically (modes 644/755/755, no temp
files), refreshes the CLAUDE.md rule only where one is installed, and prints the exact notice with the
changelog entries newer than its API version; the command's stdout and exit code never change; any
failure is one warning. Only its own board (the baked URL, whatever the spelling of its scheme's case or
a trailing slash) updates it: a reply from another board (--url, TASKS_URL) changes nothing and says so
in a note, and `update` refuses another board unless --force moves the skill there. TASKS_NO_UPDATE,
`update` and `update --force`, and the refusals: a repo copy (not an installed skill), its board down, a
read-only skill dir, served files that fail validation (no front matter, a taskctl.py that does not
compile), a changelog that cannot be fetched."""
from __future__ import annotations

import os
import stat
import subprocess
import sys
from pathlib import Path

sys.dont_write_bytecode = True  # no __pycache__ in the checkout, even when run without run_all.sh
sys.path.insert(0, str(Path(__file__).resolve().parent / 'lib'))
from harness import (WRAPPER, ScratchServer, Suite, api, clean_env, docs_version, free_port, scratch_home,  # noqa: E402
                     tasks_copy, tempdir)

S = Suite('self_update')
check = S.check
TMP = tempdir('self-update')
HOME = scratch_home(TMP)
CC = HOME / '.claude'
SKILL = CC / 'skills' / 'task-status'
ENV = clean_env(CLAUDE_CONFIG_DIR=CC)
FILES = ('SKILL.md', 'taskctl', 'taskctl.py')


def tc(*argv, env=None, exe=None):
    p = subprocess.run([str(exe or SKILL / 'taskctl'), *map(str, argv)], capture_output=True, text=True, env=env or ENV,
                       timeout=60, cwd=TMP)
    return p.returncode, p.stdout, p.stderr


def board(name, tree):
    return ScratchServer(TMP, '--host', '127.0.0.1', script=tree / 'server.py', name=name).start()


def install_from(url):
    """The skill as the usage text's one-line install leaves it."""
    SKILL.mkdir(parents=True, exist_ok=True)
    for name in FILES:
        code, raw, _ = api(url, 'GET', '/api/skill/' + name, raw=True)
        assert code == 200, (name, code)
        (SKILL / name).write_bytes(raw)
    (SKILL / 'SKILL.md').chmod(0o644)
    (SKILL / 'taskctl').chmod(0o755)
    (SKILL / 'taskctl.py').chmod(0o755)


def baked_docs():
    for line in (SKILL / 'taskctl.py').read_text().splitlines():
        if line.startswith('BAKED_DOCS'):
            return line.split('"')[1]
    return None


def served(url, name):
    return api(url, 'GET', '/api/skill/' + name, raw=True)[1]


def notice(old, new, news):
    return (f"taskctl: the board's agent instructions changed (docs {old} -> {new}); updated the skill in {SKILL}\n"
            "taskctl: what's new:\n" + news +
            f"\ntaskctl: re-read {SKILL}/SKILL.md before continuing; the copy loaded in your context is out of date\n")


def main():
    ta = tasks_copy(TMP / 'tasks_a')
    tb = tasks_copy(TMP / 'tasks_b')
    changelog = (tb / 'templates' / 'changelog.md').read_text()
    (tb / 'templates' / 'changelog.md').write_text('## v4 — 2026-10-09\n- A newer thing.\n- And another.\n\n' + changelog)
    rule = (tb / 'templates' / 'rule.md').read_text()
    (tb / 'templates' / 'rule.md').write_text(rule.replace('<!-- task-status:end -->', '- A new rule line for testing.\n<!-- task-status:end -->'))
    with open(tb / 'skill' / 'SKILL.md', 'a') as f:
        f.write('\nAn extra line in the B copy.\n')
    DA, DB = docs_version(ta), docs_version(tb)
    # The skill's own board: one address, restarted with older or newer files as a test needs (as after
    # a git pull and a restart). OTHER is another board, with the newer files, on an address of its own.
    P = free_port()
    URL = f'http://127.0.0.1:{P}'
    live = []

    def serve(tree, name):
        while live:
            live.pop().stop()
        live.append(ScratchServer(TMP, '--host', '127.0.0.1', script=tree / 'server.py', name=name, port=P).start())

    other = board('other', tb)
    OTHER = other.url
    try:
        S.section('an installed skill, current')
        serve(ta, 'a')
        check('its board and the other one serve different agent docs', DA != DB and api(URL, 'GET', '/api/health')[1]['docs_version'] == DA
              and api(OTHER, 'GET', '/api/health')[1]['docs_version'] == DB, (DA, DB))
        install_from(URL)
        (CC / 'CLAUDE.md').write_text('# mine\n\nkeep me\n')
        rc, out, err = tc('install-rule')
        check('install-rule from the baked URL', rc == 0 and 'rule installed' in err, (rc, err))
        rc, out, err = tc('ping')
        check('current: ping has no update notice', rc == 0 and out == URL + '\n' and 'instructions changed' not in err, (rc, out, err))
        check('the installed BAKED_DOCS is the board\'s docs version', baked_docs() == DA, baked_docs())

        S.section('another board (--url or TASKS_URL, a scratch board say) never changes the skill or the rule')
        before = {name: (SKILL / name).read_bytes() for name in FILES}
        md = (CC / 'CLAUDE.md').read_text()
        foreign = (f'taskctl: the board at {OTHER} has other agent instructions (docs {DB}) than this skill (docs {DA}), which follows'
                   f' its own board, {URL}; the skill was not changed\n')
        rc, out, err = tc('--url', OTHER, 'list')
        check('--url OTHER list: exit 0, and one note saying so (not a warning)', rc == 0 and err.endswith(foreign)
              and 'warning' not in err and 'instructions changed' not in err, (rc, err))
        rc, out, err = tc('ping', env=dict(ENV, TASKS_URL=OTHER))
        check('TASKS_URL=OTHER ping: the same', rc == 0 and out == OTHER + '\n' and err.endswith(foreign), (rc, out, err))
        check('the three files are untouched (BAKED_URL too)', {name: (SKILL / name).read_bytes() for name in FILES} == before
              and f'BAKED_URL = "{URL}"' in (SKILL / 'taskctl.py').read_text())
        check('the CLAUDE.md rule is untouched: it still links to the skill\'s own board', (CC / 'CLAUDE.md').read_text() == md
              and f'{URL}/r/<rid>' in md and OTHER not in md, md)
        rc, out, err = tc('ping')
        check('a plain ping still goes to its own board', rc == 0 and out == URL + '\n' and 'instructions changed' not in err, (rc, out, err))
        rc, out, err = tc('--url', OTHER, 'update')
        check('update from another board: exit 1, refused before it is asked anything, nothing changed', rc == 1 and out == ''
              and err == f'taskctl: error: cannot update the skill in {SKILL}: this skill belongs to {URL}; pass --force to move it to'
                         f' {OTHER}\n' and {name: (SKILL / name).read_bytes() for name in FILES} == before, (rc, err))
        rc, out, err = tc('--url', OTHER, 'install-rule')
        check('install-rule from another board: exit 1, refused, CLAUDE.md untouched', rc == 1 and out == ''
              and err == f'taskctl: error: cannot install the rule in {CC / "CLAUDE.md"}: this skill belongs to {URL}; pass --force'
                         f' to take the rule from {OTHER}\n' and (CC / 'CLAUDE.md').read_text() == md, (rc, err))
        rc, out, err = tc('install-rule', env=dict(ENV, TASKS_URL=OTHER))
        check('TASKS_URL=OTHER install-rule: refused the same way', rc == 1 and 'this skill belongs to' in err
              and (CC / 'CLAUDE.md').read_text() == md, (rc, err))
        rc, out, err = tc('--url', OTHER, 'install-rule', '--force')
        check('install-rule --force from another board takes its rule', rc == 0 and 'rule updated' in err
              and f'{OTHER}/r/<rid>' in (CC / 'CLAUDE.md').read_text(), (rc, err))
        rc, out, err = tc('install-rule')
        check('install-rule from its own board puts the rule back', rc == 0 and (CC / 'CLAUDE.md').read_text() == md, (rc, err))

        S.section('its own board serves newer instructions: the next reply updates the skill')
        serve(tb, 'b')
        rc, out, err = tc('ping')
        check("stdout and exit code are the command's own", rc == 0 and out == URL + '\n', (rc, out))
        check('the exact notice ends stderr, with the changelog entries newer than its API version (v4)',
              err.startswith('taskctl: ok: board at') and err.endswith(notice(DA, DB, '## v4 — 2026-10-09\n- A newer thing.\n- And another.')),
              err)
        for name in FILES:
            check(f'{name} is the board\'s copy now', (SKILL / name).read_bytes() == served(URL, name))
        check('modes 644/755/755', [stat.S_IMODE((SKILL / n).stat().st_mode) for n in FILES] == [0o644, 0o755, 0o755],
              [oct((SKILL / n).stat().st_mode) for n in FILES])
        check('no temp files left in the skill dir', sorted(os.listdir(SKILL)) == sorted(FILES), os.listdir(SKILL))
        check('BAKED_DOCS is the new version, BAKED_URL is still its board', baked_docs() == DB
              and f'BAKED_URL = "{URL}"' in (SKILL / 'taskctl.py').read_text(), baked_docs())
        md = (CC / 'CLAUDE.md').read_text()
        check('the installed CLAUDE.md rule is refreshed, the rest of the file kept', 'A new rule line for testing.' in md
              and md.startswith('# mine\n\nkeep me\n\n') and md.count('<!-- task-status:begin -->') == 1, md)
        rc, out, err = tc('ping')
        check('once updated: no notice', rc == 0 and 'instructions changed' not in err, err)

        S.section('an error reply updates too, and the exit code stays the command\'s')
        serve(ta, 'a')
        install_from(URL)
        serve(tb, 'b')
        rc, out, err = tc('--strict', 'show', 'zzzzzz')
        check('--strict show of an unknown id: exit 1, no stdout, the 404 error, then the notice', rc == 1 and out == ''
              and 'error: board rejected GET /api/requests/zzzzzz: 404' in err and notice(DA, DB, '## v4').split('\n')[0] in err
              and baked_docs() == DB, (rc, out, err))

        S.section('its own board under another spelling of its URL (the scheme\'s case, a trailing slash) is its own')
        serve(ta, 'a')
        install_from(URL)
        serve(tb, 'b')
        rc, out, err = tc('--url', f'HTTP://127.0.0.1:{P}/', 'ping')
        check('HTTP://127.0.0.1:PORT/ ping: updated', rc == 0 and 'updated the skill in' in err and baked_docs() == DB, (rc, err))

        S.section('no rule installed: CLAUDE.md untouched; nothing newer than the API version: the newest entry')
        (CC / 'CLAUDE.md').write_text('# no rule here\n')
        serve(ta, 'a')
        rc, out, err = tc('list')
        entries = [line for line in err.splitlines() if line.startswith('## v')]
        check("back to the older docs (API 3): what's new is its newest entry, v3, alone", rc == 0 and "taskctl: what's new:\n## v3 — " in err
              and entries == [entries[0]] and entries[0].startswith('## v3 — ') and baked_docs() == DA, err[-800:])
        check('a CLAUDE.md without the rule is left alone', (CC / 'CLAUDE.md').read_text() == '# no rule here\n')

        S.section('TASKS_NO_UPDATE, update, update --force, and moving the skill to another board')
        serve(tb, 'b')
        for value in ('1', 'true', 'yes'):
            rc, out, err = tc('ping', env=dict(ENV, TASKS_NO_UPDATE=value))
            check(f'TASKS_NO_UPDATE={value}: no update, no notice', rc == 0 and 'instructions changed' not in err and baked_docs() == DA, err)
        rc, out, err = tc('update', env=dict(ENV, TASKS_NO_UPDATE='1'))
        check('update is explicit: it ignores TASKS_NO_UPDATE and updates, one notice', rc == 0 and baked_docs() == DB
              and err.count("what's new") == 1, (rc, err))
        rc, out, err = tc('update')
        check('update when current: "already current", exit 0, nothing on stdout', rc == 0 and out == ''
              and err == f'taskctl: the skill in {SKILL} is already current (docs {DB})\n', (rc, err))
        rc, out, err = tc('update', '--force')
        check('update --force: reinstalls, the notice (docs B -> B), exit 0', rc == 0 and out == ''
              and f'(docs {DB} -> {DB}); updated the skill in {SKILL}' in err, (rc, err))
        rc, out, err = tc('install-rule')
        rc, out, err = tc('--url', OTHER, 'update', '--force')
        md = (CC / 'CLAUDE.md').read_text()
        check('update --force from another board moves the skill there: BAKED_URL, and the rule\'s links', rc == 0
              and f'BAKED_URL = "{OTHER}"' in (SKILL / 'taskctl.py').read_text() and f'{OTHER}/r/<rid>' in md
              and f'{URL}/r/<rid>' not in md, (rc, err, md))
        rc, out, err = tc('ping')
        check('...so a plain ping goes there now', rc == 0 and out == OTHER + '\n', (rc, out))
        rc, out, err = tc('--url', URL, 'update', '--force')
        check('and back', rc == 0 and f'BAKED_URL = "{URL}"' in (SKILL / 'taskctl.py').read_text()
              and f'{URL}/r/<rid>' in (CC / 'CLAUDE.md').read_text(), (rc, err))

        S.section('refusals: one warning (or exit 1 for update), nothing replaced')
        rc, out, err = tc('--url', URL, 'update', exe=WRAPPER)
        check('update from the repo copy: exit 1, not an installed skill', rc == 1 and 'not an installed skill' in err, (rc, err))
        rc, out, err = tc('--url', URL, 'ping', exe=WRAPPER)
        check('the repo copy (unrendered BAKED_DOCS) never updates itself', rc == 0 and 'instructions changed' not in err, err)
        live.pop().stop()
        rc, out, err = tc('update')
        check('update with its board down: exit 1', rc == 1 and f'error: cannot update the skill in {SKILL}' in err
              and 'unreachable' in err, (rc, err))
        serve(ta, 'a')
        install_from(URL)
        serve(tb, 'b')
        SKILL.chmod(0o555)
        try:
            rc, out, err = tc('ping')
        finally:
            SKILL.chmod(0o755)
        check('a read-only skill dir: one warning naming the fix, exit and stdout unchanged', rc == 0 and out == URL + '\n'
              and err.count('warning:') == 1 and f'but updating the skill in {SKILL} failed' in err
              and f'run {SKILL}/taskctl update' in err and baked_docs() == DA, (rc, out, err))
        td = tasks_copy(TMP / 'tasks_d')
        (td / 'taskctl.py').write_text('BAKED_URL = "{{BASE_URL}}"\nBAKED_DOCS = "{{DOCS_VERSION}}"\nthis is not python (\n')
        te = tasks_copy(TMP / 'tasks_e')
        (te / 'skill' / 'SKILL.md').write_text('no front matter here\n')
        tf = tasks_copy(TMP / 'tasks_f')
        (tf / 'templates' / 'changelog.md').unlink()
        installed = (SKILL / 'SKILL.md').read_bytes()
        serve(td, 'd')
        rc, out, err = tc('ping')
        check('a served taskctl.py that does not compile: refused, one warning, nothing replaced', rc == 0 and 'does not compile' in err
              and err.count('warning:') == 1 and baked_docs() == DA and (SKILL / 'SKILL.md').read_bytes() == installed, (rc, err))
        serve(te, 'e')
        rc, out, err = tc('ping')
        check('a served SKILL.md without front matter: refused, one warning', rc == 0 and 'does not start with ---' in err
              and err.count('warning:') == 1 and baked_docs() == DA, (rc, err))
        serve(tf, 'f')
        rc, out, err = tc('ping')
        check('no changelog to fetch: updated anyway, one warning, the notice without "what\'s new"', rc == 0
              and 'cannot fetch the changelog' in err and err.count('warning:') == 1 and "what's new" not in err
              and 'updated the skill in' in err and baked_docs() == docs_version(tf), (rc, err))
        rc, out, err = tc('--strict', 'new', 'still works', env=dict(ENV, CLAUDE_CONFIG_DIR=str(TMP / 'nocc')))
        check('the installed skill works normally after all that', rc == 0 and len(out.split()) == 1, (rc, out, err))
    finally:
        while live:
            live.pop().stop()
        other.stop()


if __name__ == '__main__':
    main()
    sys.exit(S.finish())
