#!/bin/bash
set -euo pipefail

usage() {
  cat <<'EOF'
Build a plug-and-play DietPi golden image for Raspberry Pi.

This script bakes the PhenoHive application, all system dependencies, and
station-specific configuration directly into a DietPi disk image. Flashing
the resulting image to an SD card and powering on the Pi is all that is
needed — no post-flash file editing required.

Station configuration is read from plain-text files in the same directory as
this script. Create them before running the build:

  dietpi/phenohive_server.txt    — Tailscale IP of the UCLouvain VM
  dietpi/phenohive_token.txt     — InfluxDB write token  (keep out of git)
  dietpi/phenohive_station_id.txt — unique station ID (e.g. 01, 02)

Usage:
  sudo ./build_image.sh \
    [--input /path/to/DietPi.img[.xz]] \
    [--output /path/to/phenohive.img] \
    [--tailscale-key tskey-auth-XXXXX] \
    [--arch aarch64|armhf]

If --input is omitted, it is auto-detected from the script directory
(first matching DietPi*.img.xz or DietPi*.img).

--tailscale-key embeds a reusable pre-auth key so each station joins the
Tailscale network automatically on first boot. Generate one from the
Tailscale admin console. Without this flag stations must be authenticated
manually after first boot.

Notes:
- Run this on a Linux host.
- Required host tools: losetup, mount, umount, chroot, sed, cp, xz, rsync.
- Required host packages: qemu-user-static and qemu-user-static-binfmt.
EOF
}

require_cmd() {
  local cmd="$1"
  if ! command -v "$cmd" >/dev/null 2>&1; then
    echo "Missing command: $cmd"
    exit 1
  fi
}

ensure_binfmt_for_arch() {
  local arch="$1"
  local key=""
  local qemu_path=""
  local magic=""
  local mask=""

  case "$arch" in
    aarch64)
      key="qemu-aarch64"
      qemu_path="/usr/bin/qemu-aarch64-static"
      magic='\\x7f\\x45\\x4c\\x46\\x02\\x01\\x01\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x02\\x00\\xb7\\x00'
      mask='\\xff\\xff\\xff\\xff\\xff\\xff\\xff\\x00\\xff\\xff\\xff\\xff\\xff\\xff\\xff\\xff\\xfe\\xff\\xff\\xff'
      ;;
    armhf)
      key="qemu-arm"
      qemu_path="/usr/bin/qemu-arm-static"
      magic='\\x7f\\x45\\x4c\\x46\\x01\\x01\\x01\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x00\\x02\\x00\\x28\\x00'
      mask='\\xff\\xff\\xff\\xff\\xff\\xff\\xff\\x00\\xff\\xff\\xff\\xff\\xff\\xff\\xff\\xff\\xfe\\xff\\xff\\xff'
      ;;
    *)
      echo "Unsupported architecture for binfmt check: $arch"
      exit 1
      ;;
  esac

  if ! mountpoint -q /proc/sys/fs/binfmt_misc; then
    mount -t binfmt_misc binfmt_misc /proc/sys/fs/binfmt_misc || true
  fi

  if [[ ! -e "/proc/sys/fs/binfmt_misc/${key}" ]]; then
    if command -v systemctl >/dev/null 2>&1; then
      systemctl start systemd-binfmt 2>/dev/null || true
    fi
  fi

  # Fallback for distros that provide qemu static binaries without binfmt presets.
  if [[ ! -e "/proc/sys/fs/binfmt_misc/${key}" ]]; then
    if [[ -x "$qemu_path" && -w /proc/sys/fs/binfmt_misc/register ]]; then
      printf ':%s:M::%b:%b:%s:F\n' "$key" "$magic" "$mask" "$qemu_path" >/proc/sys/fs/binfmt_misc/register || true
    fi
  fi

  if [[ ! -e "/proc/sys/fs/binfmt_misc/${key}" ]]; then
    echo "Missing binfmt registration for ${key}."
    echo "On Arch, install and load it with:"
    echo "  sudo pacman -S --needed qemu-user-static qemu-user-static-binfmt"
    echo "  sudo systemctl restart systemd-binfmt"
    echo "Then re-run this script."
    exit 1
  fi
}

set_kv() {
  local file="$1"
  local key="$2"
  local value="$3"
  if grep -qE "^${key}=" "$file"; then
    sed -i "s|^${key}=.*|${key}=${value}|" "$file"
  else
    echo "${key}=${value}" >>"$file"
  fi
}

INPUT_IMG=""
OUTPUT_IMG=""
ARCH=""
ARCH_REQUESTED=""
WIFI_COUNTRY_CODE="${WIFI_COUNTRY_CODE:-BE}"
TAILSCALE_KEY=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --input)
      INPUT_IMG="$2"
      shift 2
      ;;
    --output)
      OUTPUT_IMG="$2"
      shift 2
      ;;
    --tailscale-key)
      TAILSCALE_KEY="$2"
      shift 2
      ;;
    --wifi-connect-bin)
      # Deprecated: kept for backward compatibility, ignored.
      shift 2
      ;;
    --arch)
      ARCH="$2"
      ARCH_REQUESTED="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1"
      usage
      exit 1
      ;;
  esac
done

# Read station configuration from local files in the dietpi/ directory.
# Create these files before running the build script; they are baked directly
# into the image so no post-flash SD card editing is required.
# Sensitive files (token, tailscale key) should be gitignored.
BAKED_SERVER_IP=""
BAKED_TOKEN=""
BAKED_STATION_ID=""

for var_file_pair in \
    "BAKED_SERVER_IP:${SCRIPT_DIR}/phenohive_server.txt" \
    "BAKED_TOKEN:${SCRIPT_DIR}/phenohive_token.txt" \
    "BAKED_STATION_ID:${SCRIPT_DIR}/phenohive_station_id.txt"; do
  var="${var_file_pair%%:*}"
  file="${var_file_pair##*:}"
  if [[ -f "$file" ]]; then
    val=$(tr -d '\r\n ' <"$file")
    [[ -n "$val" ]] && printf -v "$var" '%s' "$val"
    echo "Loaded ${var} from $(basename "$file")"
  fi
done

if [[ -z "$INPUT_IMG" ]]; then
  shopt -s nullglob
  candidates=("$SCRIPT_DIR"/DietPi*.img.xz)
  if [[ ${#candidates[@]} -eq 0 ]]; then
    candidates=("$SCRIPT_DIR"/DietPi*.img)
  fi
  shopt -u nullglob

  if [[ ${#candidates[@]} -eq 1 ]]; then
    INPUT_IMG="${candidates[0]}"
  elif [[ ${#candidates[@]} -gt 1 ]]; then
    echo "Multiple DietPi images found in $SCRIPT_DIR. Please pass --input explicitly."
    printf ' - %s\n' "${candidates[@]}"
    exit 1
  fi
fi

if [[ -z "$INPUT_IMG" ]]; then
  echo "Could not auto-detect required input image."
  usage
  exit 1
fi

if [[ "$EUID" -ne 0 ]]; then
  echo "Run as root (use sudo)."
  exit 1
fi

if [[ ! -f "$INPUT_IMG" ]]; then
  echo "Input image not found: $INPUT_IMG"
  exit 1
fi

if [[ -z "$OUTPUT_IMG" ]]; then
  OUTPUT_IMG="${SCRIPT_DIR}/phenohive.img"
fi

echo "Using input image      : $INPUT_IMG"
if [[ -n "$ARCH_REQUESTED" ]]; then
  echo "Requested architecture : $ARCH_REQUESTED"
fi
echo "Output image           : $OUTPUT_IMG"

require_cmd losetup
require_cmd mount
require_cmd umount
require_cmd chroot
require_cmd sed
require_cmd cp
require_cmd xz
require_cmd file

WORKDIR="$(mktemp -d)"
MNT_BOOT="$WORKDIR/boot"
MNT_ROOT="$WORKDIR/root"
LOOP_DEV=""

cleanup() {
  set +e

  # Unmount in reverse dependency order; fall back to lazy umount if busy.
  for p in "$MNT_ROOT/sys" "$MNT_ROOT/proc" "$MNT_ROOT/dev/pts" "$MNT_ROOT/dev"; do
    if mountpoint -q "$p"; then
      umount "$p" 2>/dev/null || umount -l "$p" 2>/dev/null || true
    fi
  done

  if mountpoint -q "$MNT_BOOT"; then
    umount "$MNT_BOOT" 2>/dev/null || umount -l "$MNT_BOOT" 2>/dev/null || true
  fi
  if mountpoint -q "$MNT_ROOT"; then
    umount "$MNT_ROOT" 2>/dev/null || umount -l "$MNT_ROOT" 2>/dev/null || true
  fi

  if [[ -n "$LOOP_DEV" ]]; then
    losetup -d "$LOOP_DEV" 2>/dev/null || true
  fi

  rm -rf "$WORKDIR"
}
trap cleanup EXIT

mkdir -p "$MNT_BOOT" "$MNT_ROOT"

echo "Preparing output image: $OUTPUT_IMG"
if [[ "$INPUT_IMG" == *.xz ]]; then
  xz -dc "$INPUT_IMG" >"$OUTPUT_IMG"
else
  cp "$INPUT_IMG" "$OUTPUT_IMG"
fi

echo "Expanding image size by 5GB to accommodate dependencies..."
truncate -s +5G "$OUTPUT_IMG"
# Resize the second partition (rootfs) to fill the new space
parted -s "$OUTPUT_IMG" resizepart 2 100%

echo "Attaching loop device"
LOOP_DEV="$(losetup --find --show --partscan "$OUTPUT_IMG")"

echo "Resizing filesystem on ${LOOP_DEV}p2..."
e2fsck -f -y "${LOOP_DEV}p2" || true
resize2fs "${LOOP_DEV}p2"

if [[ ! -b "${LOOP_DEV}p1" || ! -b "${LOOP_DEV}p2" ]]; then
  echo "Could not find image partitions (${LOOP_DEV}p1 and ${LOOP_DEV}p2)."
  echo "Try a newer losetup util-linux, or map partitions manually."
  exit 1
fi

echo "Mounting partitions"
mount "${LOOP_DEV}p1" "$MNT_BOOT"
mount "${LOOP_DEV}p2" "$MNT_ROOT"

echo "Detecting target image architecture from rootfs"
TARGET_ELF=""
for candidate in "$MNT_ROOT/usr/bin/apt-get" "$MNT_ROOT/bin/bash" "$MNT_ROOT/bin/sh"; do
  if [[ -f "$candidate" ]]; then
    TARGET_ELF="$candidate"
    break
  fi
done

if [[ -z "$TARGET_ELF" ]]; then
  echo "Could not find an ELF binary in rootfs to detect architecture."
  exit 1
fi

ELF_INFO="$(file -b "$TARGET_ELF" | tr '[:upper:]' '[:lower:]')"
if [[ "$ELF_INFO" == *"aarch64"* ]]; then
  DETECTED_ARCH="aarch64"
  QEMU_BIN="/usr/bin/qemu-aarch64-static"
elif [[ "$ELF_INFO" == *"arm"* ]]; then
  DETECTED_ARCH="armhf"
  QEMU_BIN="/usr/bin/qemu-arm-static"
else
  echo "Unsupported target architecture from '$TARGET_ELF':"
  echo "$ELF_INFO"
  exit 1
fi

if [[ -n "$ARCH_REQUESTED" && "$ARCH_REQUESTED" != "$DETECTED_ARCH" ]]; then
  echo "Requested --arch '$ARCH_REQUESTED' does not match rootfs '$DETECTED_ARCH'."
  echo "Using detected architecture '$DETECTED_ARCH' to avoid exec format errors."
fi

ARCH="$DETECTED_ARCH"
echo "Detected architecture   : $ARCH"

if [[ ! -x "$QEMU_BIN" ]]; then
  echo "Missing emulator binary: $QEMU_BIN"
  echo "Install host package: qemu-user-static"
  exit 1
fi

echo "Checking host binfmt registration for $ARCH"
ensure_binfmt_for_arch "$ARCH"

echo "Copying runtime files into image"
cp "$QEMU_BIN" "$MNT_ROOT/usr/bin/"

echo "Preparing chroot mounts"
mount --bind /dev "$MNT_ROOT/dev"
mount --bind /dev/pts "$MNT_ROOT/dev/pts"
mount --bind /proc "$MNT_ROOT/proc"
mount --bind /sys "$MNT_ROOT/sys"

echo "Installing packages inside image rootfs"
QEMU_IN_CHROOT="/usr/bin/$(basename "$QEMU_BIN")"
chroot "$MNT_ROOT" "$QEMU_IN_CHROOT" /usr/bin/apt-get update
# rpi-utils provides pinctrl, which the i2c_bus_recovery.sh script uses to
# bit-bang the I2C bus clock line for bus-stuck recovery on BCM2835.
chroot "$MNT_ROOT" "$QEMU_IN_CHROOT" /usr/bin/env DEBIAN_FRONTEND=noninteractive /usr/bin/apt-get install -y --no-install-recommends network-manager wpasupplicant iw comitup curl jq ca-certificates i2c-tools rpi-utils python3-libcamera python3-picamera2 libcamera-apps avahi-daemon
chroot "$MNT_ROOT" "$QEMU_IN_CHROOT" /usr/bin/apt-get clean

echo "Installing Tailscale..."
DEBIAN_CODENAME=$(chroot "$MNT_ROOT" "$QEMU_IN_CHROOT" /bin/bash -c \
    '. /etc/os-release && echo "$VERSION_CODENAME"' 2>/dev/null || echo "bookworm")
echo "Detected Debian codename: ${DEBIAN_CODENAME}"
chroot "$MNT_ROOT" "$QEMU_IN_CHROOT" /bin/bash -c "
    curl -fsSL https://pkgs.tailscale.com/stable/debian/${DEBIAN_CODENAME}.noarmor.gpg \
        -o /usr/share/keyrings/tailscale-archive-keyring.gpg
    echo 'deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/debian ${DEBIAN_CODENAME} main' \
        > /etc/apt/sources.list.d/tailscale.list
    apt-get update
    apt-get install -y --no-install-recommends tailscale
    apt-get clean
"

echo "Configuring NetworkManager and provisioning service"
mkdir -p "$MNT_ROOT/etc/NetworkManager/conf.d"
cat >"$MNT_ROOT/etc/NetworkManager/conf.d/10-ifupdown-managed.conf" <<'EOF'
[ifupdown]
managed=true
EOF

# Force NetworkManager to use keyfile backend only, so ifupdown definitions
# cannot reclaim wlan0 during first boot.
cat >"$MNT_ROOT/etc/NetworkManager/NetworkManager.conf" <<'EOF'
[main]
plugins=keyfile

[device]
wifi.scan-rand-mac-address=no
EOF

# Disable WiFi power-saving fleet-wide. The Pi's onboard Broadcom adapter
# otherwise dozes when idle and silently drops *inbound* connections
# (SSH / Tailscale / debug dashboard) until traffic wakes the radio — a
# frequent cause of "the station fell off Tailscale" in the field. On this
# NetworkManager-managed image, NM owns the setting, so it must be told here;
# a bare `iw ... power_save off` would get re-enabled on the next reconnect.
# wifi.powersave: 2 = disable, 3 = enable, 0 = use-default.
cat >"$MNT_ROOT/etc/NetworkManager/conf.d/30-wifi-powersave-off.conf" <<'EOF'
[connection]
wifi.powersave = 2
EOF

# Broadcom on Pi can become unstable when P2P/vendor IEs are enabled during AP setup.
# Keep a minimal supplicant config with P2P disabled and explicit regulatory domain.
mkdir -p "$MNT_ROOT/etc/wpa_supplicant"
cat >"$MNT_ROOT/etc/wpa_supplicant/wpa_supplicant.conf" <<EOF
ctrl_interface=DIR=/run/wpa_supplicant GROUP=netdev
update_config=1
country=${WIFI_COUNTRY_CODE}
p2p_disabled=1
EOF

# On DietPi images, wlan0/eth0 are often defined in ifupdown, which can conflict
# with captive portal workflows. Keep only loopback so NM has single ownership.
cat >"$MNT_ROOT/etc/network/interfaces" <<'EOF'
auto lo
iface lo inet loopback
EOF

mkdir -p "$MNT_ROOT/etc/systemd/system/multi-user.target.wants"

if [[ -f "$MNT_ROOT/lib/systemd/system/NetworkManager.service" ]]; then
  ln -sf /lib/systemd/system/NetworkManager.service "$MNT_ROOT/etc/systemd/system/multi-user.target.wants/NetworkManager.service"
elif [[ -f "$MNT_ROOT/usr/lib/systemd/system/NetworkManager.service" ]]; then
  ln -sf /usr/lib/systemd/system/NetworkManager.service "$MNT_ROOT/etc/systemd/system/multi-user.target.wants/NetworkManager.service"
else
  echo "NetworkManager.service was not found in image"
  exit 1
fi

if [[ -f "$MNT_ROOT/lib/systemd/system/comitup.service" ]]; then
  ln -sf /lib/systemd/system/comitup.service "$MNT_ROOT/etc/systemd/system/multi-user.target.wants/comitup.service"
elif [[ -f "$MNT_ROOT/usr/lib/systemd/system/comitup.service" ]]; then
  ln -sf /usr/lib/systemd/system/comitup.service "$MNT_ROOT/etc/systemd/system/multi-user.target.wants/comitup.service"
else
  echo "comitup.service was not found in image"
  exit 1
fi

if [[ -f "$MNT_ROOT/lib/systemd/system/avahi-daemon.service" ]]; then
  ln -sf /lib/systemd/system/avahi-daemon.service "$MNT_ROOT/etc/systemd/system/multi-user.target.wants/avahi-daemon.service"
elif [[ -f "$MNT_ROOT/usr/lib/systemd/system/avahi-daemon.service" ]]; then
  ln -sf /usr/lib/systemd/system/avahi-daemon.service "$MNT_ROOT/etc/systemd/system/multi-user.target.wants/avahi-daemon.service"
fi

if [[ -f "$MNT_ROOT/lib/systemd/system/tailscaled.service" ]]; then
  ln -sf /lib/systemd/system/tailscaled.service "$MNT_ROOT/etc/systemd/system/multi-user.target.wants/tailscaled.service"
elif [[ -f "$MNT_ROOT/usr/lib/systemd/system/tailscaled.service" ]]; then
  ln -sf /usr/lib/systemd/system/tailscaled.service "$MNT_ROOT/etc/systemd/system/multi-user.target.wants/tailscaled.service"
else
  echo "tailscaled.service not found in image — Tailscale may not have installed correctly"
  exit 1
fi

# Backstop the NetworkManager wifi.powersave setting with a boot-time oneshot
# that re-asserts power-save OFF directly on the interface, after NM has brought
# wlan0 up. Backend-independent, so it also covers comitup AP mode and any
# driver default that re-enables power-save before NM applies its config.
cat >"$MNT_ROOT/etc/systemd/system/wifi-powersave-off.service" <<'EOF'
[Unit]
Description=Disable WiFi power saving on wlan0
After=NetworkManager.service sys-subsystem-net-devices-wlan0.device
Wants=sys-subsystem-net-devices-wlan0.device

[Service]
Type=oneshot
ExecStart=/bin/sh -c 'iw dev wlan0 set power_save off'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
ln -sf /etc/systemd/system/wifi-powersave-off.service \
    "$MNT_ROOT/etc/systemd/system/multi-user.target.wants/wifi-powersave-off.service"

# Ensure old wifi-connect units don't interfere with comitup-based provisioning.
rm -f "$MNT_ROOT/etc/systemd/system/multi-user.target.wants/wifi-connect.service"
rm -f "$MNT_ROOT/etc/systemd/system/wifi-connect.service"

# ConnMan can conflict with NM ownership on DietPi-like images.
if [[ -f "$MNT_ROOT/lib/systemd/system/connman.service" || -f "$MNT_ROOT/usr/lib/systemd/system/connman.service" ]]; then
  mkdir -p "$MNT_ROOT/etc/systemd/system"
  ln -sf /dev/null "$MNT_ROOT/etc/systemd/system/connman.service"
fi

echo "Making first boot unattended"
if [[ ! -f "$MNT_BOOT/dietpi.txt" ]]; then
  echo "Missing $MNT_BOOT/dietpi.txt"
  exit 1
fi

# Set install_stage to 2 to bypass DietPi's automated first boot setup completely
mkdir -p "$MNT_BOOT/dietpi"
echo "2" > "$MNT_BOOT/dietpi/.install_stage"

set_kv "$MNT_BOOT/dietpi.txt" "AUTO_SETUP_AUTOMATED" "1"
set_kv "$MNT_BOOT/dietpi.txt" "AUTO_SETUP_ACCEPT_LICENSE" "1"
set_kv "$MNT_BOOT/dietpi.txt" "AUTO_SETUP_NET_WIFI_ENABLED" "1"
set_kv "$MNT_BOOT/dietpi.txt" "AUTO_SETUP_NET_WIFI_COUNTRY_CODE" "$WIFI_COUNTRY_CODE"
set_kv "$MNT_BOOT/dietpi.txt" "AUTO_SETUP_GLOBAL_PASSWORD" "dietpi"
set_kv "$MNT_BOOT/dietpi.txt" "CONFIG_CHECK_DIETPI_UPDATES" "0"
set_kv "$MNT_BOOT/dietpi.txt" "CONFIG_CHECK_APT_UPDATES" "0"

echo "Setting hostname to 'phenohive'..."
echo "phenohive" > "$MNT_ROOT/etc/hostname"
sed -i 's/DietPi/phenohive/g' "$MNT_ROOT/etc/hosts"

echo "Setting keyboard layout to BE..."
cat >"$MNT_ROOT/etc/default/keyboard" <<'EOF'
XKBMODEL="pc105"
XKBLAYOUT="be"
XKBVARIANT=""
XKBOPTIONS=""
BACKSPACE="guess"
EOF

echo "Enabling I2C hardware and modules..."
# Enable I2C in config.txt
if ! grep -q "dtparam=i2c_arm=on" "$MNT_BOOT/config.txt"; then
    echo "dtparam=i2c_arm=on" >> "$MNT_BOOT/config.txt"
fi
if ! grep -q "dtparam=i2c_vc=on" "$MNT_BOOT/config.txt"; then
    echo "dtparam=i2c_vc=on" >> "$MNT_BOOT/config.txt"
fi
if ! grep -q "camera_auto_detect=1" "$MNT_BOOT/config.txt"; then
    echo "camera_auto_detect=1" >> "$MNT_BOOT/config.txt"
fi
if ! grep -q "dtoverlay=vc4-kms-v3d" "$MNT_BOOT/config.txt"; then
    echo "dtoverlay=vc4-kms-v3d,cma-128" >> "$MNT_BOOT/config.txt"
fi
if ! grep -q "dtoverlay=ov5647" "$MNT_BOOT/config.txt"; then
    echo "dtoverlay=ov5647" >> "$MNT_BOOT/config.txt"
fi
# Set GPU memory to 128MB to ensure camera firmware has enough room on 512MB Pi
if grep -q "gpu_mem_512=" "$MNT_BOOT/config.txt"; then
    sed -i "s/gpu_mem_512=.*/gpu_mem_512=128/" "$MNT_BOOT/config.txt"
else
    echo "gpu_mem_512=128" >> "$MNT_BOOT/config.txt"
fi
# Load i2c-dev module at boot
echo "i2c-dev" > "$MNT_ROOT/etc/modules-load.d/i2c-dev.conf"

echo "Unlocking Raspberry Pi Camera and I2C..."
# DietPi often includes configs that explicitly disable hardware. Remove them.
rm -f "$MNT_ROOT/etc/modprobe.d/dietpi-disable_rpi_camera.conf"
rm -f "$MNT_ROOT/etc/modprobe.d/dietpi-disable_rpi_codec.conf"
rm -f "$MNT_ROOT/etc/modprobe.d/dietpi-disable_i2c.conf"

# Keep this disabled so it doesn't try to curl URL '1'
set_kv "$MNT_BOOT/dietpi.txt" "AUTO_SETUP_CUSTOM_SCRIPT_EXEC" "0"

echo "Baking PhenoHive application into the image..."
mkdir -p "$MNT_ROOT/opt/phenohive"
# Copy project source (excluding bulky/temporary files)
rsync -a --exclude '.git' --exclude '.venv' --exclude '__pycache__' --exclude 'data/*' --exclude 'logs/*' --exclude 'dietpi/*' "$SCRIPT_DIR/../" "$MNT_ROOT/opt/phenohive/"

# Bootstrap config.ini from defaults (config.ini is gitignored and may not be in the repo)
if [[ ! -f "$MNT_ROOT/opt/phenohive/config.ini" ]]; then
  cp "$MNT_ROOT/opt/phenohive/config.defaults.ini" "$MNT_ROOT/opt/phenohive/config.ini"
fi

# Bake station_id into config.ini if provided via local file
if [[ -n "$BAKED_STATION_ID" ]]; then
  sed -i "s|^station_id[[:space:]]*=.*|station_id = ${BAKED_STATION_ID}|" \
      "$MNT_ROOT/opt/phenohive/config.ini"
  echo "Baking station_id=${BAKED_STATION_ID} into config.ini"
else
  echo "Warning: no phenohive_station_id.txt found — station_id will use the default from config.defaults.ini"
fi

echo "Installing PhenoHive dependencies inside image (this may take a while)..."
chroot "$MNT_ROOT" "$QEMU_IN_CHROOT" /usr/bin/apt-get install -y --no-install-recommends \
    python3-venv python3-pip build-essential python3-dev libcap-dev pkg-config \
    libcamera-dev libopenblas-dev libjpeg-dev libpng-dev libtiff-dev # Vision/Math dev deps

chroot "$MNT_ROOT" "$QEMU_IN_CHROOT" /bin/bash -c "cd /opt/phenohive && \
    python3 -m venv --system-site-packages .venv && \
    .venv/bin/pip install --upgrade pip && \
    .venv/bin/pip install --no-cache-dir -r requirements.txt"

# Cleanup after baking
chroot "$MNT_ROOT" "$QEMU_IN_CHROOT" /usr/bin/apt-get clean
chroot "$MNT_ROOT" "$QEMU_IN_CHROOT" /bin/rm -rf /root/.cache/pip

echo "Installing systemd services..."
cp "$SCRIPT_DIR/../infrastructure/systemd/phenohive.service" "$MNT_ROOT/etc/systemd/system/"

# Strip Windows CRLF line endings from all shell scripts.
# When the repo is edited on Windows, git may commit scripts with CRLF even if
# .gitattributes requests LF. bash on Linux treats \r as part of the command
# and fails with "No such file or directory" on the first line.
find "$MNT_ROOT/opt/phenohive/scripts" -name "*.sh" -exec sed -i 's/\r$//' {} +

# Make all runtime scripts executable. rsync preserves permissions but on
# Windows hosts git does not store the execute bit, so scripts arrive
# non-executable and must be chmod'd explicitly here.
chmod +x "$MNT_ROOT/opt/phenohive/scripts/"*.sh

# Create a boot-time config update script.
# TAILSCALE_KEY is expanded here by the host shell so it is baked into the image.
cat >"$MNT_ROOT/opt/phenohive/scripts/update_config.sh" <<EOF
#!/bin/bash
# Reads provisioning files from /boot and applies them to .env and config.ini.
# Runs on every boot as ExecStartPre for phenohive.service.
# Files consumed once are deleted after successful application.

IP_CONFIG="/boot/phenohive_server.txt"
TOKEN_CONFIG="/boot/phenohive_token.txt"
STATION_ID_CONFIG="/boot/phenohive_station_id.txt"
TAILSCALE_KEY_CONFIG="/boot/phenohive_tailscale_key.txt"
BAKED_TAILSCALE_KEY="${TAILSCALE_KEY}"   # embedded at image build time; empty if --tailscale-key was not passed
ENV_FILE="/opt/phenohive/.env"
INI_FILE="/opt/phenohive/config.ini"

if [ -f "\$IP_CONFIG" ]; then
    NEW_IP=\$(cat "\$IP_CONFIG" | tr -d '\r\n ')
    if [[ \$NEW_IP =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+\$ ]]; then
        echo "Updating INFLUX_URL to http://\$NEW_IP:8081"
        sed -i "s|INFLUX_URL=.*|INFLUX_URL=http://\$NEW_IP:8081|" "\$ENV_FILE"
    else
        echo "phenohive_server.txt: '\$NEW_IP' is not a valid IPv4 address, skipping"
    fi
fi

if [ -f "\$TOKEN_CONFIG" ]; then
    NEW_TOKEN=\$(cat "\$TOKEN_CONFIG" | tr -d '\r\n ')
    if [ -n "\$NEW_TOKEN" ]; then
        echo "Updating INFLUX_TOKEN"
        sed -i "s|INFLUX_TOKEN=.*|INFLUX_TOKEN=\$NEW_TOKEN|" "\$ENV_FILE"
    fi
fi

if [ -f "\$STATION_ID_CONFIG" ]; then
    NEW_ID=\$(cat "\$STATION_ID_CONFIG" | tr -d '\r\n ')
    if [ -n "\$NEW_ID" ]; then
        echo "Updating station_id to \$NEW_ID"
        sed -i "s|^station_id[[:space:]]*=.*|station_id = \$NEW_ID|" "\$INI_FILE"
        rm -f "\$STATION_ID_CONFIG"
    fi
fi

# Tailscale auth: only needed on first boot (no existing device state).
# On subsequent boots tailscaled reconnects automatically from its saved state;
# calling tailscale up --auth-key on an already-registered device registers a
# new device entry, so we guard against that with the state file check.
TAILSCALE_STATE="/var/lib/tailscale/tailscaled.state"
AUTH_KEY=""
if [ -f "\$TAILSCALE_KEY_CONFIG" ]; then
    # Explicit per-station override key on /boot — consume it regardless of outcome.
    AUTH_KEY=\$(cat "\$TAILSCALE_KEY_CONFIG" | tr -d '\r\n ')
    rm -f "\$TAILSCALE_KEY_CONFIG"
elif [ -n "\$BAKED_TAILSCALE_KEY" ] && [ ! -s "\$TAILSCALE_STATE" ]; then
    # Use the key baked at build time, but only on a genuinely fresh device
    # (state file absent or empty means tailscaled has never authenticated).
    AUTH_KEY="\$BAKED_TAILSCALE_KEY"
fi

if [ -n "\$AUTH_KEY" ]; then
    echo "Authenticating Tailscale..."
    if tailscale up --auth-key="\$AUTH_KEY" --accept-routes; then
        echo "Tailscale authenticated successfully"
    else
        echo "Tailscale auth failed — will retry on next boot"
    fi
fi
EOF
chmod +x "$MNT_ROOT/opt/phenohive/scripts/update_config.sh"

# The service file already includes update_config.sh in ExecStartPre

# Write .env into the image, substituting values read from local config files.
INFLUX_URL_BAKED="http://${BAKED_SERVER_IP:-192.168.1.15}:8081"
INFLUX_TOKEN_BAKED="${BAKED_TOKEN:-phenohive-local-dev-token}"
echo "Baking INFLUX_URL=${INFLUX_URL_BAKED}"
[[ -z "$BAKED_TOKEN" ]] && echo "Warning: no phenohive_token.txt found — token is a placeholder"
cat >"$MNT_ROOT/opt/phenohive/.env" <<EOF
MOCK_MODE=False
TIME_SYNC_ENABLED=True
INFLUXDB_ENABLED=True
INFLUX_URL=${INFLUX_URL_BAKED}
INFLUX_TOKEN=${INFLUX_TOKEN_BAKED}
INFLUX_ORG=uclouvain
INFLUX_BUCKET=phenohive
DEBUG_UI_ENABLED=True
DEBUG_UI_HOST=0.0.0.0
EOF

# Enable the service
ln -sf /etc/systemd/system/phenohive.service "$MNT_ROOT/etc/systemd/system/multi-user.target.wants/phenohive.service"

echo "Golden image ready: $OUTPUT_IMG"
echo "Next: flash this image to SD cards and boot on Pi."
