"""Tests for TimestampResolver — format detection, parsing, and edge cases."""

from __future__ import annotations

import os
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

import pytest

from log_master.core.file_finder import FileInfo
from log_master.core.timestamp_resolver import (
    DETECT_MIN_HIT_RATE,
    DETECT_SAMPLE_LINES,
    ParsedLine,
    TimestampResolver,
)

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

resolver = TimestampResolver()


def utc_to_local(utc_dt: datetime) -> datetime:
    """Mirror of the resolver's UTC→local conversion, for timezone-agnostic tests."""
    return utc_dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


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
        return FileInfo(path=path, size_bytes=path.stat().st_size, mtime=mtime)
    stat = path.stat()
    return FileInfo(
        path=path,
        size_bytes=stat.st_size,
        mtime=datetime.fromtimestamp(stat.st_mtime),
    )


def parse_all(file_info: FileInfo) -> list[ParsedLine]:
    return list(resolver.iter_parsed_lines(file_info))


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

class TestFormatDetection:

    def _detect(self, lines: list[str]) -> str | None:
        return resolver.detect_format_name(lines)

    def test_iso_with_tz(self):
        assert self._detect([
            "2024-01-15T10:30:45.123+05:00 INFO msg",
            "2024-01-15T10:30:46.000+05:00 WARN msg",
        ]) == "iso_with_tz"

    def test_iso_with_tz_space_separator(self):
        assert self._detect([
            "2024-01-15 10:30:45.123+05:00 INFO msg",
        ]) == "iso_with_tz"

    def test_iso_with_z(self):
        assert self._detect([
            "2024-01-15T10:30:45Z INFO msg",
            "2024-01-15T10:30:46.500Z INFO msg",
        ]) == "iso_with_z"

    def test_iso_t_local(self):
        assert self._detect([
            "2024-01-15T10:30:45.123 INFO msg",
            "2024-01-15T10:30:46 WARN msg",
        ]) == "iso_t_local"

    def test_apache(self):
        assert self._detect([
            '15/Jan/2024:10:30:45 +0000 "GET / HTTP/1.1" 200',
            '15/Jan/2024:10:30:46 +0000 "POST /api HTTP/1.1" 201',
        ]) == "apache"

    def test_apache_without_tz(self):
        assert self._detect([
            '15/Jan/2024:10:30:45 "GET / HTTP/1.1" 200',
        ]) == "apache"

    def test_space_frac(self):
        assert self._detect([
            "2024-01-15 10:30:45,123 INFO msg",
            "2024-01-15 10:30:46,456 WARN msg",
        ]) == "space_frac"

    def test_space_frac_dot_separator(self):
        assert self._detect([
            "2024-01-15 10:30:45.123 INFO msg",
        ]) == "space_frac"

    def test_space_datetime(self):
        assert self._detect([
            "2024-01-15 10:30:45 INFO msg",
            "2024-01-15 10:30:46 WARN msg",
        ]) == "space_datetime"

    def test_syslog(self):
        assert self._detect([
            "Jan 15 10:30:45 host app: msg",
            "Jan 15 10:30:46 host app: msg",
        ]) == "syslog"

    def test_syslog_single_digit_day(self):
        assert self._detect([
            "Jan  5 10:30:45 host app: msg",
        ]) == "syslog"

    def test_date_only(self):
        assert self._detect([
            "2024-01-15 INFO something",
            "2024-01-16 WARN something",
        ]) == "date_only"

    def test_time_frac(self):
        assert self._detect([
            "10:30:45.123 INFO msg",
            "10:30:46.456 WARN msg",
        ]) == "time_frac"

    def test_time_only(self):
        assert self._detect([
            "10:30:45 INFO msg",
            "10:30:46 WARN msg",
        ]) == "time_only"

    def test_no_timestamp_returns_none(self):
        assert self._detect([
            "plain text with no timestamp",
            "another plain line",
        ]) is None

    def test_empty_lines_returns_none(self):
        assert self._detect([]) is None
        assert self._detect(["", "   "]) is None

    def test_hit_rate_threshold(self):
        # 4 matching lines out of 10 non-empty = 40% < 50% threshold → None
        lines = ["2024-01-15 10:30:45 INFO msg"] * 4 + ["plain text"] * 6
        assert self._detect(lines) is None

    def test_hit_rate_at_threshold(self):
        # Exactly 50% hit rate should qualify
        lines = ["2024-01-15 10:30:45 INFO msg"] * 5 + ["plain text"] * 5
        assert self._detect(lines) == "space_datetime"

    def test_most_restrictive_wins_over_less_restrictive(self):
        # Lines have fractional seconds → space_frac should win over space_datetime
        assert self._detect([
            "2024-01-15 10:30:45,123 INFO msg",
        ]) == "space_frac"

    def test_iso_tz_wins_over_space_datetime(self):
        # Timezone present → iso_with_tz should win
        assert self._detect([
            "2024-01-15 10:30:45+05:00 INFO msg",
        ]) == "iso_with_tz"


# ---------------------------------------------------------------------------
# Full ISO with tz offset — parsing and UTC conversion
# ---------------------------------------------------------------------------

class TestIsoWithTz:

    def test_positive_offset_converts_to_local(self, tmp_path):
        mtime = datetime(2024, 1, 15)
        fi = write_log(tmp_path / "t.log", [
            "2024-01-15T10:30:45+05:00 INFO msg",
        ], mtime)
        results = parse_all(fi)
        utc_naive = datetime(2024, 1, 15, 10, 30, 45) - timedelta(hours=5)
        assert results[0].timestamp == utc_to_local(utc_naive)

    def test_negative_offset_converts_to_local(self, tmp_path):
        mtime = datetime(2024, 1, 15)
        fi = write_log(tmp_path / "t.log", [
            "2024-01-15T23:00:00-07:00 INFO msg",
        ], mtime)
        results = parse_all(fi)
        utc_naive = datetime(2024, 1, 15, 23, 0, 0) + timedelta(hours=7)
        assert results[0].timestamp == utc_to_local(utc_naive)

    def test_fractional_seconds_preserved(self, tmp_path):
        mtime = datetime(2024, 1, 15)
        fi = write_log(tmp_path / "t.log", [
            "2024-01-15T10:30:45.123456+00:00 INFO msg",
        ], mtime)
        results = parse_all(fi)
        utc_naive = datetime(2024, 1, 15, 10, 30, 45, 123456)
        assert results[0].timestamp == utc_to_local(utc_naive)

    def test_colon_in_offset(self, tmp_path):
        mtime = datetime(2024, 1, 15)
        fi = write_log(tmp_path / "t.log", [
            "2024-01-15T10:30:45+05:30 INFO msg",
        ], mtime)
        results = parse_all(fi)
        utc_naive = datetime(2024, 1, 15, 10, 30, 45) - timedelta(hours=5, minutes=30)
        assert results[0].timestamp == utc_to_local(utc_naive)


# ---------------------------------------------------------------------------
# ISO with Z — UTC conversion
# ---------------------------------------------------------------------------

class TestIsoWithZ:

    def test_z_suffix_converts_to_local(self, tmp_path):
        mtime = datetime(2024, 3, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-03-01T08:00:00Z INFO startup",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp == utc_to_local(datetime(2024, 3, 1, 8, 0, 0))

    def test_z_with_frac(self, tmp_path):
        mtime = datetime(2024, 3, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-03-01T08:00:00.500Z INFO startup",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp == utc_to_local(datetime(2024, 3, 1, 8, 0, 0, 500000))


# ---------------------------------------------------------------------------
# ISO T local — no timezone conversion
# ---------------------------------------------------------------------------

class TestIsoTLocal:

    def test_parsed_as_local_no_conversion(self, tmp_path):
        mtime = datetime(2024, 6, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-06-01T09:15:30.250 INFO msg",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp == datetime(2024, 6, 1, 9, 15, 30, 250000)


# ---------------------------------------------------------------------------
# Apache format
# ---------------------------------------------------------------------------

class TestApache:

    def test_with_utc_offset(self, tmp_path):
        mtime = datetime(2024, 3, 15)
        fi = write_log(tmp_path / "t.log", [
            '15/Mar/2024:10:30:45 +0000 "GET / HTTP/1.1" 200 1234',
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp == utc_to_local(datetime(2024, 3, 15, 10, 30, 45))

    def test_with_positive_offset(self, tmp_path):
        mtime = datetime(2024, 3, 15)
        fi = write_log(tmp_path / "t.log", [
            '15/Mar/2024:10:30:45 +0530 "GET / HTTP/1.1" 200 1234',
        ], mtime)
        results = parse_all(fi)
        utc_naive = datetime(2024, 3, 15, 10, 30, 45) - timedelta(hours=5, minutes=30)
        assert results[0].timestamp == utc_to_local(utc_naive)

    def test_without_tz(self, tmp_path):
        mtime = datetime(2024, 3, 15)
        fi = write_log(tmp_path / "t.log", [
            '15/Mar/2024:10:30:45 "GET / HTTP/1.1" 200',
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp == datetime(2024, 3, 15, 10, 30, 45)


# ---------------------------------------------------------------------------
# Space-separated with fractional seconds
# ---------------------------------------------------------------------------

class TestSpaceFrac:

    def test_comma_separator(self, tmp_path):
        mtime = datetime(2024, 1, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-01-01 08:00:00,123 INFO starting",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp == datetime(2024, 1, 1, 8, 0, 0, 123000)

    def test_dot_separator(self, tmp_path):
        mtime = datetime(2024, 1, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-01-01 08:00:00.750 INFO starting",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp == datetime(2024, 1, 1, 8, 0, 0, 750000)

    def test_microseconds_truncated(self, tmp_path):
        mtime = datetime(2024, 1, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-01-01 08:00:00,123456789 INFO starting",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp.microsecond == 123456


# ---------------------------------------------------------------------------
# Space-separated datetime (no fractional)
# ---------------------------------------------------------------------------

class TestSpaceDatetime:

    def test_basic_parsing(self, tmp_path):
        mtime = datetime(2024, 2, 20)
        fi = write_log(tmp_path / "t.log", [
            "2024-02-20 14:05:33 ERROR db connection failed",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp == datetime(2024, 2, 20, 14, 5, 33)

    def test_text_after_timestamp_stripped(self, tmp_path):
        mtime = datetime(2024, 2, 20)
        fi = write_log(tmp_path / "t.log", [
            "2024-02-20 14:05:33 ERROR db connection failed",
        ], mtime)
        results = parse_all(fi)
        assert results[0].text == "ERROR db connection failed"


# ---------------------------------------------------------------------------
# Syslog — no year, year rollover
# ---------------------------------------------------------------------------

class TestSyslog:

    def test_same_month_uses_mtime_year(self, tmp_path):
        # File modified in March 2024, log shows March → year 2024
        mtime = datetime(2024, 3, 10)
        fi = write_log(tmp_path / "t.log", [
            "Mar  5 08:00:00 host app: started",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp.year == 2024
        assert results[0].timestamp.month == 3
        assert results[0].timestamp.day == 5

    def test_earlier_month_uses_mtime_year(self, tmp_path):
        # File modified in March 2024, log shows January → year 2024
        mtime = datetime(2024, 3, 10)
        fi = write_log(tmp_path / "t.log", [
            "Jan 20 10:00:00 host app: msg",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp.year == 2024

    def test_later_month_triggers_year_rollover(self, tmp_path):
        # File modified in January 2024, log shows December → year 2023
        mtime = datetime(2024, 1, 5)
        fi = write_log(tmp_path / "t.log", [
            "Dec 31 23:59:59 host app: last entry of year",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp.year == 2023
        assert results[0].timestamp.month == 12
        assert results[0].timestamp.day == 31

    def test_single_digit_day_padded(self, tmp_path):
        mtime = datetime(2024, 6, 1)
        fi = write_log(tmp_path / "t.log", [
            "Jun  5 09:00:00 host app: msg",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp == datetime(2024, 6, 5, 9, 0, 0)

    def test_multiple_lines_mixed_months(self, tmp_path):
        # File mtime Jan 2024; Dec lines → 2023, Jan lines → 2024
        mtime = datetime(2024, 1, 5)
        fi = write_log(tmp_path / "t.log", [
            "Dec 31 23:59:59 host app: end of year",
            "Jan  1 00:00:01 host app: new year",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp.year == 2023
        assert results[1].timestamp.year == 2024


# ---------------------------------------------------------------------------
# Date only
# ---------------------------------------------------------------------------

class TestDateOnly:

    def test_time_defaults_to_midnight(self, tmp_path):
        mtime = datetime(2024, 5, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-05-01 INFO daily summary",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp == datetime(2024, 5, 1, 0, 0, 0)

    def test_text_stripped_correctly(self, tmp_path):
        mtime = datetime(2024, 5, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-05-01 INFO daily summary",
        ], mtime)
        results = parse_all(fi)
        assert results[0].text == "INFO daily summary"


# ---------------------------------------------------------------------------
# Time only — day rollover
# ---------------------------------------------------------------------------

class TestTimeOnly:

    def test_uses_mtime_date_as_base(self, tmp_path):
        mtime = datetime(2024, 7, 4, 12, 0, 0)
        fi = write_log(tmp_path / "t.log", [
            "08:00:00 INFO start",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp.date() == date(2024, 7, 4)
        assert results[0].timestamp.time() == time(8, 0, 0)

    def test_day_rollover_on_time_regression(self, tmp_path):
        mtime = datetime(2024, 7, 4, 22, 0, 0)
        fi = write_log(tmp_path / "t.log", [
            "23:59:58 INFO last before midnight",
            "00:00:01 INFO first after midnight",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp.date() == date(2024, 7, 4)
        assert results[1].timestamp.date() == date(2024, 7, 5)

    def test_no_rollover_when_time_advances(self, tmp_path):
        mtime = datetime(2024, 7, 4, 0, 0, 0)
        fi = write_log(tmp_path / "t.log", [
            "08:00:00 INFO morning",
            "12:00:00 INFO noon",
            "18:00:00 INFO evening",
        ], mtime)
        results = parse_all(fi)
        assert all(r.timestamp.date() == date(2024, 7, 4) for r in results)

    def test_time_frac_day_rollover(self, tmp_path):
        mtime = datetime(2024, 7, 4, 22, 0, 0)
        fi = write_log(tmp_path / "t.log", [
            "23:59:59.900 INFO near midnight",
            "00:00:00.100 INFO just past midnight",
        ], mtime)
        results = parse_all(fi)
        assert results[1].timestamp.date() == date(2024, 7, 5)

    def test_multiple_rollovers(self, tmp_path):
        mtime = datetime(2024, 7, 1, 0, 0, 0)
        fi = write_log(tmp_path / "t.log", [
            "23:00:00 INFO day 1",
            "01:00:00 INFO day 2",  # rollover → July 2
            "23:30:00 INFO still day 2",
            "02:00:00 INFO day 3",  # rollover → July 3
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp.date() == date(2024, 7, 1)
        assert results[1].timestamp.date() == date(2024, 7, 2)
        assert results[2].timestamp.date() == date(2024, 7, 2)
        assert results[3].timestamp.date() == date(2024, 7, 3)


# ---------------------------------------------------------------------------
# No-timestamp fallback — mtime base + 100 ms per line
# ---------------------------------------------------------------------------

class TestNoTimestampFallback:

    def test_first_line_gets_mtime(self, tmp_path):
        mtime = datetime(2024, 4, 10, 9, 0, 0)
        fi = write_log(tmp_path / "t.log", [
            "plain text no timestamp",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp == mtime

    def test_subsequent_lines_increment_100ms(self, tmp_path):
        mtime = datetime(2024, 4, 10, 9, 0, 0)
        fi = write_log(tmp_path / "t.log", [
            "line one",
            "line two",
            "line three",
        ], mtime)
        results = parse_all(fi)
        assert results[0].timestamp == mtime
        assert results[1].timestamp == mtime + timedelta(milliseconds=100)
        assert results[2].timestamp == mtime + timedelta(milliseconds=200)

    def test_no_timestamp_file_preserves_order(self, tmp_path):
        mtime = datetime(2024, 4, 10, 9, 0, 0)
        fi = write_log(tmp_path / "t.log", ["line"] * 5, mtime)
        results = parse_all(fi)
        timestamps = [r.timestamp for r in results]
        assert timestamps == sorted(timestamps)

    def test_line_without_match_in_detected_format_falls_back(self, tmp_path):
        # Mostly space_datetime lines, but one continuation line
        mtime = datetime(2024, 4, 10, 9, 0, 0)
        fi = write_log(tmp_path / "t.log", [
            "2024-04-10 09:00:00 ERROR something bad",
            "    at com.example.Foo.bar(Foo.java:42)",   # no timestamp
            "2024-04-10 09:00:01 INFO recovered",
        ], mtime)
        results = parse_all(fi)
        # Line 1: parsed timestamp
        assert results[0].timestamp == datetime(2024, 4, 10, 9, 0, 0)
        # Line 2: fallback = mtime + 100 ms * (2-1) = mtime + 100 ms
        assert results[1].timestamp == mtime + timedelta(milliseconds=100)
        # Line 3: parsed timestamp
        assert results[2].timestamp == datetime(2024, 4, 10, 9, 0, 1)


# ---------------------------------------------------------------------------
# Text stripping and tab replacement
# ---------------------------------------------------------------------------

class TestTextProcessing:

    def test_timestamp_stripped_from_start(self, tmp_path):
        mtime = datetime(2024, 1, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-01-01 10:00:00 INFO application started",
        ], mtime)
        results = parse_all(fi)
        assert results[0].text == "INFO application started"

    def test_tabs_replaced_with_spaces(self, tmp_path):
        mtime = datetime(2024, 1, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-01-01 10:00:00 col1\tcol2\tcol3",
        ], mtime)
        results = parse_all(fi)
        assert "\t" not in results[0].text
        assert "col1    col2    col3" == results[0].text

    def test_empty_line_produces_empty_text(self, tmp_path):
        mtime = datetime(2024, 1, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-01-01 10:00:00 INFO msg",
            "",
            "2024-01-01 10:00:01 INFO msg2",
        ], mtime)
        results = parse_all(fi)
        assert results[1].text == ""

    def test_timestamp_in_middle_of_line(self, tmp_path):
        # Syslog has "hostname" before the timestamp is sometimes absent,
        # but test that text before the match is preserved.
        mtime = datetime(2024, 6, 1)
        fi = write_log(tmp_path / "t.log", [
            "Jan 15 10:30:45 host app: connected",
        ], mtime)
        results = parse_all(fi)
        assert "host app: connected" in results[0].text


# ---------------------------------------------------------------------------
# Line numbering
# ---------------------------------------------------------------------------

class TestLineNumbering:

    def test_line_numbers_are_one_based(self, tmp_path):
        mtime = datetime(2024, 1, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-01-01 10:00:00 line A",
            "2024-01-01 10:00:01 line B",
            "2024-01-01 10:00:02 line C",
        ], mtime)
        results = parse_all(fi)
        assert [r.line_no for r in results] == [1, 2, 3]

    def test_line_numbers_span_buffer_boundary(self, tmp_path):
        """Lines beyond DETECT_SAMPLE_LINES still get correct line numbers."""
        mtime = datetime(2024, 1, 1)
        n = DETECT_SAMPLE_LINES + 10
        lines = [f"2024-01-01 10:00:{i:02d} INFO line {i}" for i in range(n)]
        fi = write_log(tmp_path / "t.log", lines, mtime)
        results = parse_all(fi)
        assert len(results) == n
        assert [r.line_no for r in results] == list(range(1, n + 1))


# ---------------------------------------------------------------------------
# File edge cases
# ---------------------------------------------------------------------------

class TestFileEdgeCases:

    def test_empty_file_yields_nothing(self, tmp_path):
        mtime = datetime(2024, 1, 1)
        fi = write_log(tmp_path / "empty.log", [], mtime)
        assert parse_all(fi) == []

    def test_missing_file_yields_nothing(self, tmp_path):
        fi = FileInfo(
            path=tmp_path / "nonexistent.log",
            size_bytes=0,
            mtime=datetime(2024, 1, 1),
        )
        assert parse_all(fi) == []

    def test_file_with_only_blank_lines(self, tmp_path):
        mtime = datetime(2024, 1, 1)
        fi = write_log(tmp_path / "t.log", ["", "   ", ""], mtime)
        results = parse_all(fi)
        # 3 lines, all fall back to mtime + 100ms increment
        assert len(results) == 3
        assert results[0].timestamp == mtime
        assert results[1].timestamp == mtime + timedelta(milliseconds=100)

    def test_large_file_beyond_sample_buffer(self, tmp_path):
        """Detection uses first DETECT_SAMPLE_LINES non-empty lines;
        remaining lines are processed with the same detected format."""
        mtime = datetime(2024, 1, 1)
        n = DETECT_SAMPLE_LINES * 3
        lines = [f"2024-01-01 10:00:00 INFO line {i}" for i in range(n)]
        fi = write_log(tmp_path / "big.log", lines, mtime)
        results = parse_all(fi)
        assert len(results) == n
        # All lines should have parsed timestamps (not fallback)
        for r in results:
            assert r.timestamp == datetime(2024, 1, 1, 10, 0, 0)

# ---------------------------------------------------------------------------
# Custom format registration
# ---------------------------------------------------------------------------

class TestRegisterFormat:

    def test_custom_format_detected(self):
        r = TimestampResolver()
        r.register_format(
            name="bracket_slash",
            regex=r"\[(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\]",
            strptime="%Y/%m/%d %H:%M:%S",
        )
        assert r.detect_format_name([
            "[2024/03/15 10:30:45] INFO application started",
        ]) == "bracket_slash"

    def test_custom_format_parses_correctly(self, tmp_path):
        r = TimestampResolver()
        r.register_format(
            name="bracket_slash",
            regex=r"\[(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\]",
            strptime="%Y/%m/%d %H:%M:%S",
        )
        mtime = datetime(2024, 3, 15)
        fi = write_log(tmp_path / "t.log", [
            "[2024/03/15 10:30:45] INFO application started",
            "[2024/03/15 10:30:46] WARN something slow",
        ], mtime)
        results = list(r.iter_parsed_lines(fi))
        assert results[0].timestamp == datetime(2024, 3, 15, 10, 30, 45)
        assert results[1].timestamp == datetime(2024, 3, 15, 10, 30, 46)

    def test_custom_format_strips_full_match_including_brackets(self, tmp_path):
        r = TimestampResolver()
        r.register_format(
            name="bracket_slash",
            regex=r"\[(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2})\]",
            strptime="%Y/%m/%d %H:%M:%S",
        )
        mtime = datetime(2024, 3, 15)
        fi = write_log(tmp_path / "t.log", [
            "[2024/03/15 10:30:45] INFO msg",
        ], mtime)
        results = list(r.iter_parsed_lines(fi))
        assert results[0].text == "INFO msg"
        assert "[" not in results[0].text

    def test_custom_format_utc_timezone(self, tmp_path):
        r = TimestampResolver()
        r.register_format(
            name="utc_format",
            regex=r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) UTC",
            strptime="%Y-%m-%d %H:%M:%S",
            timezone="utc",
        )
        mtime = datetime(2024, 1, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-01-01 12:00:00 UTC INFO event",
        ], mtime)
        results = list(r.iter_parsed_lines(fi))
        expected = utc_to_local(datetime(2024, 1, 1, 12, 0, 0))
        assert results[0].timestamp == expected

    def test_insert_before_positions_correctly(self):
        r = TimestampResolver()
        r.register_format(
            name="custom_early",
            regex=r"\d{4}/\d{2}/\d{2}",
            strptime="%Y/%m/%d",
            insert_before="syslog",
        )
        names = r.format_names()
        assert names.index("custom_early") < names.index("syslog")
        assert names.index("custom_early") > names.index("apache")

    def test_insert_before_none_gives_highest_priority(self):
        r = TimestampResolver()
        r.register_format(
            name="my_custom",
            regex=r"\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}",
            strptime="%Y/%m/%d %H:%M:%S",
            insert_before=None,
        )
        assert r.format_names()[0] == "my_custom"

    def test_insert_before_unknown_name_raises(self):
        r = TimestampResolver()
        with pytest.raises(ValueError, match="insert_before="):
            r.register_format(
                name="x",
                regex=r"\d+",
                strptime="%Y",
                insert_before="nonexistent_format",
            )

    def test_instances_are_independent(self):
        r1 = TimestampResolver()
        r2 = TimestampResolver()
        r1.register_format(
            name="only_in_r1",
            regex=r"\d{4}/\d{2}/\d{2}",
            strptime="%Y/%m/%d",
        )
        assert "only_in_r1" in r1.format_names()
        assert "only_in_r1" not in r2.format_names()

    def test_builtin_formats_still_work_after_registration(self, tmp_path):
        r = TimestampResolver()
        r.register_format(
            name="my_custom",
            regex=r"\d{4}/\d{2}/\d{2}",
            strptime="%Y/%m/%d",
        )
        mtime = datetime(2024, 1, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-01-01 09:00:00 INFO standard format still works",
        ], mtime)
        results = list(r.iter_parsed_lines(fi))
        assert results[0].timestamp == datetime(2024, 1, 1, 9, 0, 0)

    def test_custom_wins_over_builtin_when_more_specific(self, tmp_path):
        # Register a format that matches the same lines as space_datetime
        # but with higher priority — the custom one should win.
        r = TimestampResolver()
        r.register_format(
            name="custom_priority",
            regex=r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})",
            strptime="%Y-%m-%d %H:%M:%S",
            insert_before=None,  # highest priority
        )
        assert r.detect_format_name([
            "2024-01-01 10:00:00 INFO msg",
        ]) == "custom_priority"

    def test_format_names_returns_priority_ordered_list(self):
        r = TimestampResolver()
        names = r.format_names()
        # Built-in ordering: iso_with_tz is first, time_only is last
        assert names[0] == "iso_with_tz"
        assert names[-1] == "time_only"
        assert len(names) == 18  # 18 built-in formats

    def test_result_is_namedtuple_with_correct_fields(self, tmp_path):
        mtime = datetime(2024, 1, 1)
        fi = write_log(tmp_path / "t.log", [
            "2024-01-01 08:00:00 INFO hello",
        ], mtime)
        result = parse_all(fi)[0]
        assert isinstance(result, ParsedLine)
        assert isinstance(result.line_no, int)
        assert isinstance(result.timestamp, datetime)
        assert isinstance(result.text, str)
        assert result.timestamp.tzinfo is None  # always naive local


# ---------------------------------------------------------------------------
# Loghub formats (added from D:\loghub-master survey)
# ---------------------------------------------------------------------------


class TestApacheCommon:
    """[Sun Dec 04 04:47:44 2005] style (Apache combined log)."""

    def test_detected(self):
        lines = [
            "[Sun Dec 04 04:47:44 2005] [notice] workerEnv.init() ok",
            "[Mon Dec 05 12:00:01 2005] [error] something failed",
        ]
        assert resolver.detect_format_name(lines) == "apache_common"

    def test_parsed_datetime(self, tmp_path):
        mtime = datetime(2005, 12, 10)
        fi = write_log(tmp_path / "apache.log", [
            "[Sun Dec 04 04:47:44 2005] [notice] workerEnv.init() ok",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert pl.timestamp == datetime(2005, 12, 4, 4, 47, 44)

    def test_timestamp_stripped(self, tmp_path):
        mtime = datetime(2005, 12, 10)
        fi = write_log(tmp_path / "apache.log", [
            "[Sun Dec 04 04:47:44 2005] [error] mod_jk failed",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert "2005" not in pl.text
        assert "[error]" in pl.text

    def test_single_digit_day(self, tmp_path):
        mtime = datetime(2005, 12, 10)
        fi = write_log(tmp_path / "apache.log", [
            "[Fri Dec  4 04:47:44 2005] [notice] workerEnv.init() ok",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert pl.timestamp.day == 4


class TestBglDatetime:
    """2005-06-03-15.42.50.675872 style (BGL supercomputer logs)."""

    def test_detected(self):
        lines = [
            "- 1117838570 2005.06.03 R02-M1 2005-06-03-15.42.50.675872 INFO msg",
            "- 1117838573 2005.06.03 R02-M1 2005-06-03-15.42.53.276129 INFO msg",
        ]
        assert resolver.detect_format_name(lines) == "bgl_datetime"

    def test_parsed_datetime(self, tmp_path):
        mtime = datetime(2005, 6, 10)
        fi = write_log(tmp_path / "bgl.log", [
            "- 1117838570 2005.06.03 R02-M1 2005-06-03-15.42.50.675872 INFO x",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert pl.timestamp == datetime(2005, 6, 3, 15, 42, 50, 675872)

    def test_without_microseconds(self, tmp_path):
        mtime = datetime(2005, 6, 10)
        fi = write_log(tmp_path / "bgl.log", [
            "- 1117838570 2005.06.03 R02-M1 2005-06-03-15.42.50 INFO x",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert pl.timestamp == datetime(2005, 6, 3, 15, 42, 50)

    def test_wins_over_dot_date(self):
        lines = [
            "- 1117838570 2005.06.03 R02 2005-06-03-15.42.50.675872 x",
            "- 1117838573 2005.06.03 R02 2005-06-03-15.42.53.276129 x",
        ]
        assert resolver.detect_format_name(lines) == "bgl_datetime"


class TestHealthApp:
    """20171223-22:15:29:606 style (HealthApp logs)."""

    def test_detected(self):
        lines = [
            "20171223-22:15:29:606|Step_LSC|30002312|onStandStepChanged",
            "20171223-22:15:29:615|Step_LSC|30002312|onExtend:1514038530000",
        ]
        assert resolver.detect_format_name(lines) == "healthapp"

    def test_parsed_datetime(self, tmp_path):
        mtime = datetime(2017, 12, 25)
        fi = write_log(tmp_path / "health.log", [
            "20171223-22:15:29:606|Step_LSC|30002312|onStandStepChanged",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert pl.timestamp == datetime(2017, 12, 23, 22, 15, 29, 606000)

    def test_timestamp_stripped(self, tmp_path):
        mtime = datetime(2017, 12, 25)
        fi = write_log(tmp_path / "health.log", [
            "20171223-22:15:29:606|Step_LSC|message",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert "20171223" not in pl.text
        assert "Step_LSC" in pl.text


class TestHdfsCompact:
    """081109 203615 style (HDFS logs — YYMMDD HHmmss)."""

    def test_detected(self):
        lines = [
            "081109 203615 148 INFO dfs.DataNode$PacketResponder: Received",
            "081109 203807 222 INFO dfs.DataNode$PacketResponder: Sending",
        ]
        assert resolver.detect_format_name(lines) == "hdfs_compact"

    def test_parsed_datetime(self, tmp_path):
        mtime = datetime(2008, 11, 15)
        fi = write_log(tmp_path / "hdfs.log", [
            "081109 203615 148 INFO dfs.DataNode: received block",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert pl.timestamp == datetime(2008, 11, 9, 20, 36, 15)

    def test_timestamp_stripped(self, tmp_path):
        mtime = datetime(2008, 11, 15)
        fi = write_log(tmp_path / "hdfs.log", [
            "081109 203615 148 INFO dfs.DataNode: msg",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert "081109" not in pl.text
        assert "INFO" in pl.text


class TestSpark:
    """17/06/09 20:10:40 style (Spark logs — YY/MM/DD HH:MM:SS)."""

    def test_detected(self):
        lines = [
            "17/06/09 20:10:40 INFO executor.CoarseGrainedExecutorBackend: Started",
            "17/06/09 20:10:40 INFO spark.SecurityManager: Changing view acls",
        ]
        assert resolver.detect_format_name(lines) == "spark"

    def test_parsed_datetime(self, tmp_path):
        mtime = datetime(2017, 6, 15)
        fi = write_log(tmp_path / "spark.log", [
            "17/06/09 20:10:40 INFO executor.CoarseGrainedExecutorBackend: Started",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert pl.timestamp == datetime(2017, 6, 9, 20, 10, 40)

    def test_timestamp_stripped(self, tmp_path):
        mtime = datetime(2017, 6, 15)
        fi = write_log(tmp_path / "spark.log", [
            "17/06/09 20:10:40 INFO SparkContext: Running Spark",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert "17/06/09" not in pl.text
        assert "INFO" in pl.text


class TestAndroid:
    """03-17 16:13:38.811 style (Android logcat — MM-DD HH:MM:SS.mmm, no year)."""

    def test_detected(self):
        lines = [
            "03-17 16:13:38.811  1702  2395 D WindowManager: layoutWindowLw",
            "03-17 16:13:38.819  1702  8671 D PowerManagerService: release",
        ]
        assert resolver.detect_format_name(lines) == "android"

    def test_detected_from_structured_csv_row(self):
        lines = [
            "1,03-17,16:13:38.811,1702,2395,D,WindowManager,layoutWindowLw",
            "2,03-17,16:13:38.819,1702,8671,D,PowerManagerService,release",
        ]
        assert resolver.detect_format_name(lines) == "android"

    def test_parsed_datetime_uses_mtime_year(self, tmp_path):
        mtime = datetime(2024, 6, 1)
        fi = write_log(tmp_path / "android.log", [
            "03-17 16:13:38.811  1702  2395 D WindowManager: layoutWindowLw",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert pl.timestamp.year == 2024
        assert pl.timestamp == datetime(2024, 3, 17, 16, 13, 38, 811000)

    def test_structured_csv_datetime_uses_mtime_year(self, tmp_path):
        mtime = datetime(2024, 6, 1)
        fi = write_log(tmp_path / "android.csv", [
            "LineId,Date,Time,Pid,Tid,Level,Component,Content",
            "1,03-17,16:13:38.811,1702,2395,D,WindowManager,layoutWindowLw",
        ], mtime)
        lines = list(resolver.iter_parsed_lines(fi))
        assert lines[1].timestamp == datetime(2024, 3, 17, 16, 13, 38, 811000)
        assert "03-17" not in lines[1].text
        assert "16:13:38.811" not in lines[1].text
        assert "WindowManager" in lines[1].text

    def test_year_rollover(self, tmp_path):
        # File mtime in January; log month is December → prior year
        mtime = datetime(2024, 1, 15)
        fi = write_log(tmp_path / "android.log", [
            "12-25 10:00:00.000  100  200 D Tag: msg",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert pl.timestamp.year == 2023

    def test_milliseconds_parsed(self, tmp_path):
        mtime = datetime(2024, 6, 1)
        fi = write_log(tmp_path / "android.log", [
            "03-17 16:13:38.123  100  200 D Tag: msg",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert pl.timestamp.microsecond == 123000

    def test_timestamp_stripped(self, tmp_path):
        mtime = datetime(2024, 6, 1)
        fi = write_log(tmp_path / "android.log", [
            "03-17 16:13:38.811  1702  2395 D WindowManager: layoutWindowLw",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert "03-17" not in pl.text
        assert "WindowManager" in pl.text


class TestProxifier:
    """[10.30 16:49:06] style (Proxifier logs — [MM.DD HH:MM:SS], no year)."""

    def test_detected(self):
        lines = [
            "[10.30 16:49:06] chrome.exe - proxy.example.com:5070 open",
            "[10.30 16:49:07] chrome.exe - proxy.example.com:5070 closed",
        ]
        assert resolver.detect_format_name(lines) == "proxifier"

    def test_parsed_datetime_uses_mtime_year(self, tmp_path):
        mtime = datetime(2024, 11, 1)
        fi = write_log(tmp_path / "proxifier.log", [
            "[10.30 16:49:06] chrome.exe - proxy.example.com:5070 open",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert pl.timestamp == datetime(2024, 10, 30, 16, 49, 6)

    def test_year_rollover(self, tmp_path):
        mtime = datetime(2024, 1, 15)
        fi = write_log(tmp_path / "proxifier.log", [
            "[12.25 10:00:00] app.exe - proxy:80 open",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert pl.timestamp.year == 2023

    def test_timestamp_stripped(self, tmp_path):
        mtime = datetime(2024, 11, 1)
        fi = write_log(tmp_path / "proxifier.log", [
            "[10.30 16:49:06] chrome.exe - proxy:5070 open",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert "[10.30" not in pl.text
        assert "chrome.exe" in pl.text


class TestDotDate:
    """2005.06.03 style (BGL/Thunderbird date-only with dot separators)."""

    def test_detected_when_no_richer_format(self):
        lines = [
            "2005.06.03 some message without time",
            "2005.06.04 another message",
        ]
        assert resolver.detect_format_name(lines) == "dot_date"

    def test_parsed_date(self, tmp_path):
        mtime = datetime(2005, 6, 10)
        fi = write_log(tmp_path / "dot.log", [
            "2005.06.03 some message here",
            "2005.06.04 another message",
        ], mtime)
        lines = list(resolver.iter_parsed_lines(fi))
        assert lines[0].timestamp == datetime(2005, 6, 3)
        assert lines[1].timestamp == datetime(2005, 6, 4)

    def test_syslog_beats_dot_date_for_thunderbird(self):
        # Thunderbird lines have both YYYY.MM.DD and Mon D HH:MM:SS
        lines = [
            "- 1131566461 2005.11.09 dn228 Nov 9 12:01:01 ...",
            "- 1131566462 2005.11.09 dn229 Nov 9 12:01:02 ...",
        ]
        # syslog has time info and comes before dot_date in priority
        assert resolver.detect_format_name(lines) == "syslog"

    def test_timestamp_stripped(self, tmp_path):
        mtime = datetime(2005, 6, 10)
        fi = write_log(tmp_path / "dot.log", [
            "2005.06.03 kernel panic at module xyz",
        ], mtime)
        pl = list(resolver.iter_parsed_lines(fi))[0]
        assert "2005.06.03" not in pl.text
        assert "kernel" in pl.text
