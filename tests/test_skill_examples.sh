#! /bin/bash
# Runs every taskctl example in tasks/skill/SKILL.md (with ${CLAUDE_SKILL_DIR} filled in, as the skill
# loader does) with --strict semantics against a scratch board, the example ids mapped to real ones;
# under zsh when it is installed (the shell the examples are most fragile in), else bash.
. "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"

command -v curl >/dev/null 2>&1 || skip_suite "curl not installed"
SH=zsh
command -v zsh >/dev/null 2>&1 || SH=bash
SKILL="$TASKS/skill/SKILL.md"

P="$(free_port)"
start_server board "$P" --host 127.0.0.1
URL="http://127.0.0.1:$P"
# The skill as installed from this board: its taskctl reports there with no --url or TASKS_URL.
DIR="$CLAUDE_CONFIG_DIR/skills/task-status"
mkdir -p "$DIR"
for f in SKILL.md taskctl taskctl.py; do
    curl -fsS --noproxy '*' "$URL/api/skill/$f" -o "$DIR/$f"
done
chmod +x "$DIR/taskctl" "$DIR/taskctl.py"
export TASKS_STRICT=1
echo "  (running the examples under $SH)"

section "SKILL.md itself"
check "no \$ARGUMENTS / \$0..\$9 in the body (Claude Code would substitute them)" bash -c '! grep -qE "\\\$ARGUMENTS|\\\$[0-9]" "$1"' _ "$SKILL"
check "the front matter names the skill task-status" grep -qx 'name: task-status' "$SKILL"
for sub in new add start progress done fail show list ping; do
    check "allowed-tools pre-approves $sub" grep -qxF "  - Bash(\${CLAUDE_SKILL_DIR}/taskctl $sub *)" "$SKILL"
done
check "allowed-tools does not pre-approve run" bash -c '! grep -q "taskctl run \*)" "$1"' _ "$SKILL"

# Every example command line (code blocks and inline), placeholders excluded, in document order.
mapfile -t lines < <(grep -oE '\$\{CLAUDE_SKILL_DIR\}/taskctl [a-z-]+[^`]*' "$SKILL" | sed 's/[[:space:]]*$//' \
    | grep -v '<TID>\|<RID>\|COMMAND \.\.\.\| \*)$' | awk '!seen[$0]++')
echo "  (${#lines[@]} examples found)"
check "the examples were found" [ "${#lines[@]}" -ge 10 ]

section "the new examples"
news=()
for line in "${lines[@]}"; do
    case "$line" in *"taskctl new "*) news+=("$line") ;; esac
done
check "SKILL.md has a request example and a workflow example" [ "${#news[@]}" -ge 2 ]
RID=""
for line in "${news[@]}"; do
    want=$(( $(grep -o " -t " <<< "$line" | wc -l) + 1 ))
    out="$($SH -c "${line//\$\{CLAUDE_SKILL_DIR\}/$DIR}" 2>/dev/null)"
    mapfile -t ids <<< "$out"
    check "new example prints its rid + one tid per -t ($want lines): ${line:0:70}" [ "${#ids[@]}" -eq "$want" ]
    [ -n "$RID" ] || RID="${ids[0]}"         # the first request stands in for k3m9qa
    LAST_RID="${ids[0]}"
    LAST_N="$want"
done

section "every other example, in document order"
for line in "${lines[@]}"; do
    case "$line" in *"taskctl new "*) continue ;; esac
    cmd="${line//\$\{CLAUDE_SKILL_DIR\}/$DIR}"
    cmd="${cmd//k3m9qa/$RID}"
    # run examples wrap real scripts: wrap a harmless command with the same flags instead.
    if [[ $cmd == *" run "* ]]; then
        cmd="${cmd%% -- *} -- sh -c 'echo 3/10; echo 10/10'"
    fi
    out="$($SH -c "$cmd" 2>&1)"
    rc=$?
    if [ $rc -eq 0 ]; then ok "rc 0: $line"; else bad "rc $rc: $line" "$out"; fi
done

section "the dynamic fan-out form and the orchestrator's close"
out="$($SH -c "$DIR/taskctl add --start $LAST_RID 'worker:file-a'" 2>/dev/null)"
check "add --start RID LABEL prints the new tid" [ "$out" = "$LAST_RID-$LAST_N" ]
st="$(curl -fsS --noproxy '*' "$URL/api/tasks/$LAST_RID-$LAST_N" | python3 -c 'import json,sys; print(json.load(sys.stdin)["status"])')"
check "that task is running" [ "$st" = running ]
out="$($SH -c "$DIR/taskctl done $LAST_RID 'one-line summary'" 2>&1)"
check "done RID auto-closes the running task" has "$out" "still-running tasks closed as done: $LAST_RID-$LAST_N"
check "done RID cancels the pending ones" has "$out" "never-started tasks cancelled: $LAST_RID-1"
check "bare list" $SH -c "$DIR/taskctl list >/dev/null 2>&1"
check "bare ping" $SH -c "$DIR/taskctl ping >/dev/null 2>&1"

section "every subcommand the Commands table names exists"
subs="$(grep '^| `' "$SKILL" | grep -o '`[a-z][a-z-]*' | tr -d '`' | sort -u | tr '\n' ' ')"
check "the Commands table lists the core subcommands ($subs)" bash -c 'for s in new add start progress done fail show list ping run; do [[ " $1" == *" $s "* ]] || exit 1; done' _ "$subs"
for sub in $subs install-rule; do
    check "taskctl $sub -h" bash -c '"$1" "$2" -h >/dev/null 2>&1' _ "$DIR/taskctl" "$sub"
done

stop_server board
finish
