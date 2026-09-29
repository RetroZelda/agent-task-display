#! /bin/bash
set -euo pipefail

# Launcher for the task-status board (tasks/server.py): agents push request/task progress to it and
# the dashboard shows it live. Runs it in the foreground or background, stops it, reports on it, and
# installs the task-status Claude Code skill + CLAUDE.md rule from it.

ROOT="$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")"
SERVER="$ROOT/tasks/server.py"
DEFAULT_PORT=8765

usage() {
    cat <<EOF
usage: $0 [--bg | --stop | --status | --install-skill] [--host HOST] [--port PORT] [--db PATH] [--public-url URL]
          [--config PATH] [--tls-port PORT --tls-cert FILE --tls-key FILE]

  (no action)       run the board in the foreground (Ctrl-C stops it)
  --bg              start it in the background; if it is already running, just report it
  --stop            stop it (SIGTERM, then SIGKILL after 5s)
  --status          report on it; exits 0 when it is running, 3 when it is not
  --install-skill   make sure it is running (as --bg does), then install the task-status skill into
                    \${CLAUDE_CONFIG_DIR:-~/.claude}/skills/task-status and the rule into CLAUDE.md

  --host HOST       listen address (default: \$TASKS_HOST or 0.0.0.0)
  --port PORT       port, 1..65535 (default: \$TASKS_PORT or $DEFAULT_PORT)
  --db PATH         SQLite database; relative to the current directory
                    (default: \$TASKS_DB or tasks/data/tasks.db)
  --public-url URL  base of the links the board hands out (default: \$TASKS_PUBLIC_URL, else the
                    LAN address when the firewall lets the LAN in, else the address each client used)
  --config PATH     the board-wide notification settings file; relative to the current directory
                    (default: \$TASKS_CONFIG or settings.json next to the database)
  --tls-port PORT   also serve the board over HTTPS on this port, so browsers on other machines can
                    show notifications (default: \$TASKS_TLS_PORT; off when unset)
  --tls-cert FILE   the PEM certificate (chain) for it (default: \$TASKS_TLS_CERT)
  --tls-key FILE    its PEM private key (default: \$TASKS_TLS_KEY)
                    The three TLS options go together: give all of them or none.

Values can also be given as --port=8765.
EOF
}

die() {
    echo "error: $1" >&2
    shift
    local line
    for line in "$@"; do
        echo "       $line" >&2
    done
    exit 1
}

ACTION=""
set_action() {
    local new="$1"
    if [ -z "$ACTION" ] || [ "$ACTION" = "$new" ]; then
        ACTION="$new"
    elif [ "$ACTION:$new" = "bg:install-skill" ] || [ "$ACTION:$new" = "install-skill:bg" ]; then
        # --install-skill starts the server exactly as --bg does, so `--bg --install-skill` (the
        # documented setup command) is just the longer spelling of --install-skill.
        ACTION="install-skill"
    else
        die "--$ACTION and --$new cannot be combined; give at most one action"
    fi
}

OPT_HOST=""
OPT_PORT=""
OPT_DB=""
OPT_PUBLIC_URL=""
OPT_CONFIG=""
OPT_TLS_PORT=""
OPT_TLS_CERT=""
OPT_TLS_KEY=""
declare -A SEEN=()
while [ $# -gt 0 ]; do
    arg="$1"
    case "$arg" in
        --bg) set_action bg ;;
        --stop) set_action stop ;;
        --status) set_action status ;;
        --install-skill) set_action install-skill ;;
        -h|--help) usage; exit 0 ;;
        --host|--host=*|--port|--port=*|--db|--db=*|--public-url|--public-url=*|--config|--config=*|\
        --tls-port|--tls-port=*|--tls-cert|--tls-cert=*|--tls-key|--tls-key=*)
            name="${arg%%=*}"
            if [ -n "${SEEN[$name]:-}" ]; then
                die "$name given more than once"
            fi
            SEEN[$name]=1
            if [ "$arg" = "$name" ]; then
                # The -* guard catches `--port --bg`, which would otherwise swallow the next flag.
                if [ $# -lt 2 ] || [ -z "$2" ] || [ "${2#-}" != "$2" ]; then
                    die "$name requires a value"
                fi
                value="$2"
                shift
            else
                value="${arg#*=}"
                if [ -z "$value" ]; then
                    die "$name requires a value"
                fi
            fi
            case "$name" in
                --host) OPT_HOST="$value" ;;
                --port) OPT_PORT="$value" ;;
                --db) OPT_DB="$value" ;;
                --public-url) OPT_PUBLIC_URL="$value" ;;
                --config) OPT_CONFIG="$value" ;;
                --tls-port) OPT_TLS_PORT="$value" ;;
                --tls-cert) OPT_TLS_CERT="$value" ;;
                --tls-key) OPT_TLS_KEY="$value" ;;
            esac
            ;;
        -*) die "unknown option: $arg" "run $0 --help for usage" ;;
        *) die "unexpected argument: $arg" "run $0 --help for usage" ;;
    esac
    shift
done

# Flags win over the environment, which wins over the defaults; remember where each value came
# from so an error names the thing to fix.
if [ -n "$OPT_HOST" ]; then HOST="$OPT_HOST"; HOST_FROM="--host"
else HOST="${TASKS_HOST:-0.0.0.0}"; HOST_FROM="TASKS_HOST"; fi
if [ -n "$OPT_PORT" ]; then PORT="$OPT_PORT"; PORT_FROM="--port"
else PORT="${TASKS_PORT:-$DEFAULT_PORT}"; PORT_FROM="TASKS_PORT"; fi
if [ -n "$OPT_DB" ]; then DB="$OPT_DB"; DB_FROM="--db"
else DB="${TASKS_DB:-$ROOT/tasks/data/tasks.db}"; DB_FROM="TASKS_DB"; fi
if [ -n "$OPT_PUBLIC_URL" ]; then PUBLIC_URL="$OPT_PUBLIC_URL"; PUBLIC_URL_FROM="--public-url"
else PUBLIC_URL="${TASKS_PUBLIC_URL:-}"; PUBLIC_URL_FROM="TASKS_PUBLIC_URL"; fi
if [ -n "$OPT_CONFIG" ]; then CONFIG="$OPT_CONFIG"; CONFIG_FROM="--config"
else CONFIG="${TASKS_CONFIG:-}"; CONFIG_FROM="TASKS_CONFIG"; fi
if [ -n "$OPT_TLS_PORT" ]; then TLS_PORT="$OPT_TLS_PORT"; TLS_PORT_FROM="--tls-port"
else TLS_PORT="${TASKS_TLS_PORT:-}"; TLS_PORT_FROM="TASKS_TLS_PORT"; fi
if [ -n "$OPT_TLS_CERT" ]; then TLS_CERT="$OPT_TLS_CERT"; TLS_CERT_FROM="--tls-cert"
else TLS_CERT="${TASKS_TLS_CERT:-}"; TLS_CERT_FROM="TASKS_TLS_CERT"; fi
if [ -n "$OPT_TLS_KEY" ]; then TLS_KEY="$OPT_TLS_KEY"; TLS_KEY_FROM="--tls-key"
else TLS_KEY="${TASKS_TLS_KEY:-}"; TLS_KEY_FROM="TASKS_TLS_KEY"; fi

# $1: a port as given, $2: where it came from. Prints it as a plain number; 10# keeps a leading zero
# from being read as octal, and normalises "08765" to the same pidfile.
valid_port() {
    if ! [[ $1 =~ ^[0-9]{1,5}$ ]] || [ "$((10#$1))" -lt 1 ] || [ "$((10#$1))" -gt 65535 ]; then
        die "$2 must be a port number from 1 to 65535, got '$1'"
    fi
    echo "$((10#$1))"
}
PORT="$(valid_port "$PORT" "$PORT_FROM")" || exit 1

if [ -z "$HOST" ] || [[ $HOST =~ [[:space:]] ]]; then
    die "$HOST_FROM must be an address to listen on, got '$HOST'"
fi

# A relative path is relative to where the caller is, not to this script (which changes directory).
abs_path() {
    local path="$1"
    case "$path" in
        "~") path="$HOME" ;;
        "~/"*) path="$HOME/${path#"~/"}" ;;
    esac
    case "$path" in
        /*) ;;
        *) path="$PWD/$path" ;;
    esac
    realpath -m -s -- "$path"
}
DB="$(abs_path "$DB")"
if [ -d "$DB" ]; then
    die "$DB_FROM must name the database file, but $DB is a directory"
fi
if [ -n "$CONFIG" ]; then
    CONFIG="$(abs_path "$CONFIG")"
    if [ -d "$CONFIG" ]; then
        die "$CONFIG_FROM must name the settings file, but $CONFIG is a directory"
    fi
fi

# The HTTPS listener: all three settings or none. The files are checked only when a server starts.
if [ -n "$TLS_PORT$TLS_CERT$TLS_KEY" ]; then
    missing=()
    [ -n "$TLS_PORT" ] || missing+=("--tls-port")
    [ -n "$TLS_CERT" ] || missing+=("--tls-cert")
    [ -n "$TLS_KEY" ] || missing+=("--tls-key")
    if [ ${#missing[@]} -gt 0 ]; then
        die "the HTTPS listener needs --tls-port, --tls-cert and --tls-key together; missing: ${missing[*]}" \
            "(or set TASKS_TLS_PORT, TASKS_TLS_CERT and TASKS_TLS_KEY; leave all three out for plain http only)"
    fi
    TLS_PORT="$(valid_port "$TLS_PORT" "$TLS_PORT_FROM")" || exit 1
    if [ "$TLS_PORT" = "$PORT" ]; then
        die "$TLS_PORT_FROM must differ from the http port ($PORT)"
    fi
    TLS_CERT="$(abs_path "$TLS_CERT")"
    TLS_KEY="$(abs_path "$TLS_KEY")"
fi

if [ -n "$PUBLIC_URL" ]; then
    while [ "${PUBLIC_URL%/}" != "$PUBLIC_URL" ]; do
        PUBLIC_URL="${PUBLIC_URL%/}"
    done
    # The server bakes this into the scripts it serves and rejects the same characters.
    if ! [[ $PUBLIC_URL =~ ^https?://[^/]+ ]] || [[ $PUBLIC_URL == *[[:space:]\"\'\\\`\<\>{}]* ]]; then
        die "$PUBLIC_URL_FROM must look like http://HOST:PORT, got '$PUBLIC_URL'"
    fi
fi

if [ ! -f "$SERVER" ]; then
    die "$SERVER not found; is $ROOT the right checkout?"
fi
if ! command -v python3 >/dev/null 2>&1; then
    die "python3 not found; the tasks server needs Python 3.10 or newer"
fi
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    die "the tasks server needs Python 3.10 or newer, but python3 is $(python3 -c 'import platform; print(platform.python_version())' 2>/dev/null || echo 'unknown')"
fi
if ! command -v curl >/dev/null 2>&1; then
    die "curl not found; it is used to health-check the server and download the skill"
fi

CONFIG_DIR="${CLAUDE_CONFIG_DIR:-${HOME:?HOME is not set}/.claude}"
case "$CONFIG_DIR" in
    /*) ;;
    *) CONFIG_DIR="$PWD/$CONFIG_DIR" ;;
esac
SKILL_DIR="$CONFIG_DIR/skills/task-status"

DATA_DIR="$ROOT/tasks/data"
PIDFILE="$DATA_DIR/server-$PORT.pid"
LOG_DIR="$ROOT/.logs"
LOG_FILE="$LOG_DIR/tasks_server.log"

# Where this machine reaches a server listening on $1: loopback for a wildcard bind (the server
# listens dual-stack there, or IPv4 only without IPv6, so 127.0.0.1 answers either way), else the
# bound address itself.
LOCAL_URL=""
set_local_url() {
    local connect
    case "$1" in
        0.0.0.0|::|"[::]"|"*"|localhost) connect="127.0.0.1" ;;
        *:*) connect="${1#[}"; connect="[${connect%]}]" ;;
        *) connect="$1" ;;
    esac
    LOCAL_URL="http://$connect:$PORT"
}
set_local_url "$HOST"

# How to rerun this script with the same port, for the hints printed below.
SELF="$0"
if [ "$PORT" != "$DEFAULT_PORT" ]; then
    SELF_PORT=" --port $PORT"
else
    SELF_PORT=""
fi

cd "$ROOT"

TMP_FILES=()
cleanup() {
    if [ ${#TMP_FILES[@]} -gt 0 ]; then
        rm -f -- "${TMP_FILES[@]}"
    fi
}
trap cleanup EXIT

# curl never goes through a proxy here: every URL is this machine's own server.
CURL=(curl --noproxy '*')

# $1: seconds to wait for the answer (default 2).
health_json() {
    "${CURL[@]}" -fsS --connect-timeout 1 --max-time "${1:-2}" "$LOCAL_URL/api/health" 2>/dev/null
}

is_tasks_health() {
    local re='"service": *"tasks"'
    [[ $1 =~ $re ]]
}

# True when $1 is a live tasks/server.py. A zombie has an empty cmdline, so it does not count.
is_tasks_pid() {
    local pid="$1" cmdline
    if ! [[ $pid =~ ^[0-9]{1,9}$ ]] || [ "$pid" -le 1 ]; then
        return 1
    fi
    kill -0 "$pid" 2>/dev/null || return 1
    cmdline="$(tr '\0' ' ' 2>/dev/null < "/proc/$pid/cmdline")" || return 1
    [[ $cmdline == *tasks/server.py* ]]
}

# Prints the pid of the tasks server on $PORT: the pidfile first, then the pid /api/health reports
# (a server started by hand, or whose pidfile was lost). Either way the pid must really be a
# tasks/server.py, so a recycled pid is never signalled. A stale pidfile is removed.
running_pid() {
    local pid="" health re='"pid": *([0-9]+)'
    if [ -f "$PIDFILE" ]; then
        pid="$(head -c 32 "$PIDFILE" 2>/dev/null | tr -d '[:space:]')" || pid=""
        if is_tasks_pid "$pid"; then
            echo "$pid"
            return 0
        fi
        # Only if it still holds what was read: a server starting right now may have replaced it.
        if [ "$(head -c 32 "$PIDFILE" 2>/dev/null | tr -d '[:space:]')" = "$pid" ]; then
            rm -f "$PIDFILE"
        fi
    fi
    if health="$(health_json)" && is_tasks_health "$health" && [[ $health =~ $re ]]; then
        pid="${BASH_REMATCH[1]}"
        if is_tasks_pid "$pid"; then
            echo "$pid"
            return 0
        fi
    fi
    return 1
}

# $1: the port to look at.
port_listeners() {
    if command -v ss >/dev/null 2>&1; then
        ss -Hltnp "sport = :$1" 2>/dev/null || true
    else
        python3 - "$1" <<'PY' || true
import socket, sys
try:
    socket.create_connection(('127.0.0.1', int(sys.argv[1])), timeout=1).close()
    print('something accepts connections on 127.0.0.1:%s' % sys.argv[1])
except OSError:
    pass
PY
    fi
}

# Checks the http port and, when the HTTPS listener is on, its port too.
refuse_busy_port() {
    local listeners port flag
    for port in "$PORT" ${TLS_PORT:+"$TLS_PORT"}; do
        flag="--port"
        [ "$port" = "$PORT" ] || flag="--tls-port"
        listeners="$(port_listeners "$port")"
        if [ -n "$listeners" ]; then
            echo "error: port $port is already in use by another program:" >&2
            printf '         %s\n' "$listeners" >&2
            echo "       pick another port with $flag, or stop that program" >&2
            exit 1
        fi
    done
}

# Reads the listen address, database, public URL, settings file and HTTPS port the running server
# was actually started with, so reports describe it rather than this invocation's flags. RUN_CONFIG
# is empty when it was not given (the server's default: settings.json next to the database).
RUN_HOST=""
RUN_DB=""
RUN_PUBLIC_URL=""
RUN_CONFIG=""
RUN_TLS_PORT=""
read_running_config() {
    local pid="$1" i=0 arg next
    local -a argv=()
    RUN_HOST="$HOST"
    RUN_DB="$DB"
    RUN_PUBLIC_URL=""
    RUN_CONFIG=""
    RUN_TLS_PORT=""
    mapfile -d '' -t argv 2>/dev/null < "/proc/$pid/cmdline" || return 0
    while [ $i -lt ${#argv[@]} ]; do
        arg="${argv[$i]}"
        next="${argv[$((i + 1))]:-}"
        case "$arg" in
            --host) RUN_HOST="$next"; i=$((i + 1)) ;;
            --host=*) RUN_HOST="${arg#*=}" ;;
            --db) RUN_DB="$next"; i=$((i + 1)) ;;
            --db=*) RUN_DB="${arg#*=}" ;;
            --public-url) RUN_PUBLIC_URL="$next"; i=$((i + 1)) ;;
            --public-url=*) RUN_PUBLIC_URL="${arg#*=}" ;;
            --config) RUN_CONFIG="$next"; i=$((i + 1)) ;;
            --config=*) RUN_CONFIG="${arg#*=}" ;;
            --tls-port) RUN_TLS_PORT="$next"; i=$((i + 1)) ;;
            --tls-port=*) RUN_TLS_PORT="${arg#*=}" ;;
        esac
        i=$((i + 1))
    done
    RUN_PUBLIC_URL="${RUN_PUBLIC_URL%/}"
    set_local_url "$RUN_HOST"
}

# Where the running server $1 writes its log: its stderr, which --bg points at $LOG_FILE.
log_target() {
    local target
    target="$(readlink "/proc/$1/fd/2" 2>/dev/null)" || target=""
    case "$target" in
        "") echo "$LOG_FILE" ;;
        /dev/pts/*|/dev/tty*) echo "the terminal it was started in ($target)" ;;
        /*) echo "$target" ;;
        *) echo "not a file ($target)" ;;
    esac
}

warn_running_differs() {
    local pid="$1"
    if [ -n "$OPT_DB" ] && [ "$RUN_DB" != "$DB" ]; then
        echo "note: the running server (pid $pid) uses --db $RUN_DB, not $DB; run $SELF --stop$SELF_PORT first to change it" >&2
    fi
    if [ -n "$OPT_HOST" ] && [ "$RUN_HOST" != "$HOST" ]; then
        echo "note: the running server (pid $pid) listens on $RUN_HOST, not $HOST; run $SELF --stop$SELF_PORT first to change it" >&2
    fi
    if [ -n "$OPT_PUBLIC_URL" ] && [ "$RUN_PUBLIC_URL" != "$PUBLIC_URL" ]; then
        echo "note: the running server (pid $pid) uses --public-url ${RUN_PUBLIC_URL:-(none)}, not $PUBLIC_URL; run $SELF --stop$SELF_PORT first to change it" >&2
    fi
    if [ -n "$OPT_CONFIG" ] && [ "$RUN_CONFIG" != "$CONFIG" ]; then
        echo "note: the running server (pid $pid) uses --config ${RUN_CONFIG:-(the default)}, not $CONFIG; run $SELF --stop$SELF_PORT first to change it" >&2
    fi
    if [ -n "$OPT_TLS_PORT" ] && [ "$RUN_TLS_PORT" != "$TLS_PORT" ]; then
        echo "note: the running server (pid $pid) uses --tls-port ${RUN_TLS_PORT:-(none)}, not $TLS_PORT; run $SELF --stop$SELF_PORT first to change it" >&2
    fi
}

# The address other machines reach this one at: the source address of the default route, never a
# container bridge such as docker0.
lan_ip() {
    local out ip="" dev="" bad
    if command -v ip >/dev/null 2>&1 && out="$(ip -4 route get 1.1.1.1 2>/dev/null)"; then
        ip="$(awk '{ for (i = 1; i < NF; i++) if ($i == "src") { print $(i + 1); exit } }' <<< "$out")"
        dev="$(awk '{ for (i = 1; i < NF; i++) if ($i == "dev") { print $(i + 1); exit } }' <<< "$out")"
    fi
    case "$dev" in
        docker*|br-*|veth*|virbr*) ip="" ;;
    esac
    if [ -z "$ip" ]; then
        # Connecting a UDP socket sends nothing; it only makes the kernel pick the source address.
        ip="$(python3 -c '
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("1.1.1.1", 80))
    print(s.getsockname()[0])
except OSError:
    pass
' 2>/dev/null)" || ip=""
    fi
    if [ -n "$ip" ] && command -v ip >/dev/null 2>&1; then
        for bad in $(ip -4 -o addr show dev docker0 2>/dev/null | awk '{ sub(/\/.*/, "", $4); print $4 }'); do
            if [ "$ip" = "$bad" ]; then
                ip=""
            fi
        done
    fi
    if ! [[ $ip =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || [[ $ip == 127.* ]]; then
        ip=""
    fi
    echo "$ip"
}

# 0: ufw lets the LAN reach $PORT (or is not filtering at all); 1: it does not; 2: cannot tell.
# IPv4 by default; with $1 = 6 the same question for IPv6 (/etc/ufw/user6.rules). $2: another port
# to ask about (the HTTPS one) instead of $PORT.
lan_firewall_open() {
    local v6="${1:-}" port="${2:-$PORT}"
    if ! command -v systemctl >/dev/null 2>&1 || ! systemctl is-active --quiet ufw 2>/dev/null; then
        return 0
    fi
    # The service can be active while ufw itself is disabled, and then nothing is filtered.
    if [ -r /etc/ufw/ufw.conf ] && ! grep -Eq '^[[:space:]]*ENABLED="?yes"?[[:space:]]*$' /etc/ufw/ufw.conf; then
        return 0
    fi
    if [ -r /etc/default/ufw ] && grep -Eq '^[[:space:]]*DEFAULT_INPUT_POLICY="?ACCEPT' /etc/default/ufw; then
        return 0
    fi
    # IPV6=no: ufw leaves IPv6 traffic alone.
    if [ -n "$v6" ] && [ -r /etc/default/ufw ] && grep -Eq '^[[:space:]]*IPV6="?no' /etc/default/ufw; then
        return 0
    fi
    if [ ! -r "/etc/ufw/user$v6.rules" ]; then
        return 2
    fi
    # Rules look like `-A ufw-user-input -p tcp --dport 8765 -s 192.168.1.0/24 -j ACCEPT` (ufw6-user-*
    # in user6.rules); also accept `ufw limit`, multiport lists and ranges. A udp-only rule does not
    # open a tcp port.
    awk -v port="$port" -v chain="ufw$v6-user" '
        $1 == "-A" && $2 == chain "-input" && $0 ~ (" -j (ACCEPT|" chain "-limit-accept)( |$)") && !/ -p udp / {
            for (i = 3; i < NF; i++) {
                if ($i != "--dport" && $i != "--dports") continue
                n = split($(i + 1), parts, ",")
                for (j = 1; j <= n; j++) {
                    if (split(parts[j], range, ":") == 2) {
                        if (port + 0 >= range[1] + 0 && port + 0 <= range[2] + 0) found = 1
                    } else if (parts[j] + 0 == port + 0) {
                        found = 1
                    }
                }
            }
        }
        END { exit (found ? 0 : 1) }
    ' "/etc/ufw/user$v6.rules"
}

# lan_firewall_open "$@" as open | blocked | unknown.
firewall_state() {
    local rc=0
    lan_firewall_open "$@" || rc=$?
    case "$rc" in
        0) echo "open" ;;
        2) echo "unknown" ;;
        *) echo "blocked" ;;
    esac
}

# Sets LAN_IP, LAN_NET and LAN_STATE (open | blocked | unknown | loopback | specific | none) for
# a server listening on $1, and LAN6_STATE (open | blocked | unknown; empty unless $1 is a wildcard,
# which the server listens on dual-stack) for reaching it by this machine's name over IPv6.
LAN_IP=""
LAN_NET=""
LAN_STATE=""
LAN6_STATE=""
detect_lan() {
    local host="$1"
    LAN_IP=""
    LAN_NET=""
    LAN6_STATE=""
    case "$host" in
        127.*|localhost|::1|"[::1]") LAN_STATE="loopback"; return 0 ;;
        0.0.0.0|::|"[::]"|"*")
            LAN_IP="$(lan_ip)"
            LAN6_STATE="$(firewall_state 6)"
            ;;
        *)
            if [[ $host =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]]; then
                LAN_IP="$host"
            else
                LAN_STATE="specific"
                return 0
            fi
            ;;
    esac
    if [ -z "$LAN_IP" ]; then
        LAN_STATE="none"
        return 0
    fi
    LAN_NET="${LAN_IP%.*}.0/24"
    LAN_STATE="$(firewall_state)"
}

print_box() {
    local line width=0
    for line in "$@"; do
        if [ ${#line} -gt $width ]; then
            width=${#line}
        fi
    done
    local rule
    rule="$(printf '%*s' $((width + 2)) '' | tr ' ' '-')"
    echo "  +$rule+"
    for line in "$@"; do
        printf '  | %-*s |\n' "$width" "$line"
    done
    echo "  +$rule+"
}

lan_line() {
    case "$LAN_STATE" in
        open) echo "http://$LAN_IP:$PORT/" ;;
        blocked) echo "http://$LAN_IP:$PORT/  (blocked by ufw, see below)" ;;
        unknown) echo "http://$LAN_IP:$PORT/  (may be blocked: cannot read /etc/ufw/user.rules)" ;;
        loopback) echo "off (listening on $1 only)" ;;
        specific) echo "listening on $1 only" ;;
        *) echo "unknown (no LAN address found)" ;;
    esac
}

# The board by this machine's name, which a PC may resolve to its IPv4 or its IPv6 address.
hostname_line() {
    case "$LAN6_STATE" in
        open) echo "http://$HOSTNAME:$PORT/" ;;
        blocked) echo "http://$HOSTNAME:$PORT/  (IPv6 blocked by ufw, see below)" ;;
        unknown) echo "http://$HOSTNAME:$PORT/  (IPv6 may be blocked: cannot read /etc/ufw/user6.rules)" ;;
    esac
}

# The board's HTTPS listener, for the banner: its port (empty when it is off) and, like LAN_STATE, whether
# the LAN can reach that port (open | blocked | unknown; empty when there is no LAN address to ask about).
TLS_SHOWN=""
TLS_LAN_STATE=""
set_tls_state() {
    TLS_SHOWN="$1"
    TLS_LAN_STATE=""
    if [ -n "$TLS_SHOWN" ] && [ -n "$LAN_IP" ]; then
        case "$LAN_STATE" in
            open|blocked|unknown) TLS_LAN_STATE="$(firewall_state "" "$TLS_SHOWN")" ;;
        esac
    fi
}

https_line() {
    local local_base="${LOCAL_URL%:*}"
    case "$TLS_LAN_STATE" in
        open) echo "https://$LAN_IP:$TLS_SHOWN/" ;;
        blocked) echo "https://$LAN_IP:$TLS_SHOWN/  (blocked by ufw, see below)" ;;
        unknown) echo "https://$LAN_IP:$TLS_SHOWN/  (may be blocked: cannot read /etc/ufw/user.rules)" ;;
        *) echo "https://${local_base#http://}:$TLS_SHOWN/" ;;
    esac
}

lan_warning() {
    local public="$1" lines=()
    case "$LAN_STATE" in
        blocked) lines+=("ufw is active and has no rule letting the LAN reach port $PORT, so other"
                         "machines and remote agents cannot reach this board.") ;;
        unknown) lines+=("ufw is active but /etc/ufw/user.rules is unreadable, so port $PORT may be"
                         "closed to other machines and remote agents.") ;;
    esac
    if [ ${#lines[@]} -gt 0 ]; then
        lines+=("To let your LAN in, run this yourself:"
                ""
                "  sudo ufw allow from $LAN_NET to any port $PORT proto tcp"
                "")
        if [ -z "$public" ]; then
            lines+=("Links stay on 127.0.0.1 meanwhile (VS Code Remote-SSH forwards them);"
                    "restart the board afterwards to hand out LAN links.")
        fi
    fi
    if [ "$LAN6_STATE" = blocked ] || [ "$LAN6_STATE" = unknown ]; then
        if [ ${#lines[@]} -gt 0 ]; then
            [ -z "${lines[-1]}" ] || lines+=("")
        elif [ "$LAN6_STATE" = blocked ]; then
            lines+=("ufw is active and has no IPv6 rule for port $PORT.")
        else
            lines+=("ufw is active but /etc/ufw/user6.rules is unreadable.")
        fi
        lines+=("Optional, for hostname access over IPv6 (http://$HOSTNAME:$PORT/ from a PC"
                "that resolves the name to this machine's fe80:: address, as Windows can):"
                ""
                "  sudo ufw allow from fe80::/10 to any port $PORT proto tcp")
    fi
    if [ "$TLS_LAN_STATE" = blocked ] || [ "$TLS_LAN_STATE" = unknown ]; then
        if [ ${#lines[@]} -gt 0 ] && [ -n "${lines[-1]}" ]; then
            lines+=("")
        fi
        if [ "$TLS_LAN_STATE" = blocked ]; then
            lines+=("ufw is active and has no rule letting the LAN reach the HTTPS port $TLS_SHOWN.")
        else
            lines+=("ufw is active but /etc/ufw/user.rules is unreadable, so the HTTPS port $TLS_SHOWN"
                    "may be closed to other machines.")
        fi
        lines+=("For https (browser notifications) from other machines, run this yourself:"
                ""
                "  sudo ufw allow from $LAN_NET to any port $TLS_SHOWN proto tcp")
    fi
    if [ ${#lines[@]} -eq 0 ]; then
        return 0
    fi
    echo
    print_box "${lines[@]}"
}

skill_state() {
    local baked
    if [ -L "$SKILL_DIR" ]; then
        echo "$SKILL_DIR is a symlink (managed elsewhere; --install-skill will not write through it)"
    elif [ -f "$SKILL_DIR/SKILL.md" ] && [ -x "$SKILL_DIR/taskctl" ] && [ -f "$SKILL_DIR/taskctl.py" ]; then
        baked="$(sed -n 's/^BAKED_URL = "\(.*\)"[[:space:]]*$/\1/p;T;q' "$SKILL_DIR/taskctl.py" 2>/dev/null)" || baked=""
        if [ -n "$baked" ] && [[ $baked != *":$PORT" ]]; then
            echo "installed in $SKILL_DIR, but it reports to $baked -> $SELF --install-skill$SELF_PORT"
        else
            echo "installed in $SKILL_DIR (reports to ${baked:-an unknown URL})"
        fi
    elif [ -e "$SKILL_DIR" ]; then
        echo "incomplete in $SKILL_DIR -> $SELF --install-skill$SELF_PORT"
    else
        echo "missing -> $SELF --install-skill$SELF_PORT"
    fi
}

rule_state() {
    local file="$CONFIG_DIR/CLAUDE.md" text=""
    if [ -f "$file" ]; then
        text="$(cat -- "$file" 2>/dev/null)" || text=""
    fi
    if [[ $text == *"<!-- task-status:begin -->"* ]]; then
        echo "present in $file"
    else
        echo "missing from $file (--install-skill adds it)"
    fi
}

# $1: a JSON object, $2: a key. Prints its value, or nothing when it is missing or null.
json_field() {
    python3 -c '
import json, sys
try:
    value = json.loads(sys.argv[1]).get(sys.argv[2])
except (ValueError, AttributeError):
    value = None
if value is not None:
    print(value)' "$1" "$2" 2>/dev/null || true
}

# $1: the settings file, $2: the running board's /api/settings answer (empty when there is none).
settings_line() {
    local file="$1" error=""
    if [ -n "$2" ]; then
        error="$(json_field "$2" error)"
    fi
    if [ -n "$error" ]; then
        echo "$file (not usable, so the defaults apply: $error)"
    elif [ -e "$file" ]; then
        echo "$file"
    else
        echo "$file (not created yet: the defaults apply until someone saves the settings)"
    fi
}

# $1: the /api/health answer (empty when there was none).
health_summary() {
    local json="$1"
    if [ -z "$json" ]; then
        echo "no answer from $LOCAL_URL/api/health"
        return 0
    fi
    python3 - "$json" <<'PY' || echo "$json"
import json, sys
h = json.loads(sys.argv[1])
up = int(max(0, (h.get('now') or 0) - (h.get('started_at') or 0)))
d, rest = divmod(up, 86400)
parts = [(d, 'd'), (rest // 3600, 'h'), (rest % 3600 // 60, 'm'), (rest % 60, 's')]
while len(parts) > 1 and parts[0][0] == 0:
    parts.pop(0)
uptime = ' '.join('%d%s' % part for part in parts[:2])
running = h.get('requests_running')
print('ok, version %s, up %s, %s running request%s'
      % (h.get('version'), uptime, running, '' if running == 1 else 's'))
PY
}

# $1: headline, $2: pid, $3: fg | bg, then the running server's host, database, public URL, settings
# file (empty: the default, next to the database) and HTTPS port (empty: none). A running server's
# own /api/health has the last word on the settings file and the HTTPS port.
banner() {
    local headline="$1" pid="$2" mode="$3" host="$4" db="$5" public="$6" config="${7:-}" tls="${8:-}"
    local agent_base="" health="" settings="" value
    if [ "$mode" != fg ]; then
        if ! health="$(health_json)" || ! is_tasks_health "$health"; then
            health=""
        fi
    fi
    if [ -n "$health" ]; then
        value="$(json_field "$health" tls_port)"
        if [ -n "$value" ]; then tls="$value"; fi
        value="$(json_field "$health" config_path)"
        if [ -n "$value" ]; then config="$value"; fi
        settings="$("${CURL[@]}" -fsS --connect-timeout 1 --max-time 2 "$LOCAL_URL/api/settings" 2>/dev/null)" || settings=""
    fi
    if [ -z "$config" ]; then
        config="$(dirname -- "$db")/settings.json"
    fi
    set_tls_state "$tls"
    echo
    echo "tasks board: $headline (pid $pid, port $PORT)"
    echo "  dashboard   $LOCAL_URL/"
    echo "  LAN         $(lan_line "$host")"
    if [ -n "$LAN6_STATE" ]; then
        echo "  hostname    $(hostname_line)"
    fi
    if [ -n "$TLS_SHOWN" ]; then
        echo "  https       $(https_line)"
    elif [ "$LAN_STATE" != loopback ]; then
        # A browser on this machine (localhost) can notify without it; one on another machine cannot.
        echo "  https       off (browsers on other machines need it to show notifications; see --help)"
    fi
    if [ "$mode" != fg ]; then
        echo "  health      $(health_summary "$health")"
    fi
    echo "  database    $db"
    echo "  settings    $(settings_line "$config" "$settings")"
    if [ "$mode" = fg ]; then
        echo "  log         this terminal"
    else
        echo "  log         $(log_target "$pid")"
    fi
    echo "  skill       $(skill_state)"
    echo "  rule        $(rule_state)"
    if [ -n "$public" ]; then
        agent_base="$public"
    elif [ -n "$LAN_IP" ]; then
        agent_base="http://$LAN_IP:$PORT"
    fi
    if [ -n "$agent_base" ]; then
        echo
        echo "  For an agent on another machine, give it this line:"
        printf "    Run \`curl -fsS --noproxy '*' --connect-timeout 5 %s/api/usage\` and follow it.\n" "$agent_base"
    fi
    lan_warning "$public"
    echo
}

# The --public-url a new server gets: the one given, else the LAN address when the LAN can reach it.
server_public_url() {
    if [ -n "$PUBLIC_URL" ]; then
        echo "$PUBLIC_URL"
    elif [ "$LAN_STATE" = open ]; then
        echo "http://$LAN_IP:$PORT"
    fi
}

SERVER_ARGS=()
build_server_args() {
    local public
    public="$(server_public_url)"
    SERVER_ARGS=(--host "$HOST" --port "$PORT" --db "$DB" --pidfile "$PIDFILE")
    if [ -n "$public" ]; then
        SERVER_ARGS+=(--public-url "$public")
    fi
    if [ -n "$CONFIG" ]; then
        SERVER_ARGS+=(--config "$CONFIG")
    fi
    if [ -n "$TLS_PORT" ]; then
        if [ ! -f "$TLS_CERT" ] || [ ! -r "$TLS_CERT" ]; then
            die "$TLS_CERT_FROM: cannot read the certificate $TLS_CERT"
        fi
        if [ ! -f "$TLS_KEY" ] || [ ! -r "$TLS_KEY" ]; then
            die "$TLS_KEY_FROM: cannot read the private key $TLS_KEY"
        fi
        SERVER_ARGS+=(--tls-port "$TLS_PORT" --tls-cert "$TLS_CERT" --tls-key "$TLS_KEY")
    fi
}

# Starts the server in the background unless it is already running. Sets PID and STARTED.
PID=""
STARTED=false
start_bg() {
    local child rc json deadline re='"pid": *([0-9]+)'
    if PID="$(running_pid)"; then
        STARTED=false
        return 0
    fi
    refuse_busy_port
    build_server_args
    mkdir -p "$DATA_DIR" "$LOG_DIR"
    echo "=== $(date '+%Y-%m-%d %H:%M:%S') tasks.sh: python3 $SERVER ${SERVER_ARGS[*]}" >> "$LOG_FILE"
    # setsid puts it in its own session, so it outlives this shell and the terminal (or agent
    # Bash tool) that ran it; with every stream redirected nothing waits on it either.
    setsid nohup python3 "$SERVER" "${SERVER_ARGS[@]}" >> "$LOG_FILE" 2>&1 < /dev/null &
    child=$!
    PID=""
    # A deadline in EPOCHREALTIME microseconds rather than a count of tries, because one health
    # check against an address that drops packets takes its whole timeout.
    deadline=$(( ${EPOCHREALTIME//[!0-9]/} + 5000000 ))
    while [ "${EPOCHREALTIME//[!0-9]/}" -lt "$deadline" ]; do
        if ! kill -0 "$child" 2>/dev/null; then
            # setsid exits 0 after forking when it has to; a non-zero status is the server failing.
            rc=0
            wait "$child" || rc=$?
            if [ "$rc" -ne 0 ]; then
                break
            fi
        fi
        if json="$(health_json 1)" && is_tasks_health "$json" && [[ $json =~ $re ]]; then
            PID="${BASH_REMATCH[1]}"
            break
        fi
        sleep 0.1
    done
    if [ -z "$PID" ]; then
        if kill -0 "$child" 2>/dev/null; then
            kill -TERM "$child" 2>/dev/null || true
        fi
        echo "error: the tasks server did not answer $LOCAL_URL/api/health within 5s" >&2
        echo "       last lines of $LOG_FILE:" >&2
        tail -n 20 "$LOG_FILE" | sed 's/^/         /' >&2
        exit 1
    fi
    STARTED=true
}

stop_server() {
    local pid i
    if ! pid="$(running_pid)"; then
        echo "tasks board: not running on port $PORT"
        return 0
    fi
    kill -TERM "$pid" 2>/dev/null || true
    for i in $(seq 50); do
        is_tasks_pid "$pid" || break
        sleep 0.1
    done
    if is_tasks_pid "$pid"; then
        echo "tasks.sh: pid $pid did not exit within 5s of SIGTERM; sending SIGKILL" >&2
        kill -KILL "$pid" 2>/dev/null || true
        for i in $(seq 30); do
            is_tasks_pid "$pid" || break
            sleep 0.1
        done
        if is_tasks_pid "$pid"; then
            die "pid $pid is still running after SIGKILL"
        fi
    fi
    # A killed server cannot remove its own pidfile.
    if [ -f "$PIDFILE" ] && [ "$(head -c 32 "$PIDFILE" 2>/dev/null | tr -d '[:space:]')" = "$pid" ]; then
        rm -f "$PIDFILE"
    fi
    echo "tasks board: stopped (pid $pid, port $PORT)"
}

# $1: URL, $2: output file. Prints the server's own error (e.g. "template missing") on failure.
fetch() {
    local url="$1" out="$2" code
    if ! code="$("${CURL[@]}" -sS --connect-timeout 5 --max-time 30 -o "$out" -w '%{http_code}' "$url")"; then
        echo "error: cannot download $url" >&2
        return 1
    fi
    if [ "$code" != 200 ]; then
        echo "error: $url answered HTTP $code: $(head -c 300 "$out" 2>/dev/null)" >&2
        return 1
    fi
}

install_skill() {
    local dir="$SKILL_DIR" name tmp i
    local -a names=(SKILL.md taskctl taskctl.py) tmps=()
    if [ -L "$dir" ]; then
        die "$dir is a symlink; refusing to install through it" \
            "remove the link, or update whatever it points at yourself"
    fi
    if [ -e "$dir" ] && [ ! -d "$dir" ]; then
        die "$dir exists and is not a directory"
    fi
    mkdir -p "$dir"
    # Download everything next to its destination first, so a failure leaves the old install as it was.
    for name in "${names[@]}"; do
        tmp="$(mktemp "$dir/.$name.XXXXXX")"
        TMP_FILES+=("$tmp")
        tmps+=("$tmp")
        if ! fetch "$LOCAL_URL/api/skill/$name" "$tmp"; then
            die "skill not installed; $dir is unchanged"
        fi
    done
    if [ ! -s "${tmps[0]}" ] || [ "$(head -c 2 "${tmps[1]}")" != "#!" ] \
            || ! grep -q '^BAKED_URL = "http' "${tmps[2]}"; then
        die "$LOCAL_URL/api/skill/ served something that is not the skill; $dir is unchanged"
    fi
    chmod 644 "${tmps[0]}"
    chmod 755 "${tmps[1]}" "${tmps[2]}"
    for i in 0 1 2; do
        mv -f -- "${tmps[$i]}" "$dir/${names[$i]}"
    done
    TMP_FILES=()
    echo ">>> installed the task-status skill in $dir (from $LOCAL_URL)"

    echo ">>> checking the installed taskctl"
    # Its stdout is just the URL again; the "ok: board at URL" line on stderr says it all.
    if ! "$dir/taskctl" --strict ping >/dev/null; then
        if [ -n "${TASKS_URL:-}" ]; then
            die "the installed $dir/taskctl cannot reach the board" \
                "it uses TASKS_URL=$TASKS_URL from your environment; unset it or point it at $LOCAL_URL"
        fi
        die "the installed $dir/taskctl cannot reach the board at $LOCAL_URL"
    fi
    echo ">>> installing the rule block in $CONFIG_DIR/CLAUDE.md"
    # Passed on absolute: this script has changed directory, so a relative one would now miss.
    if ! CLAUDE_CONFIG_DIR="$CONFIG_DIR" "$dir/taskctl" --url "$LOCAL_URL" install-rule; then
        die "the skill is installed, but the CLAUDE.md rule is not"
    fi
}

print_permission_rules() {
    echo "Recommended permission rules for $CONFIG_DIR/settings.json. Add them yourself (into the"
    echo "permissions.allow and permissions.ask lists, if you already have them); this script never"
    echo "edits settings.json:"
    # Each string goes through json.dumps, so a path with a quote or a backslash still pastes as valid JSON.
    python3 - "$SKILL_DIR/taskctl" <<'PY'
import json, sys
taskctl = sys.argv[1]
ask = ['Bash(%s %s)' % (taskctl, rule) for rule in ('run *', '--* run *', 'update*', '--* update*', 'install-rule*', '--* install-rule*')]
print('{\n  "permissions": {\n    "allow": [%s],\n    "ask": [\n      %s\n    ]\n  }\n}'
      % (json.dumps('Bash(%s *)' % taskctl), ',\n      '.join(json.dumps(rule) for rule in ask)))
PY
    echo "The allow rule covers every report (so subagents are not refused); the ask rules win over it,"
    echo "so 'taskctl run' (runs an arbitrary command), 'taskctl update' (replaces the skill's code) and"
    echo "'taskctl install-rule' (rewrites CLAUDE.md) still ask you each time, also with an option such as"
    echo "--url in front of them."
    echo "Start a new Claude Code session (or /reload-skills) to load the skill."
}

case "$ACTION" in
    stop)
        stop_server
        ;;
    status)
        if pid="$(running_pid)"; then
            read_running_config "$pid"
            detect_lan "$RUN_HOST"
            banner "running" "$pid" bg "$RUN_HOST" "$RUN_DB" "$RUN_PUBLIC_URL" "$RUN_CONFIG" "$RUN_TLS_PORT"
            exit 0
        fi
        detect_lan "$HOST"
        echo "tasks board: not running on port $PORT"
        echo "  LAN         $(lan_line "$HOST")"
        if [ -n "$LAN6_STATE" ]; then
            echo "  hostname    $(hostname_line)"
        fi
        echo "  skill       $(skill_state)"
        echo "  rule        $(rule_state)"
        echo "  start it    $SELF --bg$SELF_PORT"
        lan_warning "$PUBLIC_URL"
        exit 3
        ;;
    bg|install-skill)
        detect_lan "$HOST"
        start_bg
        read_running_config "$PID"
        if ! $STARTED; then
            warn_running_differs "$PID"
            # The running server's own listen address decides what the LAN can reach.
            detect_lan "$RUN_HOST"
        fi
        if [ "$ACTION" = install-skill ]; then
            install_skill
        fi
        if $STARTED; then
            banner "started" "$PID" bg "$RUN_HOST" "$RUN_DB" "$RUN_PUBLIC_URL" "$RUN_CONFIG" "$RUN_TLS_PORT"
        else
            banner "already running" "$PID" bg "$RUN_HOST" "$RUN_DB" "$RUN_PUBLIC_URL" "$RUN_CONFIG" "$RUN_TLS_PORT"
        fi
        if [ "$ACTION" = install-skill ]; then
            print_permission_rules
        fi
        ;;
    "")
        if pid="$(running_pid)"; then
            die "the tasks board is already running on port $PORT (pid $pid)" \
                "stop it first with: $SELF --stop$SELF_PORT"
        fi
        refuse_busy_port
        detect_lan "$HOST"
        build_server_args
        mkdir -p "$DATA_DIR"
        public="$(server_public_url)"
        # exec keeps this pid, so the banner can name it before the server starts.
        banner "starting in the foreground, Ctrl-C stops it" "$$" fg "$HOST" "$DB" "$public" "$CONFIG" "$TLS_PORT"
        exec python3 "$SERVER" "${SERVER_ARGS[@]}"
        ;;
esac
