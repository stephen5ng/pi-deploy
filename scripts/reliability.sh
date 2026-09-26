#!/bin/bash
# ============================================================================
# reliability.sh — reliability & observability hardening for the Pi.
#
# Idempotent; invoked at the end of bootstrap.sh as root (sudo ./bootstrap.sh).
# Safe to re-run. Applies four independent measures:
#
#   1. Hardware watchdog   — auto-reboot a truly hung Pi in 15s (no more
#                            running to the machine to pull power).
#   2. Persistent journald — keep logs across reboots/crashes; DietPi's
#                            RAMlog is uninstalled because it clears them.
#   3. zram swap           — compressed-RAM safety valve so a RAM spike
#                            overflows instead of hanging the box.
#   4. Health logger       — poll firmware throttle/undervoltage into the
#                            (now persistent) journal for post-mortem.
#   5. WiFi monitor off    — DietPi's reconnect watchdog does more harm than
#                            good on a lossy link; see below.
#   6. Link-down routing   — let a dead cable fail over instead of blackholing.
#
# Context: this Pi drives a USB audio DAC + RS485 adapter + boot SSD off the
# 5V rail and suffered live-event hangs that needed a manual power-cycle.
# A bench load test (CPU peak + SSD write bursts) could NOT reproduce a
# brownout, so these measures make the box self-heal and self-diagnose
# rather than chasing an unproven power fault. See CLAUDE.md.
# ============================================================================
set -euo pipefail

echo "=== Reliability & observability hardening ==="

# ---------------------------------------------------------------------------
# 1. Hardware watchdog. systemd pets /dev/watchdog0; if pid1 stops petting it
#    (a total hang) the BCM2835 hardware resets the Pi after RuntimeWatchdogSec.
# ---------------------------------------------------------------------------
echo "[1/6] Arming hardware watchdog (15s)..."
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/10-watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=15
EOF
systemctl daemon-reexec

# ---------------------------------------------------------------------------
# 2. Persistent journald. DietPi ships /var/log as a RAMlog tmpfs, so journald
#    is volatile and a reboot erases the logs that would explain a hang. Bind
#    /var/log/journal to an SSD-backed dir so the journal persists, and
#    uninstall RAMlog, whose hourly clear empties it anyway. The x-systemd.before
#    ordering guarantees the bind is mounted before DietPi's ramlog service
#    touches /var/log; nofail keeps boot alive if the SSD is ever absent.
# ---------------------------------------------------------------------------
echo "[2/6] Configuring persistent journald..."
mkdir -p /var/lib/journal-persist
chown root:systemd-journal /var/lib/journal-persist
chmod 2755 /var/lib/journal-persist

mkdir -p /etc/systemd/journald.conf.d
cat > /etc/systemd/journald.conf.d/10-persistent.conf <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=1G
EOF

FSTAB_BIND='/var/lib/journal-persist /var/log/journal none bind,nofail,x-systemd.before=dietpi-ramlog.service 0 0'
if ! grep -qF '/var/lib/journal-persist /var/log/journal' /etc/fstab; then
    echo "$FSTAB_BIND" >> /etc/fstab
    echo "  Added journal bind mount to /etc/fstab"
else
    echo "  journal bind mount already present in /etc/fstab"
fi

mkdir -p /var/log/journal
if ! findmnt -M /var/log/journal >/dev/null 2>&1; then
    mount /var/log/journal
    echo "  Mounted /var/log/journal bind"
fi
systemctl restart systemd-journald
journalctl --flush || true

# The bind alone kept at most an hour. RAMlog's hourly cron runs
# `dietpi-logclear 1`, which truncates every file `find /var/log -type f`
# reaches -- and find descends into the bind mount, so each :17 emptied the
# journal, archives included. A full event day was lost that way. RAMlog goes:
# /var/log moves to disk, and nothing clears it.
#
# `dietpi-software uninstall 103` only schedules the tmpfs removal for the next
# boot (its disable.sh is what rewrites INDEX_LOGGING), so the index is also
# set here: the cron reads it every hour and stops clearing from this run on.
if [ -f /boot/dietpi/.installed ]; then
    sed -i 's/^INDEX_LOGGING=-[12]$/INDEX_LOGGING=0/' /boot/dietpi/.installed
    if grep -q '^aSOFTWARE_INSTALL_STATE\[103\]=2' /boot/dietpi/.installed; then
        if G_INTERACTIVE=0 /boot/dietpi/dietpi-software uninstall 103; then
            echo "  DietPi-RAMlog uninstalled; /var/log leaves tmpfs on next boot"
        else
            echo "  WARNING: DietPi-RAMlog uninstall failed; hourly clear is off, tmpfs remains"
        fi
    fi
fi

# ---------------------------------------------------------------------------
# 3. zram swap. No swap + cgroup_disable=memory means a RAM spike hangs the
#    whole box instead of getting one service OOM-killed. A compressed-RAM
#    swap (~50% of RAM, zero SSD wear) gives it somewhere to overflow.
#    DietPi's helper writes modules-load.d, the udev rule and the sysctl and
#    persists AUTO_SETUP_SWAPFILE_* to dietpi.txt. (Fresh flashes get this
#    from dietpi.template.txt: AUTO_SETUP_SWAPFILE_LOCATION=zram.)
# ---------------------------------------------------------------------------
echo "[3/6] Ensuring zram swap..."
if [[ "$(wc -l < /proc/swaps)" -le 1 ]]; then
    /boot/dietpi/func/dietpi-set_swapfile 1 zram
else
    echo "  swap already active, skipping"
fi

# ---------------------------------------------------------------------------
# 4. Health logger. Polls the firmware throttle/undervoltage bitmask + temp
#    into the (now persistent) journal. The "since boot" bits latch, so even
#    a sub-second brownout transient is caught on the next 5s poll. This is
#    the instrument that would confirm-or-exonerate a power fault under real
#    event load. Query it with: journalctl -t pi-health
# ---------------------------------------------------------------------------
echo "[4/6] Installing health logger..."
cat > /usr/local/bin/pi-health-watch.sh <<'EOF'
#!/usr/bin/env bash
# pi-health-watch: poll firmware throttle/undervoltage + temp into the
# (persistent) journal. Sticky "since boot" bits latch, so even a brief
# undervoltage transient is caught on the next poll. Tag: pi-health.
set -u
prev=""
tick=0
while true; do
  raw=$(vcgencmd get_throttled | sed 's/throttled=//')
  temp=$(vcgencmd measure_temp | sed 's/temp=//')
  val=$((raw))
  flags=""
  (( val & 0x1 ))     && flags="$flags UNDERVOLT_NOW"
  (( val & 0x2 ))     && flags="$flags FREQCAP_NOW"
  (( val & 0x4 ))     && flags="$flags THROTTLED_NOW"
  (( val & 0x8 ))     && flags="$flags TEMPLIMIT_NOW"
  (( val & 0x10000 )) && flags="$flags UNDERVOLT_SINCE_BOOT"
  (( val & 0x20000 )) && flags="$flags FREQCAP_SINCE_BOOT"
  (( val & 0x40000 )) && flags="$flags THROTTLED_SINCE_BOOT"
  (( val & 0x80000 )) && flags="$flags TEMPLIMIT_SINCE_BOOT"
  if [ "$raw" != "$prev" ] && [ "$raw" != "0x0" ]; then
    logger -t pi-health -p daemon.warning "throttled=$raw temp=$temp flags:$flags"
  fi
  if [ $((tick % 60)) -eq 0 ]; then
    logger -t pi-health -p daemon.info "heartbeat throttled=$raw temp=$temp"
  fi
  prev=$raw
  tick=$((tick+1))
  sleep 5
done
EOF
chmod +x /usr/local/bin/pi-health-watch.sh

cat > /etc/systemd/system/pi-health-watch.service <<'EOF'
[Unit]
Description=Pi health watch (throttle/undervoltage logger)
# Deliberately NOT After=multi-user.target, though WantedBy= puts it in that
# target. `WantedBy` is enough to pull the unit in; the extra ordering made it
# wait for the WHOLE target's start job, and on this rig that job can stay
# pending forever -- lexacube-address.service retries indefinitely by design
# (no cable = no claim, and Upholds= starts the app whenever the claim finally
# lands), which keeps multi-user.target's boot job "waiting" for good.
#
# Anything ordered after that target then blocks for good too, including this
# unit's own `systemctl enable --now` in bootstrap -- measured on the rig: the
# bootstrap hung here for minutes with no output, which is strictly worse than
# failing. Probed both ways against a wedged queue: with the ordering
# `systemctl start` timed out, without it returned 0.
#
# Nothing here needs the target anyway: the script polls vcgencmd and writes
# to the journal, and DefaultDependencies already orders it after
# sysinit.target, basic.target and systemd-journald.socket. Dropping the line
# also starts the logger earlier, so a boot-time undervoltage is caught.

[Service]
ExecStart=/usr/local/bin/pi-health-watch.sh
Restart=always
RestartSec=5
Nice=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable --now pi-health-watch.service

# ---------------------------------------------------------------------------
# 5. Disable DietPi's WiFi monitor.
#
#    dietpi-wifi-monitor.sh pings the default gateway ONCE every 10s and, on a
#    single lost reply, runs ifdown/ifup on the wireless interface. A one-packet
#    probe fails by chance on any lossy link, so it fires on ordinary packet
#    loss rather than on a hung adapter, and the remedy is far heavier than the
#    diagnosis: ifdown releases the DHCP lease, and a measured cycle took ~24s
#    from DHCPRELEASE to DHCPACK with no address configured at all. It also
#    flushes the interface, stripping lexacube's 192.168.8.247 -- which every
#    cube hardcodes -- so all six cubes drop at once.
#
#    Observed on this rig: 12 firings in one day on a link with 10-20% loss,
#    each a ~24s outage. wpa_supplicant reassociates on its own in ~7s, so the
#    watchdog only ever made things worse here.
#
#    Masked rather than merely disabled: dietpi-config's WiFi menu runs
#    `systemctl enable --now dietpi-wifi-monitor`, which would silently bring it
#    back; masking makes that fail loudly. The unit ships as a real file in
#    /etc/systemd/system, so it must be moved aside before mask can place its
#    /dev/null symlink.
# ---------------------------------------------------------------------------
echo "[5/6] Disabling DietPi WiFi monitor..."
wifi_monitor_unit=/etc/systemd/system/dietpi-wifi-monitor.service
wifi_monitor_backup=/opt/pi-deploy-disabled/dietpi-wifi-monitor.service

systemctl stop dietpi-wifi-monitor 2>/dev/null || true
systemctl disable dietpi-wifi-monitor 2>/dev/null || true

if [[ -f "$wifi_monitor_unit" && ! -L "$wifi_monitor_unit" ]]; then
    mkdir -p "$(dirname "$wifi_monitor_backup")"
    mv "$wifi_monitor_unit" "$wifi_monitor_backup"
    echo "  Moved the real unit aside to $wifi_monitor_backup"
    systemctl daemon-reload
fi

if [[ "$(systemctl is-enabled dietpi-wifi-monitor 2>/dev/null || true)" == "masked" ]]; then
    echo "  Already masked"
else
    systemctl mask dietpi-wifi-monitor
    echo "  Masked dietpi-wifi-monitor"
fi

# ---------------------------------------------------------------------------
# 6. Ignore routes on link-down interfaces.
#
#    When a cable is pulled the interface stays administratively UP with only
#    NO-CARRIER, and Linux keeps its routes by default -- so traffic is handed
#    to a dead NIC and blackholes instead of falling back. With this set the
#    kernel skips routes whose interface has lost carrier, so a wired Pi with
#    WiFi configured at a higher metric fails over on its own.
#
#    This also makes routing follow link state, which is what lets a service
#    address be re-homed by re-running the service-address helper: its `auto`
#    mode picks the interface via `ip route get`.
# ---------------------------------------------------------------------------
echo "[6/6] Ignoring routes on link-down interfaces..."
cat > /etc/sysctl.d/60-pi-deploy-linkdown.conf <<'SYSCTL'
# Managed by pi-deploy (scripts/reliability.sh).
# Skip routes whose interface has lost carrier, so a dead cable fails over to a
# higher-metric path instead of blackholing traffic on a NO-CARRIER interface.
net.ipv4.conf.all.ignore_routes_with_linkdown = 1
net.ipv4.conf.default.ignore_routes_with_linkdown = 1
SYSCTL
sysctl -q --system
echo "  all.ignore_routes_with_linkdown = $(cat /proc/sys/net/ipv4/conf/all/ignore_routes_with_linkdown 2>/dev/null || echo '?')"

echo "=== Reliability & observability hardening complete ==="
