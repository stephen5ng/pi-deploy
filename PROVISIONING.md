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
Render the files that belong on the DietPi boot partition:

```sh
python3 scripts/render_dietpi_provisioning.py \
  --env provisioning.env \
  --dietpi-template dietpi.template.txt \
  --wifi-template dietpi-wifi.template.txt \
  --output-directory rendered
cp rendered/dietpi.txt rendered/dietpi-wifi.txt /Volumes/boot/
cp scripts/Automation_Custom_Script.sh /Volumes/boot/
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
