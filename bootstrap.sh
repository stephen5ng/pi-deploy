#!/bin/bash
set -euo pipefail

CONFIG="apps.yaml"

echo "=== Bootstrapping from $CONFIG ==="

# Extract config values (first app only)
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

# Install apt packages if specified
apt_packages=$(yq -r '.apps[0].apt_packages[]? // empty' "$CONFIG")
if [[ -n "$apt_packages" ]]; then
    echo "Installing apt packages..."
    apt-get update
    echo "$apt_packages" | xargs apt-get install -y
fi

# Process dependencies if specified
dep_count=$(yq -r '.apps[0].dependencies | length' "$CONFIG" 2>/dev/null || echo "0")
if [[ "$dep_count" -gt 0 ]]; then
    echo "Processing $dep_count dependencies..."
    for ((i=0; i<dep_count; i++)); do
        dep_repo=$(yq -r ".apps[0].dependencies[$i].repo" "$CONFIG")
        dep_path=$(yq -r ".apps[0].dependencies[$i].path" "$CONFIG")
        dep_build=$(yq -r ".apps[0].dependencies[$i].build_cmd // empty" "$CONFIG")
        dep_python=$(yq -r ".apps[0].dependencies[$i].install_python // false" "$CONFIG")

        echo "  Dependency: $dep_repo -> $dep_path"

        # Clone or update dependency
        if [[ -d "$dep_path/.git" ]]; then
            echo "    Updating..."
            git -C "$dep_path" pull
        else
            echo "    Cloning..."
            git clone "$dep_repo" "$dep_path"
        fi

        # Build if build_cmd specified
        if [[ -n "$dep_build" ]]; then
            build_cmd="${dep_build//\{path\}/$dep_path}"
            echo "    Building: $build_cmd"
            eval "$build_cmd"
        fi
    done
fi

# Clone or update repo
if [[ -d "$path/.git" ]]; then
    echo "Updating repo..."
    git -C "$path" fetch --all
    git -C "$path" checkout "$branch"
    git -C "$path" pull origin "$branch"
else
    echo "Cloning repo..."
    mkdir -p "$path"
    git clone --branch "$branch" "$repo" "$path"
fi

# Create Python virtual environment
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

# Install Python bindings for dependencies
if [[ "$dep_count" -gt 0 ]]; then
    for ((i=0; i<dep_count; i++)); do
        dep_path=$(yq -r ".apps[0].dependencies[$i].path" "$CONFIG")
        dep_python_cmd=$(yq -r ".apps[0].dependencies[$i].install_python_cmd // empty" "$CONFIG")

        if [[ -n "$dep_python_cmd" ]]; then
            install_cmd="${dep_python_cmd//\{path\}/$dep_path}"
            echo "Installing Python bindings: $install_cmd"
            eval "$install_cmd"
        fi
    done
fi

# Configure ALSA for USB audio device
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

# Extract service user from config (default to dietpi if not specified)
service_user=$(yq -r '.apps[0].user // "dietpi"' "$CONFIG")

# Add user to audio group for audio device access
echo "Adding $service_user to audio group..."
usermod -a -G audio "$service_user"

# Create systemd service
echo "Creating systemd service..."
cat > "/etc/systemd/system/${name}.service" <<EOF
[Unit]
Description=$name service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$path
ExecStart=$exec
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

chmod 644 "/etc/systemd/system/${name}.service"

# Enable and start service
echo "Starting service..."
systemctl daemon-reload
systemctl enable "${name}.service"
systemctl restart "${name}.service"

echo ""
echo "=== Bootstrap complete ==="
