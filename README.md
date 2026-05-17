# Log Master

A multi-format log file search and analysis tool for Windows, Linux, and macOS. Search, filter, and correlate log files across directory trees from the command line or a graphical interface.

* This project was written almost entirely using Claude Code and Codex.

## Features

- **Multi-format timestamp detection** — automatically identifies the timestamp format used in each log file from 18 built-in formats (ISO 8601, Apache, syslog, Android, Spark, HDFS, BGL, Proxifier, and more)
- **Regex pattern matching** — include patterns (OR logic), exclude patterns, and whole-file skip patterns
- **Time range filtering** — filter matches to a specific date/time window
- **Context lines** — capture N lines before and after each match
- **Flexible output** — single file, per-pattern, per-source file, or per-parent-directory TSV output
- **Sort modes** — preserve original file order or sort all results by timestamp
- **Parallel processing** — multi-worker file processing with configurable thread count
- **GUI and CLI** — full Tkinter desktop interface alongside a complete command-line tool
- **JSON config files** — save and reuse search configurations; CLI flags override JSON values

## Installation

Requires Python 3.10 or later.

```
pip install -e .
```

This registers two entry points:

| Command | Description |
|---|---|
| `logmaster` | Command-line interface |
| `logmaster-gui` | Desktop GUI |

## Quick Start

### CLI

Search all `.log` files under `C:\logs` for lines containing `ERROR` or `FATAL`:

```
logmaster --root C:\logs --ext .log --include ERROR --include FATAL --output-dir .\results
```

Search with a JSON config file and override the output directory on the command line:

```
logmaster --config search.json --output-dir .\today
```

### GUI

```
logmaster-gui
```

The GUI has four tabs: **Files**, **Analysis**, **Output**, and **Results**. Fill in each tab and click **Run**.

## CLI Reference

```
logmaster [--config FILE] [file discovery options] [search options] [output options] [--workers N]
```

### File Discovery

| Flag | Description |
|---|---|
| `--root DIR` / `-r` | Root directory to search (repeatable) |
| `--glob PATTERN` / `-g` | File name glob, e.g. `*.log` (repeatable) |
| `--ext EXT` / `-e` | File extension filter, e.g. `.log` (repeatable) |
| `--depth N` | Maximum directory recursion depth |
| `--min-size BYTES` | Minimum file size |
| `--max-size BYTES` | Maximum file size |
| `--modified-after DATE` | Files modified after DATE (`YYYY-MM-DD` or `YYYY-MM-DDTHH:MM:SS`) |
| `--modified-before DATE` | Files modified before DATE |
| `--include-dir PATTERN` | Only descend into directories matching PATTERN (repeatable) |
| `--exclude-dir PATTERN` | Skip directories matching PATTERN (repeatable) |

### Search

| Flag | Description |
|---|---|
| `--include PATTERN` / `-i` | Include lines matching PATTERN — regex, OR logic (repeatable) |
| `--exclude PATTERN` / `-x` | Exclude lines matching PATTERN — regex (repeatable) |
| `--skip-file PATTERN` | Skip entire file if PATTERN is found anywhere in it (repeatable) |
| `--from DATETIME` | Match only lines at or after DATETIME |
| `--to DATETIME` | Match only lines at or before DATETIME |
| `--case-insensitive` | Case-insensitive pattern matching |
| `--context N` / `-C` | Capture N context lines before and after each match |

### Output

| Flag | Description |
|---|---|
| `--output-dir DIR` / `-o` | Directory to write results (required) |
| `--mode MODE` | Output mode: `single`, `per-pattern`, `per-source`, `per-parent` (repeatable, default: `single`) |
| `--columns COLS` | Comma-separated column list (default: all columns) |
| `--sort ORDER` | `file-order` (default) or `timestamp` |
| `--no-context` | Omit context lines from output rows |
| `--base-path PATH` | Strip this prefix from `source_file` column paths |
| `--workers N` / `-w` | Worker threads: `0` = auto (up to 8), `1` = serial (default: `1`) |

### Output Columns

Each output row is a TSV record with these columns:

| Column | Description |
|---|---|
| `timestamp` | ISO 8601 timestamp extracted from the log line |
| `source_file` | Full path to the source log file |
| `line_no` | 1-based line number within the source file |
| `pattern` | The include pattern that matched (empty for context lines) |
| `text` | The log line with the timestamp span removed |

### Output Modes

| Mode | Output file(s) |
|---|---|
| `single` | One `results.tsv` containing all matches |
| `per-pattern` | One TSV per include pattern |
| `per-source` | One TSV per source log file |
| `per-parent` | One TSV per source file's parent directory |

Modes are combinable — specify `--mode` multiple times to produce multiple output sets simultaneously.

## JSON Config File

The GUI (File > Save Config) and the CLI share the same JSON format, so a
config file can be created in either tool and used by both.

```json
{
  "files": {
    "roots": ["C:\\logs\\app", "C:\\logs\\system"],
    "extensions": ".log",
    "globs": "",
    "max_depth": "5",
    "min_size": "",
    "max_size": "",
    "modified_after": "2025-01-01",
    "modified_before": "",
    "include_dirs": "",
    "exclude_dirs": ""
  },
  "analysis": {
    "include_patterns": ["ERROR", "FATAL", "Exception"],
    "exclude_patterns": ["health.check"],
    "skip_file_patterns": ["debug"],
    "time_from": "2025-01-01",
    "time_to": "2025-01-31T23:59:59",
    "case_insensitive": false,
    "context_lines": "2"
  },
  "output": {
    "output_dir": "C:\\results",
    "mode_single": true,
    "mode_pattern": true,
    "mode_source": false,
    "mode_parent": false,
    "sort": "timestamp",
    "columns": {
      "timestamp": true,
      "source_file": true,
      "line_no": true,
      "pattern": true,
      "text": true
    },
    "include_context": true,
    "workers": "4",
    "base_path": ""
  }
}
```

Any CLI flag supplied on the command line overrides the corresponding JSON value.

## Supported Timestamp Formats

Log Master detects the dominant timestamp format from the first 40 lines of each file. Formats are evaluated most-restrictive-first, so a more specific format always wins over a less specific one on the same data.

| Format | Example |
|---|---|
| ISO 8601 with timezone | `2024-01-15T18:01:47.123+05:00` |
| ISO 8601 UTC (Z) | `2024-01-15T18:01:47.123Z` |
| ISO 8601 local | `2024-01-15 18:01:47.123` |
| Apache access log | `15/Jan/2024:18:01:47 +0000` |
| Apache error log | `[Mon Jan 15 18:01:47 2024]` |
| Syslog | `Jan 15 18:01:47` |
| Windows Event | `2024-01-15 18:01:47` (space-separated) |
| Common space-separated with comma fraction | `2024-01-15 18:01:47,978` |
| Date-only | `2024-01-15` |
| BGL (Blue Gene/L) | `2005-06-03-15.42.50.675872` |
| HealthApp | `20171223-22:15:29:606` |
| HDFS compact | `081109 203615` |
| Spark | `17/06/09 20:10:40` |
| Android logcat | `03-17 16:13:38.811` |
| Proxifier | `[10.30 16:49:06]` |
| Dot-separated date | `2005.06.03` |

Every line is guaranteed a timestamp. Lines that do not match the detected format fall back to the file's modification time plus 100 ms per line.

For formats that have no year (Android, Proxifier, syslog), the year is inferred from the file modification time with automatic year-rollover detection: if the log month is later than the mtime month, the previous year is used.

## Project Layout

```
log_master/
  core/
    file_finder.py        # Directory traversal and file filtering
    timestamp_resolver.py # Format detection, parsing, and line normalization
    expression_analyzer.py# Include/exclude/skip pattern evaluation
    output_writer.py      # TSV fan-out, sort modes, column selection
    log_processor.py      # Pipeline orchestration (serial and parallel)
  cli/
    main.py               # argparse CLI entry point
  gui/
    app.py                # Tkinter four-tab desktop interface
tests/                    # pytest test suite (274 tests)
sample_logs/              # Sample log files for manual testing
pyproject.toml
```

## Development

Run the test suite:

```
pytest
```

Run with coverage:

```
pytest --cov=log_master --cov-report=term-missing
```
