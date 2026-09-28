#! /bin/bash
# tasks.sh's firewall detection (IPv4 user.rules and IPv6 user6.rules) and what the banner then says,
# against fake /etc/ufw files: never the host's firewall. Every case is `--status` with no board
# running, so nothing is started or bound.
. "$(dirname -- "${BASH_SOURCE[0]}")/lib/common.sh"

make_fakes
R="$TMP/root"
make_root "$R"
T="$R/tasks.sh"
P="$(free_port)"
HN="$HOSTNAME"
V4_OPEN="-A ufw-user-input -p tcp --dport $P -s $FAKE_LAN_NET -j ACCEPT"
V4_OTHER="-A ufw-user-input -p tcp --dport 22 -j ACCEPT"
V6_OPEN="-A ufw6-user-input -p tcp --dport $P -s fe80::/10 -j ACCEPT"
V6_OTHER="-A ufw6-user-input -p tcp --dport 22 -j ACCEPT"
V4CMD="sudo ufw allow from $FAKE_LAN_NET to any port $P proto tcp"
V6CMD="sudo ufw allow from fe80::/10 to any port $P proto tcp"
LAN_OPEN="  LAN         http://$FAKE_LAN_IP:$P/"
LAN_BLOCKED="  LAN         http://$FAKE_LAN_IP:$P/  (blocked by ufw, see below)"
HOST_OPEN="  hostname    http://$HN:$P/"
HOST_BLOCKED="  hostname    http://$HN:$P/  (IPv6 blocked by ufw, see below)"
status() { run "$T" --port "$P" --status "$@"; OUT="$OUT$ERR"; }
has_line() { grep -qxF -- "$2" <<< "$1"; }
cd "$TMP" || exit 1

section "v4 blocked, v6 blocked"
fake_ufw "$V4_OTHER" "$V6_OTHER"
status
check "exit 3 (not running)" [ "$RC" = 3 ]
check "LAN line: blocked" has_line "$OUT" "$LAN_BLOCKED"
check "hostname line: IPv6 blocked" has_line "$OUT" "$HOST_BLOCKED"
check "v4 command in the box" has "$OUT" "|   $V4CMD"
check "v6 section labelled optional" has "$OUT" "| Optional, for hostname access over IPv6 (http://$HN:$P/ from a PC"
check "v6 command in the box" has "$OUT" "|   $V6CMD"
check "the v4 section comes first" bash -c '[[ "${1%%fe80::/10 to any*}" == *"$2 to any port"* ]]' _ "$OUT" "$FAKE_LAN_NET"
check "the links note comes before the v6 part" bash -c '[[ "${1%%Optional, for hostname*}" == *"Links stay on 127.0.0.1"* ]]' _ "$OUT"

section "v4 open, v6 blocked"
fake_ufw "$V4_OPEN" "$V6_OTHER"
status
check "LAN line: plain" has_line "$OUT" "$LAN_OPEN"
check "a standalone v6 intro" has "$OUT" "| ufw is active and has no IPv6 rule for port $P."
check "v6 command" has "$OUT" "|   $V6CMD"
check "no v4 command" hasnt "$OUT" "$V4CMD"
check "no links note" hasnt "$OUT" "Links stay on"

section "v4 open, v6 open"
fake_ufw "$V4_OPEN" "$V6_OPEN"
status
check "LAN line: plain" has_line "$OUT" "$LAN_OPEN"
check "hostname line: plain" has_line "$OUT" "$HOST_OPEN"
check "no box" hasnt "$OUT" "+---"

section "v4 blocked, v6 open: only the IPv4 box"
fake_ufw "$V4_OTHER" "$V6_OPEN"
status
check "LAN line: blocked" has_line "$OUT" "$LAN_BLOCKED"
check "hostname line: plain" has_line "$OUT" "$HOST_OPEN"
check "v4 intro, command and links note" bash -c '[[ $1 == *"ufw is active and has no rule letting the LAN reach port $2"* && $1 == *"|   $3"* && $1 == *"Links stay on 127.0.0.1"* ]]' _ "$OUT" "$P" "$V4CMD"
check "no v6 part" bash -c '[[ $1 != *fe80::/10* && $1 != *"Optional, for hostname"* ]]' _ "$OUT"

section "IPv6 rule shapes that open the port"
for rule in "-A ufw6-user-input -p tcp --dport $P -j ufw6-user-limit-accept" \
            "-A ufw6-user-input -p tcp -m multiport --dports 22,$P -j ACCEPT" \
            "-A ufw6-user-input -p tcp -m multiport --dports $((P - 1)):$((P + 1)) -j ACCEPT" \
            "-A ufw6-user-input --dport $P -j ACCEPT"; do
    fake_ufw "$V4_OPEN" "$rule"
    status
    check "v6 open via: $rule" bash -c '[[ $1 != *fe80::/10* ]] && grep -qxF -- "$2" <<< "$1"' _ "$OUT" "$HOST_OPEN"
done
section "IPv6 rule shapes that do not"
for rule in "-A ufw6-user-input -p udp --dport $P -j ACCEPT" \
            "-A ufw-user-input -p tcp --dport $P -j ACCEPT" \
            "-A ufw6-user-input -p tcp --dport $P -j DROP" \
            "-A ufw6-user-output -p tcp --dport $P -j ACCEPT" \
            "-A ufw6-user-input -p tcp -m multiport --dports $((P + 1)):$((P + 9)) -j ACCEPT"; do
    fake_ufw "$V4_OPEN" "$rule"
    status
    check "v6 still blocked with: $rule" has "$OUT" "$V6CMD"
done

section "IPv4 rule shapes"
for rule in "$V4_OPEN" "-A ufw-user-input -p tcp --dport $P -j ufw-user-limit-accept" \
            "-A ufw-user-input -p tcp -m multiport --dports $((P - 100)):$((P + 100)) -j ACCEPT" \
            "-A ufw-user-input -p tcp -m multiport --dports 1,2,$P -j ACCEPT"; do
    fake_ufw "$rule" "$V6_OPEN"
    status
    check "v4 open via: $rule" bash -c 'grep -qxF -- "$2" <<< "$1" && [[ $1 != *"+---"* ]]' _ "$OUT" "$LAN_OPEN"
done
for rule in "$V4_OTHER" "-A ufw-user-input -p udp --dport $P -j ACCEPT" "-A ufw6-user-input -p tcp --dport $P -j ACCEPT" \
            "-A ufw-user-input -p tcp --dport $P -j DROP"; do
    fake_ufw "$rule" "$V6_OPEN"
    status
    check "v4 blocked with: $rule" bash -c 'grep -qxF -- "$2" <<< "$1" && [[ $1 == *"$3"* ]]' _ "$OUT" "$LAN_BLOCKED" "$V4CMD"
done

section "files and settings that change the answer"
fake_ufw "$V4_OPEN" "$V6_OTHER" no
status
check "IPV6=no: nothing is filtered over IPv6" bash -c '[[ $1 != *fe80::/10* ]] && grep -qxF -- "$2" <<< "$1"' _ "$OUT" "$HOST_OPEN"
if [ "$(id -u)" = 0 ]; then
    skip "unreadable rule files (root reads them anyway)"
else
    fake_ufw "$V4_OPEN" "$V6_OTHER"
    chmod 000 "$FAKE/etc/ufw/user6.rules"
    status
    # (the copy's messages name the fake paths too: the substitution rewrote every /etc/ufw/ in it)
    check "user6.rules unreadable: hostname note" has "$OUT" "(IPv6 may be blocked: cannot read $FAKE/etc/ufw/user6.rules)"
    check "user6.rules unreadable: box intro" has "$OUT" "| ufw is active but $FAKE/etc/ufw/user6.rules is unreadable."
    check "user6.rules unreadable: v6 command" has "$OUT" "$V6CMD"
    chmod 644 "$FAKE/etc/ufw/user6.rules"
    fake_ufw "$V4_OTHER" "$V6_OPEN"
    chmod 000 "$FAKE/etc/ufw/user.rules"
    status
    check "user.rules unreadable: LAN note" has_line "$OUT" "  LAN         http://$FAKE_LAN_IP:$P/  (may be blocked: cannot read $FAKE/etc/ufw/user.rules)"
    check "user.rules unreadable: box intro and command" bash -c '[[ $1 == *"| ufw is active but $4/etc/ufw/user.rules is unreadable, so port $2 may be"* && $1 == *"$3"* ]]' _ "$OUT" "$P" "$V4CMD" "$FAKE"
    chmod 644 "$FAKE/etc/ufw/user.rules"
fi
fake_ufw "$V4_OTHER" "$V6_OTHER" yes no
status
check "ufw.conf ENABLED=no: nothing filtered, no box" bash -c '[[ $1 != *"+---"* ]] && grep -qxF -- "$2" <<< "$1"' _ "$OUT" "$LAN_OPEN"
fake_ufw "$V4_OTHER" "$V6_OTHER"
printf 'IPV6=yes\nDEFAULT_INPUT_POLICY="ACCEPT"\n' > "$FAKE/default_ufw"
status
check "DEFAULT_INPUT_POLICY=ACCEPT: nothing filtered" bash -c '[[ $1 != *"+---"* ]] && grep -qxF -- "$2" <<< "$1"' _ "$OUT" "$LAN_OPEN"
fake_ufw "$V4_OTHER" "$V6_OTHER"
fake_ufw_inactive
status
check "ufw service inactive: nothing filtered" bash -c '[[ $1 != *"+---"* ]] && grep -qxF -- "$2" <<< "$1" && grep -qxF -- "$3" <<< "$1"' _ "$OUT" "$LAN_OPEN" "$HOST_OPEN"

section "listen addresses"
fake_ufw "$V4_OTHER" "$V6_OTHER"
status --host 127.0.0.1
check "loopback: LAN off, no hostname line, no box" bash -c '[[ $1 == *"LAN         off (listening on 127.0.0.1 only)"* && $1 != *"hostname    "* && $1 != *"+---"* ]]' _ "$OUT"
status --host ::1
check "::1: no hostname line, no box" bash -c '[[ $1 != *"hostname    "* && $1 != *"+---"* ]]' _ "$OUT"
status --host "$FAKE_LAN_IP"
check "a specific IPv4 address: no hostname line, no v6 command, the v4 box" bash -c '[[ $1 != *"hostname    "* && $1 != *fe80::/10* && $1 == *"$2"* ]]' _ "$OUT" "$V4CMD"
for h in :: '[::]' '*'; do
    status --host "$h"
    check "wildcard $h: hostname line and v6 command" bash -c '[[ $1 == *"hostname    http://"* && $1 == *"$2"* ]]' _ "$OUT" "$V6CMD"
done
status --host fe80::1
check "a specific IPv6 address: listening there only, no hostname line" bash -c '[[ $1 == *"LAN         listening on fe80::1 only"* && $1 != *"hostname    "* ]]' _ "$OUT"
status --host board.example
check "a host name: listening there only" has "$OUT" "LAN         listening on board.example only"
: > "$FAKE/route"
status
check "no LAN address found" bash -c '[[ $1 == *"LAN         unknown (no LAN address found)"* || $1 == *"LAN         http://"* ]]' _ "$OUT"

finish
