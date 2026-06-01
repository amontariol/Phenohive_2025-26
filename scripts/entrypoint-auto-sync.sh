#!/bin/bash
set -euo pipefail

# Configuration
INTERVAL_S="${AUTO_SYNC_INTERVAL_S:-300}" # Default 5 minutes
GRAFANA_URL="${GRAFANA_URL:-http://grafana:3000}"
INFLUX_URL="${INFLUX_URL:-http://influxdb:8086}"

echo "Starting PhenoHive Auto-Provisioning Service..."
echo "Sync interval: ${INTERVAL_S}s"
echo "Grafana: ${GRAFANA_URL}"
echo "InfluxDB: ${INFLUX_URL}"

# Initial wait for services to be ready
echo "Waiting for InfluxDB and Grafana to be healthy..."
sleep 10 

while true; do
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Starting sync cycle..."
  
  if python3 /app/scripts/auto_provision_grafana.py; then
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Sync cycle completed successfully."
  else
    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Sync cycle failed."
  fi
  
  echo "Sleeping for ${INTERVAL_S}s..."
  sleep "${INTERVAL_S}"
done
