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

# Create systemd service
echo "Creating systemd service..."
cat > "/etc/systemd/system/${name}.service" <<EOF
[Unit]
Description=$name service
After=network.target

[Service]
Type=simple
User=dietpi
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
