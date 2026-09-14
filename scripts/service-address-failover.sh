#!/bin/bash
# ============================================================================
# service-address-failover — keep a service address on an interface that works.
#
# Usage: service-address-failover ADDRESS[/PREFIX] OWNER_UNIT [INTERVAL_SEC]
#
# The problem this solves: a service address is a secondary alias pinned to one
# interface. If that interface loses carrier the alias stays put, so the address
# is advertised on a dead NIC. The box may still have a perfectly good path over
# another interface, but every client of that address is offline. lexacube hits
# this hard because all six cubes hardcode 192.168.8.247.
#
# Nothing existing covers it. /etc/network/if-up.d/50-<app>-address re-claims the
# address only when it is missing from *every* interface, and losing carrier is
# not an ifup event at all.
#
# The fix is deliberately small: notice that the interface holding the address
# has lost carrier, then hand the address back to `service-address`, whose `auto`
# mode re-picks the interface with `ip route get`. Since reliability.sh sets
# ignore_routes_with_linkdown, routing already skips link-down interfaces, so
# `auto` lands on one that works. This keeps interface selection in one place
# rather than reimplementing it here.
#
# Scope: this fails OVER, it does not fail BACK. When the cable returns the
# address stays where it is until the app restarts. That is deliberate -- a
# watcher that chases the "best" interface flaps the address, and every move
# drops all MQTT sessions. Coming back to the wire is a decision, not a reflex.
# ============================================================================
set -u

ADDRESS_WITH_PREFIX=${1:?usage: service-address-failover ADDRESS[/PREFIX] OWNER_UNIT [INTERVAL]}
OWNER_UNIT=${2:?usage: service-address-failover ADDRESS[/PREFIX] OWNER_UNIT [INTERVAL]}
INTERVAL=${3:-5}
ADDRESS=${ADDRESS_WITH_PREFIX%%/*}
HELPER=/usr/local/sbin/service-address

log() { echo "service-address-failover: $*"; }

# Which interface currently holds the address (empty if none does).
holder() {
    ip -o -4 address show \
        | awk -v target="$ADDRESS" \
            '!found && ($4 == target || index($4, target "/") == 1) { print $2; found = 1 }'
}

# Carrier is only meaningful once the interface is administratively up; a down
# interface reports nothing readable, which we treat as "not usable".
has_carrier() {
    local iface=$1
    [[ -r "/sys/class/net/$iface/carrier" ]] || return 1
    [[ "$(cat "/sys/class/net/$iface/carrier" 2>/dev/null)" == "1" ]]
}

log "watching $ADDRESS_WITH_PREFIX (owner $OWNER_UNIT, every ${INTERVAL}s)"

while sleep "$INTERVAL"; do
    # Only act while the owning unit still claims the address. A stopped app
    # must not have its address resurrected on another interface.
    systemctl is-active --quiet "$OWNER_UNIT" || continue

    current=$(holder)
    [[ -n "$current" ]] || continue          # not configured; the ifup hook covers that
    has_carrier "$current" && continue       # still fine

    log "$current lost carrier while holding $ADDRESS; re-homing"

    if ! "$HELPER" stop "$ADDRESS_WITH_PREFIX" auto; then
        log "could not release $ADDRESS from $current; will retry"
        continue
    fi

    if "$HELPER" start "$ADDRESS_WITH_PREFIX" auto; then
        log "$ADDRESS now on $(holder)"
    else
        # Leaving it unconfigured is safe: the owning unit is still active, so
        # the next ifup fires the reclaim hook, and this loop retries anyway.
        log "no usable interface for $ADDRESS yet; will retry"
    fi
done
