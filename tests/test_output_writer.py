"""Tests for OutputWriter — TSV routing, formatting, sorting, and context."""

from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from log_master.core.expression_analyzer import MatchResult
from log_master.core.timestamp_resolver import ParsedLine
from log_master.core.output_writer import (
    Column,
    DEFAULT_COLUMNS,
    OutputConfig,
    OutputMode,
    OutputWriter,
    SortOrder,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_result(
    source: Path,
    root: Path | None = None,
    line_no: int = 1,
    timestamp: datetime = datetime(2024, 3, 15, 8, 0, 0),
    text: str = "INFO message",
    patterns: set[str] | None = None,
    before: tuple[str, ...] = (),
    after: tuple[str, ...] = (),
) -> MatchResult:
    ctx_before = tuple(
        ParsedLine(
            line_no=line_no - len(before) + i,
            timestamp=timestamp - timedelta(milliseconds=(len(before) - i) * 100),
            text=t,
        )
        for i, t in enumerate(before)
    )
    ctx_after = tuple(
        ParsedLine(
            line_no=line_no + 1 + i,
            timestamp=timestamp + timedelta(milliseconds=(i + 1) * 100),
            text=t,
        )
        for i, t in enumerate(after)
    )
    return MatchResult(
        source_file=source,
        root=root if root is not None else source.parent,
        line_no=line_no,
        timestamp=timestamp,
        text=text,
        matched_patterns=frozenset(patterns or set()),
        context_before=ctx_before,
        context_after=ctx_after,
    )


def read_tsv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def run_writer(
    results: list[MatchResult],
    output_dir: Path,
    **kwargs,
) -> OutputConfig:
    cfg = OutputConfig(output_dir=output_dir, **kwargs)
    with OutputWriter(cfg) as w:
        for r in results:
            w.add_result(r)
    return cfg


# ---------------------------------------------------------------------------
# Single mode
# ---------------------------------------------------------------------------


class TestSingleMode:

    def test_creates_results_tsv(self, tmp_path):
        src = tmp_path / "app.log"
        run_writer([make_result(src)], tmp_path / "out")
        assert (tmp_path / "out" / "results.tsv").exists()

    def test_header_row_present(self, tmp_path):
        src = tmp_path / "app.log"
        run_writer([make_result(src)], tmp_path / "out")
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert rows[0].keys() == {c.value for c in DEFAULT_COLUMNS}

    def test_match_row_content(self, tmp_path):
        src = tmp_path / "app.log"
        ts = datetime(2024, 3, 15, 8, 0, 3)
        result = make_result(src, line_no=4, timestamp=ts, text="ERROR timeout",
                             patterns={"ERROR"})
        run_writer([result], tmp_path / "out")
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert len(rows) == 1
        assert rows[0]["text"] == "ERROR timeout"
        assert rows[0]["line_no"] == "4"
        assert rows[0]["pattern"] == "ERROR"
        assert "2024-03-15T08:00:03" in rows[0]["timestamp"]

    def test_multiple_results_all_written(self, tmp_path):
        src = tmp_path / "app.log"
        results = [make_result(src, line_no=i, text=f"line {i}") for i in range(1, 6)]
        run_writer(results, tmp_path / "out")
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert len(rows) == 5

    def test_multiple_patterns_joined_with_pipe(self, tmp_path):
        src = tmp_path / "app.log"
        result = make_result(src, patterns={"ERROR", "FATAL"})
        run_writer([result], tmp_path / "out")
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        pat = rows[0]["pattern"]
        assert "ERROR" in pat
        assert "FATAL" in pat
        assert "|" in pat

    def test_no_patterns_empty_pattern_column(self, tmp_path):
        src = tmp_path / "app.log"
        result = make_result(src, patterns=set())
        run_writer([result], tmp_path / "out")
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert rows[0]["pattern"] == ""

    def test_empty_results_creates_header_only(self, tmp_path):
        run_writer([], tmp_path / "out", modes=frozenset({OutputMode.SINGLE}))
        # No file created if no results (lazy file creation)
        assert not (tmp_path / "out" / "results.tsv").exists()


# ---------------------------------------------------------------------------
# Per-pattern mode
# ---------------------------------------------------------------------------


class TestPerPatternMode:

    def test_creates_one_file_per_pattern(self, tmp_path):
        src = tmp_path / "app.log"
        results = [
            make_result(src, text="ERROR bad thing", patterns={"ERROR"}),
            make_result(src, text="WARN slow query", patterns={"WARN"}),
        ]
        run_writer(results, tmp_path / "out",
                   modes=frozenset({OutputMode.PER_PATTERN}))
        out = tmp_path / "out"
        assert (out / "pattern_ERROR.tsv").exists()
        assert (out / "pattern_WARN.tsv").exists()

    def test_fan_out_to_multiple_files(self, tmp_path):
        src = tmp_path / "app.log"
        # Line matches both ERROR and FATAL
        result = make_result(src, text="FATAL ERROR crash", patterns={"ERROR", "FATAL"})
        run_writer([result], tmp_path / "out",
                   modes=frozenset({OutputMode.PER_PATTERN}))
        out = tmp_path / "out"
        assert (out / "pattern_ERROR.tsv").exists()
        assert (out / "pattern_FATAL.tsv").exists()
        # Each file has one data row
        assert len(read_tsv(out / "pattern_ERROR.tsv")) == 1
        assert len(read_tsv(out / "pattern_FATAL.tsv")) == 1

    def test_pattern_column_contains_specific_pattern(self, tmp_path):
        src = tmp_path / "app.log"
        result = make_result(src, text="ERROR and FATAL", patterns={"ERROR", "FATAL"})
        run_writer([result], tmp_path / "out",
                   modes=frozenset({OutputMode.PER_PATTERN}))
        out = tmp_path / "out"
        err_rows = read_tsv(out / "pattern_ERROR.tsv")
        fat_rows = read_tsv(out / "pattern_FATAL.tsv")
        assert err_rows[0]["pattern"] == "ERROR"
        assert fat_rows[0]["pattern"] == "FATAL"

    def test_no_patterns_falls_back_to_all_file(self, tmp_path):
        src = tmp_path / "app.log"
        result = make_result(src, patterns=set())
        run_writer([result], tmp_path / "out",
                   modes=frozenset({OutputMode.PER_PATTERN}))
        assert (tmp_path / "out" / "pattern__all.tsv").exists()

    def test_regex_chars_sanitized_in_filename(self, tmp_path):
        src = tmp_path / "app.log"
        result = make_result(src, patterns={"^ERROR.*$"})
        run_writer([result], tmp_path / "out",
                   modes=frozenset({OutputMode.PER_PATTERN}))
        files = list((tmp_path / "out").glob("pattern_*.tsv"))
        assert len(files) == 1
        # Filename must not contain regex special chars
        assert "^" not in files[0].name
        assert "*" not in files[0].name


# ---------------------------------------------------------------------------
# Per-source-file mode
# ---------------------------------------------------------------------------


class TestPerSourceFileMode:

    def test_creates_one_file_per_source(self, tmp_path):
        src_a = tmp_path / "a.log"
        src_b = tmp_path / "b.log"
        results = [
            make_result(src_a, text="from a"),
            make_result(src_b, text="from b"),
        ]
        run_writer(results, tmp_path / "out",
                   modes=frozenset({OutputMode.PER_SOURCE_FILE}))
        out = tmp_path / "out"
        assert (out / "a.tsv").exists()
        assert (out / "b.tsv").exists()

    def test_results_routed_to_correct_file(self, tmp_path):
        src_a = tmp_path / "a.log"
        src_b = tmp_path / "b.log"
        results = [
            make_result(src_a, text="msg from a"),
            make_result(src_b, text="msg from b"),
        ]
        run_writer(results, tmp_path / "out",
                   modes=frozenset({OutputMode.PER_SOURCE_FILE}))
        a_rows = read_tsv(tmp_path / "out" / "a.tsv")
        b_rows = read_tsv(tmp_path / "out" / "b.tsv")
        assert a_rows[0]["text"] == "msg from a"
        assert b_rows[0]["text"] == "msg from b"

    def test_same_stem_collision_gets_suffix(self, tmp_path):
        src_a = tmp_path / "logs" / "app.log"
        src_b = tmp_path / "archive" / "app.log"
        results = [make_result(src_a), make_result(src_b)]
        run_writer(results, tmp_path / "out",
                   modes=frozenset({OutputMode.PER_SOURCE_FILE}))
        out = tmp_path / "out"
        assert (out / "app.tsv").exists()
        assert (out / "app_1.tsv").exists()


# ---------------------------------------------------------------------------
# Per-parent-dir mode
# ---------------------------------------------------------------------------


class TestPerParentDirMode:

    def test_creates_one_file_per_parent(self, tmp_path):
        src_app = tmp_path / "app" / "server.log"
        src_sys = tmp_path / "system" / "syslog.log"
        results = [make_result(src_app), make_result(src_sys)]
        run_writer(results, tmp_path / "out",
                   modes=frozenset({OutputMode.PER_PARENT_DIR}))
        out = tmp_path / "out"
        assert (out / "app.tsv").exists()
        assert (out / "system.tsv").exists()

    def test_same_parent_combined_into_one_file(self, tmp_path):
        parent = tmp_path / "logs"
        src_a = parent / "a.log"
        src_b = parent / "b.log"
        results = [make_result(src_a, text="from a"), make_result(src_b, text="from b")]
        run_writer(results, tmp_path / "out",
                   modes=frozenset({OutputMode.PER_PARENT_DIR}))
        rows = read_tsv(tmp_path / "out" / "logs.tsv")
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# Combined modes
# ---------------------------------------------------------------------------


class TestCombinedModes:

    def test_single_and_per_pattern_both_created(self, tmp_path):
        src = tmp_path / "app.log"
        result = make_result(src, text="ERROR bad", patterns={"ERROR"})
        run_writer([result], tmp_path / "out",
                   modes=frozenset({OutputMode.SINGLE, OutputMode.PER_PATTERN}))
        out = tmp_path / "out"
        assert (out / "results.tsv").exists()
        assert (out / "pattern_ERROR.tsv").exists()

    def test_single_and_per_source_both_created(self, tmp_path):
        src = tmp_path / "app.log"
        result = make_result(src)
        run_writer([result], tmp_path / "out",
                   modes=frozenset({OutputMode.SINGLE, OutputMode.PER_SOURCE_FILE}))
        out = tmp_path / "out"
        assert (out / "results.tsv").exists()
        assert (out / "app.tsv").exists()


# ---------------------------------------------------------------------------
# Column selection
# ---------------------------------------------------------------------------


class TestColumnSelection:

    def test_default_columns_all_present(self, tmp_path):
        src = tmp_path / "app.log"
        run_writer([make_result(src)], tmp_path / "out")
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert set(rows[0].keys()) == {c.value for c in DEFAULT_COLUMNS}

    def test_text_only_column(self, tmp_path):
        src = tmp_path / "app.log"
        run_writer(
            [make_result(src, text="hello")],
            tmp_path / "out",
            columns=(Column.TEXT,),
        )
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert list(rows[0].keys()) == ["text"]
        assert rows[0]["text"] == "hello"

    def test_custom_column_order(self, tmp_path):
        src = tmp_path / "app.log"
        cols = (Column.LINE_NO, Column.TEXT, Column.TIMESTAMP)
        run_writer([make_result(src)], tmp_path / "out", columns=cols)
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert list(rows[0].keys()) == ["line_no", "text", "timestamp"]

    def test_source_file_relative_to_root_by_default(self, tmp_path):
        src = tmp_path / "app.log"
        # root=tmp_path → relative path is just "app.log"
        run_writer(
            [make_result(src, root=tmp_path)],
            tmp_path / "out",
            columns=(Column.SOURCE_FILE,),
        )
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert rows[0]["source_file"] == "app.log"

    def test_source_file_path_depth_zero_filename_only(self, tmp_path):
        src = tmp_path / "logs" / "app.log"
        run_writer(
            [make_result(src, root=tmp_path)],
            tmp_path / "out",
            columns=(Column.SOURCE_FILE,),
            path_depth=0,
        )
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert rows[0]["source_file"] == "app.log"

    def test_source_file_path_depth_one_includes_parent(self, tmp_path):
        src = tmp_path / "logs" / "web" / "app.log"
        run_writer(
            [make_result(src, root=tmp_path)],
            tmp_path / "out",
            columns=(Column.SOURCE_FILE,),
            path_depth=1,
        )
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert rows[0]["source_file"] == str(Path("web") / "app.log")

    def test_source_file_path_depth_clamped_to_available(self, tmp_path):
        # depth=5 but only 1 parent exists → returns full relative path
        src = tmp_path / "logs" / "app.log"
        run_writer(
            [make_result(src, root=tmp_path)],
            tmp_path / "out",
            columns=(Column.SOURCE_FILE,),
            path_depth=5,
        )
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert rows[0]["source_file"] == str(Path("logs") / "app.log")


# ---------------------------------------------------------------------------
# Context lines
# ---------------------------------------------------------------------------


class TestContextLines:

    def test_context_before_written_as_rows(self, tmp_path):
        src = tmp_path / "app.log"
        result = make_result(
            src, line_no=3,
            before=("line 1 text", "line 2 text"),
        )
        run_writer([result], tmp_path / "out",
                   columns=(Column.LINE_NO, Column.PATTERN, Column.TEXT))
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        # 2 context_before + 1 match = 3 rows
        assert len(rows) == 3
        assert rows[0]["text"] == "line 1 text"
        assert rows[0]["pattern"] == ""         # context row has empty pattern
        assert rows[1]["text"] == "line 2 text"
        assert rows[2]["text"] == result.text   # the match row

    def test_context_after_written_as_rows(self, tmp_path):
        src = tmp_path / "app.log"
        result = make_result(
            src, line_no=3,
            after=("line 4 text", "line 5 text"),
        )
        run_writer([result], tmp_path / "out",
                   columns=(Column.LINE_NO, Column.PATTERN, Column.TEXT))
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert len(rows) == 3   # 1 match + 2 context_after
        assert rows[0]["text"] == result.text
        assert rows[1]["text"] == "line 4 text"
        assert rows[2]["text"] == "line 5 text"

    def test_context_line_nos_are_correct(self, tmp_path):
        src = tmp_path / "app.log"
        result = make_result(src, line_no=5,
                             before=("b1", "b2"), after=("a1",))
        run_writer([result], tmp_path / "out",
                   columns=(Column.LINE_NO, Column.TEXT))
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        line_nos = [int(r["line_no"]) for r in rows]
        assert line_nos == [3, 4, 5, 6]  # before(3,4), match(5), after(6)

    def test_context_rows_have_timestamps(self, tmp_path):
        src = tmp_path / "app.log"
        result = make_result(src, line_no=2, before=("context line",))
        run_writer([result], tmp_path / "out")
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert rows[0]["timestamp"] != ""   # context row has real timestamp
        assert rows[1]["timestamp"] != ""   # match row has real timestamp
        # Context timestamp should be earlier than match timestamp
        assert rows[0]["timestamp"] < rows[1]["timestamp"]

    def test_include_context_false_omits_context(self, tmp_path):
        src = tmp_path / "app.log"
        result = make_result(src, line_no=3,
                             before=("b",), after=("a",))
        run_writer([result], tmp_path / "out", include_context=False)
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert len(rows) == 1
        assert rows[0]["text"] == result.text


# ---------------------------------------------------------------------------
# Sort order
# ---------------------------------------------------------------------------


class TestSortOrder:

    def test_file_order_preserves_insertion_order(self, tmp_path):
        src = tmp_path / "app.log"
        ts_base = datetime(2024, 1, 1, 10, 0, 0)
        results = [
            make_result(src, line_no=i, timestamp=ts_base + timedelta(seconds=i),
                        text=f"line {i}")
            for i in range(1, 6)
        ]
        run_writer(results, tmp_path / "out", sort=SortOrder.FILE_ORDER)
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        texts = [r["text"] for r in rows]
        assert texts == [f"line {i}" for i in range(1, 6)]

    def test_timestamp_sort_orders_by_timestamp(self, tmp_path):
        src = tmp_path / "app.log"
        ts_base = datetime(2024, 1, 1, 10, 0, 0)
        # Insert in reverse order
        results = [
            make_result(src, line_no=i, timestamp=ts_base + timedelta(seconds=5 - i),
                        text=f"line {i}")
            for i in range(1, 6)
        ]
        run_writer(results, tmp_path / "out", sort=SortOrder.TIMESTAMP)
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        timestamps = [r["timestamp"] for r in rows]
        assert timestamps == sorted(timestamps)

    def test_timestamp_sort_across_files(self, tmp_path):
        src_a = tmp_path / "a.log"
        src_b = tmp_path / "b.log"
        ts = datetime(2024, 1, 1, 10, 0, 0)
        results = [
            make_result(src_a, timestamp=ts + timedelta(seconds=2), text="a later"),
            make_result(src_b, timestamp=ts + timedelta(seconds=1), text="b earlier"),
            make_result(src_a, timestamp=ts + timedelta(seconds=3), text="a latest"),
        ]
        run_writer(results, tmp_path / "out", sort=SortOrder.TIMESTAMP)
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        texts = [r["text"] for r in rows]
        assert texts == ["b earlier", "a later", "a latest"]

    def test_timestamp_sort_merges_context_rows_across_files(self, tmp_path):
        src_a = tmp_path / "a.log"
        src_b = tmp_path / "b.log"
        ts = datetime(2024, 1, 1, 10, 0, 0)
        results = [
            make_result(
                src_a,
                line_no=2,
                timestamp=ts + timedelta(seconds=3),
                text="a match",
                before=("a before",),
                after=("a after",),
            ),
            make_result(
                src_b,
                line_no=2,
                timestamp=ts + timedelta(seconds=3, milliseconds=50),
                text="b match",
                before=("b before",),
                after=("b after",),
            ),
        ]
        run_writer(results, tmp_path / "out", sort=SortOrder.TIMESTAMP)
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert [r["text"] for r in rows] == [
            "a before",
            "b before",
            "a match",
            "b match",
            "a after",
            "b after",
        ]
        timestamps = [r["timestamp"] for r in rows]
        assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# Context-manager and resource cleanup
# ---------------------------------------------------------------------------


class TestContextManager:

    def test_context_manager_closes_files_on_exit(self, tmp_path):
        src = tmp_path / "app.log"
        cfg = OutputConfig(output_dir=tmp_path / "out")
        with OutputWriter(cfg) as w:
            w.add_result(make_result(src, text="hello"))
        # After exit, file should be complete and readable
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert rows[0]["text"] == "hello"

    def test_output_dir_created_if_missing(self, tmp_path):
        deep = tmp_path / "a" / "b" / "c"
        assert not deep.exists()
        cfg = OutputConfig(output_dir=deep)
        with OutputWriter(cfg):
            pass
        assert deep.exists()
