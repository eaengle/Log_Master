"""
Expression-based line matching with time filtering, context capture,
and file-skip support.

Every ParsedLine is evaluated exactly once in this order:
  1. Time range  — reject if timestamp outside [time_from, time_to]
  2. Exclude     — reject if ANY exclude pattern matches
  3. Include     — accept if ANY include pattern matches (or no patterns given)

File-skip uses a deferred-commit pattern: skip patterns are checked on every
line during the same streaming pass as the main analysis.  If a skip pattern
fires at any point, the accumulated results are discarded at EOF and the file
is reported as skipped — the file is never read twice.

Context lines are collected with a rolling deque (before) and a pending-match
queue (after).  Context never crosses file boundaries.
"""

from __future__ import annotations

import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import NamedTuple

from .file_finder import FileInfo
from .timestamp_resolver import ParsedLine, TimestampResolver

# ContextLine reuses ParsedLine — both carry (line_no, timestamp, text).
ContextLine = ParsedLine


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SearchConfig:
    """
    Immutable search configuration.  All pattern strings are compiled once
    inside ExpressionAnalyzer.__init__ — callers pass raw regex strings.
    """

    include_patterns: tuple[str, ...] = ()
    exclude_patterns: tuple[str, ...] = ()
    skip_file_patterns: tuple[str, ...] = ()
    time_from: datetime | None = None
    time_to: datetime | None = None
    case_sensitive: bool = True
    context_lines: int = 0


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MatchResult:
    """One accepted line from one source file."""

    source_file: Path
    line_no: int
    timestamp: datetime
    text: str
    matched_patterns: frozenset[str]          # include pattern strings that triggered
    context_before: tuple[ContextLine, ...]   # up to context_lines ParsedLines before
    context_after: tuple[ContextLine, ...]    # up to context_lines ParsedLines after


class FileAnalysisResult(NamedTuple):
    matches: list[MatchResult]
    was_skipped: bool


# ---------------------------------------------------------------------------
# Internal: pending match accumulator
# ---------------------------------------------------------------------------


@dataclass
class _PendingMatch:
    """
    A match that has been accepted but is still collecting its context_after
    lines.  Converted to an immutable MatchResult once needed_after reaches 0
    or the file ends.
    """

    source_file: Path
    line_no: int
    timestamp: datetime
    text: str
    matched_patterns: frozenset[str]
    context_before: tuple[ContextLine, ...]
    context_after: list[ContextLine] = field(default_factory=list)
    needed_after: int = 0

    def to_result(self) -> MatchResult:
        return MatchResult(
            source_file=self.source_file,
            line_no=self.line_no,
            timestamp=self.timestamp,
            text=self.text,
            matched_patterns=self.matched_patterns,
            context_before=self.context_before,
            context_after=tuple(self.context_after),
        )


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class ExpressionAnalyzer:
    """
    Evaluates a log file against a SearchConfig in a single streaming pass.

    Patterns are pre-compiled at construction time.  The same analyzer
    instance can be reused across multiple files.
    """

    def __init__(self, config: SearchConfig) -> None:
        flags = 0 if config.case_sensitive else re.IGNORECASE
        self._config = config
        # Store (raw_string, compiled) so matched_patterns can record the
        # original regex string the caller supplied.
        self._include: list[tuple[str, re.Pattern]] = [
            (s, re.compile(s, flags)) for s in config.include_patterns
        ]
        self._exclude: list[re.Pattern] = [
            re.compile(s, flags) for s in config.exclude_patterns
        ]
        self._skip: list[re.Pattern] = [
            re.compile(s, flags) for s in config.skip_file_patterns
        ]

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def analyze_file(
        self,
        file_info: FileInfo,
        resolver: TimestampResolver,
    ) -> FileAnalysisResult:
        """
        Stream *file_info* through *resolver* in a single pass.

        Returns FileAnalysisResult with was_skipped=True (and empty matches)
        if a skip_file_pattern fires at any point in the file.
        """
        cfg = self._config
        results: list[MatchResult] = []
        skip_triggered = False

        # Rolling buffer of recent ParsedLines — supplies context_before.
        # maxlen=0 means the deque never retains items (correct for context=0).
        ctx_buf: deque[ParsedLine] = deque(maxlen=cfg.context_lines)

        # Matches that are still collecting their context_after ParsedLines.
        pending: deque[_PendingMatch] = deque()

        for pl in resolver.iter_parsed_lines(file_info):

            # Step 1 — skip-file check (runs until triggered, then stops)
            if not skip_triggered and self._skip:
                if any(p.search(pl.text) for p in self._skip):
                    skip_triggered = True

            # Step 2 — deliver this line as context_after to pending matches
            for pm in pending:
                if pm.needed_after > 0:
                    pm.context_after.append(pl)
                    pm.needed_after -= 1

            # Step 3 — flush pending matches whose context window is complete
            while pending and pending[0].needed_after == 0:
                results.append(pending.popleft().to_result())

            # Step 4 — evaluate this line
            matched = self._match_line(pl)
            if matched is not None:
                pending.append(_PendingMatch(
                    source_file=file_info.path,
                    line_no=pl.line_no,
                    timestamp=pl.timestamp,
                    text=pl.text,
                    matched_patterns=matched,
                    context_before=tuple(ctx_buf),
                    needed_after=cfg.context_lines,
                ))

            # Step 5 — advance context_before buffer
            ctx_buf.append(pl)

        # Flush remaining pending matches (file ended before window filled)
        while pending:
            results.append(pending.popleft().to_result())

        if skip_triggered:
            return FileAnalysisResult(matches=[], was_skipped=True)
        return FileAnalysisResult(matches=results, was_skipped=False)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _match_line(self, pl: ParsedLine) -> frozenset[str] | None:
        """
        Return the set of matched include-pattern strings, or None if the
        line is rejected.  Evaluation order: time → exclude → include.
        """
        cfg = self._config

        if cfg.time_from is not None and pl.timestamp < cfg.time_from:
            return None
        if cfg.time_to is not None and pl.timestamp > cfg.time_to:
            return None

        if self._exclude and any(p.search(pl.text) for p in self._exclude):
            return None

        if self._include:
            matched = frozenset(s for s, p in self._include if p.search(pl.text))
            if not matched:
                return None
            return matched

        # No include patterns — accept everything not excluded or out-of-range
        return frozenset()
