# CLAUDE.md — filament-monitor

Raspberry Pi daemon that monitors filament motion pulses and an optional runout signal for Marlin/Prusa 3D printers, pausing the print (default: M600) when a fault is detected.

---

## Project layout

```
filament-monitor.py   # CLI entry point (thin wrapper around filmon.cli.main)
filmonctl.py          # Local control client (connects to daemon via UNIX socket)
filmon/
  cli.py              # Argument parsing and startup
  monitor.py          # FilamentMonitor — core state machine and detection logic
  state.py            # MonitorState dataclass
  doctor.py           # config_defaults_from(), build_arg_parser(), run_doctor(), run_self_test()
  serialio.py         # SerialThread — background serial reader
  gpio.py             # DigitalInputDevice wrapper (gpiozero / LGPIOFactory)
  logging.py          # JsonLogger
  notify.py           # Notifier (Pushover push notifications, optional)
  constants.py        # VERSION, control marker strings, USAGE_EXAMPLES
  util.py             # now_s() — monotonic time
tests/
config.example.toml
filament-monitor.service  # systemd unit template
```

---

## Running the daemon

```bash
# Normal run
python filament-monitor.py -p /dev/ttyACM0

# With a TOML config (CLI args take precedence over file)
python filament-monitor.py --config config.toml

# Diagnostics (does not start the monitor)
python filament-monitor.py --doctor --config config.toml

# Dry-run / self-test (no pause G-code sent)
python filament-monitor.py --self-test -p /dev/ttyACM0
```

---

## Tests

```bash
# Unit tests only (default; skips integration)
pytest

# All tests including integration
pytest -m ""

# Integration tests only
pytest -m integration

# Single file or test
pytest tests/test_state_machine_invariants.py
pytest tests/test_markers.py::test_control_markers_enable_arm_unarm_disable_reset
```

**pytest.ini** registers the `integration` marker. Integration tests spawn subprocesses or use virtual serial ports; they are excluded from the default run.

### Test helpers (conftest.py / builtins)

| Helper | Purpose |
|--------|---------|
| `load_module()` | Dynamically loads `filament-monitor.py` as a module |
| `DummyGPIO` / `DummyDigitalInputDevice` | Hardware-free GPIO stub with `trigger_activated()` / `trigger_deactivated()` |
| `CapturingLogger` | Records `(event, fields)` tuples for assertions |
| `DummySerial` | Captures bytes written to serial |

**Standard test pattern:**

```python
def test_something(monkeypatch):
    m, mon, logger = _make_monitor(monkeypatch, jam_timeout_s=1.0)
    t = {"now": 1000.0}
    monkeypatch.setattr(m.time, "monotonic", lambda: t["now"], raising=True)

    mon._handle_control_marker("filmon:arm")
    t["now"] += 2.0
    mon._maybe_jam()
    assert mon.state.latched is True
```

Always mock `time.monotonic` via the module reference (`m.time.monotonic`, not `time.monotonic`) so the patched value reaches `now_s()`.

---

## Architecture notes

### Threading model

- **Main loop** — daemon thread started by `mon.start()`; processes the serial queue, calls `_maybe_jam()` and `_maybe_breadcrumbs()` in a tight loop (0.2 s queue timeout)
- **SerialThread** — reads lines from the printer serial port and enqueues them
- **Control socket thread** — accepts UNIX socket connections from `filmonctl`
- **GPIO callbacks** — fired by gpiozero on motion pulses, runout edges, and button presses
- **Notifier** — sends Pushover requests from a short-lived background thread

Serial writes are protected by `_ser_lock`. Everything else communicates through `MonitorState` fields and a `queue.Queue`.

### State machine

Four logical states governed by three `MonitorState` booleans (`enabled`, `armed`, `latched`):

```
DISABLED → (filmon:enable) → ENABLED_UNARMED → (filmon:arm) → ENABLED_ARMED
ENABLED_ARMED → (jam / runout) → LATCHED
LATCHED → (filmon:reset) → DISABLED
LATCHED → (rearm button long-press / socket rearm) → ENABLED_ARMED
any → (filmon:disable) → DISABLED
filmon:reset always wins regardless of current state
```

### Control markers

Parsed from the printer serial stream (`M118 A1 filmon:<verb>`), case-insensitive:

| Marker | Effect |
|--------|--------|
| `filmon:reset` | Clear latch/counters, **disable** monitoring (always wins) |
| `filmon:enable` | Enable monitoring, unarmed |
| `filmon:arm` | Enable + arm; resets `motion_pulses_since_arm` and sets `arm_ts` |
| `filmon:unarm` | Disarm, stay enabled; counters preserved |
| `filmon:disable` | Disable monitoring |

### Jam detection

1. Main loop calls `_maybe_jam()` every cycle.
2. Skipped unless `enabled`, `armed`, and not `latched`.
3. **Grace gate** (optional): suppresses jam latching until *either* `arm_grace_pulses` pulses *or* `arm_grace_s` seconds have elapsed since arm — whichever comes first. The `or` is intentional: lack of pulses *is* the jam condition, so the time gate must be able to release independently.
4. Effective timeout is static (`jam_timeout_s`) or adaptive (`jam_timeout_k / max(pps_ema, pps_floor)`, clamped to `[jam_timeout_min_s, jam_timeout_max_s]`).
5. If `now - last_pulse_ts >= effective_timeout`, calls `_trigger_pause("jam")`.

### Timing convention

All internal timestamps use **`now_s()`** (`time.monotonic()`). Never mix in `time.time()`.

---

## Configuration

### TOML sections and keys

```toml
[serial]
port = "/dev/ttyACM0"
baud = 115200

[gpio]
motion_gpio = 26
runout_enabled = false
runout_gpio = 27
runout_active_high = false        # default false (active-low)
runout_debounce = 0.05            # seconds; optional
rearm_button_gpio = 25            # optional
rearm_button_debounce = 0.25
rearm_button_long_press = 1.5     # seconds; distinguishes long vs short press
rearm_button_active_high = false  # default false (active-low); config-only

[detection]
arm_min_pulses = 12               # legacy/unused
jam_timeout = 8.0
jam_timeout_adaptive = true
jam_timeout_min = 6.0
jam_timeout_max = 18.0
jam_timeout_k = 16.0
jam_timeout_pps_floor = 0.3
jam_timeout_ema_halflife = 3.0
arm_grace_pulses = 12
arm_grace_s = 12.0
pause_gcode = "M600"

[logging]
verbose = false
no_banner = false
json = false
breadcrumb_interval = 2.0
pulse_window = 2.0
stall_thresholds = "3,6"

[control]
socket = "/run/filmon/filmon.sock"
```

CLI arguments override all TOML values. `rearm_button_active_high` has no CLI flag — config-only.

### Environment variables

| Variable | Purpose |
|----------|---------|
| `FILMON_NOTIFY=1` | Enable Pushover push notifications |
| `PUSHOVER_TOKEN` | Pushover application token |
| `PUSHOVER_USER` | Pushover user key |
| `FILMON_SOCKET` | Override default control socket path in filmonctl |
| `PYTHONUNBUFFERED=1` | Recommended when running under systemd |

---

## filmonctl commands

```bash
./filmonctl.py status           # JSON state snapshot
./filmonctl.py rearm            # Clear latch and re-arm
./filmonctl.py reset            # Clear latch and disable
./filmonctl.py arm / unarm / enable / disable
./filmonctl.py test-notify      # Send test Pushover notification
./filmonctl.py --socket /path/to/sock status   # Custom socket path
```

---

## Key log events

| Event | When |
|-------|------|
| `startup` | Daemon started |
| `armed` / `unarmed` / `enabled` / `disabled` / `reset` | State transitions |
| `first_pulse_after_arm` | First motion pulse after arming |
| `hb` | Periodic heartbeat (enabled only); includes pps, pps_ema, jam_timeout_effective_s |
| `stall` | dt_since_pulse crosses a stall threshold while armed |
| `pause_triggered` | Fault detected; pause G-code sent |
| `gcode_sent` | Any G-code transmitted |
| `runout_asserted` / `runout_cleared` | Runout edge while armed |
| `rearmed` | Latch cleared and detection re-armed |

JSON output (`--json`): one line per event, `{"ts": <epoch>, "ts_iso": "...", "event": "...", ...}`.

---

## Dependencies

```
pyserial>=3.5        # Printer serial communication
gpiozero>=2.0        # GPIO abstraction
lgpio>=0.2.2.0       # Native GPIO backend (Pi 5 / Debian Trixie)
requests             # Pushover HTTP notifications
tomllib              # TOML config (stdlib in Python 3.11+; tomli backport for older)
```

Dev: `pytest>=7.0`

---

## Conventions

- Time variables are suffixed `_s` (duration in seconds) or `_ts` (monotonic timestamp).
- Private methods are prefixed `_`; GPIO callbacks are `_on_<input>_<event>`.
- All timing uses `now_s()` (monotonic). Never call `time.time()` inside the monitor.
- Grace period gate uses `or`: the gate releases when *either* criterion is satisfied, not both.
- `_trigger_pause` is idempotent — it checks `latched` before doing anything.
- No new `time.time()` calls inside `FilamentMonitor`; use `now_s()`.
