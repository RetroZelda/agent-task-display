#! /bin/bash
# tasks.sh lifecycle: arguments, --bg / --stop / --status / foreground, the banner, stale pidfiles, a
# busy port, start failures, relative paths and env, the LAN/firewall paths (fake ufw, fake ip), the
# settings file and HTTPS options (--config, --tls-*; a self-signed certificate when openssl is
# installed) and the SIGKILL path. Runs a copy of the checkout in $TMP on free ports; never touches
# the real one.
. "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"

command -v curl >/dev/null 2>&1 || skip_suite "curl not installed (tasks.sh needs it)"
command -v setsid >/dev/null 2>&1 || skip_suite "setsid not installed (tasks.sh --bg needs it)"
[ -d /proc ] || skip_suite "no /proc (tasks.sh identifies its server through /proc)"
command -v timeout >/dev/null 2>&1 || skip_suite "timeout (coreutils) not installed"

make_fakes
V4_OTHER="-A ufw-user-input -p tcp --dport 22 -j ACCEPT"
V6_OTHER="-A ufw6-user-input -p tcp --dport 22 -j ACCEPT"
fake_ufw "$V4_OTHER" "$V6_OTHER"   # ufw active, the board's port closed to the LAN (both stacks)
# A sudo that records any call, so "never runs sudo" is checked, not assumed.
printf '#!/bin/sh\necho "sudo $*" >> "%s/sudo.log"\nexit 1\n' "$TMP" > "$FAKE/bin/sudo"
chmod +x "$FAKE/bin/sudo"

R="$TMP/root"
make_root "$R"
T="$R/tasks.sh"
P="$(free_port)"
Q="$(free_port)"
P2="$(free_port)"
DB="$TMP/launch.db"
PIDF="$R/tasks/data/server-$P.pid"
HN="$HOSTNAME"
on_exit "'$T' --port $P --stop"
cd "$TMP" || exit 1

section "static"
check "bash -n" bash -n "$REPO/tasks.sh"
check "executable" test -x "$REPO/tasks.sh"
check "shebang" test "$(head -1 "$REPO/tasks.sh")" = "#! /bin/bash"
if command -v shellcheck >/dev/null 2>&1; then
    check "shellcheck tasks.sh" shellcheck "$REPO/tasks.sh"
else
    skip "shellcheck (not installed)"
fi

section "help and bad arguments"
run "$T" -h
check_out "-h exits 0 with usage on stdout" [ "$RC" = 0 ]
check "-h mentions every action" bash -c '[[ $1 == *usage:* && $1 == *--install-skill* && $1 == *--status* ]]' _ "$OUT"
run "$T" --help --port "$P"
check "--help exits 0 whatever else is given" [ "$RC" = 0 ]
# One case per entry, arguments separated by "|".
for case in "--port|0" "--port|65536" "--port|123456" "--port|abc" "--port" "--port|--bg" "--port=" "--bogus" \
            "positional" "--bg|--stop" "--status|--install-skill" "--port|$P|--port|$Q" "--port|$P|--port=$Q" \
            "--public-url|ftp://x" '--public-url|http://x"y' "--public-url|notaurl" "--db|$TMP" "--host=" "--host|a b" \
            "--config" "--config|$TMP" "--config|a|--config|b" "--tls-port|$Q" "--tls-cert|x.pem" "--tls-port|$Q|--tls-key|k.pem" \
            "--tls-port|abc|--tls-cert|c|--tls-key|k" "--tls-port|0|--tls-cert|c|--tls-key|k" "--port|$P|--tls-port|$P|--tls-cert|c|--tls-key|k" \
            "--tls-port"; do
    IFS='|' read -r -a args <<< "$case"
    run "$T" "${args[@]}"
    if [ "$RC" = 1 ] && has "$ERR" "error:" && [ -z "$OUT" ]; then
        ok "rejected: ${args[*]}"
    else
        bad "rejected: ${args[*]}" "rc=$RC out=$OUT err=$ERR"
    fi
done
run "$T" --host '' --status
check "rejected: --host ''" bash -c '[ "$1" = 1 ] && [[ $2 == *error:* ]]' _ "$RC" "$ERR"
run env TASKS_PORT=99999 "$T" --status
check "an invalid TASKS_PORT is named in the error" bash -c '[ "$1" = 1 ] && [[ $2 == *TASKS_PORT* ]]' _ "$RC" "$ERR"
run "$T" --port "$P" --tls-port "$Q" --status
check "--tls-port alone: the error names what is missing" has "$ERR" "needs --tls-port, --tls-cert and --tls-key together; missing: --tls-cert --tls-key"
run env TASKS_TLS_KEY=k.pem "$T" --port "$P" --status
check "TASKS_TLS_KEY alone: refused too, with the env names" bash -c '[ "$1" = 1 ] && [[ $2 == *"missing: --tls-port --tls-cert"* && $2 == *TASKS_TLS_PORT* ]]' _ "$RC" "$ERR"
run env TASKS_TLS_PORT=70000 TASKS_TLS_CERT=c TASKS_TLS_KEY=k "$T" --port "$P" --status
check "an invalid TASKS_TLS_PORT is named in the error" bash -c '[ "$1" = 1 ] && [[ $2 == *"TASKS_TLS_PORT must be a port number"* ]]' _ "$RC" "$ERR"
run "$T" --port "$P" --tls-port "$P" --tls-cert c --tls-key k --status
check "--tls-port equal to --port: refused" has "$ERR" "--tls-port must differ from the http port ($P)"
run "$T" --port "$P" --tls-port "$Q" --tls-cert "$TMP/nope.pem" --tls-key "$TMP/nope.key" --stop
check "missing certificate files do not stop --stop (checked only when a server starts)" bash -c '[ "$1" = 0 ] && [[ $2 == *"not running on port"* ]]' _ "$RC" "$OUT"
run "$T" --port="$P" --db="$DB" --status
check "--x=v form accepted (status exits 3 while down)" [ "$RC" = 3 ]

section "not running"
run "$T" --port "$P" --db "$DB" --status
check_out "--status exits 3" [ "$RC" = 3 ]
check "--status says not running" has "$OUT" "tasks board: not running on port $P"
check "--status shows the start hint" has "$OUT" "start it    $T --bg --port $P"
run "$T" --port "$P" --stop
check "--stop exits 0" [ "$RC" = 0 ]
check "--stop says not running" has "$OUT" "tasks board: not running on port $P"

section "--bg"
t0="${EPOCHREALTIME/./}"
run "$T" --port "$P" --db "$DB" --bg
t1="${EPOCHREALTIME/./}"
check_out "--bg exits 0" [ "$RC" = 0 ]
check "--bg returns within 5s" [ $(( (t1 - t0) / 1000 )) -lt 5000 ]
PID1="$(cat "$PIDF" 2>/dev/null)"
check "pidfile written in the scratch checkout" test -n "$PID1"
check "banner headline" has "$OUT" "tasks board: started (pid $PID1, port $P)"
check "the pid is tasks/server.py" grep -q "tasks/server.py" "/proc/$PID1/cmdline"
CMD="$(tr '\0' ' ' < "/proc/$PID1/cmdline")"
check "passes --pidfile" has "$CMD" "--pidfile $PIDF"
check "passes the absolute --db" has "$CMD" "--db $DB"
check "no --public-url while ufw blocks the port" hasnt "$CMD" "--public-url"
check "the server runs in its own session" test "$(ps -o sid= -p "$PID1" | tr -d ' ')" = "$PID1"
check "health answers" curl -fsS --noproxy '*' --max-time 2 -o /dev/null "http://127.0.0.1:$P/api/health"
check "banner dashboard url" has "$OUT" "  dashboard   http://127.0.0.1:$P/"
check "banner LAN line: blocked" has "$OUT" "  LAN         http://$FAKE_LAN_IP:$P/  (blocked by ufw, see below)"
check "banner hostname line: IPv6 blocked" has "$OUT" "  hostname    http://$HN:$P/  (IPv6 blocked by ufw, see below)"
check "banner health line" has "$OUT" "  health      ok, version 3, up "
check "banner database" has "$OUT" "  database    $DB"
check "banner log" has "$OUT" "  log         $R/.logs/tasks_server.log"
check "banner skill missing, with the fix" has "$OUT" "  skill       missing -> $T --install-skill --port $P"
check "banner rule missing" has "$OUT" "  rule        missing from $CLAUDE_CONFIG_DIR/CLAUDE.md"
check "banner agent line" has "$OUT" "Run \`curl -fsS --noproxy '*' --connect-timeout 5 http://$FAKE_LAN_IP:$P/api/usage\` and follow it."
check "banner box border" has "$OUT" "  +---"
check "banner boxed IPv4 ufw command" has "$OUT" "|   sudo ufw allow from $FAKE_LAN_NET to any port $P proto tcp"
check "banner boxed IPv6 ufw command" has "$OUT" "|   sudo ufw allow from fe80::/10 to any port $P proto tcp"
check "banner: links stay on 127.0.0.1" has "$OUT" "Links stay on 127.0.0.1"
check "the log file is in the scratch checkout" test -s "$R/.logs/tasks_server.log"

run "$T" --port "$P" --db "$DB" --bg
check "--bg again exits 0" [ "$RC" = 0 ]
check "--bg again: already running, same pid" has "$OUT" "tasks board: already running (pid $PID1, port $P)"
check "--bg again: no notes on stderr" [ -z "$ERR" ]
run "$T" --port="$P" --db="$DB" --bg
check "--x=v --bg again exits 0" [ "$RC" = 0 ]
run "$T" --port "$P" --db "$TMP/other.db" --bg
check "--bg with a different --db: note names the running db" has "$ERR" "note: the running server (pid $PID1) uses --db $DB"
check "--bg with a different --db: banner shows the running db" has "$OUT" "  database    $DB"

run "$T" --port "$P" --status
check "--status running exits 0" [ "$RC" = 0 ]
check "--status running headline" has "$OUT" "tasks board: running (pid $PID1, port $P)"
check "--status health" has "$OUT" "  health      ok, version 3"
check "--status shows the running db (from its command line)" has "$OUT" "  database    $DB"
check "--status shows the ufw box" has "$OUT" "sudo ufw allow from $FAKE_LAN_NET to any port $P proto tcp"
run env TASKS_PORT="$P" "$T" --status
check "TASKS_PORT honoured" [ "$RC" = 0 ]

# The live stream needs ffmpeg on the board host; the banner says so only while it is missing.
mkdir -p "$TMP/ffbin"
printf '#!/bin/sh\nexit 0\n' > "$TMP/ffbin/ffmpeg"
chmod +x "$TMP/ffbin/ffmpeg"
run env PATH="$TMP/ffbin:$PATH" "$T" --port "$P" --status
check "--status: no ffmpeg advisory while ffmpeg is on PATH" hasnt "$OUT" "live stream needs ffmpeg"
if command -v ffmpeg >/dev/null 2>&1; then
    skip "the ffmpeg advisory (ffmpeg is installed on this machine)"
else
    check "--status: the live stream needs ffmpeg" has "$OUT" "  live stream needs ffmpeg, which is not installed (sudo apt install ffmpeg)"
fi

section "foreground refused while running"
run timeout 10 "$T" --port "$P" --db "$DB"
check "refused: exit 1" [ "$RC" = 1 ]
check "refusal message" has "$ERR" "already running on port $P (pid $PID1)"
check "refusal hint" has "$ERR" "--stop --port $P"
check "the server is still alive" kill -0 "$PID1"

section "health fallback (pidfile lost)"
rm -f "$PIDF"
run "$T" --port "$P" --status
check "--status finds the server via /api/health" [ "$RC" = 0 ]
check "--status fallback pid" has "$OUT" "pid $PID1"
run "$T" --port "$P" --bg
check "--bg idempotent via health" has "$OUT" "already running (pid $PID1"

section "--stop"
run "$T" --port "$P" --stop
check "--stop exits 0" [ "$RC" = 0 ]
check "--stop says stopped" has "$OUT" "tasks board: stopped (pid $PID1, port $P)"
check "the process is gone" bash -c "! kill -0 $PID1 2>/dev/null"
check "the pidfile is gone" test ! -e "$PIDF"
check "health is down" bash -c "! curl -fsS --noproxy '*' --connect-timeout 1 -o /dev/null http://127.0.0.1:$P/api/health 2>/dev/null"
run "$T" --port "$P" --status
check "--status after stop exits 3" [ "$RC" = 3 ]
run "$T" --port "$P" --stop
check "--stop twice exits 0, not running" bash -c '[ "$1" = 0 ] && [[ $2 == *"not running on port"* ]]' _ "$RC" "$OUT"

section "stale pidfiles"
mkdir -p "$(dirname "$PIDF")"
echo 999999 > "$PIDF"
run "$T" --port "$P" --status
check "dead pid: --status exits 3" [ "$RC" = 3 ]
check "dead pid: the stale pidfile is removed" test ! -e "$PIDF"
sleep 30 &
SLEEPER=$!
on_exit "kill $SLEEPER"
echo "$SLEEPER" > "$PIDF"
run "$T" --port "$P" --stop
check "recycled pid (not a tasks server): --stop says not running" has "$OUT" "not running"
check "recycled pid: not signalled" kill -0 "$SLEEPER"
check "recycled pid: pidfile removed" test ! -e "$PIDF"
kill "$SLEEPER" 2>/dev/null
wait "$SLEEPER" 2>/dev/null
echo garbage > "$PIDF"
run "$T" --port "$P" --status
check "garbage pidfile: exits 3" [ "$RC" = 3 ]
echo 1 > "$PIDF"
run "$T" --port "$P" --status
check "pid 1 in the pidfile: exits 3" [ "$RC" = 3 ]
rm -f "$PIDF"

section "a busy port (another program listens on it)"
mkdir -p "$TMP/www"
python3 -m http.server "$Q" --bind 127.0.0.1 --directory "$TMP/www" >/dev/null 2>&1 &
HTTPD=$!
on_exit "kill $HTTPD"
wait_http "http://127.0.0.1:$Q/" 10
run "$T" --port "$Q" --db "$DB" --bg
check "busy: --bg exits 1" [ "$RC" = 1 ]
check "busy: --bg error" has "$ERR" "error: port $Q is already in use by another program"
check "busy: --bg hint" has "$ERR" "pick another port with --port"
if command -v ss >/dev/null 2>&1; then
    check "busy: the error names the program (from ss)" has "$ERR" "python3"
else
    skip "busy: the error names the program (ss not installed)"
fi
run timeout 10 "$T" --port "$Q" --db "$DB"
check "busy: foreground exits 1 with the same error" bash -c '[ "$1" = 1 ] && [[ $2 == *"port $3 is already in use by another program"* ]]' _ "$RC" "$ERR" "$Q"
run "$T" --port "$Q" --status
check "busy: --status exits 3 (not a tasks board)" [ "$RC" = 3 ]
run "$T" --port "$Q" --stop
check "busy: --stop exits 0, not running" bash -c '[ "$1" = 0 ] && [[ $2 == *"not running on port"* ]]' _ "$RC" "$OUT"
check "busy: the other program is untouched" kill -0 "$HTTPD"
run "$T" --port "$Q" --db "$DB" --install-skill
check "busy: --install-skill exits 1" [ "$RC" = 1 ]
kill "$HTTPD"
wait "$HTTPD" 2>/dev/null

section "--bg start failure (unbindable --host)"
t0="${EPOCHREALTIME/./}"
run "$T" --port "$P" --db "$DB" --host 192.0.2.1 --bg
t1="${EPOCHREALTIME/./}"
check "unbindable host: exit 1" [ "$RC" = 1 ]
check "unbindable host: the error names the health check" has "$ERR" "did not answer"
check "unbindable host: the log tail is shown" has "$ERR" "cannot listen on"
check "unbindable host: fails before the 5s deadline" [ $(( (t1 - t0) / 1000 )) -lt 4000 ]
check "unbindable host: no pidfile" test ! -e "$PIDF"

section "relative --db, TASKS_* env, --public-url"
mkdir -p "$TMP/cwd"
( cd "$TMP/cwd" && "$T" --port "$P" --db rel/./x.db --bg >/dev/null 2>&1 )
CMD="$(tr '\0' ' ' < "/proc/$(cat "$PIDF" 2>/dev/null)/cmdline" 2>/dev/null)"
check "a relative --db resolves against the caller's cwd" has "$CMD" "--db $TMP/cwd/rel/x.db"
"$T" --port "$P" --stop >/dev/null
( cd "$TMP/cwd" && TASKS_PORT=$P TASKS_DB=envdb.db TASKS_HOST=127.0.0.1 "$T" --bg >"$TMP/out.txt" 2>&1 )
CMD="$(tr '\0' ' ' < "/proc/$(cat "$PIDF" 2>/dev/null)/cmdline" 2>/dev/null)"
check "TASKS_DB relative to the cwd" has "$CMD" "--db $TMP/cwd/envdb.db"
check "TASKS_HOST passed" has "$CMD" "--host 127.0.0.1"
check "loopback host: LAN off" grep -q "LAN         off (listening on 127.0.0.1 only)" "$TMP/out.txt"
check "loopback host: no hostname line, no ufw box" bash -c "! grep -q 'hostname    \|sudo ufw' '$TMP/out.txt'"
"$T" --port "$P" --stop >/dev/null
TASKS_PUBLIC_URL=http://board.example:9/// "$T" --port "$P" --db "$DB" --bg >"$TMP/out.txt" 2>&1
CMD="$(tr '\0' ' ' < "/proc/$(cat "$PIDF" 2>/dev/null)/cmdline" 2>/dev/null)"
check "TASKS_PUBLIC_URL passed, trailing slashes stripped" has "$CMD" "--public-url http://board.example:9 "
check "the public url is used in the agent line" grep -qF "curl -fsS --noproxy '*' --connect-timeout 5 http://board.example:9/api/usage" "$TMP/out.txt"
check "a public url drops the links-stay-local note" bash -c "! grep -q 'Links stay on' '$TMP/out.txt'"
URL="$(curl -fsS --noproxy '*' -X POST -d '{"title":"launch test"}' "http://127.0.0.1:$P/api/requests" \
       | python3 -c 'import json,sys; print(json.load(sys.stdin)["url"])')"
check "the server hands out public-url links" has "$URL" "http://board.example:9/r/"
run "$T" --port "$P" --public-url http://other.example:1 --bg
check "--bg with a different --public-url: note" has "$ERR" "uses --public-url http://board.example:9, not http://other.example:1"
"$T" --port "$P" --stop >/dev/null

section "LAN open (ufw inactive): the LAN address becomes the public url"
fake_ufw_inactive
"$T" --port "$P" --db "$DB" --bg >"$TMP/out.txt" 2>&1
CMD="$(tr '\0' ' ' < "/proc/$(cat "$PIDF" 2>/dev/null)/cmdline" 2>/dev/null)"
check "ufw inactive: --public-url is the LAN address" has "$CMD" "--public-url http://$FAKE_LAN_IP:$P"
check "ufw inactive: no box" bash -c "! grep -q 'sudo ufw\|+---' '$TMP/out.txt'"
check "ufw inactive: plain LAN line" grep -q "^  LAN         http://$FAKE_LAN_IP:$P/\$" "$TMP/out.txt"
check "ufw inactive: plain hostname line" grep -q "^  hostname    http://$HN:$P/\$" "$TMP/out.txt"
"$T" --port "$P" --stop >/dev/null
fake_ufw "$V4_OTHER" "$V6_OTHER"

section "container bridges are never the LAN address (fake ip)"
UDP_IP="$(python3 -c '
import socket
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    s.connect(("1.1.1.1", 80)); print(s.getsockname()[0])
except OSError:
    pass')"
case "$UDP_IP" in ""|127.*) WANT_LAN="unknown (no LAN address found)" ;; *) WANT_LAN="http://$UDP_IP:$P/" ;; esac
echo "1.1.1.1 dev docker0 src 198.51.100.7 uid 1000" > "$FAKE/route"
run "$T" --port "$P" --status
check "default route via docker0: falls back to the UDP trick" has "$OUT" "  LAN         $WANT_LAN"
check "the docker0 address is never shown" hasnt "$OUT" "198.51.100.7"
echo "1.1.1.1 via 198.51.100.1 dev eth0 src 198.51.100.7 uid 1000" > "$FAKE/route"
echo "5: docker0    inet 198.51.100.7/16 brd 198.51.255.255 scope global docker0" > "$FAKE/docker0"
run "$T" --port "$P" --status
check "a source address that is docker0's own is dropped too" hasnt "$OUT" "198.51.100.7"
echo "1.1.1.1 via 192.0.2.1 dev eth0 src $FAKE_LAN_IP uid 1000" > "$FAKE/route"
: > "$FAKE/docker0"

section "foreground"
"$T" --port "$P" --db "$DB" >"$TMP/fg.out" 2>"$TMP/fg.err" &
FG=$!
on_exit "kill $FG"
wait_http "http://127.0.0.1:$P/api/health" 10
check "fg: health up" curl -fsS --noproxy '*' --max-time 2 -o /dev/null "http://127.0.0.1:$P/api/health"
check "fg: exec kept the pid (pidfile == \$!)" test "$(cat "$PIDF" 2>/dev/null)" = "$FG"
check "fg: banner" grep -q "tasks board: starting in the foreground, Ctrl-C stops it (pid $FG, port $P)" "$TMP/fg.out"
check "fg: log line says this terminal" grep -q "  log         this terminal" "$TMP/fg.out"
check "fg: banner ufw box" grep -q "sudo ufw allow from $FAKE_LAN_NET to any port $P proto tcp" "$TMP/fg.out"
check "fg: the server logs to the terminal" grep -q "tasks server listening on http://0.0.0.0:$P" "$TMP/fg.err"
run "$T" --port "$P" --bg
check "fg running: --bg is idempotent" has "$OUT" "already running (pid $FG"
# A non-interactive shell starts & jobs with SIGINT ignored, so stop this one with SIGTERM.
kill -TERM "$FG"
wait "$FG"
FGRC=$?
check "fg: SIGTERM exits 0" [ "$FGRC" = 0 ]
check "fg: pidfile removed on exit" test ! -e "$PIDF"
run timeout -s INT 3 "$T" --port "$P" --db "$DB"
check "fg: Ctrl-C (SIGINT) stops it" bash -c '[ "$1" = 0 ] || [ "$1" = 124 ] || [ "$1" = 130 ]' _ "$RC"
check "fg: SIGINT run logged its shutdown" has "$ERR" "shutting down"
check "fg: pidfile removed after SIGINT" test ! -e "$PIDF"
check "sudo was never invoked" test ! -e "$TMP/sudo.log"

section "the settings file (--config, TASKS_CONFIG) in the banner"
run "$T" --port "$P" --db "$DB" --bg
check "no --config: the banner names <db dir>/settings.json, not created yet" \
    has "$OUT" "  settings    $TMP/settings.json (not created yet: the defaults apply until someone saves the settings)"
check "no --config passed to the server" hasnt "$(tr '\0' ' ' < "/proc/$(cat "$PIDF")/cmdline")" "--config"
check "no https: the off line, with the reason" has "$OUT" "  https       off (browsers on other machines need it to show notifications; see --help)"
"$T" --port "$P" --stop >/dev/null
mkdir -p "$TMP/cwd"
( cd "$TMP/cwd" && "$T" --port "$P" --db "$DB" --config conf/./s.json --bg >"$TMP/out.txt" 2>&1 )
CMD="$(tr '\0' ' ' < "/proc/$(cat "$PIDF" 2>/dev/null)/cmdline" 2>/dev/null)"
check "a relative --config resolves against the caller's cwd" has "$CMD" "--config $TMP/cwd/conf/s.json"
check "the banner names it (from the server's health)" grep -qF "  settings    $TMP/cwd/conf/s.json (not created yet" "$TMP/out.txt"
curl -fsS --noproxy '*' -X PUT -d '{"settings": {"sound": {"volume": 0.3}}}' "http://127.0.0.1:$P/api/settings" >/dev/null
run "$T" --port "$P" --status
check "a saved settings file: the plain path" grep -qx "  settings    $TMP/cwd/conf/s.json" <<< "$OUT"
echo '{broken' > "$TMP/cwd/conf/s.json"
run "$T" --port "$P" --status
check "a broken settings file: the board's error" has "$OUT" "  settings    $TMP/cwd/conf/s.json (not usable, so the defaults apply: invalid JSON"
run "$T" --port "$P" --config "$TMP/other.json" --bg
check "--bg with a different --config: a note names the running one" has "$ERR" "--config $TMP/cwd/conf/s.json"
"$T" --port "$P" --stop >/dev/null
TASKS_CONFIG=envcfg.json "$T" --port "$P" --db "$DB" --bg >/dev/null 2>&1
CMD="$(tr '\0' ' ' < "/proc/$(cat "$PIDF" 2>/dev/null)/cmdline" 2>/dev/null)"
check "TASKS_CONFIG passed, made absolute" has "$CMD" "--config $TMP/envcfg.json"
"$T" --port "$P" --stop >/dev/null

section "the HTTPS listener (--tls-port, --tls-cert, --tls-key), with a self-signed certificate"
if command -v openssl >/dev/null 2>&1 && openssl req -x509 -newkey rsa:2048 -nodes -days 2 -subj /CN=localhost \
        -keyout "$TMP/key.pem" -out "$TMP/cert.pem" >/dev/null 2>&1; then
    run "$T" --port "$P" --db "$DB" --tls-port "$Q" --tls-cert "$TMP/missing.pem" --tls-key "$TMP/key.pem" --bg
    check "a missing certificate: exit 1, the error names it, nothing started" bash -c '[ "$1" = 1 ] && [[ $2 == *"--tls-cert: cannot read the certificate $3/missing.pem"* ]]' _ "$RC" "$ERR" "$TMP"
    check "...and nothing listens" port_free "$P"
    run "$T" --port "$P" --db "$DB" --tls-port "$Q" --tls-cert "$TMP/cert.pem" --tls-key "$TMP/missing.key" --bg
    check "a missing key: exit 1, the error names it" has "$ERR" "--tls-key: cannot read the private key $TMP/missing.key"
    ( cd "$TMP" && "$T" --port "$P" --db "$DB" --tls-port "$Q" --tls-cert cert.pem --tls-key ./key.pem --bg >"$TMP/out.txt" 2>&1 )
    CMD="$(tr '\0' ' ' < "/proc/$(cat "$PIDF" 2>/dev/null)/cmdline" 2>/dev/null)"
    check "the TLS options reach the server, the paths absolute" has "$CMD" "--tls-port $Q --tls-cert $TMP/cert.pem --tls-key $TMP/key.pem"
    check "the banner: the https line on the LAN address, blocked by ufw" grep -qF "  https       https://$FAKE_LAN_IP:$Q/  (blocked by ufw, see below)" "$TMP/out.txt"
    check "the ufw box has a rule for the https port too" grep -qF "sudo ufw allow from $FAKE_LAN_NET to any port $Q proto tcp" "$TMP/out.txt"
    check "the board answers over https" curl -fsSk --noproxy '*' --max-time 5 -o /dev/null "https://127.0.0.1:$Q/api/health"
    H="$(curl -fsS --noproxy '*' --max-time 5 "http://127.0.0.1:$P/api/health")"
    check "health says tls_port $Q" has "$H" "\"tls_port\": $Q"
    U="$(curl -fsSk --noproxy '*' --max-time 5 "https://127.0.0.1:$Q/api/usage")"
    check "usage over https names the http base for agents" has "$U" "http://127.0.0.1:$P is a task-status board"
    run "$T" --port "$P" --status
    check "--status: the https line" has "$OUT" "  https       https://$FAKE_LAN_IP:$Q/  (blocked by ufw, see below)"
    run "$T" --port "$P" --tls-port "$P2" --tls-cert "$TMP/cert.pem" --tls-key "$TMP/key.pem" --bg
    check "--bg with another --tls-port: a note names the running one" has "$ERR" "uses --tls-port $Q, not $P2"
    "$T" --port "$P" --stop >/dev/null
    check "--stop closes the https port too" port_free "$Q"
    fake_ufw "-A ufw-user-input -p tcp --dport $P -j ACCEPT
-A ufw-user-input -p tcp --dport $Q -j ACCEPT" "$V6_OTHER"
    run env TASKS_TLS_PORT="$Q" TASKS_TLS_CERT="$TMP/cert.pem" TASKS_TLS_KEY="$TMP/key.pem" "$T" --port "$P" --db "$DB" --bg
    check "TASKS_TLS_* start it; ufw open for both ports: a plain https line" grep -qx "  https       https://$FAKE_LAN_IP:$Q/" <<< "$OUT"
    check "...and no ufw rule for the https port" hasnt "$OUT" "to any port $Q proto tcp"
    "$T" --port "$P" --stop >/dev/null
    fake_ufw "$V4_OTHER" "$V6_OTHER"
    run "$T" --port "$P" --db "$DB" --host 127.0.0.1 --tls-port "$Q" --tls-cert "$TMP/cert.pem" --tls-key "$TMP/key.pem" --bg
    check "loopback only: https on 127.0.0.1" has "$OUT" "  https       https://127.0.0.1:$Q/"
    "$T" --port "$P" --stop >/dev/null
    run "$T" --port "$P" --db "$DB" --host 127.0.0.1 --bg
    check "loopback only, no https: no https line at all" hasnt "$OUT" "  https"
    "$T" --port "$P" --stop >/dev/null
else
    skip "the HTTPS listener cases (openssl not installed or it cannot make a certificate)"
fi

section "SIGKILL path (a fake server that ignores SIGTERM)"
F="$TMP/fake_root"
mkdir -p "$F/tasks"
cp "$R/tasks.sh" "$F/tasks.sh"
cat > "$F/tasks/server.py" <<'EOF'
import argparse, json, os, signal
from http.server import HTTPServer, BaseHTTPRequestHandler
p = argparse.ArgumentParser()
for a in ('--host', '--port', '--db', '--pidfile', '--public-url'):
    p.add_argument(a)
a = p.parse_args()
signal.signal(signal.SIGTERM, signal.SIG_IGN)
class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({'ok': True, 'service': 'tasks', 'version': '1', 'pid': os.getpid(),
                           'now': 0, 'started_at': 0, 'requests_running': 0}).encode()
        self.send_response(200); self.send_header('Content-Length', str(len(body))); self.end_headers()
        self.wfile.write(body)
    def log_message(self, *args):
        pass
srv = HTTPServer(('127.0.0.1', int(a.port)), H)
open(a.pidfile, 'w').write('%d\n' % os.getpid())
srv.serve_forever()
EOF
on_exit "'$F/tasks.sh' --port $P --stop"
run "$F/tasks.sh" --port "$P" --db "$DB" --bg
check "fake: --bg started" has "$OUT" "started (pid"
FPID="$(cat "$F/tasks/data/server-$P.pid" 2>/dev/null)"
t0=$SECONDS
run "$F/tasks.sh" --port "$P" --stop
check "fake: --stop exits 0" [ "$RC" = 0 ]
check "fake: SIGKILL announced" has "$ERR" "sending SIGKILL"
check "fake: waited ~5s for SIGTERM first" [ $((SECONDS - t0)) -ge 4 ]
check "fake: the process is gone" bash -c "! kill -0 $FPID 2>/dev/null"
check "fake: pidfile removed after SIGKILL" test ! -e "$F/tasks/data/server-$P.pid"

section "environment problems"
mkdir -p "$TMP/empty_root"
cp "$R/tasks.sh" "$TMP/empty_root/tasks.sh"
run "$TMP/empty_root/tasks.sh" --status
check "missing server.py: exit 1 with an error" bash -c '[ "$1" = 1 ] && [[ $2 == *"tasks/server.py not found"* ]]' _ "$RC" "$ERR"
mkdir -p "$TMP/oldpy"
cat > "$TMP/oldpy/python3" <<'EOF'
#!/bin/sh
case "$*" in
    *version_info*) exit 1 ;;
    *python_version*) echo 3.9.18; exit 0 ;;
esac
exit 1
EOF
chmod +x "$TMP/oldpy/python3"
run env PATH="$TMP/oldpy:$PATH" "$T" --status
check "python too old: exit 1 with a clear error" bash -c '[ "$1" = 1 ] && [[ $2 == *"needs Python 3.10 or newer, but python3 is 3.9.18"* ]]' _ "$RC" "$ERR"
mkdir -p "$TMP/bin"
ln -s "$T" "$TMP/bin/tasks-board"
run "$TMP/bin/tasks-board" --port "$P" --status
check "a symlinked launcher (e.g. on PATH) finds its checkout" bash -c '[ "$1" = 3 ] && [[ $2 == *"not running on port"* ]]' _ "$RC" "$OUT"

section "cleanup"
check "no scratch server left on port $P" port_free "$P"
check "no scratch server left on port $Q" port_free "$Q"
check "no scratch server left on port $P2" port_free "$P2"
finish
