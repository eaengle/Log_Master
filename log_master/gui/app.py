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
"""

from __future__ import annotations

import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from log_master.core.expression_analyzer import SearchConfig
from log_master.core.file_finder import FileFindCriteria
from log_master.core.log_processor import LogProcessor, ProcessorConfig, ProcessorResult
from log_master.core.output_writer import Column, OutputConfig, OutputMode, SortOrder


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

    entry.bind("<Return>", lambda _: add())
    ttk.Button(frame, text="Add", command=add, width=6).grid(
        row=1, column=1, padx=2)
    ttk.Button(frame, text="Remove", command=remove, width=8).grid(
        row=1, column=2, padx=2)

    return lb


def _listbox_items(lb: tk.Listbox) -> list[str]:
    return list(lb.get(0, tk.END))


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

        self._mod_after_var = _labeled_entry(self, "Modified after:", 6, width=20)
        ttk.Label(self, text="YYYY-MM-DD").grid(row=6, column=2, sticky="w", padx=4)

        self._mod_before_var = _labeled_entry(self, "Modified before:", 7, width=20)
        ttk.Label(self, text="YYYY-MM-DD").grid(row=7, column=2, sticky="w", padx=4)

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

    def build_criteria(self) -> FileFindCriteria:
        from datetime import datetime

        def parse_date(s: str, field: str) -> datetime | None:
            s = s.strip()
            if not s:
                return None
            try:
                return datetime.strptime(s, "%Y-%m-%d")
            except ValueError:
                raise ValueError(f"{field}: expected YYYY-MM-DD, got '{s}'")

        def parse_int(var: tk.StringVar, field: str) -> int | None:
            s = var.get().strip()
            if not s:
                return None
            try:
                return int(s)
            except ValueError:
                raise ValueError(f"{field}: expected integer, got '{s}'")

        return FileFindCriteria(
            root_dirs=[Path(r) for r in self.roots()],
            name_globs=self._csv(self._globs_var),
            extensions=self._csv(self._ext_var),
            max_depth=parse_int(self._depth_var, "Max depth"),
            min_size_bytes=parse_int(self._min_size_var, "Min size"),
            max_size_bytes=parse_int(self._max_size_var, "Max size"),
            modified_after=parse_date(self._mod_after_var.get(), "Modified after"),
            modified_before=parse_date(self._mod_before_var.get(), "Modified before"),
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

        self._time_from_var = _labeled_entry(opts, "Time from:", 0, width=22)
        ttk.Label(opts, text="YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS").grid(
            row=0, column=2, sticky="w", padx=4)

        self._time_to_var = _labeled_entry(opts, "Time to:", 1, width=22)
        ttk.Label(opts, text="YYYY-MM-DD or YYYY-MM-DDTHH:MM:SS").grid(
            row=1, column=2, sticky="w", padx=4)

        self._case_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(opts, text="Case insensitive",
                        variable=self._case_var).grid(
            row=2, column=0, columnspan=2, sticky="w", padx=4, pady=4)

        self._context_var = _labeled_spin(opts, "Context lines:", 3,
                                          from_=0, to=50, default="0")

    def build_config(self) -> SearchConfig:
        from datetime import datetime

        def parse_dt(s: str, field: str):
            s = s.strip()
            if not s:
                return None
            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
                try:
                    return datetime.strptime(s, fmt)
                except ValueError:
                    pass
            raise ValueError(f"{field}: expected YYYY-MM-DD[THH:MM:SS], got '{s}'")

        ctx = self._context_var.get().strip()
        try:
            context_lines = int(ctx) if ctx else 0
        except ValueError:
            raise ValueError(f"Context lines: expected integer, got '{ctx}'")

        return SearchConfig(
            include_patterns=tuple(_listbox_items(self._include_lb)),
            exclude_patterns=tuple(_listbox_items(self._exclude_lb)),
            skip_file_patterns=tuple(_listbox_items(self._skip_lb)),
            time_from=parse_dt(self._time_from_var.get(), "Time from"),
            time_to=parse_dt(self._time_to_var.get(), "Time to"),
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

        self._base_path_var = _labeled_entry(opts, "Base path:", 2, width=30)
        ttk.Label(opts, text="(source_file written relative to this)").grid(
            row=2, column=2, sticky="w", padx=4)

        def browse_base():
            d = filedialog.askdirectory(title="Select base path")
            if d:
                self._base_path_var.set(d)

        ttk.Button(opts, text="Browse…", command=browse_base).grid(
            row=2, column=3, padx=4)

    def output_dir(self) -> str:
        return self._out_dir_var.get().strip()

    def workers(self) -> int:
        s = self._workers_var.get().strip()
        try:
            return int(s) if s else 1
        except ValueError:
            return 1

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

        base_raw = self._base_path_var.get().strip()

        return OutputConfig(
            output_dir=Path(out_dir),
            modes=frozenset(modes),
            columns=columns,
            sort=sort,
            include_context=self._inc_ctx_var.get(),
            base_path=Path(base_raw) if base_raw else None,
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

        # Run button
        btn_frame = ttk.Frame(self)
        btn_frame.grid(row=0, column=0, sticky="ew", pady=4)

        self._run_btn = ttk.Button(btn_frame, text="▶  Run",
                                   command=self._start_run, width=14)
        self._run_btn.pack(side="left", padx=4)

        self._status_var = tk.StringVar(value="Ready")
        ttk.Label(btn_frame, textvariable=self._status_var,
                  foreground="gray").pack(side="left", padx=12)

        # Progress bar
        self._progress = ttk.Progressbar(self, mode="indeterminate")
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

        self._running = True
        self._run_btn.configure(state="disabled")
        self._status_var.set("Running…")
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

    def _on_done(self, result: ProcessorResult, cfg: ProcessorConfig) -> None:
        self._progress.stop()
        self._run_btn.configure(state="normal")
        self._status_var.set("Done")
        self._running = False

        self._log(f"Files found   : {result.files_found}")
        self._log(f"Files analyzed: {result.files_analyzed}")
        self._log(f"Files skipped : {result.files_skipped}")
        self._log(f"Matches       : {result.matches_total}")
        self._log(f"Output dir    : {cfg.output_config.output_dir}")

    def _on_error(self, exc: Exception) -> None:
        self._progress.stop()
        self._run_btn.configure(state="normal")
        self._status_var.set("Error")
        self._running = False
        self._log(f"ERROR: {exc}")
        messagebox.showerror("Run failed", str(exc))


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Log Master")
        self.geometry("760x640")
        self.minsize(600, 500)

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

    def _build_config(self) -> ProcessorConfig:
        roots = self._files_tab.roots()
        if not roots:
            raise ValueError("Add at least one root directory in the Files tab.")

        find_criteria = self._files_tab.build_criteria()
        search_config = self._analysis_tab.build_config()
        output_config = self._output_tab.build_config()
        workers = self._output_tab.workers()

        return ProcessorConfig(
            find_criteria=find_criteria,
            search_config=search_config,
            output_config=output_config,
            workers=workers,
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    app = App()
    app.mainloop()


if __name__ == "__main__":
    main()
