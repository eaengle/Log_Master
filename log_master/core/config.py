"""Shared config parsing for CLI, GUI, and JSON state files."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from .expression_analyzer import SearchConfig
from .file_finder import FileFindCriteria
from .log_processor import ProcessorConfig
from .output_writer import Column, OutputConfig, OutputMode, SortOrder


class ConfigError(ValueError):
    """Raised when user-facing configuration cannot be parsed."""


def parse_datetime(value: str, field: str) -> datetime:
    """Parse supported date/datetime strings."""
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            pass
    raise ConfigError(
        f"{field}: expected YYYY-MM-DD or YYYY-MM-DDTHH:MM[:SS], got '{value}'"
    )


def csv_list(value: Any) -> list[str]:
    """Return a list from a comma-separated string or list-like value."""
    if value is None:
        return []
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value if str(v).strip()]


def optional_int(value: Any, field: str) -> int | None:
    """Parse an optional integer from a string or number."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"{field}: expected integer, got '{value}'") from exc


def _override(overrides: Any | None, name: str, default: Any = None) -> Any:
    return getattr(overrides, name, default) if overrides is not None else default


def build_processor_config(
    state: dict[str, Any],
    *,
    overrides: Any | None = None,
) -> ProcessorConfig:
    """
    Build a ProcessorConfig from GUI/JSON state plus optional CLI-style overrides.

    ``state`` uses the persisted GUI JSON shape: top-level ``files``, ``analysis``,
    and ``output`` sections. ``overrides`` may be an argparse.Namespace with the
    CLI option names used by ``log_master.cli.main``.
    """
    files = state.get("files", {})
    analysis = state.get("analysis", {})
    output = state.get("output", {})

    root_override = _override(overrides, "root")
    roots = root_override if root_override is not None else files.get("roots", [])
    if not roots:
        raise ConfigError("at least one root directory is required")

    _v = _override(overrides, "glob")
    name_globs = _v if _v is not None else csv_list(files.get("globs", ""))

    _v = _override(overrides, "ext")
    extensions = _v if _v is not None else csv_list(files.get("extensions", ""))

    _v = _override(overrides, "include_dir")
    include_dirs = _v if _v is not None else csv_list(files.get("include_dirs", ""))

    _v = _override(overrides, "exclude_dir")
    exclude_dirs = _v if _v is not None else csv_list(files.get("exclude_dirs", ""))

    _v = _override(overrides, "depth")
    max_depth = _v if _v is not None else optional_int(files.get("max_depth"), "Max depth")

    _v = _override(overrides, "min_size")
    min_size = _v if _v is not None else optional_int(files.get("min_size"), "Min size")

    _v = _override(overrides, "max_size")
    max_size = _v if _v is not None else optional_int(files.get("max_size"), "Max size")

    _v = _override(overrides, "modified_after")
    modified_after_raw = _v if _v is not None else str(files.get("modified_after", "")).strip() or None

    _v = _override(overrides, "modified_before")
    modified_before_raw = _v if _v is not None else str(files.get("modified_before", "")).strip() or None

    find_criteria = FileFindCriteria(
        root_dirs=tuple(Path(r) for r in roots),
        name_globs=tuple(name_globs),
        extensions=tuple(extensions),
        max_depth=max_depth,
        min_size_bytes=min_size,
        max_size_bytes=max_size,
        modified_after=(
            parse_datetime(modified_after_raw, "Modified after")
            if modified_after_raw else None
        ),
        modified_before=(
            parse_datetime(modified_before_raw, "Modified before")
            if modified_before_raw else None
        ),
        include_dir_globs=tuple(include_dirs),
        exclude_dir_globs=tuple(exclude_dirs),
    )

    _v = _override(overrides, "include")
    include_patterns = _v if _v is not None else analysis.get("include_patterns", [])

    _v = _override(overrides, "exclude")
    exclude_patterns = _v if _v is not None else analysis.get("exclude_patterns", [])

    _v = _override(overrides, "skip_file")
    skip_file_patterns = _v if _v is not None else analysis.get("skip_file_patterns", [])

    _v = _override(overrides, "time_from")
    time_from_raw = _v if _v is not None else str(analysis.get("time_from", "")).strip() or None

    _v = _override(overrides, "time_to")
    time_to_raw = _v if _v is not None else str(analysis.get("time_to", "")).strip() or None

    _v = _override(overrides, "case_insensitive")
    case_insensitive = _v if _v is not None else analysis.get("case_insensitive", False)

    _v = _override(overrides, "context")
    context_lines = _v if _v is not None else optional_int(analysis.get("context_lines", "0"), "Context lines") or 0

    search_config = SearchConfig(
        include_patterns=tuple(include_patterns),
        exclude_patterns=tuple(exclude_patterns),
        skip_file_patterns=tuple(skip_file_patterns),
        time_from=parse_datetime(time_from_raw, "Time from") if time_from_raw else None,
        time_to=parse_datetime(time_to_raw, "Time to") if time_to_raw else None,
        case_sensitive=not case_insensitive,
        context_lines=context_lines,
    )

    _v = _override(overrides, "output_dir")
    output_dir_raw = _v if _v is not None else str(output.get("output_dir", "")).strip() or None
    if not output_dir_raw:
        raise ConfigError("output directory is required")

    mode_map = {
        "single": OutputMode.SINGLE,
        "per-pattern": OutputMode.PER_PATTERN,
        "per-source": OutputMode.PER_SOURCE_FILE,
        "per-parent": OutputMode.PER_PARENT_DIR,
    }
    mode_override = _override(overrides, "mode")
    if mode_override is not None:
        modes_list = mode_override
    else:
        modes_list = [
            name for name, key in [
                ("single", "mode_single"),
                ("per-pattern", "mode_pattern"),
                ("per-source", "mode_source"),
                ("per-parent", "mode_parent"),
            ]
            if output.get(key, name == "single")
        ]
    modes = frozenset(mode_map[m] for m in (modes_list or ["single"]))

    col_map = {c.value: c for c in Column}
    columns_override = _override(overrides, "columns")
    if columns_override is not None:
        columns = tuple(
            col_map[name.strip()]
            for name in str(columns_override).split(",")
            if name.strip() in col_map
        )
    else:
        json_cols = output.get("columns", {})
        columns = tuple(
            col for name, col in col_map.items()
            if json_cols.get(name, True)
        ) if json_cols else tuple(Column)
    if not columns:
        columns = tuple(Column)

    _v = _override(overrides, "sort")
    sort_raw = _v if _v is not None else output.get("sort", "file-order")
    sort = SortOrder.TIMESTAMP if sort_raw == "timestamp" else SortOrder.FILE_ORDER

    no_context = _override(overrides, "no_context")
    include_context = False if no_context else output.get("include_context", True)

    _v = _override(overrides, "path_depth")
    path_depth = _v if _v is not None else optional_int(output.get("path_depth"), "Path depth")

    output_config = OutputConfig(
        output_dir=Path(output_dir_raw),
        modes=modes,
        columns=columns,
        sort=sort,
        include_context=include_context,
        path_depth=path_depth,
        root_dirs=tuple(find_criteria.root_dirs),
        include_patterns=search_config.include_patterns,
    )

    _v = _override(overrides, "workers")
    workers = _v if _v is not None else optional_int(output.get("workers", "1"), "Workers") or 1

    return ProcessorConfig(
        find_criteria=find_criteria,
        search_config=search_config,
        output_config=output_config,
        workers=workers,
    )
