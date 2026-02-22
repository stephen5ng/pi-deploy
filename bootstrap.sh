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

# ============================================================================
# PARSE CONFIGURATION
# ============================================================================
name=$(yq -r '.apps[0].name' "$CONFIG")
repo=$(yq -r '.apps[0].repo' "$CONFIG")
branch=$(yq -r '.apps[0].branch' "$CONFIG")
path=$(yq -r '.apps[0].path' "$CONFIG")
exec=$(yq -r '.apps[0].exec' "$CONFIG")

echo "App:    $name"
echo "Repo:   $repo"
echo "Branch: $branch"
echo "Path:   $path"
echo "Exec:   $exec"
echo ""

# ============================================================================
# INSTALL SYSTEM PACKAGES
# ============================================================================
apt_packages=$(yq -r '.apps[0].apt_packages[]? // empty' "$CONFIG")
if [[ -n "$apt_packages" ]]; then
    echo "Installing apt packages..."
    apt-get update
    echo "$apt_packages" | xargs apt-get install -y
fi

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

# Replace pygame's bundled SDL2 (compiled without kmsdrm) with our kmsdrm-enabled version
# The venv python path is not known yet, so this is done after venv setup below

# ============================================================================
# BUILD EXTERNAL DEPENDENCIES
# ============================================================================
dep_count=$(yq -r '.apps[0].dependencies | length' "$CONFIG" 2>/dev/null || echo "0")
if [[ "$dep_count" -gt 0 ]]; then
    echo "Processing $dep_count dependencies..."
    for ((i=0; i<dep_count; i++)); do
        dep_repo=$(yq -r ".apps[0].dependencies[$i].repo" "$CONFIG")
        dep_path=$(yq -r ".apps[0].dependencies[$i].path" "$CONFIG")
        dep_build=$(yq -r ".apps[0].dependencies[$i].build_cmd // empty" "$CONFIG")

        echo "  Dependency: $dep_repo -> $dep_path"
        git_clone_or_update "$dep_repo" "$dep_path"

        # Build if build_cmd specified
        if [[ -n "$dep_build" ]]; then
            echo "    Building: ${dep_build//\{path\}/$dep_path}"
            eval "${dep_build//\{path\}/$dep_path}"
        fi
    done
fi

# ============================================================================
# DEPLOY APPLICATION
# ============================================================================
echo "Processing app repo..."
mkdir -p "$path"
git_clone_or_update "$repo" "$path" "$branch"

# ============================================================================
# SETUP PYTHON ENVIRONMENT
# ============================================================================
if [[ ! -d "$path/cube_env" ]]; then
    echo "Creating virtual environment..."
    python3 -m venv "$path/cube_env"
fi

# Install requirements if they exist
if [[ -f "$path/requirements.txt" ]]; then
    echo "Installing requirements..."
    source "$path/cube_env/bin/activate"
    pip install --upgrade pip
    pip install -r "$path/requirements.txt"
    deactivate
fi

# Install Python bindings for dependencies (using data from earlier loop)
if [[ "$dep_count" -gt 0 ]]; then
    echo "Installing Python bindings for dependencies..."
    for ((i=0; i<dep_count; i++)); do
        dep_path=$(yq -r ".apps[0].dependencies[$i].path" "$CONFIG")
        dep_python_cmd=$(yq -r ".apps[0].dependencies[$i].install_python_cmd // empty" "$CONFIG")

        if [[ -n "$dep_python_cmd" ]]; then
            echo "  Installing: ${dep_python_cmd//\{path\}/$dep_path}"
            eval "${dep_python_cmd//\{path\}/$dep_path}"
        fi
    done
fi

# Replace pygame's bundled SDL2 with our kmsdrm-enabled version
echo "Replacing pygame's bundled SDL2 with kmsdrm-enabled version..."
PYGAME_LIBS=$(find "$path/cube_env" -name "pygame.libs" -type d 2>/dev/null | head -1)
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

# ============================================================================
# CONFIGURE SYSTEM
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

# Extract service user from config (default to dietpi if not specified)
service_user=$(yq -r '.apps[0].user // "dietpi"' "$CONFIG")

# Add user to audio group for audio device access
echo "Adding $service_user to audio group..."
usermod -a -G audio "$service_user"

# Install Claude backend switch scripts for root and dietpi users
echo "Installing Claude backend switch scripts..."

# Get the directory where this script is located
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

if [[ -f "$SCRIPT_DIR/scripts/use-anthropic.sh" ]]; then
    for USER_HOME in /root /home/dietpi; do
        CLAUDE_SWITCH_DIR="$USER_HOME/.claude-switch"
        mkdir -p "$CLAUDE_SWITCH_DIR"

        cp "$SCRIPT_DIR/scripts/use-anthropic.sh" "$CLAUDE_SWITCH_DIR/"
        cp "$SCRIPT_DIR/scripts/use-zai.sh" "$CLAUDE_SWITCH_DIR/"
        chmod +x "$CLAUDE_SWITCH_DIR"/*.sh
        echo "  Copied switch scripts to $CLAUDE_SWITCH_DIR"

        # Create example key file for Z.ai (Anthropic uses default auth, no key needed)
        if [[ ! -f "$CLAUDE_SWITCH_DIR/zai-key" ]]; then
            echo "# Add your Z.ai API key here (sk-zai-...)" > "$CLAUDE_SWITCH_DIR/zai-key.example"
        fi

        # Add aliases to .bashrc if not already present
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

        # Fix ownership for dietpi user
        if [[ "$USER_HOME" == "/home/dietpi" ]]; then
            chown -R dietpi:dietpi "$CLAUDE_SWITCH_DIR"
        fi
    done

    echo "  Add your Z.ai key to ~/.claude-switch/zai-key"
    echo "  (Anthropic uses default authentication, no key needed)"
else
    echo "  Warning: Switch scripts not found in $SCRIPT_DIR/scripts/"
fi

# ============================================================================
# SETUP SYSTEMD SERVICE
# ============================================================================
echo "Creating systemd service..."

# Build Environment directives from config
env_lines=""
env_count=$(yq -r '.apps[0].environment | length' "$CONFIG" 2>/dev/null || echo "0")
if [[ "$env_count" -gt 0 ]]; then
    for ((i=0; i<env_count; i++)); do
        env_var=$(yq -r ".apps[0].environment[$i]" "$CONFIG")
        env_lines="${env_lines}Environment=${env_var}\n"
    done
fi

cat > "/etc/systemd/system/${name}.service" <<EOF
[Unit]
Description=$name service
After=network.target

[Service]
Type=simple
User=$service_user
WorkingDirectory=$path
ExecStart=$exec
$(echo -e "$env_lines")Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "/etc/systemd/system/${name}.service"

# Create output directory and set permissions
# The rpi-rgb-led-matrix library drops privileges from root to daemon user
# so the output directory needs to be owned by daemon (not the entire app path)
echo "Setting up application permissions..."
mkdir -p "$path/output"
chown -R daemon:daemon "$path/output"

# Enable and start service
echo "Starting service..."
systemctl daemon-reload
systemctl enable "${name}.service"
systemctl restart "${name}.service"

echo ""
echo "=== Bootstrap complete ==="
