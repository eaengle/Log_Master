"""Command-line interface for Log Master."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from log_master.core.config import ConfigError, build_processor_config
from log_master.core.log_processor import LogProcessor, ProcessorConfig


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="logmaster",
        description="Search and filter log files, writing results to TSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    p.add_argument(
        "--config", "-c",
        metavar="FILE",
        help="JSON config file (CLI flags override JSON values)",
    )

    disc = p.add_argument_group("file discovery")
    disc.add_argument("--root", "-r", metavar="DIR", action="append", default=None)
    disc.add_argument("--glob", "-g", metavar="PATTERN", action="append", default=None)
    disc.add_argument("--ext", "-e", metavar="EXT", action="append", default=None)
    disc.add_argument("--depth", metavar="N", type=int, default=None)
    disc.add_argument("--min-size", metavar="BYTES", type=int, default=None)
    disc.add_argument("--max-size", metavar="BYTES", type=int, default=None)
    disc.add_argument("--modified-after", metavar="DATE", default=None)
    disc.add_argument("--modified-before", metavar="DATE", default=None)
    disc.add_argument("--include-dir", metavar="PATTERN", action="append", default=None)
    disc.add_argument("--exclude-dir", metavar="PATTERN", action="append", default=None)

    srch = p.add_argument_group("search")
    srch.add_argument("--include", "-i", metavar="PATTERN", action="append", default=None)
    srch.add_argument("--exclude", "-x", metavar="PATTERN", action="append", default=None)
    srch.add_argument("--skip-file", metavar="PATTERN", action="append", default=None)
    srch.add_argument("--from", dest="time_from", metavar="DATETIME", default=None)
    srch.add_argument("--to", dest="time_to", metavar="DATETIME", default=None)
    srch.add_argument("--case-insensitive", action="store_true", default=None)
    srch.add_argument("--context", "-C", metavar="N", type=int, default=None)

    out = p.add_argument_group("output")
    out.add_argument("--output-dir", "-o", metavar="DIR", default=None)
    out.add_argument(
        "--mode",
        metavar="MODE",
        action="append",
        default=None,
        choices=["single", "per-pattern", "per-source", "per-parent"],
    )
    out.add_argument("--columns", metavar="COLS", default=None)
    out.add_argument(
        "--sort",
        metavar="ORDER",
        default=None,
        choices=["file-order", "timestamp"],
    )
    out.add_argument("--no-context", action="store_true", default=None)
    out.add_argument("--base-path", metavar="PATH", default=None)

    pipe = p.add_argument_group("pipeline")
    pipe.add_argument("--workers", "-w", metavar="N", type=int, default=None)

    return p


def _build_processor_config(args: argparse.Namespace, json_cfg: dict) -> ProcessorConfig:
    """Compatibility wrapper for tests and callers that imported this helper."""
    return build_processor_config(json_cfg, overrides=args)


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    json_cfg: dict = {}
    if args.config:
        config_path = Path(args.config)
        if not config_path.exists():
            raise SystemExit(f"error: config file not found: {args.config}")
        try:
            json_cfg = json.loads(config_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SystemExit(f"error: invalid JSON in {args.config}: {exc}") from exc

    try:
        cfg = _build_processor_config(args, json_cfg)
    except ConfigError as exc:
        raise SystemExit(f"error: {exc}") from exc
    except Exception as exc:
        raise SystemExit(f"error: {exc}") from exc

    result = LogProcessor(cfg).run()

    print(
        f"Files found: {result.files_found}  |  "
        f"Analyzed: {result.files_analyzed}  |  "
        f"Skipped: {result.files_skipped}  |  "
        f"Matches: {result.matches_total}"
    )
    print(f"Output: {cfg.output_config.output_dir}")


if __name__ == "__main__":
    main()
