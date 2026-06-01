"""Spawn N mock PhenoHive stations for load testing.

Each station gets its own config file and data directory, then runs as a
subprocess of this process.  All processes share the host's network stack,
so Tailscale on the Windows machine covers all of them — no per-container
Tailscale setup required.

Usage:
    # Set token once, then run:
    $env:INFLUXDB_TOKEN = "phenohive-local-dev-token"
    python scripts/spawn_mock_stations.py

    # Or pass it inline:
    python scripts/spawn_mock_stations.py --influxdb-token phenohive-local-dev-token

    # Fewer stations, faster intervals:
    python scripts/spawn_mock_stations.py --count 5 --collection-interval 30 --publish-interval 60

    # Single measurement cycle then exit (smoke test):
    python scripts/spawn_mock_stations.py --once
"""

from __future__ import annotations

import argparse
import configparser
import os
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULTS = REPO_ROOT / "config.defaults.ini"

INFLUXDB_URL = "http://100.117.27.12:8081"
INFLUXDB_ORG = "uclouvain"
INFLUXDB_BUCKET = "phenohive"


def generate_config(station_id: int, base_dir: Path, args: argparse.Namespace) -> Path:
    station_dir = base_dir / f"station-{station_id:02d}"
    station_dir.mkdir(parents=True, exist_ok=True)

    cfg = configparser.ConfigParser()
    cfg.read(DEFAULTS)

    cfg["general"]["mock_mode"] = "True"
    cfg["general"]["station_id"] = str(station_id)
    cfg["general"]["hardware_uuid"] = str(uuid.uuid4())

    cfg["sampling"]["collection_interval_s"] = str(args.collection_interval)
    cfg["sampling"]["publish_interval_s"] = str(args.publish_interval)
    cfg["sampling"]["default_samples"] = "1"
    cfg["sampling"]["sht35_samples"] = "1"
    cfg["sampling"]["tcs3448_samples"] = "1"
    cfg["sampling"]["scale_hx711_samples"] = "1"

    cfg["influxdb"]["enabled"] = "True"
    cfg["influxdb"]["url"] = INFLUXDB_URL
    cfg["influxdb"]["org"] = INFLUXDB_ORG
    cfg["influxdb"]["bucket"] = INFLUXDB_BUCKET
    cfg["influxdb"]["token"] = args.influxdb_token

    cfg["paths"]["log_dir"] = str(station_dir / "logs")
    cfg["paths"]["csv_path"] = str(station_dir / "measurements.csv")
    cfg["paths"]["offline_queue_path"] = str(station_dir / "offline_queue.jsonl")

    cfg["camera"]["enabled"] = "False"
    cfg["camera"]["image_output_dir"] = str(station_dir / "images")

    cfg["debug_ui"]["enabled"] = "False"
    cfg["led_strip"]["enabled"] = "False"

    config_path = station_dir / "config.ini"
    with config_path.open("w") as f:
        cfg.write(f)
    return config_path


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Spawn mock PhenoHive stations for load testing",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--count", type=int, default=19, metavar="N",
                        help="Number of mock stations (default: 19)")
    parser.add_argument("--start-id", type=int, default=2, metavar="ID",
                        help="First station_id (default: 2; station 1 is the real RPI)")
    parser.add_argument("--collection-interval", type=int, default=120, metavar="S",
                        help="collection_interval_s per station (default: 120)")
    parser.add_argument("--publish-interval", type=int, default=600, metavar="S",
                        help="publish_interval_s per station (default: 600)")
    parser.add_argument(
        "--influxdb-token",
        default=os.environ.get("INFLUXDB_TOKEN") or os.environ.get("INFLUX_TOKEN") or "",
        metavar="TOKEN",
        help="InfluxDB token (default: $INFLUXDB_TOKEN / $INFLUX_TOKEN env var)",
    )
    parser.add_argument("--once", action="store_true",
                        help="Pass --once to each station (one measurement cycle then exit)")
    parser.add_argument("--data-dir",
                        default=str(REPO_ROOT / "data" / "mock-stations"),
                        metavar="DIR",
                        help="Base directory for per-station data and configs")
    parser.add_argument("--start-delay", type=float, default=0.5, metavar="S",
                        help="Seconds between launching each station (default: 0.5)")
    parser.add_argument("--status-interval", type=int, default=60, metavar="S",
                        help="Seconds between status lines when running continuously (default: 60)")
    args = parser.parse_args()

    if not DEFAULTS.exists():
        print(f"ERROR: config.defaults.ini not found at {DEFAULTS}", file=sys.stderr)
        sys.exit(1)

    if not args.influxdb_token:
        print(
            "ERROR: InfluxDB token is required.\n"
            "  Pass --influxdb-token <token>  or  set $INFLUXDB_TOKEN in your environment.",
            file=sys.stderr,
        )
        sys.exit(1)

    base_dir = Path(args.data_dir)
    base_dir.mkdir(parents=True, exist_ok=True)

    python = sys.executable
    station_ids = list(range(args.start_id, args.start_id + args.count))

    print(f"Spawning {args.count} mock stations (IDs {station_ids[0]}–{station_ids[-1]})")
    print(f"InfluxDB: {INFLUXDB_URL}  org={INFLUXDB_ORG}  bucket={INFLUXDB_BUCKET}")
    print(f"Intervals: collect={args.collection_interval}s  publish={args.publish_interval}s")
    print(f"Data directory: {base_dir}")
    print()

    processes: list[tuple[int, subprocess.Popen, object]] = []

    for sid in station_ids:
        config_path = generate_config(sid, base_dir, args)
        cmd = [python, str(REPO_ROOT / "main.py"), "--config", str(config_path)]
        if args.once:
            cmd.append("--once")

        stdout_log = (config_path.parent / "stdout.log").open("w")
        proc = subprocess.Popen(cmd, cwd=str(REPO_ROOT), stdout=stdout_log, stderr=subprocess.STDOUT)
        processes.append((sid, proc, stdout_log))
        print(f"  Station {sid:02d}  PID {proc.pid:6d}  log → {config_path.parent / 'stdout.log'}")

        if args.start_delay > 0 and sid != station_ids[-1]:
            time.sleep(args.start_delay)

    print()

    if args.once:
        print("Waiting for all stations to complete one cycle…")
        for sid, proc, lf in processes:
            proc.wait()
            lf.close()
            status = "OK" if proc.returncode == 0 else f"exit {proc.returncode}"
            print(f"  Station {sid:02d}  {status}")
        return

    print(f"All {args.count} stations running. Press Ctrl+C to stop all.\n")

    def shutdown(signum, frame):  # noqa: ARG001
        print("\nShutting down all stations…")
        for _, proc, _ in processes:
            proc.terminate()
        for sid, proc, lf in processes:
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
            lf.close()
            print(f"  Station {sid:02d}  stopped (exit {proc.returncode})")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, shutdown)

    while True:
        time.sleep(args.status_interval)
        alive = sum(1 for _, p, _ in processes if p.poll() is None)
        crashed = [(sid, p.returncode) for sid, p, _ in processes if p.poll() is not None]
        ts = time.strftime("%H:%M:%S")
        print(f"[{ts}] {alive}/{args.count} stations alive", end="")
        if crashed:
            print(f"  — crashed: {crashed}", end="")
        print()


if __name__ == "__main__":
    main()
