![PhenoHive Logo](docs/Report/Images/logo_phenohive.jpg)

-----

# PhenoHive

Low-cost Raspberry Pi-based phenotyping station for the LBIR1251 plant biology course at UCLouvain.
Third generation — built on [Colinet 2025](https://github.com/locolinet/PhenoHive) and [Goffinet 2024](https://github.com/Oldgram/PhenoHive).

## Table of Contents

- [Project Description](#project-description)
- [System Operation](#system-operation)
  - [Configuration](#configuration)
  - [Station Identity](#station-identity)
  - [Measurement Loop](#measurement-loop)
    - [Burst Sampling and Outlier Filtering](#burst-sampling-and-outlier-filtering)
    - [Data Persistence](#data-persistence)
    - [InfluxDB Sync and Offline Queue](#influxdb-sync-and-offline-queue)
    - [Measurement Format](#measurement-format)
  - [Vision Pipeline](#vision-pipeline)
  - [Debug Web Interface](#debug-web-interface)
  - [Logging and Error Handling](#logging-and-error-handling)
- [Installation](#installation)
  - [Prerequisites](#prerequisites)
  - [OS Setup (DietPi)](#os-setup-dietpi)
  - [Automated Deployment](#automated-deployment)
  - [Manual Setup](#manual-setup)
  - [SSH Connection](#ssh-connection)
- [Development (Mock Mode)](#development-mock-mode)
- [Infrastructure (InfluxDB and Grafana)](#infrastructure-influxdb-and-grafana)

## Project Description

PhenoHive is a low-cost station for continuous plant phenotyping, designed for parallel deployment across ~20 student groups in the LBIR1251 plant biology course. Each station monitors a single potted plant over a month-long experiment in an uncontrolled environment (student homes).

The station runs on a Raspberry Pi with DietPi OS and is equipped with:
- An SHT35 sensor (I2C) to measure air temperature and relative humidity.
- A TCS3448 14-channel spectral sensor (I2C) to measure light spectrum and intensity.
- A Tal226 load cell connected to an HX711 controller (GPIO) to measure plant pot weight.
- A Raspberry Pi Camera and an LED strip controlled via a KY-019 relay module to capture plant images.

The software is written in Python and organised as follows:
- [main.py](main.py) is the entry point: it runs the measurement loop, handles sensor orchestration, and drives the two-tier collection/publish pipeline.
- [src/core/](src/core/) contains the core services: `ConfigManager`, `DataManager`, `SensorFactory`, `TimeSyncService`, and the rotating logger.
- [src/sensors/](src/sensors/) contains one module per sensor, each providing a real (`Real*`) and a mock (`Mock*`) implementation behind the `BaseSensor` interface.
- [src/ui/](src/ui/) contains the `DebugUIService`, a local HTTP server with a live dashboard and a config API.
- [src/vision/](src/vision/) contains `PlantImageProcessor`, which uses PlantCV to compute leaf area from camera images.
- [tests/](tests/) contains the pytest test suite (mock sensors only; no hardware required).
- [scripts/](scripts/) contains deployment helpers, calibration tools, and Grafana provisioning scripts.

All measurements are written to a local CSV file first, then pushed to a shared InfluxDB/Grafana stack hosted on a UCLouvain VM. Students view their data through a per-group Grafana dashboard.

## System Operation

### Configuration

All station parameters are defined in `config.ini` (copy from [config.defaults.ini](config.defaults.ini) before first run). Key sections:

| Section | Purpose |
|---------|---------|
| `[general]` | `station_id`, `mock_mode`, base interval |
| `[sampling]` | Collection and publish intervals, burst sample counts, outlier method |
| `[calibration]` | Scale re-baseline interval and drift alert threshold |
| `[influxdb]` | Remote database URL, token, org, bucket |
| `[debug_ui]` | HTTP dashboard host, port, write token |
| `[led_strip]` | GPIO pin and mock flag for the LED relay |
| `[tcs3448]` | Per-channel scale and offset calibration |

Environment variables override any config key as `SECTION_OPTION` (e.g. `INFLUXDB_TOKEN`). On the Raspberry Pi, `/opt/phenohive/.env` is loaded as an `EnvironmentFile` by the systemd unit and takes precedence over `config.ini`.

### Station Identity

Each station has a `station_id` (human-readable group label, e.g. `"1"`) and a `hardware_uuid` (UUID generated on first boot, written back to `config.ini`). Both are injected as InfluxDB tags into every record and drive the Grafana per-group access-control partitioning.

### Measurement Loop

The runtime uses a two-tier loop:

- **Collection tier** (`collection_interval_s`): reads all sensors, applies burst sampling and MAD filtering, and accumulates samples in memory.
- **Publish tier** (`publish_interval_s`): smooths the collected samples (mean, median, min, max, stddev per field) and writes one aggregate record to CSV and InfluxDB.

#### Burst Sampling and Outlier Filtering

Each collection step reads every sensor `{sensor}_samples` times (default 5). Outliers are rejected using Median Absolute Deviation (MAD) before averaging. Each published record carries quality metadata:

| Field | Description |
|-------|-------------|
| `success_ratio` | Fraction of burst reads that succeeded |
| `low_confidence` | Flag set when `success_ratio` falls below threshold |
| `quality_score` | Composite quality score (0–1) |
| `critical_quality_issue` | Boolean flag for Grafana alerting |

#### Data Persistence

Every aggregate record is appended to a local CSV file (`data/measurements.csv`) **before** any network operation. The CSV is the authoritative archive and survives InfluxDB outages.

#### InfluxDB Sync and Offline Queue

After writing to CSV, the runtime attempts to push the record to InfluxDB. If the push fails, the record is saved to `data/offline_queue.jsonl`. On the next successful write, all queued records are flushed in order.

#### Measurement Format

Each record includes:

| Field group | Fields |
|-------------|--------|
| Temperature/humidity | `temperature`, `humidity`, `vpd` |
| Spectral | `f1`–`f8`, `fz`, `fy`, `fxl`, `nir`, `2x_vis_1`, `fd_1`, `red`, `green`, `blue`, `lux` |
| Weight | `weight_g`, `tare`, `calibration_factor` |
| Vision | `leaf_area_px`, `leaf_area_cm2` |
| Quality | `success_ratio`, `low_confidence`, `quality_score`, `critical_quality_issue` per sensor |
| Identity | `station_id`, `hardware_uuid` (InfluxDB tags) |

### Vision Pipeline

When a camera is available, the station captures a plant image and runs PlantCV-based leaf area analysis ([src/vision/image_processing.py](src/vision/image_processing.py)). A background image is captured once (via the debug UI or on first run) and used to isolate the foreground plant. Leaf area is expressed in pixels and, if the physical scale factor is configured, in cm².

### Debug Web Interface

When `debug_ui.enabled = True` in `config.ini`, a local HTTP server starts on the configured port (default 8080):

| Endpoint | Description |
|----------|-------------|
| `GET /` | Live HTML dashboard |
| `GET /api/status` | Runtime status JSON |
| `GET /api/config/editable` | Editable config fields |
| `POST /api/config` | Update a config option at runtime |
| `POST /api/camera/capture-background` | Trigger background image capture |
| `POST /api/restart` | Restart the service |

Write endpoints require `Authorization: Bearer <write_token>` (if `write_token` is set) and are blocked for non-localhost clients unless `allow_remote_writes = True`.

### Logging and Error Handling

Logs are written to `logs/phenohive.log` (rotating, configurable level). On the Raspberry Pi, `journalctl -u phenohive` provides the same output via systemd. Pass `--log-level DEBUG` to [main.py](main.py) for verbose output.

Sensor errors are non-fatal: a sensor in `ERROR` state is still polled each cycle and can recover autonomously. The runtime only aborts if a critical unrecoverable error is raised.

## Installation

Deploying a station involves building a pre-configured SD card image on a Linux machine, flashing it, and completing Wi-Fi onboarding via a captive portal. No SSH access or manual file editing is required after flashing.

### Prerequisites

- Raspberry Pi Zero 2 W with a microSD card (≥8 GB).
- A Linux build machine with `qemu-user-static`, `binfmt` support, and standard utilities (`losetup`, `parted`, `e2fsck`, `resize2fs`, `rsync`).
- A Tailscale account with a reusable pre-auth key (generated from the [Tailscale admin console](https://login.tailscale.com/admin/settings/keys)).
- SSH access to the UCLouvain VM and an admin InfluxDB token.
- The PhenoHive repository cloned locally.

### 1. Prepare the configuration files

In the [dietpi/](dietpi/) directory, create three plain-text files (one value per file, no trailing whitespace):

| File | Content |
|------|---------|
| `dietpi/phenohive_server.txt` | Tailscale IP of the UCLouvain VM (e.g. `100.117.27.12`) |
| `dietpi/phenohive_token.txt` | InfluxDB write token *(gitignored — do not commit)* |
| `dietpi/phenohive_station_id.txt` | Unique station identifier (e.g. `01`, `02`) |

The build script reads these files and writes the values directly into `/opt/phenohive/.env` and `config.ini` inside the image.

### 2. Build the image

Download the latest DietPi ARMv8 image for Raspberry Pi Zero 2 W from [dietpi.com](https://dietpi.com) and place it in the `dietpi/` directory. Then run from the repository root:

```bash
sudo ./dietpi/build_image.sh --tailscale-key tskey-auth-XXXXX
```

The script performs the following inside a chroot of the base image:
1. Installs system packages: NetworkManager, comitup (captive-portal Wi-Fi onboarding), Tailscale, `python3-picamera2`, I²C tools, and Avahi.
2. Enables `NetworkManager`, `comitup`, `avahi-daemon`, `tailscaled`, and `phenohive` as systemd services.
3. Bakes the PhenoHive source tree into `/opt/phenohive/`, creates a Python virtual environment, and installs all dependencies.
4. Writes `.env` (server IP and token) and `config.ini` (station ID) from the local configuration files.
5. Embeds the Tailscale pre-auth key for automatic first-boot authentication.
6. Enables I²C and the camera module via `config.txt` overlays and configures an unattended DietPi first boot.

The output is `dietpi/phenohive.img`. Build time is 20–40 minutes depending on network speed and host CPU.

### 3. Flash the image

Flash `dietpi/phenohive.img` to the microSD card. On Windows, use [Raspberry Pi Imager](https://www.raspberrypi.com/software/) or [Balena Etcher](https://www.balena.io/etcher/). On Linux:

```bash
dd if=dietpi/phenohive.img of=/dev/sdX bs=4M status=progress
```

Insert the card into the Raspberry Pi and power it on. No further SD card preparation is needed.

### 4. Wi-Fi setup via comitup

No Wi-Fi credentials are stored in the image; the comitup service handles onboarding on first boot:

1. Power on the station. After ~30 seconds a Wi-Fi hotspot named `comitup-XXXX` appears.
2. On a phone or laptop, connect to that hotspot. A captive portal opens automatically (or navigate to `http://comitup.local`).
3. Select the target Wi-Fi network, enter the password, and confirm. The hotspot disappears and the station joins the network.
4. The station reboots automatically. On the second boot it authenticates to Tailscale and the PhenoHive service starts.

If the station later cannot find its saved network, comitup automatically re-broadcasts the hotspot — repeat steps 2–3 to reconfigure.

### 5. Verify the service

Once on the network, confirm the service is running via SSH (the Pi's Tailscale IP is visible in the [Tailscale admin console](https://login.tailscale.com/admin/machines)):

```bash
ssh root@<PI_TAILSCALE_IP>
systemctl status phenohive
journalctl -u phenohive -n 60 --no-pager
```

The log should show sensor initialisation messages (`SHT35 OK`, `TCS3448 OK`, etc.), an NTP sync confirmation, and `Collection cycle started` / `Publishing aggregate record` lines after the first intervals.

If a sensor fails to initialise, the service continues and retries on every poll cycle. If the service cannot reach the server, check:
- `tailscale status` — both the VM and the station should appear as connected.
- `/opt/phenohive/.env` — verify `INFLUX_URL` and `INFLUX_TOKEN` were applied correctly.

### 6. Scale and camera calibration

Open the debug dashboard from any device on the same Tailscale network:

```
http://<PI_TAILSCALE_IP>:8080
```

Navigate to the **Calibration** tab:

**Scale:**
1. Leave the scale empty and click *Confirm: Scale is empty* to set the tare.
2. Place an object of known mass, enter its weight in grams, and click *Calibrate*.
3. Optionally add an offset to account for the pot's weight.

**Camera:**
1. Position the station in its final location without the plant in frame, then click *Capture background*.
2. Measure a reference object in the captured image, enter its real-world size in cm and its pixel size, and click *Calibrate*. The computed scale factor is written to `config.ini` and applied to all subsequent recordings.

### 7. Verify data on Grafana

Within a few minutes of the first data push, the auto-provisioning service on the VM detects the new `station_id` in InfluxDB and automatically creates a Grafana team, a student user account (`studentXX` / `phenohive_XX`), and a per-station dashboard cloned from the master template.

Log in to Grafana at `http://100.117.27.12:3000` as admin and confirm the new dashboard appears.

### Optional: server-side infrastructure setup

If the backend stack is not yet running (e.g. for a new course edition):

1. SSH into the VM and clone the repository.
2. Copy `.env.example` to `.env` and set `INFLUXDB_INIT_ADMIN_TOKEN`, `GRAFANA_ADMIN_PASSWORD`, and `INFLUXDB_INIT_PASSWORD`.
3. Start the Docker stack: `docker compose up -d`
4. Create a scoped InfluxDB write token (append-only to the `phenohive` bucket) via the InfluxDB web UI at port 8086 (accessible locally on the VM). This token goes into `dietpi/phenohive_token.txt` for each station.
5. Start the auto-provisioning service: `python scripts/auto_provision_grafana.py` (register as a systemd unit for persistence).
6. Install Tailscale on the VM and join the PhenoHive tailnet: `tailscale up --auth-key=<TSKEY-AUTH-...>`

### Day-to-day development updates

For pushing code changes to an already-deployed station from your dev machine:

```bash
# One-time SSH key setup
bash scripts/setup_ssh_key.sh

# Push only files changed since last commit (auto-restarts service if .py/.html changed)
bash scripts/push_to_pi.sh

# Push entire project tree
bash scripts/push_to_pi.sh --all
```

## Development (Mock Mode)

All sensors have mock implementations that generate plausible data without any hardware. Enable mock mode in `config.ini`:

```ini
[general]
mock_mode = True
```

Or per sensor:

```ini
[sensors]
sht35 = mock
tcs3448 = mock
scale_hx711 = mock
```

Start the station locally:

```bash
python main.py --config config.ini

# Single measurement cycle (useful for testing)
python main.py --once

# Run the test suite (no hardware required)
pytest tests/
```

To run the full stack (InfluxDB + Grafana + a mock station) with Docker:

```bash
cp .env.example .env
# Edit .env with secrets before first run
docker compose --profile local up -d
```

Grafana is available at `http://localhost:3000` and InfluxDB at `http://localhost:8086`.

## Infrastructure (InfluxDB and Grafana)

There are two separate stacks — do not confuse them:

| Stack | Where | Grafana | InfluxDB |
|-------|-------|---------|----------|
| **VM (production)** | UCLouvain VM | `100.117.27.12:3000` | `100.117.27.12:8086` |
| **Local (dev)** | Your machine | `127.0.0.1:3000` | `127.0.0.1:8086` |

To view real station data, always use the **VM Grafana**. The local stack only receives mock data.

Grafana dashboards and provisioning configs are in [grafana/](grafana/). To provision a new station dashboard, run:

```bash
python scripts/generate_station_dashboard.py --station-id <N>
```

Access control (teams, users, per-station dashboard permissions) is managed through CSV files in [grafana/access-control/](grafana/access-control/) and applied with:

```bash
python scripts/sync_grafana_access.py
```

-----

![UCLouvain Logo](docs/Report/Images/logo_UCLouvain.jpg)
