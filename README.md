# Filament Monitor (Beta)

A serial- and GPIO-based filament jam and runout monitor for Marlin-based printers, including Prusa firmware variants.

---

<details>
<summary><strong>Table of contents</strong></summary>

- [Overview](#overview)
- [Supported firmware](#supported-firmware)
- [Installation](#installation)
- [PrusaSlicer configuration](#prusaslicer-configuration)
  - [Start G-code](#start-g-code-required)
  - [Layer change G-code](#layer-change-g-code-important)
  - [End G-code](#end-g-code-required)
- [Layer-change gating timeline](#layer-change-gating-timeline-conceptual)
- [Known limitations](#known-limitations)
- [Unit tests](#unit-tests)
- [License](#license)

</details>

---

## Overview

Filament Monitor observes printer serial output and optional GPIO sensors to detect filament jams and runout conditions.
When a jam is detected, the monitor issues a configurable pause command (e.g. `M600`).

This document describes **recommended usage patterns** that align with the monitor’s internal state machine.

---

## Supported firmware

- Marlin
- Prusa firmware (PrusaSlicer / PrusaLink / PrusaConnect compatible)

Other firmware may work but is not officially supported.

---

## Installation

See the repository root for installation instructions and requirements.

---

## PrusaSlicer configuration

### Start G-code (required)

State-changing control markers **must be issued exactly once** at the start of a print.

Place these **after purge preparation and immediately before the first real extrusion**:

```gcode
M118 A1 filmon:reset
M118 A1 filmon:enable
```

This ensures:
- the monitor starts from a clean state
- arming begins only once extrusion actually starts

---

### Layer change G-code (important)

**Do NOT place state-changing control markers in Before or After layer change G-code.**

PrusaSlicer layer-change hooks run **once per layer**. Repeating any of the following during a print will reset or de-arm the monitor mid-print:

- `filmon:enable`
- `filmon:disable`
- `filmon:reset`

Layer-change hooks may only be used for **optional, non-stateful breadcrumbs**, for example:

```gcode
M118 A1 filmon:layer
```

Breadcrumb markers are informational only and do not affect monitoring state.

---

### End G-code (required)

Disable monitoring exactly once at the end of the print:

```gcode
M118 A1 filmon:disable
```

This should occur before heaters and motors are shut down.

---

## Layer-change gating timeline (conceptual)

**Important:**  
This timeline describes the **internal state progression of the monitor**, not where control markers should be placed in slicer templates.

PrusaSlicer layer-change G-code runs once per layer and must **not** be used to emit state-changing markers.

### Internal phases

1. **Startup**
   - Monitor is disabled or idle
2. **Reset**
   - `filmon:reset` clears any previous latch state
3. **Enabled**
   - `filmon:enable` allows arming to begin
4. **Arming**
   - Monitor waits for sufficient filament motion pulses
5. **Monitoring**
   - Jam detection becomes active
6. **Shutdown**
   - `filmon:disable` cleanly ends monitoring

This model explains *why* arming exists and *when* jam detection is active.
It does **not** imply that slicer layer hooks should be used to control state.

---

## Known limitations

- Layer-change G-code executes once per layer; state markers there will repeat.
- Repeated `enable` or `reset` commands will de-arm the monitor.
- GPIO-based motion sensing depends on correct electrical wiring and debounce tuning.

---

## Unit tests

Unit tests validate argument parsing, state transitions, and guardrails.

Integration tests using virtual serial ports are available but are run separately.

---

## License

See LICENSE file.
