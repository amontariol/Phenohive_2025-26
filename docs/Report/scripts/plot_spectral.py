import numpy as np
import matplotlib.pyplot as plt

plt.style.use('seaborn-v0_8-paper')
plt.rcParams.update({
    "font.family": "serif",
    "font.serif": ["Computer Modern Roman"],
    "text.usetex": False,
    "axes.labelsize": 10,
    "legend.fontsize": 9,
    "xtick.labelsize": 9,
    "ytick.labelsize": 9,
})

def generate_spectral_plot():
    channels = ['Red', 'Green', 'Blue', 'Clear']
    
    # Normalized counts (simulated)
    sunlight = [0.28, 0.32, 0.25, 1.0]
    grow_led = [0.45, 0.10, 0.40, 1.0]
    
    x = np.arange(len(channels))
    width = 0.35
    
    fig, ax = plt.subplots(figsize=(6, 3.5), layout='constrained')
    
    ax.bar(x - width/2, sunlight, width, label='Full Spectrum (Sunlight)', color='#fdbb2d', alpha=0.8)
    ax.bar(x + width/2, grow_led, width, label='Grow LED (R/B heavy)', color='#9b59b6', alpha=0.8)
    
    ax.set_ylabel('Normalized Response')
    ax.set_title('Spectral Signatures: Sunlight vs. Growth LED')
    ax.set_xticks(x)
    ax.set_xticklabels(channels)
    ax.legend()
    ax.grid(axis='y', linestyle='--', alpha=0.6)
    
    plt.savefig('../Images/spectral_signatures.pdf')
    print("Saved spectral_signatures.pdf to Images/")

if __name__ == "__main__":
    generate_spectral_plot()
