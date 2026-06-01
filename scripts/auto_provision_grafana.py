#!/usr/bin/env python3
"""Automatically provision Grafana dashboards, teams, and users based on detected InfluxDB station IDs."""

import csv
import json
import os
import subprocess
import urllib.request
from pathlib import Path

# Paths (relative to /app in container)
SCRIPTS_DIR = Path("/app/scripts")
ACCESS_CONTROL_DIR = Path("/app/grafana/access-control")
TEAMS_CSV = ACCESS_CONTROL_DIR / "teams.csv"
USERS_CSV = ACCESS_CONTROL_DIR / "users.local.csv"
PERMISSIONS_CSV = ACCESS_CONTROL_DIR / "dashboard_permissions.csv"
DASHBOARDS_DIR = Path("/app/grafana/dashboards")
MASTER_DASHBOARD = DASHBOARDS_DIR / "phenohive-mvp-dashboard.json"  # Template

INFLUX_URL = os.getenv("INFLUX_URL", "http://influxdb:8086")
INFLUX_TOKEN = os.getenv("INFLUX_TOKEN", "")
INFLUX_ORG = os.getenv("INFLUX_ORG", "uclouvain")
INFLUX_BUCKET = os.getenv("INFLUX_BUCKET", "phenohive")


def get_station_ids():
    """Query InfluxDB for unique station_id tags."""
    query = f'import "influxdata/influxdb/schema"\nschema.tagValues(bucket: "{INFLUX_BUCKET}", tag: "station_id")'
    url = f"{INFLUX_URL}/api/v2/query?org={INFLUX_ORG}"
    
    payload = {"query": query, "type": "flux"}
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, method="POST", data=data)
    req.add_header("Authorization", f"Token {INFLUX_TOKEN}")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/csv")
    
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            lines = content.splitlines()
            # Influx CSV format usually: ,result,table,_value
            ids = []
            for line in lines:
                if not line or line.startswith("#"):
                    continue
                parts = line.split(",")
                if len(parts) >= 4:
                    val = parts[3].strip().strip('"')
                    if val and val != "_value" and val != "station_id":
                        ids.append(val)
            return sorted(list(set(ids)))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8") if e.fp else ""
        print(f"Error querying InfluxDB for IDs: {e.code} {e.reason} - {body}")
        return []
    except Exception as e:
        print(f"Error querying InfluxDB for IDs: {e}")
        return []


def provision_station(station_id):
    """Ensure team, user, dashboard, and permissions exist for a given station ID."""
    print(f"--- Provisioning Station: {station_id} ---")
    
    team_name = f"phenohive_station_{station_id}"
    username = f"student{station_id}"
    dashboard_uid = f"phenohive-station-{station_id}"

    # 1. Ensure team in teams.csv
    teams = []
    if TEAMS_CSV.exists():
        with open(TEAMS_CSV, "r", encoding="utf-8") as f:
            teams = list(csv.DictReader(f))
    
    if not any(t.get("team_name") == team_name for t in teams):
        print(f"Adding team {team_name} to CSV...")
        with open(TEAMS_CSV, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([team_name, ""])
    
    # 2. Ensure student user in users.local.csv
    users = []
    if USERS_CSV.exists():
        with open(USERS_CSV, "r", encoding="utf-8") as f:
            users = list(csv.DictReader(f))
    
    if not any(u.get("username") == username for u in users):
        print(f"Adding user {username} to CSV...")
        with open(USERS_CSV, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            # username,password,name,email,teams
            writer.writerow([username, f"phenohive_{station_id}", f"Student {station_id}", "", team_name])

    # 3. Ensure permissions in dashboard_permissions.csv
    perms = []
    if PERMISSIONS_CSV.exists():
        with open(PERMISSIONS_CSV, "r", encoding="utf-8") as f:
            perms = list(csv.DictReader(f))
            
    # Check for student View permission
    if not any(p.get("dashboard_uid") == dashboard_uid and p.get("team_name") == team_name for p in perms):
        print(f"Adding View permission for {team_name} on {dashboard_uid}...")
        with open(PERMISSIONS_CSV, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            # dashboard_uid,team_name,permission
            writer.writerow([dashboard_uid, team_name, "View"])

    # Check for assistants Edit permission
    if not any(p.get("dashboard_uid") == dashboard_uid and p.get("team_name") == "phenohive_assistants" for p in perms):
        print(f"Adding Edit permission for assistants on {dashboard_uid}...")
        with open(PERMISSIONS_CSV, "a", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow([dashboard_uid, "phenohive_assistants", "Edit"])

    # 4. Ensure Dashboard JSON exists (Always regenerate to ensure fixes are applied)
    dest_path = DASHBOARDS_DIR / f"{dashboard_uid}.json"
    print(f"Generating/Updating dashboard for {station_id}...")
    try:
        subprocess.run([
            "python3", str(SCRIPTS_DIR / "generate_station_dashboard.py"),
            "--source", str(MASTER_DASHBOARD),
            "--station", station_id,
            "--output", str(dest_path)
        ], check=True)
    except subprocess.CalledProcessError as e:
        print(f"Failed to generate dashboard JSON: {e}")


def main():
    print("Starting PhenoHive Auto-Provisioning cycle...")
    
    # 1. Discover all active station IDs
    ids = get_station_ids()
    if not ids:
        print("No station IDs found in InfluxDB. Skipping provisioning.")
    else:
        print(f"Found station IDs: {ids}")
        for sid in ids:
            provision_station(sid)
    
    # 2. Run the actual Grafana Sync
    print("Running Grafana Access Sync...")
    try:
        cmd = [
            "python3", str(SCRIPTS_DIR / "sync_grafana_access.py"),
            "--grafana-url", os.getenv("GRAFANA_URL", "http://grafana:3000"),
            "--admin-user", os.getenv("GRAFANA_ADMIN_USER", "admin"),
            "--admin-password", os.getenv("GRAFANA_ADMIN_PASSWORD", ""),
            "--teams-csv", str(TEAMS_CSV),
            "--users-csv", str(USERS_CSV),
            "--permissions-csv", str(PERMISSIONS_CSV)
        ]
        subprocess.run(cmd, check=True)
    except subprocess.CalledProcessError as e:
        print(f"Grafana sync failed: {e}")


if __name__ == "__main__":
    main()
