#!/bin/bash
# wait_for_network.sh
# This script loops until the network is reachable.

TARGET="8.8.8.8"
RETRY_INTERVAL=5
MAX_RETRIES=12 # 12 * 5s = 60 seconds
COUNT=0

echo "Waiting for network connectivity to $TARGET (Timeout: 60s)..."

while ! ping -c 1 -W 2 "$TARGET" > /dev/null 2>&1; do
    COUNT=$((COUNT + 1))
    if [ $COUNT -ge $MAX_RETRIES ]; then
        echo "Network timeout reached. Proceeding anyway to keep system responsive."
        exit 0
    fi
    echo "Network unreachable (Try $COUNT/$MAX_RETRIES). Retrying in $RETRY_INTERVAL seconds..."
    sleep "$RETRY_INTERVAL"
done

echo "Network is up! Now waiting for NTP time sync..."
COUNT=0
while ! timedatectl status | grep -q "System clock synchronized: yes"; do
    COUNT=$((COUNT + 1))
    if [ $COUNT -ge 6 ]; then
        echo "NTP sync timeout. Proceeding with local clock."
        break
    fi
    echo "Waiting for NTP sync (Try $COUNT/6)..."
    sleep 5
done

echo "System ready! Proceeding..."
exit 0
