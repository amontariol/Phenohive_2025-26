#!/bin/bash
set -euo pipefail

if [ "$#" -ne 2 ]; then
    echo "Usage: $0 <SSH_TARGET> <HOST_PC_IP>"
    echo "Example: $0 root@192.168.1.100 192.168.1.50"
    echo "Example: $0 root@fe80::74e1:2eda:ba95:73ee%enp58s0 192.168.68.100"
    exit 1
fi

SSH_TARGET="$1"
HOST_IP="$2"
REMOTE_DIR="/opt/phenohive"
PASSWORD="dietpi"

export SSHPASS="$PASSWORD"
SSH_CMD="sshpass -e ssh -o StrictHostKeyChecking=no"
RSYNC_CMD="rsync -avz -e 'sshpass -e ssh -o StrictHostKeyChecking=no' --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude 'data/*' --exclude 'logs/*' --exclude 'dietpi/*' --exclude 'config.ini'"

# Try to read local token if available
TOKEN="admin_token_replace_me"
if [ -f .env ]; then
    # Extract token using grep to avoid executing the whole .env
    EXTRACTED=$(grep "^INFLUXDB_INIT_ADMIN_TOKEN=" .env | cut -d '=' -f2)
    if [ ! -z "$EXTRACTED" ]; then
        TOKEN="$EXTRACTED"
    fi
fi

echo "Deploying to $SSH_TARGET..."

# Parse SSH_TARGET for rsync IPv6 support
if [[ "$SSH_TARGET" == *@*:* ]]; then
    USER_PART="${SSH_TARGET%@*}"
    HOST_PART="${SSH_TARGET#*@}"
    RSYNC_TARGET="${USER_PART}@[${HOST_PART}]"
else
    RSYNC_TARGET="$SSH_TARGET"
fi

# Ensure target directory exists
$SSH_CMD "$SSH_TARGET" "mkdir -p $REMOTE_DIR"

# Copy code
echo "Syncing code..."
eval $RSYNC_CMD ./ "$RSYNC_TARGET:$REMOTE_DIR/"

# Bootstrap config.ini from defaults if it does not exist
$SSH_CMD "$SSH_TARGET" "[ -f $REMOTE_DIR/config.ini ] || cp $REMOTE_DIR/config.defaults.ini $REMOTE_DIR/config.ini"

# Setup virtual environment and dependencies
echo "Setting up Python environment on Pi..."
$SSH_CMD "$SSH_TARGET" "cd $REMOTE_DIR && \
    apt-get update && apt-get install -y python3-venv python3-pip build-essential python3-dev libcap-dev pkg-config python3-picamera2 libcamera-apps && \
    python3 -m venv --system-site-packages .venv && \
    .venv/bin/pip install -r requirements.txt"

# Create .env on Pi
echo "Configuring environment variables..."
$SSH_CMD "$SSH_TARGET" "cat > $REMOTE_DIR/.env <<EOF
MOCK_MODE=False
TIME_SYNC_ENABLED=True
INFLUXDB_ENABLED=True
INFLUX_URL=http://$HOST_IP:8081
INFLUX_TOKEN=$TOKEN
INFLUX_ORG=uclouvain
INFLUX_BUCKET=phenohive
DEBUG_UI_ENABLED=True
DEBUG_UI_HOST=0.0.0.0
EOF"

# Install and start systemd service
echo "Installing systemd service..."
$SSH_CMD "$SSH_TARGET" "chmod +x $REMOTE_DIR/scripts/wait_for_network.sh && \
    cp $REMOTE_DIR/infrastructure/systemd/phenohive.service /etc/systemd/system/ && \
    systemctl daemon-reload && \
    systemctl enable phenohive && \
    systemctl restart phenohive"

echo ""
echo "Deployment complete!"
echo "The PhenoHive application is now running as a background service on the Pi."
echo "You can view its logs on the Pi by running: $SSH_CMD $SSH_TARGET 'journalctl -fu phenohive'"
