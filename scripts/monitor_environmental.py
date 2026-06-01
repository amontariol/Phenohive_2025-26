import requests
import time
import os
import argparse
from datetime import datetime

def monitor_environmental(ip, interval):
    print(f"Monitoring SHT35 Environmental Data on {ip}...")
    print(f"{'Time':<20} | {'Temp (°C)':<10} | {'Humidity (%)':<10}")
    print("-" * 50)
    
    url = f"http://{ip}:8080/api/status"
    
    try:
        while True:
            try:
                res = requests.get(url, timeout=2)
                res.raise_for_status()
                data = res.json().get('latest_raw_sample', {})
                
                temp = data.get('sht35_air_temperature_c', '--')
                hum = data.get('sht35_air_humidity_pct', '--')
                
                now = datetime.now().strftime('%H:%M:%S')
                
                # Format output
                t_str = f"{temp:.2f}" if isinstance(temp, (int, float)) else str(temp)
                h_str = f"{hum:.2f}" if isinstance(hum, (int, float)) else str(hum)
                
                print(f"{now:<20} | {t_str:<10} | {h_str:<10}")
                
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
    monitor_environmental(args.ip, args.interval)
