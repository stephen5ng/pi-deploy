#!/bin/bash
# ============================================================================
# dhcp-preference.sh — choose which DHCP server this Pi listens to.
#
# Idempotent; invoked from bootstrap.sh as root with zero or more server IPs
# to ignore. Safe to re-run; re-running with a different list replaces it.
#
# Why this exists: this LAN has two DHCP servers on ONE bridged L2 segment --
# the house router on 192.168.0.0/24 and the rig's router on 192.168.8.0/24 --
# and dhclient takes whichever OFFER arrives first. The rig Pi kept losing, so
# it sat on the house subnet with no route to the cubes (192.168.8.{20+N}) or
# to the broker's service address, while its SSID and signal looked perfect.
# Measured on the rig before this: every lease since the machine was imaged
# came from the house server, never once from the rig's.
#
# `reject` is dhclient's own answer -- it discards OFFERs and ACKs from the
# listed servers, so the remaining one wins. Observed with it in place:
#
#   DHCPOFFER from 192.168.0.1 rejected by rule 192.168.0.1 mask 255.255...
#   DHCPOFFER of 192.168.8.129 from 192.168.8.1
#   DHCPACK of 192.168.8.129 from 192.168.8.1
#
# Not a substitute for fixing the LAN -- two DHCP servers on one segment is
# the actual defect -- but that is the house network, and this is the one
# machine that cannot tolerate losing the race.
#
# This writes configuration only; it does NOT re-lease. bootstrap.sh often runs
# over SSH on the very interface that would drop, and a lease change mid-run is
# how you lose the session and the remaining steps with it. The rule takes
# effect at the next renewal or reboot.
#
# dhclient.conf has no `include` directive, so this owns a marked block inside
# the distro's file rather than dropping in a fragment.
# ============================================================================
set -euo pipefail

CONF=${DHCLIENT_CONF:-/etc/dhcp/dhclient.conf}
BEGIN="# >>> pi-deploy dhcp-preference >>>"
END="# <<< pi-deploy dhcp-preference <<<"

echo "=== DHCP server preference ==="

if [[ ! -f "$CONF" ]]; then
    echo "  $CONF not present; nothing to configure"
    exit 0
fi

# Validate before touching anything. A malformed directive makes dhclient exit
# on startup, which on a WiFi-only box means no network and no way back in --
# strictly worse than losing a DHCP race.
# python3 rather than a regex: a pattern loose enough to read accepts
# 192.168.0.256, and dhclient then refuses the file outright --
# "line 1: 256 exceeds max (255) for precision", verified against 4.4.3-P1 --
# which is the exact no-network failure this check exists to prevent.
# ipaddress also rejects leading-zero and short forms. python3 is already a
# hard dependency below.
#
# Narrower than the directive on purpose: dhclient accepts a CIDR mask
# (`reject 192.168.0.0/16;`) and this does not. Add it here deliberately if a
# rig ever needs one, rather than by loosening the check.
for server in "$@"; do
    if ! python3 -c 'import ipaddress, sys; ipaddress.IPv4Address(sys.argv[1])' "$server" 2>/dev/null; then
        echo "  refusing to write '$server': not an IPv4 address" >&2
        exit 1
    fi
done

block=""
if (( $# )); then
    block="$BEGIN"$'\n'
    for server in "$@"; do
        block+="reject $server;"$'\n'
    done
    block+="$END"
fi

python3 - "$CONF" "$BEGIN" "$END" "$block" <<'PY'
import sys

path, begin, end, block = sys.argv[1:5]
text = open(path, encoding="utf-8").read()

start, stop = text.find(begin), text.find(end)
if start != -1 and stop != -1:
    # Replace the previous block wholesale, so a changed list does not append.
    text = text[:start] + text[stop + len(end):]
text = text.rstrip("\n") + "\n"
if block:
    text += "\n" + block + "\n"

open(path, "w", encoding="utf-8").write(text)
PY

if (( $# )); then
    echo "  ignoring DHCP offers from: $*"
    echo "  (applies at the next lease renewal or reboot)"
else
    echo "  no servers to ignore; any managed block removed"
fi
