#!/usr/bin/env python3

"""
Generate a PDF report explaining pps_ema and the adaptive jam timeout.

Includes:
- Variable definitions table (with code variable names)
- Mathematical derivations appendix
- Graphs:
  - If --jsonl is provided: plots derived from your monitor JSONL (hb events)
  - Otherwise: plots from a synthetic example signal

Requirements:
  pip install reportlab matplotlib numpy

Usage:
  python docs/analysis/make_adaptive_jam_report.py
  python docs/analysis/make_adaptive_jam_report.py --jsonl monitor.jsonl
"""

import argparse
import json
import math
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import matplotlib.pyplot as plt

from reportlab.platypus import (
    SimpleDocTemplate,
    Paragraph,
    Spacer,
    Image,
    PageBreak,
    Table,
    TableStyle,
)
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib import colors


def _load_hb_series(jsonl_path: Path) -> Dict[str, np.ndarray]:
    """
    Parse monitor JSONL and extract hb series:
      - t (seconds since first hb)
      - pps
      - pps_ema (if present)
      - jam_timeout_effective_s (if present)
      - dt_since_pulse (if present)
    """
    times: List[float] = []
    pps: List[float] = []
    pps_ema: List[float] = []
    jam_eff: List[float] = []
    dt_since: List[float] = []

    t0: Optional[float] = None

    with jsonl_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                ev = json.loads(line)
            except Exception:
                continue
            if ev.get("event") != "hb":
                continue
            ts = ev.get("ts")
            if ts is None:
                continue
            if t0 is None:
                t0 = float(ts)
            times.append(float(ts) - float(t0))
            pps.append(float(ev.get("pps", 0.0)))
            pps_ema.append(float(ev.get("pps_ema", float("nan"))))
            jam_eff.append(float(ev.get("jam_timeout_effective_s", float("nan"))))
            dt_since.append(float(ev.get("dt_since_pulse", float("nan"))))

    if not times:
        raise SystemExit(f"No hb events found in {jsonl_path}")

    arr = {
        "t": np.asarray(times, dtype=float),
        "pps": np.asarray(pps, dtype=float),
        "pps_ema": np.asarray(pps_ema, dtype=float),
        "jam_eff": np.asarray(jam_eff, dtype=float),
        "dt_since_pulse": np.asarray(dt_since, dtype=float),
    }
    return arr


def _synthetic_series() -> Dict[str, np.ndarray]:
    """
    Create a synthetic pps signal and compute pps_ema and jam timeout.
    """
    t = np.linspace(0, 30, 300)
    pps = np.piecewise(
        t,
        [t < 10, (t >= 10) & (t < 20), t >= 20],
        [2.5, 0.8, 0.0],
    )
    half_life = 3.0
    tau = half_life / math.log(2.0)
    dt = t[1] - t[0]

    pps_ema = np.zeros_like(pps)
    for i in range(1, len(t)):
        alpha = 1.0 - math.exp(-dt / tau)
        pps_ema[i] = (1.0 - alpha) * pps_ema[i - 1] + alpha * pps[i]

    Tmin, Tmax, K, pps_floor = 6.0, 18.0, 16.0, 0.3
    jam_eff = np.clip(K / np.maximum(pps_ema, pps_floor), Tmin, Tmax)

    return {"t": t, "pps": pps, "pps_ema": pps_ema, "jam_eff": jam_eff, "dt_since_pulse": np.full_like(t, np.nan)}


def _plot_series(series: Dict[str, np.ndarray], out_dir: Path, title_prefix: str) -> Tuple[Path, Path, Optional[Path]]:
    """
    Generate plots:
      1) pps vs pps_ema
      2) effective jam timeout (if present; otherwise derived)
      3) dt_since_pulse (optional, if present)
    """
    out_dir.mkdir(parents=True, exist_ok=True)

    t = series["t"]
    pps = series["pps"]
    pps_ema = series["pps_ema"]
    jam_eff = series["jam_eff"]
    dt_since = series["dt_since_pulse"]

    # Plot 1
    plt.figure()
    plt.plot(t, pps, label="pps (instantaneous)")
    if np.isfinite(pps_ema).any():
        plt.plot(t, pps_ema, label="pps_ema (EMA)")
    plt.xlabel("Time (s)")
    plt.ylabel("Pulses per second")
    plt.title(f"{title_prefix}: pps vs pps_ema")
    plt.legend()
    plt.tight_layout()
    p1 = out_dir / "pps_ema_plot.png"
    plt.savefig(p1, dpi=160)
    plt.close()

    # Plot 2
    plt.figure()
    if np.isfinite(jam_eff).any():
        plt.plot(t, jam_eff)
        plt.ylabel("Effective jam timeout (s)")
    else:
        plt.plot(t, np.full_like(t, np.nan))
        plt.ylabel("Effective jam timeout (s)")
    plt.xlabel("Time (s)")
    plt.title(f"{title_prefix}: effective jam timeout")
    plt.tight_layout()
    p2 = out_dir / "jam_timeout_plot.png"
    plt.savefig(p2, dpi=160)
    plt.close()

    # Plot 3 (optional)
    p3: Optional[Path] = None
    if np.isfinite(dt_since).any():
        plt.figure()
        plt.plot(t, dt_since)
        plt.xlabel("Time (s)")
        plt.ylabel("dt_since_pulse (s)")
        plt.title(f"{title_prefix}: dt_since_pulse")
        plt.tight_layout()
        p3 = out_dir / "dt_since_pulse_plot.png"
        plt.savefig(p3, dpi=160)
        plt.close()

    return p1, p2, p3


def _build_pdf(
    out_pdf: Path,
    pps_plot: Path,
    timeout_plot: Path,
    dt_plot: Optional[Path],
    data_source_note: str,
) -> None:
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(out_pdf), pagesize=letter)

    story: List[Any] = []
    story.append(Paragraph("<b>Adaptive Jam Timeout and pps_ema</b>", styles["Title"]))
    story.append(Spacer(1, 0.25 * inch))
    story.append(Paragraph(data_source_note, styles["BodyText"]))
    story.append(Spacer(1, 0.20 * inch))

    # Variable definition table with code variable names
    story.append(Paragraph("<b>Variable definitions</b>", styles["Heading2"]))
    table_data = [
        ["Symbol", "Code name", "Units", "Meaning"],
        ["N", "len(self._pulse_times)", "pulses", "Pulse count in the sliding window"],
        ["T", "self._pulse_window_s", "s", "Sliding pulse window duration"],
        ["pps", "self._pps(now)", "pulses/s", "Instantaneous pulse rate N / T"],
        ["ppsₑₘₐ", "self._pps_ema", "pulses/s", "Exponential moving average of pps"],
        ["Δt", "dt = now - self._pps_ema_last_ts", "s", "Time since last EMA update"],
        ["H", "jam_timeout_ema_halflife_s", "s", "Half-life used to set EMA aggressiveness"],
        ["τ", "tau = H / ln(2)", "s", "EMA time constant from half-life"],
        ["α", "alpha = 1 - exp(-Δt/τ)", "–", "EMA update gain"],
        ["K", "jam_timeout_k", "s·pulses", "Scale factor in timeout ≈ K / pps_ema"],
        ["pps_floor", "jam_timeout_pps_floor", "pulses/s", "Minimum denominator to avoid blow-up"],
        ["T_min", "jam_timeout_min_s", "s", "Lower clamp on effective timeout"],
        ["T_max", "jam_timeout_max_s", "s", "Upper clamp on effective timeout"],
        ["T_jam_eff", "jam_timeout_effective_s", "s", "Effective jam timeout after scaling+clamp"],
        ["N_grace", "arm_grace_pulses", "pulses", "Suppress jam until ≥ N_grace pulses since (re)arm"],
        ["G", "arm_grace_s", "s", "Suppress jam until ≥ G seconds since (re)arm"],
    ]
    tbl = Table(table_data, colWidths=[0.9*inch, 2.2*inch, 0.8*inch, 3.1*inch])
    tbl.setStyle(TableStyle([
        ("BACKGROUND", (0,0), (-1,0), colors.lightgrey),
        ("GRID", (0,0), (-1,-1), 0.5, colors.grey),
        ("VALIGN", (0,0), (-1,-1), "MIDDLE"),
        ("ALIGN", (2,1), (2,-1), "CENTER"),
        ("FONTSIZE", (0,0), (-1,-1), 9),
    ]))
    story.append(tbl)

    story.append(PageBreak())

    # Math
    story.append(Paragraph("<b>Mathematical model</b>", styles["Heading2"]))
    story.append(Paragraph(
        """
        <b>Raw pulse rate.</b><br/>
        pps(t) = N / T
        <br/><br/>
        <b>EMA update.</b><br/>
        pps_ema(t) = (1 − α)·pps_ema(t−Δt) + α·pps(t)
        <br/><br/>
        <b>Half-life parameterization.</b><br/>
        Define half-life H such that an exponential decay reaches 1/2 at t=H:
        1/2 = exp(−H/τ) ⇒ τ = H / ln(2)
        <br/><br/>
        With τ defined, the discrete-time EMA gain is:
        α = 1 − exp(−Δt/τ) = 1 − exp(−(ln 2)·Δt/H)
        <br/><br/>
        <b>Adaptive timeout.</b><br/>
        T_jam_eff = clamp(T_min, T_max, K / max(pps_ema, pps_floor))
        """,
        styles["BodyText"]
    ))

    story.append(Spacer(1, 0.15*inch))
    story.append(Paragraph("<b>Plots</b>", styles["Heading2"]))
    story.append(Paragraph("pps vs pps_ema:", styles["BodyText"]))
    story.append(Spacer(1, 0.08*inch))
    story.append(Image(str(pps_plot), width=6.2*inch, height=3.6*inch))

    story.append(Spacer(1, 0.20*inch))
    story.append(Paragraph("Effective jam timeout:", styles["BodyText"]))
    story.append(Spacer(1, 0.08*inch))
    story.append(Image(str(timeout_plot), width=6.2*inch, height=3.6*inch))

    if dt_plot is not None:
        story.append(PageBreak())
        story.append(Paragraph("<b>Additional diagnostic: dt_since_pulse</b>", styles["Heading2"]))
        story.append(Paragraph(
            "If your JSONL contains dt_since_pulse in heartbeat events, this plot helps correlate "
            "approaching timeouts with sparse-extrusion regions.",
            styles["BodyText"]
        ))
        story.append(Spacer(1, 0.10*inch))
        story.append(Image(str(dt_plot), width=6.2*inch, height=3.6*inch))

    # Appendix
    story.append(PageBreak())
    story.append(Paragraph("<b>Appendix: derivations</b>", styles["Heading2"]))
    story.append(Paragraph(
        """
        <b>Where ln(2) comes from.</b><br/>
        Half-life means “the remaining weight is 1/2 after H seconds” for an exponential decay:
        w(t)=exp(−t/τ). Setting w(H)=1/2 gives 1/2=exp(−H/τ).
        Taking natural logs yields ln(1/2)=−H/τ. Since ln(1/2)=−ln(2), we obtain τ = H/ln(2).<br/><br/>
        <b>Discrete gain α.</b><br/>
        The EMA update corresponds to a discrete-time low-pass filter whose retained fraction is exp(−Δt/τ).
        Therefore 1−α = exp(−Δt/τ) ⇒ α = 1−exp(−Δt/τ). Substituting τ=H/ln(2) yields
        α = 1 − exp(−(ln 2)·Δt/H).
        """,
        styles["BodyText"]
    ))

    doc.build(story)



def _write_markdown(
    out_md: Path,
    pps_plot: Path,
    timeout_plot: Path,
    dt_plot: Optional[Path],
    data_source_note: str,
    assets_dir: Path,
) -> None:
    """
    Write a GitHub-renderable Markdown report.

    Notes:
    - Uses standard Markdown tables.
    - Embeds plots as relative image links into assets_dir.
    """
    # Compute relative paths from the markdown file location
    md_dir = out_md.parent.resolve()
    def rel(p: Path) -> str:
        try:
            return str(p.resolve().relative_to(md_dir)).replace("\\\\", "/")
        except Exception:
            # fall back: use assets_dir-relative
            return str(p).replace("\\\\", "/")

    lines: List[str] = []
    lines.append("# Adaptive Jam Timeout and pps_ema")
    lines.append("")
    lines.append(data_source_note.replace("<b>", "**").replace("</b>", "**"))
    lines.append("")
    lines.append("## Variable definitions")
    lines.append("")
    lines.append("| Symbol | Code name | Units | Meaning |")
    lines.append("|---|---|---:|---|")
    rows = [
        ("N", "`len(self._pulse_times)`", "pulses", "Pulse count in the sliding window"),
        ("T", "`self._pulse_window_s`", "s", "Sliding pulse window duration"),
        ("pps", "`self._pps(now)`", "pulses/s", "Instantaneous pulse rate N / T"),
        ("ppsₑₘₐ", "`self._pps_ema`", "pulses/s", "Exponential moving average of pps"),
        ("Δt", "`dt = now - self._pps_ema_last_ts`", "s", "Time since last EMA update"),
        ("H", "`jam_timeout_ema_halflife_s`", "s", "Half-life used to set EMA aggressiveness"),
        ("τ", "`tau = H / ln(2)`", "s", "EMA time constant from half-life"),
        ("α", "`alpha = 1 - exp(-Δt/τ)`", "–", "EMA update gain"),
        ("K", "`jam_timeout_k`", "s·pulses", "Scale factor in timeout ≈ K / pps_ema"),
        ("pps_floor", "`jam_timeout_pps_floor`", "pulses/s", "Minimum denominator to avoid blow-up"),
        ("T_min", "`jam_timeout_min_s`", "s", "Lower clamp on effective timeout"),
        ("T_max", "`jam_timeout_max_s`", "s", "Upper clamp on effective timeout"),
        ("T_jam_eff", "`jam_timeout_effective_s`", "s", "Effective jam timeout after scaling+clamp"),
        ("N_grace", "`arm_grace_pulses`", "pulses", "Suppress jam until ≥ N_grace pulses since (re)arm"),
        ("G", "`arm_grace_s`", "s", "Suppress jam until ≥ G seconds since (re)arm"),
    ]
    for sym, code, units, meaning in rows:
        lines.append(f"| {sym} | {code} | {units} | {meaning} |")

    lines.append("")
    lines.append("## Mathematical model")
    lines.append("")
    lines.append("### Raw pulse rate")
    lines.append("")
    lines.append("$$\\mathrm{pps}(t) = \\frac{N}{T}$$")
    lines.append("")
    lines.append("### EMA update")
    lines.append("")
    lines.append("$$\\mathrm{pps}_{\\mathrm{ema}}(t) = (1-\\alpha)\\,\\mathrm{pps}_{\\mathrm{ema}}(t-\\Delta t) + \\alpha\\,\\mathrm{pps}(t)$$")
    lines.append("")
    lines.append("### Half-life parameterization")
    lines.append("")
    lines.append("Define an exponential decay weight:")
    lines.append("")
    lines.append("$$w(t)=e^{-t/\\tau}$$")
    lines.append("")
    lines.append("Half-life $H$ means $w(H)=1/2$:")
    lines.append("")
    lines.append("$$\\frac{1}{2} = e^{-H/\\tau} \\;\\Rightarrow\\; \\tau = \\frac{H}{\\ln 2}$$")
    lines.append("")
    lines.append("Discrete-time EMA gain:")
    lines.append("")
    lines.append("$$\\alpha = 1 - e^{-\\Delta t/\\tau} = 1 - e^{-(\\ln 2)\\,\\Delta t/H}$$")
    lines.append("")
    lines.append("### Adaptive jam timeout")
    lines.append("")
    lines.append("$$T_{\\mathrm{jam}}^{\\mathrm{eff}} = \\mathrm{clamp}(T_{\\min}, T_{\\max}, \\; K / \\max(\\mathrm{pps}_{\\mathrm{ema}}, \\mathrm{pps}_{\\mathrm{floor}}))$$")
    lines.append("")
    lines.append("## Plots")
    lines.append("")
    lines.append(f"### pps vs pps_ema")
    lines.append("")
    lines.append(f"![pps vs pps_ema]({rel(pps_plot)})")
    lines.append("")
    lines.append("### Effective jam timeout")
    lines.append("")
    lines.append(f"![effective jam timeout]({rel(timeout_plot)})")

    if dt_plot is not None:
        lines.append("")
        lines.append("### dt_since_pulse (diagnostic)")
        lines.append("")
        lines.append(f"![dt_since_pulse]({rel(dt_plot)})")

    lines.append("")
    lines.append("## Appendix: where ln(2) comes from")
    lines.append("")
    lines.append("Half-life means the remaining weight is 1/2 after $H$ seconds for an exponential decay $w(t)=e^{-t/\\tau}$.")
    lines.append("Setting $w(H)=1/2$ gives $1/2=e^{-H/\\tau}$. Taking natural logs yields $\\ln(1/2)=-H/\\tau$.")
    lines.append("Since $\\ln(1/2)=-\\ln 2$, we obtain $\\tau=H/\\ln 2$.")
    lines.append("")

    out_md.write_text("\\n".join(lines), encoding="utf-8")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", type=Path, default=None, help="Monitor JSONL file (uses hb events for plots).")
    ap.add_argument("--out", type=Path, default=Path("Adaptive_Jam_Timeout_Report.pdf"), help="Output PDF path.")
    ap.add_argument("--md-out", type=Path, default=Path("Adaptive_Jam_Timeout_Report.md"), help="Output Markdown path.")
    ap.add_argument("--assets-dir", type=Path, default=Path(".report_artifacts"), help="Directory for generated plot images.")
    args = ap.parse_args()

    if args.jsonl is not None:
        series = _load_hb_series(args.jsonl)
        note = f"This report uses heartbeat ('hb') events parsed from: <b>{args.jsonl}</b>."
        prefix = "From JSONL"
    else:
        series = _synthetic_series()
        note = "This report uses a synthetic example signal (no --jsonl provided)."
        prefix = "Synthetic example"

    out_dir = args.assets_dir
    p1, p2, p3 = _plot_series(series, out_dir, prefix)
    _build_pdf(args.out, p1, p2, p3, note)
    _write_markdown(args.md_out, p1, p2, p3, note, out_dir)
    print(f"Wrote {args.out}")
    print(f"Wrote {args.md_out}")


if __name__ == "__main__":  # pragma: no cover
    main()
