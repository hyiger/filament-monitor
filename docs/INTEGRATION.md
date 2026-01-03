# Marlin / G-code Integration

[Back to README](../README.md)

### Related documentation
- [Usage](USAGE.md)
- [Marlin / G-code Integration](INTEGRATION.md)
- [Deployment & Architecture](DEPLOYMENT.md)

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


## Rearm Button Semantics (Optional)

If configured, the physical button provides a local recovery path after a latched fault:
- Short press performs a **reset** (`filmon:reset` semantics).
- Long press performs a **rearm** (clears latch and arms detection).

This does not replace the marker-driven enable/arm workflow; it simply provides a local way to clear faults.
