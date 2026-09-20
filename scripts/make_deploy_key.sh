#!/bin/bash
# Create and register a read-only GitHub deploy key for a private application
# repository, storing the private half where prepare_dietpi_sd.sh will stage it
# onto the next image.
#
# GitHub refuses the same deploy key on a second repository, so every private
# repo the Pi must clone gets its own key. Without one, a fresh flash cannot
# clone that repo at all and bootstrap stops at the first clone.
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
REPOSITORY_DIR="$( cd "$SCRIPT_DIR/.." && pwd )"
CONFIG="$REPOSITORY_DIR/provisioning.env"
TITLE=""

usage() {
    cat <<EOF
Usage: $0 [--config PATH] [--title TEXT] owner/repo [owner/repo ...]

Generates ~/.lexacube-secrets/github-deploy-keys/<owner>.<repo> (or SECRETS_DIR
from provisioning.env) and adds its public half to the repository as a
read-only deploy key. Idempotent: an existing local key is reused, and a key
already registered on the repository is left alone.
EOF
}

fail() {
    echo "Error: $*" >&2
    exit 1
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --config) [[ $# -ge 2 ]] || fail "--config requires a value"; CONFIG=$2; shift 2 ;;
        --title) [[ $# -ge 2 ]] || fail "--title requires a value"; TITLE=$2; shift 2 ;;
        --help) usage; exit 0 ;;
        -*) fail "unknown option: $1" ;;
        *) break ;;
    esac
done

[[ $# -ge 1 ]] || { usage; exit 2; }
command -v gh > /dev/null || fail "the GitHub CLI (gh) is required"
command -v ssh-keygen > /dev/null || fail "ssh-keygen is required"

SECRETS_DIR="$HOME/.lexacube-secrets"
if [[ -f "$CONFIG" ]]; then
    CONFIGURED=$(sed -n "s/^SECRETS_DIR=['\"]\{0,1\}\([^'\"]*\)['\"]\{0,1\}[[:space:]]*$/\1/p" "$CONFIG" | tail -1)
    [[ -n "$CONFIGURED" ]] && SECRETS_DIR="${CONFIGURED/#\~/$HOME}"
fi
KEY_DIR="$SECRETS_DIR/github-deploy-keys"
mkdir -p "$KEY_DIR"
chmod 700 "$SECRETS_DIR" "$KEY_DIR"

: "${TITLE:=$(hostname -s) pi-deploy (read-only)}"

for TARGET in "$@"; do
    [[ "$TARGET" == */* ]] || fail "expected owner/repo, got '$TARGET'"
    OWNER=${TARGET%%/*}
    REPO=${TARGET#*/}
    # bootstrap.sh splits <owner>.<repo> on the first dot; a GitHub owner name
    # cannot contain one, so this stays unambiguous for hyphenated names.
    KEY="$KEY_DIR/$OWNER.$REPO"

    if [[ ! -f "$KEY" ]]; then
        ssh-keygen -t ed25519 -N "" -C "$TITLE $OWNER/$REPO" -f "$KEY" > /dev/null
        echo "Generated $KEY"
    fi
    chmod 600 "$KEY"

    PUBLIC=$(awk '{ print $1, $2 }' "$KEY.pub")
    if gh repo deploy-key list --repo "$TARGET" 2>/dev/null | grep -qF "$PUBLIC"; then
        echo "$TARGET: deploy key already registered"
        continue
    fi

    gh repo deploy-key add "$KEY.pub" --repo "$TARGET" --title "$TITLE" \
        || fail "could not add a deploy key to $TARGET (needs admin on the repo)"
    echo "$TARGET: deploy key registered (read-only)"
done

echo ""
echo "Keys live in $KEY_DIR and are staged on the next prepare_dietpi_sd.sh run."
