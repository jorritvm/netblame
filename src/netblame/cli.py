"""Command-line entry point."""

from __future__ import annotations

import argparse
import shutil
import sys
import textwrap
from dataclasses import replace

from . import __version__
from .analysis import analyse
from .config import (
    ConfigError,
    Settings,
    describe_settings,
    format_duration,
    load_settings,
    parse_duration,
    parse_target_spec,
    validate,
)
from .pinger import PingSession
from .report import WIDTH, render_report

EXIT_OK = 0
EXIT_PROBLEM = 1
EXIT_ERROR = 2
EXIT_INTERRUPTED = 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="netblame",
        description=(
            "Ping every hop of your network path in parallel (LAN router, ISP gateway, "
            "internet), then tell you which segment is to blame for loss and latency."
        ),
        epilog=textwrap.dedent(
            """\
            examples:
              netblame                          run with config/defaults (2 minutes)
              netblame -d 5m                    measure for 5 minutes
              netblame -t ER605=192.168.1.1:lan -t HGW=192.168.0.1:isp_gateway \\
                       -t Google=8.8.8.8        custom targets (replace the config's)
              netblame --show-config            print the effective settings
              netblame --explain                add an LLM explanation (netblame[llm])

            exit codes: 0 healthy, 1 problem found, 2 error, 130 stopped before any data
            """
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-c", "--config", metavar="PATH",
                        help="YAML config file (default: $NETBLAME_CONFIG, then "
                             "config\\netblame.yaml in the current directory)")
    parser.add_argument("-d", "--duration", metavar="TIME",
                        help="measurement window, e.g. 90, 90s, 2m, 1m30s (default 2m)")
    parser.add_argument("-i", "--interval", metavar="SEC", type=float,
                        help="seconds between pings per target (default 1)")
    parser.add_argument("-w", "--timeout", metavar="MS", type=int,
                        help="reply timeout per ping in ms (default 1000)")
    parser.add_argument("-t", "--target", metavar="NAME=HOST[:KIND]", action="append",
                        help="target to ping; repeat in path order. KIND is lan, "
                             "isp_gateway or internet (default internet). Replaces the "
                             "configured targets.")
    parser.add_argument("--explain", action="store_true",
                        help="add a plain-language explanation from Claude "
                             "(needs netblame[llm] and ANTHROPIC_API_KEY)")
    parser.add_argument("--model", help="model for --explain (overrides config)")
    parser.add_argument("--no-progress", action="store_true",
                        help="do not show the live progress line")
    parser.add_argument("--show-config", action="store_true",
                        help="print the effective settings and exit")
    parser.add_argument("-V", "--version", action="version", version=f"netblame {__version__}")
    return parser


def apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    updates = {}
    if args.duration is not None:
        updates["duration_s"] = parse_duration(args.duration)
    if args.interval is not None:
        updates["interval_s"] = args.interval
    if args.timeout is not None:
        updates["timeout_ms"] = args.timeout
    if args.target:
        updates["targets"] = tuple(parse_target_spec(spec) for spec in args.target)
    if args.model:
        updates["llm_model"] = args.model
    return replace(settings, **updates) if updates else settings


def _make_progress(session: PingSession, settings: Settings):
    total_txt = format_duration(settings.duration_s)

    def progress(tick: int, total: int) -> None:
        elapsed = format_duration(tick * settings.interval_s)
        counts = session.live_counts()
        parts = []
        for t in session.targets:
            done, lost = counts[t.name]
            pct = 100.0 * lost / done if done else 0.0
            parts.append(f"{t.name[:14]} {pct:4.1f}%")
        line = f"  {elapsed:>7}/{total_txt}  loss: " + " | ".join(parts)
        sys.stdout.write("\r" + line[: WIDTH + 30].ljust(WIDTH))
        sys.stdout.flush()

    return progress


def _err(message: str) -> None:
    print(f"netblame: error: {message}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            pass

    args = build_parser().parse_args(argv)
    try:
        settings = apply_overrides(load_settings(args.config), args)
        validate(settings)
    except ConfigError as exc:
        _err(str(exc))
        return EXIT_ERROR

    if args.show_config:
        print(describe_settings(settings))
        return EXIT_OK

    if sys.platform != "win32":
        _err("netblame currently supports Windows only.")
        return EXIT_ERROR
    if shutil.which("ping") is None:
        _err("the 'ping' command was not found on PATH.")
        return EXIT_ERROR

    targets_txt = ", ".join(f"{t.name} ({t.host})" for t in sorted(settings.targets, key=lambda t: t.level))
    print(
        f"netblame {__version__}: measuring for {format_duration(settings.duration_s)} "
        f"(every {settings.interval_s:g} s, timeout {settings.timeout_ms} ms)"
    )
    print(f"  targets: {targets_txt}")
    print("  press Ctrl+C to stop early and analyse what was collected\n")

    session = PingSession(
        targets=settings.targets,
        duration_s=settings.duration_s,
        interval_s=settings.interval_s,
        timeout_ms=settings.timeout_ms,
    )
    show_progress = not args.no_progress and sys.stdout.isatty()
    session.run(_make_progress(session, settings) if show_progress else None)
    if show_progress:
        sys.stdout.write("\r" + " " * WIDTH + "\r")
        sys.stdout.flush()

    if not any(session.samples.values()):
        print("Stopped before any samples were collected.")
        return EXIT_INTERRUPTED

    analysis = analyse(session, settings)
    print(render_report(analysis))

    if args.explain:
        from .explain import ExplainError, explain

        print("\nAsking Claude for an explanation...")
        try:
            text = explain(analysis.to_dict(), settings.llm_model)
        except ExplainError as exc:
            print(f"  (explanation unavailable: {exc})")
        else:
            print("\nExplanation (LLM, based on the analysis above)")
            print("-" * 46)
            for paragraph in text.splitlines():
                print(textwrap.fill(paragraph, width=WIDTH) if paragraph.strip() else "")

    return EXIT_PROBLEM if analysis.blamed else EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
