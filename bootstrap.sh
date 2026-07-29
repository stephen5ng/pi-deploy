#!/bin/bash
set -euo pipefail

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
CONFIG="$SCRIPT_DIR/apps.yaml"
SELECTED_APPS=("$@")

if [[ ${EUID:-$(id -u)} -ne 0 ]]; then
    echo "bootstrap.sh must run as root." >&2
    exit 1
fi

if ! command -v yq &> /dev/null; then
    echo "Installing bootstrap prerequisites..."
    apt-get update
    apt-get install -y --no-install-recommends yq
fi

app_is_selected() {
    local candidate=$1
    local selected_app

    if [[ ${#SELECTED_APPS[@]} -eq 0 ]]; then
        return 0
    fi

    for selected_app in "${SELECTED_APPS[@]}"; do
        if [[ "$candidate" == "$selected_app" ]]; then
            return 0
        fi
    done

    return 1
}

# =============================================================================
# SSH SETUP FOR ROOT
# Bootstrap runs as root, but SSH keys are typically in the dietpi user home.
# Copy dietpi SSH keys to root so git clone/pull works with GitHub SSH auth.
#
# This only applies if GitHub SSH authentication actually works - otherwise we
# keep using HTTPS which works fine for public repositories.
# =============================================================================

# GitHub SSH host keys (pinned for security, not ssh-keyscan)
# See: https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/githubs-ssh-key-fingerprints
GITHUB_RSA_HOST_KEY="github.com ssh-rsa AAAAB3NzaC1yc2EAAAABIwAAAQEAq2A7hRGmdnm9tUDbO9IDSwBK6TbQa+PXYPCPy7rb/tT5ubbMy3phfIWKUQQF0su7lKTV0qVRtoylf6PqPxLzLjl2vu+Yc/wwHEmNs68tpJchOaNFk8bdK6UmvFAiZrmVS/cpuMlZ8+Y0baQpMpLfZ0DJAGHdB2V38tnOKDFjLUKBdP/FoKRs8K8NKkI6PZwcPJAwpvydRprLHm1Xo7vhDhRSA/nNSItv+wICMn+GhA6s+QYwt/fAv+QH3/X1w=="
GITHUB_ED25519_HOST_KEY="github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl"

# Managed known_hosts file for GitHub only (avoids touching root's known_hosts)
GITHUB_KNOWN_HOSTS="/root/.ssh/github_known_hosts"
GITHUB_IDENTITY_FILE=""

# Verify SSH authentication to GitHub works
# Note: GitHub SSH returns exit code 1 even on success, so we capture output first
# Uses managed known_hosts file with StrictHostKeyChecking=yes for MITM protection
github_ssh_auth_works() {
    local identity_opts=""
    if [[ -n "$GITHUB_IDENTITY_FILE" ]]; then
        identity_opts="-i $GITHUB_IDENTITY_FILE -o IdentitiesOnly=yes"
    fi
    local ssh_output
    ssh_output=$(ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=yes -o UserKnownHostsFile="$GITHUB_KNOWN_HOSTS" $identity_opts -T git@github.com 2>&1 || true)
    echo "$ssh_output" | grep -q "successfully authenticated"
}

# Convert HTTPS GitHub URL to SSH
convert_https_to_ssh() {
    local repo=$1
    # Convert https://github.com/user/repo.git -> git@github.com:user/repo
    # Also handles URLs without .git suffix
    echo "$repo" | sed -E 's,https://github.com/([^/]+)/([^/]+)\.git.*,git@github.com:\1/\2,;s,https://github.com/([^/]+)/([^/]+)$,git@github.com:\1/\2,'
}

setup_ssh_for_root() {
    local dietpi_home="/home/dietpi"
    local root_ssh="/root/.ssh"
    local dietpi_ssh="$dietpi_home/.ssh"

    # Ensure SSH client is available before any SSH operations
    if ! command -v ssh &> /dev/null; then
        apt-get update -qq
        apt-get install -y -qq openssh-client
    fi

    # Create .ssh directory with correct permissions
    mkdir -p "$root_ssh"
    chmod 700 "$root_ssh"

    # Write pinned GitHub host keys to managed file (do not touch root's main known_hosts)
    {
        echo "$GITHUB_RSA_HOST_KEY"
        echo "$GITHUB_ED25519_HOST_KEY"
    } > "$GITHUB_KNOWN_HOSTS"
    chmod 644 "$GITHUB_KNOWN_HOSTS"

    local working_key=""

    # Try DietPi keys first, testing all available types
    if [[ -d "$dietpi_ssh" ]]; then
        echo "Checking for DietPi SSH keys..."
        for key_type in ed25519 rsa; do
            if [[ -f "$dietpi_ssh/id_$key_type" ]]; then
                local new_key="$root_ssh/id_dietpi_$key_type"
                cp "$dietpi_ssh/id_$key_type" "$new_key"
                cp "$dietpi_ssh/id_${key_type}.pub" "${new_key}.pub" 2>/dev/null || true
                chmod 600 "$new_key"
                chmod 644 "${new_key}.pub" 2>/dev/null || true

                GITHUB_IDENTITY_FILE="$new_key"
                if github_ssh_auth_works; then
                    working_key="$new_key"
                    echo "GitHub SSH auth verified using DietPi $key_type key."
                    break
                else
                    GITHUB_IDENTITY_FILE=""
                fi
            fi
        done
    fi

    # If no DietPi key worked, fall back to root's default keys
    if [[ -z "$working_key" ]]; then
        if [[ -f "$root_ssh/id_ed25519" ]] || [[ -f "$root_ssh/id_rsa" ]]; then
            echo "Falling back to existing root SSH keys..."
            GITHUB_IDENTITY_FILE=""
            if github_ssh_auth_works; then
                working_key="default"
                echo "GitHub SSH auth verified using root's default keys."
            fi
        fi
    fi

    if [[ -z "$working_key" ]]; then
        echo "Warning: No working GitHub SSH keys found, will keep HTTPS for GitHub repos"
        GITHUB_IDENTITY_FILE=""
    fi
}

# Clone or update a git repository
git_clone_or_update() {
    local repo=$1
    local dest=$2
    local branch=${3:-}
    local use_ssh=false
    local current_url=""

    if github_ssh_auth_works; then
        use_ssh=true
    fi

    local ssh_opts="-o StrictHostKeyChecking=yes -o UserKnownHostsFile=$GITHUB_KNOWN_HOSTS"
    if [[ -n "$GITHUB_IDENTITY_FILE" ]]; then
        ssh_opts="$ssh_opts -i $GITHUB_IDENTITY_FILE -o IdentitiesOnly=yes"
    fi

    if [[ -d "$dest/.git" ]]; then
        echo "Updating $dest..."
        current_url=$(git -C "$dest" remote get-url origin 2>/dev/null || echo "")
        if [[ "$use_ssh" == "true" && "$current_url" == https://github.com/* ]]; then
            ssh_url=$(convert_https_to_ssh "$current_url")
            echo "  Converting HTTPS remote to SSH: $ssh_url"
            git -C "$dest" remote set-url origin "$ssh_url"
            current_url="$ssh_url"
        fi

        # --no-rebase, explicitly: a deployment checkout may legitimately carry
        # local commits (per-rig config that must not be pushed), which makes
        # the branch divergent. Without a strategy on the command line git
        # refuses to pull at all unless pull.rebase happens to be configured on
        # that particular Pi, so bootstrap would succeed or fail depending on
        # ambient machine state. Merge keeps the local commits; on a clean
        # checkout it is just a fast-forward.
        # Bootstrap runs as root, which typically has no git identity, so a
        # merge commit would abort with "Author identity unknown". Supply one
        # explicitly rather than requiring every Pi to configure root's git.
        local -a git_id=(-c "user.email=bootstrap@pi-deploy.local"
                         -c "user.name=pi-deploy bootstrap")

        if [[ "$current_url" == *@github.com:* || "$current_url" == ssh://*github.com/* ]]; then
            GIT_SSH_COMMAND="ssh $ssh_opts" git "${git_id[@]}" -C "$dest" pull --no-rebase
        else
            git "${git_id[@]}" -C "$dest" pull --no-rebase
        fi
    else
        echo "Cloning $repo..."
        if [[ "$use_ssh" == "true" && "$repo" == https://github.com/* ]]; then
            repo=$(convert_https_to_ssh "$repo")
            echo "  Using SSH: $repo"
        fi

        if [[ "$repo" == *@github.com:* || "$repo" == ssh://*github.com/* ]]; then
            if [[ -n "$branch" ]]; then
                GIT_SSH_COMMAND="ssh $ssh_opts" git clone --branch "$branch" "$repo" "$dest"
            else
                GIT_SSH_COMMAND="ssh $ssh_opts" git clone "$repo" "$dest"
            fi
        else
            if [[ -n "$branch" ]]; then
                git clone --branch "$branch" "$repo" "$dest"
            else
                git clone "$repo" "$dest"
            fi
        fi
    fi
}

app_count=$(yq -r '.apps | length' "$CONFIG")

# Validate the complete selection before making any system changes. App
# selection controls what is installed during this run; it never disables
# services installed by an earlier bootstrap.
for selected_app in "${SELECTED_APPS[@]}"; do
    selected_app_found=false
    selected_app_idx=-1
    for ((app_idx=0; app_idx<app_count; app_idx++)); do
        configured_app=$(yq -r ".apps[$app_idx].name" "$CONFIG")
        if [[ "$configured_app" == "$selected_app" ]]; then
            selected_app_found=true
            selected_app_idx=$app_idx
            break
        fi
    done

    if [[ "$selected_app_found" != true ]]; then
        echo "Unknown app '$selected_app'. Available apps:" >&2
        yq -r '.apps[].name | "  " + .' "$CONFIG" >&2
        exit 2
    fi

    required_apps=$(yq -r ".apps[$selected_app_idx].requires[]? // empty" "$CONFIG")
    while IFS= read -r required_app; do
        [[ -n "$required_app" ]] || continue
        if ! app_is_selected "$required_app"; then
            echo "App '$selected_app' requires '$required_app'; include both app names." >&2
            exit 2
        fi
    done <<< "$required_apps"
done

setup_ssh_for_root

if [[ ${#SELECTED_APPS[@]} -gt 0 ]]; then
    echo "=== Bootstrapping ${SELECTED_APPS[*]} from $CONFIG ==="
else
    echo "=== Bootstrapping all apps from $CONFIG ==="
fi

echo "Found $app_count app(s) in config"
echo ""

# ============================================================================
# BUILD SDL2 WITH KMSDRM SUPPORT (for pygame HDMI output on headless RPi)
# The Debian SDL2 package is compiled without kmsdrm/fbdev. We build from
# source with kmsdrm enabled so pygame can display on HDMI without X11.
# ============================================================================
echo "Building SDL2 with kmsdrm support..."
apt-get install -y --no-install-recommends build-essential cmake git libdrm-dev libgbm-dev libgl1-mesa-dev libasound2-dev libpulse-dev

SDL2_BUILD_DIR="/tmp/SDL2_build"
if [[ ! -f "/usr/local/lib/libSDL2-2.0.so.0" ]]; then
    rm -rf "$SDL2_BUILD_DIR"
    git clone --depth 1 --branch SDL2 https://github.com/libsdl-org/SDL.git "$SDL2_BUILD_DIR"
    mkdir -p "$SDL2_BUILD_DIR/build"
    pushd "$SDL2_BUILD_DIR/build"
    cmake -DCMAKE_BUILD_TYPE=Release \
        -DSDL_KMSDRM=ON \
        -DSDL_X11=OFF \
        -DSDL_WAYLAND=OFF \
        -DSDL_VULKAN=OFF \
        -DSDL_UNIX_CONSOLE_BUILD=ON \
        ..
    make -j$(nproc)
    make install
    ldconfig
    popd
    rm -rf "$SDL2_BUILD_DIR"
    echo "SDL2 with kmsdrm built and installed"
else
    echo "SDL2 with kmsdrm already installed"
fi

# ============================================================================
# PROCESS EACH APP
# ============================================================================
for ((app_idx=0; app_idx<app_count; app_idx++)); do
    name=$(yq -r ".apps[$app_idx].name" "$CONFIG")

    if ! app_is_selected "$name"; then
        continue
    fi

    repo=$(yq -r ".apps[$app_idx].repo" "$CONFIG")
    branch=$(yq -r ".apps[$app_idx].branch // empty" "$CONFIG")
    path=$(yq -r ".apps[$app_idx].path" "$CONFIG")
    venv_name=$(yq -r ".apps[$app_idx].venv // empty" "$CONFIG")
    exec=$(yq -r ".apps[$app_idx].exec // empty" "$CONFIG")
    service_user=$(yq -r ".apps[$app_idx].user // \"dietpi\"" "$CONFIG")
    after=$(yq -r ".apps[$app_idx].after // \"network.target\"" "$CONFIG")
    service_address=$(yq -r ".apps[$app_idx].service_address.address // empty" "$CONFIG")
    service_address_interface=$(yq -r ".apps[$app_idx].service_address.interface // \"auto\"" "$CONFIG")
    exclusive_group=$(yq -r ".apps[$app_idx].exclusive_group // empty" "$CONFIG")
    bound_to=$(yq -r ".apps[$app_idx].bound_to // empty" "$CONFIG")
    unit_source=$(yq -r ".apps[$app_idx].unit_source // empty" "$CONFIG")

    echo "--- App: $name ---"
    echo "Repo:   $repo"
    echo "Path:   $path"
    if [[ -n "$unit_source" ]]; then
        echo "Unit:   $unit_source (owned by app repo)"
    else
        echo "Exec:   $exec"
    fi
    [[ -n "$exclusive_group" ]] && echo "Group:  $exclusive_group (exclusive)"
    echo ""

    # --------------------------------------------------------------------------
    # Install apt packages
    # --------------------------------------------------------------------------
    apt_packages=$(yq -r ".apps[$app_idx].apt_packages[]? // empty" "$CONFIG")
    if [[ -n "$apt_packages" ]]; then
        echo "Installing apt packages..."
        apt-get update
        echo "$apt_packages" | xargs apt-get install -y
    fi

    # --------------------------------------------------------------------------
    # Build external dependencies
    # --------------------------------------------------------------------------
    dep_count=$(yq -r ".apps[$app_idx].dependencies | length" "$CONFIG" 2>/dev/null || echo "0")
    if [[ "$dep_count" -gt 0 ]]; then
        echo "Processing $dep_count dependencies..."
        for ((i=0; i<dep_count; i++)); do
            dep_repo=$(yq -r ".apps[$app_idx].dependencies[$i].repo" "$CONFIG")
            dep_path=$(yq -r ".apps[$app_idx].dependencies[$i].path" "$CONFIG")
            dep_build=$(yq -r ".apps[$app_idx].dependencies[$i].build_cmd // empty" "$CONFIG")
            dep_submodules=$(yq -r ".apps[$app_idx].dependencies[$i].submodules // false" "$CONFIG")
            dep_secret_source=$(yq -r ".apps[$app_idx].dependencies[$i].secret_file.source // empty" "$CONFIG")
            dep_secret_destination=$(yq -r ".apps[$app_idx].dependencies[$i].secret_file.destination // empty" "$CONFIG")
            dep_secret_generator=$(yq -r ".apps[$app_idx].dependencies[$i].secret_file.generate // empty" "$CONFIG")

            echo "  Dependency: $dep_repo -> $dep_path"
            git_clone_or_update "$dep_repo" "$dep_path"

            if [[ "$dep_submodules" == "true" ]]; then
                echo "    Initializing recursive submodules..."
                git -C "$dep_path" submodule sync --recursive
                git -C "$dep_path" submodule update --init --recursive
            fi

            if [[ -n "$dep_secret_source" || -n "$dep_secret_destination" ]]; then
                if [[ -z "$dep_secret_source" || -z "$dep_secret_destination" ]]; then
                    echo "Both secret_file.source and secret_file.destination are required for $dep_repo" >&2
                    exit 1
                fi
                if [[ "$dep_secret_destination" == /* || "$dep_secret_destination" == *".."* ]]; then
                    echo "Secret destination must be a relative path without '..': $dep_secret_destination" >&2
                    exit 1
                fi
                if [[ ! -f "$dep_secret_source" ]]; then
                    if [[ "$dep_secret_generator" == "dietpi_wifi" ]]; then
                        echo "    Generating firmware secrets from the DietPi WiFi profile..."
                        python3 "$SCRIPT_DIR/scripts/firmware_secrets_from_dietpi_wifi.py" \
                            --output "$dep_secret_source"
                    elif [[ -n "$dep_secret_generator" ]]; then
                        echo "Unknown secret file generator '$dep_secret_generator' for $dep_repo" >&2
                        exit 1
                    fi
                fi
                if [[ ! -f "$dep_secret_source" ]]; then
                    echo "Required firmware secrets file not found: $dep_secret_source" >&2
                    echo "Create it from $SCRIPT_DIR/lexacube-firmware-secrets.h.example, then rerun bootstrap." >&2
                    exit 1
                fi
                echo "    Installing protected firmware secrets..."
                install -D -m 600 "$dep_secret_source" "$dep_path/$dep_secret_destination"
            fi

            if [[ -n "$dep_build" ]]; then
                echo "    Building: ${dep_build//\{path\}/$dep_path}"
                eval "${dep_build//\{path\}/$dep_path}"
            fi
        done
    fi

    # --------------------------------------------------------------------------
    # Deploy application repo
    # --------------------------------------------------------------------------
    echo "Processing app repo..."
    mkdir -p "$path"
    git_clone_or_update "$repo" "$path" "$branch"

    # --------------------------------------------------------------------------
    # Per-rig files
    # Values that describe THIS installation and are gitignored in the app repo,
    # so a reflash would otherwise lose them and the app would come up wrong (or,
    # by design, refuse to start). Recording them here is not the app "guessing" a
    # default -- it is the deployment repo remembering what is actually wired up.
    #
    # Written only when absent. If one exists and differs, say so loudly and leave
    # it alone: clobbering a map someone is mid-way through tuning on the box is
    # worse than a warning, and silent drift is what this whole mechanism exists
    # to prevent.
    # --------------------------------------------------------------------------
    # A YAML block scalar already ends in a newline and yq -r appends another, so
    # render through a command substitution (which strips trailing newlines) and
    # add exactly one back. Without this every file differs by a blank line and
    # bootstrap warns forever.
    rig_content() {
        printf '%s\n' "$(yq -r ".apps[$1].rig_files[$2].content" "$CONFIG")"
    }

    rig_file_count=$(yq -r ".apps[$app_idx].rig_files | length" "$CONFIG" 2>/dev/null || echo "0")
    for ((i=0; i<rig_file_count; i++)); do
        rig_rel=$(yq -r ".apps[$app_idx].rig_files[$i].path" "$CONFIG")
        rig_mode=$(yq -r ".apps[$app_idx].rig_files[$i].mode // \"644\"" "$CONFIG")

        if [[ "$rig_rel" == /* || "$rig_rel" == *".."* ]]; then
            echo "rig_files paths must be relative without '..': $rig_rel" >&2
            exit 1
        fi
        rig_dest="$path/$rig_rel"

        if [[ ! -f "$rig_dest" ]]; then
            rig_content "$app_idx" "$i" > "$rig_dest"
            chmod "$rig_mode" "$rig_dest"
            echo "  Per-rig file created: $rig_dest"
        elif ! diff -q <(rig_content "$app_idx" "$i") "$rig_dest" >/dev/null; then
            echo "  WARNING: $rig_dest differs from apps.yaml and was left as-is." >&2
            echo "           The Pi is the one running; apps.yaml is what survives a" >&2
            echo "           reflash. Reconcile them:" >&2
            diff <(rig_content "$app_idx" "$i") "$rig_dest" \
                | sed 's/^/             /' >&2 || true
        else
            echo "  Per-rig file up to date: $rig_dest"
        fi
    done

    # --------------------------------------------------------------------------
    # Secrets from the boot partition
    # Credentials cannot be defaulted or committed, but they must survive a
    # reflash. Same route as WiFi: drop <name>.env on the boot partition during
    # SD prep (see *.env.template.txt) and bootstrap installs it to the
    # /etc/<name>.env that the generated unit already reads via EnvironmentFile.
    # Same policy as per-rig files: install when absent, warn rather than clobber.
    # --------------------------------------------------------------------------
    for boot_dir in /boot/firmware /boot; do
        boot_env="$boot_dir/${name}.env"
        [[ -f "$boot_env" ]] || continue
        if [[ ! -f "/etc/${name}.env" ]]; then
            install -m 600 "$boot_env" "/etc/${name}.env"
            echo "  Secrets installed from $boot_env -> /etc/${name}.env"
        elif ! diff -q "$boot_env" "/etc/${name}.env" >/dev/null; then
            # Never diffed to the log: these are credentials.
            echo "  WARNING: $boot_env differs from /etc/${name}.env; kept the" >&2
            echo "           installed one. Delete /etc/${name}.env to adopt the" >&2
            echo "           boot-partition copy." >&2
        fi
        break
    done

    # --------------------------------------------------------------------------
    # Setup Python environment (only if requirements.txt exists)
    # --------------------------------------------------------------------------
    if [[ -f "$path/uv.lock" ]]; then
        echo "Syncing Python environment with uv..."
        if ! command -v uv &> /dev/null; then
            echo "  uv not found, installing..."
            curl -LsSf https://astral.sh/uv/install.sh | sh
            export PATH="$HOME/.local/bin:$PATH"
        fi
        (cd "$path" && uv sync --all-extras)
    elif [[ -n "$venv_name" && -f "$path/requirements.txt" ]]; then
        if [[ ! -f "$path/$venv_name/bin/activate" ]]; then
            echo "Creating virtual environment..."
            python3 -m venv "$path/$venv_name"
        fi
        echo "Installing requirements..."
        source "$path/$venv_name/bin/activate"
        pip install --upgrade pip
        pip install -r "$path/requirements.txt"
        deactivate
    fi

    # Install Python bindings for dependencies
    if [[ "$dep_count" -gt 0 ]]; then
        echo "Installing Python bindings for dependencies..."
        venv_dir="$path/$venv_name"
        [[ -n "$venv_name" && -d "$venv_dir" ]] && source "$venv_dir/bin/activate"
        for ((i=0; i<dep_count; i++)); do
            dep_path=$(yq -r ".apps[$app_idx].dependencies[$i].path" "$CONFIG")
            dep_python_cmd=$(yq -r ".apps[$app_idx].dependencies[$i].install_python_cmd // empty" "$CONFIG")
            dep_toolchain_cmd=$(yq -r ".apps[$app_idx].dependencies[$i].toolchain_cmd // empty" "$CONFIG")

            if [[ -n "$dep_python_cmd" ]]; then
                echo "  Installing: ${dep_python_cmd//\{path\}/$dep_path}"
                eval "${dep_python_cmd//\{path\}/$dep_path}"
            fi

            # Cross-compiler toolchains and embedded SDKs, fetched after
            # install_python_cmd because the tool that fetches them (e.g.
            # platformio) is itself installed into the venv above. Kept separate
            # from build_cmd, which runs before any venv exists. The command is
            # expected to be idempotent -- a no-op once the toolchain is cached.
            if [[ -n "$dep_toolchain_cmd" ]]; then
                echo "  Toolchain: ${dep_toolchain_cmd//\{path\}/$dep_path}"
                eval "${dep_toolchain_cmd//\{path\}/$dep_path}"
            fi
        done
        [[ -n "$venv_name" && -d "$venv_dir" ]] && deactivate
    fi

    # --------------------------------------------------------------------------
    # Replace pygame's bundled SDL2 with kmsdrm-enabled version (lexacube only)
    # --------------------------------------------------------------------------
    if [[ "$name" == "lexacube" ]]; then
        echo "Replacing pygame's bundled SDL2 with kmsdrm-enabled version..."
        PYGAME_LIBS=$(find "$path/$venv_name" -name "pygame.libs" -type d 2>/dev/null | head -1)
        if [[ -n "$PYGAME_LIBS" ]]; then
            SDL_BUNDLED=$(ls "$PYGAME_LIBS"/libSDL2-2*.so.* 2>/dev/null | head -1)
            SDL_NEW=$(ls /usr/local/lib/libSDL2-2.0.so.0.*.0 2>/dev/null | head -1)
            if [[ -n "$SDL_BUNDLED" && -n "$SDL_NEW" ]]; then
                cp "$SDL_BUNDLED" "${SDL_BUNDLED}.bak"
                cp "$SDL_NEW" "$SDL_BUNDLED"
                echo "  Replaced $SDL_BUNDLED with $SDL_NEW"
            else
                echo "  WARNING: Could not find bundled SDL2 or new SDL2 to replace"
            fi
        else
            echo "  WARNING: pygame.libs directory not found in venv"
        fi

        # Download word sounds audio assets from GitHub release (no auth required, public repo)
        ASSETS_DIR="$path/assets"
        if [[ ! -d "$ASSETS_DIR/word_sounds_0" ]]; then
            echo "Downloading word sounds audio assets..."
            RELEASE_API="https://api.github.com/repos/stephen5ng/cubes/releases/tags/audio-assets"
            AUDIO_DOWNLOAD_DIR=$(mktemp -d -p /var/tmp)

            ASSET_URLS=$(curl -sf "$RELEASE_API" \
                | python3 -c "import sys,json; assets=json.load(sys.stdin)['assets']; print('\n'.join(sorted(a['browser_download_url'] for a in assets if 'word_sounds.tar.gz.part' in a['name'])))")

            if [[ -z "$ASSET_URLS" ]]; then
                echo "  WARNING: No audio asset parts found in release, skipping."
            else
                while IFS= read -r url; do
                    filename=$(basename "$url")
                    echo "  Downloading $filename..."
                    curl -Lf "$url" -o "$AUDIO_DOWNLOAD_DIR/$filename"
                done <<< "$ASSET_URLS"

                echo "  Extracting audio assets..."
                cat "$AUDIO_DOWNLOAD_DIR"/word_sounds.tar.gz.part.* > "$AUDIO_DOWNLOAD_DIR/word_sounds.tar.gz"
                mkdir -p "$ASSETS_DIR"
                tar xzf "$AUDIO_DOWNLOAD_DIR/word_sounds.tar.gz" -C "$ASSETS_DIR"

                # word_sounds_0 is the neutral voice; copy it for player 2
                if [[ -d "$ASSETS_DIR/word_sounds_0" ]]; then
                    cp -r "$ASSETS_DIR/word_sounds_0" "$ASSETS_DIR/word_sounds_2"
                fi

                rm -rf "$AUDIO_DOWNLOAD_DIR"
                echo "  Audio assets installed."
            fi
        else
            echo "Audio assets already present, skipping download."
        fi

        # Create output directory owned by daemon (rpi-rgb-led-matrix drops to daemon user)
        echo "Setting up application permissions..."
        mkdir -p "$path/output"
        chown -R daemon:daemon "$path/output"
    fi

    # --------------------------------------------------------------------------
    # Setup systemd service
    # --------------------------------------------------------------------------
    echo "Creating systemd service for $name..."

    address_unit=""
    if [[ -n "$service_address" ]]; then
        address_service="${name}-address.service"
        address_helper="/usr/local/sbin/service-address"
        address_bare=${service_address%%/*}

        echo "Creating service address unit for $name ($service_address)..."
        install -m 755 "$SCRIPT_DIR/scripts/service-address.sh" "$address_helper"

        cat > "/etc/systemd/system/$address_service" <<EOF
[Unit]
Description=$name service address
Wants=network-online.target
After=network-online.target
PartOf=$name.service
Before=$name.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$address_helper start $service_address $service_address_interface
ExecStop=$address_helper stop $service_address $service_address_interface
EOF
        chmod 644 "/etc/systemd/system/$address_service"
        address_unit="Requires=$address_service
After=$address_service"

        # ifdown flushes every address on the interface, taking this secondary
        # alias with it; ifup restores only the DHCP lease. The unit above is a
        # oneshot with RemainAfterExit, so systemd keeps reporting it active and
        # never re-adds the alias. DietPi's WiFi monitor runs ifdown/ifup on
        # every connection loss, so without this hook the address vanishes
        # silently on the first WiFi blip and nothing reports a failure.
        # The hook cannot add the address itself: the probe outlives the hook, so
        # the app can be stopped while it runs and the address unit's ExecStop
        # can remove the address before this process puts it back, leaving the
        # address claimed by a stopped app. Ownership is therefore rechecked
        # after the probe, from a trap so it runs whether the reclaim exits
        # normally or is torn down by BindsTo= when the address unit stops.
        reclaim_helper="/usr/local/sbin/${name}-address-reclaim"
        cat > "$reclaim_helper" <<EOF
#!/bin/sh
# Managed by pi-deploy: claim $service_address for $name, then verify $name
# still owns it.

release_if_orphaned() {
    systemctl is-active --quiet $address_service && return 0
    $address_helper stop $service_address $service_address_interface \\
        >/dev/null 2>&1 || true
}
trap release_if_orphaned EXIT TERM INT

$address_helper start $service_address $service_address_interface
EOF
        chmod 755 "$reclaim_helper"

        address_hook="/etc/network/if-up.d/50-${name}-address"
        cat > "$address_hook" <<EOF
#!/bin/sh
# Managed by pi-deploy: re-claim $service_address for $name after ifup.

if [ "\$IFACE" = "lo" ]; then
    exit 0
fi

# Reclaim only while $name still owns the address; a stopped service must not
# have it handed back by an unrelated interface event.
if ! systemctl is-active --quiet $address_service; then
    exit 0
fi

if ip -o -4 address show | awk -v target="$address_bare" \\
        '\$4 == target || index(\$4, target "/") == 1 { found = 1 }
         END { exit(found ? 0 : 1) }'; then
    exit 0
fi

# Re-add through the reclaim helper rather than restarting $address_service:
# $name.service has Requires= on it, so a restart stops and restarts the app.
# Detached because the arping probe can take ~20s and ifup must not block on it.
if command -v systemd-run >/dev/null 2>&1; then
    systemd-run --collect --unit=${name}-address-reclaim \\
        --property=BindsTo=$address_service \\
        --property=After=$address_service \\
        $reclaim_helper >/dev/null 2>&1 || true
else
    setsid $reclaim_helper >/dev/null 2>&1 &
fi
exit 0
EOF
        chmod 755 "$address_hook"
    else
        rm -f "/etc/network/if-up.d/50-${name}-address"
        rm -f "/usr/local/sbin/${name}-address-reclaim"
    fi

    env_lines=""
    env_count=$(yq -r ".apps[$app_idx].environment | length" "$CONFIG" 2>/dev/null || echo "0")
    if [[ "$env_count" -gt 0 ]]; then
        for ((i=0; i<env_count; i++)); do
            env_var=$(yq -r ".apps[$app_idx].environment[$i]" "$CONFIG")
            env_lines="${env_lines}
Environment=${env_var}"
        done
    fi

    env_file_line=""
    if [[ -f "/etc/${name}.env" ]]; then
        env_file_line="
EnvironmentFile=/etc/${name}.env"
    fi

    if [[ -n "$unit_source" ]]; then
        # The app repo owns this unit; install it verbatim rather than
        # generating a lossy copy from apps.yaml keys.
        if [[ "$unit_source" == /* || "$unit_source" == *".."* ]]; then
            echo "unit_source must be a relative path without '..': $unit_source" >&2
            exit 1
        fi
        if [[ ! -f "$path/$unit_source" ]]; then
            echo "Declared unit_source not found: $path/$unit_source" >&2
            exit 1
        fi
        echo "Installing unit from app repo: $unit_source"
        install -m 644 "$path/$unit_source" "/etc/systemd/system/${name}.service"
    else
        if [[ -z "$exec" ]]; then
            echo "App $name needs either an 'exec' or a 'unit_source'." >&2
            exit 1
        fi

        bound_unit=""
        install_target="multi-user.target"
        if [[ -n "$bound_to" ]]; then
            # Follow the app we are bound to: start with it (via its .wants
            # directory), and stop/restart with it (via PartOf).
            bound_unit="PartOf=${bound_to}.service
After=${bound_to}.service"
            install_target="${bound_to}.service"
        fi

        cat > "/etc/systemd/system/${name}.service" <<EOF
[Unit]
Description=$name service
After=$after
$address_unit
$bound_unit

[Service]
Type=simple
User=$service_user
WorkingDirectory=$path${env_file_line}
ExecStart=$exec${env_lines}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=$install_target
EOF
        chmod 644 "/etc/systemd/system/${name}.service"
    fi

    # Sibling units shipped by the app repo, available to both unit styles.
    # A plain string entry is installed only -- the app's main unit is what
    # wires it in (knockstrip's preflight works this way). A map entry with
    # `enable: true` is also enabled and restarted after daemon-reload, for
    # units that stand on their own (lexacube's admin page).
    extra_units_to_enable=""
    extra_unit_count=$(yq -r ".apps[$app_idx].extra_units | length" "$CONFIG" 2>/dev/null || echo "0")
    for ((i=0; i<extra_unit_count; i++)); do
        # python-yq says "object", go yq says "!!map"; accept either.
        entry_type=$(yq -r ".apps[$app_idx].extra_units[$i] | type" "$CONFIG")
        if [[ "$entry_type" == "object" || "$entry_type" == "!!map" ]]; then
            extra_unit=$(yq -r ".apps[$app_idx].extra_units[$i].source" "$CONFIG")
            extra_enable=$(yq -r ".apps[$app_idx].extra_units[$i].enable // false" "$CONFIG")
        else
            extra_unit=$(yq -r ".apps[$app_idx].extra_units[$i]" "$CONFIG")
            extra_enable="false"
        fi
        if [[ "$extra_unit" == /* || "$extra_unit" == *".."* ]]; then
            echo "extra_units entries must be relative paths without '..': $extra_unit" >&2
            exit 1
        fi
        if [[ ! -f "$path/$extra_unit" ]]; then
            echo "Declared extra unit not found: $path/$extra_unit" >&2
            exit 1
        fi
        echo "  Installing sibling unit: $(basename "$extra_unit")"
        install -m 644 "$path/$extra_unit" "/etc/systemd/system/$(basename "$extra_unit")"
        if [[ "$extra_enable" == "true" ]]; then
            extra_units_to_enable="$extra_units_to_enable $(basename "$extra_unit")"
        fi
    done

    # Exclusivity is applied as a drop-in so it composes with repo-owned units.
    dropin_dir="/etc/systemd/system/${name}.service.d"
    if [[ -n "$exclusive_group" ]]; then
        conflicts=$(yq -r ".apps[] | select(.exclusive_group == \"$exclusive_group\") | select(.name != \"$name\") | .name + \".service\"" "$CONFIG" | paste -sd' ')
        mkdir -p "$dropin_dir"
        cat > "$dropin_dir/10-exclusive.conf" <<EOF
# Managed by pi-deploy bootstrap. Members of the '$exclusive_group' group are
# mutually exclusive: starting this unit stops the others, so a manual
# 'systemctl start' can never leave two games contending for audio and RAM.
[Unit]
Conflicts=$conflicts
EOF
        chmod 644 "$dropin_dir/10-exclusive.conf"
    else
        rm -f "$dropin_dir/10-exclusive.conf" 2>/dev/null || true
        rmdir "$dropin_dir" 2>/dev/null || true
    fi

    systemctl daemon-reload
    if [[ -n "$service_address" ]]; then
        # The address belongs to the application lifecycle and must not start
        # independently at boot.
        systemctl disable "$address_service" 2>/dev/null || true
    fi

    if [[ -n "$bound_to" ]]; then
        # reenable, not enable: the [Install] target moved from multi-user.target
        # to the bound unit, and enable alone would leave the stale symlink.
        systemctl reenable "${name}.service" 2>/dev/null || true
        echo "Service $name installed; follows ${bound_to}.service."
    elif [[ -n "$exclusive_group" ]]; then
        # Activation is decided after every app is installed, so that the whole
        # group is known before anything is started or stopped.
        echo "Service $name installed; activation deferred to group '$exclusive_group'."
    else
        systemctl enable "${name}.service"
        systemctl restart "${name}.service"
        echo "Service $name started."
    fi

    # Self-standing sibling units run regardless of the app's own activation
    # (a deferred group member still gets its admin page).
    for extra_unit_name in $extra_units_to_enable; do
        systemctl enable "$extra_unit_name"
        systemctl restart "$extra_unit_name"
        echo "Sibling unit $extra_unit_name started."
    done
    echo ""
done

# ============================================================================
# ACTIVATE EXCLUSIVE GROUPS
# Every group member is installed above; exactly one may run. The existing
# systemd enable-state is the source of truth, so re-running bootstrap never
# changes which game is live. default_in_group only breaks the tie on a fresh
# flash where no member has been enabled yet.
# ============================================================================
groups=$(yq -r '.apps[].exclusive_group // empty' "$CONFIG" | sort -u)
for group in $groups; do
    members=$(yq -r ".apps[] | select(.exclusive_group == \"$group\") | .name" "$CONFIG")

    # Only arbitrate a group whose members were all installed this run.
    skip_group=false
    for member in $members; do
        app_is_selected "$member" || skip_group=true
    done
    if [[ "$skip_group" == "true" ]]; then
        echo "Group '$group' not fully selected this run; leaving activation untouched."
        continue
    fi

    active=""
    for member in $members; do
        if systemctl is-enabled "${member}.service" &>/dev/null; then
            active="$member"
            break
        fi
    done

    if [[ -z "$active" ]]; then
        active=$(yq -r ".apps[] | select(.exclusive_group == \"$group\") | select(.default_in_group == true) | .name" "$CONFIG" | head -1)
        if [[ -z "$active" ]]; then
            echo "Group '$group' has no enabled member and no default_in_group; leaving all stopped." >&2
            continue
        fi
        echo "Group '$group': no member enabled, falling back to default '$active'."
    else
        echo "Group '$group': preserving active member '$active'."
    fi

    # Stop the losers first so the winner never briefly contends with them.
    for member in $members; do
        if [[ "$member" != "$active" ]]; then
            systemctl disable "${member}.service" 2>/dev/null || true
            systemctl stop "${member}.service" 2>/dev/null || true
        fi
    done
    systemctl enable "${active}.service"
    systemctl restart "${active}.service"
    echo "Group '$group': $active running, others stopped and disabled."
    echo ""
done

if [[ -n "$groups" && -f "$SCRIPT_DIR/scripts/select-app.sh" ]]; then
    # /usr/local/bin, not sbin: showing the active app needs no privileges, and
    # sbin is absent from the unprivileged PATH. Switching still requires root,
    # which the script enforces itself.
    rm -f /usr/local/sbin/pi-game
    sed "s|@CONFIG@|$CONFIG|" "$SCRIPT_DIR/scripts/select-app.sh" > /usr/local/bin/pi-game
    chmod 755 /usr/local/bin/pi-game
    echo "Installed /usr/local/bin/pi-game (run 'pi-game' to see or switch the active app)."
    echo ""
fi

# ============================================================================
# CONFIGURE SYSTEM (shared, run once)
# ============================================================================

# Configure mosquitto only when provisioning an app that uses it. This keeps
# selected deployments self-contained on fresh hosts.
if app_is_selected lexacube || app_is_selected nfc-control; then
    echo "Configuring mosquitto for network access..."
    mkdir -p /etc/mosquitto/conf.d
    cat > /etc/mosquitto/conf.d/network.conf <<'MQTT_EOF'
listener 1883 0.0.0.0
allow_anonymous true
persistence false
MQTT_EOF
    systemctl restart mosquitto
fi

echo "Configuring ALSA..."
cat > /etc/asound.conf <<'ALSA_EOF'
pcm.!default {
    type hw
    card ICUSBAUDIO7D
}

ctl.!default {
    type hw
    card ICUSBAUDIO7D
}
ALSA_EOF

# Enable VC4 KMS (Kernel Mode Setting) for HDMI display output with DRM
echo "Configuring VC4 KMS for pygame/SDL display output..."
CONFIG_FILE="/boot/firmware/config.txt"
if [[ ! -f "$CONFIG_FILE" ]]; then
    CONFIG_FILE="/boot/config.txt"
fi
if ! grep -q "dtoverlay=vc4-kms-v3d" "$CONFIG_FILE"; then
    echo "Adding VC4 KMS overlay to $CONFIG_FILE"
    sed -i '/^#-------Display---------/a dtoverlay=vc4-kms-v3d' "$CONFIG_FILE"
    echo "NOTE: Reboot required for VC4 KMS to take effect"
else
    echo "VC4 KMS overlay already configured in $CONFIG_FILE"
fi

# Configure CPU isolation for LED matrix performance
echo "Configuring CPU isolation..."
CMDLINE_FILE="/boot/firmware/cmdline.txt"
if [[ ! -f "$CMDLINE_FILE" ]]; then
    CMDLINE_FILE="/boot/cmdline.txt"
fi
if ! grep -q "isolcpus=3" "$CMDLINE_FILE"; then
    echo "Adding isolcpus=3 to $CMDLINE_FILE"
    sed -i 's/$/ isolcpus=3/' "$CMDLINE_FILE"
    echo "NOTE: Reboot required for CPU isolation to take effect"
else
    echo "CPU isolation already configured in $CMDLINE_FILE"
fi

# Add user to audio group for audio device access
echo "Adding root to audio group..."
usermod -a -G audio root

echo "Adding dietpi to hardware groups (audio, dialout, gpio)..."
usermod -a -G audio,dialout,gpio dietpi || true

# Install Claude backend switch scripts for root and dietpi users
echo "Installing Claude backend switch scripts..."

# prepare_dietpi_sd.sh leaves the Z.ai key on the boot partition when
# provisioning.env sets ZAI_API_KEY. The boot partition is FAT32 and cannot hold
# file permissions, so the key is consumed here -- installed at 0600 in each
# home, then removed from /boot. Later runs find no boot copy and leave the
# installed key untouched, so re-bootstrapping never clobbers a hand-edited key.
BOOT_ZAI_KEY="/boot/firmware/lexacube-zai-key"
if [[ ! -f "$BOOT_ZAI_KEY" ]]; then
    BOOT_ZAI_KEY="/boot/lexacube-zai-key"
fi
ZAI_KEY_VALUE=""
if [[ -f "$BOOT_ZAI_KEY" ]]; then
    ZAI_KEY_VALUE=$(tr -d '\r\n' < "$BOOT_ZAI_KEY")
fi

install_claude_code() {
    # The aliases invoke `claude`, so the CLI has to exist for whichever user
    # runs them. The native installer is per-user under ~/.local, so each home
    # gets its own copy rather than sharing one across the privilege boundary.
    local user_home=$1

    if [[ -x "$user_home/.local/bin/claude" ]]; then
        echo "  Claude Code already installed for $user_home"
        return 0
    fi
    if ! command -v curl &> /dev/null; then
        echo "  Warning: curl not found; skipping Claude Code install for $user_home"
        return 0
    fi

    echo "  Installing Claude Code for $user_home..."
    # pipefail is not inherited by a fresh `bash -c`, and without it a failed
    # curl is masked by the downstream bash exiting 0 on empty stdin -- the
    # install would silently "succeed" with no CLI and no warning.
    local -a installer=(bash -o pipefail -c 'curl -fsSL https://claude.ai/install.sh | bash')
    if [[ "$user_home" != "/root" ]]; then
        installer=(runuser -u dietpi -- env "HOME=$user_home" "${installer[@]}")
    fi

    # A network failure here must not abort a first-boot game deployment.
    if ! HOME="$user_home" "${installer[@]}"; then
        echo "  Warning: Claude Code install failed for $user_home;" \
            "claude-ant/claude-zai will not work until it is installed"
    fi
}

if [[ -f "$SCRIPT_DIR/scripts/use-anthropic.sh" ]]; then
    for USER_HOME in /root /home/dietpi; do
        CLAUDE_SWITCH_DIR="$USER_HOME/.claude-switch"
        mkdir -p "$CLAUDE_SWITCH_DIR"

        cp "$SCRIPT_DIR/scripts/use-anthropic.sh" "$CLAUDE_SWITCH_DIR/"
        cp "$SCRIPT_DIR/scripts/use-zai.sh" "$CLAUDE_SWITCH_DIR/"
        chmod +x "$CLAUDE_SWITCH_DIR"/*.sh
        echo "  Copied switch scripts to $CLAUDE_SWITCH_DIR"

        if [[ -n "$ZAI_KEY_VALUE" ]]; then
            printf '%s\n' "$ZAI_KEY_VALUE" > "$CLAUDE_SWITCH_DIR/zai-key"
            chmod 600 "$CLAUDE_SWITCH_DIR/zai-key"
            echo "  Installed provisioned Z.ai key in $CLAUDE_SWITCH_DIR/zai-key"
        elif [[ ! -f "$CLAUDE_SWITCH_DIR/zai-key" ]]; then
            echo "# Add your Z.ai API key here (sk-zai-...)" > "$CLAUDE_SWITCH_DIR/zai-key.example"
        fi

        install_claude_code "$USER_HOME"

        BASHRC_FILE="$USER_HOME/.bashrc"
        PATH_LINE='export PATH="$HOME/.local/bin:$PATH"'
        if ! grep -qF "$PATH_LINE" "$BASHRC_FILE" 2>/dev/null; then
            echo "" >> "$BASHRC_FILE"
            echo "# Claude Code and other per-user tools install here" >> "$BASHRC_FILE"
            echo "$PATH_LINE" >> "$BASHRC_FILE"
            echo "  Added ~/.local/bin to PATH in $BASHRC_FILE"
        fi

        ALIAS_MARKER="# Claude backend switch aliases"
        if ! grep -q "$ALIAS_MARKER" "$BASHRC_FILE" 2>/dev/null; then
            echo "" >> "$BASHRC_FILE"
            echo "$ALIAS_MARKER" >> "$BASHRC_FILE"
            echo "alias claude-ant='source ~/.claude-switch/use-anthropic.sh && claude'" >> "$BASHRC_FILE"
            echo "alias claude-zai='source ~/.claude-switch/use-zai.sh && claude'" >> "$BASHRC_FILE"
            echo "  Added aliases to $BASHRC_FILE"
        else
            echo "  Aliases already exist in $BASHRC_FILE"
        fi

        if [[ "$USER_HOME" == "/home/dietpi" ]]; then
            chown -R dietpi:dietpi "$CLAUDE_SWITCH_DIR"
        fi
    done

    if [[ -n "$ZAI_KEY_VALUE" ]]; then
        rm -f "$BOOT_ZAI_KEY"
        echo "  Removed the Z.ai key from the boot partition"
    elif [[ ! -f /root/.claude-switch/zai-key ]]; then
        echo "  Add your Z.ai key to ~/.claude-switch/zai-key"
        echo "  (or set ZAI_API_KEY in provisioning.env before imaging)"
    fi
    echo "  (Anthropic uses default authentication, no key needed)"
else
    echo "  Warning: Switch scripts not found in $SCRIPT_DIR/scripts/"
fi

# ============================================================================
# RELIABILITY & OBSERVABILITY (watchdog, zram swap, persistent journal,
# health logger). Idempotent; see scripts/reliability.sh.
# ============================================================================
if [[ -f "$SCRIPT_DIR/scripts/reliability.sh" ]]; then
    echo "Applying reliability & observability hardening..."
    bash "$SCRIPT_DIR/scripts/reliability.sh"
else
    echo "  Warning: scripts/reliability.sh not found, skipping hardening"
fi

echo ""
echo "=== Bootstrap complete ==="
