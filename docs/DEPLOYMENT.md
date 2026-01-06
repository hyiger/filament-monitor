# Deployment & Architecture

[Back to README](../README.md)

### Related documentation
- [Usage](USAGE.md)
- [Marlin / G-code Integration](INTEGRATION.md)
- [Deployment & Architecture](DEPLOYMENT.md)

---

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

## Systemd service

The included `filament-monitor.service` is a template. **Edit `WorkingDirectory` and `ExecStart`** to match where you installed the project and which serial/GPIO arguments you want.

A sample service file is included as `filament-monitor.service`.

Typical install:
```bash
sudo cp filament-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now filament-monitor.service
```

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

## Control socket path (`/run/filmon`)

The provided systemd service uses:

- `RuntimeDirectory=filmon` (systemd creates `/run/filmon` at startup)
- `control_socket=/run/filmon/filmon.sock`

**If you run the monitor manually (not via systemd):** `/run/filmon` may not exist and your user may not have permission to create it. In that case, set `control_socket` to a path under `/tmp` (e.g. `/tmp/filmon.sock`) or create the directory as root.

