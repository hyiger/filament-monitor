# Filament Motion & Runout Monitor (Marlin / Prusa firmware)

A Raspberry Pi daemon that monitors **filament motion pulses** and an optional **runout signal**, and issues a pause command when motion is expected but not observed.

> **Beta release**
>
> This is a **1.0.0-beta** release. The control interface (`filmon:*`) and the state model are considered stable,
> but the project is still gathering real-world feedback. Expect conservative defaults and incremental refinements.

This project is intended for **Marlin-based firmware**, including **Prusa firmware variants**.
### Documentation
- [Usage](docs/USAGE.md)
- [Marlin / G-code Integration](docs/INTEGRATION.md)
- [Deployment & Architecture](docs/DEPLOYMENT.md)

## Design Philosophy (Jam-Resistant Operation)

This monitor is intentionally conservative:

- False positives are considered worse than delayed detection
- Monitoring must be explicitly enabled via slicer or G-code markers
- Motion expectation is derived from *actual commanded extrusion*
- Jam detection requires multiple invariants to be violated simultaneously

The goal is predictable, reviewable behavior rather than aggressive detection.

---


## Code structure

The project is organized as a small package with a thin CLI entrypoint:

- **filament-monitor.py** – command-line entrypoint and configuration loading
- **filmon.monitor** – core monitoring logic and jam/runout detection
- **filmon.state** – explicit state machine and transitions
- **filmon.serialio** – Marlin serial I/O and G-code emission
- **filmon.gpio** – motion sensor, runout input, and optional rearm button handling
- **filmon.doctor** – `run_doctor` diagnostics and interactive checks

This structure improves testability and keeps hardware, protocol, and state logic cleanly separated.

> **Control marker format**
>
> Control markers are parsed as **exact commands** (e.g., `filmon:arm`, `filmon:reset`). Substring/suffixed forms (like `filmon:rearm`) are **not** recognized.


> **Arming vs runout**
>
> - **Jam detection** is only evaluated while monitoring is **enabled _and_ armed**.
> - **Runout detection** is evaluated whenever monitoring is **enabled** (it does **not** require arm), since the runout switch is unambiguous: filament is present or it isn’t.
>
> This means a runout can still pause the print before `filmon:arm` if you have already enabled monitoring.

### End-of-print handling

Always include `filmon:disable` in your slicer **end G-code** to prevent false pauses during cooldown, filament retract/unload, or after the print finishes.

Example:
```gcode
M118 A1 filmon:disable
```

### Control socket path

The recommended default control socket is `/run/filmon/filmon.sock` when running under systemd (the provided unit creates `/run/filmon` via `RuntimeDirectory=filmon`).  
If you run the monitor manually (without systemd), use a socket path under `/tmp`, e.g. `/tmp/filmon.sock`.

