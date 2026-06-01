#!/usr/bin/env python3
"""Interactive calibration script for the HX711 scale."""

import sys
import time
import statistics
import configparser
from pathlib import Path

# Add src to path if needed
sys.path.append(str(Path(__file__).parent.parent))

try:
    from hx711 import HX711
except ImportError:
    print("Error: 'hx711' library not found. Install it with: pip install hx711")
    sys.exit(1)

def get_raw_median(hx, samples=15):
    data = hx.get_raw_data(times=samples)
    if not data:
        return None
    return statistics.median(data)

def main():
    config_path = Path(__file__).parent.parent / "config.ini"
    config = configparser.ConfigParser()
    config.read(config_path)

    dout = config.getint("scale_hx711", "dout_pin", fallback=5)
    sck = config.getint("scale_hx711", "pd_sck_pin", fallback=6)

    print(f"--- PhenoHive Scale Calibration ---")
    print(f"Initializing HX711 on DOUT={dout}, SCK={sck}...")

    try:
        hx = HX711(dout_pin=dout, pd_sck_pin=sck)
        hx.reset()
    except Exception as e:
        print(f"Failed to initialize HX711: {e}")
        return

    print("\nSTEP 1: TARE (Zeroing)")
    print("Ensure the scale is EMPTY and stable.")
    input("Press Enter to start taring...")
    
    print("Reading zero offset...")
    tare_val = get_raw_median(hx, samples=30)
    if tare_val is None:
        print("Error: Could not read from sensor. Check wiring.")
        return
    
    print(f"Tare value (Zero Offset): {tare_val}")

    print("\nSTEP 2: CALIBRATION FACTOR")
    print("Place a KNOWN weight on the scale.")
    known_weight = input("Enter the weight in grams (e.g., 100): ")
    try:
        known_weight = float(known_weight)
    except ValueError:
        print("Invalid weight. Exiting.")
        return

    input(f"Press Enter once the {known_weight}g weight is stable...")
    
    print("Reading weighted value...")
    reading = get_raw_median(hx, samples=30)
    if reading is None:
        print("Error: Could not read from sensor.")
        return

    # calculation: (reading - tare) / known_weight = factor
    # raw_bits = (weight * factor) + tare
    # weight = (raw_bits - tare) / factor
    # Wait, my RealScaleHX711 uses: weight = (raw - tare) * factor
    # So factor = known_weight / (reading - tare)
    
    diff = reading - tare_val
    if abs(diff) < 100:
        print("Warning: Difference too small. Is the weight too light or sensor not responding?")
        factor = 1.0
    else:
        factor = known_weight / diff

    print(f"\n--- CALIBRATION RESULTS ---")
    print(f"Tare Value: {tare_val}")
    print(f"Calibration Factor: {factor}")
    print(f"Formula: weight_g = (raw - {tare_val}) * {factor}")

    print("\nSTEP 3: WEIGHT OFFSET (optional)")
    print("Use this to subtract the weight of a container (pot, tray, etc.) from all readings.")
    print("If you skip this, weight_offset will be set to 0.0.")
    do_offset = input("Do you want to measure a container offset now? (y/N): ").lower()

    weight_offset = 0.0
    if do_offset == 'y':
        print("\nPlace the EMPTY container (pot, tray, etc.) on the scale.")
        input("Press Enter once it is stable...")

        print("Reading container weight...")
        offset_reading = get_raw_median(hx, samples=30)
        if offset_reading is None:
            print("Error: Could not read from sensor. Skipping offset.")
        else:
            weight_offset = (offset_reading - tare_val) * factor
            print(f"Container weight: {weight_offset:.2f} g  → this will be subtracted from all readings")

    print(f"\n--- FINAL CALIBRATION RESULTS ---")
    print(f"Tare Value:         {tare_val}")
    print(f"Calibration Factor: {factor}")
    print(f"Weight Offset:      {weight_offset:.2f} g")
    print(f"Formula: weight_g = (raw - {tare_val}) * {factor} - {weight_offset:.2f}")

    print("\nTo apply these settings, update [scale_hx711] in config.ini:")
    print(f"tare = {tare_val}")
    print(f"calibration_factor = {factor}")
    print(f"weight_offset = {weight_offset}")

    save = input("\nWould you like to save these to config.ini now? (y/N): ").lower()
    if save == 'y':
        config.set("scale_hx711", "tare", str(round(tare_val, 2)))
        config.set("scale_hx711", "calibration_factor", str(factor))
        config.set("scale_hx711", "weight_offset", str(round(weight_offset, 2)))
        with open(config_path, "w") as f:
            config.write(f)
        print("Settings saved to config.ini!")

if __name__ == "__main__":
    main()
