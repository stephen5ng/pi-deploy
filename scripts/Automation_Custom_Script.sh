#!/bin/bash
set -euo pipefail

# DietPi runs this file as root after its unattended first-boot setup.
# Keep it small: mutable machine configuration belongs in bootstrap.sh.

DEPLOY_DIR="/opt/pi-deploy"
DEPLOY_REPOSITORY="https://github.com/stephen5ng/pi-deploy"
DEPLOY_BRANCH="main"

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "Automation_Custom_Script.sh must run as root." >&2
    exit 1
fi

if ! command -v git &> /dev/null; then
    apt-get update
    apt-get install -y --no-install-recommends git ca-certificates
fi

if [[ -d "$DEPLOY_DIR/.git" ]]; then
    echo "Updating pi-deploy..."
    git -C "$DEPLOY_DIR" pull --ff-only origin "$DEPLOY_BRANCH"
elif [[ -e "$DEPLOY_DIR" ]]; then
    echo "$DEPLOY_DIR exists but is not a Git checkout; refusing to replace it." >&2
    exit 1
else
    echo "Cloning pi-deploy..."
    git clone --branch "$DEPLOY_BRANCH" "$DEPLOY_REPOSITORY" "$DEPLOY_DIR"
fi

cd "$DEPLOY_DIR"
exec ./bootstrap.sh lexacube
