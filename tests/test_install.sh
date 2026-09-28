#! /bin/bash
# Be a remote agent: fetch the rendered usage text from a scratch board by a non-default address
# (http://localhost:PORT, standing in for the board's LAN address), then follow it verbatim in bash,
# zsh and fish (the last two are skipped when not installed): the one-line install, install-rule,
# and the installed taskctl with only its baked URL. Also: crafted Host headers, proxies, and the
# CLAUDE_CONFIG_DIR variant of the install command.
. "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"

command -v curl >/dev/null 2>&1 || skip_suite "curl not installed (the install command uses it)"

P="$(free_port)"
start_server board "$P" --host 127.0.0.1
LOCAL="http://127.0.0.1:$P"
REMOTE="http://localhost:$P"
HN="$HOSTNAME"
CURL=(curl -fsS --noproxy '*' --connect-timeout 5)

api_field() { # api_field PATH FIELD: one field of a JSON answer
    "${CURL[@]}" "$LOCAL$1" | python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$2"
}

section "crafted Host headers never reach the rendered text"
for h in 'x";rm -rf ~;"' 'a b' "evil.example:$P/x" '{{BASE_URL}}' ''; do
    out="$("${CURL[@]}" -H "Host: $h" "$LOCAL/api/usage")"
    base="$(sed -n 's/^\(http[^ ]*\) is a task-status board.*/\1/p' <<< "$out" | head -1)"
    check "crafted Host [$h]: usage falls back to a clean base ($base)" [ "$base" = "$LOCAL" ]
    py="$("${CURL[@]}" -H "Host: $h" "$LOCAL/api/skill/taskctl.py" | grep '^BAKED_URL')"
    check "crafted Host [$h]: not baked into taskctl.py" [ "$py" = "BAKED_URL = \"$LOCAL\"" ]
done
out="$("${CURL[@]}" -H 'Accept: */*' "$LOCAL/")"
check "GET / without text/html is the usage text" has "$out" "is a task-status board"
out="$("${CURL[@]}" -H 'Accept: text/html' "$LOCAL/")"
check "GET / with text/html is the page" has "$out" "<html"

section "the usage text"
usage="$("${CURL[@]}" "$LOCAL/api/usage")"
n_curl="$(grep -o 'curl -f\?sS ' <<< "$usage" | wc -l)"
n_noproxy="$(grep -o "curl -f\?sS --connect-timeout 5 --noproxy '\*' " <<< "$usage" | wc -l)"
check "every curl in it has --connect-timeout 5 --noproxy '*' ($n_noproxy of $n_curl)" [ "$n_curl" = "$n_noproxy" ]
check "all 8 curls are there (3 install + 5 examples)" [ "$n_noproxy" = 8 ]
check "it mentions the IPv6 link-local / IP form note" has "$usage" "IPv6 link-local"
check "it names the board by the address the agent used" has "$usage" "$LOCAL is a task-status board"

install_cmd_of() { awk '/^    mkdir -p ~\/.claude\/skills\/task-status && curl/ { sub(/^    /, ""); print; exit }' <<< "$1"; }
rule_cmd_of() { awk '/^    ~\/.claude\/skills\/task-status\/taskctl install-rule$/ { sub(/^    /, ""); print; exit }' <<< "$1"; }

for shell in bash zsh fish; do
    if ! command -v "$shell" >/dev/null 2>&1; then
        skip "$shell: the install flow ($shell not installed)"
        continue
    fi
    section "$shell: the remote agent's install flow"
    H="$TMP/home_inst_$shell"
    mkdir -p "$H"
    usage="$(HOME=$H "${CURL[@]}" "$REMOTE/api/usage")"
    check "$shell: usage fetched from the remote address" has "$usage" "$REMOTE is a task-status board"
    cmd="$(install_cmd_of "$usage")"
    check "$shell: install command found" [ -n "$cmd" ]
    check "$shell: install command uses the remote base" has "$cmd" "$REMOTE/api/skill/taskctl.py"
    check "$shell: every curl in it has --noproxy '*'" [ "$(grep -o 'curl ' <<< "$cmd" | wc -l)" = "$(grep -o "curl -fsS --connect-timeout 5 --noproxy '\*' " <<< "$cmd" | wc -l)" ]
    out="$(cd "$H" && HOME=$H "$shell" -c "$cmd" 2>&1)"
    rc=$?
    check "$shell: install command exits 0" [ $rc -eq 0 ]
    [ $rc -eq 0 ] || echo "$out" | sed 's/^/       /'
    S="$H/.claude/skills/task-status"
    check "$shell: SKILL.md present" [ -s "$S/SKILL.md" ]
    check "$shell: taskctl executable" [ -x "$S/taskctl" ]
    check "$shell: taskctl.py executable" [ -x "$S/taskctl.py" ]
    check "$shell: baked url is the remote base" grep -qx "BAKED_URL = \"$REMOTE\"" "$S/taskctl.py"
    check "$shell: ping printed the base" has "$out" "$REMOTE"
    check "$shell: ping printed ok" has "$out" "taskctl: ok: board at $REMOTE"
    PH="$TMP/home_proxy_$shell"
    mkdir -p "$PH"
    pout="$(cd "$PH" && HOME=$PH http_proxy=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 ALL_PROXY=http://127.0.0.1:9 \
            all_proxy=http://127.0.0.1:9 "$shell" -c "$cmd" 2>&1)"
    prc=$?
    check "$shell: install works with a dead proxy in the environment (rc $prc)" test $prc -eq 0 -a -x "$PH/.claude/skills/task-status/taskctl"
    check "$shell: the proxied install still pings ok" has "$pout" "taskctl: ok: board at $REMOTE"

    rule_cmd="$(rule_cmd_of "$usage")"
    check "$shell: install-rule command found" [ "$rule_cmd" = "~/.claude/skills/task-status/taskctl install-rule" ]
    printf 'my own notes\n' > "$H/.claude/CLAUDE.md"
    out1="$(cd "$H" && env -u CLAUDE_CONFIG_DIR HOME=$H "$shell" -c "$rule_cmd" 2>&1)"
    rc1=$?
    out2="$(cd "$H" && env -u CLAUDE_CONFIG_DIR HOME=$H "$shell" -c "$rule_cmd" 2>&1)"
    rc2=$?
    check "$shell: install-rule 1 installed" bash -c '[ "$1" = 0 ] && [[ $2 == *"taskctl: rule installed in $3/.claude/CLAUDE.md"* ]]' _ "$rc1" "$out1" "$H"
    check "$shell: install-rule 2 unchanged" bash -c '[ "$1" = 0 ] && [[ $2 == *"taskctl: rule unchanged in $3/.claude/CLAUDE.md"* ]]' _ "$rc2" "$out2" "$H"
    check "$shell: exactly one begin marker" [ "$(grep -c '^<!-- task-status:begin -->$' "$H/.claude/CLAUDE.md")" = 1 ]
    check "$shell: exactly one end marker" [ "$(grep -c '^<!-- task-status:end -->$' "$H/.claude/CLAUDE.md")" = 1 ]
    check "$shell: own notes kept" grep -qx 'my own notes' "$H/.claude/CLAUDE.md"
    check "$shell: the rule links to the remote base" grep -qF "$REMOTE/r/<rid>" "$H/.claude/CLAUDE.md"

    # The installed taskctl, with only its baked URL.
    TC="$S/taskctl"
    ids="$(cd "$H" && env -u TASKS_URL HOME=$H "$shell" -c "$TC --strict new 'Remote agent check $shell' -t 'first step' -t 'second step'" 2>"$TMP/new_err")"
    rc=$?
    err="$(cat "$TMP/new_err")"
    check "$shell: new exits 0" [ $rc -eq 0 ]
    mapfile -t idv <<< "$ids"
    RID="${idv[0]:-}"
    T1="${idv[1]:-}"
    T2="${idv[2]:-}"
    check "$shell: new printed rid + 2 tids" bash -c '[ "$1" = 3 ] && [ "$3" = "$2-1" ] && [ "$4" = "$2-2" ]' _ "${#idv[@]}" "$RID" "$T1" "$T2"
    check "$shell: new's page link is on the remote base" has "$err" "page: $REMOTE/r/$RID"
    check "$shell: origin is hostname:cwd" [ "$(api_field "/api/requests/$RID" origin)" = "$HN:home_inst_$shell" ]
    out="$(env -u TASKS_URL HOME=$H "$shell" -c "$TC --strict progress $T1 40 'Halfway through the first step' --eta 5m" 2>&1)"
    check "$shell: progress confirms" bash -c '[ "$1" = 0 ] && [[ $2 == *"$3 running 40%  eta ~5m00s"* ]]' _ $? "$out" "$T1"
    out="$(env -u TASKS_URL HOME=$H "$shell" -c "$TC --strict done $T1 'First step finished'" 2>&1)"
    check "$shell: done task confirms, no start nudge" bash -c '[ "$1" = 0 ] && [[ $2 == *"$3 done"* && $2 != *"never started"* ]]' _ $? "$out" "$T1"
    out="$(env -u TASKS_URL HOME=$H "$shell" -c "$TC --strict done $RID 'All done'" 2>&1)"
    check "$shell: done request confirms 1/1 (the pending one cancelled)" bash -c '[ "$1" = 0 ] && [[ $2 == *"request $3 done (1/1 tasks done)"* ]]' _ $? "$out" "$RID"
    check "$shell: done request lists the cancelled task" has "$out" "never-started tasks cancelled: $T2"
    out="$(env -u TASKS_URL HOME=$H "$shell" -c "$TC --strict show $RID" 2>/dev/null)"
    check "$shell: show header" has "$out" "$RID  done      100%  1/1 done"
    check "$shell: show page line" has "$out" "page: $REMOTE/r/$RID"
    check "$shell: show task 1" has "$out" "$T1  first step: First step finished"
    check "$shell: show task 2 cancelled" has "$out" "cancelled"
    out="$(env -u TASKS_URL HOME=$H "$shell" -c "$TC --strict show $T1" 2>/dev/null)"
    check "$shell: show one task" has "$out" "done        100%"
    out="$(env -u TASKS_URL HOME=$H "$shell" -c "$TC --strict list --all" 2>/dev/null)"
    check "$shell: list --all includes it" has "$out" "$RID"
    out="$(env -u TASKS_URL HOME=$H "$shell" -c "$TC --strict ping" 2>/dev/null)"
    check "$shell: ping uses the baked url" [ "$out" = "$REMOTE" ]
done

section "the CLAUDE_CONFIG_DIR variant (every ~/.claude replaced, as the text says)"
SH=fish
command -v fish >/dev/null 2>&1 || SH=bash
H="$TMP/home_ccd"
C="$TMP/cc_ccd"
mkdir -p "$H"
cmd="$(install_cmd_of "$("${CURL[@]}" "$REMOTE/api/usage")")"
cmd="${cmd//\~\/.claude/$C}"
out="$(cd "$H" && HOME=$H CLAUDE_CONFIG_DIR=$C "$SH" -c "$cmd" 2>&1)"
check "substituted install exits 0 ($SH)" [ $? -eq 0 ]
check "it lands in CLAUDE_CONFIG_DIR" [ -x "$C/skills/task-status/taskctl" ]
HOME=$H CLAUDE_CONFIG_DIR=$C "$C/skills/task-status/taskctl" install-rule >/dev/null 2>&1
HOME=$H CLAUDE_CONFIG_DIR=$C "$C/skills/task-status/taskctl" install-rule >/dev/null 2>&1
check "install-rule twice: one block in \$CLAUDE_CONFIG_DIR/CLAUDE.md" [ "$(grep -c 'task-status:begin' "$C/CLAUDE.md")" = 1 ]
check "HOME untouched" [ ! -e "$H/.claude" ]

stop_server board
check "port $P free at the end" port_free "$P"
finish
