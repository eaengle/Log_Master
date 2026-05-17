"""
Command-line interface for Log Master.

Configuration is layered: a JSON config file provides the base; any flag
supplied on the command line overrides the corresponding JSON field.

Usage examples:

    logmaster --root /var/log --include ERROR --output-dir ./out
    logmaster --config search.json --include FATAL --workers 4
    logmaster --config search.json --sort timestamp --output-dir /tmp/results
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

from log_master.core.expression_analyzer import SearchConfig
from log_master.core.file_finder import FileFindCriteria
from log_master.core.log_processor import LogProcessor, ProcessorConfig
from log_master.core.output_writer import Column, OutputConfig, OutputMode, SortOrder


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------


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

    # -- File discovery -------------------------------------------------------
    disc = p.add_argument_group("file discovery")
    disc.add_argument(
        "--root", "-r",
        metavar="DIR",
        action="append",
        default=None,
        help="Root directory to search (repeatable)",
    )
    disc.add_argument(
        "--glob", "-g",
        metavar="PATTERN",
        action="append",
        default=None,
        help="File name glob pattern, e.g. '*.log' (repeatable)",
    )
    disc.add_argument(
        "--ext", "-e",
        metavar="EXT",
        action="append",
        default=None,
        help="File extension filter, e.g. '.log' or 'log' (repeatable)",
    )
    disc.add_argument(
        "--depth",
        metavar="N",
        type=int,
        default=None,
        help="Maximum directory depth (default: unlimited)",
    )
    disc.add_argument(
        "--min-size",
        metavar="BYTES",
        type=int,
        default=None,
        help="Minimum file size in bytes",
    )
    disc.add_argument(
        "--max-size",
        metavar="BYTES",
        type=int,
        default=None,
        help="Maximum file size in bytes",
    )
    disc.add_argument(
        "--modified-after",
        metavar="DATE",
        default=None,
        help="Files modified after DATE (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)",
    )
    disc.add_argument(
        "--modified-before",
        metavar="DATE",
        default=None,
        help="Files modified before DATE (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)",
    )
    disc.add_argument(
        "--include-dir",
        metavar="PATTERN",
        action="append",
        default=None,
        help="Only enter directories matching PATTERN (repeatable)",
    )
    disc.add_argument(
        "--exclude-dir",
        metavar="PATTERN",
        action="append",
        default=None,
        help="Skip directories matching PATTERN (repeatable)",
    )

    # -- Search ---------------------------------------------------------------
    srch = p.add_argument_group("search")
    srch.add_argument(
        "--include", "-i",
        metavar="PATTERN",
        action="append",
        default=None,
        help="Include pattern — regex, OR logic (repeatable)",
    )
    srch.add_argument(
        "--exclude", "-x",
        metavar="PATTERN",
        action="append",
        default=None,
        help="Exclude pattern — regex (repeatable)",
    )
    srch.add_argument(
        "--skip-file",
        metavar="PATTERN",
        action="append",
        default=None,
        help="Skip entire file if pattern found anywhere (repeatable)",
    )
    srch.add_argument(
        "--from",
        dest="time_from",
        metavar="DATETIME",
        default=None,
        help="Time range start (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)",
    )
    srch.add_argument(
        "--to",
        dest="time_to",
        metavar="DATETIME",
        default=None,
        help="Time range end (YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS)",
    )
    srch.add_argument(
        "--case-insensitive",
        action="store_true",
        default=None,
        help="Case-insensitive pattern matching",
    )
    srch.add_argument(
        "--context", "-C",
        metavar="N",
        type=int,
        default=None,
        help="Context lines around each match (default: 0)",
    )

    # -- Output ---------------------------------------------------------------
    out = p.add_argument_group("output")
    out.add_argument(
        "--output-dir", "-o",
        metavar="DIR",
        default=None,
        help="Output directory (required)",
    )
    out.add_argument(
        "--mode",
        metavar="MODE",
        action="append",
        default=None,
        choices=["single", "per-pattern", "per-source", "per-parent"],
        help="Output mode: single|per-pattern|per-source|per-parent (repeatable, default: single)",
    )
    out.add_argument(
        "--columns",
        metavar="COLS",
        default=None,
        help="Comma-separated column list: timestamp,source_file,line_no,pattern,text",
    )
    out.add_argument(
        "--sort",
        metavar="ORDER",
        default=None,
        choices=["file-order", "timestamp"],
        help="Sort order: file-order|timestamp (default: file-order)",
    )
    out.add_argument(
        "--no-context",
        action="store_true",
        default=None,
        help="Omit context rows from output",
    )
    out.add_argument(
        "--base-path",
        metavar="PATH",
        default=None,
        help="Write source_file paths relative to this base directory",
    )

    # -- Pipeline -------------------------------------------------------------
    pipe = p.add_argument_group("pipeline")
    pipe.add_argument(
        "--workers", "-w",
        metavar="N",
        type=int,
        default=None,
        help="Worker threads: 0=auto, 1=serial (default), N=explicit",
    )

    return p


# ---------------------------------------------------------------------------
# Config merging
# ---------------------------------------------------------------------------


def _parse_datetime(value: str, field: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise SystemExit(f"error: {field} '{value}' is not YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")


def _resolve(cli_val, json_cfg: dict, key: str, default):
    """Return CLI value if provided, else JSON value, else default."""
    if cli_val is not None:
        return cli_val
    return json_cfg.get(key, default)


def _resolve_list(cli_list, json_cfg: dict, key: str) -> list:
    """Return CLI list if provided (non-None), else JSON list, else []."""
    if cli_list is not None:
        return cli_list
    return json_cfg.get(key, [])


def _build_processor_config(args: argparse.Namespace, json_cfg: dict) -> ProcessorConfig:
    # --- File discovery ---
    roots = _resolve_list(args.root, json_cfg, "root")
    if not roots:
        raise SystemExit("error: at least one --root directory is required")

    modified_after_raw = _resolve(args.modified_after, json_cfg, "modified_after", None)
    modified_before_raw = _resolve(args.modified_before, json_cfg, "modified_before", None)

    find_criteria = FileFindCriteria(
        root_dirs=tuple(Path(r) for r in roots),
        name_globs=tuple(_resolve_list(args.glob, json_cfg, "glob")),
        extensions=tuple(_resolve_list(args.ext, json_cfg, "ext")),
        max_depth=_resolve(args.depth, json_cfg, "depth", None),
        min_size_bytes=_resolve(args.min_size, json_cfg, "min_size", None),
        max_size_bytes=_resolve(args.max_size, json_cfg, "max_size", None),
        modified_after=(
            _parse_datetime(modified_after_raw, "--modified-after")
            if modified_after_raw else None
        ),
        modified_before=(
            _parse_datetime(modified_before_raw, "--modified-before")
            if modified_before_raw else None
        ),
        include_dir_globs=tuple(_resolve_list(args.include_dir, json_cfg, "include_dir")),
        exclude_dir_globs=tuple(_resolve_list(args.exclude_dir, json_cfg, "exclude_dir")),
    )

    # --- Search ---
    time_from_raw = _resolve(args.time_from, json_cfg, "from", None)
    time_to_raw = _resolve(args.time_to, json_cfg, "to", None)
    case_insensitive = _resolve(args.case_insensitive, json_cfg, "case_insensitive", False)

    search_config = SearchConfig(
        include_patterns=tuple(_resolve_list(args.include, json_cfg, "include")),
        exclude_patterns=tuple(_resolve_list(args.exclude, json_cfg, "exclude")),
        skip_file_patterns=tuple(_resolve_list(args.skip_file, json_cfg, "skip_file")),
        time_from=_parse_datetime(time_from_raw, "--from") if time_from_raw else None,
        time_to=_parse_datetime(time_to_raw, "--to") if time_to_raw else None,
        case_sensitive=not case_insensitive,
        context_lines=_resolve(args.context, json_cfg, "context", 0),
    )

    # --- Output ---
    output_dir_raw = _resolve(args.output_dir, json_cfg, "output_dir", None)
    if not output_dir_raw:
        raise SystemExit("error: --output-dir is required")

    modes_raw = _resolve_list(args.mode, json_cfg, "mode") or ["single"]
    mode_map = {
        "single": OutputMode.SINGLE,
        "per-pattern": OutputMode.PER_PATTERN,
        "per-source": OutputMode.PER_SOURCE_FILE,
        "per-parent": OutputMode.PER_PARENT_DIR,
    }
    modes = frozenset(mode_map[m] for m in modes_raw)

    columns_raw = _resolve(args.columns, json_cfg, "columns", None)
    if columns_raw:
        col_map = {c.value: c for c in Column}
        columns = tuple(
            col_map[name.strip()]
            for name in columns_raw.split(",")
            if name.strip() in col_map
        )
    else:
        columns = tuple(Column)

    sort_raw = _resolve(args.sort, json_cfg, "sort", "file-order")
    sort = SortOrder.TIMESTAMP if sort_raw == "timestamp" else SortOrder.FILE_ORDER

    no_context = _resolve(args.no_context, json_cfg, "no_context", False)

    base_path_raw = _resolve(args.base_path, json_cfg, "base_path", None)

    output_config = OutputConfig(
        output_dir=Path(output_dir_raw),
        modes=modes,
        columns=columns,
        sort=sort,
        include_context=not no_context,
        base_path=Path(base_path_raw) if base_path_raw else None,
    )

    workers = _resolve(args.workers, json_cfg, "workers", 1)

    return ProcessorConfig(
        find_criteria=find_criteria,
        search_config=search_config,
        output_config=output_config,
        workers=workers,
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Load JSON config if provided
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
    except SystemExit:
        raise
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
