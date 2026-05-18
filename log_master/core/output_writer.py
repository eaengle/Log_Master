"""
TSV output routing, formatting, and sorting.

Results are fanned out to one or more destination files determined by the
active OutputMode(s).  Multiple modes can be active simultaneously — a single
MatchResult may be written to several files at once.

For per-pattern mode a MatchResult that matched N include patterns produces N
rows (one per pattern, each in its own destination file).  All other modes
produce one row per MatchResult with the matched patterns joined by " | ".

Context lines (from MatchResult.context_before/after) appear as additional
rows immediately surrounding the match row.  They carry the source_file and
estimated line_no but have empty timestamp and pattern columns.

source_file formatting:
  Every file is written relative to the root_dir it was discovered under.
  path_depth controls how many parent folders appear alongside the filename:
    None  — full relative path (e.g. "nginx/web/access.log")
    0     — filename only   (e.g. "access.log")
    1     — one parent      (e.g. "web/access.log")
    N     — N parents       (clamped to however many exist)

Sort order:
  file-order  — rows written incrementally as add_result() is called.
  timestamp   — all results buffered, sorted by timestamp on flush().

Usage as a context manager ensures files are always closed:

    with OutputWriter(config) as writer:
        for result in results:
            writer.add_result(result)
    # files closed, sorted output written here
"""

from __future__ import annotations

import csv
import re as _re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import IO

from .expression_analyzer import MatchResult
from .file_finder import FileInfo


@dataclass(frozen=True)
class _BufferedRow:
    """One concrete output row waiting for timestamp-sorted flush."""

    timestamp: datetime
    sequence: int
    dest: Path
    text: str
    fmt_source: str   # pre-formatted source_file string
    line_no: int
    timestamp_str: str
    pattern_str: str


# ---------------------------------------------------------------------------
# Enums and config
# ---------------------------------------------------------------------------


class OutputMode(str, Enum):
    SINGLE = "single"
    PER_PATTERN = "per-pattern"
    PER_SOURCE_FILE = "per-source"
    PER_PARENT_DIR = "per-parent"


class Column(str, Enum):
    TIMESTAMP = "timestamp"
    SOURCE_FILE = "source_file"
    LINE_NO = "line_no"
    PATTERN = "pattern"
    TEXT = "text"


class SortOrder(str, Enum):
    FILE_ORDER = "file-order"
    TIMESTAMP = "timestamp"


DEFAULT_COLUMNS: tuple[Column, ...] = (
    Column.TIMESTAMP,
    Column.SOURCE_FILE,
    Column.LINE_NO,
    Column.PATTERN,
    Column.TEXT,
)


@dataclass(frozen=True)
class OutputConfig:
    """
    Immutable configuration for an OutputWriter.

    output_dir  : directory where all output TSV files are created.
    modes       : one or more routing modes (all active simultaneously).
    columns     : which columns to include and their order.
    sort        : file-order (incremental) or timestamp (buffered sort).
    include_context : write context lines as additional rows around each match.
    path_depth  : parent folders to include alongside the filename in
                  source_file.  None = full path relative to root,
                  0 = filename only, 1 = one parent + filename, etc.
    """

    output_dir: Path
    modes: frozenset[OutputMode] = field(
        default_factory=lambda: frozenset({OutputMode.SINGLE})
    )
    columns: tuple[Column, ...] = DEFAULT_COLUMNS
    sort: SortOrder = SortOrder.FILE_ORDER
    include_context: bool = True
    path_depth: int | None = None
    root_dirs: tuple[Path, ...] = ()
    include_patterns: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# Writer
# ---------------------------------------------------------------------------


class OutputWriter:
    """
    Accepts MatchResult objects and routes them to TSV files.

    Use as a context manager — __exit__ calls flush() automatically.
    """

    def __init__(self, config: OutputConfig) -> None:
        self._config = config
        config.output_dir.mkdir(parents=True, exist_ok=True)

        self._handles: dict[Path, IO[str]] = {}
        self._writers: dict[Path, csv.writer] = {}

        # Timestamp sort: buffer all results and sort on flush
        self._buffer: list[MatchResult] | None = (
            [] if config.sort == SortOrder.TIMESTAMP else None
        )
        self._row_sequence = 0

        # Collision avoidance for per-source-file filenames
        self._stem_counts: dict[str, int] = defaultdict(int)
        self._source_to_dest: dict[Path, Path] = {}

    # ------------------------------------------------------------------
    # Context-manager support
    # ------------------------------------------------------------------

    def __enter__(self) -> OutputWriter:
        return self

    def __exit__(self, *_) -> None:
        self.flush()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def prepare(self, files: list[FileInfo]) -> None:
        """
        Pre-compute per-source destination paths using a global uniqueness algorithm.

        Must be called before add_result() when PER_SOURCE_FILE mode is active.
        The algorithm progressively adds parent directory components to each
        candidate name until all names are unique.  Files that still conflict
        after exhausting their relative path parts (identical relative paths from
        different roots) receive a ``root_{N}_`` prefix using the deterministic
        index from OutputConfig.root_dirs.
        """
        if OutputMode.PER_SOURCE_FILE not in self._config.modes:
            return

        # Build root index: config-supplied list is authoritative; fall back to
        # the order roots are first seen in the files list.
        root_dirs = self._config.root_dirs
        if not root_dirs:
            seen_set: set[Path] = set()
            seen_list: list[Path] = []
            for fi in files:
                if fi.root not in seen_set:
                    seen_set.add(fi.root)
                    seen_list.append(fi.root)
            root_dirs = tuple(seen_list)
        root_index: dict[Path, int] = {r: i for i, r in enumerate(root_dirs)}

        # Deduplicate by path (a file may appear multiple times if globs overlap)
        seen_paths: set[Path] = set()
        unique_fis: list[FileInfo] = []
        for fi in files:
            if fi.path not in seen_paths:
                seen_paths.add(fi.path)
                unique_fis.append(fi)

        if not unique_fis:
            return

        fi_map: dict[Path, FileInfo] = {fi.path: fi for fi in unique_fis}

        # Relative path parts for each file (e.g. ["subdir", "app.log"])
        rel_parts: dict[Path, list[str]] = {}
        for fi in unique_fis:
            try:
                parts = list(fi.path.relative_to(fi.root).parts)
            except ValueError:
                parts = [fi.path.name]
            rel_parts[fi.path] = parts

        def _candidate(path: Path, depth: int) -> str | None:
            """Candidate name using ``depth`` parent dirs + stem. None if exhausted."""
            parts = rel_parts[path]
            n = len(parts)
            if depth >= n:
                return None
            taken = parts[n - 1 - depth:]
            stem = Path(taken[-1]).stem
            return "_".join([*taken[:-1], stem]) if len(taken) > 1 else stem

        # Start every file at depth 0 (stem only) and widen until no conflicts
        depths: dict[Path, int] = {fi.path: 0 for fi in unique_fis}
        changed = True
        while changed:
            changed = False
            name_to_paths: dict[str, list[Path]] = defaultdict(list)
            for path, depth in depths.items():
                name = _candidate(path, depth)
                if name is not None:
                    name_to_paths[name].append(path)

            for paths in name_to_paths.values():
                if len(paths) > 1:
                    for path in paths:
                        if _candidate(path, depths[path] + 1) is not None:
                            depths[path] += 1
                            changed = True

        # Collect final names; files still sharing a name get a root-index prefix
        final_name: dict[str, list[Path]] = defaultdict(list)
        for path, depth in depths.items():
            name = _candidate(path, depth) or Path(rel_parts[path][-1]).stem
            final_name[name].append(path)

        assignments: dict[Path, str] = {}
        for name, paths in final_name.items():
            if len(paths) == 1:
                assignments[paths[0]] = name
            else:
                for path in paths:
                    root_idx = root_index.get(fi_map[path].root, 0)
                    assignments[path] = f"root_{root_idx}_{name}"

        for path, name in assignments.items():
            self._source_to_dest[path] = self._config.output_dir / f"{name}.tsv"

    def add_result(self, result: MatchResult) -> None:
        """
        Accept one MatchResult.  In file-order mode the result is written
        immediately; in timestamp mode it is buffered until flush().
        """
        if self._buffer is not None:
            self._buffer.append(result)
        else:
            self._route(result)

    def flush(self) -> None:
        """
        Finalise output.  For timestamp sort: sort the buffer and write.
        Always closes all open file handles.
        """
        if self._buffer is not None:
            rows: list[_BufferedRow] = []
            for result in self._buffer:
                rows.extend(self._rows_for_result(result))
            for row in sorted(rows, key=lambda r: (r.timestamp, r.sequence)):
                writer = self._get_writer(row.dest)
                self._write_row(
                    writer,
                    row.text,
                    row.fmt_source,
                    row.line_no,
                    timestamp_str=row.timestamp_str,
                    pattern_str=row.pattern_str,
                )
        for fh in self._handles.values():
            fh.close()
        self._handles.clear()
        self._writers.clear()

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def _route(self, result: MatchResult) -> None:
        """Fan the result out to every active mode's destination(s)."""
        for mode in self._config.modes:
            if mode == OutputMode.PER_PATTERN:
                # One file per matched pattern (fan-out)
                patterns = sorted(result.matched_patterns) if result.matched_patterns else ["_all"]
                for pat in patterns:
                    self._emit(self._dest_per_pattern(pat), result, pattern_col=pat)
            elif mode == OutputMode.SINGLE:
                self._emit(
                    self._config.output_dir / "results.tsv",
                    result,
                    pattern_col=" | ".join(sorted(result.matched_patterns)),
                )
            elif mode == OutputMode.PER_SOURCE_FILE:
                self._emit(
                    self._dest_per_source(result.source_file),
                    result,
                    pattern_col=" | ".join(sorted(result.matched_patterns)),
                )
            elif mode == OutputMode.PER_PARENT_DIR:
                self._emit(
                    self._dest_per_parent(result.source_file),
                    result,
                    pattern_col=" | ".join(sorted(result.matched_patterns)),
                )

    def _rows_for_result(self, result: MatchResult) -> list[_BufferedRow]:
        """Return concrete destination rows for timestamp-sorted output."""
        rows: list[_BufferedRow] = []
        for mode in self._config.modes:
            if mode == OutputMode.PER_PATTERN:
                patterns = sorted(result.matched_patterns) if result.matched_patterns else ["_all"]
                for pat in patterns:
                    rows.extend(
                        self._make_rows(self._dest_per_pattern(pat), result, pat)
                    )
            elif mode == OutputMode.SINGLE:
                rows.extend(
                    self._make_rows(
                        self._config.output_dir / "results.tsv",
                        result,
                        " | ".join(sorted(result.matched_patterns)),
                    )
                )
            elif mode == OutputMode.PER_SOURCE_FILE:
                rows.extend(
                    self._make_rows(
                        self._dest_per_source(result.source_file),
                        result,
                        " | ".join(sorted(result.matched_patterns)),
                    )
                )
            elif mode == OutputMode.PER_PARENT_DIR:
                rows.extend(
                    self._make_rows(
                        self._dest_per_parent(result.source_file),
                        result,
                        " | ".join(sorted(result.matched_patterns)),
                    )
                )
        return rows

    def _make_rows(
        self,
        dest: Path,
        result: MatchResult,
        pattern_col: str,
    ) -> list[_BufferedRow]:
        """Build buffered rows for one destination."""
        rows: list[_BufferedRow] = []
        cfg = self._config
        fmt_source = self._fmt_source(result.source_file, result.root)

        if cfg.include_context:
            for ctx in result.context_before:
                rows.append(self._buffered_row(dest, fmt_source, ctx.line_no,
                                               ctx.timestamp, ctx.text, ""))

        rows.append(self._buffered_row(dest, fmt_source, result.line_no,
                                       result.timestamp, result.text, pattern_col))

        if cfg.include_context:
            for ctx in result.context_after:
                rows.append(self._buffered_row(dest, fmt_source, ctx.line_no,
                                               ctx.timestamp, ctx.text, ""))

        return rows

    def _buffered_row(
        self,
        dest: Path,
        fmt_source: str,
        line_no: int,
        timestamp: datetime,
        text: str,
        pattern_str: str,
    ) -> _BufferedRow:
        row = _BufferedRow(
            timestamp=timestamp,
            sequence=self._row_sequence,
            dest=dest,
            text=text,
            fmt_source=fmt_source,
            line_no=line_no,
            timestamp_str=timestamp.isoformat(timespec="milliseconds"),
            pattern_str=pattern_str,
        )
        self._row_sequence += 1
        return row

    def _emit(self, dest: Path, result: MatchResult, pattern_col: str) -> None:
        """Write context_before, the match row, and context_after to *dest*."""
        writer = self._get_writer(dest)
        cfg = self._config
        fmt_source = self._fmt_source(result.source_file, result.root)

        if cfg.include_context:
            for ctx in result.context_before:
                self._write_row(
                    writer, ctx.text, fmt_source, ctx.line_no,
                    timestamp_str=ctx.timestamp.isoformat(timespec="milliseconds"),
                    pattern_str="",
                )

        ts_str = result.timestamp.isoformat(timespec="milliseconds")
        self._write_row(
            writer, result.text, fmt_source,
            result.line_no, timestamp_str=ts_str, pattern_str=pattern_col,
        )

        if cfg.include_context:
            for ctx in result.context_after:
                self._write_row(
                    writer, ctx.text, fmt_source, ctx.line_no,
                    timestamp_str=ctx.timestamp.isoformat(timespec="milliseconds"),
                    pattern_str="",
                )

    def _write_row(
        self,
        writer: csv.writer,
        text: str,
        fmt_source: str,
        line_no: int,
        timestamp_str: str,
        pattern_str: str,
    ) -> None:
        row: list[str] = []
        for col in self._config.columns:
            if col == Column.TIMESTAMP:
                row.append(timestamp_str)
            elif col == Column.SOURCE_FILE:
                row.append(fmt_source)
            elif col == Column.LINE_NO:
                row.append(str(line_no))
            elif col == Column.PATTERN:
                row.append(pattern_str)
            elif col == Column.TEXT:
                row.append(text)
        writer.writerow(row)

    # ------------------------------------------------------------------
    # Destination path helpers
    # ------------------------------------------------------------------

    def _dest_per_pattern(self, pattern: str) -> Path:
        safe = _re.sub(r"[^\w\-]", "_", pattern)[:32]
        patterns = self._config.include_patterns
        if patterns:
            try:
                idx = patterns.index(pattern)
                return self._config.output_dir / f"pattern_{idx}_{safe}.tsv"
            except ValueError:
                pass  # sentinel like "_all" is not in the configured list
        return self._config.output_dir / f"pattern_{safe}.tsv"

    def _dest_per_source(self, source: Path) -> Path:
        if source in self._source_to_dest:
            return self._source_to_dest[source]
        stem = source.stem
        count = self._stem_counts[stem]
        self._stem_counts[stem] += 1
        suffix = f"_{count}" if count > 0 else ""
        dest = self._config.output_dir / f"{stem}{suffix}.tsv"
        self._source_to_dest[source] = dest
        return dest

    def _dest_per_parent(self, source: Path) -> Path:
        name = source.parent.name or "root"
        return self._config.output_dir / f"{name}.tsv"

    def _fmt_source(self, source: Path, root: Path) -> str:
        """
        Format *source* relative to *root*, then trim to path_depth parent
        folders.  Falls back to the absolute path if relativisation fails.
        """
        try:
            rel = source.relative_to(root)
        except ValueError:
            return str(source)

        depth = self._config.path_depth
        if depth is None:
            return str(rel)

        parts = rel.parts
        keep = depth + 1  # depth parent folders + the filename itself
        if len(parts) > keep:
            return str(Path(*parts[-keep:]))
        return str(rel)

    # ------------------------------------------------------------------
    # File-handle management
    # ------------------------------------------------------------------

    def _get_writer(self, dest: Path) -> csv.writer:
        if dest not in self._handles:
            fh = open(dest, "w", encoding="utf-8", newline="")
            self._handles[dest] = fh
            w = csv.writer(fh, delimiter="\t")
            self._writers[dest] = w
            w.writerow([col.value for col in self._config.columns])
        return self._writers[dest]
