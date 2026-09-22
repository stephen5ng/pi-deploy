#!/bin/bash
# ============================================================================
# network-interfaces.sh — prefer wired, keep WiFi as a fallback.
#
# Idempotent; invoked from bootstrap.sh as root. Four things:
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
#   4. Make the WiFi path self-healing at boot. ifupdown starts
#      wpa_supplicant in pre-up, and we have measured it failing there --
#      `daemon failed to start`, ifup aborting, wlan0 left down, the box
#      unreachable until someone drove it from the console. Reproduced across
#      two reboots.
#
#      It is timing, not the invocation. Run verbatim on the same machine once
#      settled -- `-s -B -P /run/wpa_supplicant.wlan0.pid -i wlan0
#      -D nl80211,wext -c ...` -- it exits 0, writes its pid file and
#      associates. Same arguments, same config, same box; only the moment
#      differs. (An earlier comparison here used FEWER arguments and so proved
#      nothing; this one is the wrapper's own command line.)
#
#      What makes boot different is still unknown -- most likely the brcmfmac
#      firmware not being ready that early, but unproven, because the
#      boot-time failure logs nothing at all. Hence a oneshot unit that waits
#      for the standard path, starts the daemon itself if it never came up,
#      and says so in the journal: a no-op on every normal boot, and the first
#      durable evidence on a bad one.
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
    echo "  [1/4] stanzas already managed"
else
    backup="${INTERFACES}.before-pi-deploy.$(date +%Y%m%d-%H%M%S)"
    cp -a "$INTERFACES" "$backup"
    echo "  [1/4] rewriting stanzas (backup: $backup)"

    # Keep anything that is not one of the two stanzas we own, so a hand-added
    # drop-in source line or a third interface survives.
    python3 - "$INTERFACES" "$MARKER" "$WIRED_METRIC" "$WIRELESS_METRIC" <<'PY'
import sys

path, marker, wired_metric, wireless_metric = sys.argv[1:5]

OWNED = ("eth0", "wlan0")
# Keywords that take a list of interface names.
ALLOW_KEYWORDS = ("auto", "allow-auto", "allow-hotplug")
# Every ifupdown keyword that begins a top-level stanza. A column-0 line
# starting with anything else is left exactly where it is.
STANZA_KEYWORDS = ALLOW_KEYWORDS + (
    "iface", "mapping", "source", "source-directory",
    "no-auto-down", "no-scripts",
)


def stanza_fields(line):
    """The fields of a top-level stanza line, or None if this is not one.

    Comments are never stanza heads. A commented-out directive is inert: it
    cannot duplicate the stanzas this script appends, so there is nothing to
    gain by deleting it and a whole class of mis-parses to avoid by leaving
    every comment exactly where it is.
    """
    if not line.strip() or line[:1] in (" ", "\t", "#"):
        return None
    fields = line.split()
    return fields if fields[0] in STANZA_KEYWORDS else None


# Parsed line by line rather than by regex. Two stanzas have to be removed
# whole from a file that may contain a third interface nobody told us about,
# and a regex bounded by "the next auto/allow-hotplug line" gets that wrong
# twice: it swallows a following bare `iface usb0` stanza, and for a bare
# `iface eth0` it removes the head while orphaning the indented options at top
# level. An orphaned option line is a malformed interfaces file, so that one
# breaks more than the interface it came from.
#
# The two keyword shapes must not be conflated. `iface <name> <family>
# <method>` names exactly one interface; `auto`/`allow-hotplug` take a list.
# Reading `inet`/`dhcp` as interface names leaves the `iface` stanza in place
# while removing its `allow-hotplug`, and a duplicate `iface` is rejected by
# ifupdown outright -- no networking at all, rather than one interface short.
kept = []
dropping = False
for line in open(path).read().splitlines():
    fields = stanza_fields(line)
    if fields is not None:
        keyword = fields[0]
        if keyword in ALLOW_KEYWORDS:
            # No indented options follow an allow line, so this never starts a
            # drop. A line naming other interfaces as well is rewritten rather
            # than removed, so `auto eth0 usb0` does not cost usb0 its boot.
            remaining = [name for name in fields[1:] if name not in OWNED]
            if len(remaining) != len(fields) - 1:
                if remaining:
                    kept.append(" ".join([keyword] + remaining))
            else:
                kept.append(line)
            dropping = False
            continue
        dropping = keyword == "iface" and len(fields) > 1 and fields[1] in OWNED
        if dropping:
            continue
        kept.append(line)
        continue
    if dropping:
        # Still inside an owned stanza. Every line that is not itself a stanza
        # head belongs to it, indentation or not: DietPi's own file puts
        # `address`/`netmask`/`gateway` at column 0, so treating indentation as
        # the marker of an option line would strand exactly the directives this
        # rewrite exists to remove. The boundary is the next stanza head -- now
        # including a bare `iface`, which is what the regex missed.
        #
        # A comment introducing the *following* stanza is swallowed with it.
        # That is cosmetic: comments are inert, and the stanza itself survives.
        continue
    kept.append(line)

text = "\n".join(kept).rstrip() + "\n"

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
# Installed in two places, because neither covers the other. An if-up.d hook
# rather than a `post-up` line, so it applies to every ifup; AND a dhclient
# exit hook, because if-up.d is run by ifup and not by dhclient at all. An
# earlier version of this comment claimed the one covered lease renewals. It
# does not -- see the exit hook below for what actually happens on a renewal
# that changes the address.
# ---------------------------------------------------------------------------
echo "  [2/4] installing route-metric hook"
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
        # Remove every existing kernel/link route for this prefix on this
        # device, then install one at the metric we want.
        #
        # Not "ip route replace": metric is part of the route key, so replacing
        # a metric-0 route with a metric-100 one ADDS a second route and leaves
        # the original. The kernel creates that metric-0 route automatically
        # whenever an address is assigned, on both interfaces, so after a boot
        # the table held FOUR routes for this prefix -- kernel metric-0 on eth0
        # AND wlan0, plus this function's 100 and 600. The metric-0 pair
        # outranks both of ours, they tie with each other, and which one wins
        # is insertion order at boot rather than anything configured here.
        # Measured on the rig after a reboot: it happened to pick eth0, with
        # nothing guaranteeing the next boot does.
        #
        # Nor can the metric-0 route be deleted on its own. iproute2 treats an
        # unspecified metric and an explicit "metric 0" alike as "match any",
        # so both forms delete whichever route is found first -- verified by
        # deleting wlan0's metric-600 route twice while trying to remove a
        # metric-0 one that did not exist. Deleting until none remain is the
        # only selective-enough operation available.
        #
        # The gap between the last delete and the add is microseconds, and the
        # other interface's route for the same prefix covers it.
        attempts=0
        while ip route del "\$network" dev "\$iface" proto kernel scope link \\
                2>/dev/null; do
            attempts=\$((attempts + 1))
            [ "\$attempts" -gt 10 ] && break
        done
        ip route add "\$network" dev "\$iface" proto kernel scope link \\
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

# ...and again from a dhclient exit hook, which is the path that actually
# covers a renewal.
#
# /etc/network/if-up.d is run by ifup. It is NOT run by dhclient: the Debian
# /sbin/dhclient-script handles RENEW and REBIND itself and calls
# /etc/dhcp/dhclient-exit-hooks.d instead -- verified on the rig, the string
# "if-up.d" does not appear in that script at all. An earlier comment here
# claimed if-up.d covered renewals. It does not.
#
# It matters on the path where the lease comes back with a different address.
# dhclient-script then runs `ip -4 addr flush dev $interface label $interface`
# and re-adds, which destroys the metric route installed above and leaves the
# kernel's fresh metric-0 one in its place -- so the wired preference silently
# reverts to a coin toss until the next ifup. The common renewal, where the
# address is unchanged, takes an `ip addr change` path that leaves routes
# alone, which is why this has not been noticed.
mkdir -p /etc/dhcp/dhclient-exit-hooks.d
cat > /etc/dhcp/dhclient-exit-hooks.d/50-pi-deploy-route-metrics <<'EOF'
# Managed by pi-deploy (scripts/network-interfaces.sh).
#
# SOURCED by /sbin/dhclient-script (`. $script`), not executed: use `return`,
# never `exit`, or dhclient-script terminates here and skips everything after.
# No shebang and mode 644 to match the other hooks in this directory.
case "$reason" in
    BOUND|RENEW|REBIND|REBOOT)
        if [ -x /usr/local/sbin/pi-deploy-route-metrics ]; then
            /usr/local/sbin/pi-deploy-route-metrics || true
        fi
        ;;
esac
# dhclient-script logs a daemon.err for any non-zero status a hook leaves
# behind, and the case above falls through with whatever the last test set.
true
EOF
chmod 644 /etc/dhcp/dhclient-exit-hooks.d/50-pi-deploy-route-metrics

# ---------------------------------------------------------------------------
# 3. ARP, for two interfaces in one subnet.
# ---------------------------------------------------------------------------
echo "  [3/4] scoping ARP to the interface that owns each address"
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

# ---------------------------------------------------------------------------
# 4/4. WiFi self-healing. See header item 4: ifupdown's pre-up starts
# wpa_supplicant as a daemon, and on this rig that has failed at boot with the
# interface left down and nothing in the logs -- while that same command line,
# run verbatim once the machine has settled, exits 0 and associates. So the
# invocation is sound and the moment is not; what differs about boot is not
# yet established.
#
# This unit is the witness, not the fix: it waits for the standard path to
# produce a wpa_supplicant on wlan0, and only if it never does, starts the
# same daemon the wrapper would have, then leases. On every normal boot it is
# a no-op that says so in the journal.
# ---------------------------------------------------------------------------
echo "  [4/4] installing WiFi boot rescue"
cat > /usr/local/sbin/pi-deploy-wifi-rescue <<'EOF'
#!/bin/sh
# Managed by pi-deploy. One-shot: no-op if ifupdown got wpa_supplicant onto
# wlan0 within the wait, otherwise start the daemon and lease. Tag:
# pi-deploy-wifi-rescue.
LOG="pi-deploy-wifi-rescue:"
for i in $(seq 1 24); do
    if pgrep -f "wpa_supplicant.*wlan0" >/dev/null 2>&1; then
        echo "$LOG standard wpa_supplicant present; nothing to do"
        exit 0
    fi
    sleep 5
done
echo "$LOG no wpa_supplicant after 120s; starting one"
ip link set wlan0 up
# Same invocation Debian's /etc/network/if-pre-up.d/wpasupplicant uses, so the
# rescue reproduces the intended configuration rather than a variant of it.
# Resolved via PATH with the package's location as fallback, matching
# wifi-preference.sh's wpa_cli handling.
WPA_SUPPLICANT=$(command -v wpa_supplicant || echo /usr/sbin/wpa_supplicant)
# -s: log to syslog. Under -B nothing else reaches the journal, and the one
# boot this daemon runs on is the boot whose evidence is worth having.
"$WPA_SUPPLICANT" -s -B -P /run/wpa_supplicant.wlan0.pid \
    -i wlan0 -D nl80211,wext -c /etc/wpa_supplicant/wpa_supplicant.conf
sleep 5
if ! ip -4 -o a show wlan0 | grep -q inet; then
    # -e IF_METRIC: ifupdown passes the stanza's `metric` to dhclient this way,
    # and dhclient-script is the only thing that puts a metric on the default
    # route. A bare `dhclient` leaves it at 0 -- ahead of eth0's 100 -- so a
    # rescued boot would send internet traffic over WiFi while the wire idles,
    # inverting the preference section 1+2 of this script exists to set. The
    # literal must track WIRELESS_METRIC above; the quoted heredoc cannot
    # expand it, so a test asserts the two agree.
    #
    # -pf/-lf name the same pid and lease files ifupdown's dhcp method uses, so
    # a later `ifdown wlan0` can still find and stop this client.
    dhclient -e IF_METRIC=600 \
        -pf /run/dhclient.wlan0.pid -lf /var/lib/dhcp/dhclient.wlan0.leases \
        wlan0
fi
ip -4 -br a show wlan0
EOF
chmod 755 /usr/local/sbin/pi-deploy-wifi-rescue

cat > /etc/systemd/system/pi-deploy-wifi-rescue.service <<'EOF'
[Unit]
Description=Start wpa_supplicant if ifupdown failed to (pi-deploy)
# WantedBy alone, deliberately: ordering After=multi-user.target would make
# this wait for a target whose start job can stay pending on this rig -- the
# service address loops by design while eth0 has no carrier (pi-deploy #40).
[Service]
Type=oneshot
# RemainAfterExit + KillMode=process, because this oneshot's whole job is to
# leave a daemon running behind it.
#
# Type=oneshot defaults to KillMode=control-group: when the main process (the
# script) exits, systemd tears down the service cgroup and kills everything
# still in it -- including the `-B` wpa_supplicant the script just started.
# Measured on the rig, from the unit's own journal:
#
#     20:47:13 wlan0: CTRL-EVENT-CONNECTED - Connection to 96:83:c4:6b:f2:91 completed
#     20:47:15 pi-deploy-wifi-rescue[1255]: wlan0  UP  192.168.8.129/24
#     20:47:15 wlan0: CTRL-EVENT-TERMINATING
#     20:47:15 pi-deploy-wifi-rescue.service: Deactivated successfully.
#
# The rescue associated in three seconds and systemd killed it two seconds
# later, then reported status=0/SUCCESS -- so the unit looked like it had never
# run while the box sat offline for the whole boot.
#
# RemainAfterExit keeps the unit active once the script exits, so the cgroup is
# not collected; KillMode=process bounds an eventual stop to the main process,
# which has already exited, rather than to everything the service spawned.
RemainAfterExit=yes
KillMode=process
ExecStart=/usr/local/sbin/pi-deploy-wifi-rescue
[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable pi-deploy-wifi-rescue.service 2>/dev/null || true
