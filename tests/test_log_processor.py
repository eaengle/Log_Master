"""Tests for LogProcessor — end-to-end pipeline integration."""

from __future__ import annotations

import csv
import os
from datetime import datetime
from pathlib import Path

import pytest

from log_master.core.expression_analyzer import SearchConfig
from log_master.core.file_finder import FileFindCriteria
from log_master.core.log_processor import LogProcessor, ProcessorConfig, ProcessorResult
from log_master.core.output_writer import OutputConfig, OutputMode, SortOrder

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MTIME = datetime(2024, 6, 1)


def write_log(path: Path, lines: list[str], mtime: datetime = MTIME) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    content = "\n".join(lines)
    if lines:
        content += "\n"
    path.write_text(content, encoding="utf-8")
    ts = mtime.timestamp()
    os.utime(path, (ts, ts))
    return path


def read_tsv(path: Path) -> list[dict[str, str]]:
    with open(path, encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f, delimiter="\t"))


def make_config(
    root: Path,
    out: Path,
    *,
    include_patterns: tuple[str, ...] = (),
    exclude_patterns: tuple[str, ...] = (),
    skip_patterns: tuple[str, ...] = (),
    workers: int = 1,
    sort: SortOrder = SortOrder.FILE_ORDER,
    modes: frozenset[OutputMode] = frozenset({OutputMode.SINGLE}),
) -> ProcessorConfig:
    return ProcessorConfig(
        find_criteria=FileFindCriteria(root_dirs=(root,)),
        search_config=SearchConfig(
            include_patterns=include_patterns,
            exclude_patterns=exclude_patterns,
            skip_file_patterns=skip_patterns,
        ),
        output_config=OutputConfig(output_dir=out, modes=modes, sort=sort),
        workers=workers,
    )


# ---------------------------------------------------------------------------
# Basic pipeline
# ---------------------------------------------------------------------------


class TestBasicPipeline:

    def test_returns_processor_result(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "a.log", ["2024-01-01 09:00:00 INFO hello"])
        cfg = make_config(root, tmp_path / "out")
        result = LogProcessor(cfg).run()
        assert isinstance(result, ProcessorResult)

    def test_files_found_count(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "a.log", ["2024-01-01 09:00:00 INFO hello"])
        write_log(root / "b.log", ["2024-01-01 09:00:01 INFO world"])
        cfg = make_config(root, tmp_path / "out")
        result = LogProcessor(cfg).run()
        assert result.files_found == 2

    def test_matches_written_to_tsv(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 09:00:00 ERROR failure",
            "2024-01-01 09:00:01 INFO ok",
        ])
        cfg = make_config(root, tmp_path / "out",
                          include_patterns=("ERROR",))
        LogProcessor(cfg).run()
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert len(rows) == 1
        assert "failure" in rows[0]["text"]

    def test_no_include_matches_all_lines(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 09:00:00 INFO one",
            "2024-01-01 09:00:01 INFO two",
            "2024-01-01 09:00:02 INFO three",
        ])
        cfg = make_config(root, tmp_path / "out")
        result = LogProcessor(cfg).run()
        assert result.matches_total == 3

    def test_matches_total_count(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "a.log", [
            "2024-01-01 09:00:00 ERROR one",
            "2024-01-01 09:00:01 ERROR two",
        ])
        write_log(root / "b.log", [
            "2024-01-01 09:00:02 ERROR three",
        ])
        cfg = make_config(root, tmp_path / "out",
                          include_patterns=("ERROR",))
        result = LogProcessor(cfg).run()
        assert result.matches_total == 3

    def test_empty_directory_produces_no_output(self, tmp_path):
        root = tmp_path / "logs"
        root.mkdir()
        cfg = make_config(root, tmp_path / "out")
        result = LogProcessor(cfg).run()
        assert result.files_found == 0
        assert result.matches_total == 0
        assert not (tmp_path / "out" / "results.tsv").exists()

    def test_output_dir_created_automatically(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "a.log", ["2024-01-01 09:00:00 INFO hi"])
        deep_out = tmp_path / "x" / "y" / "z"
        cfg = make_config(root, deep_out)
        LogProcessor(cfg).run()
        assert deep_out.exists()


# ---------------------------------------------------------------------------
# Exclude and skip
# ---------------------------------------------------------------------------


class TestExcludeAndSkip:

    def test_exclude_pattern_reduces_matches(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 09:00:00 ERROR bad",
            "2024-01-01 09:00:01 ERROR noise",
        ])
        cfg = make_config(root, tmp_path / "out",
                          include_patterns=("ERROR",),
                          exclude_patterns=("noise",))
        result = LogProcessor(cfg).run()
        assert result.matches_total == 1

    def test_skip_file_pattern_skips_entire_file(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "a.log", [
            "2024-01-01 09:00:00 ERROR keep",
        ])
        write_log(root / "b.log", [
            "2024-01-01 09:00:00 SKIP_MARKER here",
            "2024-01-01 09:00:01 ERROR skip this too",
        ])
        cfg = make_config(root, tmp_path / "out",
                          include_patterns=("ERROR",),
                          skip_patterns=("SKIP_MARKER",))
        result = LogProcessor(cfg).run()
        assert result.files_skipped == 1
        assert result.files_analyzed == 1
        assert result.matches_total == 1

    def test_files_analyzed_equals_found_minus_skipped(self, tmp_path):
        root = tmp_path / "logs"
        for i in range(4):
            write_log(root / f"{i}.log", [
                f"2024-01-01 09:00:0{i} INFO line",
            ])
        write_log(root / "bad.log", ["SKIP_ME here"])
        cfg = make_config(root, tmp_path / "out",
                          skip_patterns=("SKIP_ME",))
        result = LogProcessor(cfg).run()
        assert result.files_found == 5
        assert result.files_skipped == 1
        assert result.files_analyzed == 4


# ---------------------------------------------------------------------------
# Multi-file ordering (file-order mode)
# ---------------------------------------------------------------------------


class TestFileOrder:

    def test_results_appear_in_file_discovery_order(self, tmp_path):
        root = tmp_path / "logs"
        # Use sorted filenames so discovery order is predictable
        write_log(root / "a.log", ["2024-01-01 09:00:02 INFO from-a"])
        write_log(root / "b.log", ["2024-01-01 09:00:01 INFO from-b"])
        cfg = make_config(root, tmp_path / "out", sort=SortOrder.FILE_ORDER)
        LogProcessor(cfg).run()
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        # a.log comes before b.log alphabetically — regardless of timestamps
        assert rows[0]["text"] == "INFO from-a"
        assert rows[1]["text"] == "INFO from-b"


# ---------------------------------------------------------------------------
# Timestamp sort
# ---------------------------------------------------------------------------


class TestTimestampSort:

    def test_timestamp_sort_orders_across_files(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "a.log", ["2024-01-01 10:00:02 INFO second"])
        write_log(root / "b.log", ["2024-01-01 10:00:01 INFO first"])
        cfg = make_config(root, tmp_path / "out", sort=SortOrder.TIMESTAMP)
        LogProcessor(cfg).run()
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert rows[0]["text"] == "INFO first"
        assert rows[1]["text"] == "INFO second"

    def test_timestamp_sort_within_single_file(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 09:00:03 INFO c",
            "2024-01-01 09:00:01 INFO a",
            "2024-01-01 09:00:02 INFO b",
        ])
        cfg = make_config(root, tmp_path / "out", sort=SortOrder.TIMESTAMP)
        LogProcessor(cfg).run()
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        timestamps = [r["timestamp"] for r in rows]
        assert timestamps == sorted(timestamps)


# ---------------------------------------------------------------------------
# Output modes
# ---------------------------------------------------------------------------


class TestOutputModes:

    def test_per_pattern_mode_creates_pattern_files(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 09:00:00 ERROR bad",
            "2024-01-01 09:00:01 WARN slow",
        ])
        cfg = make_config(root, tmp_path / "out",
                          include_patterns=("ERROR", "WARN"),
                          modes=frozenset({OutputMode.PER_PATTERN}))
        LogProcessor(cfg).run()
        out = tmp_path / "out"
        assert (out / "pattern_ERROR.tsv").exists()
        assert (out / "pattern_WARN.tsv").exists()

    def test_per_source_file_mode(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "alpha.log", ["2024-01-01 09:00:00 INFO alpha"])
        write_log(root / "beta.log",  ["2024-01-01 09:00:00 INFO beta"])
        cfg = make_config(root, tmp_path / "out",
                          modes=frozenset({OutputMode.PER_SOURCE_FILE}))
        LogProcessor(cfg).run()
        out = tmp_path / "out"
        assert (out / "alpha.tsv").exists()
        assert (out / "beta.tsv").exists()

    def test_combined_single_and_per_pattern(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", ["2024-01-01 09:00:00 ERROR crash"])
        cfg = make_config(root, tmp_path / "out",
                          include_patterns=("ERROR",),
                          modes=frozenset({OutputMode.SINGLE, OutputMode.PER_PATTERN}))
        LogProcessor(cfg).run()
        out = tmp_path / "out"
        assert (out / "results.tsv").exists()
        assert (out / "pattern_ERROR.tsv").exists()


# ---------------------------------------------------------------------------
# Workers / parallelism
# ---------------------------------------------------------------------------


class TestWorkers:

    def _make_multi_file_config(self, root, out, n=5, workers=1):
        for i in range(n):
            write_log(root / f"f{i:02d}.log", [
                f"2024-01-01 09:00:{i:02d} ERROR line{i}",
            ])
        return make_config(root, out,
                           include_patterns=("ERROR",),
                           workers=workers)

    def test_serial_workers_1(self, tmp_path):
        root = tmp_path / "logs"
        cfg = self._make_multi_file_config(root, tmp_path / "out", workers=1)
        result = LogProcessor(cfg).run()
        assert result.matches_total == 5

    def test_parallel_workers_2(self, tmp_path):
        root = tmp_path / "logs"
        cfg = self._make_multi_file_config(root, tmp_path / "out", workers=2)
        result = LogProcessor(cfg).run()
        assert result.matches_total == 5

    def test_parallel_workers_4(self, tmp_path):
        root = tmp_path / "logs"
        cfg = self._make_multi_file_config(root, tmp_path / "out", workers=4)
        result = LogProcessor(cfg).run()
        assert result.matches_total == 5

    def test_auto_workers_0(self, tmp_path):
        root = tmp_path / "logs"
        cfg = self._make_multi_file_config(root, tmp_path / "out", workers=0)
        result = LogProcessor(cfg).run()
        assert result.matches_total == 5

    def test_parallel_produces_same_matches_as_serial(self, tmp_path):
        root_s = tmp_path / "serial"
        root_p = tmp_path / "parallel"
        lines = [f"2024-01-01 09:00:{i:02d} ERROR msg{i}" for i in range(10)]
        for i in range(3):
            write_log(root_s / f"f{i}.log", lines)
            write_log(root_p / f"f{i}.log", lines)

        serial_cfg = make_config(root_s, tmp_path / "out_s",
                                 include_patterns=("ERROR",), workers=1)
        parallel_cfg = make_config(root_p, tmp_path / "out_p",
                                   include_patterns=("ERROR",), workers=4,
                                   sort=SortOrder.TIMESTAMP)

        r_serial = LogProcessor(serial_cfg).run()
        r_parallel = LogProcessor(parallel_cfg).run()
        assert r_serial.matches_total == r_parallel.matches_total

    def test_parallel_file_order_preserved(self, tmp_path):
        root = tmp_path / "logs"
        # Files named so sorted discovery order == alpha order
        write_log(root / "a.log", ["2024-01-01 09:00:02 INFO from-a"])
        write_log(root / "b.log", ["2024-01-01 09:00:01 INFO from-b"])
        cfg = make_config(root, tmp_path / "out",
                          sort=SortOrder.FILE_ORDER, workers=2)
        LogProcessor(cfg).run()
        rows = read_tsv(tmp_path / "out" / "results.tsv")
        assert rows[0]["text"] == "INFO from-a"
        assert rows[1]["text"] == "INFO from-b"
