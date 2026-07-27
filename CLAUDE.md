# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository automates deployment of applications on Raspberry Pi running DietPi OS. The system is designed for unattended installation and configuration of systemd services with external dependencies.

## Common Commands

### Main Deployment
```bash
# Run complete bootstrap (idempotent)
sudo ./bootstrap.sh

# Bootstrap only the named apps; selection is additive
sudo ./bootstrap.sh lexacube nfc-control
sudo ./bootstrap.sh lexacube nfc-control knockstrip
```

### Service Management
```bash
# Check service status
sudo systemctl status lexacube

# View live logs
sudo journalctl -u lexacube -f

# Restart service
sudo systemctl restart lexacube
```

### Testing Configuration Changes
```bash
# Validate YAML syntax
yq eval apps.yaml

# Check systemd service file
cat /etc/systemd/system/lexacube.service
```

## Architecture

### Configuration-Driven Deployment System

The entire deployment is driven by `apps.yaml`, which defines:
- Application repository, branch, and installation path
- Dependencies between selectable apps (`requires`)
- System package dependencies (`apt_packages`)
- External library dependencies (repos, build commands, Python bindings)
- Systemd service configuration (exec command, user)

### Bootstrap Flow

`bootstrap.sh` orchestrates the complete setup sequence:

1. **Parse Configuration**: Extract all settings from `apps.yaml` using `yq`
2. **Install System Dependencies**: APT packages required for compilation/runtime
3. **Build External Libraries**: Clone, build C/C++ dependencies (e.g., rpi-rgb-led-matrix)
4. **Deploy Application**: Clone/update app repo, checkout branch
5. **Python Environment**: Create venv, install requirements.txt
6. **Install Python Bindings**: Link Python wrappers for C libraries into venv
7. **System Configuration**: ALSA audio, CPU isolation (isolcpus=3)
8. **Service Setup**: Generate systemd service and optional service-address unit, set permissions, enable/start

Key characteristic: **Path substitution pattern** in dependency build commands uses `{path}` placeholder that gets replaced with actual installation path at runtime.

### Privilege Model

The rpi-rgb-led-matrix library requires root for GPIO but drops to `daemon` user:
- Systemd service runs as `User=root`
- Application output directory owned by `daemon:daemon`
- This dual-privilege pattern is critical for LED matrix operation

### CPU Isolation for Real-Time Performance

The system configures `isolcpus=3` in `/boot/cmdline.txt` to dedicate CPU core 3 exclusively to LED matrix rendering. This prevents scheduler jitter that causes display artifacts. The bootstrap script is idempotent on this setting.

## File Structure

- `apps.yaml` - Central configuration for all deployment aspects
- `bootstrap.sh` - Main automation script that reads apps.yaml and performs deployment
- `dietpi.template.txt` - Pre-boot DietPi configuration (copy to /boot before first boot)
- `dietpi-wifi.template.txt` - WiFi credentials template (copy to /boot before first boot)

## Important Context

### Working with apps.yaml

When modifying application configuration:
- Always update `apps.yaml` first, then run `bootstrap.sh`
- The `dependencies` array supports build_cmd and install_python_cmd with `{path}` substitution
- Python bindings are installed into the app's venv, not system-wide
- APT packages are system-wide, installed before dependency builds

### Apps

**lexacube** — LED matrix game
- Lives in: `/opt/lexacube`
- Runs: `/opt/lexacube/runpygame.sh`
- Claims `192.168.8.247/24` as a secondary address through
  `lexacube-address.service`; the Pi's ordinary DHCP address remains available
  for administration
- Depends on: rpi-rgb-led-matrix library for LED control
- Uses: Python venv at `/opt/lexacube/cube_env`
- Output: Written to `/opt/lexacube/output/` (owned by daemon user)

- Member of the `game` exclusive group, and its `default_in_group`

**nfc-control** — NFC admin action daemon
- Lives in: `/opt/nfc-control`
- Runs: `/opt/lexacube/cube_env/bin/python3 /opt/nfc-control/nfc_control_daemon.py`
- Reuses lexacube's venv (aiomqtt already installed there)
- Requires: `After=mosquitto.service` (configured via `after` field in apps.yaml)
- `bound_to: lexacube`, so it starts and stops with lexacube instead of at boot

**knockstrip** — LED strip game
- Lives in: `/home/dietpi/knockstrip`
- Member of the `game` exclusive group, so it never runs alongside lexacube
- **Owns its own unit file.** `apps.yaml` points at `ops/knockstrip.service` in
  the app repo via `unit_source` rather than describing the service with
  `exec`/`environment` keys. That unit carries `ExecStartPre` steps (grant
  outbox directory, compiled song build), the pursuit `Environment` settings
  and `Restart=always`, and `game/tests/test_deployment.py` in that repo
  enforces that it stays byte-identical to `ops/knockstrip-preflight.service`
  on every execution directive. Do not reintroduce `exec:` for this app — a
  generated unit would be a lossy copy that silently drifts.
- Reads secrets from `/etc/knockstrip.env` (picked up by bootstrap's
  `/etc/<name>.env` convention)

### Systemd Service Pattern

Services created by bootstrap.sh have:
- Auto-restart on failure (5 sec delay)
- `After` dependencies from the `after` field in apps.yaml (defaults to `network.target`)
- WorkingDirectory set to app path
- ExecStart pointing to configured exec script

Apps may define `service_address.address` and an optional
`service_address.interface` (`auto` by default). Bootstrap installs a separate
oneshot unit that claims the address before the app starts. The address unit is
part of the application lifecycle: stopping the app releases the address, and
starting it claims the address again. The helper refuses to claim an address
already in use by another host. Legacy hosts where the service address is still
the primary static address must be migrated to DHCP administratively before
deployment.

### Exclusive App Groups

Every app is always *installed*; whether it *runs* is separate. Apps sharing an
`exclusive_group` in apps.yaml are mutually exclusive — exactly one member runs
at a time. This exists because the Pi has one audio output and 4GB of RAM, so
lexacube and knockstrip must never run together. (They drive different LED
hardware — matrix and strip — so that is not the conflict.)

Three mechanisms, deliberately layered:

1. **`Conflicts=` drop-in** at `/etc/systemd/system/<name>.service.d/10-exclusive.conf`.
   Starting one member makes systemd stop the others, so even a manual
   `systemctl start knockstrip` cannot leave two games running. It is a drop-in
   rather than a generated directive so it composes with repo-owned units
   (see knockstrip's `unit_source`).
2. **Enable-state is the source of truth.** There is no state file to drift.
   Bootstrap reads `systemctl is-enabled` for each member and *preserves*
   whichever is already active, so re-running bootstrap never changes which
   game is live. `default_in_group: true` breaks the tie only on a fresh flash
   where no member has been enabled yet.
3. **`pi-game`** (`scripts/select-app.sh`, installed to `/usr/local/bin`)
   switches members: `sudo pi-game knockstrip`. With no argument it prints the
   current member and the alternatives.

The inactive member is stopped *and* disabled, so it consumes no memory and no
cycles. Note that `isolcpus=3` still reserves a core for the LED matrix
regardless of which game is active; changing that requires a reboot.

Apps may also declare `bound_to: <app>`, which emits `PartOf=` and
`WantedBy=<app>.service` so the app starts and stops with its parent rather
than at boot. nfc-control uses this to follow lexacube.

Pass one or more app names to bootstrap only those apps during initial
provisioning, for example `sudo ./bootstrap.sh lexacube nfc-control knockstrip`.
Group activation is skipped unless every member of that group was selected, so
a partial run cannot silently switch games.
With no app names, bootstrap installs every configured app. Selection is
additive: bootstrap never disables services installed by an earlier run, and
an unknown app name or incomplete `requires` selection fails before system
configuration begins.

### Idempotency

The bootstrap script can be run multiple times safely:
- Git repos are updated (pull), not re-cloned
- Venv creation skipped if exists
- CPU isolation config only added if not present
- Systemd service overwritten and restarted
- The active member of an exclusive group is preserved, never reset to the default

## DietPi Templates

These files are used during SD card preparation (before first boot):
- Customize `dietpi.template.txt` and copy to `/boot/dietpi.txt` for unattended setup
- Customize `dietpi-wifi.template.txt` and copy to `/boot/dietpi-wifi.txt` for WiFi
- Must be placed on boot partition before powering on the Pi

## Reliability & Observability

`scripts/reliability.sh` (run at the end of `bootstrap.sh`, idempotent) hardens
the Pi against the live-event failure mode where it hung and needed a manual
power-cycle. Four independent measures:

1. **Hardware watchdog** — `/etc/systemd/system.conf.d/10-watchdog.conf` sets
   `RuntimeWatchdogSec=15`. If pid1 stops petting `/dev/watchdog0` (a true
   hang), the BCM2835 hardware resets the Pi in 15s. No more running to the box.
2. **Persistent journald** — `/var/log` is a DietPi RAMlog tmpfs, so the
   journal is volatile and a reboot erases the logs that would explain a hang.
   A `nofail` bind mount ties `/var/log/journal` to SSD-backed
   `/var/lib/journal-persist` (with `x-systemd.before=dietpi-ramlog.service`
   ordering), and a `journald.conf.d` drop-in sets `Storage=persistent`. RAMlog
   still handles the rest of `/var/log`.
3. **zram swap** — no swap + `cgroup_disable=memory` means a RAM spike hangs the
   whole box. `dietpi-set_swapfile 1 zram` adds ~50%-of-RAM compressed swap
   (zero SSD wear). Fresh flashes get this from `dietpi.template.txt`
   (`AUTO_SETUP_SWAPFILE_LOCATION=zram`).
4. **Health logger** — `pi-health-watch.service` runs `pi-health-watch.sh`,
   polling the firmware throttle/undervoltage bitmask + temp into the (now
   persistent) journal every 5s. The "since boot" bits latch, so even a
   sub-second brownout transient is caught. Inspect with `journalctl -t pi-health`.

Background: a bench load test (all cores + SSD write bursts) could **not**
reproduce a 5V-rail brownout, so rather than chase an unproven power fault
these measures make the Pi self-heal (1, 3) and self-diagnose (2, 4). The
definitive power test is running the real game engine at max stations with
audio while watching `vcgencmd get_throttled`.
