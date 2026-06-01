import numpy as np
import matplotlib.pyplot as plt
from scipy.stats import median_abs_deviation

# Set style for academic report
plt.style.use('seaborn-v0_8-paper')
plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["Computer Modern Roman"],
    "text.usetex": False, # Set to True if you have TeX installed
    "axes.labelsize": 10,
    "legend.fontsize": 9,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
})

def generate_mad_plot():
    # 1. Generate data
    t = np.linspace(0, 10, 200)
    signal = 25 + 2 * np.sin(t)
    noise = np.random.normal(0, 0.2, 200)
    data = signal + noise
    
    # Add some outliers
    outliers = [20, 55, 120, 180]
    data[outliers] = [32, 18, 35, 15]
    
    # 2. Apply MAD filtering
    med = np.median(data)
    mad = median_abs_deviation(data)
    threshold = 3.0
    
    # Simple MAD filter for visualization
    is_outlier = np.abs(data - med) > threshold * mad
    cleaned_data = np.copy(data)
    cleaned_data[is_outlier] = med
    
    # 3. Plot
    fig, ax = plt.subplots(figsize=(6, 3.5), layout='constrained')
    
    ax.plot(t, data, color='#cccccc', label='Raw Sensor Data', alpha=0.7)
    ax.scatter(t[is_outlier], data[is_outlier], color='#d62728', marker='x', s=40, label='Detected Outliers')
    ax.plot(t, cleaned_data, color='#1f77b4', linewidth=1.5, label='MAD Filtered')
    
    ax.set_xlabel('Time (relative)')
    ax.set_ylabel('Measurement Value')
    ax.set_title('Outlier Detection using Median Absolute Deviation (MAD)')
    ax.legend()
    ax.grid(True, linestyle='--', alpha=0.6)
    
    plt.savefig('../Images/mad_filtering.pdf')
    print("Saved mad_filtering.pdf to Images/")

if __name__ == "__main__":
    generate_mad_plot()
