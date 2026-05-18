"""Tests for ExpressionAnalyzer — matching, filtering, context, and file-skip."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from log_master.core.expression_analyzer import (
    ExpressionAnalyzer,
    FileAnalysisResult,
    MatchResult,
    SearchConfig,
)
from log_master.core.file_finder import FileInfo
from log_master.core.timestamp_resolver import TimestampResolver

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_resolver = TimestampResolver()


def write_log(
    path: Path,
    lines: list[str],
    mtime: datetime | None = None,
) -> FileInfo:
    content = "\n".join(lines)
    if lines:
        content += "\n"
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))
        return FileInfo(path=path, root=path.parent, size_bytes=path.stat().st_size, mtime=mtime)
    stat = path.stat()
    return FileInfo(
        path=path,
        root=path.parent,
        size_bytes=stat.st_size,
        mtime=datetime.fromtimestamp(stat.st_mtime),
    )


def analyze(fi: FileInfo, **kwargs) -> FileAnalysisResult:
    cfg = SearchConfig(**kwargs)
    return ExpressionAnalyzer(cfg).analyze_file(fi, _resolver)


def texts(result: FileAnalysisResult) -> list[str]:
    return [r.text for r in result.matches]


# Lines written at known timestamps (space_datetime format):
#   line 1  08:00:00  INFO  application starting
#   line 2  08:00:01  INFO  database connected
#   line 3  08:00:02  WARN  slow query detected
#   line 4  08:00:03  ERROR database timeout
#   line 5  08:00:04  ERROR connection reset
#   line 6  08:00:05  INFO  retrying
#   line 7  08:00:06  INFO  recovered

_MTIME = datetime(2024, 3, 15)
_LINES = [
    "2024-03-15 08:00:00 INFO application starting",
    "2024-03-15 08:00:01 INFO database connected",
    "2024-03-15 08:00:02 WARN slow query detected",
    "2024-03-15 08:00:03 ERROR database timeout",
    "2024-03-15 08:00:04 ERROR connection reset",
    "2024-03-15 08:00:05 INFO retrying",
    "2024-03-15 08:00:06 INFO recovered",
]


@pytest.fixture
def log(tmp_path):
    return write_log(tmp_path / "app.log", _LINES, _MTIME)


# ---------------------------------------------------------------------------
# Basic include matching
# ---------------------------------------------------------------------------


class TestIncludePatterns:

    def test_single_pattern_matches(self, log):
        result = analyze(log, include_patterns=("ERROR",))
        assert texts(result) == [
            "ERROR database timeout",
            "ERROR connection reset",
        ]

    def test_no_patterns_matches_all_lines(self, log):
        result = analyze(log)
        assert len(result.matches) == len(_LINES)

    def test_pattern_no_match_returns_empty(self, log):
        result = analyze(log, include_patterns=("CRITICAL",))
        assert result.matches == []

    def test_or_logic_any_pattern_sufficient(self, log):
        result = analyze(log, include_patterns=("ERROR", "WARN"))
        ts = texts(result)
        assert "WARN slow query detected" in ts
        assert "ERROR database timeout" in ts
        assert "ERROR connection reset" in ts
        assert "INFO application starting" not in ts

    def test_matched_patterns_records_which_triggered(self, log):
        result = analyze(log, include_patterns=("ERROR", "WARN"))
        warn_match = next(r for r in result.matches if "WARN" in r.text)
        err_match = next(r for r in result.matches if "timeout" in r.text)
        assert warn_match.matched_patterns == frozenset({"WARN"})
        assert err_match.matched_patterns == frozenset({"ERROR"})

    def test_matched_patterns_records_multiple_when_both_match(self, log):
        # "database" matches both patterns on the timeout line
        result = analyze(log, include_patterns=("database", "timeout"))
        timeout_match = next(r for r in result.matches if "timeout" in r.text)
        assert timeout_match.matched_patterns == frozenset({"database", "timeout"})

    def test_matched_patterns_empty_frozenset_when_no_include_given(self, log):
        result = analyze(log)
        assert all(r.matched_patterns == frozenset() for r in result.matches)

    def test_regex_pattern_matches(self, log):
        # Match lines starting with ERROR or WARN
        result = analyze(log, include_patterns=(r"^(ERROR|WARN)",))
        assert len(result.matches) == 3

    def test_source_file_is_correct(self, log):
        result = analyze(log, include_patterns=("ERROR",))
        assert all(r.source_file == log.path for r in result.matches)

    def test_line_numbers_are_correct(self, log):
        result = analyze(log, include_patterns=("ERROR",))
        assert [r.line_no for r in result.matches] == [4, 5]

    def test_timestamps_are_correct(self, log):
        result = analyze(log, include_patterns=("ERROR",))
        assert result.matches[0].timestamp == datetime(2024, 3, 15, 8, 0, 3)
        assert result.matches[1].timestamp == datetime(2024, 3, 15, 8, 0, 4)


# ---------------------------------------------------------------------------
# Exclude patterns
# ---------------------------------------------------------------------------


class TestExcludePatterns:

    def test_exclude_rejects_matching_lines(self, log):
        result = analyze(log, exclude_patterns=("INFO",))
        ts = texts(result)
        assert not any("INFO" in t for t in ts)
        assert "WARN slow query detected" in ts
        assert "ERROR database timeout" in ts

    def test_exclude_takes_precedence_over_include(self, log):
        # "database" appears in both INFO and ERROR lines
        result = analyze(
            log,
            include_patterns=("database",),
            exclude_patterns=("INFO",),
        )
        ts = texts(result)
        assert "INFO database connected" not in ts
        assert "ERROR database timeout" in ts

    def test_multiple_excludes_any_fires(self, log):
        result = analyze(log, exclude_patterns=("INFO", "WARN"))
        ts = texts(result)
        assert not any("INFO" in t or "WARN" in t for t in ts)
        assert len(ts) == 2  # only the two ERROR lines

    def test_exclude_pattern_no_match_passes_all(self, log):
        result = analyze(log, exclude_patterns=("CRITICAL",))
        assert len(result.matches) == len(_LINES)

    def test_exclude_with_regex(self, log):
        result = analyze(log, exclude_patterns=(r"data\w+",))
        ts = texts(result)
        assert not any("database" in t for t in ts)


# ---------------------------------------------------------------------------
# Time filtering
# ---------------------------------------------------------------------------


class TestTimeFiltering:

    def test_time_from_rejects_earlier_lines(self, log):
        result = analyze(
            log,
            time_from=datetime(2024, 3, 15, 8, 0, 3),
        )
        assert all(r.timestamp >= datetime(2024, 3, 15, 8, 0, 3) for r in result.matches)
        assert len(result.matches) == 4  # lines 4-7

    def test_time_to_rejects_later_lines(self, log):
        result = analyze(
            log,
            time_to=datetime(2024, 3, 15, 8, 0, 2),
        )
        assert all(r.timestamp <= datetime(2024, 3, 15, 8, 0, 2) for r in result.matches)
        assert len(result.matches) == 3  # lines 1-3

    def test_time_range_combined(self, log):
        result = analyze(
            log,
            time_from=datetime(2024, 3, 15, 8, 0, 2),
            time_to=datetime(2024, 3, 15, 8, 0, 4),
        )
        assert len(result.matches) == 3  # lines 3, 4, 5

    def test_time_filter_inclusive_bounds(self, log):
        result = analyze(
            log,
            time_from=datetime(2024, 3, 15, 8, 0, 0),
            time_to=datetime(2024, 3, 15, 8, 0, 0),
        )
        assert len(result.matches) == 1
        assert result.matches[0].line_no == 1

    def test_no_time_filter_matches_all(self, log):
        result = analyze(log, time_from=None, time_to=None)
        assert len(result.matches) == len(_LINES)

    def test_time_filter_with_include_pattern(self, log):
        result = analyze(
            log,
            include_patterns=("ERROR",),
            time_from=datetime(2024, 3, 15, 8, 0, 4),
        )
        assert len(result.matches) == 1
        assert "connection reset" in result.matches[0].text


# ---------------------------------------------------------------------------
# Context lines
# ---------------------------------------------------------------------------


class TestContextLines:

    def test_context_before_correct(self, log):
        result = analyze(log, include_patterns=("ERROR",), context_lines=2)
        timeout_match = next(r for r in result.matches if "timeout" in r.text)
        # Line 4 = ERROR database timeout; lines 2-3 are context_before
        assert len(timeout_match.context_before) == 2
        assert "database connected" in timeout_match.context_before[0].text
        assert "slow query" in timeout_match.context_before[1].text

    def test_context_before_has_timestamp(self, log):
        result = analyze(log, include_patterns=("ERROR",), context_lines=1)
        timeout_match = next(r for r in result.matches if "timeout" in r.text)
        ctx = timeout_match.context_before[0]
        assert isinstance(ctx.timestamp, datetime)
        assert ctx.timestamp == datetime(2024, 3, 15, 8, 0, 2)  # WARN line

    def test_context_before_has_line_no(self, log):
        result = analyze(log, include_patterns=("ERROR",), context_lines=1)
        timeout_match = next(r for r in result.matches if "timeout" in r.text)
        assert timeout_match.context_before[0].line_no == 3  # one before line 4

    def test_context_after_correct(self, log):
        result = analyze(log, include_patterns=("ERROR",), context_lines=2)
        reset_match = next(r for r in result.matches if "reset" in r.text)
        # Line 5 = ERROR connection reset; lines 6-7 are context_after
        assert len(reset_match.context_after) == 2
        assert "retrying" in reset_match.context_after[0].text
        assert "recovered" in reset_match.context_after[1].text

    def test_context_after_has_timestamp(self, log):
        result = analyze(log, include_patterns=("ERROR",), context_lines=1)
        reset_match = next(r for r in result.matches if "reset" in r.text)
        ctx = reset_match.context_after[0]
        assert isinstance(ctx.timestamp, datetime)
        assert ctx.timestamp == datetime(2024, 3, 15, 8, 0, 5)  # INFO retrying

    def test_context_after_has_line_no(self, log):
        result = analyze(log, include_patterns=("ERROR",), context_lines=1)
        reset_match = next(r for r in result.matches if "reset" in r.text)
        assert reset_match.context_after[0].line_no == 6  # one after line 5

    def test_context_clamped_at_file_start(self, log):
        result = analyze(log, include_patterns=("application",), context_lines=3)
        # Line 1 is first — no context_before possible
        assert result.matches[0].context_before == ()

    def test_context_clamped_at_file_end(self, log):
        result = analyze(log, include_patterns=("recovered",), context_lines=3)
        # Line 7 is last — fewer than 3 context_after lines available
        assert len(result.matches[0].context_after) == 0

    def test_context_partially_available_at_file_end(self, log):
        result = analyze(log, include_patterns=("retrying",), context_lines=3)
        # Line 6; only 1 line follows (line 7)
        assert len(result.matches[0].context_after) == 1
        assert "recovered" in result.matches[0].context_after[0].text

    def test_zero_context_lines_gives_empty_tuples(self, log):
        result = analyze(log, include_patterns=("ERROR",), context_lines=0)
        for r in result.matches:
            assert r.context_before == ()
            assert r.context_after == ()

    def test_context_contains_stripped_text_not_raw_lines(self, log):
        # Context lines should be the stripped text (no timestamp prefix)
        result = analyze(log, include_patterns=("WARN",), context_lines=1)
        match = result.matches[0]
        assert not any(
            "2024-03-15" in line.text
            for line in (*match.context_before, *match.context_after)
        )

    def test_overlapping_context_windows_both_complete(self, log):
        # ERROR lines are at positions 4 and 5; with context=1 they overlap
        result = analyze(log, include_patterns=("ERROR",), context_lines=1)
        timeout = next(r for r in result.matches if "timeout" in r.text)
        reset = next(r for r in result.matches if "reset" in r.text)
        # timeout's context_after is the line immediately after it (ERROR reset)
        assert "connection reset" in timeout.context_after[0].text
        # reset's context_before is the line immediately before it (ERROR timeout)
        assert "database timeout" in reset.context_before[0].text

    def test_context_lines_one(self, tmp_path):
        lines = [
            "2024-01-01 09:00:00 before",
            "2024-01-01 09:00:01 TARGET match",
            "2024-01-01 09:00:02 after",
        ]
        fi = write_log(tmp_path / "t.log", lines, datetime(2024, 1, 1))
        result = analyze(fi, include_patterns=("TARGET",), context_lines=1)
        assert len(result.matches) == 1
        assert "before" in result.matches[0].context_before[0].text
        assert "after" in result.matches[0].context_after[0].text


# ---------------------------------------------------------------------------
# File-skip
# ---------------------------------------------------------------------------


class TestFileSkip:

    def test_skip_pattern_found_returns_empty_matches(self, log):
        result = analyze(log, skip_file_patterns=("DIAG-ONLY",))
        assert result.was_skipped is False  # pattern not in file

    def test_skip_pattern_present_sets_was_skipped(self, log):
        # "INFO" is present in the file
        result = analyze(log, skip_file_patterns=("INFO",))
        assert result.was_skipped is True
        assert result.matches == []

    def test_skip_discards_matches_found_before_skip_line(self, tmp_path):
        lines = [
            "2024-01-01 09:00:00 ERROR this would match",
            "2024-01-01 09:00:01 SKIP_MARKER found here",
            "2024-01-01 09:00:02 INFO normal line",
        ]
        fi = write_log(tmp_path / "t.log", lines, datetime(2024, 1, 1))
        result = analyze(
            fi,
            include_patterns=("ERROR",),
            skip_file_patterns=("SKIP_MARKER",),
        )
        assert result.was_skipped is True
        assert result.matches == []

    def test_skip_pattern_not_present_normal_results(self, tmp_path):
        lines = [
            "2024-01-01 09:00:00 ERROR something",
        ]
        fi = write_log(tmp_path / "t.log", lines, datetime(2024, 1, 1))
        result = analyze(
            fi,
            include_patterns=("ERROR",),
            skip_file_patterns=("SKIP_MARKER",),
        )
        assert result.was_skipped is False
        assert len(result.matches) == 1

    def test_multiple_skip_patterns_any_triggers(self, log):
        result = analyze(log, skip_file_patterns=("NOTFOUND", "INFO"))
        assert result.was_skipped is True


# ---------------------------------------------------------------------------
# Case sensitivity
# ---------------------------------------------------------------------------


class TestCaseSensitivity:

    def test_case_sensitive_by_default(self, log):
        result = analyze(log, include_patterns=("error",))
        assert result.matches == []  # "ERROR" ≠ "error"

    def test_case_insensitive_matches(self, log):
        result = analyze(log, include_patterns=("error",), case_sensitive=False)
        assert len(result.matches) == 2

    def test_case_insensitive_exclude(self, log):
        result = analyze(log, exclude_patterns=("info",), case_sensitive=False)
        ts = texts(result)
        assert not any("INFO" in t for t in ts)

    def test_case_insensitive_skip(self, tmp_path):
        lines = ["2024-01-01 09:00:00 DIAG-ONLY skip this file"]
        fi = write_log(tmp_path / "t.log", lines, datetime(2024, 1, 1))
        result = analyze(fi, skip_file_patterns=("diag-only",), case_sensitive=False)
        assert result.was_skipped is True


# ---------------------------------------------------------------------------
# Return types and edge cases
# ---------------------------------------------------------------------------


class TestReturnTypes:

    def test_file_analysis_result_is_named_tuple(self, log):
        result = analyze(log)
        assert isinstance(result, FileAnalysisResult)
        assert isinstance(result.matches, list)
        assert isinstance(result.was_skipped, bool)

    def test_match_result_fields(self, log):
        result = analyze(log, include_patterns=("ERROR",))
        r = result.matches[0]
        assert isinstance(r, MatchResult)
        assert isinstance(r.source_file, Path)
        assert isinstance(r.line_no, int)
        assert isinstance(r.timestamp, datetime)
        assert isinstance(r.text, str)
        assert isinstance(r.matched_patterns, frozenset)
        assert isinstance(r.context_before, tuple)
        assert isinstance(r.context_after, tuple)

    def test_empty_file_yields_empty_results(self, tmp_path):
        fi = write_log(tmp_path / "empty.log", [], datetime(2024, 1, 1))
        result = analyze(fi)
        assert result.matches == []
        assert result.was_skipped is False

    def test_analyzer_reusable_across_files(self, tmp_path):
        cfg = SearchConfig(include_patterns=("ERROR",))
        analyzer = ExpressionAnalyzer(cfg)

        fi1 = write_log(
            tmp_path / "a.log",
            ["2024-01-01 09:00:00 ERROR file a"],
            datetime(2024, 1, 1),
        )
        fi2 = write_log(
            tmp_path / "b.log",
            ["2024-01-01 09:00:00 INFO no errors here"],
            datetime(2024, 1, 1),
        )
        r1 = analyzer.analyze_file(fi1, _resolver)
        r2 = analyzer.analyze_file(fi2, _resolver)
        assert len(r1.matches) == 1
        assert len(r2.matches) == 0

    def test_was_skipped_false_when_no_skip_patterns(self, log):
        result = analyze(log)
        assert result.was_skipped is False

    def test_all_lines_excluded_returns_empty_not_skipped(self, log):
        result = analyze(log, exclude_patterns=(".*",))
        assert result.matches == []
        assert result.was_skipped is False
