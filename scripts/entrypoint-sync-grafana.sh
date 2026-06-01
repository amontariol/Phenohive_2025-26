#!/bin/bash
set -euo pipefail

GRAFANA_URL="${GRAFANA_URL:-http://grafana:3000}"
GRAFANA_ADMIN_USER="${GRAFANA_ADMIN_USER:-admin}"
GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD:-}"

if [ -z "${GRAFANA_ADMIN_PASSWORD}" ]; then
  echo "GRAFANA_ADMIN_PASSWORD is required"
  exit 1
fi

echo "Starting Grafana access sync with retry..."

max_attempts="${GRAFANA_SYNC_MAX_ATTEMPTS:-60}"
retry_delay_s="${GRAFANA_SYNC_RETRY_DELAY_S:-2}"
attempt=1

while [ "$attempt" -le "$max_attempts" ]; do
  if python3 /app/scripts/sync_grafana_access.py \
    --grafana-url "${GRAFANA_URL}" \
    --admin-user "${GRAFANA_ADMIN_USER}" \
    --admin-password "${GRAFANA_ADMIN_PASSWORD}" \
    --teams-csv "/app/grafana/access-control/teams.csv" \
    --users-csv "/app/grafana/access-control/users.local.csv" \
    --permissions-csv "/app/grafana/access-control/dashboard_permissions.csv"; then
    echo "Grafana access sync completed successfully"
    exit 0
  fi

  echo "Sync attempt ${attempt}/${max_attempts} failed; retrying in ${retry_delay_s}s..."
  attempt=$((attempt + 1))
  sleep "${retry_delay_s}"
done

echo "Grafana access sync failed after ${max_attempts} attempts"
exit 1
