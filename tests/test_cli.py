"""Tests for the CLI entry point (main.py)."""

from __future__ import annotations

import csv
import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from log_master.cli.main import main


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


def run(args: list[str]) -> None:
    """Invoke main() with the given argv list."""
    main(args)


# ---------------------------------------------------------------------------
# Basic invocation
# ---------------------------------------------------------------------------


class TestBasicInvocation:

    def test_creates_output_tsv(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", ["2024-01-01 09:00:00 ERROR crash"])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out)])
        assert (out / "results.tsv").exists()

    def test_match_count_in_output(self, tmp_path, capsys):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 09:00:00 ERROR one",
            "2024-01-01 09:00:01 ERROR two",
        ])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--include", "ERROR"])
        captured = capsys.readouterr()
        assert "Matches: 2" in captured.out

    def test_summary_printed_to_stdout(self, tmp_path, capsys):
        root = tmp_path / "logs"
        write_log(root / "app.log", ["2024-01-01 09:00:00 INFO hi"])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out)])
        captured = capsys.readouterr()
        assert "Files found:" in captured.out
        assert "Analyzed:" in captured.out
        assert "Skipped:" in captured.out

    def test_output_dir_created(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", ["2024-01-01 09:00:00 INFO hi"])
        out = tmp_path / "new" / "dir"
        run(["--root", str(root), "--output-dir", str(out)])
        assert out.exists()


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrors:

    def test_missing_root_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            run(["--output-dir", str(tmp_path / "out")])

    def test_missing_output_dir_exits(self, tmp_path):
        root = tmp_path / "logs"
        root.mkdir()
        with pytest.raises(SystemExit):
            run(["--root", str(root)])

    def test_bad_json_config_exits(self, tmp_path):
        cfg_file = tmp_path / "bad.json"
        cfg_file.write_text("not valid json", encoding="utf-8")
        with pytest.raises(SystemExit):
            run(["--config", str(cfg_file),
                 "--root", str(tmp_path),
                 "--output-dir", str(tmp_path / "out")])

    def test_missing_config_file_exits(self, tmp_path):
        with pytest.raises(SystemExit):
            run(["--config", str(tmp_path / "nonexistent.json"),
                 "--root", str(tmp_path),
                 "--output-dir", str(tmp_path / "out")])

    def test_bad_modified_after_exits(self, tmp_path):
        root = tmp_path / "logs"
        root.mkdir()
        with pytest.raises(SystemExit):
            run(["--root", str(root), "--output-dir", str(tmp_path / "out"),
                 "--modified-after", "not-a-date"])

    def test_bad_time_from_exits(self, tmp_path):
        root = tmp_path / "logs"
        root.mkdir()
        with pytest.raises(SystemExit):
            run(["--root", str(root), "--output-dir", str(tmp_path / "out"),
                 "--from", "not-a-time"])


# ---------------------------------------------------------------------------
# JSON config
# ---------------------------------------------------------------------------


class TestJsonConfig:

    def test_config_file_sets_root(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", ["2024-01-01 09:00:00 INFO hello"])
        out = tmp_path / "out"
        cfg = {
            "files":  {"roots": [str(root)]},
            "output": {"output_dir": str(out)},
        }
        cfg_file = tmp_path / "cfg.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        run(["--config", str(cfg_file)])
        assert (out / "results.tsv").exists()

    def test_config_sets_include_patterns(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 09:00:00 ERROR bad",
            "2024-01-01 09:00:01 INFO ok",
        ])
        out = tmp_path / "out"
        cfg = {
            "files":    {"roots": [str(root)]},
            "analysis": {"include_patterns": ["ERROR"]},
            "output":   {"output_dir": str(out)},
        }
        cfg_file = tmp_path / "cfg.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        run(["--config", str(cfg_file)])
        rows = read_tsv(out / "results.tsv")
        assert len(rows) == 1

    def test_cli_flag_overrides_json_include(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 09:00:00 ERROR bad",
            "2024-01-01 09:00:01 WARN slow",
        ])
        out = tmp_path / "out"
        cfg = {
            "files":    {"roots": [str(root)]},
            "analysis": {"include_patterns": ["ERROR"]},
            "output":   {"output_dir": str(out)},
        }
        cfg_file = tmp_path / "cfg.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        # CLI --include overrides JSON include_patterns
        run(["--config", str(cfg_file), "--include", "WARN"])
        rows = read_tsv(out / "results.tsv")
        assert len(rows) == 1
        assert "slow" in rows[0]["text"]

    def test_cli_output_dir_overrides_json(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", ["2024-01-01 09:00:00 INFO hi"])
        json_out = tmp_path / "json_out"
        cli_out = tmp_path / "cli_out"
        cfg = {
            "files":  {"roots": [str(root)]},
            "output": {"output_dir": str(json_out)},
        }
        cfg_file = tmp_path / "cfg.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        run(["--config", str(cfg_file), "--output-dir", str(cli_out)])
        assert (cli_out / "results.tsv").exists()
        assert not json_out.exists()

    def test_config_sets_workers(self, tmp_path, capsys):
        root = tmp_path / "logs"
        for i in range(3):
            write_log(root / f"{i}.log",
                      [f"2024-01-01 09:00:0{i} INFO line{i}"])
        out = tmp_path / "out"
        cfg = {
            "files":  {"roots": [str(root)]},
            "output": {"output_dir": str(out), "workers": "2"},
        }
        cfg_file = tmp_path / "cfg.json"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        run(["--config", str(cfg_file)])
        rows = read_tsv(out / "results.tsv")
        assert len(rows) == 3


# ---------------------------------------------------------------------------
# Search flags
# ---------------------------------------------------------------------------


class TestSearchFlags:

    def test_include_flag_filters_matches(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 09:00:00 ERROR bad",
            "2024-01-01 09:00:01 INFO ok",
        ])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--include", "ERROR"])
        rows = read_tsv(out / "results.tsv")
        assert len(rows) == 1

    def test_exclude_flag_removes_matches(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 09:00:00 ERROR crash",
            "2024-01-01 09:00:01 ERROR noise",
        ])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--include", "ERROR", "--exclude", "noise"])
        rows = read_tsv(out / "results.tsv")
        assert len(rows) == 1

    def test_multiple_include_flags(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 09:00:00 ERROR bad",
            "2024-01-01 09:00:01 WARN slow",
            "2024-01-01 09:00:02 INFO ok",
        ])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--include", "ERROR", "--include", "WARN"])
        rows = read_tsv(out / "results.tsv")
        assert len(rows) == 2

    def test_skip_file_flag(self, tmp_path, capsys):
        root = tmp_path / "logs"
        write_log(root / "a.log", ["2024-01-01 09:00:00 ERROR keep"])
        write_log(root / "b.log", ["SKIP_ME here", "2024-01-01 09:00:01 ERROR gone"])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--include", "ERROR", "--skip-file", "SKIP_ME"])
        captured = capsys.readouterr()
        assert "Skipped: 1" in captured.out

    def test_case_insensitive_flag(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", ["2024-01-01 09:00:00 error lowercase"])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--include", "ERROR", "--case-insensitive"])
        rows = read_tsv(out / "results.tsv")
        assert len(rows) == 1

    def test_context_flag(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 09:00:00 INFO before",
            "2024-01-01 09:00:01 ERROR match",
            "2024-01-01 09:00:02 INFO after",
        ])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--include", "ERROR", "--context", "1"])
        rows = read_tsv(out / "results.tsv")
        # 1 context_before + 1 match + 1 context_after = 3 rows
        assert len(rows) == 3

    def test_time_from_flag(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 08:00:00 INFO early",
            "2024-01-01 10:00:00 INFO late",
        ])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--from", "2024-01-01T09:00:00"])
        rows = read_tsv(out / "results.tsv")
        assert len(rows) == 1
        assert "late" in rows[0]["text"]

    def test_time_to_flag(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 08:00:00 INFO early",
            "2024-01-01 10:00:00 INFO late",
        ])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--to", "2024-01-01T09:00:00"])
        rows = read_tsv(out / "results.tsv")
        assert len(rows) == 1
        assert "early" in rows[0]["text"]


# ---------------------------------------------------------------------------
# Output flags
# ---------------------------------------------------------------------------


class TestOutputFlags:

    def test_sort_timestamp_flag(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "a.log", ["2024-01-01 10:00:02 INFO second"])
        write_log(root / "b.log", ["2024-01-01 10:00:01 INFO first"])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out), "--sort", "timestamp"])
        rows = read_tsv(out / "results.tsv")
        assert rows[0]["text"] == "INFO first"
        assert rows[1]["text"] == "INFO second"

    def test_mode_per_pattern(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", ["2024-01-01 09:00:00 ERROR crash"])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--include", "ERROR", "--mode", "per-pattern"])
        assert (out / "pattern_ERROR.tsv").exists()

    def test_mode_per_source(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "alpha.log", ["2024-01-01 09:00:00 INFO hi"])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out), "--mode", "per-source"])
        assert (out / "alpha.tsv").exists()

    def test_no_context_flag_omits_context_rows(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", [
            "2024-01-01 09:00:00 INFO before",
            "2024-01-01 09:00:01 ERROR match",
            "2024-01-01 09:00:02 INFO after",
        ])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--include", "ERROR", "--context", "1", "--no-context"])
        rows = read_tsv(out / "results.tsv")
        assert len(rows) == 1

    def test_columns_flag_restricts_output(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", ["2024-01-01 09:00:00 INFO hello"])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--columns", "text,line_no"])
        rows = read_tsv(out / "results.tsv")
        assert set(rows[0].keys()) == {"text", "line_no"}

    def test_base_path_makes_source_relative(self, tmp_path):
        root = tmp_path / "logs"
        write_log(root / "app.log", ["2024-01-01 09:00:00 INFO hi"])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--columns", "source_file", "--base-path", str(tmp_path)])
        rows = read_tsv(out / "results.tsv")
        assert not Path(rows[0]["source_file"]).is_absolute()


# ---------------------------------------------------------------------------
# Workers flag
# ---------------------------------------------------------------------------


class TestWorkersFlag:

    def test_workers_flag_serial(self, tmp_path, capsys):
        root = tmp_path / "logs"
        write_log(root / "a.log", ["2024-01-01 09:00:00 ERROR x"])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--include", "ERROR", "--workers", "1"])
        captured = capsys.readouterr()
        assert "Matches: 1" in captured.out

    def test_workers_flag_parallel(self, tmp_path, capsys):
        root = tmp_path / "logs"
        for i in range(4):
            write_log(root / f"{i}.log",
                      [f"2024-01-01 09:00:0{i} ERROR line{i}"])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out),
             "--include", "ERROR", "--workers", "2"])
        captured = capsys.readouterr()
        assert "Matches: 4" in captured.out

    def test_workers_auto(self, tmp_path, capsys):
        root = tmp_path / "logs"
        write_log(root / "a.log", ["2024-01-01 09:00:00 INFO hi"])
        out = tmp_path / "out"
        run(["--root", str(root), "--output-dir", str(out), "--workers", "0"])
        captured = capsys.readouterr()
        assert "Files found: 1" in captured.out
