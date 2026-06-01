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

def calculate_svp(temp):
    """Saturated Vapor Pressure in kPa"""
    return 0.61078 * np.exp((17.27 * temp) / (temp + 237.3))

def generate_vpd_plot():
    # Range of temperatures and humidities
    temp = np.linspace(15, 35, 100)
    rh_levels = [40, 60, 80]
    
    fig, ax = plt.subplots(figsize=(6, 3.5), layout='constrained')
    
    for rh in rh_levels:
        svp = calculate_svp(temp)
        avp = svp * (rh / 100)
        vpd = svp - avp
        ax.plot(temp, vpd, label=f'RH = {rh}%')
    
    ax.set_xlabel('Temperature (°C)')
    ax.set_ylabel('VPD (kPa)')
    ax.set_title('Vapor Pressure Deficit (VPD) as a function of Temperature')
    ax.legend()
    ax.grid(True, linestyle='--', alpha=0.6)
    
    # Highlight critical zones
    ax.fill_between(temp, 0.8, 1.2, color='green', alpha=0.1, label='Transpiration Sweet Spot')
    
    plt.savefig('../Images/vpd_calculation.pdf')
    print("Saved vpd_calculation.pdf to Images/")

if __name__ == "__main__":
    generate_vpd_plot()
