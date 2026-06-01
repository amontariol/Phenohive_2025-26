import re
import matplotlib.pyplot as plt
import numpy as np
import os

# Use a clean style
plt.style.use('ggplot')

def parse_calibration_data(filepath):
    data = {} # wavelength -> {power, counts}
    
    current_wavelength = None
    current_power = None
    
    if not os.path.exists(filepath):
        print(f"Error: {filepath} not found")
        return {}

    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        
    for i, line in enumerate(lines):
        line = line.strip()
        if not line: continue
        
        # Match wavelength: "300:", "380nm:", "400nm:"
        m_wave = re.match(r'^(\d{3})(?:nm)?:', line)
        if m_wave:
            current_wavelength = int(m_wave.group(1))
            current_power = None # reset power for new wavelength
            continue
            
        # Match power: "power: 32.2nW", "power: 0.333µW", "power: 16.29µW"
        m_pow = re.match(r'power:\s*([\d.]+)\s*([nµmμ]W)', line)
        if m_pow:
            val = float(m_pow.group(1))
            unit = m_pow.group(2)
            if unit == 'nW': val *= 1e-3
            elif unit in ['µW', 'μW']: val *= 1.0 # normalize to uW
            elif unit == 'mW': val *= 1e3
            current_power = val
            continue
            
        # Match data line: "16:10:32 | 4.0 | 4.0 | ..."
        if '|' in line and current_wavelength is not None:
            parts = line.split('|')
            if len(parts) > 10:
                try:
                    # Extract channel counts (columns 1 to 10)
                    vals = []
                    for p in parts[1:11]:
                        v = p.strip()
                        if not v: vals.append(0.0)
                        else: vals.append(float(v))
                    
                    if current_wavelength not in data:
                        data[current_wavelength] = {'power': current_power, 'counts': vals}
                    else:
                        # If multiple lines, take the one with higher values (not noise)
                        if sum(vals) > sum(data[current_wavelength]['counts']):
                            data[current_wavelength]['counts'] = vals
                except ValueError:
                    pass
    return data

def generate_plot():
    data = parse_calibration_data('scripts/calibration/manual_calibration_results_tcs3448.txt')
    if not data: return
    
    wavelengths = sorted(data.keys())
    
    # Channel labels from the log header
    channels = ['F1 (407)', 'F2 (448)', 'F3 (500)', 'F4 (534)', 'F5 (594)', 'F6 (665)', 'F7 (715)', 'F8 (865)', 'NIR (940)', 'FXL (628)']
    
    fig, ax = plt.subplots(figsize=(10, 6))
    
    # Colors for the spectrum
    colors = plt.cm.rainbow(np.linspace(0, 1, len(channels)))

    for i, ch in enumerate(channels):
        y = []
        x = []
        for w in wavelengths:
            counts = data[w]['counts'][i]
            p = data[w]['power']
            
            # Rejection of saturated values (65535)
            if counts >= 65535:
                continue
                
            if p is not None and p > 0:
                # Responsivity = Counts / Power
                y.append(counts / p)
                x.append(w)
        
        if y:
            y = np.array(y)
            # Normalize to peak for curve shape visualization
            y = y / np.max(y)
            ax.plot(x, y, label=ch, color=colors[i], linewidth=2, alpha=0.8)
            
    ax.set_xlabel('Wavelength (nm)', fontsize=12)
    ax.set_ylabel('Normalized Responsivity (counts/μW)', fontsize=12)
    ax.set_title('Empirical Spectral Sensitivity Curves (TCS3448 Calibration)', fontsize=14)
    ax.legend(title='Sensor Channels', bbox_to_anchor=(1.05, 1), loc='upper left')
    ax.set_xlim(350, 950)
    ax.grid(True, linestyle='--', alpha=0.6)
    
    plt.tight_layout()
    output_path = 'docs/Report/Images/empirical_spectral_response.pdf'
    plt.savefig(output_path)
    print(f"Plot saved to {output_path}")

if __name__ == "__main__":
    generate_plot()
