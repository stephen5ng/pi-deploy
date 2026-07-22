#!/bin/bash
# ============================================================================
# reliability.sh — reliability & observability hardening for the Pi.
#
# Idempotent; invoked at the end of bootstrap.sh as root (sudo ./bootstrap.sh).
# Safe to re-run. Applies four independent measures:
#
#   1. Hardware watchdog   — auto-reboot a truly hung Pi in 15s (no more
#                            running to the machine to pull power).
#   2. Persistent journald — keep logs across reboots/crashes, surgically,
#                            without giving up DietPi's RAMlog.
#   3. zram swap           — compressed-RAM safety valve so a RAM spike
#                            overflows instead of hanging the box.
#   4. Health logger       — poll firmware throttle/undervoltage into the
#                            (now persistent) journal for post-mortem.
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
echo "[1/4] Arming hardware watchdog (15s)..."
mkdir -p /etc/systemd/system.conf.d
cat > /etc/systemd/system.conf.d/10-watchdog.conf <<'EOF'
[Manager]
RuntimeWatchdogSec=15
EOF
systemctl daemon-reexec

# ---------------------------------------------------------------------------
# 2. Persistent journald. /var/log is a DietPi RAMlog tmpfs, so journald is
#    volatile and a reboot erases the logs that would explain a hang. Bind
#    /var/log/journal to an SSD-backed dir so the journal (only) persists,
#    leaving RAMlog to handle the rest of /var/log. The x-systemd.before
#    ordering guarantees the bind is mounted before DietPi's ramlog service
#    touches /var/log; nofail keeps boot alive if the SSD is ever absent.
# ---------------------------------------------------------------------------
echo "[2/4] Configuring persistent journald..."
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

# ---------------------------------------------------------------------------
# 3. zram swap. No swap + cgroup_disable=memory means a RAM spike hangs the
#    whole box instead of getting one service OOM-killed. A compressed-RAM
#    swap (~50% of RAM, zero SSD wear) gives it somewhere to overflow.
#    DietPi's helper writes modules-load.d, the udev rule and the sysctl and
#    persists AUTO_SETUP_SWAPFILE_* to dietpi.txt. (Fresh flashes get this
#    from dietpi.template.txt: AUTO_SETUP_SWAPFILE_LOCATION=zram.)
# ---------------------------------------------------------------------------
echo "[3/4] Ensuring zram swap..."
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
echo "[4/4] Installing health logger..."
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
After=multi-user.target

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

echo "=== Reliability & observability hardening complete ==="
