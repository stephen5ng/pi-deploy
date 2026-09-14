#!/bin/bash
# ============================================================================
# network-interfaces.sh — prefer wired, keep WiFi as a fallback.
#
# Idempotent; invoked from bootstrap.sh as root. Three things:
#
#   1. Bring eth0 up at boot at all. DietPi ships `#allow-hotplug eth0`
#      commented out, so ifupdown never touches it and a plugged cable does
#      nothing -- the interface sits DOWN with an address-less NIC while the
#      Pi talks over WiFi.
#
#   2. Make wired the preferred path when present, WiFi the fallback. Two
#      routes matter, not one: dhclient honours `metric` for the DEFAULT route
#      (IF_METRIC, see /sbin/dhclient-script), but the cubes are ON-LINK in the
#      same /24 and that is decided by the subnet route, which dhclient does not
#      set. Preferring only the default route leaves cube traffic on WiFi while
#      the wire idles -- measured: `ip route get <cube>` chose wlan0 until the
#      subnet metric was fixed too.
#
#   3. Stop the two interfaces fighting over ARP. Both hold an address in the
#      same /24, and Linux answers ARP for any local address on any interface by
#      default, so wlan0 would answer for a service address living on eth0 and
#      the router would learn the wrong MAC.
#
# Why wired matters here: every cube message crosses the air twice
# (cube -> AP -> Pi). Wiring the Pi removes one of the two hops for every
# message in both directions, and takes the Pi's own traffic off the 2.4GHz
# band the ESP32 cubes cannot leave.
# ============================================================================
set -euo pipefail

INTERFACES=/etc/network/interfaces
MARKER="# Managed by pi-deploy: wired preferred, WiFi fallback."
WIRED_METRIC=100
WIRELESS_METRIC=600

echo "=== Network interfaces: wired preferred, WiFi fallback ==="

# ---------------------------------------------------------------------------
# 1 + 2. The ifupdown stanzas.
#
# Rewritten in place rather than dropped into interfaces.d: the stanzas below
# already exist in the main file, and ifupdown rejects a duplicate `iface`
# definition, so a drop-in would break networking rather than override it.
# ---------------------------------------------------------------------------
if grep -qF "$MARKER" "$INTERFACES"; then
    echo "  [1/3] stanzas already managed"
else
    backup="${INTERFACES}.before-pi-deploy.$(date +%Y%m%d-%H%M%S)"
    cp -a "$INTERFACES" "$backup"
    echo "  [1/3] rewriting stanzas (backup: $backup)"

    # Keep anything that is not one of the two stanzas we own, so a hand-added
    # drop-in source line or a third interface survives.
    python3 - "$INTERFACES" "$MARKER" "$WIRED_METRIC" "$WIRELESS_METRIC" <<'PY'
import re
import sys

path, marker, wired_metric, wireless_metric = sys.argv[1:5]
text = open(path).read()

# Drop the existing eth0 and wlan0 stanzas entirely, comments included. The
# eth0 one carries static address/netmask/gateway lines under an `inet dhcp`
# method, which ifupdown ignores -- they read as configuration but are not.
text = re.sub(
    r"(?ms)^[ \t]*#?[ \t]*(?:auto|allow-hotplug)[ \t]+(eth0|wlan0)\b.*?"
    r"(?=^[ \t]*#?[ \t]*(?:auto|allow-hotplug)[ \t]+\S|\Z)",
    "",
    text,
)
# And any bare `iface eth0/wlan0` stanza not preceded by auto/allow-hotplug.
text = re.sub(
    r"(?ms)^[ \t]*iface[ \t]+(?:eth0|wlan0)[ \t]+inet\b.*?(?=^[ \t]*\S|\Z)",
    "",
    text,
)
text = text.rstrip() + "\n"

text += f"""
{marker}
# eth0 is brought up at boot (DietPi ships this commented out) and wins on
# metric. Cubes are on-link in this subnet, so the subnet route matters as much
# as the default one; 50-pi-deploy-route-metrics applies that half.
allow-hotplug eth0
iface eth0 inet dhcp
    metric {wired_metric}

# WiFi stays associated as the fallback path. Its higher metric means it only
# carries traffic when the wire is gone -- and with
# ignore_routes_with_linkdown (see reliability.sh) that switch is automatic.
allow-hotplug wlan0
iface wlan0 inet dhcp
    metric {wireless_metric}
    pre-up iw dev wlan0 set power_save off
    post-down iw dev wlan0 set power_save on
    wpa-conf /etc/wpa_supplicant/wpa_supplicant.conf
"""
open(path, "w").write(text)
PY
fi

# ---------------------------------------------------------------------------
# 2b. The subnet-route half, which dhclient cannot do.
#
# An if-up.d hook rather than a `post-up` line: it must also run when dhclient
# renews a lease and re-adds the route, not only on ifup.
# ---------------------------------------------------------------------------
echo "  [2/3] installing route-metric hook"
cat > /usr/local/sbin/pi-deploy-route-metrics <<EOF
#!/bin/sh
# Managed by pi-deploy: give each interface's on-link subnet route the same
# preference its default route has. dhclient sets IF_METRIC on the default
# route only, and on-link destinations -- every cube -- ignore it.
set -eu

apply() {
    iface=\$1
    metric=\$2
    [ -d "/sys/class/net/\$iface" ] || return 0
    ip -o -4 address show dev "\$iface" scope global | while read -r _ _ _ cidr _; do
        network=\$(ip -4 route list dev "\$iface" proto kernel scope link \\
            | awk '{print \$1; exit}')
        [ -n "\$network" ] || continue
        source_ip=\${cidr%%/*}
        ip route replace "\$network" dev "\$iface" proto kernel scope link \\
            src "\$source_ip" metric "\$metric" 2>/dev/null || true
        break
    done
}

apply eth0 $WIRED_METRIC
apply wlan0 $WIRELESS_METRIC
ip route flush cache 2>/dev/null || true
EOF
chmod 755 /usr/local/sbin/pi-deploy-route-metrics

cat > /etc/network/if-up.d/50-pi-deploy-route-metrics <<'EOF'
#!/bin/sh
# Managed by pi-deploy.
[ "$IFACE" = "lo" ] && exit 0
/usr/local/sbin/pi-deploy-route-metrics || true
exit 0
EOF
chmod 755 /etc/network/if-up.d/50-pi-deploy-route-metrics

# ---------------------------------------------------------------------------
# 3. ARP, for two interfaces in one subnet.
# ---------------------------------------------------------------------------
echo "  [3/3] scoping ARP to the interface that owns each address"
cat > /etc/sysctl.d/61-pi-deploy-arp.conf <<'EOF'
# Managed by pi-deploy (scripts/network-interfaces.sh).
#
# eth0 and wlan0 both hold an address in the same /24. By default Linux answers
# ARP for ANY local address on ANY interface, so wlan0 would answer for a
# service address configured on eth0 and the router would cache the wrong MAC --
# traffic for that address then arrives on the WiFi path while the wire idles,
# or blackholes if WiFi is down.
#
# arp_ignore=1   answer only for addresses configured on the receiving interface
# arp_announce=2 source ARP requests from an address on the outgoing interface
net.ipv4.conf.all.arp_ignore = 1
net.ipv4.conf.all.arp_announce = 2
net.ipv4.conf.default.arp_ignore = 1
net.ipv4.conf.default.arp_announce = 2
EOF
sysctl -q --system

echo "=== Network interfaces configured ==="
