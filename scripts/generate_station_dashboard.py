#!/usr/bin/env python3
"""Generate a station-specific Grafana dashboard with hardcoded data filters."""

import argparse
import json
import os
import re
import sys

def process_dashboard(data_dict, station_id):
    """Deep copy dashboard and inject station_id filters into all queries."""
    dash = json.loads(json.dumps(data_dict))
    dash["uid"] = f"phenohive-station-{station_id}"
    dash["title"] = f"PhenoHive Station {station_id}"
    
    def process_panels(panels):
        for panel in panels:
            if "targets" in panel:
                for target in panel["targets"]:
                    if "query" in target:
                        query = target["query"]
                        # 1. Replace dynamic variable-based station_id filters
                        query = re.sub(
                            r'\|>\s*filter\(fn:\s*\(r\)\s*=>\s*"\$\{station_id\}"\s*==\s*"all"\s*or\s*"\$\{station_id\}"\s*==\s*"\$__all"\s*or\s*r\.station_id\s*==\s*"\$\{station_id\}"\)',
                            f'|> filter(fn: (r) => r.station_id == "{station_id}")',
                            query
                        )
                        # 2. Replace any existing hardcoded station_id filters
                        query = re.sub(
                            r'\|>\s*filter\(fn:\s*\(r\)\s*=>\s*r\.station_id\s*==\s*".*?"\)',
                            f'|> filter(fn: (r) => r.station_id == "{station_id}")',
                            query
                        )
                        # 3. Inject after measurement filter ONLY if not already present
                        if f'r.station_id == "{station_id}"' not in query:
                            query = re.sub(
                                r'(\|>\s*filter\(fn:\s*\(r\)\s*=>\s*r\._measurement\s*==\s*"phenohive_measurements"\))',
                                r'\1\n  |> filter(fn: (r) => r.station_id == "' + station_id + '")',
                                query
                            )
                        # 4. Final deduplication of exact lines
                        lines = query.split('\n')
                        unique_lines = []
                        for line in lines:
                            if line.strip() not in unique_lines or not line.strip().startswith('|> filter'):
                                unique_lines.append(line)
                        query = '\n'.join(unique_lines)
                        target["query"] = query
            if "panels" in panel:
                process_panels(panel["panels"])

    if "panels" in dash:
        process_panels(dash["panels"])
        
    # Remove templating variable for station_id
    if "templating" in dash and "list" in dash["templating"]:
        new_list = [v for v in dash["templating"]["list"] if v.get("name") != "station_id"]
        dash["templating"]["list"] = new_list
        
    return dash

def main():
    parser = argparse.ArgumentParser(description="Generate station-specific dashboards.")
    parser.add_argument("--source", required=True, help="Path to the master dashboard JSON")
    parser.add_argument("--station", required=True, help="Station ID (e.g., 03)")
    parser.add_argument("--output", help="Output path (default: grafana/dashboards/phenohive-station-<ID>.json)")

    args = parser.parse_args()

    if not os.path.exists(args.source):
        print(f"Error: Source file {args.source} not found.")
        sys.exit(1)

    with open(args.source, "r") as f:
        data = json.load(f)

    output_dash = process_dashboard(data, args.station)

    output_path = args.output or f"grafana/dashboards/phenohive-station-{args.station}.json"
    
    with open(output_path, "w") as f:
        json.dump(output_dash, f, indent=2)

    print(f"Successfully generated: {output_path}")

if __name__ == "__main__":
    main()
