#!/bin/bash
# Switch which member of an exclusive app group is active.
#
# Installed by bootstrap.sh as /usr/local/bin/pi-game. The systemd enable-state
# is the source of truth for which member is active -- there is no separate
# state file to drift -- so bootstrap preserves whatever this script sets.
#
#   pi-game            # show the current member and the alternatives
#   pi-game knockstrip # make knockstrip the active game, now and at boot
set -euo pipefail

# @CONFIG@ is replaced by bootstrap with the absolute path of the apps.yaml it
# deployed from, so the installed copy in /usr/local/bin stays pointed at the
# real checkout wherever that lives.
CONFIG="${PI_DEPLOY_CONFIG:-@CONFIG@}"
if [[ ! -f "$CONFIG" ]]; then
    echo "Cannot find apps.yaml at '$CONFIG' (override with PI_DEPLOY_CONFIG)." >&2
    exit 1
fi

group_members() {
    yq -r ".apps[] | select(.exclusive_group == \"$1\") | .name" "$CONFIG"
}

all_groups() {
    yq -r '.apps[].exclusive_group // empty' "$CONFIG" | sort -u
}

active_member() {
    local member
    for member in $(group_members "$1"); do
        if systemctl is-enabled "${member}.service" &>/dev/null; then
            echo "$member"
            return
        fi
    done
}

show_status() {
    local group member active state
    for group in $(all_groups); do
        active=$(active_member "$group")
        echo "Group '$group':"
        for member in $(group_members "$group"); do
            state=$(systemctl is-active "${member}.service" 2>/dev/null || true)
            if [[ "$member" == "$active" ]]; then
                echo "  * $member (enabled, $state)"
            else
                echo "    $member (disabled, $state)"
            fi
        done
    done
}

if [[ $# -eq 0 ]]; then
    show_status
    exit 0
fi

target=$1
target_group=$(yq -r ".apps[] | select(.name == \"$target\") | .exclusive_group // empty" "$CONFIG")
if [[ -z "$target_group" ]]; then
    echo "'$target' is not a member of any exclusive group. Known members:" >&2
    for group in $(all_groups); do
        group_members "$group" | sed 's/^/  /' >&2
    done
    exit 1
fi

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "Switching the active app requires root; rerun with sudo." >&2
    exit 1
fi

# Stop the others before starting the target. Conflicts= in the drop-in would
# handle this on its own, but doing it explicitly keeps the disable-state
# correct for the next boot.
for member in $(group_members "$target_group"); do
    if [[ "$member" != "$target" ]]; then
        systemctl disable "${member}.service" 2>/dev/null || true
        systemctl stop "${member}.service" 2>/dev/null || true
    fi
done

systemctl enable "${target}.service"
systemctl restart "${target}.service"

echo "Active member of '$target_group' is now $target."
echo ""
show_status
