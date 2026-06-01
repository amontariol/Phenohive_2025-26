#!/bin/bash
# I2C bus recovery: run before phenohive starts.
#
# When phenohive is killed mid-transaction the BCM2835 I2C controller can be
# left with a slave holding SDA low (waiting for clock pulses to complete the
# interrupted byte).  This makes every subsequent I2C_RDWR call return EIO or
# EREMOTEIO, even though i2cdetect (SMBUS ioctl) still sees the devices.
#
# Fix: temporarily reassign SCL (GPIO3) to a GPIO output and clock it 9 times
# while SDA (GPIO2) is in input mode.  After 9 pulses any stuck byte is
# clocked out, the slave releases SDA, and we emit a STOP condition.
# Restoring ALT0 on both pins reinitialises the BCM2835 I2C controller.
#
# Then send soft-reset commands to each sensor so they start from a known
# state before the Python process opens the bus.

set -euo pipefail

I2C_BUS=1
SHT35_ADDR="0x45"
TCS3448_ADDR="0x59"
SCL_GPIO=3   # GPIO3 = SCL1
SDA_GPIO=2   # GPIO2 = SDA1

log() { echo "[i2c_recovery] $*"; }

# ── Step 1: 9-clock-pulse recovery ──────────────────────────────────────────
log "Starting 9-clock-pulse I2C bus recovery (SCL=GPIO${SCL_GPIO}, SDA=GPIO${SDA_GPIO})"

pinctrl set ${SDA_GPIO} ip          # SDA: input, let pullup hold it HIGH
pinctrl set ${SCL_GPIO} op dh       # SCL: output HIGH

for i in $(seq 1 9); do
    pinctrl set ${SCL_GPIO} dl      # SCL LOW
    sleep 0.001
    pinctrl set ${SCL_GPIO} dh      # SCL HIGH
    sleep 0.001
done

# STOP condition: SDA LOW→HIGH while SCL is HIGH
pinctrl set ${SDA_GPIO} op dl       # SDA LOW
sleep 0.001
pinctrl set ${SCL_GPIO} dh          # SCL HIGH (already is, but be explicit)
sleep 0.001
pinctrl set ${SDA_GPIO} dh          # SDA HIGH → STOP

sleep 0.005

# ── Step 2: restore I2C ALT0 on both pins ───────────────────────────────────
log "Restoring GPIO${SDA_GPIO}/GPIO${SCL_GPIO} to I2C ALT0"
pinctrl set ${SDA_GPIO} a0
pinctrl set ${SCL_GPIO} a0
sleep 0.1

# ── Step 3: soft-reset sensors via SMBus ioctl (reliable path) ──────────────
log "Sending soft-reset to SHT35 at ${SHT35_ADDR}"
i2cset -y ${I2C_BUS} ${SHT35_ADDR} 0x30 0xa2 2>/dev/null \
    && log "SHT35 soft reset OK" \
    || log "SHT35 soft reset failed (sensor may still be recovering)"

log "Powering off TCS3448 at ${TCS3448_ADDR}"
i2cset -y ${I2C_BUS} ${TCS3448_ADDR} 0x80 0x00 2>/dev/null \
    && log "TCS3448 power-off OK" \
    || log "TCS3448 power-off failed (sensor may still be recovering)"

sleep 0.1
log "I2C bus recovery complete"
