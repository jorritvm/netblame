"""Configuration: targets, timing and thresholds.

Settings come from (highest priority first): CLI arguments, a YAML config
file, and the built-in defaults defined here.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any

import yaml

#: Known target kinds and the hop level they map to (path order, PC outwards).
KINDS: dict[str, int] = {"lan": 1, "isp_gateway": 2, "internet": 3}

DEFAULT_CONFIG_RELPATH = Path("config") / "netblame.yaml"
CONFIG_ENV_VAR = "NETBLAME_CONFIG"

MIN_DURATION_S = 10.0
MAX_DURATION_S = 24 * 3600.0


class ConfigError(ValueError):
    """Raised for invalid configuration files or CLI values."""


@dataclass(frozen=True)
class Thresholds:
    """Limits that decide whether a target is OK, WARN or BAD.

    ``spike_ms`` marks a single reply as a "bad sample" (just like a lost
    ping); bad samples are what incidents and time-correlation are built on.
    """

    loss_warn_pct: float
    loss_bad_pct: float
    p95_warn_ms: float
    p95_bad_ms: float
    jitter_warn_ms: float
    jitter_bad_ms: float
    spike_ms: float


DEFAULT_THRESHOLDS: dict[str, Thresholds] = {
    "lan": Thresholds(0.5, 2.0, 20, 50, 8, 20, 150),
    "isp_gateway": Thresholds(0.5, 2.0, 25, 60, 8, 20, 150),
    "internet": Thresholds(1.0, 3.0, 60, 150, 15, 40, 300),
}


@dataclass(frozen=True)
class Target:
    name: str
    host: str
    kind: str
    level: int


DEFAULT_TARGETS: tuple[Target, ...] = (
    Target("LAN router", "192.168.1.1", "lan", KINDS["lan"]),
    Target("ISP gateway", "192.168.0.1", "isp_gateway", KINDS["isp_gateway"]),
    Target("Google DNS", "8.8.8.8", "internet", KINDS["internet"]),
    Target("Cloudflare DNS", "1.1.1.1", "internet", KINDS["internet"]),
)


@dataclass(frozen=True)
class Settings:
    targets: tuple[Target, ...] = DEFAULT_TARGETS
    duration_s: float = 120.0
    interval_s: float = 1.0
    timeout_ms: int = 1000
    burst_min_samples: int = 2
    thresholds: dict[str, Thresholds] = field(default_factory=lambda: dict(DEFAULT_THRESHOLDS))
    llm_model: str = "claude-sonnet-5-5"
    source: str = "built-in defaults"


# --------------------------------------------------------------------------- #
# Parsing helpers
# --------------------------------------------------------------------------- #

_DURATION_RE = re.compile(
    r"^(?:(?P<h>\d+)h)?(?:(?P<m>\d+)m)?(?:(?P<s>\d+(?:\.\d+)?)s?)?$"
)


def parse_duration(value: Any) -> float:
    """Parse ``90``, ``"90s"``, ``"2m"``, ``"1m30s"`` or ``"1h"`` into seconds."""
    if isinstance(value, bool):
        raise ConfigError(f"Invalid duration {value!r}")
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().lower().replace(" ", "")
    match = _DURATION_RE.match(text)
    if not text or match is None or not any(match.groupdict().values()):
        raise ConfigError(f"Invalid duration {value!r} (use e.g. 90, 90s, 2m, 1m30s, 1h)")
    return int(match["h"] or 0) * 3600 + int(match["m"] or 0) * 60 + float(match["s"] or 0)


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours}h{minutes:02d}m{secs:02d}s"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def make_target(name: Any, host: Any, kind: Any = "internet", level: Any = None) -> Target:
    name = str(name or "").strip()
    host = str(host or "").strip()
    kind = str(kind or "internet").strip().lower()
    if not name:
        raise ConfigError("Every target needs a name")
    if not host:
        raise ConfigError(f"Target {name!r} needs a host")
    if kind not in KINDS:
        raise ConfigError(
            f"Target {name!r}: unknown kind {kind!r} (choose from {', '.join(KINDS)})"
        )
    if level is None:
        level_int = KINDS[kind]
    else:
        try:
            level_int = int(level)
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Target {name!r}: level must be an integer") from exc
    return Target(name=name, host=host, kind=kind, level=level_int)


def parse_target_spec(spec: str) -> Target:
    """Parse a CLI target ``NAME=HOST[:KIND]``, e.g. ``ER605=192.168.1.1:lan``."""
    if "=" not in spec:
        raise ConfigError(f"Invalid target {spec!r}: expected NAME=HOST[:KIND]")
    name, rest = spec.split("=", 1)
    host, kind = rest, "internet"
    if ":" in rest:
        maybe_host, maybe_kind = rest.rsplit(":", 1)
        if maybe_kind.lower() in KINDS:
            host, kind = maybe_host, maybe_kind
    return make_target(name, host, kind)


def _check_keys(section: str, data: dict[str, Any], allowed: set[str]) -> None:
    unknown = set(data) - allowed
    if unknown:
        raise ConfigError(
            f"Unknown key(s) in '{section}': {', '.join(sorted(unknown))} "
            f"(allowed: {', '.join(sorted(allowed))})"
        )


def _as_dict(section: str, value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(f"'{section}' must be a mapping")
    return value


def settings_from_dict(data: dict[str, Any], source: str) -> Settings:
    _check_keys("<root>", data, {"run", "targets", "thresholds", "llm"})
    settings = Settings(source=source)
    updates: dict[str, Any] = {}

    run = _as_dict("run", data.get("run"))
    _check_keys("run", run, {"duration", "interval_s", "timeout_ms", "burst_min_samples"})
    try:
        if "duration" in run:
            updates["duration_s"] = parse_duration(run["duration"])
        if "interval_s" in run:
            updates["interval_s"] = float(run["interval_s"])
        if "timeout_ms" in run:
            updates["timeout_ms"] = int(run["timeout_ms"])
        if "burst_min_samples" in run:
            updates["burst_min_samples"] = int(run["burst_min_samples"])
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"Invalid value in 'run': {exc}") from exc

    if "targets" in data:
        raw_targets = data["targets"]
        if not isinstance(raw_targets, list) or not raw_targets:
            raise ConfigError("'targets' must be a non-empty list")
        targets = []
        for item in raw_targets:
            item = _as_dict("targets[]", item)
            _check_keys("targets[]", item, {"name", "host", "kind", "level"})
            targets.append(
                make_target(item.get("name"), item.get("host"), item.get("kind"), item.get("level"))
            )
        updates["targets"] = tuple(targets)

    thresholds = dict(DEFAULT_THRESHOLDS)
    raw_thresholds = _as_dict("thresholds", data.get("thresholds"))
    _check_keys("thresholds", raw_thresholds, set(KINDS))
    allowed_fields = {f.name for f in fields(Thresholds)}
    for kind, overrides in raw_thresholds.items():
        overrides = _as_dict(f"thresholds.{kind}", overrides)
        _check_keys(f"thresholds.{kind}", overrides, allowed_fields)
        try:
            thresholds[kind] = replace(
                thresholds[kind], **{k: float(v) for k, v in overrides.items()}
            )
        except (TypeError, ValueError) as exc:
            raise ConfigError(f"Invalid value in 'thresholds.{kind}': {exc}") from exc
    updates["thresholds"] = thresholds

    llm = _as_dict("llm", data.get("llm"))
    _check_keys("llm", llm, {"model"})
    if "model" in llm:
        updates["llm_model"] = str(llm["model"])

    return replace(settings, **updates)


def _resolve_config_path(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        if not path.is_file():
            raise ConfigError(f"Config file not found: {path}")
        return path
    env_value = os.environ.get(CONFIG_ENV_VAR)
    if env_value:
        path = Path(env_value).expanduser()
        if not path.is_file():
            raise ConfigError(f"Config file from {CONFIG_ENV_VAR} not found: {path}")
        return path
    local = Path.cwd() / DEFAULT_CONFIG_RELPATH
    return local if local.is_file() else None


def load_settings(explicit_path: str | None = None) -> Settings:
    """Load settings from the first config file found, or the built-in defaults."""
    path = _resolve_config_path(explicit_path)
    if path is None:
        return Settings()
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError(f"Could not read config file {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"Config file {path} must contain a mapping at the top level")
    return settings_from_dict(data, source=str(path))


def validate(settings: Settings) -> None:
    """Sanity-check final settings (after CLI overrides)."""
    if not MIN_DURATION_S <= settings.duration_s <= MAX_DURATION_S:
        raise ConfigError(
            f"Duration must be between {format_duration(MIN_DURATION_S)} "
            f"and {format_duration(MAX_DURATION_S)}"
        )
    if not 0.2 <= settings.interval_s <= 10:
        raise ConfigError("Interval must be between 0.2 and 10 seconds")
    if not 100 <= settings.timeout_ms <= 10_000:
        raise ConfigError("Timeout must be between 100 and 10000 ms")
    if settings.burst_min_samples < 1:
        raise ConfigError("burst_min_samples must be at least 1")
    if not settings.targets:
        raise ConfigError("At least one target is required")
    names = [t.name for t in settings.targets]
    duplicates = {n for n in names if names.count(n) > 1}
    if duplicates:
        raise ConfigError(f"Duplicate target name(s): {', '.join(sorted(duplicates))}")
    for kind, th in settings.thresholds.items():
        if th.loss_warn_pct > th.loss_bad_pct or th.p95_warn_ms > th.p95_bad_ms or (
            th.jitter_warn_ms > th.jitter_bad_ms
        ):
            raise ConfigError(f"thresholds.{kind}: every *_warn value must be <= its *_bad value")


def describe_settings(settings: Settings) -> str:
    """Human-readable dump of the effective settings (for --show-config)."""
    lines = [
        f"Config source : {settings.source}",
        f"Duration      : {format_duration(settings.duration_s)}",
        f"Interval      : {settings.interval_s:g} s",
        f"Timeout       : {settings.timeout_ms} ms",
        f"Burst minimum : {settings.burst_min_samples} samples",
        f"LLM model     : {settings.llm_model}",
        "Targets (path order):",
    ]
    for t in sorted(settings.targets, key=lambda t: t.level):
        lines.append(f"  level {t.level}  {t.name:<20} {t.host:<20} {t.kind}")
    lines.append("Thresholds:")
    for kind, th in settings.thresholds.items():
        lines.append(
            f"  {kind:<12} loss {th.loss_warn_pct:g}/{th.loss_bad_pct:g} %  "
            f"p95 {th.p95_warn_ms:g}/{th.p95_bad_ms:g} ms  "
            f"jitter {th.jitter_warn_ms:g}/{th.jitter_bad_ms:g} ms  spike {th.spike_ms:g} ms"
        )
    return "\n".join(lines)
