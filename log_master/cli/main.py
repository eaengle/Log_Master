"""
Command-line interface for Log Master.

Configuration is layered: a JSON config file provides the base; any flag
supplied on the command line overrides the corresponding JSON field.

The JSON config format is the same format saved by the GUI (File > Save Config),
so a config file can be created in either the GUI or a text editor and used by
both tools interchangeably.

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
# Config helpers
# ---------------------------------------------------------------------------


def _parse_datetime(value: str, field: str) -> datetime:
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise SystemExit(f"error: {field} '{value}' is not YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS")


def _csv(s: str) -> list[str]:
    """Split a comma-separated string, filtering blank tokens."""
    return [v.strip() for v in s.split(",") if v.strip()]


def _opt_int(val) -> int | None:
    """Parse an optional int from a string or number; return None for blank/None."""
    if val is None or val == "":
        return None
    try:
        return int(val)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Config builder — merges GUI-format JSON with CLI overrides
# ---------------------------------------------------------------------------


def _build_processor_config(args: argparse.Namespace, json_cfg: dict) -> ProcessorConfig:
    f = json_cfg.get("files", {})
    a = json_cfg.get("analysis", {})
    o = json_cfg.get("output", {})

    # --- File discovery ---
    roots = args.root if args.root is not None else f.get("roots", [])
    if not roots:
        raise SystemExit("error: at least one --root directory is required")

    # globs / extensions / dir-filters are stored in the JSON as comma-separated
    # strings (matching the GUI text fields), so split them here.
    def _json_csv(section: dict, key: str) -> list[str]:
        val = section.get(key, "")
        return _csv(val) if isinstance(val, str) else list(val)

    name_globs      = args.glob       if args.glob       is not None else _json_csv(f, "globs")
    extensions      = args.ext        if args.ext        is not None else _json_csv(f, "extensions")
    include_dirs    = args.include_dir if args.include_dir is not None else _json_csv(f, "include_dirs")
    exclude_dirs    = args.exclude_dir if args.exclude_dir is not None else _json_csv(f, "exclude_dirs")
    max_depth       = args.depth    if args.depth    is not None else _opt_int(f.get("max_depth"))
    min_size        = args.min_size if args.min_size is not None else _opt_int(f.get("min_size"))
    max_size        = args.max_size if args.max_size is not None else _opt_int(f.get("max_size"))

    modified_after_raw  = (args.modified_after  if args.modified_after  is not None
                           else f.get("modified_after",  "").strip() or None)
    modified_before_raw = (args.modified_before if args.modified_before is not None
                           else f.get("modified_before", "").strip() or None)

    find_criteria = FileFindCriteria(
        root_dirs=tuple(Path(r) for r in roots),
        name_globs=tuple(name_globs),
        extensions=tuple(extensions),
        max_depth=max_depth,
        min_size_bytes=min_size,
        max_size_bytes=max_size,
        modified_after=(
            _parse_datetime(modified_after_raw, "--modified-after")
            if modified_after_raw else None
        ),
        modified_before=(
            _parse_datetime(modified_before_raw, "--modified-before")
            if modified_before_raw else None
        ),
        include_dir_globs=tuple(include_dirs),
        exclude_dir_globs=tuple(exclude_dirs),
    )

    # --- Search ---
    include_patterns    = args.include   if args.include   is not None else a.get("include_patterns",   [])
    exclude_patterns    = args.exclude   if args.exclude   is not None else a.get("exclude_patterns",   [])
    skip_file_patterns  = args.skip_file if args.skip_file is not None else a.get("skip_file_patterns", [])

    time_from_raw = (args.time_from if args.time_from is not None
                     else a.get("time_from", "").strip() or None)
    time_to_raw   = (args.time_to   if args.time_to   is not None
                     else a.get("time_to",   "").strip() or None)

    case_insensitive = args.case_insensitive if args.case_insensitive else a.get("case_insensitive", False)
    context_lines    = args.context if args.context is not None else _opt_int(a.get("context_lines", "0")) or 0

    search_config = SearchConfig(
        include_patterns=tuple(include_patterns),
        exclude_patterns=tuple(exclude_patterns),
        skip_file_patterns=tuple(skip_file_patterns),
        time_from=_parse_datetime(time_from_raw, "--from") if time_from_raw else None,
        time_to=_parse_datetime(time_to_raw, "--to")       if time_to_raw   else None,
        case_sensitive=not case_insensitive,
        context_lines=context_lines,
    )

    # --- Output ---
    output_dir_raw = (args.output_dir if args.output_dir is not None
                      else o.get("output_dir", "").strip() or None)
    if not output_dir_raw:
        raise SystemExit("error: --output-dir is required")

    # Modes: CLI --mode list overrides; JSON stores per-mode booleans.
    mode_map = {
        "single":      OutputMode.SINGLE,
        "per-pattern": OutputMode.PER_PATTERN,
        "per-source":  OutputMode.PER_SOURCE_FILE,
        "per-parent":  OutputMode.PER_PARENT_DIR,
    }
    if args.mode is not None:
        modes_list = args.mode
    else:
        modes_list = [
            name for name, flag_key in [
                ("single",      "mode_single"),
                ("per-pattern", "mode_pattern"),
                ("per-source",  "mode_source"),
                ("per-parent",  "mode_parent"),
            ]
            if o.get(flag_key, name == "single")
        ]
    modes = frozenset(mode_map[m] for m in (modes_list or ["single"]))

    # Columns: CLI --columns string overrides; JSON stores a {name: bool} dict.
    col_map = {c.value: c for c in Column}
    if args.columns is not None:
        columns = tuple(
            col_map[name.strip()]
            for name in args.columns.split(",")
            if name.strip() in col_map
        )
    else:
        json_cols = o.get("columns", {})
        columns = tuple(
            col for name, col in col_map.items()
            if json_cols.get(name, True)
        ) if json_cols else tuple(Column)
    if not columns:
        columns = tuple(Column)

    sort_raw = args.sort if args.sort is not None else o.get("sort", "file-order")
    sort = SortOrder.TIMESTAMP if sort_raw == "timestamp" else SortOrder.FILE_ORDER

    include_context = (not args.no_context) if args.no_context else o.get("include_context", True)

    base_path_raw = (args.base_path if args.base_path is not None
                     else o.get("base_path", "").strip() or None)

    output_config = OutputConfig(
        output_dir=Path(output_dir_raw),
        modes=modes,
        columns=columns,
        sort=sort,
        include_context=include_context,
        base_path=Path(base_path_raw) if base_path_raw else None,
    )

    workers = args.workers if args.workers is not None else _opt_int(o.get("workers", "1")) or 1

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
