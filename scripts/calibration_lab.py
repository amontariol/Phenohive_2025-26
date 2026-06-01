import requests
import time
import csv
import argparse
import json
from datetime import datetime

# --- CALIBRATION CONFIGURATION ---
DEFAULT_WAVELENGTHS = range(400, 1010, 10)

# Target Sensors
TARGET_PREFIXES = ["sht35_", "tcs3448_"]

def get_phenohive_data(ip):
    """Fetch and filter live data from PhenoHive API."""
    url = f"http://{ip}:8080/api/status"
    try:
        res = requests.get(url, timeout=5)
        res.raise_for_status()
        full_data = res.json().get('latest_raw_sample', {})
        
        # Filter for relevant sensors only
        filtered = {k: v for k, v in full_data.items() 
                    if any(k.startswith(p) for p in TARGET_PREFIXES)}
        return filtered
    except Exception as e:
        print(f"Error reaching PhenoHive at {ip}: {e}")
        return {}

def set_lab_wavelength(ip, wavelength):
    """Placeholder: Call the DOpeS API on the lab PC to set wavelength."""
    print(f"--- Setting Monochromator to {wavelength}nm ---")
    try:
        # This matches the endpoint in our mock script
        requests.post(f"http://{ip}:5000/set_wavelength", json={"nm": wavelength}, timeout=5)
    except Exception:
        pass
    time.sleep(2) 

def get_reference_power(ip):
    """Placeholder: Get the reading from the lab's reference power meter."""
    try:
        res = requests.get(f"http://{ip}:5000/get_power", timeout=5)
        return res.json().get('watts', 0.0)
    except Exception:
        return 0.0

def run_calibration(hive_ip, lab_ip, output_file):
    print(f"Starting Calibration Sweep for SHT35 and TCS3448...")
    print(f"Connecting to Hive: {hive_ip}")
    print(f"Connecting to Lab: {lab_ip}")
    
    with open(output_file, mode='w', newline='') as f:
        writer = None
        
        for wl in DEFAULT_WAVELENGTHS:
            set_lab_wavelength(lab_ip, wl)
            ref_p = get_reference_power(lab_ip)
            hive_data = get_phenohive_data(hive_ip)
            
            if not hive_data:
                print(f"Skipping {wl}nm due to missing PhenoHive data.")
                continue
            
            row = {
                "timestamp_utc": datetime.now().isoformat(),
                "target_wavelength_nm": wl,
                "ref_power_watts": ref_p
            }
            row.update(hive_data)
            
            if writer is None:
                # Use all current keys as fieldnames
                writer = csv.DictWriter(f, fieldnames=row.keys(), extrasaction='ignore')
                writer.writeheader()
            
            writer.writerow(row)
            f.flush()
            print(f"Captured {wl}nm | Ref Power: {ref_p}")

    print(f"\nCalibration Complete! File saved: {output_file}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--hive-ip", required=True)
    parser.add_argument("--lab-ip", default="127.0.0.1")
    parser.add_argument("--output")
    args = parser.parse_args()
    
    output = args.output or f"calibration_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    run_calibration(args.hive_ip, args.lab_ip, output)
