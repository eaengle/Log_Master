"""
LogProcessor: ties FileFinder → TimestampResolver → ExpressionAnalyzer → OutputWriter
into a single configurable pipeline.

workers=1  : serial (default — deterministic, no threading overhead)
workers=0  : auto   — min(8, cpu_count())
workers>1  : explicit thread count

File-discovery order is preserved in all modes: the parallel path submits
futures in discovery order and iterates them in that same order, so
OutputWriter always sees results file-by-file in a consistent sequence.
The timestamp sort mode is unaffected because OutputWriter buffers and sorts
on flush regardless.
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .expression_analyzer import ExpressionAnalyzer, SearchConfig
from .file_finder import FileFindCriteria, FileFinder
from .output_writer import OutputConfig, OutputWriter
from .timestamp_resolver import TimestampResolver


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProcessorConfig:
    """
    Immutable configuration for a LogProcessor run.

    find_criteria : controls which files are discovered.
    search_config : include/exclude/skip patterns, time range, context lines.
    output_config : TSV routing, columns, sort order.
    workers       : 1=serial, 0=auto (min(8, cpu_count())), >1=explicit.
    """

    find_criteria: FileFindCriteria
    search_config: SearchConfig
    output_config: OutputConfig
    workers: int = 1


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass
class ProcessorResult:
    """Summary counts returned by LogProcessor.run()."""

    files_found: int
    files_analyzed: int
    files_skipped: int
    matches_total: int


# ---------------------------------------------------------------------------
# Processor
# ---------------------------------------------------------------------------


class LogProcessor:
    """
    Orchestrates a complete analysis run.

    A single TimestampResolver and ExpressionAnalyzer are shared across all
    files.  Both are safe for concurrent reads: TimestampResolver creates fresh
    per-file parse state on each iter_parsed_lines() call; ExpressionAnalyzer
    holds only pre-compiled read-only patterns.
    """

    def __init__(self, config: ProcessorConfig) -> None:
        self._config = config

    def run(self) -> ProcessorResult:
        """
        Execute the full pipeline and return summary counts.

        Discovers files, analyses each one, writes matching results to the
        configured output, and returns a ProcessorResult.
        """
        cfg = self._config
        finder = FileFinder(cfg.find_criteria)
        files = list(finder.find())

        analyzer = ExpressionAnalyzer(cfg.search_config)
        resolver = TimestampResolver()

        files_skipped = 0
        matches_total = 0
        workers = self._resolve_workers()

        with OutputWriter(cfg.output_config) as writer:
            iterator = (
                self._run_serial(files, analyzer, resolver)
                if workers == 1
                else self._run_parallel(files, analyzer, resolver, workers)
            )
            for far in iterator:
                if far.was_skipped:
                    files_skipped += 1
                else:
                    for match in far.matches:
                        writer.add_result(match)
                        matches_total += 1

        return ProcessorResult(
            files_found=len(files),
            files_analyzed=len(files) - files_skipped,
            files_skipped=files_skipped,
            matches_total=matches_total,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_workers(self) -> int:
        w = self._config.workers
        if w == 0:
            return min(8, os.cpu_count() or 1)
        return w

    def _run_serial(self, files, analyzer, resolver):
        for fi in files:
            yield analyzer.analyze_file(fi, resolver)

    def _run_parallel(self, files, analyzer, resolver, workers):
        with ThreadPoolExecutor(max_workers=workers) as pool:
            # Submit in discovery order; iterate futures in the same order
            # so file-order output remains deterministic.
            futures = [
                pool.submit(analyzer.analyze_file, fi, resolver)
                for fi in files
            ]
            for future in futures:
                yield future.result()
