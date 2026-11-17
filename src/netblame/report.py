"""Plain-text console report."""

from __future__ import annotations

import math
import textwrap

from .analysis import Analysis, TargetStats
from .config import format_duration

WIDTH = 88
TIMELINE_MAX_COLS = 60


def _fmt(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:.1f}" if v < 10 else f"{v:.0f}"


def _secs(seconds: float) -> str:
    return f"{seconds:.1f}s" if seconds < 10 else format_duration(seconds)


def _heading(title: str) -> list[str]:
    return ["", title, "-" * len(title)]


def _wrap(text: str, indent: str = "  ", first: str | None = None) -> list[str]:
    return textwrap.wrap(
        text,
        width=WIDTH,
        initial_indent=first if first is not None else indent,
        subsequent_indent=indent,
    ) or [first or indent]


def _table(analysis: Analysis) -> list[str]:
    header = ["Hop", "Target", "Host", "Sent", "Loss", "p50", "p95", "Max", "Jitter", "Status"]
    rows = [header]
    for t in analysis.targets:
        rows.append([
            str(t.target.level),
            t.target.name,
            t.target.host,
            str(t.sent),
            f"{t.loss_pct:.1f}%",
            _fmt(t.rtt_p50),
            _fmt(t.rtt_p95),
            _fmt(t.rtt_max),
            _fmt(t.jitter_ms),
            t.status.name,
        ])
    widths = [max(len(r[i]) for r in rows) for i in range(len(header))]
    right = {3, 4, 5, 6, 7, 8}
    lines = []
    for n, row in enumerate(rows):
        cells = [c.rjust(widths[i]) if i in right else c.ljust(widths[i]) for i, c in enumerate(row)]
        lines.append("  " + "  ".join(cells).rstrip())
        if n == 0:
            lines.append("  " + "  ".join("-" * w for w in widths))
    lines.append("  (times in ms; p50/p95 = median / 95th percentile of replies)")
    reasons = [(t.target.name, t.reasons) for t in analysis.targets if t.reasons]
    for name, why in reasons:
        lines.append(f"  {name}: {'; '.join(why)}")
    return lines


def _timeline_char(stats: TargetStats, ticks: range) -> str:
    by_tick = {s.tick: s for s in stats.samples}
    chunk = [by_tick[t] for t in ticks if t in by_tick]
    if not chunk:
        return " "
    lost = sum(1 for s in chunk if s.lost)
    if lost * 2 >= len(chunk):
        return "X"
    if lost:
        return "x"
    if any(s.rtt_ms is not None and s.rtt_ms > stats.thresholds.spike_ms for s in chunk):
        return "~"
    return "."


def _timeline(analysis: Analysis) -> list[str]:
    total = max(1, analysis.ticks_run)
    bucket = max(1, math.ceil(total / TIMELINE_MAX_COLS))
    cols = math.ceil(total / bucket)
    seconds_per_col = bucket * analysis.settings.interval_s
    name_w = max(len(t.target.name) for t in analysis.targets)
    lines = [
        f"  one column = {seconds_per_col:g} s    . ok   ~ slow (> spike limit)   "
        f"x some lost   X half or more lost",
    ]
    for t in analysis.targets:
        chars = "".join(
            _timeline_char(t, range(c * bucket, min((c + 1) * bucket, total))) for c in range(cols)
        )
        lines.append(f"  {t.target.name.ljust(name_w)} |{chars}|")
    start = analysis.started_at.strftime("%H:%M:%S")
    end = analysis.time_of(total).strftime("%H:%M:%S")
    lines.append(f"  {' ' * name_w}  {start}{end.rjust(max(0, cols - len(start)))}")
    return lines


def _incidents(analysis: Analysis) -> list[str]:
    if not analysis.incidents:
        return [
            f"  None (no streaks of {analysis.settings.burst_min_samples}+ consecutive bad samples)."
        ]
    lines = []
    name_w = max(len(b.target) for b in analysis.incidents)
    for b in analysis.incidents:
        secs = b.length * analysis.settings.interval_s
        max_txt = f"max {_fmt(b.max_rtt_ms)} ms" if b.max_rtt_ms is not None else "no replies"
        lines.append(
            f"  {analysis.time_of(b.start_tick):%H:%M:%S}  {b.target.ljust(name_w)}  "
            f"{_secs(secs):>6}  {b.lost:>3} lost, {max_txt:<14}  [{b.closer_context}]"
        )
    return lines


def render_report(analysis: Analysis) -> str:
    s = analysis.settings
    end = analysis.time_of(analysis.ticks_run)
    lines = [
        "=" * WIDTH,
        "netblame report",
        "=" * WIDTH,
        f"  Run      : {analysis.started_at:%Y-%m-%d %H:%M:%S} -> {end:%H:%M:%S}  "
        f"({format_duration(analysis.measured_s)}"
        f"{', stopped early' if analysis.interrupted else ''})",
        f"  Sampling : every {s.interval_s:g} s, timeout {s.timeout_ms} ms, "
        f"{len(s.targets)} targets in parallel",
        f"  Config   : {s.source}",
    ]
    lines += _heading("Per target (in path order)")
    lines += _table(analysis)
    lines += _heading("Timeline")
    lines += _timeline(analysis)
    lines += _heading("Worst incidents")
    lines += _incidents(analysis)

    lines += _heading("Verdict")
    marker = "OK " if not analysis.blamed else "!! "
    lines += _wrap(analysis.verdict, indent="     ", first=f"  {marker}")
    if analysis.evidence:
        lines += ["", "  Evidence:"]
        for e in analysis.evidence:
            lines += _wrap(e, indent="      ", first="    - ")
    if analysis.advice:
        lines += ["", "  What to do:"]
        for a in analysis.advice:
            lines += _wrap(a, indent="      ", first="    - ")
    if analysis.notes:
        lines += ["", "  Notes:"]
        for n in analysis.notes:
            lines += _wrap(n, indent="      ", first="    - ")
    lines.append("=" * WIDTH)
    return "\n".join(lines)


