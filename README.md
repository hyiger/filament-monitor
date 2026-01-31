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

## Notifications (optional)

Filament Monitor can send push notifications when a filament jam or runout
is detected. Notifications are delivered using the **Pushover** service,
which supports iOS, Android, and Apple Watch.

Notifications are disabled by default and must be explicitly enabled.

### Requirements

- A Pushover account: https://pushover.net
- An application token and user key

### Enable notifications

Set the following environment variables for the filament-monitor service:

- `FILMON_NOTIFY=1`
- `PUSHOVER_TOKEN=<your application token>`
- `PUSHOVER_USER=<your user key>`

The recommended way to configure these is via a systemd override:

```
sudo systemctl edit filament-monitor
```

Add:

```
[Service]
Environment=FILMON_NOTIFY=1
Environment=PUSHOVER_TOKEN=your_app_token_here
Environment=PUSHOVER_USER=your_user_key_here
```

Then restart the service:

```
sudo systemctl daemon-reload
sudo systemctl restart filament-monitor
```

### Test notifications

You can test notifications without inducing a jam or runout using:

```
./filmonctl.py test-notify
```

This sends a one-time test notification using the configured credentials.
