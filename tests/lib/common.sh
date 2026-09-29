# shellcheck shell=bash
# Shared helpers for the bash test suites: source it first thing.
#
#   . "$(dirname "$0")/lib/common.sh"
#   check "label" COMMAND...        passes when COMMAND succeeds
#   run COMMAND...                  sets OUT (stdout), ERR (stderr) and RC
#   has "$TEXT" "needle"            substring test (hasnt: the opposite)
#   finish                          prints the RESULT line and exits 0/1
#
# Everything lives in $TMP (removed at exit, with any process whose command line mentions it), HOME,
# CLAUDE_CONFIG_DIR and taskctl's offline queue (TASKS_SPOOL_DIR) point into it, and every other
# TASKS_* variable is unset.

set -uo pipefail

LIB_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
TESTS_DIR="$(dirname -- "$LIB_DIR")"
REPO="$(dirname -- "$TESTS_DIR")"
TASKS="$REPO/tasks"
SUITE="${SUITE:-$(basename -- "$0" .sh)}"
REAL_PORT=8765
SKIP_EXIT=77

TMP="$(mktemp -d "${TMPDIR:-/tmp}/tasks-test-$SUITE.XXXXXX")"
export HOME="$TMP/home"
export CLAUDE_CONFIG_DIR="$HOME/.claude"
mkdir -p "$CLAUDE_CONFIG_DIR"
unset TASKS_URL TASKS_HOST TASKS_PORT TASKS_DB TASKS_PUBLIC_URL TASKS_STRICT TASKS_TIMEOUT CDPATH \
    TASKS_CONFIG TASKS_TLS_PORT TASKS_TLS_CERT TASKS_TLS_KEY TASKS_NO_UPDATE XDG_CACHE_HOME
while read -r name; do unset "$name"; done < <(compgen -e | grep '^TASKS_')
export TASKS_SPOOL_DIR="$TMP/spool"
export PYTHONDONTWRITEBYTECODE=1

PASS=0
FAIL=0
SKIPPED=0
FAILED_LABELS=()
CLEANUP_CMDS=()
T0=$SECONDS

# Kills every process (but this shell) whose command line mentions $TMP.
reap_tmp() {
    local f pid pids=()
    for f in /proc/[0-9]*/cmdline; do
        pid="${f#/proc/}"
        pid="${pid%/cmdline}"
        [ "$pid" = "$$" ] && continue
        if { tr '\0' ' ' < "$f"; } 2>/dev/null | grep -qF -- "$TMP"; then
            pids+=("$pid")
        fi
    done
    [ ${#pids[@]} -eq 0 ] && return 0
    kill -TERM "${pids[@]}" 2>/dev/null
    sleep 0.5
    kill -KILL "${pids[@]}" 2>/dev/null
    return 0
}

cleanup() {
    local cmd
    for cmd in "${CLEANUP_CMDS[@]}"; do
        eval "$cmd" >/dev/null 2>&1
    done
    [ -d /proc ] && reap_tmp
    chmod -R u+rwX "$TMP" 2>/dev/null
    rm -rf "$TMP"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

# on_exit 'COMMAND': run it (quietly) before $TMP is removed.
on_exit() { CLEANUP_CMDS+=("$1"); }

ok() {
    PASS=$((PASS + 1))
    if [ "${TESTS_VERBOSE:-}" = 1 ]; then echo "  ok   $1"; fi
}
bad() {
    FAIL=$((FAIL + 1))
    FAILED_LABELS+=("$1")
    echo "  FAIL $1"
    if [ -n "${2:-}" ]; then printf '       %s\n' "${2:0:1500}"; fi
}
check() { # check LABEL COMMAND...
    local label="$1"
    shift
    if "$@"; then ok "$label"; else bad "$label"; fi
}
# check_out LABEL COMMAND...: like check, and on failure shows the last run's output.
check_out() {
    local label="$1"
    shift
    if "$@"; then ok "$label"; else bad "$label" "rc=${RC:-} out=${OUT:-} err=${ERR:-}"; fi
}
skip() { SKIPPED=$((SKIPPED + 1)); echo "  SKIP $*"; }
section() { echo "== $*"; }
has() { [[ $1 == *"$2"* ]]; }
hasnt() { [[ $1 != *"$2"* ]]; }
run() { # run COMMAND...: sets OUT, ERR, RC
    OUT="$("$@" 2>"$TMP/.stderr")"
    RC=$?
    ERR="$(cat "$TMP/.stderr")"
}

skip_suite() {
    echo "SKIP: $*"
    echo "RESULT: passed=0 failed=0 skipped=1"
    exit "$SKIP_EXIT"
}

finish() {
    local label
    echo
    echo "$SUITE: $PASS passed, $FAIL failed, $SKIPPED skipped ($((SECONDS - T0))s)"
    for label in "${FAILED_LABELS[@]}"; do
        echo "  FAILED: $label"
    done
    echo "RESULT: passed=$PASS failed=$FAIL skipped=$SKIPPED"
    [ "$FAIL" = 0 ] && exit 0
    exit 1
}

free_port() { python3 "$LIB_DIR/harness.py" free-port; }

# port_free PORT: nothing accepts connections on it (IPv4 or IPv6 loopback).
port_free() {
    python3 - "$1" <<'PY'
import socket, sys
port = int(sys.argv[1])
for fam, addr in ((socket.AF_INET, '127.0.0.1'), (socket.AF_INET6, '::1')):
    try:
        with socket.socket(fam, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            s.connect((addr, port))
        sys.exit(1)
    except OSError:
        pass
PY
}

# wait_http URL [SECONDS]: until URL answers 2xx.
wait_http() {
    local url="$1" i
    for i in $(seq $(( ${2:-10} * 10 ))); do
        curl -fsS --noproxy '*' --max-time 1 -o /dev/null "$url" 2>/dev/null && return 0
        sleep 0.1
    done
    return 1
}

# start_server NAME PORT [SERVER ARGS...]: a scratch tasks/server.py with $TMP/NAME.db, .pid and .log.
start_server() {
    local name="$1" port="$2"
    shift 2
    python3 "$TASKS/server.py" --port "$port" --db "$TMP/$name.db" --pidfile "$TMP/$name.pid" "$@" \
        >"$TMP/$name.log" 2>&1 &
    eval "SERVER_PID_$name=$!"
    on_exit "kill -TERM $! 2>/dev/null; wait $! 2>/dev/null"
    if ! wait_http "http://127.0.0.1:$port/api/health" 10; then
        echo "scratch server $name did not start:" >&2
        cat "$TMP/$name.log" >&2
        exit 1
    fi
}

stop_server() { # stop_server NAME
    local pid
    eval "pid=\${SERVER_PID_$1:-}"
    [ -n "$pid" ] || return 0
    kill -TERM "$pid" 2>/dev/null
    wait "$pid" 2>/dev/null
}

# Launcher fixtures. tasks.sh finds its checkout from its own path, so a copy of it next to a copy of
# tasks/ keeps pidfiles, logs and the default database inside $TMP. Its firewall detection reads
# /etc/ufw and asks systemctl; the copy reads fake files in $FAKE instead, and $FAKE/bin (put first
# on PATH) holds a systemctl that says ufw is active while $FAKE/active exists, and an ip whose
# default route leaves from $FAKE_LAN_IP (a documentation address), so no result depends on the host.
FAKE="$TMP/fake"
FAKE_LAN_IP=192.0.2.10
FAKE_LAN_NET=192.0.2.0/24

make_fakes() {
    mkdir -p "$FAKE/bin" "$FAKE/etc/ufw"
    cat > "$FAKE/bin/systemctl" <<EOF
#!/bin/sh
case "\$*" in
    *is-active*ufw*) [ -e "$FAKE/active" ] && exit 0; exit 3 ;;
esac
exit 1
EOF
    cat > "$FAKE/bin/ip" <<EOF
#!/bin/sh
case "\$*" in
    "-4 route get 1.1.1.1") [ -s "$FAKE/route" ] && { cat "$FAKE/route"; exit 0; }; exit 2 ;;
    "-4 -o addr show dev docker0") [ -s "$FAKE/docker0" ] && { cat "$FAKE/docker0"; exit 0; }; exit 1 ;;
esac
exit 1
EOF
    chmod +x "$FAKE/bin/systemctl" "$FAKE/bin/ip"
    echo "1.1.1.1 via 192.0.2.1 dev eth0 src $FAKE_LAN_IP uid 1000" > "$FAKE/route"
    : > "$FAKE/docker0"
    export PATH="$FAKE/bin:$PATH"
}

# fake_ufw V4_RULES V6_RULES [IPV6 yes|no] [ENABLED yes|no]: ufw active with these user rules.
fake_ufw() {
    touch "$FAKE/active"
    printf 'ENABLED=%s\nLOGLEVEL=low\n' "${4:-yes}" > "$FAKE/etc/ufw/ufw.conf"
    printf 'IPV6=%s\nDEFAULT_INPUT_POLICY="DROP"\n' "${3:-yes}" > "$FAKE/default_ufw"
    printf '*filter\n:ufw-user-input - [0:0]\n### RULES ###\n%s\nCOMMIT\n' "$1" > "$FAKE/etc/ufw/user.rules"
    printf '*filter\n:ufw6-user-input - [0:0]\n### RULES ###\n%s\nCOMMIT\n' "$2" > "$FAKE/etc/ufw/user6.rules"
}
fake_ufw_inactive() { rm -f "$FAKE/active"; }

# make_root DIR: DIR/tasks.sh (reading $FAKE's ufw files) and DIR/tasks (a copy, without data/).
make_root() {
    local dir="$1" entry n
    mkdir -p "$dir/tasks"
    for entry in server.py taskctl taskctl.py skill static templates; do
        cp -R "$TASKS/$entry" "$dir/tasks/"
    done
    sed -e "s#/etc/ufw/#$FAKE/etc/ufw/#g" -e "s#/etc/default/ufw#$FAKE/default_ufw#g" "$REPO/tasks.sh" > "$dir/tasks.sh"
    chmod +x "$dir/tasks.sh"
    # Guard: if tasks.sh stops reading these paths, the fakes would silently test the host instead.
    n="$(grep -c -- "$FAKE/" "$dir/tasks.sh")"
    if [ "$n" -lt 4 ] || grep -vF -- "$FAKE/" "$dir/tasks.sh" | grep -q '/etc/ufw\|/etc/default/ufw'; then
        echo "error: tasks.sh reads its firewall state from paths these tests cannot fake" >&2
        exit 1
    fi
}
