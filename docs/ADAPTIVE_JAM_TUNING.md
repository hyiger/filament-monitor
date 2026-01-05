# Adaptive Jam Timeout Tuning Guide

This guide explains how to tune adaptive jam detection using real print data.

## Why adaptive timeouts
Fixed jam timeouts fail when legitimate extrusion gaps exceed a hard threshold.
Adaptive timeouts scale with observed filament motion and remain fast for real jams.

## Recommended starting values

```toml
[detection]
jam_timeout_adaptive = true
jam_timeout_min = 6.0
jam_timeout_max = 18.0
jam_timeout_k = 16.0
jam_timeout_pps_floor = 0.3
jam_timeout_ema_halflife = 3.0
```

These values are derived from empirical pulse-gap statistics and provide ~2.4× headroom
over worst-case legitimate gaps.

## How to validate
1. Enable JSON logging.
2. Inspect `jam_timeout_effective_s` during sparse extrusion.
3. Confirm no pauses occur below `jam_timeout_max`.

## When to increase jam_timeout_max
If `dt_since_pulse` regularly approaches the clamp value, increase
`jam_timeout_max` in 2-second increments.

## When to decrease
If true jams are detected too slowly, reduce `jam_timeout_max`
or increase `jam_timeout_k`.

## Rearm behavior
Use grace windows to avoid immediate re-jams after operator intervention:

```toml
arm_grace_pulses = 12
arm_grace_s = 12.0
```