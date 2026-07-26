# LEXACUBE provisioning

LEXACUBE uses a stock DietPi image with minimal first-boot configuration. All
mutable machine setup remains in the idempotent `bootstrap.sh`.

## Local secrets

Create the Git-ignored provisioning file:

```sh
cp provisioning.env.example provisioning.env
chmod 600 provisioning.env
```

Set the WiFi network, a unique DietPi password, and optionally an SSH public key.
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
./scripts/prepare_dietpi_sd.sh --device /dev/disk4 --dry-run
```

Then prepare the card:

```sh
./scripts/prepare_dietpi_sd.sh --device /dev/disk4
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
  --device /dev/disk4 \
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

## knockstrip service secrets

`bootstrap.sh` wires `EnvironmentFile=/etc/knockstrip.env` into the knockstrip
unit but does not populate it — the secret values are provisioned separately so
they never touch the SD card. After the Pi is up, from the laptop:

```sh
# Fill in the knockstrip secrets in provisioning.env first (see the example),
# then push them to /etc/knockstrip.env (mode 0600) over ssh:
python3 scripts/provision_knockstrip_env.py            # or --pi user@host
python3 scripts/provision_knockstrip_env.py --dry-run  # preview, no write
```

It sets `POSTHOG_API_KEY`, `CLUE_STATUS_API_KEY`, and `PUSHER_SECRET` (skipping
any left blank), then reminds you to gate and restart:

```sh
ssh <pi> sudo systemctl start knockstrip-preflight.service
ssh <pi> sudo systemctl restart knockstrip.service
```

The `POSTHOG_API_KEY` is PostHog's public project ingest key; the script fetches
it from the PostHog API using the `phx_` personal key, which stays on the laptop.
The values are idempotent and never printed (only masked confirmations), so the
step is safe to re-run on a key rotation. It is the same command for a re-image
and for a one-off rotation.
