#!/usr/bin/env bash
#
# p2p-connect.sh -- bring up a Wi-Fi Direct (P2P) link between two nodes.
#
# Run the SAME command on node A and node B:
#
#     sudo ./p2p-connect.sh wlan0
#
# The two nodes discover each other, negotiate a Wi-Fi Direct group, and each
# end assigns itself a static address on the 192.168.4.0/24 subnet:
#
#     group owner (GO) -> 192.168.4.1
#     client           -> 192.168.4.2
#
# The GO is chosen deterministically (higher P2P device address wins) so the
# identical script does the right thing on both machines -- no DHCP, no editing.
#
# When it finishes the nodes can reach each other directly, e.g. from either:
#     ping 192.168.4.1   ping 192.168.4.2
#
# Tear down with:
#     sudo ./p2p-connect.sh down wlan0
#
set -euo pipefail

# ---- args / config ---------------------------------------------------------
MODE="up"
if [[ "${1:-}" == "down" ]]; then MODE="down"; shift; fi

IFACE="${1:-wlan0}"
PREFIX="${2:-ktest-p2p}"           # shared device-name prefix so peers find each other
SUBNET="${SUBNET:-192.168.4}"      # /24 the link lives on
CTRL="/run/wpa_supplicant_p2p"     # private ctrl socket dir (avoids NM's instance)
CONF="/run/wpa_supplicant_p2p.conf"
PIDF="/run/wpa_supplicant_p2p.pid"
WC=(wpa_cli -p "$CTRL" -i "$IFACE")

log() { echo "[p2p] $*" >&2; }
die() { echo "[p2p] ERROR: $*" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run as root (e.g. sudo $0 ...)"

# ---- teardown --------------------------------------------------------------
teardown() {
    log "tearing down P2P on $IFACE"
    if [[ -S "$CTRL/$IFACE" ]]; then
        "${WC[@]}" p2p_group_remove '*' 2>/dev/null || true
        "${WC[@]}" p2p_stop_find    2>/dev/null || true
        "${WC[@]}" terminate        2>/dev/null || true
    fi
    [[ -f "$PIDF" ]] && kill "$(cat "$PIDF")" 2>/dev/null || true
    rm -f "$PIDF" "$CONF"
    command -v nmcli >/dev/null 2>&1 && nmcli dev set "$IFACE" managed yes 2>/dev/null || true
    log "done"
}

if [[ "$MODE" == "down" ]]; then teardown; exit 0; fi

# ---- preflight -------------------------------------------------------------
for t in iw wpa_supplicant wpa_cli ip; do
    command -v "$t" >/dev/null || die "missing required tool: $t"
done

iw list 2>/dev/null | grep -q 'P2P-' \
    || log "WARNING: driver may not advertise P2P support; continuing anyway"

# Stop NetworkManager (and any stray supplicant) from owning the radio.
command -v nmcli >/dev/null 2>&1 && nmcli dev set "$IFACE" managed no 2>/dev/null || true
pkill -f "wpa_supplicant.*$CONF" 2>/dev/null || true
sleep 1

# ---- start a dedicated wpa_supplicant --------------------------------------
mkdir -p "$CTRL"
cat > "$CONF" <<EOF
ctrl_interface=$CTRL
update_config=1
device_name=$PREFIX-$(hostname -s 2>/dev/null || echo node)
device_type=1-0050F204-1
p2p_go_intent=7
# WPA2-PSK group, AES only -- required for a clean P2P group:
p2p_go_ht40=1
EOF

ip link set "$IFACE" up
log "starting wpa_supplicant on $IFACE"
wpa_supplicant -B -P "$PIDF" -i "$IFACE" -D nl80211 -c "$CONF" \
    || die "wpa_supplicant failed to start (is the iface name correct? try: iw dev)"

# Wait for the control socket to come alive.
for _ in $(seq 1 10); do [[ -S "$CTRL/$IFACE" ]] && break; sleep 0.5; done
[[ -S "$CTRL/$IFACE" ]] || die "wpa_supplicant ctrl socket never appeared"

MYADDR=$("${WC[@]}" status | sed -n 's/^p2p_device_address=//p' | tr 'a-f' 'A-F')
[[ -n "$MYADDR" ]] || die "could not read our own p2p_device_address"
log "this node P2P address: $MYADDR  (name: $PREFIX-$(hostname -s 2>/dev/null || echo node))"

norm() { tr -d ':' | tr 'a-f' 'A-F'; }   # MAC -> comparable hex string

# ---- discover the peer -----------------------------------------------------
log "searching for peer named '$PREFIX-*' (run this script on the other node too)..."
"${WC[@]}" p2p_find type=social >/dev/null || "${WC[@]}" p2p_find >/dev/null

PEER=""
for _ in $(seq 1 120); do            # up to ~2 min
    for mac in $("${WC[@]}" p2p_peers 2>/dev/null); do
        name=$("${WC[@]}" p2p_peer "$mac" 2>/dev/null | sed -n 's/^device_name=//p')
        if [[ "$name" == "$PREFIX-"* ]]; then PEER="$mac"; break; fi
    done
    [[ -n "$PEER" ]] && break
    sleep 1
done
[[ -n "$PEER" ]] || die "no peer found -- check both nodes are running this script on the same band/channel"
log "found peer: $PEER"

# ---- deterministic GO selection -------------------------------------------
# Higher device address becomes the Group Owner -> .1, the other -> .2.
if [[ "$(echo "$MYADDR" | norm)" > "$(echo "$PEER" | norm)" ]]; then
    ROLE="GO";     INTENT=15; HOSTN=1
else
    ROLE="client"; INTENT=0;  HOSTN=2
fi
log "role: $ROLE  (go_intent=$INTENT, will take ${SUBNET}.${HOSTN})"

# ---- connect (retry until the group interface shows up) --------------------
wait_group_iface() {
    for _ in $(seq 1 30); do
        for d in $(iw dev 2>/dev/null | awk '/Interface/{print $2}'); do
            [[ "$d" == "$IFACE" ]] && continue
            iw dev "$d" info 2>/dev/null | grep -qi 'type P2P' && { echo "$d"; return 0; }
        done
        sleep 1
    done
    return 1
}

GRP=""
for attempt in 1 2 3; do
    log "connect attempt $attempt ..."
    "${WC[@]}" p2p_stop_find >/dev/null 2>&1 || true
    "${WC[@]}" p2p_connect "$PEER" pbc go_intent=$INTENT persistent >/dev/null 2>&1 || true
    if GRP=$(wait_group_iface); then break; fi
    log "no group yet; re-scanning"
    "${WC[@]}" p2p_find >/dev/null 2>&1 || true
    sleep 2
done
[[ -n "$GRP" ]] || die "P2P group did not form (try re-running on both nodes at the same time)"
log "P2P group interface: $GRP"

# ---- static addressing -----------------------------------------------------
ip link set "$GRP" up
ip addr flush dev "$GRP" 2>/dev/null || true
ip addr add "${SUBNET}.${HOSTN}/24" dev "$GRP"

PEER_HOST=$(( HOSTN == 1 ? 2 : 1 ))
log "READY -- this node is ${SUBNET}.${HOSTN} ($ROLE) on $GRP"
log "       -- reach the other node at ${SUBNET}.${PEER_HOST}, e.g.:  ping ${SUBNET}.${PEER_HOST}"
