#!/bin/bash
set -euo pipefail

usage() {
    echo "Usage: $0 {start|stop} ADDRESS[/PREFIX] [INTERFACE]" >&2
    exit 2
}

[[ $# -ge 2 && $# -le 3 ]] || usage

action=$1
address_with_prefix=$2
configured_interface=${3:-auto}
address=${address_with_prefix%%/*}
state_directory=/run/service-address
state_file="$state_directory/$address"

find_address_interface() {
    ip -o -4 address show \
        | awk -v target="$address" \
            '!found && ($4 == target || index($4, target "/") == 1) { print $2; found = 1 }'
}

find_route_interface() {
    ip -4 route get "$address" \
        | awk '{ for (i = 1; i <= NF; i++) if ($i == "dev") { print $(i + 1); exit } }'
}

case "$action" in
    start)
        existing_interface=$(find_address_interface)
        if [[ -n "$existing_interface" ]]; then
            echo "$address is already configured on $existing_interface; leaving it unmanaged"
            exit 0
        fi

        if [[ "$configured_interface" == "auto" ]]; then
            interface=$(find_route_interface)
        else
            interface=$configured_interface
        fi

        if [[ -z "$interface" ]]; then
            echo "Unable to determine the network interface for $address" >&2
            exit 1
        fi

        if command -v arping >/dev/null 2>&1; then
            max_retries=10
            retry_count=0
            arping_status=0

            while [[ $retry_count -lt $max_retries ]]; do
                arping_status=0
                arping -D -q -c 2 -I "$interface" "$address" || arping_status=$?

                if [[ $arping_status -eq 0 ]]; then
                    break
                elif [[ $arping_status -eq 1 ]]; then
                    echo "Refusing to claim $address: another host answered ARP on $interface" >&2
                    exit 1
                else
                    echo "arping failed with status $arping_status (link not ready?); retrying in 2 seconds..." >&2
                    sleep 2
                    ((retry_count++))
                fi
            done

            if [[ $arping_status -ne 0 ]]; then
                echo "Failed to verify address uniqueness after $max_retries retries (arping status $arping_status)" >&2
                exit 1
            fi
        fi

        ip address add "$address_with_prefix" dev "$interface"
        mkdir -p "$state_directory"
        touch "$state_file"
        echo "Added service address $address_with_prefix to $interface"

        if command -v arping >/dev/null 2>&1; then
            arping -U -q -c 3 -I "$interface" "$address" || true
        fi
        ;;
    stop)
        if [[ ! -e "$state_file" ]]; then
            echo "$address was not added by this service; leaving it unchanged"
            exit 0
        fi

        interface=$(find_address_interface)
        if [[ -z "$interface" ]]; then
            echo "$address is not configured"
            rm -f "$state_file"
            exit 0
        fi

        ip address del "$address_with_prefix" dev "$interface"
        rm -f "$state_file"
        echo "Removed service address $address_with_prefix from $interface"
        ;;
    *)
        usage
        ;;
esac
