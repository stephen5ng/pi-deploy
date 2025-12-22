# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

This repository automates deployment of applications on Raspberry Pi running DietPi OS. The system is designed for unattended installation and configuration of systemd services with external dependencies.

## Common Commands

### Main Deployment
```bash
# Run complete bootstrap (idempotent)
sudo ./bootstrap.sh
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
8. **Service Setup**: Generate systemd service, set permissions, enable/start

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

### Understanding the Current App (lexacube)

- Lives in: `/opt/lexacube`
- Runs: `/opt/lexacube/runpygame.sh`
- Depends on: rpi-rgb-led-matrix library for LED control
- Uses: Python venv at `/opt/lexacube/cube_env`
- Output: Written to `/opt/lexacube/output/` (owned by daemon user)

### Systemd Service Pattern

Services created by bootstrap.sh have:
- Auto-restart on failure (5 sec delay)
- Network dependency (`After=network.target`)
- WorkingDirectory set to app path
- ExecStart pointing to configured exec script

### Idempotency

The bootstrap script can be run multiple times safely:
- Git repos are updated (pull), not re-cloned
- Venv creation skipped if exists
- CPU isolation config only added if not present
- Systemd service overwritten and restarted

## DietPi Templates

These files are used during SD card preparation (before first boot):
- Customize `dietpi.template.txt` and copy to `/boot/dietpi.txt` for unattended setup
- Customize `dietpi-wifi.template.txt` and copy to `/boot/dietpi-wifi.txt` for WiFi
- Must be placed on boot partition before powering on the Pi
