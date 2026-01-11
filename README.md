# Filament Motion & Runout Monitor (Marlin / Prusa firmware)

A Raspberry Pi daemon that monitors **filament motion pulses** and an optional **runout signal**, and issues a pause command when motion is expected but not observed.

> **Beta release**
>
> This is a **1.0.0-beta** release. The control interface (`filmon:*`) and the state model are considered stable,
> but the project is still gathering real-world feedback. Expect conservative defaults and incremental refinements.

This project is intended for **Marlin-based firmware**, including **Prusa firmware variants**.

The project defaults are chosen to work with a [BTT SFS V2.0 Smart Filament Sensor](https://biqu.equipment/products/btt-sfs-v2-0-smart-filament-sensor?gad_source=1&gad_campaignid=19101072959&gbraid=0AAAAAoLyodXwvW3tuE564PCAPh0SMCNJi&gclid=Cj0KCQiAsY3LBhCwARIsAF6O6Xi2HYIULPzggB0b2J_zHbLu9XPRrEJcX2Yf-u7BhSGYuc42Vso_4ocaAvCYEALw_wcB)

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
