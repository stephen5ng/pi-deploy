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
