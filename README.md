# Filament Motion & Runout Monitor (Marlin / Prusa firmware)

A Raspberry Pi daemon that monitors **filament motion pulses** and an optional **runout signal**, and issues a pause command when motion is expected but not observed.

> **Beta release**
>
> This is a **1.0.0-beta** release. The control interface (`filmon:*`) and the state model are considered stable,
> but the project is still gathering real-world feedback. Expect conservative defaults and incremental refinements.

This project is intended for **Marlin-based firmware**, including **Prusa firmware variants**.

## Table of Contents

<details>
<summary>Click to expand</summary>

- [Status](#status)
- [Quick start](#quick-start)
- [PrusaSlicer configuration](#prusaslicer-configuration)
  - [Start G-code](#start-g-code)
  - [Before and after layer change](#before-and-after-layer-change)
  - [End G-code](#end-g-code)
- [How it works](#how-it-works)
  - [State model](#state-model)
  - [Layer-change gating timeline](#layer-change-gating-timeline)
- [Command-line arguments](#command-line-arguments)
  - [Serial connection](#serial-connection)
  - [GPIO inputs](#gpio-inputs)
  - [Jam detection tuning](#jam-detection-tuning)
  - [Diagnostics and safety](#diagnostics-and-safety)
- [Installation](#installation)
  - [1) OS packages](#1-os-packages)
  - [2) Python dependencies](#2-python-dependencies)
- [GPIO backend selection](#gpio-backend-selection)
  - [Legacy backend: `python3-rpi.gpio`](#legacy-backend-python3-rpigpio)
  - [Modern backend (recommended on Debian Trixie and newer): `python3-rpi-lgpio`](#modern-backend-recommended-on-debian-trixie-and-newer-python3-rpi-lgpio)
- [Wiring](#wiring)
  - [Generic wiring](#generic-wiring)
  - [Example: BTT SFS v2 0](#example-btt-sfs-v2-0)
- [Usage](#usage)
- [Systemd service](#systemd-service)
- [Logging](#logging)
- [Known limitations](#known-limitations)
- [Troubleshooting](#troubleshooting)
- [Design Philosophy (Jam-Resistant Operation)](#design-philosophy-jam-resistant-operation)
- [Safety Invariants](#safety-invariants)
- [Pause Latch Behavior](#pause-latch-behavior)

</details>
## Status
Release status: **1.0.0-beta**

Control markers (sent via `M118 A1`):
- `filmon:enable`
- `filmon:disable`
- `filmon:reset`
 - `filmon:arm`
 - `filmon:unarm`

## Quick start
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt

# Optional: safe self-test (does not send M600)
python filament-monitor.py --self-test -p /dev/ttyACM0

# Run for real
python filament-monitor.py -p /dev/ttyACM0
```

## PrusaSlicer configuration



### Marker-only arming (recommended)

Jam/runout detection is **marker-driven** to prevent false positives ("jam storms").
The monitor will **never** declare a jam or runout unless you explicitly arm it.

Use these markers (sent via `M118 A1 ...`):

- `filmon:reset`  — clears latch/counters and disables monitoring
- `filmon:enable` — enables monitoring (unarmed; safe during travel/heatup)
- `filmon:arm`    — enables monitoring and arms jam/runout detection
- `filmon:unarm`  — keeps enabled but disarms detection
- `filmon:disable`— disables monitoring

Recommended pattern (production-safe):
- In **Start G-code**: `reset`, then `enable` (**do not arm on layer 1**)
- In **Before layer change G-code**: arm at the start of **layer 2**
- In **End G-code**: `disable`

Why not arm on layer 1?
- First-layer behavior often includes very low flow, short segments, and pauses.
- With pulse-based sensors (e.g. BTT SFS v2.0 at ~2.88 mm/pulse), legitimate extrusion can occur with long gaps between pulses.
  Arming during ultra-low flow can therefore cause premature pauses.

Use PrusaSlicer Custom G-code hooks to explicitly control when monitoring is active.

For an overview of the monitor’s internal states and why layer-change gating works, see **How it works → State model** below.

### Start G-code
Add these lines in Start G-code (before printing begins):

```gcode
M118 A1 filmon:reset
M118 A1 filmon:enable
```

Recommended (defensive): add a disable near the top of Start G-code (helps if the monitor survives between prints):

```gcode
M118 A1 filmon:disable
```

### Before layer change (arming policy)

Arm exactly once at the start of **layer 2** (PrusaSlicer `layer_num` is zero-based: 0=layer 1, 1=layer 2):

```gcode
;BEFORE_LAYER_CHANGE
{if layer_num==1}M118 A1 filmon:arm{endif}
```

This pattern is robust for normal printing flows, including frequent retractions and travel moves.

Optional: if you have known ultra-low-flow features where pulses may be very sparse, bracket them with `filmon:unarm` / `filmon:arm`.

### End G-code
Disable monitoring early in End G-code:

```gcode
M118 A1 filmon:disable
```

## How it works

### State model
The filament monitor operates as a small, explicit state machine. Understanding these states makes it easier to reason about false positives, layer-change behavior, and recovery after a pause.

```mermaid
stateDiagram-v2
  [*] --> DISABLED: startup

  DISABLED --> ENABLED_UNARMED: filmon enable
  ENABLED_UNARMED --> DISABLED: filmon disable
  ENABLED_ARMED --> DISABLED: filmon disable
  LATCHED --> DISABLED: filmon disable

  ENABLED_UNARMED --> ENABLED_ARMED: filmon arm

  ENABLED_ARMED --> LATCHED: jam timeout
  ENABLED_ARMED --> LATCHED: runout asserted

  LATCHED --> ENABLED_UNARMED: filmon reset
```

**State legend**
- **DISABLED** — monitoring is off; motion and runout checks are ignored.
- **ENABLED_UNARMED** — enabled but unarmed; safe during travel, heatup, and ultra-low-flow segments.
- **ENABLED_ARMED** — enabled and armed; jam/runout conditions can trigger a pause.
- **LATCHED** — a pause has been triggered; no further actions occur until reset.

Control marker mapping:
- *filmon enable* → `filmon:enable`
- *filmon disable* → `filmon:disable`
- *filmon reset* → `filmon:reset`

### Layer-change gating timeline
Recommended arming timeline:

```
Start G-code:    filmon:reset, filmon:enable   (unarmed)
Layer 2 start:   filmon:arm
End G-code:      filmon:disable
```


## Command-line arguments

- Running with **no arguments** prints the built-in help and usage examples.
- `-h/--help` includes a short **Usage examples** section.

These options control how the monitor connects to the printer, interprets filament motion, and decides when to pause.
The table below is synced to the script’s `argparse` help strings.

### Serial connection

| Argument | Purpose | Default |
|---------|---------|---------|
| `-p, --port` | Serial device for the printer connection (e.g., /dev/ttyACM0). | `` |
| `--baud` | Serial baud rate for the printer connection (default: 115200). | `115200` |

### GPIO inputs

| Argument | Purpose | Default |
|---------|---------|---------|
| `--motion-gpio` | BCM GPIO pin number for the filament motion pulse input. | `26` |
| `--runout-enabled` | Enable runout monitoring (default: disabled). | `False` |
| `--runout-gpio` | BCM GPIO pin number for the optional runout input. | `27` |
| `--runout-debounce` | Debounce time (seconds) applied to the runout input to ignore short glitches. | `` |
| `--runout-active-high` | Treat the runout signal as active-high (default is active-low). | `False` |

### Jam detection tuning

| Argument | Purpose | Default |
|---------|---------|---------|
| `--arm-min-pulses` | (Legacy/unused) Jam detection is marker-driven via `filmon:arm`. | `12` |
| `--jam-timeout` | Seconds without motion pulses (after arming) before declaring a jam (default: 8.0). | `8.0` |
| `--pause-gcode` | G-code to send when a jam/runout is detected (default: M600). | `M600` |

### Diagnostics and safety

| Argument | Purpose | Default |
|---------|---------|---------|
| `--doctor` | Run host/printer diagnostics (GPIO + serial checks) and exit. | `False` |
| `--self-test` | Dry-run mode: monitor inputs and parsing but do not send pause commands. | `False` |
| `--verbose` | Verbose logging (includes serial chatter). | `False` |
| `--json` | Emit structured JSON log events (one per line). | `False` |
| `--no-json` | Disable JSON log output. | `False` |
| `--no-banner` | Disable the startup banner. | `False` |
| `--version` | Print version and exit. | `False` |
| `--config` | Path to a TOML config file (CLI overrides config). | `` |
| `--print-config` | Print the resolved configuration and exit. | `False` |

Example (motion + optional runout):
```bash
python filament-monitor.py -p /dev/ttyACM0 \
  --motion-gpio 26 \
  --runout-enabled \
  --runout-gpio 27
```

## Installation


> **Note:** `pyserial` is required to connect to the printer over USB/serial.

All installation and configuration instructions are maintained in this README.


### 1) OS packages
```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip python3-gpiozero
```

### 2) Python dependencies
From the project directory:
```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## GPIO backend selection

This project uses the `RPi.GPIO` API via `gpiozero`. On modern Linux distributions, choose **one** compatible backend:

### Legacy backend: `python3-rpi.gpio`
- Uses the legacy sysfs GPIO interface
- Works on older Raspberry Pi OS / Debian releases

```bash
sudo apt install -y python3-rpi.gpio python3-gpiozero
```

### Modern backend (recommended on Debian Trixie and newer): `python3-rpi-lgpio`
- Uses the modern `gpiochip` interface
- Drop-in compatible with `RPi.GPIO`
- Recommended for Debian Trixie and newer

```bash
sudo apt remove -y python3-rpi.gpio
sudo apt install -y python3-rpi-lgpio python3-gpiozero
```

> Note: `python3-rpi.gpio` and `python3-rpi-lgpio` cannot be installed at the same time. Choose exactly one backend.

## Wiring

### Generic wiring
You need:
- one **motion pulse** signal (digital)
- optional **runout** signal (digital)
- **GND**
- sensor logic compatible with **3.3V GPIO**

### Example: BTT SFS v2 0
Common reference wiring (BCM numbering):

```
BTT SFS v2.0            Raspberry Pi (BCM)
------------------------------------------
GND        ---------->  GND
SIG (PULSE)---------->  GPIO 26   (motion)
SW (RUNOUT)---------->  GPIO 27   (optional runout)
VCC        ---------->  3.3V
```

Notes:
- Use **BCM** numbers (e.g., 26/27), not physical pin numbers
- Do **not** use 5V logic on GPIO pins
- The runout input is optional

## Usage

Typical command line:
```bash
python filament-monitor.py -p /dev/ttyACM0 --motion-gpio 26 --runout-gpio 27 --runout-active-high
```

For help:
```bash
python filament-monitor.py -h
```


## Configuration (TOML)

Use a TOML file to keep systemd `ExecStart` short.

**Precedence:** CLI arguments override TOML values, which override built-in defaults.

Start from `config.example.toml`:

```bash
cp config.example.toml /etc/filmon/config.toml
```

Run:

```bash
python filament-monitor.py --config /etc/filmon/config.toml
```

Show the resolved configuration:

```bash
python filament-monitor.py --config /etc/filmon/config.toml --print-config
```

## Systemd service

The included `filament-monitor.service` is a template. **Edit `WorkingDirectory` and `ExecStart`** to match where you installed the project and which serial/GPIO arguments you want.

A sample service file is included as `filament-monitor.service`.

Typical install:
```bash
sudo cp filament-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now filament-monitor.service
```

## Logging
The monitor can emit either:

- **Human-readable** logs (default) with millisecond timestamps
- **JSON Lines** (one JSON object per line) with `--json` (recommended for analysis)

Breadcrumbs:
- `--verbose` enables **heartbeat** (`hb`) and **stall** breadcrumbs to help tune timeouts.
- Default heartbeat interval: **2s**
- Default stall thresholds: **3s, 6s** (seconds since last pulse while armed)

When run under systemd, view logs with:

```bash
journalctl -u filament-monitor.service -f
```


## Diagnostic Breadcrumb Logging

The monitor supports **breadcrumb logging** to provide low-rate, aggregated insight into filament motion without logging every sensor pulse. Breadcrumbs are intended for **tuning, validation, and post‑mortem analysis**, not for normal production logging.

Breadcrumbs are **disabled by default** and are automatically enabled when running with `--verbose`.

### Emitted breadcrumb events

**`hb` — Heartbeat**  
Emitted periodically while the monitor is enabled (default: every 2 seconds).

Includes:
- `dt_since_pulse` — seconds since the last motion pulse
- `pps` — pulses per second over a sliding window
- `pulses_reset` — pulses since last reset
- `enabled`, `armed`, `latched` — current state

Used to establish normal pulse cadence and measure worst‑case legitimate pulse gaps.

**`stall` — Stall breadcrumb**  
Emitted when `dt_since_pulse` exceeds configured thresholds **while armed**.

Defaults: `3s`, `6s`

Used to observe pulse starvation leading up to a jam and to verify that retract/travel patterns do **not** cause false triggers.

**`first_pulse_after_arm`**  
Emitted once after arming, when the first motion pulse is observed. Confirms correct arming placement and sensor engagement.

**`pause_triggered`**  
When a jam or runout is detected, the pause event includes breadcrumb evidence such as `dt_since_pulse`, `pps`, and pulse counters.

### Breadcrumb-related command-line options

```
--verbose
    Enable diagnostic logging, including breadcrumb events (hb, stall, etc).

--breadcrumb-interval SECONDS
    Heartbeat interval while enabled (default: 2.0).
    Set to 0 to disable heartbeat breadcrumbs.

--stall-thresholds SECONDS[,SECONDS...]
    Comma-separated list of stall thresholds in seconds.
    Default: 3,6

--pulse-window SECONDS
    Sliding window used to compute pulses-per-second (pps).
    Default: 2.0
```

### Log formats

Breadcrumbs are available in both log formats:

- **Human-readable logs**
  - Millisecond timestamps
  - Suitable for live inspection (`tail -f`)
- **JSON logs (`--json`)**
  - Recommended for analysis and tooling
  - Stable field names
  - One JSON object per line (JSONL)

Breadcrumbs indicate **observed conditions**, not errors. A stall breadcrumb does not imply a jam; it simply records a period without detected filament motion.

## Known limitations
- **Sensor resolution at ultra-low flow.** Pulse-based sensors (e.g. BTT SFS v2.0 at ~2.88 mm/pulse) can have long legitimate gaps
  between pulses when extrusion is extremely slow or highly segmented. In these regimes, time-based “no pulses for N seconds” detection
  can false-trigger. Use a slicer **arming policy** (e.g. arm at layer 2) and avoid arming during known ultra-low-flow segments.
- **Pulse-based motion sensors only.** The monitor expects a digital pulse stream correlated with filament motion.
  Sensors that only provide a static “present/not present” signal cannot detect jams.
- **Non-extruding moves while armed.** If you arm during long non-extruding periods (heatup, long waits), the lack of pulses may be interpreted as a jam.
  Keep the monitor enabled but **unarmed** during those times (`filmon:enable` without `filmon:arm`).
- **GPIO backend selection is platform-dependent.** On newer Debian releases (e.g., Trixie), `python3-rpi-lgpio`
  is typically the correct backend; on older systems, `python3-rpi.gpio` may work better. Only one backend can be installed.
- **Not a substitute for firmware safety features.** This tool augments firmware behavior but cannot detect all failure modes
  (e.g., partial clogs that still generate some pulses, or mechanical slip without pulse loss depending on sensor placement).


## Troubleshooting
- If you get no pulse events, confirm wiring, BCM numbering, and that your GPIO backend is installed.
- If you see premature pauses at very low flow, do not arm on layer 1; arm at layer 2 and/or unarm during ultra-low-flow features.
- If serial control markers are not observed, confirm the printer echoes `M118` messages to the console.

#### Integration tests (virtual serial)

Integration tests are marked with `@pytest.mark.integration` and are **not** run by default.

Run them locally:

```bash
DD_TRACE_ENABLED=false pytest -q -m integration
```

On GitHub Actions, integration tests run via the manual workflow (**Actions → integration → Run workflow**).

### Running unit tests

> **Note on `ddtrace`:** On some systems, automatic `ddtrace` instrumentation can interfere with `pytest` (tests may hang or fail to import). If you encounter issues, disable it explicitly when running tests:

```bash
DD_TRACE_ENABLED=false pytest
```

This does not affect normal operation of `filament-monitor`.

---

## Design Philosophy (Jam-Resistant Operation)

This monitor is intentionally conservative:

- False positives are considered worse than delayed detection
- Monitoring must be explicitly enabled via slicer or G-code markers
- Motion expectation is derived from *actual commanded extrusion*
- Jam detection requires multiple invariants to be violated simultaneously

The goal is predictable, reviewable behavior rather than aggressive detection.

---

## Safety Invariants

The following invariants are enforced by both runtime logic and unit tests:

- Jam timers do **not** start until valid filament motion has been observed
- `filmon:disable` or `filmon:reset` immediately cancels all active timers
- Duplicate enable/disable markers are idempotent
- GPIO callbacks are ignored once shutdown begins
- A triggered pause blocks further detection until reset

If any invariant cannot be satisfied, the monitor fails safe.

---

## Pause Latch Behavior

When a jam or runout is detected:

1. A single pause command (default: `M600`) is issued
2. The monitor enters a **latched** state
3. No additional pause commands are sent
4. Jam detection remains disabled until `filmon:reset`

This latch is explicitly tested to prevent repeated pause commands
(“jam storms”) during recovery or user intervention.

## Arming Policy (Production)

Jam detection must only be active during extrusion regimes where filament motion is resolvable by the sensor.

**Validated policy (recommended):**
- **Reset + enable** at print start
- **Arm at the start of Layer 2**
- **Remain armed** for the remainder of the print
- **Disable** at print end

This policy avoids ultra-low-flow conditions on the first layer and has been validated under:
- continuous extrusion
- slow perimeters
- heavy retraction and travel (island printing)

### PrusaSlicer implementation

**Printer Settings → Start G-code**
```gcode
M118 A1 filmon:reset
M118 A1 filmon:enable
```

**Print Settings → Before layer change G-code**
```gcode
{if layer_num==1}M118 A1 filmon:arm{endif}
```

**Printer Settings → End G-code**
```gcode
M118 A1 filmon:disable
```

Do **not** arm during the first layer or during known ultra-low-flow features.

## Re-arming After a Pause (No Console Required)

When the monitor triggers a pause, it **latches** to prevent repeated `M600` commands. After you clear the jam/runout and are about to resume the print, you must **clear the latch and re-arm** to detect subsequent faults.

The daemon owns the printer serial port, so a second console typically cannot connect. Use the **local control socket** instead.

### filmonctl

The project includes `filmonctl.py`, a local UNIX-socket client. Default socket path: `/run/filmon/filmon.sock`.

Re-arm detection after clearing a jam:

```bash
python filmonctl.py rearm
```

Inspect state:

```bash
python filmonctl.py status
```

Disable the control socket (if desired):

```bash
python filament-monitor.py ... --no-control-socket
```

The socket path can be overridden with `--control-socket` (daemon) and `--socket` or `FILMON_SOCKET` (client).

## Sensor Resolution and Limitations

Motion-based jam detection is constrained by sensor resolution.

For the BTT SFS v2.0:
- **Calibration:** ~2.88 mm of filament per pulse

At very low volumetric flow rates, legitimate extrusion may advance **less than one pulse** over several seconds. In these regimes, pulse-absence alone cannot distinguish normal extrusion from a jam.

**Implications:**
- Jam detection must be **disabled or unarmed** during ultra-low-flow extrusion
- Increasing `jam_timeout_s` indefinitely is not a viable solution
- Marker-based arming is the intended mitigation

Breadcrumb logging exists to measure these limits empirically.

## Default Parameters

The following defaults are chosen to balance detection latency and false-positive resistance:

- `jam_timeout_s`: **8.0**
- `stall_thresholds`: **3,6**
- `breadcrumb_interval`: **2.0**
- `pulse_window`: **2.0**

These values are validated for typical PLA printing with a 0.4 mm nozzle and normal perimeter/infill speeds. Adjust only after reviewing breadcrumb data.

If the daemon owns the printer serial port, you can re-arm after clearing a jam using a physical button wired to a Raspberry Pi GPIO.

- `--rearm-button-gpio BCM_PIN` (example: `25`)
- `--rearm-button-active-high` (default) or `--rearm-button-active-low`
- `--rearm-button-debounce SECONDS` (default: `0.25`)

The button triggers the same action as `filmonctl rearm`: clears the latch, resets counters, and arms detection.



### Physical rearm button (active-low)
- Wire the button between **GPIO 25 and GND**
- The input uses an internal pull-up
- Pressing the button pulls the pin low and triggers a rearm
- Debounce is handled in software

Configuration:
```toml
[gpio]
rearm_button_gpio = 25
rearm_button_active_high = false
rearm_button_debounce = 0.25
```
