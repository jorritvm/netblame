"""Parallel ping sampling using the Windows ``ping`` command.

Every ``interval_s`` a "tick" fires and one ``ping -n 1`` is started for each
target at the same moment. Samples from different targets that share a tick
were taken at the same time, which is what makes time-correlation possible.
"""

from __future__ import annotations

import codecs
import math
import re
import subprocess
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime

from .config import Target

# "time=12ms", "time<1ms", Dutch "tijd=12 ms", ... The first match in the
# output is the echo reply line (the statistics block comes later).
_RTT_RE = re.compile(r"([=<])\s*(\d+(?:[.,]\d+)?)\s*ms\b", re.IGNORECASE)

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _console_encoding() -> str | None:
    """Windows console tools write in the OEM code page."""
    try:
        codecs.lookup("oem")
    except LookupError:
        return None
    return "oem"


_ENCODING = _console_encoding()


@dataclass(frozen=True)
class Sample:
    tick: int
    offset_s: float  # scheduled time since the start of the run
    rtt_ms: float | None  # None means no echo reply
    error: str | None = None

    @property
    def lost(self) -> bool:
        return self.rtt_ms is None


def parse_ping_output(output: str) -> tuple[float | None, str | None]:
    """Return ``(rtt_ms, None)`` for a reply or ``(None, reason)`` otherwise."""
    match = _RTT_RE.search(output)
    if match:
        value = float(match.group(2).replace(",", "."))
        if match.group(1) == "<":  # "time<1ms": report half the bound
            value /= 2
        return value, None
    lowered = output.lower()
    if "unreachable" in lowered:
        return None, "unreachable"
    if "expired" in lowered:
        return None, "ttl expired"
    if "general failure" in lowered:
        return None, "general failure"
    if "could not find host" in lowered:
        return None, "dns lookup failed"
    return None, "no reply"


def ping_once(host: str, timeout_ms: int) -> tuple[float | None, str | None]:
    """Send a single echo request with the Windows ``ping`` command."""
    cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding=_ENCODING,
            errors="replace",
            timeout=timeout_ms / 1000 + 5,
            creationflags=_NO_WINDOW,
        )
    except subprocess.TimeoutExpired:
        return None, "ping process hung"
    except OSError as exc:
        return None, f"could not run ping: {exc}"
    return parse_ping_output(proc.stdout or "")


PingFn = Callable[[str, int], tuple[float | None, str | None]]
ProgressFn = Callable[[int, int], None]


def _sleep_until(deadline: float) -> None:
    # Short sleeps keep Ctrl+C responsive on Windows.
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        time.sleep(min(remaining, 0.25))


class PingSession:
    """Runs the parallel measurement and collects samples per target."""

    def __init__(
        self,
        targets: tuple[Target, ...],
        duration_s: float,
        interval_s: float,
        timeout_ms: int,
        pinger: PingFn = ping_once,
    ) -> None:
        self.targets = targets
        self.interval_s = interval_s
        self.timeout_ms = timeout_ms
        self.total_ticks = max(1, int(round(duration_s / interval_s)))
        self.samples: dict[str, list[Sample]] = {t.name: [] for t in targets}
        self.started_at: datetime | None = None
        self.ticks_started = 0
        self.interrupted = False
        self._pinger = pinger
        self._lock = threading.Lock()

    def _sample(self, target: Target, tick: int, offset_s: float) -> None:
        try:
            rtt, error = self._pinger(target.host, self.timeout_ms)
        except Exception as exc:  # never let one bad ping kill the run
            rtt, error = None, f"error: {exc}"
        with self._lock:
            self.samples[target.name].append(Sample(tick, offset_s, rtt, error))

    def live_counts(self) -> dict[str, tuple[int, int]]:
        """``{target name: (samples done, samples lost)}`` for progress display."""
        with self._lock:
            return {
                name: (len(samples), sum(1 for s in samples if s.lost))
                for name, samples in self.samples.items()
            }

    def run(self, progress: ProgressFn | None = None) -> None:
        in_flight_per_target = math.ceil(self.timeout_ms / 1000 / self.interval_s) + 2
        workers = max(4, len(self.targets) * in_flight_per_target)
        self.started_at = datetime.now()
        start = time.monotonic()
        pending: list[Future[None]] = []
        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="netblame")
        try:
            for tick in range(self.total_ticks):
                _sleep_until(start + tick * self.interval_s)
                offset = tick * self.interval_s
                for target in self.targets:
                    pending.append(pool.submit(self._sample, target, tick, offset))
                self.ticks_started = tick + 1
                pending = [f for f in pending if not f.done()]
                if progress:
                    progress(tick + 1, self.total_ticks)
            # let the last pings finish (at most one timeout)
            _sleep_until(time.monotonic() + 0.05)
            for future in pending:
                future.result()
        except KeyboardInterrupt:
            self.interrupted = True
            try:
                for future in pending:
                    future.result(timeout=self.timeout_ms / 1000 + 6)
            except KeyboardInterrupt:
                pass  # second Ctrl+C: stop waiting, analyse what we have
            except Exception:
                pass
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
            with self._lock:
                for samples in self.samples.values():
                    samples.sort(key=lambda s: s.tick)
