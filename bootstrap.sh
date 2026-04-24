#!/bin/bash
set -euo pipefail

CONFIG="apps.yaml"

# Clone or update a git repository
git_clone_or_update() {
    local repo=$1
    local dest=$2
    local branch=${3:-}

    if [[ -d "$dest/.git" ]]; then
        echo "Updating $dest..."
        git -C "$dest" pull
    else
        echo "Cloning $repo..."
        if [[ -n "$branch" ]]; then
            git clone --branch "$branch" "$repo" "$dest"
        else
            git clone "$repo" "$dest"
        fi
    fi
}

echo "=== Bootstrapping from $CONFIG ==="

app_count=$(yq -r '.apps | length' "$CONFIG")
echo "Found $app_count app(s) to deploy"
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
    repo=$(yq -r ".apps[$app_idx].repo" "$CONFIG")
    branch=$(yq -r ".apps[$app_idx].branch // empty" "$CONFIG")
    path=$(yq -r ".apps[$app_idx].path" "$CONFIG")
    venv_name=$(yq -r ".apps[$app_idx].venv // empty" "$CONFIG")
    exec=$(yq -r ".apps[$app_idx].exec" "$CONFIG")
    service_user=$(yq -r ".apps[$app_idx].user // \"dietpi\"" "$CONFIG")
    after=$(yq -r ".apps[$app_idx].after // \"network.target\"" "$CONFIG")

    echo "--- App: $name ---"
    echo "Repo:   $repo"
    echo "Path:   $path"
    echo "Exec:   $exec"
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

            echo "  Dependency: $dep_repo -> $dep_path"
            git_clone_or_update "$dep_repo" "$dep_path"

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
    # Setup Python environment (only if requirements.txt exists)
    # --------------------------------------------------------------------------
    if [[ -n "$venv_name" && -f "$path/requirements.txt" ]]; then
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

            if [[ -n "$dep_python_cmd" ]]; then
                echo "  Installing: ${dep_python_cmd//\{path\}/$dep_path}"
                eval "${dep_python_cmd//\{path\}/$dep_path}"
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

        # Create output directory owned by daemon (rpi-rgb-led-matrix drops to daemon user)
        echo "Setting up application permissions..."
        mkdir -p "$path/output"
        chown -R daemon:daemon "$path/output"
    fi

    # --------------------------------------------------------------------------
    # Setup systemd service
    # --------------------------------------------------------------------------
    echo "Creating systemd service for $name..."

    env_lines=""
    env_count=$(yq -r ".apps[$app_idx].environment | length" "$CONFIG" 2>/dev/null || echo "0")
    if [[ "$env_count" -gt 0 ]]; then
        for ((i=0; i<env_count; i++)); do
            env_var=$(yq -r ".apps[$app_idx].environment[$i]" "$CONFIG")
            env_lines="${env_lines}
Environment=${env_var}"
        done
    fi

    cat > "/etc/systemd/system/${name}.service" <<EOF
[Unit]
Description=$name service
After=$after

[Service]
Type=simple
User=$service_user
WorkingDirectory=$path
ExecStart=$exec${env_lines}
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    chmod 644 "/etc/systemd/system/${name}.service"
    systemctl daemon-reload
    systemctl enable "${name}.service"
    systemctl restart "${name}.service"
    echo "Service $name started."
    echo ""
done

# ============================================================================
# CONFIGURE SYSTEM (shared, run once)
# ============================================================================

# Configure mosquitto to listen on all interfaces
echo "Configuring mosquitto for network access..."
mkdir -p /etc/mosquitto/conf.d
cat > /etc/mosquitto/conf.d/network.conf <<'MQTT_EOF'
listener 1883 0.0.0.0
allow_anonymous true
MQTT_EOF
systemctl restart mosquitto

echo "Configuring ALSA..."
cat > /etc/asound.conf <<'ALSA_EOF'
pcm.!default {
    type hw
    card 0
}

ctl.!default {
    type hw
    card 0
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

# Install Claude backend switch scripts for root and dietpi users
echo "Installing Claude backend switch scripts..."

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

if [[ -f "$SCRIPT_DIR/scripts/use-anthropic.sh" ]]; then
    for USER_HOME in /root /home/dietpi; do
        CLAUDE_SWITCH_DIR="$USER_HOME/.claude-switch"
        mkdir -p "$CLAUDE_SWITCH_DIR"

        cp "$SCRIPT_DIR/scripts/use-anthropic.sh" "$CLAUDE_SWITCH_DIR/"
        cp "$SCRIPT_DIR/scripts/use-zai.sh" "$CLAUDE_SWITCH_DIR/"
        chmod +x "$CLAUDE_SWITCH_DIR"/*.sh
        echo "  Copied switch scripts to $CLAUDE_SWITCH_DIR"

        if [[ ! -f "$CLAUDE_SWITCH_DIR/zai-key" ]]; then
            echo "# Add your Z.ai API key here (sk-zai-...)" > "$CLAUDE_SWITCH_DIR/zai-key.example"
        fi

        BASHRC_FILE="$USER_HOME/.bashrc"
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

    echo "  Add your Z.ai key to ~/.claude-switch/zai-key"
    echo "  (Anthropic uses default authentication, no key needed)"
else
    echo "  Warning: Switch scripts not found in $SCRIPT_DIR/scripts/"
fi

echo ""
echo "=== Bootstrap complete ==="
