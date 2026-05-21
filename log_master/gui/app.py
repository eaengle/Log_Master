"""
Tkinter GUI for Log Master — four-tab ttk.Notebook interface.

Tabs:
  Files    — root directories, glob/extension filters, depth/size/date limits
  Analysis — include/exclude/skip patterns, time range, context lines
  Output   — output directory, modes, columns, sort order, workers
  Results  — Run button, live status, summary after completion

The Run button dispatches LogProcessor in a background thread so the UI
stays responsive.  Results are posted back to the main thread via
root.after(0, callback).

UI state is automatically saved to ~/.logmaster/last_session.json on exit
and restored on the next launch.  Use File > Load/Save Config to exchange
named config files.
"""

from __future__ import annotations

import calendar
import dataclasses
import json
import sys
import threading
import tkinter as tk
from datetime import date, datetime
from importlib import resources
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from log_master.core.config import build_processor_config, parse_datetime
from log_master.core.expression_analyzer import SearchConfig
from log_master.core.file_finder import FileFindCriteria
from log_master.core.log_processor import LogProcessor, ProcessorConfig, ProcessorResult
from log_master.core.output_writer import Column, OutputConfig, OutputMode, SortOrder

# Path used for automatic session save/restore.
_AUTOSAVE_PATH = Path.home() / ".logmaster" / "last_session.json"
_WINDOWS_APP_ID = "LogMaster.App"

# Column enum ↔ JSON key mapping (stable across renames).
_COL_KEY: dict[Column, str] = {
    Column.TIMESTAMP:   "timestamp",
    Column.SOURCE_FILE: "source_file",
    Column.LINE_NO:     "line_no",
    Column.PATTERN:     "pattern",
    Column.TEXT:        "text",
}


def _set_windows_app_id() -> None:
    if sys.platform != "win32":
        return
    try:
        import ctypes

        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(_WINDOWS_APP_ID)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Shared widget helpers
# ---------------------------------------------------------------------------


def _labeled_entry(parent, label: str, row: int, col: int = 0,
                   width: int = 30) -> tk.StringVar:
    ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w",
                                       padx=4, pady=2)
    var = tk.StringVar()
    ttk.Entry(parent, textvariable=var, width=width).grid(
        row=row, column=col + 1, sticky="ew", padx=4, pady=2)
    return var


def _labeled_spin(parent, label: str, row: int, col: int = 0,
                  from_: int = 0, to: int = 9999,
                  default: str = "") -> tk.StringVar:
    ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w",
                                       padx=4, pady=2)
    var = tk.StringVar(value=default)
    sb = ttk.Spinbox(parent, textvariable=var, from_=from_, to=to, width=8)
    sb.grid(row=row, column=col + 1, sticky="w", padx=4, pady=2)
    return var


class DateTimeEntry(ttk.Frame):
    """Compact date picker plus optional HH:MM:SS entry."""

    def __init__(self, parent, width: int = 20):
        super().__init__(parent)
        self._date_var = tk.StringVar()
        self._hour_var = tk.StringVar()
        self._minute_var = tk.StringVar()
        self._second_var = tk.StringVar()
        self._popup: tk.Toplevel | None = None
        today = date.today()
        self._calendar_year = today.year
        self._calendar_month = today.month

        ttk.Entry(self, textvariable=self._date_var, width=11).grid(
            row=0, column=0, sticky="w")
        ttk.Button(self, text="Pick", command=self._show_calendar, width=5).grid(
            row=0, column=1, padx=(4, 0))

        time_frame = ttk.Frame(self)
        time_frame.grid(row=0, column=2, padx=(8, 0), sticky="w")
        ttk.Spinbox(
            time_frame, textvariable=self._hour_var, from_=0, to=23,
            width=3, wrap=True, format="%02.0f",
        ).grid(row=0, column=0)
        ttk.Label(time_frame, text=":").grid(row=0, column=1)
        ttk.Spinbox(
            time_frame, textvariable=self._minute_var, from_=0, to=59,
            width=3, wrap=True, format="%02.0f",
        ).grid(row=0, column=2)
        ttk.Label(time_frame, text=":").grid(row=0, column=3)
        ttk.Spinbox(
            time_frame, textvariable=self._second_var, from_=0, to=59,
            width=3, wrap=True, format="%02.0f",
        ).grid(row=0, column=4)

        ttk.Button(self, text="Clear", command=self.clear, width=6).grid(
            row=0, column=3, padx=(4, 0))

    def get(self) -> str:
        date_text = self._date_var.get().strip()
        time_text = self._time_text()
        if date_text and time_text:
            return f"{date_text}T{time_text}"
        return date_text or time_text

    def set(self, value: str) -> None:
        text = str(value or "").strip()
        if not text:
            self.clear()
            return
        if "T" in text:
            date_text, time_text = text.split("T", 1)
            self._date_var.set(date_text)
            self._set_time(time_text)
        else:
            self._date_var.set(text)
            self._clear_time()

    def clear(self) -> None:
        self._date_var.set("")
        self._clear_time()

    def _time_text(self) -> str:
        raw_parts = [
            self._hour_var.get().strip(),
            self._minute_var.get().strip(),
            self._second_var.get().strip(),
        ]
        if not any(raw_parts):
            return ""
        try:
            hour, minute, second = (int(part or "0") for part in raw_parts)
        except ValueError:
            return ":".join(raw_parts)
        return f"{hour:02d}:{minute:02d}:{second:02d}"

    def _set_time(self, value: str) -> None:
        parts = value.split(":")
        self._hour_var.set(parts[0] if len(parts) > 0 else "")
        self._minute_var.set(parts[1] if len(parts) > 1 else "")
        self._second_var.set(parts[2] if len(parts) > 2 else "")

    def _clear_time(self) -> None:
        self._hour_var.set("")
        self._minute_var.set("")
        self._second_var.set("")

    def _show_calendar(self) -> None:
        if self._popup is not None and self._popup.winfo_exists():
            self._popup.lift()
            return

        current = self._selected_date()
        self._calendar_year = current.year
        self._calendar_month = current.month

        popup = tk.Toplevel(self)
        popup.title("Select date")
        popup.transient(self.winfo_toplevel())
        popup.resizable(False, False)
        self._popup = popup
        self._position_popup(popup)
        self._draw_calendar()

    def _selected_date(self) -> date:
        try:
            return date.fromisoformat(self._date_var.get().strip())
        except ValueError:
            return date.today()

    def _position_popup(self, popup: tk.Toplevel) -> None:
        self.update_idletasks()
        x = self.winfo_rootx()
        y = self.winfo_rooty() + self.winfo_height()
        popup.geometry(f"+{x}+{y}")

    def _draw_calendar(self) -> None:
        if self._popup is None:
            return
        for child in self._popup.winfo_children():
            child.destroy()

        frame = ttk.Frame(self._popup, padding=6)
        frame.grid(row=0, column=0, sticky="nsew")

        ttk.Button(frame, text="<", width=3, command=self._prev_month).grid(
            row=0, column=0, sticky="w")
        ttk.Label(
            frame,
            text=f"{calendar.month_name[self._calendar_month]} {self._calendar_year}",
            anchor="center",
            width=18,
        ).grid(row=0, column=1, columnspan=5, sticky="ew")
        ttk.Button(frame, text=">", width=3, command=self._next_month).grid(
            row=0, column=6, sticky="e")

        for col, name in enumerate(("Mo", "Tu", "We", "Th", "Fr", "Sa", "Su")):
            ttk.Label(frame, text=name, anchor="center", width=4).grid(
                row=1, column=col, pady=(4, 2))

        weeks = calendar.monthcalendar(self._calendar_year, self._calendar_month)
        for row, week in enumerate(weeks, start=2):
            for col, day in enumerate(week):
                if day == 0:
                    ttk.Label(frame, text="", width=4).grid(row=row, column=col)
                else:
                    ttk.Button(
                        frame,
                        text=str(day),
                        width=4,
                        command=lambda d=day: self._choose_day(d),
                    ).grid(row=row, column=col, padx=1, pady=1)

    def _prev_month(self) -> None:
        if self._calendar_month == 1:
            self._calendar_year -= 1
            self._calendar_month = 12
        else:
            self._calendar_month -= 1
        self._draw_calendar()

    def _next_month(self) -> None:
        if self._calendar_month == 12:
            self._calendar_year += 1
            self._calendar_month = 1
        else:
            self._calendar_month += 1
        self._draw_calendar()

    def _choose_day(self, day: int) -> None:
        self._date_var.set(
            date(self._calendar_year, self._calendar_month, day).isoformat())
        if self._popup is not None:
            self._popup.destroy()
            self._popup = None


def _labeled_datetime(parent, label: str, row: int, col: int = 0) -> DateTimeEntry:
    ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w",
                                       padx=4, pady=2)
    widget = DateTimeEntry(parent)
    widget.grid(row=row, column=col + 1, sticky="w", padx=4, pady=2)
    return widget


def _pattern_list_widget(parent, label: str, row: int) -> tk.Listbox:
    """A labeled listbox with Add / Remove controls for entering patterns."""
    frame = ttk.LabelFrame(parent, text=label, padding=4)
    frame.grid(row=row, column=0, columnspan=4, sticky="ew", padx=4, pady=4)
    frame.columnconfigure(0, weight=1)

    lb = tk.Listbox(frame, height=4, selectmode=tk.EXTENDED)
    lb.grid(row=0, column=0, columnspan=3, sticky="ew", pady=2)

    entry_var = tk.StringVar()
    entry = ttk.Entry(frame, textvariable=entry_var, width=32)
    entry.grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=2)

    def add():
        val = entry_var.get().strip()
        if val:
            lb.insert(tk.END, val)
            entry_var.set("")

    def remove():
        for i in reversed(lb.curselection()):
            lb.delete(i)

    def edit_selected(_event=None):
        sel = lb.curselection()
        if not sel:
            return
        idx = sel[0]
        entry_var.set(lb.get(idx))
        lb.delete(idx)
        entry.focus_set()
        entry.icursor(tk.END)

    lb.bind("<Double-Button-1>", edit_selected)
    entry.bind("<Return>", lambda _: add())
    ttk.Button(frame, text="Add", command=add, width=6).grid(
        row=1, column=1, padx=2)
    ttk.Button(frame, text="Remove", command=remove, width=8).grid(
        row=1, column=2, padx=2)

    return lb


def _listbox_items(lb: tk.Listbox) -> list[str]:
    return list(lb.get(0, tk.END))


def _set_listbox(lb: tk.Listbox, items: list[str]) -> None:
    lb.delete(0, tk.END)
    for item in items:
        lb.insert(tk.END, item)


# ---------------------------------------------------------------------------
# Tab 1: Files
# ---------------------------------------------------------------------------


class FilesTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=8)
        self.columnconfigure(1, weight=1)

        # Root directories
        roots_frame = ttk.LabelFrame(self, text="Root directories", padding=4)
        roots_frame.grid(row=0, column=0, columnspan=4, sticky="ew",
                         padx=4, pady=4)
        roots_frame.columnconfigure(0, weight=1)

        self._roots_lb = tk.Listbox(roots_frame, height=4, selectmode=tk.EXTENDED)
        self._roots_lb.grid(row=0, column=0, columnspan=3, sticky="ew", pady=2)

        def browse_root():
            d = filedialog.askdirectory(title="Select root directory")
            if d:
                self._roots_lb.insert(tk.END, d)

        def remove_root():
            for i in reversed(self._roots_lb.curselection()):
                self._roots_lb.delete(i)

        ttk.Button(roots_frame, text="Browse…", command=browse_root).grid(
            row=1, column=0, sticky="w", padx=2)
        ttk.Button(roots_frame, text="Remove", command=remove_root).grid(
            row=1, column=1, sticky="w", padx=2)

        # Filters
        self._globs_var = _labeled_entry(self, "Name globs:", 1,
                                         width=40)
        ttk.Label(self, text="(comma-separated, e.g. *.log,app*)").grid(
            row=1, column=2, sticky="w", padx=4)

        self._ext_var = _labeled_entry(self, "Extensions:", 2, width=20)
        ttk.Label(self, text="(comma-separated, e.g. .log,.txt)").grid(
            row=2, column=2, sticky="w", padx=4)

        self._depth_var = _labeled_spin(self, "Max depth:", 3,
                                        default="", from_=0, to=99)
        ttk.Label(self, text="(blank = unlimited)").grid(
            row=3, column=2, sticky="w", padx=4)

        self._min_size_var = _labeled_entry(self, "Min size (bytes):", 4, width=12)
        self._max_size_var = _labeled_entry(self, "Max size (bytes):", 5, width=12)

        self._mod_after_var = _labeled_datetime(self, "Modified after:", 6)
        ttk.Label(self, text="Date and optional time").grid(
            row=6, column=2, sticky="w", padx=4)

        self._mod_before_var = _labeled_datetime(self, "Modified before:", 7)
        ttk.Label(self, text="Date and optional time").grid(
            row=7, column=2, sticky="w", padx=4)

        self._inc_dir_var = _labeled_entry(self, "Include dirs:", 8, width=30)
        ttk.Label(self, text="(comma-separated globs)").grid(
            row=8, column=2, sticky="w", padx=4)

        self._exc_dir_var = _labeled_entry(self, "Exclude dirs:", 9, width=30)
        ttk.Label(self, text="(comma-separated globs)").grid(
            row=9, column=2, sticky="w", padx=4)

    def roots(self) -> list[str]:
        return _listbox_items(self._roots_lb)

    def _csv(self, var: tk.StringVar) -> list[str]:
        return [v.strip() for v in var.get().split(",") if v.strip()]

    def get_state(self) -> dict:
        return {
            "roots":          _listbox_items(self._roots_lb),
            "globs":          self._globs_var.get(),
            "extensions":     self._ext_var.get(),
            "max_depth":      self._depth_var.get(),
            "min_size":       self._min_size_var.get(),
            "max_size":       self._max_size_var.get(),
            "modified_after": self._mod_after_var.get(),
            "modified_before":self._mod_before_var.get(),
            "include_dirs":   self._inc_dir_var.get(),
            "exclude_dirs":   self._exc_dir_var.get(),
        }

    def set_state(self, state: dict) -> None:
        _set_listbox(self._roots_lb, state.get("roots", []))
        self._globs_var.set(state.get("globs", ""))
        self._ext_var.set(state.get("extensions", ""))
        self._depth_var.set(state.get("max_depth", ""))
        self._min_size_var.set(state.get("min_size", ""))
        self._max_size_var.set(state.get("max_size", ""))
        self._mod_after_var.set(state.get("modified_after", ""))
        self._mod_before_var.set(state.get("modified_before", ""))
        self._inc_dir_var.set(state.get("include_dirs", ""))
        self._exc_dir_var.set(state.get("exclude_dirs", ""))

    def build_criteria(self) -> FileFindCriteria:
        def parse_int(var: tk.StringVar, field: str) -> int | None:
            s = var.get().strip()
            if not s:
                return None
            try:
                return int(s)
            except ValueError:
                raise ValueError(f"{field}: expected integer, got '{s}'")

        mod_after = self._mod_after_var.get().strip()
        mod_before = self._mod_before_var.get().strip()
        return FileFindCriteria(
            root_dirs=[Path(r) for r in self.roots()],
            name_globs=self._csv(self._globs_var),
            extensions=self._csv(self._ext_var),
            max_depth=parse_int(self._depth_var, "Max depth"),
            min_size_bytes=parse_int(self._min_size_var, "Min size"),
            max_size_bytes=parse_int(self._max_size_var, "Max size"),
            modified_after=parse_datetime(mod_after, "Modified after") if mod_after else None,
            modified_before=parse_datetime(mod_before, "Modified before") if mod_before else None,
            include_dir_globs=self._csv(self._inc_dir_var),
            exclude_dir_globs=self._csv(self._exc_dir_var),
        )


# ---------------------------------------------------------------------------
# Tab 2: Analysis
# ---------------------------------------------------------------------------


class AnalysisTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=8)
        self.columnconfigure(1, weight=1)

        self._include_lb = _pattern_list_widget(self, "Include patterns (OR logic)", 0)
        self._exclude_lb = _pattern_list_widget(self, "Exclude patterns", 1)
        self._skip_lb    = _pattern_list_widget(self, "Skip-file patterns", 2)

        opts = ttk.Frame(self, padding=4)
        opts.grid(row=3, column=0, columnspan=4, sticky="ew", padx=4, pady=4)
        opts.columnconfigure(1, weight=1)
        opts.columnconfigure(3, weight=1)

        self._time_from_var = _labeled_datetime(opts, "Time from:", 0)
        ttk.Label(opts, text="Date and optional time").grid(
            row=0, column=2, sticky="w", padx=4)

        self._time_to_var = _labeled_datetime(opts, "Time to:", 1)
        ttk.Label(opts, text="Date and optional time").grid(
            row=1, column=2, sticky="w", padx=4)

        self._case_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Case insensitive",
                        variable=self._case_var).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=4, pady=4)

        self._context_var = _labeled_spin(opts, "Context lines:", 3,
                                          from_=0, to=50, default="0")

    def get_state(self) -> dict:
        return {
            "include_patterns":   _listbox_items(self._include_lb),
            "exclude_patterns":   _listbox_items(self._exclude_lb),
            "skip_file_patterns": _listbox_items(self._skip_lb),
            "time_from":          self._time_from_var.get(),
            "time_to":            self._time_to_var.get(),
            "case_insensitive":   self._case_var.get(),
            "context_lines":      self._context_var.get(),
        }

    def set_state(self, state: dict) -> None:
        _set_listbox(self._include_lb, state.get("include_patterns", []))
        _set_listbox(self._exclude_lb, state.get("exclude_patterns", []))
        _set_listbox(self._skip_lb,    state.get("skip_file_patterns", []))
        self._time_from_var.set(state.get("time_from", ""))
        self._time_to_var.set(state.get("time_to", ""))
        self._case_var.set(state.get("case_insensitive", False))
        self._context_var.set(state.get("context_lines", "0"))

    def build_config(self) -> SearchConfig:
        ctx = self._context_var.get().strip()
        try:
            context_lines = int(ctx) if ctx else 0
        except ValueError:
            raise ValueError(f"Context lines: expected integer, got '{ctx}'")

        time_from = self._time_from_var.get().strip()
        time_to = self._time_to_var.get().strip()
        return SearchConfig(
            include_patterns=tuple(_listbox_items(self._include_lb)),
            exclude_patterns=tuple(_listbox_items(self._exclude_lb)),
            skip_file_patterns=tuple(_listbox_items(self._skip_lb)),
            time_from=parse_datetime(time_from, "Time from") if time_from else None,
            time_to=parse_datetime(time_to, "Time to") if time_to else None,
            case_sensitive=not self._case_var.get(),
            context_lines=context_lines,
        )


# ---------------------------------------------------------------------------
# Tab 3: Output
# ---------------------------------------------------------------------------


class OutputTab(ttk.Frame):
    def __init__(self, parent):
        super().__init__(parent, padding=8)
        self.columnconfigure(1, weight=1)

        # Output directory
        dir_frame = ttk.LabelFrame(self, text="Output directory", padding=4)
        dir_frame.grid(row=0, column=0, columnspan=4, sticky="ew", padx=4, pady=4)
        dir_frame.columnconfigure(0, weight=1)

        self._out_dir_var = tk.StringVar()
        ttk.Entry(dir_frame, textvariable=self._out_dir_var).grid(
            row=0, column=0, sticky="ew", padx=4)

        def browse_out():
            d = filedialog.askdirectory(title="Select output directory")
            if d:
                self._out_dir_var.set(d)

        ttk.Button(dir_frame, text="Browse…", command=browse_out).grid(
            row=0, column=1, padx=4)

        # Modes
        modes_frame = ttk.LabelFrame(self, text="Output modes", padding=4)
        modes_frame.grid(row=1, column=0, columnspan=4, sticky="ew", padx=4, pady=4)

        self._mode_single  = tk.BooleanVar(value=True)
        self._mode_pattern = tk.BooleanVar(value=False)
        self._mode_source  = tk.BooleanVar(value=False)
        self._mode_parent  = tk.BooleanVar(value=False)

        ttk.Checkbutton(modes_frame, text="Single (results.tsv)",
                        variable=self._mode_single).grid(row=0, column=0, sticky="w", padx=8)
        ttk.Checkbutton(modes_frame, text="Per matched pattern",
                        variable=self._mode_pattern).grid(row=0, column=1, sticky="w", padx=8)
        ttk.Checkbutton(modes_frame, text="Per source file",
                        variable=self._mode_source).grid(row=1, column=0, sticky="w", padx=8)
        ttk.Checkbutton(modes_frame, text="Per parent directory",
                        variable=self._mode_parent).grid(row=1, column=1, sticky="w", padx=8)

        # Sort order
        sort_frame = ttk.LabelFrame(self, text="Sort order", padding=4)
        sort_frame.grid(row=2, column=0, columnspan=2, sticky="ew", padx=4, pady=4)

        self._sort_var = tk.StringVar(value="file-order")
        ttk.Radiobutton(sort_frame, text="File order (fast)",
                        variable=self._sort_var,
                        value="file-order").grid(row=0, column=0, sticky="w", padx=8)
        ttk.Radiobutton(sort_frame, text="Timestamp (buffered)",
                        variable=self._sort_var,
                        value="timestamp").grid(row=0, column=1, sticky="w", padx=8)

        # Columns
        cols_frame = ttk.LabelFrame(self, text="Columns", padding=4)
        cols_frame.grid(row=2, column=2, columnspan=2, sticky="ew", padx=4, pady=4)

        self._col_vars: dict[Column, tk.BooleanVar] = {}
        col_labels = {
            Column.TIMESTAMP:   "Timestamp",
            Column.SOURCE_FILE: "Source file",
            Column.LINE_NO:     "Line number",
            Column.PATTERN:     "Pattern",
            Column.TEXT:        "Text",
        }
        for i, (col, label) in enumerate(col_labels.items()):
            var = tk.BooleanVar(value=True)
            self._col_vars[col] = var
            ttk.Checkbutton(cols_frame, text=label, variable=var).grid(
                row=i // 2, column=i % 2, sticky="w", padx=4)

        # Options row
        opts = ttk.Frame(self, padding=4)
        opts.grid(row=3, column=0, columnspan=4, sticky="ew", padx=4)
        opts.columnconfigure(1, weight=1)
        opts.columnconfigure(3, weight=1)

        self._inc_ctx_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts, text="Include context rows in output",
                        variable=self._inc_ctx_var).grid(
            row=0, column=0, columnspan=2, sticky="w", padx=4, pady=4)

        self._workers_var = _labeled_spin(opts, "Workers:", 1,
                                          from_=0, to=64, default="1")
        ttk.Label(opts, text="(0=auto, 1=serial)").grid(
            row=1, column=2, sticky="w", padx=4)

        self._path_depth_var = _labeled_spin(opts, "Path depth:", 2,
                                              from_=0, to=99, default="")
        ttk.Label(opts, text="(parent folders shown; blank = full relative path)").grid(
            row=2, column=2, sticky="w", padx=4)

    def output_dir(self) -> str:
        return self._out_dir_var.get().strip()

    def workers(self) -> int:
        s = self._workers_var.get().strip()
        try:
            return int(s) if s else 1
        except ValueError:
            return 1

    def get_state(self) -> dict:
        return {
            "output_dir":      self._out_dir_var.get(),
            "mode_single":     self._mode_single.get(),
            "mode_pattern":    self._mode_pattern.get(),
            "mode_source":     self._mode_source.get(),
            "mode_parent":     self._mode_parent.get(),
            "sort":            self._sort_var.get(),
            "columns":         {_COL_KEY[col]: var.get()
                                for col, var in self._col_vars.items()},
            "include_context": self._inc_ctx_var.get(),
            "workers":         self._workers_var.get(),
            "path_depth":      self._path_depth_var.get(),
        }

    def set_state(self, state: dict) -> None:
        self._out_dir_var.set(state.get("output_dir", ""))
        self._mode_single.set(state.get("mode_single", True))
        self._mode_pattern.set(state.get("mode_pattern", False))
        self._mode_source.set(state.get("mode_source", False))
        self._mode_parent.set(state.get("mode_parent", False))
        self._sort_var.set(state.get("sort", "file-order"))
        cols = state.get("columns", {})
        for col, var in self._col_vars.items():
            var.set(cols.get(_COL_KEY[col], True))
        self._inc_ctx_var.set(state.get("include_context", True))
        self._workers_var.set(state.get("workers", "1"))
        self._path_depth_var.set(state.get("path_depth", ""))

    def build_config(self) -> OutputConfig:
        out_dir = self.output_dir()
        if not out_dir:
            raise ValueError("Output directory is required")

        modes: set[OutputMode] = set()
        if self._mode_single.get():
            modes.add(OutputMode.SINGLE)
        if self._mode_pattern.get():
            modes.add(OutputMode.PER_PATTERN)
        if self._mode_source.get():
            modes.add(OutputMode.PER_SOURCE_FILE)
        if self._mode_parent.get():
            modes.add(OutputMode.PER_PARENT_DIR)
        if not modes:
            modes.add(OutputMode.SINGLE)

        columns = tuple(col for col, var in self._col_vars.items() if var.get())
        if not columns:
            columns = tuple(Column)

        sort = (SortOrder.TIMESTAMP
                if self._sort_var.get() == "timestamp"
                else SortOrder.FILE_ORDER)

        depth_raw = self._path_depth_var.get().strip()
        try:
            path_depth = int(depth_raw) if depth_raw else None
        except ValueError:
            raise ValueError(f"Path depth: expected integer, got '{depth_raw}'")

        return OutputConfig(
            output_dir=Path(out_dir),
            modes=frozenset(modes),
            columns=columns,
            sort=sort,
            include_context=self._inc_ctx_var.get(),
            path_depth=path_depth,
        )


# ---------------------------------------------------------------------------
# Tab 4: Results
# ---------------------------------------------------------------------------


class ResultsTab(ttk.Frame):
    def __init__(self, parent, get_config_fn):
        super().__init__(parent, padding=8)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        self._get_config = get_config_fn
        self._running = False
        self._stop_event: threading.Event | None = None

        # Run / Cancel buttons
        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=0, column=0, sticky="ew", pady=4)

        self._run_btn = ttk.Button(btn_frame, text="▶  Run",
                                   command=self._start_run, width=14)
        self._run_btn.pack(side="left", padx=4)

        self._cancel_btn = ttk.Button(btn_frame, text="Stop",
                                      command=self._on_cancel, width=8,
                                      state="disabled")
        self._cancel_btn.pack(side="left", padx=4)

        self._status_var = tk.StringVar(value="Ready")
        ttk.Label(btn_frame, textvariable=self._status_var,
                  foreground="gray").pack(side="left", padx=12)

        # Progress bar (determinate; switches to indeterminate while discovering files)
        self._progress = ttk.Progressbar(self, mode="determinate",
                                         maximum=1, value=0)
        self._progress.grid(row=1, column=0, sticky="ew", padx=4, pady=2)

        # Results text area
        text_frame = ttk.LabelFrame(self, text="Output", padding=4)
        text_frame.grid(row=2, column=0, sticky="nsew", padx=4, pady=4)
        text_frame.columnconfigure(0, weight=1)
        text_frame.rowconfigure(0, weight=1)

        self._text = tk.Text(text_frame, state="disabled", wrap="word",
                             height=16, font=("Courier New", 9))
        scroll = ttk.Scrollbar(text_frame, command=self._text.yview)
        self._text.configure(yscrollcommand=scroll.set)
        self._text.grid(row=0, column=0, sticky="nsew")
        scroll.grid(row=0, column=1, sticky="ns")

    def _log(self, line: str) -> None:
        self._text.configure(state="normal")
        self._text.insert(tk.END, line + "\n")
        self._text.see(tk.END)
        self._text.configure(state="disabled")

    def _clear(self) -> None:
        self._text.configure(state="normal")
        self._text.delete("1.0", tk.END)
        self._text.configure(state="disabled")

    def _start_run(self) -> None:
        if self._running:
            return
        try:
            cfg = self._get_config()
        except ValueError as exc:
            messagebox.showerror("Configuration error", str(exc))
            return

        self._stop_event = threading.Event()
        cfg = dataclasses.replace(cfg,
                                  stop_event=self._stop_event,
                                  on_progress=self._on_progress)

        self._running = True
        self._run_btn.configure(state="disabled")
        self._cancel_btn.configure(state="normal")
        self._status_var.set("Running…")
        self._progress.configure(mode="indeterminate", value=0)
        self._progress.start(12)
        self._clear()
        self._log("Starting…")

        def worker():
            try:
                result = LogProcessor(cfg).run()
                self.after(0, lambda: self._on_done(result, cfg))
            except Exception as exc:
                self.after(0, lambda: self._on_error(exc))

        threading.Thread(target=worker, daemon=True).start()

    def _on_progress(self, done: int, total: int) -> None:
        def update():
            if str(self._progress["mode"]) == "indeterminate":
                self._progress.stop()
                self._progress.configure(mode="determinate",
                                         maximum=max(total, 1))
            self._progress["value"] = done
            self._status_var.set(f"Processing {done} / {total}…")
        self.after(0, update)

    def _on_cancel(self) -> None:
        if self._stop_event is not None:
            self._stop_event.set()
        self._cancel_btn.configure(state="disabled")
        self._status_var.set("Cancelling…")

    def _on_done(self, result: ProcessorResult, cfg: ProcessorConfig) -> None:
        self._progress.stop()
        self._progress.configure(mode="determinate",
                                 maximum=max(result.files_found, 1),
                                 value=result.files_found)
        self._run_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")
        cancelled = self._stop_event is not None and self._stop_event.is_set()
        self._status_var.set("Cancelled" if cancelled else "Done")
        self._running = False

        self._log(f"Files found   : {result.files_found}")
        self._log(f"Files analyzed: {result.files_analyzed}")
        self._log(f"Files skipped : {result.files_skipped}")
        self._log(f"Matches       : {result.matches_total}")
        self._log(f"Output dir    : {cfg.output_config.output_dir}")

    def _on_error(self, exc: Exception) -> None:
        self._progress.stop()
        self._progress.configure(mode="determinate", value=0)
        self._run_btn.configure(state="normal")
        self._cancel_btn.configure(state="disabled")
        self._status_var.set("Error")
        self._running = False
        self._log(f"ERROR: {exc}")
        messagebox.showerror("Run failed", str(exc))


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class App(tk.Tk):
    def __init__(self):
        _set_windows_app_id()
        super().__init__()
        self.title("Log Master")
        self._set_app_icon()
        self.geometry("760x640")
        self.minsize(600, 500)

        self._current_config_path: Path | None = None

        self._build_menu()

        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=6, pady=6)

        self._files_tab    = FilesTab(self._nb)
        self._analysis_tab = AnalysisTab(self._nb)
        self._output_tab   = OutputTab(self._nb)
        self._results_tab  = ResultsTab(self._nb, self._build_config)

        self._nb.add(self._files_tab,    text="  Files  ")
        self._nb.add(self._analysis_tab, text="  Analysis  ")
        self._nb.add(self._output_tab,   text="  Output  ")
        self._nb.add(self._results_tab,  text="  Results  ")

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._load_autosave()

    def _set_app_icon(self) -> None:
        try:
            assets = resources.files("log_master.assets")
            if sys.platform == "win32":
                ico = assets.joinpath("log_master_icon.ico")
                with resources.as_file(ico) as ico_path:
                    self.iconbitmap(default=str(ico_path))
            icon_names = (
                "log_master_icon_16.png",
                "log_master_icon_32.png",
                "log_master_icon_64.png",
                "log_master_icon.png",
            )
            self._icon_images = []
            for icon_name in icon_names:
                icon = assets.joinpath(icon_name)
                with resources.as_file(icon) as icon_path:
                    self._icon_images.append(tk.PhotoImage(file=str(icon_path)))
            self.iconphoto(True, *self._icon_images)
        except Exception:
            self._icon_images = []

    # ------------------------------------------------------------------
    # Menu
    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        menubar = tk.Menu(self)

        file_menu = tk.Menu(menubar, tearoff=False)
        file_menu.add_command(label="Load Config…",    command=self._load_config,
                              accelerator="Ctrl+O")
        file_menu.add_command(label="Save Config",     command=self._save_config,
                              accelerator="Ctrl+S")
        file_menu.add_command(label="Save Config As…", command=self._save_config_as,
                              accelerator="Ctrl+Shift+S")
        file_menu.add_separator()
        file_menu.add_command(label="Exit",            command=self._on_close)

        menubar.add_cascade(label="File", menu=file_menu)
        self.configure(menu=menubar)

        self.bind_all("<Control-o>", lambda _: self._load_config())
        self.bind_all("<Control-s>", lambda _: self._save_config())
        self.bind_all("<Control-S>", lambda _: self._save_config_as())

    # ------------------------------------------------------------------
    # State serialisation
    # ------------------------------------------------------------------

    def get_state(self) -> dict:
        return {
            "files":    self._files_tab.get_state(),
            "analysis": self._analysis_tab.get_state(),
            "output":   self._output_tab.get_state(),
        }

    def set_state(self, state: dict) -> None:
        self._files_tab.set_state(state.get("files", {}))
        self._analysis_tab.set_state(state.get("analysis", {}))
        self._output_tab.set_state(state.get("output", {}))

    # ------------------------------------------------------------------
    # Load / save actions
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        path = filedialog.askopenfilename(
            title="Load config",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            state = json.loads(Path(path).read_text(encoding="utf-8"))
            self.set_state(state)
            self._current_config_path = Path(path)
            self.title(f"Log Master — {Path(path).name}")
        except Exception as exc:
            messagebox.showerror("Load failed", str(exc))

    def _save_config(self) -> None:
        if self._current_config_path:
            self._write_config(self._current_config_path)
        else:
            self._save_config_as()

    def _save_config_as(self) -> None:
        raw = filedialog.asksaveasfilename(
            title="Save config as",
            defaultextension=".json",
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")],
        )
        if not raw:
            return
        path = Path(raw)
        if self._write_config(path):
            self._current_config_path = path
            self.title(f"Log Master — {path.name}")

    def _write_config(self, path: Path) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(self.get_state(), indent=2),
                encoding="utf-8",
            )
            return True
        except Exception as exc:
            messagebox.showerror("Save failed", str(exc))
            return False

    # ------------------------------------------------------------------
    # Auto-save / auto-load
    # ------------------------------------------------------------------

    def _load_autosave(self) -> None:
        if _AUTOSAVE_PATH.exists():
            try:
                state = json.loads(_AUTOSAVE_PATH.read_text(encoding="utf-8"))
                self.set_state(state)
            except Exception:
                pass  # corrupt autosave — start fresh

    def _on_close(self) -> None:
        try:
            _AUTOSAVE_PATH.parent.mkdir(parents=True, exist_ok=True)
            _AUTOSAVE_PATH.write_text(
                json.dumps(self.get_state(), indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass
        self.destroy()

    # ------------------------------------------------------------------
    # Pipeline config builder
    # ------------------------------------------------------------------

    def _build_config(self) -> ProcessorConfig:
        try:
            return build_processor_config(self.get_state())
        except ValueError as exc:
            raise ValueError(str(exc)) from exc


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
