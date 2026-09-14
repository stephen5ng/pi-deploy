#!/bin/bash
# ============================================================================
# wifi-preference.sh — make the Pi prefer one configured WiFi network.
#
# Idempotent; invoked from bootstrap.sh as root with the preferred SSID.
#
# Why this exists: DietPi's own generator (/boot/dietpi/func/dietpi-wifidb)
# writes ssid, scan_ssid, key_mgmt and psk into each wpa_supplicant network
# block and nothing else -- in particular no `priority=`. With two networks
# configured, wpa_supplicant then picks on signal strength, and at any real
# distance the 2.4GHz radio wins. That is exactly backwards here: 2.4GHz is the
# band the single-band ESP32 cubes cannot leave, and every watt of Pi traffic
# on it is airtime taken from them. Moving the Pi to 5GHz measurably improved
# cube responsiveness.
#
# What this does NOT fix: DietPi rewrites wpa_supplicant.conf wholesale
# whenever its WiFi settings are changed (dietpi-config), which drops these
# priorities until the next bootstrap. The credentials survive, because they
# live in dietpi-wifi.db; only the preference is lost. That is a far smaller
# hole than the one it replaces, where the 5GHz network was hand-written into
# wpa_supplicant.conf and a regenerate removed it outright.
#
# Note psk is PBKDF2(passphrase, SSID), so the same password hashes
# differently per SSID. This script never touches psk -- dietpi-wifidb derives
# each one with `wpa_passphrase "$ssid" "$key"`, which gets that right.
# ============================================================================
set -euo pipefail

PREFERRED_SSID="${1-}"
SUPPLICANT=/etc/wpa_supplicant/wpa_supplicant.conf
PREFERRED_PRIORITY=10
OTHER_PRIORITY=1

if [[ -z "$PREFERRED_SSID" ]]; then
    echo "  no preferred WiFi network configured; leaving priorities alone"
    exit 0
fi
if [[ ! -f "$SUPPLICANT" ]]; then
    echo "  $SUPPLICANT not present; nothing to prioritise"
    exit 0
fi

echo "=== WiFi preference: $PREFERRED_SSID ==="

status=0
{
python3 - "$SUPPLICANT" "$PREFERRED_SSID" "$PREFERRED_PRIORITY" "$OTHER_PRIORITY" <<'PY'
import re
import sys

path, preferred, preferred_priority, other_priority = sys.argv[1:5]
text = open(path, encoding="utf-8").read()

# Each `network={ ... }` block, matched whole so a priority line can be set
# inside it without disturbing the rest of the file (country=, ctrl_interface=,
# update_config= and any comments all stay exactly as DietPi wrote them).
BLOCK = re.compile(r"network=\{.*?\n\}", re.S)
SSID = re.compile(r'^\s*ssid="(.*)"\s*$', re.M)
PRIORITY = re.compile(r"^\s*priority=.*\n?", re.M)

seen = []
changed = 0


def apply(match):
    global changed
    block = match.group(0)
    ssid_match = SSID.search(block)
    if ssid_match is None:
        return block
    ssid = ssid_match.group(1)
    seen.append(ssid)
    wanted = preferred_priority if ssid == preferred else other_priority

    stripped = PRIORITY.sub("", block)
    # Insert before the closing brace, indented like DietPi's own directives.
    updated = stripped[: stripped.rindex("\n}")] + f"\n\tpriority={wanted}\n}}"
    if updated != block:
        changed += 1
    return updated


result = BLOCK.sub(apply, text)

if preferred not in seen:
    # Not an error: the network may simply not be configured on this rig. But
    # it is worth saying, because the silent outcome is a Pi that never uses
    # the band it was told to prefer.
    print(
        f"  WARNING: preferred SSID {preferred!r} is not in {path}; "
        f"configured networks are {seen or ['none']}",
        file=sys.stderr,
    )

if result != text:
    open(path, "w", encoding="utf-8").write(result)

for ssid in seen:
    mark = "preferred" if ssid == preferred else "fallback"
    print(f"  {ssid}: {mark}")
print(f"  {changed} block(s) updated")

# 10 means "the file changed". The caller reloads wpa_supplicant only then:
# reloading unconditionally would bounce WiFi on every bootstrap, and on a
# WiFi-only rig that is the path bootstrap is running over.
sys.exit(10 if changed else 0)
PY
} || status=$?

if (( status != 0 && status != 10 )); then
    exit "$status"
fi

if (( status == 0 )); then
    echo "=== WiFi preference already applied ==="
    exit 0
fi

# ---------------------------------------------------------------------------
# The file is only half of it. A running wpa_supplicant holds its network
# configuration in memory and never re-reads the file on its own, so without
# this an existing Pi prints "applied" and stays on the other band until it
# happens to reboot. Only reached when the file actually changed.
# ---------------------------------------------------------------------------
WPA_CLI=$(command -v wpa_cli || echo /sbin/wpa_cli)
if [[ ! -x "$WPA_CLI" ]]; then
    echo "  WARNING: wpa_cli not found; the new priorities apply at next reboot" >&2
    exit 0
fi

managed=""
for wireless in /sys/class/net/*/wireless; do
    [[ -e "$wireless" ]] || continue
    iface=$(basename "$(dirname "$wireless")")
    # Only interfaces wpa_supplicant is actually driving answer a ping, so this
    # skips an AP-mode or unmanaged radio without having to guess names.
    "$WPA_CLI" -i "$iface" ping >/dev/null 2>&1 || continue
    managed="$managed $iface"
    if "$WPA_CLI" -i "$iface" reconfigure >/dev/null 2>&1; then
        echo "  reloaded wpa_supplicant on $iface"
    else
        echo "  WARNING: could not reload wpa_supplicant on $iface" >&2
    fi
done

if [[ -z "$managed" ]]; then
    echo "  no wpa_supplicant-managed interface; priorities apply at next boot"
    exit 0
fi

# Verify rather than assume. Re-association takes a moment, and it may not
# happen at all -- the preferred network can simply be out of range here, which
# is a note and not a failure.
for iface in $managed; do
    current=""
    for _ in $(seq 1 15); do
        current=$("$WPA_CLI" -i "$iface" status 2>/dev/null | sed -n 's/^ssid=//p')
        [[ "$current" == "$PREFERRED_SSID" ]] && break
        sleep 1
    done
    if [[ "$current" == "$PREFERRED_SSID" ]]; then
        echo "  $iface: associated to $current"
    else
        echo "  $iface: on '${current:-nothing}', not $PREFERRED_SSID -- expected"
        echo "          if that network is out of range here; it will be"
        echo "          preferred whenever both are available"
    fi
done

echo "=== WiFi preference applied ==="
