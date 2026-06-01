"""Synchronize Grafana users, teams, and dashboard permissions from CSV files."""

from __future__ import annotations

import argparse
import base64
import csv
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


@dataclass
class GrafanaClient:
    base_url: str
    admin_user: str
    admin_password: str
    dry_run: bool = False
    _dry_run_team_id_seq: int = -1

    def _headers(self) -> dict[str, str]:
        token = base64.b64encode(f"{self.admin_user}:{self.admin_password}".encode("utf-8")).decode("ascii")
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        if self.dry_run and method in {"POST", "PUT", "PATCH", "DELETE"}:
            print(f"[DRY-RUN] {method} {path} payload={payload}")
            return {}

        url = f"{self.base_url.rstrip('/')}{path}"
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")

        req = Request(url=url, method=method, data=data, headers=self._headers())
        try:
            with urlopen(req, timeout=20) as response:
                body = response.read().decode("utf-8")
                if not body:
                    return {}
                return json.loads(body)
        except HTTPError as exc:
            body = exc.read().decode("utf-8") if exc.fp else ""
            raise RuntimeError(f"Grafana API error {exc.code} {method} {path}: {body}") from exc

    def lookup_user(self, login: str) -> dict[str, Any] | None:
        query = urlencode({"loginOrEmail": login})
        try:
            return self.request("GET", f"/api/users/lookup?{query}")
        except RuntimeError as exc:
            if " 404 " in str(exc):
                return None
            raise

    def ensure_user(self, login: str, password: str, name: str, email: str) -> int:
        existing = self.lookup_user(login)
        if existing is None:
            payload = {
                "name": name or login,
                "email": email or f"{login}@local.invalid",
                "login": login,
                "password": password,
            }
            result = self.request("POST", "/api/admin/users", payload)
            return int(result["id"])

        user_id = int(existing["id"])
        self.request("PUT", f"/api/admin/users/{user_id}/password", {"password": password})
        return user_id

    def list_teams(self) -> dict[str, int]:
        teams = self.request("GET", "/api/teams/search?perpage=1000")
        output: dict[str, int] = {}
        for team in teams.get("teams", []):
            output[str(team["name"])] = int(team["id"])
        return output

    def ensure_team(self, team_name: str, email: str = "") -> int:
        teams = self.list_teams()
        if team_name in teams:
            return teams[team_name]

        if self.dry_run:
            self._dry_run_team_id_seq -= 1
            synthetic_id = self._dry_run_team_id_seq
            print(f"[DRY-RUN] create team {team_name} -> synthetic id {synthetic_id}")
            return synthetic_id

        payload = {"name": team_name, "email": email}
        result = self.request("POST", "/api/teams", payload)
        return int(result["teamId"])

    def add_user_to_team(self, team_id: int, user_id: int) -> None:
        try:
            self.request("POST", f"/api/teams/{team_id}/members", {"userId": user_id})
        except RuntimeError as exc:
            # Ignore if already a member.
            if "already added" in str(exc).lower():
                return
            raise

    def set_dashboard_permissions(self, dashboard_uid: str, items: list[dict[str, Any]]) -> None:
        self.request("POST", f"/api/dashboards/uid/{dashboard_uid}/permissions", {"items": items})

    def set_folder_permissions(self, folder_uid: str, items: list[dict[str, Any]]) -> None:
        self.request("POST", f"/api/folders/{folder_uid}/permissions", {"items": items})

    def list_folders(self) -> dict[str, str]:
        folders = self.request("GET", "/api/folders")
        return {f["title"]: f["uid"] for f in folders}


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        raise FileNotFoundError(f"Missing CSV file: {path}")

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [dict(row) for row in reader]


def read_csv_if_exists(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    return read_csv(path)


def parse_team_list(value: str) -> list[str]:
    return [part.strip() for part in value.split(";") if part.strip()]


def permission_to_int(value: str) -> int:
    normalized = value.strip().lower()
    mapping = {
        "view": 1,
        "edit": 2,
        "admin": 4,
    }
    if normalized not in mapping:
        raise ValueError(f"Unsupported permission value: {value}")
    return mapping[normalized]


def sync_teams(client: GrafanaClient, teams_csv: Path) -> dict[str, int]:
    rows = read_csv(teams_csv)
    team_ids: dict[str, int] = {}
    for row in rows:
        team_name = row.get("team_name", "").strip()
        if not team_name:
            continue
        team_email = row.get("team_email", "").strip()
        team_id = client.ensure_team(team_name, email=team_email)
        team_ids[team_name] = team_id
        print(f"Team ready: {team_name} (id={team_id})")
    return team_ids


def sync_users(client: GrafanaClient, users_csv: Path, team_ids: dict[str, int]) -> None:
    rows = read_csv_if_exists(users_csv)
    if not rows:
        print(f"No users to sync from {users_csv}")
        return
    for row in rows:
        login = row.get("username", "").strip()
        password = row.get("password", "").strip()
        if not login:
            continue
        if not password:
            raise ValueError(f"User {login} is missing password")

        name = row.get("name", "").strip() or login
        email = row.get("email", "").strip()
        teams = parse_team_list(row.get("teams", ""))

        user_id = client.ensure_user(login=login, password=password, name=name, email=email)
        print(f"User ready: {login} (id={user_id})")

        for team_name in teams:
            if team_name not in team_ids:
                raise ValueError(f"Unknown team in users CSV: {team_name}")
            client.add_user_to_team(team_ids[team_name], user_id)
            print(f"  - added to team {team_name}")


def sync_permissions(client: GrafanaClient, permissions_csv: Path, team_ids: dict[str, int]) -> None:
    rows = read_csv(permissions_csv)
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        uid = row.get("dashboard_uid", "").strip() or row.get("uid", "").strip()
        team_name = row.get("team_name", "").strip()
        permission_value = row.get("permission", "").strip()
        if not uid or not team_name or not permission_value:
            continue

        if team_name not in team_ids:
            raise ValueError(f"Unknown team in permissions CSV: {team_name}")

        item = {
            "teamId": team_ids[team_name],
            "permission": permission_to_int(permission_value),
        }
        grouped.setdefault(uid, []).append(item)

    # Automatically identify folders to apply correct API endpoint
    folders = client.list_folders()
    folder_uids = set(folders.values())

    for uid, items in grouped.items():
        if uid in folder_uids:
            client.set_folder_permissions(uid, items)
            print(f"Folder permissions updated: {uid} ({len(items)} entries)")
        else:
            client.set_dashboard_permissions(uid, items)
            print(f"Dashboard permissions updated: {uid} ({len(items)} entries)")

    # Special case: Hard-lock the "PhenoHive" folder if not already in CSV to prevent global visibility
    # This is a safety measure to remove the default "Viewer" role from the folder.
    phenohive_uid = folders.get("PhenoHive")
    if phenohive_uid and phenohive_uid not in grouped:
        print(f"Securing 'PhenoHive' folder (uid={phenohive_uid}) by restricting to assistants only...")
        safety_items = [
            {"teamId": team_ids["phenohive_assistants"], "permission": 2}
        ]
        
        client.set_folder_permissions(phenohive_uid, safety_items)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync Grafana access control from editable CSV files")
    parser.add_argument("--grafana-url", default=os.getenv("GRAFANA_URL", "http://localhost:3000"), help="Grafana base URL")
    parser.add_argument("--admin-user", default=os.getenv("GRAFANA_ADMIN_USER", "admin"), help="Grafana admin user")
    parser.add_argument(
        "--admin-password",
        default=os.getenv("GRAFANA_ADMIN_PASSWORD", ""),
        help="Grafana admin password (required; pass via env or CLI)",
    )
    parser.add_argument("--teams-csv", default="grafana/access-control/teams.csv", help="Path to teams CSV")
    parser.add_argument("--users-csv", default="grafana/access-control/users.local.csv", help="Path to users CSV")
    parser.add_argument(
        "--permissions-csv",
        default="grafana/access-control/dashboard_permissions.csv",
        help="Path to dashboard permissions CSV",
    )
    parser.add_argument("--dry-run", action="store_true", help="Show planned API writes without changing Grafana")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if not args.admin_password:
        raise ValueError("Grafana admin password is required via --admin-password or GRAFANA_ADMIN_PASSWORD")

    client = GrafanaClient(
        base_url=args.grafana_url,
        admin_user=args.admin_user,
        admin_password=args.admin_password,
        dry_run=args.dry_run,
    )

    teams_csv = Path(args.teams_csv)
    users_csv = Path(args.users_csv)
    permissions_csv = Path(args.permissions_csv)

    team_ids = sync_teams(client, teams_csv)
    sync_users(client, users_csv, team_ids)
    sync_permissions(client, permissions_csv, team_ids)
    print("Grafana access sync completed.")


if __name__ == "__main__":
    main()
