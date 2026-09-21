#!/bin/bash
# ============================================================================
# mdns.sh — publish this Pi's hostname on the LAN over mDNS.
#
# Idempotent; invoked from bootstrap.sh as root (sudo ./bootstrap.sh).
#
# Why this exists: the Pi takes its address by DHCP, and on a LAN with more
# than one DHCP server the lease -- and therefore the subnet -- is not stable
# between boots. `ssh dietpi@<remembered address>` then fails with no
# indication of where the box went, and finding it means sweeping a /24.
# avahi publishes <hostname>.local from the system hostname (DietPi sets it
# from AUTO_SETUP_NET_HOSTNAME), so `ssh dietpi@lexacube.local` keeps working
# across a lease change.
#
# Only avahi-daemon is installed, deliberately. Being *discoverable* needs the
# daemon alone; resolving OTHER hosts' .local names additionally needs
# libnss-mdns and an nsswitch.conf edit, which nothing here wants.
#
# Known, unquantified cost: avahi announces over multicast, and on this rig
# that lands on the 2.4GHz band the single-band ESP32 cubes cannot leave --
# the same airtime scripts/wifi-preference.sh exists to protect. The traffic
# is small (periodic announcements, not a stream) and has not been measured
# on the rig. Debian's avahi-daemon.conf already ships publish-workstation=no,
# so no tuning is applied here; if the airtime ever needs accounting for,
# measure first rather than guessing at settings.
#
# Gotcha worth knowing before debugging a dead daemon: avahi refuses to start
# when the system's own search domain is "local", because it cannot own a
# name it would also have to resolve upstream. The rig's DHCP hands out
# domain "lan", so this does not bite today.
# ============================================================================
set -euo pipefail

echo "=== mDNS hostname publishing ==="

installed=false
if dpkg-query -W -f='${Status}' avahi-daemon 2>/dev/null | grep -q "^install ok installed$"; then
    echo "  avahi-daemon already installed"
    installed=true
else
    echo "  Installing avahi-daemon..."
    # `apt-get update` runs only on this branch, unlike bootstrap.sh's own apt
    # calls which pay it every time: a re-bootstrap on a box that already has
    # the daemon should not spend a network round-trip to learn nothing.
    if apt-get update && apt-get install -y --no-install-recommends avahi-daemon; then
        installed=true
    else
        echo "  WARNING: could not install avahi-daemon; .local will not resolve" >&2
    fi
fi

if [[ "$installed" == true ]]; then
    # --now matters as much as enable: an installed-but-stopped daemon
    # publishes nothing and says nothing about it -- the name simply does not
    # resolve, which looks identical to avahi never having been installed.
    if systemctl enable --now avahi-daemon; then
        echo "  $(hostname).local published"
    else
        echo "  WARNING: avahi-daemon did not start; .local will not resolve" >&2
    fi
fi

# Resolving by name is a convenience, not a prerequisite for the rig running,
# so this never fails the bootstrap -- scripts/wifi-preference.sh takes the
# same line. It matters more here than there: bootstrap.sh runs under
# `set -euo pipefail` and calls each of these steps bare, so a non-zero exit
# would abort the run before wifi-preference.sh, the step this one is
# deliberately ordered ahead of.
exit 0
