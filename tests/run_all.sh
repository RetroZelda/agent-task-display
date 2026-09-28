#! /bin/bash
# Runs the test suites and prints a PASS / FAIL / SKIP table with check counts and times.
#
#   tests/run_all.sh                   every suite, one after another
#   tests/run_all.sh cli ufw           just these (names as --list prints them)
#   tests/run_all.sh -v ...            stream every suite's output (and its passing checks) live; with -j, only its log
#   tests/run_all.sh -j 4 ...          run up to 4 suites at once (they share nothing but the CPU)
#   tests/run_all.sh --list            the suite names
#
# Exits 0 when nothing failed (SKIPs, for optional tools that are not installed, are fine), 1 otherwise.
# A failing suite's output is printed after the table and its log kept.
set -uo pipefail
# Every suite already scrubs these, but a TASKS_URL exported for day-to-day agent use must never
# steer a test at a real board, so clear them here too before anything is launched.
unset TASKS_URL TASKS_HOST TASKS_PORT TASKS_DB TASKS_PUBLIC_URL TASKS_STRICT TASKS_TIMEOUT

HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
# name|file|what it covers
SUITES=(
    "taskctl_units|test_taskctl_units.py|taskctl.py parsers, formatters, rule merge, run monitor"
    "server|test_server.py|server contract: routes, shapes, validation, state rules, templates"
    "cli_offline|test_cli_offline.py|taskctl with no board: exit codes, offline ids, the sh wrapper"
    "cli|test_cli.py|taskctl <-> server: every subcommand, run, install-rule"
    "ipv6|test_ipv6.py|dual-stack, IPv6 Origin/Host, --host variants, HTTP error paths"
    "concurrency|test_concurrency.py|40 polling readers + a writer"
    "launcher|test_launcher.sh|tasks.sh lifecycle, banner, pidfiles, busy port, LAN paths"
    "install_skill|test_install_skill.sh|tasks.sh --install-skill"
    "ufw|test_ufw.sh|tasks.sh firewall detection with fake ufw files"
    "install|test_install.sh|the remote-agent install flow in bash, zsh, fish"
    "skill_examples|test_skill_examples.sh|every taskctl example in SKILL.md"
    "ui|test_ui.py|the dashboard in headless Firefox"
)

usage() { sed -n '2,11s/^# \{0,1\}//p' "$0"; }

VERBOSE=0
JOBS=1
WANT=()
while [ $# -gt 0 ]; do
    case "$1" in
        -h|--help) usage; exit 0 ;;
        -l|--list)
            for s in "${SUITES[@]}"; do IFS='|' read -r n f d <<< "$s"; printf '  %-15s %s\n' "$n" "$d"; done
            exit 0 ;;
        -v|--verbose) VERBOSE=1 ;;
        -j) JOBS="${2:-}"; shift ;;
        -j*) JOBS="${1#-j}" ;;
        -*) echo "run_all.sh: unknown option $1" >&2; usage >&2; exit 2 ;;
        *) WANT+=("$1") ;;
    esac
    shift
done
[[ $JOBS =~ ^[1-9][0-9]*$ ]] || { echo "run_all.sh: -j needs a positive number" >&2; exit 2; }

if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    echo "run_all.sh: python3 3.10 or newer is required" >&2
    exit 2
fi

# The selected suites, in the table's order; a name may be given as cli, test_cli or test_cli.py.
SELECTED=()
for s in "${SUITES[@]}"; do
    IFS='|' read -r name file desc <<< "$s"
    if [ ${#WANT[@]} -eq 0 ]; then
        SELECTED+=("$s")
        continue
    fi
    for w in "${WANT[@]}"; do
        w="${w##*/}"; w="${w#test_}"; w="${w%.py}"; w="${w%.sh}"
        if [ "$w" = "$name" ]; then SELECTED+=("$s"); fi
    done
done
for w in "${WANT[@]}"; do
    n="${w##*/}"; n="${n#test_}"; n="${n%.py}"; n="${n%.sh}"
    if ! printf '%s\n' "${SUITES[@]}" | cut -d'|' -f1 | grep -qx -- "$n"; then
        echo "run_all.sh: no suite named '$w' (see --list)" >&2
        exit 2
    fi
done

LOGS="$(mktemp -d "${TMPDIR:-/tmp}/tasks-tests-logs.XXXXXX")"
export PYTHONDONTWRITEBYTECODE=1
[ "$VERBOSE" = 1 ] && export TESTS_VERBOSE=1

# run_suite NAME FILE: runs it, writes its log, and $LOGS/NAME.result = "rc milliseconds". The suite is
# a background job of its own (its pid in $LOGS/NAME.pid), so an interrupt can be passed on to it.
run_suite() {
    local name="$1" file="$2" t0 rc pid
    local -a cmd
    case "$file" in
        *.py) cmd=(python3 "$HERE/$file") ;;
        *) cmd=(bash "$HERE/$file") ;;
    esac
    t0="${EPOCHREALTIME/./}"
    if [ "$VERBOSE" = 1 ] && [ "$JOBS" = 1 ]; then
        "${cmd[@]}" > >(trap '' INT; tee "$LOGS/$name.log") 2>&1 &
    else
        "${cmd[@]}" >"$LOGS/$name.log" 2>&1 &
    fi
    pid=$!
    echo "$pid" > "$LOGS/$name.pid"
    wait "$pid"
    rc=$?
    echo "$rc $(( (${EPOCHREALTIME/./} - t0) / 1000 ))" > "$LOGS/$name.result"
}

# Ctrl-C or SIGTERM: every running suite gets a SIGTERM, on which it stops its servers and removes
# its temp dirs; this script waits for that, then exits 130.
interrupted() {
    trap '' INT TERM
    local f
    for f in "$LOGS"/*.pid; do
        [ -e "$f" ] && [ ! -e "${f%.pid}.result" ] && kill -TERM "$(cat "$f")" 2>/dev/null
    done
    wait
    echo
    echo "interrupted; logs in $LOGS"
    exit 130
}
trap interrupted INT TERM

T0="${EPOCHREALTIME/./}"
running=0
for s in "${SELECTED[@]}"; do
    IFS='|' read -r name file desc <<< "$s"
    if [ "$JOBS" = 1 ]; then
        [ "$VERBOSE" = 1 ] && echo "######## $name ($file)"
        [ "$VERBOSE" = 1 ] || printf '%-15s ... ' "$name"
        run_suite "$name" "$file"
        if [ "$VERBOSE" = 0 ]; then
            read -r rc ms < "$LOGS/$name.result"
            case "$rc" in 0) st=PASS ;; 77) st=SKIP ;; *) st=FAIL ;; esac
            printf '%s (%d.%ds)\n' "$st" $((ms / 1000)) $((ms % 1000 / 100))
        fi
    else
        while [ "$running" -ge "$JOBS" ]; do
            wait -n
            running=$((running - 1))
        done
        run_suite "$name" "$file" &
        running=$((running + 1))
    fi
done
wait
WALL=$(( (${EPOCHREALTIME/./} - T0) / 1000 ))

# The table
FAILED_SUITES=()
tp=0 tf=0 ts=0
echo
printf '%-15s %-6s %7s %7s %8s %9s\n' SUITE STATUS PASSED FAILED SKIPPED TIME
printf '%-15s %-6s %7s %7s %8s %9s\n' --------------- ------ ------- ------- -------- ---------
for s in "${SELECTED[@]}"; do
    IFS='|' read -r name file desc <<< "$s"
    read -r rc ms < "$LOGS/$name.result"
    line="$(grep -E '^RESULT: passed=[0-9]+ failed=[0-9]+ skipped=[0-9]+$' "$LOGS/$name.log" | tail -1)"
    p=0 f=0 k=0
    if [[ $line =~ passed=([0-9]+)\ failed=([0-9]+)\ skipped=([0-9]+) ]]; then
        p=${BASH_REMATCH[1]} f=${BASH_REMATCH[2]} k=${BASH_REMATCH[3]}
    fi
    if [ "$rc" = 77 ]; then
        st=SKIP
    elif [ "$rc" = 0 ] && [ -n "$line" ] && [ "$f" = 0 ]; then
        st=PASS
    else
        st=FAIL
        FAILED_SUITES+=("$name")
        [ -n "$line" ] || f="?"   # crashed before its summary
    fi
    tp=$((tp + p)); ts=$((ts + k)); [ "$f" = "?" ] || tf=$((tf + f))
    printf '%-15s %-6s %7s %7s %8s %6d.%ds\n' "$name" "$st" "$p" "$f" "$k" $((ms / 1000)) $((ms % 1000 / 100))
done
printf '%-15s %-6s %7s %7s %8s %9s\n' --------------- ------ ------- ------- -------- ---------
printf '%-15s %-6s %7d %7d %8d %6d.%ds\n' TOTAL "$([ ${#FAILED_SUITES[@]} -eq 0 ] && echo PASS || echo FAIL)" \
    "$tp" "$tf" "$ts" $((WALL / 1000)) $((WALL % 1000 / 100))

# Skip reasons, then the failures in full.
for s in "${SELECTED[@]}"; do
    IFS='|' read -r name file desc <<< "$s"
    grep -h -E '^(SKIP: |  SKIP )' "$LOGS/$name.log" 2>/dev/null | sed "s/^ *SKIP:\{0,1\} */  $name: skipped /"
done
if [ ${#FAILED_SUITES[@]} -gt 0 ]; then
    for name in "${FAILED_SUITES[@]}"; do
        echo
        echo "######## $name failed; last 60 lines of its output:"
        tail -n 60 "$LOGS/$name.log"
    done
    echo
    echo "logs kept in $LOGS"
    exit 1
fi
rm -rf "$LOGS"
exit 0
