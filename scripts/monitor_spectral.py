import requests
import time
import argparse
from datetime import datetime

# Mapping of TCS3448 internal channel names to their physical center wavelengths
CHANNEL_MAP = {
    "f1": "407nm",
    "f2": "448nm",
    "fz": "480nm",
    "f3": "500nm",
    "f4": "534nm",
    "fy": "593nm",
    "f5": "594nm",
    "fxl": "628nm",
    "f6": "665nm",
    "f7": "715nm",
    "f8": "865nm",
    "nir": "940nm",
    "lux": "Lux"
}


# The order we want to display them in the table
CHANNELS = ["f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "nir", "fxl", "lux"]

def monitor_spectral(ip, interval):
    print(f"Monitoring TCS3448 Spectral Data on {ip}...")
    
    # Header showing nanometers/labels
    header = f"{'Time':<10} | " + " | ".join([f"{CHANNEL_MAP.get(ch, ch):<8}" for ch in CHANNELS])
    print(header)
    print("-" * len(header))
    
    url = f"http://{ip}:8080/api/status"
    
    try:
        while True:
            try:
                res = requests.get(url, timeout=2)
                res.raise_for_status()
                data = res.json().get('latest_raw_sample', {})
                
                now = datetime.now().strftime('%H:%M:%S')
                
                row_vals = []
                for ch in CHANNELS:
                    val = data.get(f'tcs3448_{ch}', '--')
                    v_str = f"{val:.1f}" if isinstance(val, (int, float)) else str(val)
                    row_vals.append(f"{v_str:<8}")
                
                print(f"{now:<10} | " + " | ".join(row_vals))
                
            except requests.exceptions.RequestException as e:
                print(f"Connection error: {e}")
            
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nMonitoring stopped.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--ip", default="127.0.0.1")
    parser.add_argument("--interval", type=float, default=1.0)
    args = parser.parse_args()
    monitor_spectral(args.ip, args.interval)
