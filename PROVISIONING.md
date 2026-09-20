# LEXACUBE provisioning

LEXACUBE uses a stock DietPi image with minimal first-boot configuration. All
mutable machine setup remains in the idempotent `bootstrap.sh`.

## Local secrets

Create the Git-ignored provisioning file:

```sh
cp provisioning.env.example provisioning.env
chmod 600 provisioning.env
```

Set the WiFi network, a unique DietPi password, and optionally an SSH public key
and a `ZAI_API_KEY` for the `claude-zai` alias.

For the LEXACUBE dual-band rig, set `WIFI_SSID`/`WIFI_PASSWORD` to
`FunAcross-5G` for the Pi. Set `CUBE_WIFI_SSID`/`CUBE_WIFI_PASSWORD` to the
ESP32 cubes' 2.4 GHz network when needed: those credentials are staged only
long enough to generate the firmware header, and are never added to the Pi's
WiFi profiles. The Pi therefore remains exclusively on 5 GHz.
Install Raspberry Pi Imager if needed:

```sh
brew install --cask raspberry-pi-imager
```

Insert the SD card and identify its whole-disk device:

```sh
diskutil list external
```

Preview the operation without downloading or writing an image:

```sh
./scripts/prepare_dietpi_sd.sh --device /dev/diskN --dry-run
```

Replace `diskN` with the whole-disk identifier reported for the target device,
then prepare the card:

```sh
./scripts/prepare_dietpi_sd.sh --device /dev/diskN
```

If Raspberry Pi Imager stops at `Unmounting drive...`, cancel that run. The
same helper can use its raw-write fallback while retaining the checksum checks
and first-boot-file staging:

```sh
./scripts/prepare_dietpi_sd.sh --device /dev/diskN --writer dd
```

The script accepts only a whole external/removable macOS disk, refuses
`/dev/disk0` and internal disks, verifies DietPi's published SHA-256 checksum,
and requires the exact confirmation `ERASE /dev/diskN`. It uses Raspberry Pi
Imager's CLI to flash the compressed image, writes only the minimal first-boot
files, then ejects the card.

The default image is DietPi Trixie ARMv8 for Raspberry Pi 2/3/4. For another
supported model, pass its official image URL:

```sh
./scripts/prepare_dietpi_sd.sh \
  --device /dev/diskN \
  --image-url https://dietpi.com/downloads/images/DietPi_RPi5-ARMv8-Trixie.img.xz
```

On first boot, DietPi joins WiFi and runs `Automation_Custom_Script.sh`. The
loader clones this repository to `/opt/pi-deploy` and runs:

```sh
./bootstrap.sh lexacube
```

If setup is interrupted, connect over SSH and rerun:

```sh
cd /opt/pi-deploy
git pull --ff-only
sudo ./bootstrap.sh lexacube
```

The WiFi values are consumed by DietPi into its root-only WiFi database.
`bootstrap.sh` uses that local database to generate the ignored ESP32 firmware
header. Neither `provisioning.env` nor populated firmware credentials are
committed to Git.

`ZAI_API_KEY`, when set, is staged on the boot partition as `lexacube-zai-key`.
On first boot `bootstrap.sh` installs it as `~/.claude-switch/zai-key` (0600)
for both `root` and `dietpi` and deletes the boot copy, since the FAT32 boot
partition cannot hold permissions. Bootstrap also installs the Claude Code CLI
for both users, so the `claude-ant` and `claude-zai` aliases work on a fresh
flash. To set the key later instead, write it to `~/.claude-switch/zai-key` by
hand — bootstrap never overwrites an installed key.

## Replacing an existing boot SSD

Prefer a clean DietPi installation over cloning the old SSD. A clean install
lets `pi-deploy` recreate the operating system, dependencies, application
checkouts, and systemd units without copying filesystem damage or stale machine
state. Keep the old SSD unchanged until the replacement has passed a hardware
test.

### 1. Save machine-specific state

The application code and services are reproducible. Credentials, pending
analytics, output files, and changes made only on the Pi are not.

On the old Pi, first inspect the deployment checkouts for changes that have not
been committed:

```sh
sudo git -C /opt/pi-deploy status --short
sudo git -C /opt/lexacube status --short
sudo git -C /home/dietpi/knockstrip status --short 2>/dev/null || true
```

Reconcile or separately copy anything unexpected. Then make a small archive of
the protected machine credentials. Missing optional files are ignored:

```sh
sudo tar --ignore-failed-read -czf /tmp/lexacube-machine-state.tgz \
  /etc/lexacube.env \
  /etc/knockstrip.env \
  /etc/lexacube-firmware-secrets.h \
  /root/.claude-switch/zai-key \
  2>/dev/null
sudo chown dietpi:dietpi /tmp/lexacube-machine-state.tgz
```

Copy it to the Mac, along with any application data worth retaining:

```sh
mkdir -p "$HOME/lexacube-old-pi-backup"
scp dietpi@lexacube:/tmp/lexacube-machine-state.tgz \
  "$HOME/lexacube-old-pi-backup/"
rsync -a --partial dietpi@lexacube:/opt/lexacube/output/ \
  "$HOME/lexacube-old-pi-backup/output/"
```

Gameplay analytics waiting to upload live in `/var/lib/lexacube/analytics`.
Normally allow the uploader to drain them before changing disks. If that is not
possible and they matter, archive that root-owned directory separately.

Extract the credentials archive locally into a private directory:

```sh
mkdir -m 700 "$HOME/lexacube-old-pi-backup/machine-state"
tar -xzf "$HOME/lexacube-old-pi-backup/lexacube-machine-state.tgz" \
  -C "$HOME/lexacube-old-pi-backup/machine-state"
```

### 2. Prepare first-boot configuration

Create the ignored local provisioning configuration if it does not already
exist:

```sh
cp provisioning.env.example provisioning.env
chmod 600 provisioning.env
```

Fill in the WiFi settings, a unique DietPi password, hostname, timezone, and an
SSH public key. Add `ZAI_API_KEY` if it should be installed automatically:

```sh
${EDITOR:-vi} provisioning.env
```

Per-rig application secrets live in a directory outside the repository,
`~/.lexacube-secrets` by default (`SECRETS_DIR` in `provisioning.env`). Every
`<app>.env` file found there is staged on the boot partition, and `bootstrap.sh`
installs it to `/etc/<app>.env` (0600) and deletes the boot copy. Keep them
there rather than in `/tmp`, which does not survive to the next flash:

```sh
mkdir -m 700 -p "$HOME/.lexacube-secrets"
test ! -f "$HOME/lexacube-old-pi-backup/machine-state/etc/lexacube.env" || \
  cp "$HOME/lexacube-old-pi-backup/machine-state/etc/lexacube.env" \
    "$HOME/.lexacube-secrets/lexacube.env"
test ! -f "$HOME/lexacube-old-pi-backup/machine-state/etc/knockstrip.env" || \
  cp "$HOME/lexacube-old-pi-backup/machine-state/etc/knockstrip.env" \
    "$HOME/.lexacube-secrets/knockstrip.env"
chmod 600 "$HOME"/.lexacube-secrets/*.env 2>/dev/null || true
```

Only copy files that existed on the old Pi. `lexacube.env` carries the PostHog
upload settings; `knockstrip.env` carries Knockstrip service credentials.
`prepare_dietpi_sd.sh` prints what it staged, and warns when the directory is
missing or empty — a flash with no `lexacube.env` plays normally and records no
analytics at all, and one with no `knockstrip.env` leaves knockstrip unable to
start.

The firmware secrets file normally does not need to be restored. Set
`CUBE_WIFI_SSID` and `CUBE_WIFI_PASSWORD` in `provisioning.env` to stage a
protected header for bootstrap; this does not add that network to the Pi's
WiFi profiles. Preserve the old file only if it was a deliberate manual
override.

### 3. Flash the replacement SSD

Attach the replacement SSD to the Mac and identify its whole-disk device:

```sh
diskutil list external
```

Although the helper is named `prepare_dietpi_sd.sh`, it also accepts an
external/removable USB SSD. Preview the exact target first:

```sh
./scripts/prepare_dietpi_sd.sh --device /dev/diskN --dry-run
```

Replace `diskN` with the device found above, then flash it:

```sh
./scripts/prepare_dietpi_sd.sh --device /dev/diskN
```

The helper verifies DietPi's checksum, writes the first-boot configuration, and
ejects the SSD. It does not currently stage the application `.env` files. If
either file was preserved above, remount the boot partition and copy it before
first boot:

```sh
diskutil mount /dev/diskNs1
diskutil info /dev/diskNs1 | grep 'Mount Point'
cp /tmp/lexacube.env /the/reported/mount/point/lexacube.env
cp /tmp/knockstrip.env /the/reported/mount/point/knockstrip.env
sync
diskutil eject /dev/diskN
```

Use the mount point printed by `diskutil`; its volume name is not guaranteed.
The FAT boot partition cannot enforce Unix permissions. Bootstrap installs
these files under `/etc` with mode 0600 before dependency builds and removes
the boot-partition copies automatically.

### 4. Boot and verify the replacement

Shut down the old installation cleanly, swap the SSD, and power the Pi on:

```sh
sudo poweroff
```

First boot joins WiFi, clones this repository into `/opt/pi-deploy`, and runs
`./bootstrap.sh lexacube`. The build can take a while because it compiles SDL,
installs Python dependencies, downloads audio assets, and prefetches the ESP32
toolchain.

After SSH becomes available, verify the service and fixed cube address:

```sh
ssh dietpi@lexacube
sudo systemctl status lexacube --no-pager
sudo systemctl status lexacube-address --no-pager
sudo systemctl status lexacube-admin --no-pager
ip address show
curl -fsS http://127.0.0.1:8080/
sudo journalctl -u lexacube -b --no-pager
```

Confirm that `192.168.8.247/24` is present. If an application credentials file
was staged, confirm that it was installed with restrictive permissions:

```sh
sudo stat -c '%a %n' /etc/lexacube.env 2>/dev/null || true
sudo stat -c '%a %n' /etc/knockstrip.env 2>/dev/null || true
```

The unattended first boot selects only `lexacube`. If this machine also uses
NFC control, provision it explicitly afterward:

```sh
cd /opt/pi-deploy
sudo ./bootstrap.sh lexacube nfc-control
```

To install every configured application, including Knockstrip, use:

```sh
sudo ./bootstrap.sh lexacube nfc-control knockstrip
```

Then run `pi-game` to display the active game, or select one explicitly with
`sudo pi-game lexacube` or `sudo pi-game knockstrip`.

Exercise the display, audio, cube connectivity, admin page, and a reboot before
retiring the old SSD. Restore old `output/` data only if the application needs
it; it is not required for a clean deployment.
