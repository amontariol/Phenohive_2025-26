# Grafana Access Control (TA Editable)

This folder lets teaching assistants manage student groups with editable CSV files.

## Files

- teams.csv
  - Defines Grafana teams to ensure exist.
- users.template.csv
  - Example format for user creation and team assignments.
- users.local.csv
  - Actual usernames/passwords used during sync. Committed as a header-only file; populate locally before running the sync.
- dashboard_permissions.csv
  - Which team can view/edit which dashboard.

## Populate User File

`users.local.csv` is already present in the repository with only the header row. Edit it directly to add student and TA accounts — no copy step needed.

## CSV Format

users.local.csv columns:

- username
- password
- name
- email
- teams

`teams` accepts multiple team names separated by `;`.

Example row:

```csv
alice_st01,StrongPass123,Alice Student,alice@example.edu,phenohive_station_01;phenohive_students
```

## Run Sync

### Automatic (Docker Compose)

When you run `docker-compose up`, the sync happens **automatically**:

1. The `grafana_access_sync` service waits for Grafana to be ready
2. Reads all three CSV files (teams.csv, users.local.csv, dashboard_permissions.csv)
3. Creates/updates teams, users, and permissions in Grafana
4. Logs output to docker-compose console

No manual action needed—just populate your CSV files and start the containers.

### Manual (After Docker is Running)

If you update CSVs after docker-compose is already running:

```bash
python3 scripts/sync_grafana_access.py \
  --grafana-url http://localhost:3000 \
  --admin-user admin \
  --admin-password admin12345
```

Dry run (preview changes without applying):

```bash
python3 scripts/sync_grafana_access.py --dry-run
```

## Group-based Station Access Pattern

Recommended for one station used by a group of students:

1. Create one station team (example: phenohive_station_01) in teams.csv.
2. Add all student accounts for that group in users.local.csv with teams including:
   - phenohive_station_01
   - phenohive_students
3. Keep dashboard_permissions.csv mapping station teams and student teams to Student Data Explorer.

## Notes

- The script creates missing users and teams.
- If a user already exists, the script updates password.
- Dashboard permissions are reconciled from dashboard_permissions.csv.
- `users.local.csv` is tracked by git but committed empty (header only). Add rows locally and do not commit populated versions containing real credentials.
