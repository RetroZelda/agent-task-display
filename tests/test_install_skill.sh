#! /bin/bash
# tasks.sh --install-skill: starts the board if needed, installs SKILL.md + taskctl + taskctl.py into
# ${CLAUDE_CONFIG_DIR:-~/.claude}/skills/task-status, installs the CLAUDE.md rule, prints the allow
# rules; and every way that can go wrong. A copy of the checkout in $TMP, a scratch config dir.
. "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"

command -v curl >/dev/null 2>&1 || skip_suite "curl not installed (tasks.sh needs it)"
command -v setsid >/dev/null 2>&1 || skip_suite "setsid not installed (tasks.sh --bg needs it)"
[ -d /proc ] || skip_suite "no /proc (tasks.sh identifies its server through /proc)"

make_fakes
fake_ufw "-A ufw-user-input -p tcp --dport 22 -j ACCEPT" "-A ufw6-user-input -p tcp --dport 22 -j ACCEPT"
printf '#!/bin/sh\necho "sudo $*" >> "%s/sudo.log"\nexit 1\n' "$TMP" > "$FAKE/bin/sudo"
chmod +x "$FAKE/bin/sudo"

R="$TMP/root"
make_root "$R"
T="$R/tasks.sh"
P="$(free_port)"
Q="$(free_port)"
DB="$TMP/install.db"
CC="$TMP/cc"
export CLAUDE_CONFIG_DIR="$CC"
D="$CC/skills/task-status"
on_exit "'$T' --port $P --stop"
cd "$TMP" || exit 1

snapshot() { stat -c '%n %Y %s %a' "$CC/skills/synced" "$CC/skills/synced"/*; md5sum "$CC/skills/synced/marker"; }
mkdir -p "$CC/skills/synced"
echo keep > "$CC/skills/synced/marker"
SYNC_BEFORE="$(snapshot)"

section "fresh install (board not running)"
run "$T" --port "$P" --db "$DB" --install-skill
check_out "exit 0" [ "$RC" = 0 ]
check "the board was started" has "$OUT" "tasks board: started (pid"
check "SKILL.md installed, mode 644" test "$(stat -c %a "$D/SKILL.md" 2>/dev/null)" = 644
check "taskctl installed, mode 755" test "$(stat -c %a "$D/taskctl" 2>/dev/null)" = 755
check "taskctl.py installed, mode 755" test "$(stat -c %a "$D/taskctl.py" 2>/dev/null)" = 755
check "SKILL.md is the served (repo) one" cmp -s "$D/SKILL.md" "$TASKS/skill/SKILL.md"
check "taskctl is the wrapper" cmp -s "$D/taskctl" "$TASKS/taskctl"
check "taskctl.py baked with the local URL" grep -qx "BAKED_URL = \"http://127.0.0.1:$P\"" "$D/taskctl.py"
check "one rule block in CLAUDE.md" test "$(grep -c '<!-- task-status:begin -->' "$CC/CLAUDE.md")" = 1
check "the rule links to the board" grep -qF "http://127.0.0.1:$P/r/<rid>" "$CC/CLAUDE.md"
check "the installed taskctl's ping is shown" has "$ERR" "taskctl: ok: board at http://127.0.0.1:$P"
check "install-rule reported" has "$ERR" "taskctl: rule installed in $CC/CLAUDE.md"
# The recommended rules: one JSON object that pastes into settings.json as it is, allowing every
# taskctl call and asking for `taskctl run` and `taskctl update`, also behind a global option such as
# --url (an ask rule wins over an allow rule).
ASK_RULES='["run *", "--* run *", "update*", "--* update*", "install-rule*", "--* install-rule*"]'

perm_json() { # the {...} block of the output, one object
    python3 -c '
import json, re, sys
m = re.search(r"^\{\n.*?^\}$", sys.argv[1], re.M | re.S)
print(json.dumps(json.loads(m.group(0))) if m else "")' "$1"
}
PERMS="$(perm_json "$OUT")"
check "the permission block is valid JSON" test -n "$PERMS"
check "allow: exactly one rule, for every taskctl call" python3 -c '
import json, sys
p = json.loads(sys.argv[1])["permissions"]
sys.exit(p["allow"] != ["Bash(%s/taskctl *)" % sys.argv[2]])' "$PERMS" "$D"
check "ask: run and update, each also behind a global option" python3 -c '
import json, sys
p = json.loads(sys.argv[1])["permissions"]
sys.exit(p["ask"] != ["Bash(%s/taskctl %s)" % (sys.argv[2], r) for r in json.loads(sys.argv[3])] or set(p) != {"allow", "ask"})' \
    "$PERMS" "$D" "$ASK_RULES"
check "the ask rules are explained (they win over the allow rule)" has "$OUT" "the ask rules win over it"
check "banner: skill installed" has "$OUT" "  skill       installed in $D (reports to http://127.0.0.1:$P)"
check "banner: rule present" has "$OUT" "  rule        present in $CC/CLAUDE.md"
check "the permission rules come last" bash -c 'tail -2 <<< "$1" | grep -q "reload-skills"' _ "$OUT"
check "the rule block in CLAUDE.md asks for taskctl ask / resume" grep -qF "taskctl ask <rid>" "$CC/CLAUDE.md"
check "no settings.json written" test ! -e "$CC/settings.json"
check "no temp files left" test -z "$(find "$D" -name '.*' -type f)"
check "skills/synced untouched" test "$(snapshot)" = "$SYNC_BEFORE"
check "the installed taskctl works" bash -c '"$1" --strict ping >/dev/null 2>&1' _ "$D/taskctl"
check "sudo never invoked" test ! -e "$TMP/sudo.log"
PID1="$(cat "$R/tasks/data/server-$P.pid")"

section "re-run (idempotent, via --bg --install-skill)"
cp "$CC/CLAUDE.md" "$TMP/claude_before.md"
run "$T" --port "$P" --db "$DB" --bg --install-skill
check "exit 0" [ "$RC" = 0 ]
check "already running, same pid" has "$OUT" "tasks board: already running (pid $PID1, port $P)"
check "rule unchanged" has "$ERR" "taskctl: rule unchanged in $CC/CLAUDE.md"
check "CLAUDE.md byte-identical" cmp -s "$CC/CLAUDE.md" "$TMP/claude_before.md"
check "no stray files in the skill dir" test "$(ls -A "$D" | tr '\n' ' ')" = "SKILL.md taskctl taskctl.py "

section "a CLAUDE.md with other content keeps it"
printf '# my notes\nkeep me\n' > "$CC/CLAUDE.md"
run "$T" --port="$P" --install-skill
check "exit 0" [ "$RC" = 0 ]
check "existing content kept" grep -qx 'keep me' "$CC/CLAUDE.md"
check "block appended once" test "$(grep -c '<!-- task-status:end -->' "$CC/CLAUDE.md")" = 1

section "--status reports the install"
run "$T" --port "$P" --status
check "status exit 0" [ "$RC" = 0 ]
check "status: skill installed" has "$OUT" "  skill       installed in $D (reports to http://127.0.0.1:$P)"
run "$T" --port "$Q" --status
check "status on another port: the skill reports elsewhere, with the fix" \
    has "$OUT" "  skill       installed in $D, but it reports to http://127.0.0.1:$P -> $T --install-skill --port $Q"

section "TASKS_URL pointing elsewhere makes the check fail loudly"
run env TASKS_URL=http://127.0.0.1:9 "$T" --port "$P" --install-skill
check "exit 1" [ "$RC" = 1 ]
check "the error names TASKS_URL" has "$ERR" "TASKS_URL=http://127.0.0.1:9"

section "a symlinked skill dir is refused"
L="$TMP/cc_link"
mkdir -p "$L/skills" "$TMP/link_target"
echo old > "$TMP/link_target/SKILL.md"
ln -s "$TMP/link_target" "$L/skills/task-status"
run env CLAUDE_CONFIG_DIR="$L" "$T" --port "$P" --install-skill
check "exit 1" [ "$RC" = 1 ]
check "the error says symlink" has "$ERR" "is a symlink"
check "the link target is untouched" test "$(cat "$TMP/link_target/SKILL.md")" = old
check "no CLAUDE.md written" test ! -e "$L/CLAUDE.md"
run env CLAUDE_CONFIG_DIR="$L" "$T" --port "$P" --status
check "--status shows the symlink" has "$OUT" "is a symlink"
mkdir -p "$TMP/cc_partial/skills/task-status"
echo partial > "$TMP/cc_partial/skills/task-status/SKILL.md"
run env CLAUDE_CONFIG_DIR="$TMP/cc_partial" "$T" --port "$P" --status
check "--status shows an incomplete install, with the fix" has "$OUT" "  skill       incomplete in $TMP/cc_partial/skills/task-status -> $T --install-skill --port $P"

section "HOME fallback when CLAUDE_CONFIG_DIR is unset"
H="$TMP/home_fallback"
mkdir -p "$H"
run env -u CLAUDE_CONFIG_DIR HOME="$H" "$T" --port "$P" --install-skill
check "exit 0" [ "$RC" = 0 ]
check "installed under HOME" test -x "$H/.claude/skills/task-status/taskctl"
check "rule under HOME" grep -q '<!-- task-status:begin -->' "$H/.claude/CLAUDE.md"
check "the permission rules use the HOME path" python3 -c '
import json, sys
p = json.loads(sys.argv[1])["permissions"]
sys.exit(p != {"allow": ["Bash(%s/taskctl *)" % sys.argv[2]],
              "ask": ["Bash(%s/taskctl %s)" % (sys.argv[2], r) for r in json.loads(sys.argv[3])]})' \
    "$(perm_json "$OUT")" "$H/.claude/skills/task-status" "$ASK_RULES"

section "a relative CLAUDE_CONFIG_DIR resolves against the caller's cwd"
mkdir -p "$TMP/cwd"
( cd "$TMP/cwd" && CLAUDE_CONFIG_DIR=relcc "$T" --port "$P" --install-skill >/dev/null 2>&1 )
RC=$?
check "exit 0" [ "$RC" = 0 ]
check "skill under cwd/relcc" test -x "$TMP/cwd/relcc/skills/task-status/taskctl"
check "rule under cwd/relcc too" grep -q '<!-- task-status:begin -->' "$TMP/cwd/relcc/CLAUDE.md"
check "nothing written under the checkout" test ! -e "$R/relcc"

section "--stop, then --status still reports the install; --bg --install-skill from cold"
run "$T" --port "$P" --stop
check "stopped" has "$OUT" "tasks board: stopped (pid $PID1, port $P)"
run "$T" --port "$P" --status
check "--status after stop: exit 3, skill installed" bash -c '[ "$1" = 3 ] && [[ $2 == *"skill       installed in"* ]]' _ "$RC" "$OUT"
run "$T" --port "$P" --db "$DB" --bg --install-skill
check "--bg --install-skill from cold: started" bash -c '[ "$1" = 0 ] && [[ $2 == *"tasks board: started"* ]]' _ "$RC" "$OUT"
"$T" --port "$P" --stop >/dev/null

section "a failed download keeps the old install (fake server: SKILL.md template missing)"
F="$TMP/fake_root"
mkdir -p "$F/tasks"
cp "$R/tasks.sh" "$F/tasks.sh"
cat > "$F/tasks/server.py" <<'EOF'
import argparse, json, os
from http.server import HTTPServer, BaseHTTPRequestHandler
p = argparse.ArgumentParser()
for a in ('--host', '--port', '--db', '--pidfile', '--public-url'):
    p.add_argument(a)
a = p.parse_args()
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == '/api/health':
            code, body = 200, json.dumps({'ok': True, 'service': 'tasks', 'version': '1', 'pid': os.getpid(),
                                          'now': 0, 'started_at': 0, 'requests_running': 0})
        elif self.path == '/api/skill/SKILL.md':
            code, body = 500, '{"error": "template missing: skill/SKILL.md"}'
        else:
            code, body = 200, '#!/bin/sh\nBAKED_URL = "http://x"\n'
        body = body.encode()
        self.send_response(code); self.send_header('Content-Length', str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
srv = HTTPServer(('127.0.0.1', int(a.port)), H)
open(a.pidfile, 'w').write('%d\n' % os.getpid())
srv.serve_forever()
EOF
on_exit "'$F/tasks.sh' --port $P --stop"
BEFORE="$(md5sum "$D/SKILL.md" "$D/taskctl" "$D/taskctl.py")"
run "$F/tasks.sh" --port "$P" --db "$DB" --install-skill
check "exit 1" [ "$RC" = 1 ]
check "shows the server's error" has "$ERR" "template missing: skill/SKILL.md"
check "says the install is unchanged" has "$ERR" "is unchanged"
check "the old install is intact" test "$(md5sum "$D/SKILL.md" "$D/taskctl" "$D/taskctl.py")" = "$BEFORE"
check "no temp files left" test -z "$(find "$D" -name '.*' -type f)"
"$F/tasks.sh" --port "$P" --stop >/dev/null

check "port $P free at the end" port_free "$P"
finish
