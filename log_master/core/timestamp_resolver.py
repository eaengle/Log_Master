"""
Timestamp format detection, parsing, and line normalization.

Each file is streamed exactly once.  The first DETECT_SAMPLE_LINES non-empty
lines are buffered to detect the dominant timestamp format; those buffered
lines plus the remainder of the file are then yielded as ParsedLine objects.

Format priority — most restrictive first so that a format that is a strict
superset of another (e.g. ISO-with-tz vs plain space-separated) is always
preferred when both would match the same lines.

Every line is guaranteed to have a timestamp:
  - Lines that match the detected format  → parsed timestamp.
  - Lines that don't match (or no format) → file mtime + (line_no - 1) * 100 ms.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone
from typing import Callable, Iterator, NamedTuple

from .file_finder import FileInfo

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

DETECT_SAMPLE_LINES: int = 40
DETECT_MIN_HIT_RATE: float = 0.50

# ---------------------------------------------------------------------------
# Internal month map
# ---------------------------------------------------------------------------

_MONTH_MAP: dict[str, int] = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4,  "May": 5,  "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}

_MONTH_ABBR = r"(?P<month_abbr>Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)"

# ---------------------------------------------------------------------------
# Public output type
# ---------------------------------------------------------------------------


class ParsedLine(NamedTuple):
    """One line from a log file after timestamp extraction."""

    line_no: int        # 1-based position in source file
    timestamp: datetime  # local, timezone-naive; always set
    text: str           # line with timestamp span removed; tabs → 4 spaces


# ---------------------------------------------------------------------------
# Per-file mutable state (day-rollover tracking for time-only formats)
# ---------------------------------------------------------------------------


class _ParseState:
    __slots__ = ("file_mtime", "running_date", "prev_time_of_day")

    def __init__(self, file_mtime: datetime) -> None:
        self.file_mtime = file_mtime
        self.running_date: date = file_mtime.date()
        self.prev_time_of_day: time | None = None


# ---------------------------------------------------------------------------
# Format descriptor
# ---------------------------------------------------------------------------


class _Format(NamedTuple):
    name: str
    pattern: re.Pattern
    has_year: bool
    has_date: bool       # True when month + day are present
    produces_utc: bool   # True when the raw parsed value is already UTC
    parse: Callable[[re.Match, _ParseState], datetime]


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


def _frac_to_micros(frac: str | None) -> int:
    """Convert a fractional-seconds string (e.g. '123', '123456789') to µs."""
    if not frac:
        return 0
    return int(frac[:6].ljust(6, "0"))


def _utc_naive_to_local(utc_dt: datetime) -> datetime:
    """Convert a naive UTC datetime to a naive local datetime."""
    return utc_dt.replace(tzinfo=timezone.utc).astimezone().replace(tzinfo=None)


def _parse_tz_offset_minutes(tz_str: str) -> int:
    """Return signed minutes east of UTC from strings like '+05:00' or '-0530'."""
    s = tz_str.replace(":", "")
    sign = 1 if s[0] == "+" else -1
    h = int(s[1:3])
    m = int(s[3:5]) if len(s) >= 5 else 0
    return sign * (h * 60 + m)


# ---------------------------------------------------------------------------
# Parser functions — one per format family
# ---------------------------------------------------------------------------


def _parse_iso_tz(m: re.Match, state: _ParseState) -> datetime:
    dt = datetime(
        int(m["year"]), int(m["month"]), int(m["day"]),
        int(m["hour"]), int(m["minute"]), int(m["second"]),
        _frac_to_micros(m.groupdict().get("frac")),
    )
    utc_dt = dt - timedelta(minutes=_parse_tz_offset_minutes(m["tz"]))
    return _utc_naive_to_local(utc_dt)


def _parse_iso_z(m: re.Match, state: _ParseState) -> datetime:
    utc_dt = datetime(
        int(m["year"]), int(m["month"]), int(m["day"]),
        int(m["hour"]), int(m["minute"]), int(m["second"]),
        _frac_to_micros(m.groupdict().get("frac")),
    )
    return _utc_naive_to_local(utc_dt)


def _parse_full_local(m: re.Match, state: _ParseState) -> datetime:
    """Full year+date+time with no timezone — treat directly as local."""
    return datetime(
        int(m["year"]), int(m["month"]), int(m["day"]),
        int(m["hour"]), int(m["minute"]), int(m["second"]),
        _frac_to_micros(m.groupdict().get("frac")),
    )


def _parse_apache(m: re.Match, state: _ParseState) -> datetime:
    dt = datetime(
        int(m["year"]), _MONTH_MAP[m["month_abbr"]], int(m["day"]),
        int(m["hour"]), int(m["minute"]), int(m["second"]),
    )
    tz = m.groupdict().get("tz")
    if tz:
        utc_dt = dt - timedelta(minutes=_parse_tz_offset_minutes(tz))
        return _utc_naive_to_local(utc_dt)
    return dt


def _parse_syslog(m: re.Match, state: _ParseState) -> datetime:
    """
    Syslog has no year.  Use file mtime year as the candidate; subtract 1 if
    the log month is later than the file's mtime month (year-boundary rollover,
    e.g. file modified in January but log entries show December).
    """
    month = _MONTH_MAP[m["month_abbr"]]
    year = state.file_mtime.year
    if month > state.file_mtime.month:
        year -= 1
    return datetime(year, month, int(m["day"]),
                    int(m["hour"]), int(m["minute"]), int(m["second"]))


def _parse_date_only(m: re.Match, state: _ParseState) -> datetime:
    return datetime(int(m["year"]), int(m["month"]), int(m["day"]))


def _parse_apache_common(m: re.Match, state: _ParseState) -> datetime:
    """Apache combined log: [Sun Dec 04 04:47:44 2005]"""
    return datetime(
        int(m["year"]), _MONTH_MAP[m["month_abbr"]], int(m["day"]),
        int(m["hour"]), int(m["minute"]), int(m["second"]),
    )


def _parse_bgl_datetime(m: re.Match, state: _ParseState) -> datetime:
    """BGL: 2005-06-03-15.42.50.675872 (dash-dot datetime with optional microseconds)"""
    return datetime(
        int(m["year"]), int(m["month"]), int(m["day"]),
        int(m["hour"]), int(m["minute"]), int(m["second"]),
        _frac_to_micros(m.groupdict().get("frac")),
    )


def _parse_healthapp(m: re.Match, state: _ParseState) -> datetime:
    """HealthApp: 20171223-22:15:29:606 (compact date, colon-delimited time+ms)"""
    return datetime(
        int(m["year"]), int(m["month"]), int(m["day"]),
        int(m["hour"]), int(m["minute"]), int(m["second"]),
        _frac_to_micros(m["frac"]),
    )


def _parse_hdfs_compact(m: re.Match, state: _ParseState) -> datetime:
    """HDFS: 081109 203615 (YYMMDD HHmmss, no separators)"""
    return datetime.strptime(f"{m['ymd']} {m['hms']}", "%y%m%d %H%M%S")


def _parse_spark(m: re.Match, state: _ParseState) -> datetime:
    """Spark: 17/06/09 20:10:40 (YY/MM/DD HH:MM:SS)"""
    return datetime.strptime(
        f"{m['yr']}/{m['month']}/{m['day']} {m['hour']}:{m['minute']}:{m['second']}",
        "%y/%m/%d %H:%M:%S",
    )


def _parse_android(m: re.Match, state: _ParseState) -> datetime:
    """Android: 03-17 16:13:38.811 (MM-DD HH:MM:SS.mmm, no year — infer from mtime)"""
    month = int(m["month"])
    year = state.file_mtime.year
    if month > state.file_mtime.month:
        year -= 1
    return datetime(year, month, int(m["day"]),
                    int(m["hour"]), int(m["minute"]), int(m["second"]),
                    _frac_to_micros(m["frac"]))


def _parse_proxifier(m: re.Match, state: _ParseState) -> datetime:
    """Proxifier: [10.30 16:49:06] (MM.DD HH:MM:SS in brackets, no year)"""
    month = int(m["month"])
    year = state.file_mtime.year
    if month > state.file_mtime.month:
        year -= 1
    return datetime(year, month, int(m["day"]),
                    int(m["hour"]), int(m["minute"]), int(m["second"]))


def _parse_dot_date(m: re.Match, state: _ParseState) -> datetime:
    """Dot-separated date only: 2005.06.03"""
    return datetime(int(m["year"]), int(m["month"]), int(m["day"]))


def _parse_time_of_day(m: re.Match, state: _ParseState) -> datetime:
    """
    Time-only format.  Use file mtime date as the base.  Detect day rollovers
    by watching for the time value to decrease between successive lines.
    """
    tod = time(
        int(m["hour"]), int(m["minute"]), int(m["second"]),
        _frac_to_micros(m.groupdict().get("frac")),
    )
    if state.prev_time_of_day is not None and tod < state.prev_time_of_day:
        state.running_date += timedelta(days=1)
    state.prev_time_of_day = tod
    return datetime.combine(state.running_date, tod)


# ---------------------------------------------------------------------------
# Format list — ordered most-restrictive → least-restrictive
# ---------------------------------------------------------------------------

_FORMATS: list[_Format] = [
    # 1. ISO 8601 with numeric tz offset  2024-01-15T10:30:45.123+05:00
    #    [T ] accepts both T-separator and space-separator variants.
    #    tz group is REQUIRED so this won't shadow format 3 (no-tz).
    _Format(
        "iso_with_tz",
        re.compile(
            r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})[T ]"
            r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
            r"(?:[.,](?P<frac>\d+))?"
            r"(?P<tz>[+-]\d{2}:?\d{2})"
        ),
        has_year=True, has_date=True, produces_utc=False,
        parse=_parse_iso_tz,
    ),

    # 2. ISO 8601 with Z                  2024-01-15T10:30:45.123Z
    _Format(
        "iso_with_z",
        re.compile(
            r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})[T ]"
            r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
            r"(?:[.,](?P<frac>\d+))?Z"
        ),
        has_year=True, has_date=True, produces_utc=True,
        parse=_parse_iso_z,
    ),

    # 3. ISO with T separator, no tz      2024-01-15T10:30:45.123
    #    Negative lookahead prevents matching when a tz char follows,
    #    ensuring formats 1 and 2 take precedence during per-line parsing.
    _Format(
        "iso_t_local",
        re.compile(
            r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})T"
            r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
            r"(?:[.,](?P<frac>\d+))?"
            r"(?![Z+\d-])"
        ),
        has_year=True, has_date=True, produces_utc=False,
        parse=_parse_full_local,
    ),

    # 4. Apache combined log               [Sun Dec 04 04:47:44 2005]
    #    Weekday name + bracket envelope makes this unambiguous; placed before
    #    the standard apache format so both can coexist in the priority list.
    _Format(
        "apache_common",
        re.compile(
            r"\[(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun) " + _MONTH_ABBR +
            r" {1,2}(?P<day>\d{1,2}) "
            r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2}) "
            r"(?P<year>\d{4})\]"
        ),
        has_year=True, has_date=True, produces_utc=False,
        parse=_parse_apache_common,
    ),

    # 5. Apache / Nginx                   15/Jan/2024:10:30:45 +0000
    _Format(
        "apache",
        re.compile(
            r"(?P<day>\d{2})/" + _MONTH_ABBR + r"/(?P<year>\d{4})"
            r":(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
            r"(?: (?P<tz>[+-]\d{4}))?"
        ),
        has_year=True, has_date=True, produces_utc=False,
        parse=_parse_apache,
    ),

    # 5. Space-separated with fractional  2024-01-15 10:30:45,123
    #    Fractional separator is REQUIRED so format 6 stays distinct.
    _Format(
        "space_frac",
        re.compile(
            r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2}) "
            r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
            r"[.,](?P<frac>\d+)"
        ),
        has_year=True, has_date=True, produces_utc=False,
        parse=_parse_full_local,
    ),

    # 6. Space-separated datetime         2024-01-15 10:30:45
    #    Lookahead prevents matching lines that belong to format 5.
    _Format(
        "space_datetime",
        re.compile(
            r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2}) "
            r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
            r"(?![.,])"
        ),
        has_year=True, has_date=True, produces_utc=False,
        parse=_parse_full_local,
    ),

    # 8. BGL dash-dot datetime             2005-06-03-15.42.50.675872
    #    Full year+date+time with unconventional separators; placed before
    #    date_only so it wins when both could match.
    _Format(
        "bgl_datetime",
        re.compile(
            r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
            r"-(?P<hour>\d{2})\.(?P<minute>\d{2})\.(?P<second>\d{2})"
            r"(?:\.(?P<frac>\d+))?"
        ),
        has_year=True, has_date=True, produces_utc=False,
        parse=_parse_bgl_datetime,
    ),

    # 9. HealthApp compact                20171223-22:15:29:606
    #    8-digit date run, dash, colon-separated HH:MM:SS:mmm.
    _Format(
        "healthapp",
        re.compile(
            r"(?P<year>\d{4})(?P<month>\d{2})(?P<day>\d{2})"
            r"-(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
            r":(?P<frac>\d{3})"
        ),
        has_year=True, has_date=True, produces_utc=False,
        parse=_parse_healthapp,
    ),

    # 10. HDFS compact                    081109 203615
    #     Six-digit date run (YYMMDD) followed by six-digit time run (HHmmss).
    _Format(
        "hdfs_compact",
        re.compile(r"(?<!\d)(?P<ymd>\d{6}) (?P<hms>\d{6})(?!\d)"),
        has_year=True, has_date=True, produces_utc=False,
        parse=_parse_hdfs_compact,
    ),

    # 11. Spark                           17/06/09 20:10:40
    #     Two-digit year with slash separators; placed before syslog (has year).
    _Format(
        "spark",
        re.compile(
            r"(?<!\d)(?P<yr>\d{2})/(?P<month>\d{2})/(?P<day>\d{2}) "
            r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
        ),
        has_year=True, has_date=True, produces_utc=False,
        parse=_parse_spark,
    ),

    # 12. Syslog — no year                Jan 15 10:30:45
    #    Day field is space-padded for single digits: "Jan  5 …"
    _Format(
        "syslog",
        re.compile(
            _MONTH_ABBR + r" {1,2}(?P<day>\d{1,2}) "
            r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
        ),
        has_year=False, has_date=True, produces_utc=False,
        parse=_parse_syslog,
    ),

    # 14. Android — no year               03-17 16:13:38.811
    #     MM-DD (numeric month) + HH:MM:SS.mmm; year inferred from mtime like syslog.
    #     Month validated to 01-12 to avoid false matches against other numeric runs.
    _Format(
        "android",
        re.compile(
            r"(?<!\d)(?P<month>0[1-9]|1[0-2])-(?P<day>0[1-9]|[12]\d|3[01])"
            r"[ ,]"
            r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
            r"\.(?P<frac>\d+)"
        ),
        has_year=False, has_date=True, produces_utc=False,
        parse=_parse_android,
    ),

    # 15. Proxifier — no year             [10.30 16:49:06]
    #     Bracket-enclosed MM.DD HH:MM:SS; year inferred from mtime.
    _Format(
        "proxifier",
        re.compile(
            r"\[(?P<month>\d{2})\.(?P<day>\d{2}) "
            r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})\]"
        ),
        has_year=False, has_date=True, produces_utc=False,
        parse=_parse_proxifier,
    ),

    # 16. Dot-separated date only         2005.06.03
    #     YYYY.MM.DD; appears in BGL and Thunderbird alongside richer formats.
    #     Placed after syslog so syslog (which carries time) wins when both match.
    _Format(
        "dot_date",
        re.compile(
            r"(?P<year>\d{4})\.(?P<month>\d{2})\.(?P<day>\d{2})(?!\d)"
        ),
        has_year=True, has_date=True, produces_utc=False,
        parse=_parse_dot_date,
    ),

    # 17. Date only                       2024-01-15
    #    Lookahead prevents matching the date portion of a full datetime:
    #    "[ T]\d" would indicate a time component follows.
    _Format(
        "date_only",
        re.compile(
            r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
            r"(?![ T]\d)"
        ),
        has_year=True, has_date=True, produces_utc=False,
        parse=_parse_date_only,
    ),

    # 18. Time with fractional seconds     10:30:45.123
    _Format(
        "time_frac",
        re.compile(
            r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
            r"[.,](?P<frac>\d+)"
        ),
        has_year=False, has_date=False, produces_utc=False,
        parse=_parse_time_of_day,
    ),

    # 19. Time only — least restrictive   10:30:45
    #     Lookahead prevents matching lines that belong to format 9.
    _Format(
        "time_only",
        re.compile(
            r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
            r"(?![.,\d])"
        ),
        has_year=False, has_date=False, produces_utc=False,
        parse=_parse_time_of_day,
    ),
]


# ---------------------------------------------------------------------------
# Core pipeline helpers (module-level so they are importable for testing)
# ---------------------------------------------------------------------------


def _make_strptime_parser(
    strptime_fmt: str, timezone: str
) -> Callable[[re.Match, _ParseState], datetime]:
    """
    Build a parse callable from a strptime format string.

    The regex match is used to locate and strip the timestamp span from the
    line.  If the regex contains a capture group, group(1) is passed to
    strptime; otherwise the full match (group 0) is used.  This lets callers
    write a regex that matches surrounding delimiters (e.g. brackets) while
    capturing only the parseable text.
    """
    def parser(m: re.Match, _state: _ParseState) -> datetime:
        ts_str = m.group(1) if m.lastindex and m.lastindex >= 1 else m.group(0)
        dt = datetime.strptime(ts_str, strptime_fmt)
        return _utc_naive_to_local(dt) if timezone == "utc" else dt
    return parser


def _detect_format(
    sample: list[str],
    non_empty_count: int,
    formats: list[_Format],
) -> _Format | None:
    """
    Return the first format (in priority order) whose hit rate on the non-empty
    lines of *sample* meets or exceeds DETECT_MIN_HIT_RATE, or None.
    """
    if non_empty_count == 0:
        return None
    for fmt in formats:
        hits = sum(
            1 for line in sample if line.strip() and fmt.pattern.search(line)
        )
        if hits / non_empty_count >= DETECT_MIN_HIT_RATE:
            return fmt
    return None


def _strip_match(line: str, m: re.Match) -> str:
    """Remove the matched span from *line* and strip surrounding whitespace."""
    return (line[: m.start()] + line[m.end() :]).strip()


def _make_parsed_line(
    line: str,
    line_no: int,
    fmt: _Format | None,
    state: _ParseState,
    file_mtime: datetime,
) -> ParsedLine:
    text = line
    ts: datetime

    if fmt is not None:
        m = fmt.pattern.search(line)
        if m is not None:
            ts = fmt.parse(m, state)
            text = _strip_match(line, m)
        else:
            # Line doesn't match the detected format (continuation line, etc.)
            ts = file_mtime + timedelta(milliseconds=(line_no - 1) * 100)
    else:
        # No timestamp format detected in this file
        ts = file_mtime + timedelta(milliseconds=(line_no - 1) * 100)

    return ParsedLine(
        line_no=line_no,
        timestamp=ts,
        text=text.replace("\t", "    "),
    )


# ---------------------------------------------------------------------------
# Public class
# ---------------------------------------------------------------------------


class TimestampResolver:
    """
    Detects the timestamp format used in a log file and yields ParsedLine
    objects for every line.  Each file is read exactly once.

    Each instance maintains its own ordered format list (a copy of the
    module-level defaults).  Use register_format() to add application-specific
    formats without affecting other instances or the defaults.
    """

    def __init__(self) -> None:
        self._formats: list[_Format] = list(_FORMATS)

    # ------------------------------------------------------------------
    # Format registration
    # ------------------------------------------------------------------

    def register_format(
        self,
        name: str,
        regex: str,
        strptime: str,
        insert_before: str | None = None,
        timezone: str = "local",
    ) -> None:
        """
        Register a custom timestamp format.

        Args:
            name:          Unique label used in detect_format_name() output
                           and diagnostics.
            regex:         Regular expression that locates the timestamp in a
                           line.  Use a capture group to isolate the text
                           passed to strptime; the full match is always
                           stripped from the output line.
                           Example: r"\\[(\\d{4}/\\d{2}/\\d{2} \\d{2}:\\d{2}:\\d{2})\\]"
            strptime:      Python strptime format string for the captured text.
                           Example: "%Y/%m/%d %H:%M:%S"
            insert_before: Name of an existing format to insert before
                           (higher priority).  None inserts at position 0
                           (highest priority — before all built-ins).
            timezone:      "local" (default) — treat parsed datetime as local.
                           "utc" — convert from UTC to local time.

        Raises:
            ValueError: if insert_before names a format that does not exist.
        """
        compiled = re.compile(regex)
        parser = _make_strptime_parser(strptime, timezone)
        fmt = _Format(
            name=name,
            pattern=compiled,
            has_year="%Y" in strptime or "%y" in strptime,
            has_date=any(c in strptime for c in ("%m", "%b", "%B", "%j")),
            produces_utc=timezone == "utc",
            parse=parser,
        )
        if insert_before is None:
            self._formats.insert(0, fmt)
        else:
            names = [f.name for f in self._formats]
            if insert_before not in names:
                raise ValueError(
                    f"insert_before={insert_before!r} not found; "
                    f"known formats: {names}"
                )
            self._formats.insert(names.index(insert_before), fmt)

    def format_names(self) -> list[str]:
        """Return the current format priority list (highest priority first)."""
        return [f.name for f in self._formats]

    # ------------------------------------------------------------------
    # Detection and parsing
    # ------------------------------------------------------------------

    def detect_format_name(self, lines: list[str]) -> str | None:
        """
        Return the name of the winning format given a list of sample lines,
        or None if no format reaches the hit threshold.  Intended for tests
        and diagnostics — callers do not need to open a file.
        """
        non_empty = sum(1 for ln in lines if ln.strip())
        fmt = _detect_format(lines, non_empty, self._formats)
        return fmt.name if fmt is not None else None

    def iter_parsed_lines(self, file_info: FileInfo) -> Iterator[ParsedLine]:
        """
        Stream every line of *file_info.path* as a ParsedLine.

        Phase 1 — buffer up to DETECT_SAMPLE_LINES non-empty lines and detect
                   the dominant timestamp format.
        Phase 2 — yield all buffered lines then continue streaming the rest of
                   the file, applying the detected format to each line.
        """
        try:
            fobj = open(file_info.path, encoding="utf-8", errors="replace")
        except OSError:
            return

        with fobj as f:
            # --- Phase 1: buffer sample lines ---
            buffered: list[str] = []
            non_empty = 0
            for raw in f:
                line = raw.rstrip("\n\r")
                buffered.append(line)
                if line.strip():
                    non_empty += 1
                if non_empty >= DETECT_SAMPLE_LINES:
                    break

            fmt = _detect_format(buffered, non_empty, self._formats)
            state = _ParseState(file_info.mtime)
            line_no = 0

            # --- Phase 2a: yield buffered lines ---
            for line in buffered:
                line_no += 1
                yield _make_parsed_line(line, line_no, fmt, state, file_info.mtime)

            # --- Phase 2b: stream remainder of file ---
            for raw in f:
                line_no += 1
                yield _make_parsed_line(
                    raw.rstrip("\n\r"), line_no, fmt, state, file_info.mtime
                )
