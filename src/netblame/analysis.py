"""Deterministic analysis: per-target statistics, hop-level correlation, verdict.

See docs/analysis.md for the reasoning behind the rules.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import IntEnum
from typing import Any

from .config import Settings, Target, Thresholds, format_duration
from .pinger import PingSession, Sample

#: When at least this share of a hop's bad samples coincide with bad samples on
#: closer hops, the hop's trouble is treated as a knock-on effect.
KNOCK_ON_SHARE = 0.6

#: Replies slower than this (while still arriving) suggest queueing / bufferbloat.
QUEUEING_RTT_MS = 1000.0

#: Runs shorter than this get a "try a longer window" note when healthy.
SHORT_RUN_S = 300.0

KIND_LABELS = {
    "lan": "your local network",
    "isp_gateway": "the link between your router and the ISP gateway",
    "internet": "the ISP line / upstream network",
}

KIND_HINTS = {
    "lan": (
        "Look inside your own network: Wi-Fi signal or roaming, the access point or "
        "mesh node, the switch, cabling, or the router's LAN side. Re-run from a wired "
        "PC: if wired is clean, the problem is Wi-Fi."
    ),
    "isp_gateway": (
        "Look at the link between your router and the ISP gateway: the cable between "
        "them, the router's WAN port, or the gateway's LAN port."
    ),
    "internet": (
        "The problem is beyond the ISP gateway: the line itself (coax/fibre/DSL), the "
        "gateway's modem part, or the ISP's network. Check the gateway's line "
        "diagnostics and event log, restart it once, check the coax connectors, then "
        "contact the ISP with this report."
    ),
}


class Status(IntEnum):
    OK = 0
    WARN = 1
    BAD = 2


@dataclass
class Burst:
    """A streak of consecutive bad samples on one target."""

    target: str
    start_tick: int
    end_tick: int  # inclusive
    lost: int
    max_rtt_ms: float | None
    closer_context: str = ""

    @property
    def length(self) -> int:
        return self.end_tick - self.start_tick + 1


@dataclass
class TargetStats:
    target: Target
    thresholds: Thresholds
    samples: list[Sample]
    sent: int
    received: int
    loss_pct: float
    rtt_min: float | None
    rtt_p50: float | None
    rtt_p95: float | None
    rtt_max: float | None
    rtt_mean: float | None
    jitter_ms: float | None
    spikes: int
    longest_loss_streak: int
    bad_ticks: set[int]
    bursts: list[Burst]
    status: Status
    reasons: list[str]

    @property
    def lost(self) -> int:
        return self.sent - self.received


@dataclass
class LevelResult:
    level: int
    targets: list[TargetStats]
    status: Status
    bad_ticks: set[int]
    own_bad_ticks: set[int]
    knock_on_bad_ticks: set[int]
    segment: str
    hint: str

    @property
    def names(self) -> str:
        return " / ".join(t.target.name for t in self.targets)

    @property
    def kind(self) -> str:
        return self.targets[0].target.kind


@dataclass
class Analysis:
    settings: Settings
    started_at: datetime
    ticks_planned: int
    ticks_run: int
    interrupted: bool
    targets: list[TargetStats]
    levels: list[LevelResult]
    blamed: list[LevelResult]
    verdict: str
    evidence: list[str] = field(default_factory=list)
    advice: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    incidents: list[Burst] = field(default_factory=list)

    @property
    def measured_s(self) -> float:
        return self.ticks_run * self.settings.interval_s

    def time_of(self, tick: int) -> datetime:
        return self.started_at + timedelta(seconds=tick * self.settings.interval_s)

    def to_dict(self) -> dict[str, Any]:
        """Compact, JSON-serialisable summary (used for --explain)."""

        def r(v: float | None) -> float | None:
            return None if v is None else round(v, 1)

        return {
            "run": {
                "started_at": self.started_at.isoformat(timespec="seconds"),
                "measured_seconds": round(self.measured_s),
                "interval_s": self.settings.interval_s,
                "timeout_ms": self.settings.timeout_ms,
                "stopped_early": self.interrupted,
            },
            "targets": [
                {
                    "name": t.target.name,
                    "host": t.target.host,
                    "kind": t.target.kind,
                    "level": t.target.level,
                    "sent": t.sent,
                    "lost": t.lost,
                    "loss_pct": r(t.loss_pct),
                    "rtt_ms": {
                        "min": r(t.rtt_min),
                        "p50": r(t.rtt_p50),
                        "p95": r(t.rtt_p95),
                        "max": r(t.rtt_max),
                    },
                    "jitter_ms": r(t.jitter_ms),
                    "spikes": t.spikes,
                    "longest_loss_streak": t.longest_loss_streak,
                    "status": t.status.name,
                    "reasons": t.reasons,
                }
                for t in self.targets
            ],
            "levels": [
                {
                    "level": lv.level,
                    "targets": lv.names,
                    "segment": lv.segment,
                    "status": lv.status.name,
                    "bad_samples": len(lv.bad_ticks),
                    "bad_while_closer_hops_healthy": len(lv.own_bad_ticks),
                    "bad_while_closer_hops_also_bad": len(lv.knock_on_bad_ticks),
                }
                for lv in self.levels
            ],
            "verdict": self.verdict,
            "blamed_segments": [lv.segment for lv in self.blamed],
            "evidence": self.evidence,
            "advice": self.advice,
            "notes": self.notes,
            "incidents": [
                {
                    "target": b.target,
                    "start": self.time_of(b.start_tick).strftime("%H:%M:%S"),
                    "samples": b.length,
                    "lost": b.lost,
                    "max_rtt_ms": r(b.max_rtt_ms),
                    "closer_hops": b.closer_context,
                }
                for b in self.incidents
            ],
        }


# --------------------------------------------------------------------------- #
# Per-target statistics
# --------------------------------------------------------------------------- #


def _percentile(sorted_values: list[float], p: float) -> float | None:
    if not sorted_values:
        return None
    k = (len(sorted_values) - 1) * p
    lo = math.floor(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def _is_bad(sample: Sample, th: Thresholds) -> bool:
    return sample.lost or (sample.rtt_ms is not None and sample.rtt_ms > th.spike_ms)


def _grade(value: float | None, warn: float, bad: float, label: str, unit: str,
           reasons: list[str]) -> Status:
    if value is None:
        return Status.OK
    if value >= bad:
        reasons.append(f"{label} {value:.1f}{unit} (bad >= {bad:g}{unit})")
        return Status.BAD
    if value >= warn:
        reasons.append(f"{label} {value:.1f}{unit} (warn >= {warn:g}{unit})")
        return Status.WARN
    return Status.OK


def _find_bursts(target: str, samples: list[Sample], th: Thresholds, min_len: int) -> list[Burst]:
    bursts: list[Burst] = []
    run: list[Sample] = []

    def close() -> None:
        if len(run) >= min_len:
            rtts = [s.rtt_ms for s in run if s.rtt_ms is not None]
            bursts.append(
                Burst(
                    target=target,
                    start_tick=run[0].tick,
                    end_tick=run[-1].tick,
                    lost=sum(1 for s in run if s.lost),
                    max_rtt_ms=max(rtts) if rtts else None,
                )
            )

    for sample in samples:
        if _is_bad(sample, th) and (not run or sample.tick == run[-1].tick + 1):
            run.append(sample)
            continue
        close()
        run = [sample] if _is_bad(sample, th) else []
    close()
    return bursts


def target_stats(target: Target, samples: list[Sample], th: Thresholds,
                 burst_min: int) -> TargetStats:
    sent = len(samples)
    rtts = [s.rtt_ms for s in samples if s.rtt_ms is not None]
    received = len(rtts)
    loss_pct = 100.0 * (sent - received) / sent if sent else 100.0
    ordered = sorted(rtts)

    jitter = None
    if len(rtts) >= 2:
        jitter = statistics.fmean(abs(b - a) for a, b in zip(rtts, rtts[1:]))

    longest = current = 0
    for s in samples:
        current = current + 1 if s.lost else 0
        longest = max(longest, current)

    reasons: list[str] = []
    if sent == 0:
        status = Status.BAD
        reasons.append("no samples collected")
    elif received == 0:
        status = Status.BAD
        reasons.append("no replies at all")
    else:
        p95 = _percentile(ordered, 0.95)
        status = max(
            _grade(loss_pct, th.loss_warn_pct, th.loss_bad_pct, "loss", "%", reasons),
            _grade(p95, th.p95_warn_ms, th.p95_bad_ms, "p95", " ms", reasons),
            _grade(jitter, th.jitter_warn_ms, th.jitter_bad_ms, "jitter", " ms", reasons),
        )

    return TargetStats(
        target=target,
        thresholds=th,
        samples=samples,
        sent=sent,
        received=received,
        loss_pct=loss_pct,
        rtt_min=ordered[0] if ordered else None,
        rtt_p50=_percentile(ordered, 0.5),
        rtt_p95=_percentile(ordered, 0.95),
        rtt_max=ordered[-1] if ordered else None,
        rtt_mean=statistics.fmean(rtts) if rtts else None,
        jitter_ms=jitter,
        spikes=sum(1 for r in rtts if r > th.spike_ms),
        longest_loss_streak=longest,
        bad_ticks={s.tick for s in samples if _is_bad(s, th)},
        bursts=_find_bursts(target.name, samples, th, burst_min),
        status=Status(status),
        reasons=reasons,
    )


# --------------------------------------------------------------------------- #
# Hop levels and attribution
# --------------------------------------------------------------------------- #


def _fmt(v: float | None) -> str:
    if v is None:
        return "-"
    return f"{v:.1f}" if v < 10 else f"{v:.0f}"


def _summary_line(t: TargetStats) -> str:
    return (
        f"{t.target.name} ({t.target.host}): {t.loss_pct:.1f}% loss, "
        f"p50 {_fmt(t.rtt_p50)} ms, p95 {_fmt(t.rtt_p95)} ms, max {_fmt(t.rtt_max)} ms, "
        f"jitter {_fmt(t.jitter_ms)} ms"
    )


def _build_levels(stats: list[TargetStats]) -> list[LevelResult]:
    by_level: dict[int, list[TargetStats]] = {}
    for t in stats:
        by_level.setdefault(t.target.level, []).append(t)

    levels: list[LevelResult] = []
    closer_bad: set[int] = set()
    previous_names = "this PC"
    for level in sorted(by_level):
        members = by_level[level]
        # The hop as a whole is bad at a tick only if every target at this hop
        # is bad then; one bad destination among healthy ones is route-specific.
        bad = set.intersection(*(m.bad_ticks for m in members))
        names = " / ".join(m.target.name for m in members)
        kind = members[0].target.kind
        levels.append(
            LevelResult(
                level=level,
                targets=members,
                status=Status(min(m.status for m in members)),
                bad_ticks=bad,
                own_bad_ticks=bad - closer_bad,
                knock_on_bad_ticks=bad & closer_bad,
                segment=f"{KIND_LABELS[kind]} (between {previous_names} and {names})",
                hint=KIND_HINTS[kind],
            )
        )
        closer_bad |= bad
        previous_names = names
    return levels


def _closer_context(burst: Burst, level: int, levels: list[LevelResult]) -> str:
    ticks = set(range(burst.start_tick, burst.end_tick + 1))
    closer = [lv for lv in levels if lv.level < level]
    if not closer:
        return "first hop"
    parts = []
    for lv in closer:
        overlap = len(ticks & lv.bad_ticks)
        if overlap:
            parts.append(f"{lv.names} bad {overlap}/{len(ticks)}")
    return "; ".join(parts) if parts else "closer hops clean"


def _select_incidents(stats: list[TargetStats], levels: list[LevelResult],
                      limit: int = 10) -> list[Burst]:
    all_bursts = []
    for t in stats:
        for b in t.bursts:
            b.closer_context = _closer_context(b, t.target.level, levels)
            all_bursts.append(b)
    worst = sorted(all_bursts, key=lambda b: (b.lost, b.length), reverse=True)[:limit]
    return sorted(worst, key=lambda b: (b.start_tick, b.target))


def analyse(session: PingSession, settings: Settings) -> Analysis:
    stats = [
        target_stats(
            t,
            list(session.samples[t.name]),
            settings.thresholds[t.kind],
            settings.burst_min_samples,
        )
        for t in sorted(settings.targets, key=lambda t: t.level)
    ]
    levels = _build_levels(stats)

    notes: list[str] = []
    blamed: list[LevelResult] = []

    for i, lv in enumerate(levels):
        # Route-specific trouble: one target clearly worse than its siblings.
        if len(lv.targets) > 1:
            for t in lv.targets:
                if t.status > lv.status:
                    others = ", ".join(o.target.name for o in lv.targets if o is not t)
                    notes.append(
                        f"{t.target.name} is worse than {others} at the same hop, so its "
                        f"problem is specific to that destination or route, not to your "
                        f"connection."
                    )

        if lv.status == Status.OK:
            continue

        farther = levels[i + 1:]
        if farther and all(f.status == Status.OK for f in farther):
            notes.append(
                f"{lv.names} looks degraded, but traffic passing through it to farther hops "
                f"is healthy. The device most likely rate-limits or deprioritises pings "
                f"aimed at itself; that is usually harmless."
            )
            continue

        if lv.bad_ticks:
            share = len(lv.knock_on_bad_ticks) / len(lv.bad_ticks)
            if share >= KNOCK_ON_SHARE and i > 0:
                notes.append(
                    f"{share:.0%} of the bad samples on {lv.names} happened while a closer "
                    f"hop was also bad, so they are mostly a knock-on effect."
                )
                if not blamed:
                    # Closer hop stayed under its thresholds, yet its hiccups line
                    # up with the farther problem: it is the more likely culprit.
                    culprit = max(
                        levels[:i], key=lambda c: len(c.bad_ticks & lv.bad_ticks)
                    )
                    blamed.append(culprit)
                    notes.append(
                        f"{culprit.names} stayed within its thresholds on its own, but its "
                        f"hiccups line up with the problems further out."
                    )
                continue

        if lv not in blamed:
            blamed.append(lv)

    evidence: list[str] = []
    advice: list[str] = []
    duration_txt = format_duration(session.ticks_started * settings.interval_s)

    if not blamed:
        if all(t.status == Status.OK for t in stats):
            verdict = f"No problems detected: every hop was healthy during this {duration_txt} window."
        else:
            verdict = "No real problem with your connection detected (see notes)."
        if session.ticks_started * settings.interval_s < SHORT_RUN_S:
            advice.append(
                "Intermittent problems can hide between runs; if you still notice drops, "
                "try a longer window (e.g. -d 15m) while the problem is happening."
            )
    else:
        primary = blamed[0]
        if len(blamed) == 1:
            verdict = f"Problem located in {primary.segment}."
        else:
            verdict = "Problems found on multiple segments: " + "; ".join(
                lv.segment for lv in blamed
            ) + "."

        for lv in blamed:
            for t in lv.targets:
                evidence.append(_summary_line(t))
            closer = [c for c in levels if c.level < lv.level]
            if closer:
                closer_txt = ", ".join(
                    f"{t.target.name} {t.loss_pct:.1f}% loss / p95 {_fmt(t.rtt_p95)} ms"
                    for c in closer
                    for t in c.targets
                )
                evidence.append(f"Closer hops over the same period: {closer_txt}.")
            if lv.bad_ticks and closer:
                evidence.append(
                    f"{len(lv.own_bad_ticks)} of {len(lv.bad_ticks)} bad moments at this hop "
                    f"({lv.names}) happened while all closer hops were healthy."
                )
            advice.append(lv.hint)
            if lv.kind == "internet" and any(
                (t.rtt_max or 0) >= QUEUEING_RTT_MS for t in lv.targets
            ):
                advice.append(
                    "Replies that still arrive after more than a second point to packets "
                    "queueing, not just disappearing: congestion on the ISP side or your "
                    "own upload saturating the line. Pause big uploads/backups and re-run "
                    "to rule out the latter."
                )

    if session.interrupted:
        notes.append("The run was stopped early; results cover only the measured part.")

    return Analysis(
        settings=settings,
        started_at=session.started_at or datetime.now(),
        ticks_planned=session.total_ticks,
        ticks_run=session.ticks_started,
        interrupted=session.interrupted,
        targets=stats,
        levels=levels,
        blamed=blamed,
        verdict=verdict,
        evidence=evidence,
        advice=advice,
        notes=notes,
        incidents=_select_incidents(stats, levels),
    )
