#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPOSITORY_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"

DEFAULT_IMAGE_URL="https://dietpi.com/downloads/images/DietPi_RPi234-ARMv8-Trixie.img.xz"
DEVICE=""
CONFIG="$REPOSITORY_DIR/provisioning.env"
IMAGE_URL="${DIETPI_IMAGE_URL:-$DEFAULT_IMAGE_URL}"
CACHE_ROOT="${XDG_CACHE_HOME:-${HOME}/Library/Caches}"
CACHE_DIRECTORY="$CACHE_ROOT/lexacube"
WRITER="imager"
DRY_RUN=false
ALLOW_STALE=false

usage() {
    cat <<EOF
Usage: $0 --device /dev/diskN [options]

Flash and minimally configure a DietPi SD card for LEXACUBE.

Required:
  --device /dev/diskN   Whole external/removable disk to erase

Options:
  --config PATH         Local provisioning env (default: provisioning.env)
  --image-url URL       DietPi .img.xz URL (default: RPi 2/3/4 ARMv8)
  --cache-directory DIR Download cache
  --writer NAME         Image writer: imager (default) or dd
  --dry-run             Validate and render, but do not download or erase
  --allow-stale         Prepare even if this checkout is behind origin/main
  --help
EOF
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --device)
            [[ $# -ge 2 ]] || fail "--device requires a value"
            DEVICE=$2
            shift 2
            ;;
        --config)
            [[ $# -ge 2 ]] || fail "--config requires a value"
            CONFIG=$2
            shift 2
            ;;
        --image-url)
            [[ $# -ge 2 ]] || fail "--image-url requires a value"
            IMAGE_URL=$2
            shift 2
            ;;
        --cache-directory)
            [[ $# -ge 2 ]] || fail "--cache-directory requires a value"
            CACHE_DIRECTORY=$2
            shift 2
            ;;
        --writer)
            [[ $# -ge 2 ]] || fail "--writer requires a value"
            WRITER=$2
            shift 2
            ;;
        --dry-run)
            DRY_RUN=true
            shift
            ;;
        --allow-stale)
            ALLOW_STALE=true
            shift
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

[[ -n "$DEVICE" ]] || fail "--device is required"
[[ "$DEVICE" =~ ^/dev/disk[0-9]+$ ]] \
    || fail "--device must name a whole macOS disk, for example /dev/disk4"
[[ "$DEVICE" != "/dev/disk0" ]] || fail "refusing to erase the primary system disk"
[[ -f "$CONFIG" ]] || fail "provisioning file not found: $CONFIG"
[[ "$IMAGE_URL" == https://dietpi.com/downloads/images/*.img.xz ]] \
    || fail "--image-url must be an official HTTPS DietPi .img.xz URL"
[[ "$WRITER" == "imager" || "$WRITER" == "dd" ]] \
    || fail "--writer must be 'imager' or 'dd'"

if [[ "$(uname -s)" != "Darwin" && "$DRY_RUN" != true ]]; then
    fail "this imaging script currently supports macOS only"
fi

for command in curl diskutil git python3 shasum sudo; do
    command -v "$command" &> /dev/null || fail "required command not found: $command"
done
if [[ "$WRITER" == "dd" ]]; then
    command -v xz &> /dev/null || fail "--writer dd requires the 'xz' command"
    command -v dd &> /dev/null || fail "--writer dd requires the 'dd' command"
fi

# The card is built from THIS checkout's scripts -- the first-boot loader, the
# renderer, which secrets get staged -- not from GitHub. A card prepared from a
# checkout that had fallen behind main silently shipped an older recipe: it
# predated deploy-key staging, so the private cubes clone had no credential
# and first boot never finished. Being ahead of main is fine (that is how a
# branch gets tested); being behind it is the defect.
checkout_is_current() {
    local repo=$1 behind branch
    if ! git -C "$repo" fetch --quiet origin main 2>/dev/null; then
        echo "Warning: could not fetch origin/main; cannot tell whether this" >&2
        echo "         checkout is current." >&2
        return 0
    fi
    behind=$(git -C "$repo" rev-list --count HEAD..origin/main)
    (( behind == 0 )) && return 0
    branch=$(git -C "$repo" rev-parse --abbrev-ref HEAD)
    echo "Error: this checkout ($branch) is $behind commit(s) behind origin/main." >&2
    echo "       The card is built from this checkout's scripts, not GitHub's." >&2
    echo "       Update it, or prepare from a worktree of origin/main, or pass" >&2
    echo "       --allow-stale to prepare from it anyway." >&2
    return 1
}
if [[ "$ALLOW_STALE" != true ]]; then
    checkout_is_current "$REPOSITORY_DIR" || exit 1
fi

case "$CONFIG" in
    "$REPOSITORY_DIR"/*)
        git -C "$REPOSITORY_DIR" check-ignore --quiet "$CONFIG" \
            || fail "$CONFIG is inside the repository but is not ignored by Git"
        ;;
esac

DISK_INFO=$(diskutil info "$DEVICE") || fail "disk not found: $DEVICE"
if grep -Eq '^[[:space:]]*Internal:[[:space:]]*Yes' <<< "$DISK_INFO"; then
    fail "refusing to erase an internal disk: $DEVICE"
fi
if ! grep -Eq \
    '^[[:space:]]*(Device Location:[[:space:]]*External|Removable Media:[[:space:]]*Removable)' \
    <<< "$DISK_INFO"; then
    fail "disk is not reported as external or removable: $DEVICE"
fi

echo "Selected disk:"
grep -E \
    '^[[:space:]]*(Device / Media Name|Disk Size|Device Location|Removable Media|Internal):' \
    <<< "$DISK_INFO" || true
echo ""

WORK_DIRECTORY=$(mktemp -d "${TMPDIR:-/tmp}/lexacube-provision.XXXXXX")
cleanup() {
    rm -rf "$WORK_DIRECTORY"
}
trap cleanup EXIT

python3 "$SCRIPT_DIR/render_dietpi_provisioning.py" \
    --env "$CONFIG" \
    --dietpi-template "$REPOSITORY_DIR/dietpi.template.txt" \
    --wifi-template "$REPOSITORY_DIR/dietpi-wifi.template.txt" \
    --output-directory "$WORK_DIRECTORY/rendered" \
    --apps-config "$REPOSITORY_DIR/apps.yaml"
cp "$SCRIPT_DIR/Automation_Custom_Script.sh" \
    "$WORK_DIRECTORY/rendered/Automation_Custom_Script.sh"

if [[ "$DRY_RUN" == true ]]; then
    echo "DRY RUN: configuration rendered; no image was downloaded and no disk was erased."
    exit 0
fi

if [[ "$WRITER" == "imager" ]]; then
    if [[ -n "${RPI_IMAGER_BIN:-}" ]]; then
        IMAGER=$RPI_IMAGER_BIN
    elif command -v rpi-imager &> /dev/null; then
        IMAGER=$(command -v rpi-imager)
    elif [[ -x "/Applications/Raspberry Pi Imager.app/Contents/MacOS/rpi-imager" ]]; then
        IMAGER="/Applications/Raspberry Pi Imager.app/Contents/MacOS/rpi-imager"
    else
        fail "Raspberry Pi Imager is not installed"
    fi
    [[ -x "$IMAGER" ]] || fail "Raspberry Pi Imager is not executable: $IMAGER"
fi

echo "This will permanently erase all data on $DEVICE."
read -r -p "Type 'ERASE $DEVICE' to continue: " CONFIRMATION
[[ "$CONFIRMATION" == "ERASE $DEVICE" ]] || fail "confirmation did not match"

mkdir -p "$CACHE_DIRECTORY"
IMAGE_NAME=${IMAGE_URL##*/}
IMAGE_PATH="$CACHE_DIRECTORY/$IMAGE_NAME"
CHECKSUM_PATH="$CACHE_DIRECTORY/$IMAGE_NAME.sha256"

echo "Downloading DietPi checksum..."
curl --fail --location --silent --show-error \
    "$IMAGE_URL.sha256" --output "$CHECKSUM_PATH"
EXPECTED_CHECKSUM=$(awk 'NR == 1 { print $1 }' "$CHECKSUM_PATH")
[[ "$EXPECTED_CHECKSUM" =~ ^[0-9a-fA-F]{64}$ ]] \
    || fail "DietPi checksum response was invalid"

verify_image() {
    [[ -f "$IMAGE_PATH" ]] || return 1
    local actual_checksum
    actual_checksum=$(shasum -a 256 "$IMAGE_PATH" | awk '{ print $1 }')
    [[ "$actual_checksum" == "$EXPECTED_CHECKSUM" ]]
}

if ! verify_image; then
    echo "Downloading DietPi image..."
    rm -f "$IMAGE_PATH"
    curl --fail --location --show-error "$IMAGE_URL" --output "$IMAGE_PATH"
    verify_image || fail "DietPi image checksum verification failed"
else
    echo "Using verified cached image: $IMAGE_PATH"
fi

if [[ "$WRITER" == "imager" ]]; then
    echo "Flashing $DEVICE with Raspberry Pi Imager..."
    sudo "$IMAGER" --cli --disable-eject "$IMAGE_PATH" "$DEVICE"
else
    RAW_DEVICE="/dev/r${DEVICE#/dev/}"
    echo "Unmounting $DEVICE before raw write..."
    sudo diskutil unmountDisk force "$DEVICE"
    echo "Flashing $RAW_DEVICE with xz and dd (this may take several minutes)..."
    xz --decompress --stdout "$IMAGE_PATH" | sudo dd of="$RAW_DEVICE" bs=4m
    sync
fi

BOOT_DEVICE="${DEVICE}s1"
diskutil mount "$BOOT_DEVICE" > /dev/null 2>&1 \
    || diskutil mountDisk "$DEVICE" > /dev/null
BOOT_INFO=$(diskutil info "$BOOT_DEVICE")
BOOT_MOUNT=$(awk -F: '
    /^[[:space:]]*Mount Point:/ {
        sub(/^[[:space:]]+/, "", $2)
        print $2
        exit
    }
' <<< "$BOOT_INFO")
[[ -n "$BOOT_MOUNT" && -d "$BOOT_MOUNT" ]] \
    || fail "unable to locate the DietPi boot partition"
[[ -f "$BOOT_MOUNT/dietpi.txt" ]] \
    || fail "$BOOT_MOUNT does not look like a DietPi boot partition"

echo "Installing minimal first-boot configuration..."
cp "$WORK_DIRECTORY/rendered/dietpi.txt" "$BOOT_MOUNT/dietpi.txt"
cp "$WORK_DIRECTORY/rendered/dietpi-wifi.txt" "$BOOT_MOUNT/dietpi-wifi.txt"
cp "$WORK_DIRECTORY/rendered/Automation_Custom_Script.sh" \
    "$BOOT_MOUNT/Automation_Custom_Script.sh"
if [[ -f "$WORK_DIRECTORY/rendered/lexacube-zai-key" ]]; then
    cp "$WORK_DIRECTORY/rendered/lexacube-zai-key" "$BOOT_MOUNT/lexacube-zai-key"
    echo "  Z.ai key staged; bootstrap.sh installs it and removes it from /boot."
fi
if [[ -f "$WORK_DIRECTORY/rendered/github-api-token" ]]; then
    cp "$WORK_DIRECTORY/rendered/github-api-token" "$BOOT_MOUNT/github-api-token"
    echo "  GitHub API token staged; bootstrap.sh installs it and removes it from /boot."
fi
for STAGED_KEY in "$WORK_DIRECTORY"/rendered/github-deploy-key-*; do
    [[ -e "$STAGED_KEY" ]] || break
    cp "$STAGED_KEY" "$BOOT_MOUNT/$(basename "$STAGED_KEY")"
    echo "  $(basename "$STAGED_KEY") staged; bootstrap.sh installs it and removes it from /boot."
done
for STAGED_ENV in "$WORK_DIRECTORY"/rendered/*.env; do
    [[ -e "$STAGED_ENV" ]] || break
    cp "$STAGED_ENV" "$BOOT_MOUNT/$(basename "$STAGED_ENV")"
    echo "  $(basename "$STAGED_ENV") staged; bootstrap.sh installs it to /etc and removes it from /boot."
done
if [[ -f "$WORK_DIRECTORY/rendered/lexacube-firmware-secrets.h" ]]; then
    cp "$WORK_DIRECTORY/rendered/lexacube-firmware-secrets.h" \
        "$BOOT_MOUNT/lexacube-firmware-secrets.h"
    echo "  Cube firmware credentials staged; bootstrap.sh installs and removes them."
fi
sync
diskutil eject "$DEVICE" > /dev/null

echo "DietPi media is ready. Insert it into the Pi and power on."
