#!/bin/bash
set -euo pipefail

if [[ $# -ne 5 ]]; then
    echo "Usage: $0 INTERFACE ADDRESS[/PREFIX] INTERFACES_FILE BACKUP_FILE APP_NAME" >&2
    exit 2
fi

interface=$1
service_address=$2
interfaces_file=$3
backup_file=$4
app_name=$5
address_service="${app_name}-address.service"

echo "Migrating $interface to DHCP..."

# The persistent configuration has already been changed to DHCP. Run detached
# from bootstrap because removing the old primary address may interrupt SSH.
ifdown --force "$interface" || true
ip address del "$service_address" dev "$interface" 2>/dev/null || true

if ifup "$interface"; then
    systemctl restart "$address_service"
    systemctl restart "$app_name.service"
    echo "$interface is using DHCP; $service_address is now managed by $address_service"
    exit 0
fi

echo "DHCP failed on $interface; restoring the previous network configuration" >&2
install -m 644 "$backup_file" "$interfaces_file"
ifdown --force "$interface" || true
ifup "$interface"
systemctl restart "$address_service" || true
systemctl restart "$app_name.service" || true
exit 1
