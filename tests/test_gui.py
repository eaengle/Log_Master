"""
Headless tests for GUI widget logic: state round-trips, build_criteria /
build_config validation, and DateTimeEntry get/set/clear.

All tests that touch Tkinter widgets require a live Tk root.  The module-
scoped 'root' fixture creates one hidden window and tears it down after the
module runs.  If no display is available (some Linux CI environments) the
fixture skips the entire module.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path

import pytest

from log_master.gui.app import (
    AnalysisTab,
    DateTimeEntry,
    FilesTab,
    OutputTab,
    _listbox_items,
    _set_listbox,
)
from log_master.core.output_writer import OutputMode, SortOrder, Column


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def root():
    try:
        r = tk.Tk()
        r.withdraw()
    except tk.TclError:
        pytest.skip("no display available")
    yield r
    r.destroy()


@pytest.fixture
def files_tab(root):
    tab = FilesTab(root)
    yield tab
    tab.destroy()


@pytest.fixture
def analysis_tab(root):
    tab = AnalysisTab(root)
    yield tab
    tab.destroy()


@pytest.fixture
def output_tab(root):
    tab = OutputTab(root)
    yield tab
    tab.destroy()


@pytest.fixture
def dte(root):
    w = DateTimeEntry(root)
    yield w
    w.destroy()


# ---------------------------------------------------------------------------
# _listbox helpers
# ---------------------------------------------------------------------------


def test_listbox_round_trip(root):
    lb = tk.Listbox(root)
    items = ["alpha", "beta", "gamma"]
    _set_listbox(lb, items)
    assert _listbox_items(lb) == items
    lb.destroy()


def test_listbox_overwrite(root):
    lb = tk.Listbox(root)
    _set_listbox(lb, ["a", "b"])
    _set_listbox(lb, ["x"])
    assert _listbox_items(lb) == ["x"]
    lb.destroy()


# ---------------------------------------------------------------------------
# DateTimeEntry
# ---------------------------------------------------------------------------


def test_dte_empty_by_default(dte):
    assert dte.get() == ""


def test_dte_set_date_only(dte):
    dte.set("2025-06-15")
    assert dte.get() == "2025-06-15"


def test_dte_set_datetime(dte):
    dte.set("2025-06-15T14:30:00")
    assert dte.get() == "2025-06-15T14:30:00"


def test_dte_clear(dte):
    dte.set("2025-06-15T14:30:00")
    dte.clear()
    assert dte.get() == ""


def test_dte_set_empty_string(dte):
    dte.set("2025-01-01")
    dte.set("")
    assert dte.get() == ""


# ---------------------------------------------------------------------------
# FilesTab state round-trips
# ---------------------------------------------------------------------------


def test_files_tab_defaults(files_tab):
    state = files_tab.get_state()
    assert state["roots"] == []
    assert state["globs"] == ""
    assert state["extensions"] == ""
    assert state["max_depth"] == ""
    assert state["min_size"] == ""
    assert state["max_size"] == ""
    assert state["modified_after"] == ""
    assert state["modified_before"] == ""
    assert state["include_dirs"] == ""
    assert state["exclude_dirs"] == ""


def test_files_tab_round_trip(files_tab):
    original = {
        "roots":           ["/tmp/logs", "/var/log"],
        "globs":           "*.log,app*",
        "extensions":      ".log,.txt",
        "max_depth":       "3",
        "min_size":        "100",
        "max_size":        "1048576",
        "modified_after":  "2025-01-01",
        "modified_before": "2025-12-31T23:59:59",
        "include_dirs":    "app,web",
        "exclude_dirs":    "test,tmp",
    }
    files_tab.set_state(original)
    assert files_tab.get_state() == original


def test_files_tab_build_criteria(files_tab, tmp_path):
    files_tab.set_state({
        "roots":      [str(tmp_path)],
        "globs":      "*.log",
        "extensions": ".log",
        "max_depth":  "2",
        "min_size":   "",
        "max_size":   "",
        "modified_after":  "",
        "modified_before": "",
        "include_dirs": "",
        "exclude_dirs": "",
    })
    criteria = files_tab.build_criteria()
    assert Path(str(tmp_path)) in criteria.root_dirs
    assert list(criteria.name_globs) == ["*.log"]
    assert list(criteria.extensions) == [".log"]
    assert criteria.max_depth == 2
    assert criteria.min_size_bytes is None
    assert criteria.max_size_bytes is None
    assert criteria.modified_after is None
    assert criteria.modified_before is None


def test_files_tab_build_criteria_bad_int(files_tab, tmp_path):
    files_tab.set_state({
        "roots":      [str(tmp_path)],
        "globs":      "",
        "extensions": "",
        "max_depth":  "abc",
        "min_size":   "",
        "max_size":   "",
        "modified_after":  "",
        "modified_before": "",
        "include_dirs": "",
        "exclude_dirs": "",
    })
    with pytest.raises(ValueError, match="Max depth"):
        files_tab.build_criteria()


def test_files_tab_build_criteria_datetime_fields(files_tab, tmp_path):
    files_tab.set_state({
        "roots":           [str(tmp_path)],
        "globs":           "",
        "extensions":      "",
        "max_depth":       "",
        "min_size":        "",
        "max_size":        "",
        "modified_after":  "2025-03-01T08:00",
        "modified_before": "2025-03-31",
        "include_dirs":    "",
        "exclude_dirs":    "",
    })
    criteria = files_tab.build_criteria()
    assert criteria.modified_after is not None
    assert criteria.modified_after.year == 2025
    assert criteria.modified_after.month == 3
    assert criteria.modified_after.hour == 8
    assert criteria.modified_before is not None
    assert criteria.modified_before.day == 31


# ---------------------------------------------------------------------------
# AnalysisTab state round-trips
# ---------------------------------------------------------------------------


def test_analysis_tab_defaults(analysis_tab):
    state = analysis_tab.get_state()
    assert state["include_patterns"] == []
    assert state["exclude_patterns"] == []
    assert state["skip_file_patterns"] == []
    assert state["time_from"] == ""
    assert state["time_to"] == ""
    assert state["case_insensitive"] is False
    assert state["context_lines"] == "0"


def test_analysis_tab_round_trip(analysis_tab):
    original = {
        "include_patterns":   ["ERROR", "WARN"],
        "exclude_patterns":   ["DEBUG"],
        "skip_file_patterns": ["binary"],
        "time_from":          "2025-01-01",
        "time_to":            "2025-12-31T23:59:59",
        "case_insensitive":   True,
        "context_lines":      "3",
    }
    analysis_tab.set_state(original)
    assert analysis_tab.get_state() == original


def test_analysis_tab_build_config(analysis_tab):
    analysis_tab.set_state({
        "include_patterns":   ["ERROR"],
        "exclude_patterns":   [],
        "skip_file_patterns": [],
        "time_from":          "",
        "time_to":            "",
        "case_insensitive":   False,
        "context_lines":      "2",
    })
    cfg = analysis_tab.build_config()
    assert cfg.include_patterns == ("ERROR",)
    assert cfg.exclude_patterns == ()
    assert cfg.case_sensitive is True
    assert cfg.context_lines == 2
    assert cfg.time_from is None
    assert cfg.time_to is None


def test_analysis_tab_build_config_case_insensitive(analysis_tab):
    analysis_tab.set_state({
        "include_patterns":   [],
        "exclude_patterns":   [],
        "skip_file_patterns": [],
        "time_from":          "",
        "time_to":            "",
        "case_insensitive":   True,
        "context_lines":      "0",
    })
    cfg = analysis_tab.build_config()
    assert cfg.case_sensitive is False


def test_analysis_tab_build_config_bad_context(analysis_tab):
    analysis_tab.set_state({
        "include_patterns":   [],
        "exclude_patterns":   [],
        "skip_file_patterns": [],
        "time_from":          "",
        "time_to":            "",
        "case_insensitive":   False,
        "context_lines":      "xyz",
    })
    with pytest.raises(ValueError, match="Context lines"):
        analysis_tab.build_config()


def test_analysis_tab_build_config_time_range(analysis_tab):
    analysis_tab.set_state({
        "include_patterns":   [],
        "exclude_patterns":   [],
        "skip_file_patterns": [],
        "time_from":          "2025-06-01T09:00",
        "time_to":            "2025-06-30",
        "case_insensitive":   False,
        "context_lines":      "0",
    })
    cfg = analysis_tab.build_config()
    assert cfg.time_from is not None
    assert cfg.time_from.hour == 9
    assert cfg.time_to is not None
    assert cfg.time_to.month == 6


# ---------------------------------------------------------------------------
# OutputTab state round-trips
# ---------------------------------------------------------------------------


def test_output_tab_defaults(output_tab):
    state = output_tab.get_state()
    assert state["output_dir"] == ""
    assert state["mode_single"] is True
    assert state["mode_pattern"] is False
    assert state["mode_source"] is False
    assert state["mode_parent"] is False
    assert state["sort"] == "file-order"
    assert state["include_context"] is True
    assert state["workers"] == "1"
    assert state["path_depth"] == ""
    # all columns on by default
    assert all(state["columns"].values())


def test_output_tab_round_trip(output_tab, tmp_path):
    original = {
        "output_dir":      str(tmp_path),
        "mode_single":     False,
        "mode_pattern":    True,
        "mode_source":     True,
        "mode_parent":     False,
        "sort":            "timestamp",
        "columns":         {
            "timestamp": True, "source_file": True,
            "line_no": False, "pattern": True, "text": True,
        },
        "include_context": False,
        "workers":         "4",
        "path_depth":      "2",
    }
    output_tab.set_state(original)
    assert output_tab.get_state() == original


def test_output_tab_build_config(output_tab, tmp_path):
    output_tab.set_state({
        "output_dir":      str(tmp_path),
        "mode_single":     True,
        "mode_pattern":    False,
        "mode_source":     False,
        "mode_parent":     False,
        "sort":            "file-order",
        "columns":         {
            "timestamp": True, "source_file": True,
            "line_no": True, "pattern": True, "text": True,
        },
        "include_context": True,
        "workers":         "1",
        "path_depth":      "",
    })
    cfg = output_tab.build_config()
    assert cfg.output_dir == tmp_path
    assert OutputMode.SINGLE in cfg.modes
    assert cfg.sort == SortOrder.FILE_ORDER
    assert cfg.include_context is True
    assert cfg.path_depth is None


def test_output_tab_build_config_missing_dir(output_tab):
    output_tab.set_state({
        "output_dir":      "",
        "mode_single":     True,
        "mode_pattern":    False,
        "mode_source":     False,
        "mode_parent":     False,
        "sort":            "file-order",
        "columns":         {},
        "include_context": True,
        "workers":         "1",
        "path_depth":      "",
    })
    with pytest.raises(ValueError, match="[Oo]utput directory"):
        output_tab.build_config()


def test_output_tab_build_config_timestamp_sort(output_tab, tmp_path):
    output_tab.set_state({
        "output_dir":      str(tmp_path),
        "mode_single":     True,
        "mode_pattern":    False,
        "mode_source":     False,
        "mode_parent":     False,
        "sort":            "timestamp",
        "columns":         {},
        "include_context": True,
        "workers":         "1",
        "path_depth":      "",
    })
    cfg = output_tab.build_config()
    assert cfg.sort == SortOrder.TIMESTAMP


def test_output_tab_build_config_column_selection(output_tab, tmp_path):
    output_tab.set_state({
        "output_dir":      str(tmp_path),
        "mode_single":     True,
        "mode_pattern":    False,
        "mode_source":     False,
        "mode_parent":     False,
        "sort":            "file-order",
        "columns":         {
            "timestamp": True, "source_file": False,
            "line_no": False, "pattern": True, "text": True,
        },
        "include_context": True,
        "workers":         "1",
        "path_depth":      "",
    })
    cfg = output_tab.build_config()
    assert Column.TIMESTAMP in cfg.columns
    assert Column.SOURCE_FILE not in cfg.columns
    assert Column.PATTERN in cfg.columns


def test_output_tab_build_config_multi_mode(output_tab, tmp_path):
    output_tab.set_state({
        "output_dir":      str(tmp_path),
        "mode_single":     True,
        "mode_pattern":    True,
        "mode_source":     False,
        "mode_parent":     False,
        "sort":            "file-order",
        "columns":         {},
        "include_context": True,
        "workers":         "1",
        "path_depth":      "",
    })
    cfg = output_tab.build_config()
    assert OutputMode.SINGLE in cfg.modes
    assert OutputMode.PER_PATTERN in cfg.modes
    assert OutputMode.PER_SOURCE_FILE not in cfg.modes
