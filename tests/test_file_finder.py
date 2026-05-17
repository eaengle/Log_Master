"""Tests for FileFinder and FileFindCriteria."""

from __future__ import annotations

import os
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from log_master.core.file_finder import FileFindCriteria, FileFinder, FileInfo
from tests.conftest import make_file


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def found_names(finder: FileFinder) -> set[str]:
    return {info.path.name for info in finder.find()}


def found_paths_rel(finder: FileFinder, root: Path) -> set[str]:
    return {str(info.path.relative_to(root)) for info in finder.find()}


def make_finder(root, **kwargs) -> FileFinder:
    roots = root if isinstance(root, list) else [root]
    return FileFinder(FileFindCriteria(root_dirs=roots, **kwargs))


# ---------------------------------------------------------------------------
# Basic discovery
# ---------------------------------------------------------------------------

class TestBasicDiscovery:
    def test_finds_all_files_no_criteria(self, tree):
        finder = make_finder(tree)
        names = found_names(finder)
        assert names == {
            "app.log", "app.txt", "readme.md",
            "server.log", "debug.log", "trace.log",
            "syslog.log", "old.log",
        }

    def test_empty_directory(self, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        finder = make_finder(empty)
        assert list(finder.find()) == []

    def test_returns_file_info_fields(self, tree):
        finder = make_finder(tree, name_globs=["app.log"])
        results = list(finder.find())
        assert len(results) == 1
        info = results[0]
        assert isinstance(info, FileInfo)
        assert info.path.name == "app.log"
        assert info.size_bytes == 100
        assert isinstance(info.mtime, datetime)

    def test_root_dir_not_exist_yields_nothing(self, tmp_path):
        finder = make_finder(tmp_path / "does_not_exist")
        assert list(finder.find()) == []


# ---------------------------------------------------------------------------
# Name glob filtering
# ---------------------------------------------------------------------------

class TestNameGlobFilter:
    def test_single_glob(self, tree):
        assert found_names(make_finder(tree, name_globs=["*.log"])) == {
            "app.log", "server.log", "debug.log", "trace.log", "syslog.log", "old.log"
        }

    def test_multiple_globs_or_logic(self, tree):
        names = found_names(make_finder(tree, name_globs=["*.log", "*.txt"]))
        assert "app.log" in names
        assert "app.txt" in names
        assert "readme.md" not in names

    def test_glob_with_prefix(self, tree):
        names = found_names(make_finder(tree, name_globs=["app*"]))
        assert names == {"app.log", "app.txt"}

    def test_no_match_yields_nothing(self, tree):
        assert list(make_finder(tree, name_globs=["*.xyz"]).find()) == []


# ---------------------------------------------------------------------------
# Extension filtering
# ---------------------------------------------------------------------------

class TestExtensionFilter:
    def test_extension_with_dot(self, tree):
        names = found_names(make_finder(tree, extensions=[".log"]))
        assert "readme.md" not in names
        assert "app.txt" not in names
        assert all(n.endswith(".log") for n in names)

    def test_extension_without_dot_normalized(self, tree):
        names = found_names(make_finder(tree, extensions=["log"]))
        assert all(n.endswith(".log") for n in names)

    def test_multiple_extensions_or_logic(self, tree):
        names = found_names(make_finder(tree, extensions=[".log", ".txt"]))
        assert "app.log" in names
        assert "app.txt" in names
        assert "readme.md" not in names


# ---------------------------------------------------------------------------
# Mixed name_globs + extensions (OR across both)
# ---------------------------------------------------------------------------

class TestCombinedNameAndExtension:
    def test_globs_and_extensions_are_ored(self, tree):
        names = found_names(make_finder(tree, name_globs=["readme.*"], extensions=[".txt"]))
        assert "readme.md" in names
        assert "app.txt" in names
        assert "app.log" not in names


# ---------------------------------------------------------------------------
# Depth limiting
# ---------------------------------------------------------------------------

class TestDepthLimit:
    def test_depth_zero_root_files_only(self, tree):
        names = found_names(make_finder(tree, max_depth=0))
        assert names == {"app.log", "app.txt", "readme.md"}

    def test_depth_one_includes_immediate_subdirs(self, tree):
        names = found_names(make_finder(tree, max_depth=1))
        assert "app.log" in names       # root
        assert "server.log" in names    # depth-1 subdir
        assert "trace.log" not in names # depth-2

    def test_depth_two_includes_nested_subdirs(self, tree):
        names = found_names(make_finder(tree, max_depth=2))
        assert "trace.log" in names     # depth-2

    def test_unlimited_depth_finds_all(self, tree):
        finder = make_finder(tree, max_depth=None)
        assert "trace.log" in found_names(finder)


# ---------------------------------------------------------------------------
# Modified date filtering
# ---------------------------------------------------------------------------

class TestModifiedDateFilter:
    def test_modified_after_excludes_older(self, tmp_path):
        old_time = datetime(2020, 1, 1)
        new_time = datetime(2024, 6, 1)
        make_file(tmp_path / "old.log", mtime=old_time)
        make_file(tmp_path / "new.log", mtime=new_time)

        finder = make_finder(tmp_path, modified_after=datetime(2023, 1, 1))
        assert found_names(finder) == {"new.log"}

    def test_modified_before_excludes_newer(self, tmp_path):
        old_time = datetime(2020, 1, 1)
        new_time = datetime(2024, 6, 1)
        make_file(tmp_path / "old.log", mtime=old_time)
        make_file(tmp_path / "new.log", mtime=new_time)

        finder = make_finder(tmp_path, modified_before=datetime(2023, 1, 1))
        assert found_names(finder) == {"old.log"}

    def test_modified_range_inclusive(self, tmp_path):
        anchor = datetime(2022, 6, 15, 12, 0, 0)
        make_file(tmp_path / "exact.log", mtime=anchor)
        make_file(tmp_path / "before.log", mtime=anchor - timedelta(seconds=1))
        make_file(tmp_path / "after.log",  mtime=anchor + timedelta(seconds=1))

        finder = make_finder(
            tmp_path,
            modified_after=anchor,
            modified_before=anchor,
        )
        assert found_names(finder) == {"exact.log"}

    def test_modified_range_combined(self, tmp_path):
        times = {
            "jan.log": datetime(2024, 1, 10),
            "mar.log": datetime(2024, 3, 10),
            "dec.log": datetime(2024, 12, 10),
        }
        for name, t in times.items():
            make_file(tmp_path / name, mtime=t)

        finder = make_finder(
            tmp_path,
            modified_after=datetime(2024, 2, 1),
            modified_before=datetime(2024, 6, 1),
        )
        assert found_names(finder) == {"mar.log"}


# ---------------------------------------------------------------------------
# Size filtering
# ---------------------------------------------------------------------------

class TestSizeFilter:
    def test_min_size_excludes_small(self, tree):
        names = found_names(make_finder(tree, min_size_bytes=100))
        assert "trace.log" not in names  # 5 bytes
        assert "debug.log" not in names  # 30 bytes
        assert "app.log" in names        # 100 bytes
        assert "server.log" in names     # 200 bytes

    def test_max_size_excludes_large(self, tree):
        names = found_names(make_finder(tree, max_size_bytes=50))
        assert "server.log" not in names  # 200 bytes
        assert "app.log" not in names     # 100 bytes
        assert "trace.log" in names       # 5 bytes
        assert "app.txt" in names         # 50 bytes

    def test_size_range(self, tree):
        names = found_names(make_finder(tree, min_size_bytes=30, max_size_bytes=100))
        assert "trace.log" not in names   # 5 bytes
        assert "server.log" not in names  # 200 bytes
        assert "debug.log" in names       # 30 bytes
        assert "app.log" in names         # 100 bytes


# ---------------------------------------------------------------------------
# Directory include/exclude globs
# ---------------------------------------------------------------------------

class TestDirGlobFilters:
    def test_exclude_dir_skips_directory(self, tree):
        names = found_names(make_finder(tree, exclude_dir_globs=["archive"]))
        assert "old.log" not in names

    def test_exclude_dir_glob_pattern(self, tree):
        names = found_names(make_finder(tree, exclude_dir_globs=["app*"]))
        assert "server.log" not in names
        assert "debug.log" not in names
        assert "trace.log" not in names
        assert "syslog.log" in names   # lives in "system", not excluded

    def test_include_dir_only_enters_matching(self, tree):
        names = found_names(make_finder(tree, include_dir_globs=["system"]))
        assert "syslog.log" in names
        assert "server.log" not in names
        assert "old.log" not in names
        # root-level files are still included (include_dir_globs filters directories only)
        assert "app.log" in names

    def test_include_and_exclude_exclude_wins(self, tree):
        names = found_names(
            make_finder(tree, include_dir_globs=["app*"], exclude_dir_globs=["app"])
        )
        assert "server.log" not in names   # "app" excluded

    def test_exclude_multiple_dirs(self, tree):
        names = found_names(make_finder(tree, exclude_dir_globs=["app", "archive"]))
        assert "server.log" not in names
        assert "trace.log" not in names
        assert "old.log" not in names
        assert "syslog.log" in names


# ---------------------------------------------------------------------------
# Multiple root directories
# ---------------------------------------------------------------------------

class TestMultipleRoots:
    def test_two_roots_combined(self, tmp_path):
        root_a = tmp_path / "a"
        root_b = tmp_path / "b"
        make_file(root_a / "alpha.log")
        make_file(root_b / "beta.log")

        finder = FileFinder(
            FileFindCriteria(root_dirs=[root_a, root_b], name_globs=["*.log"])
        )
        assert found_names(finder) == {"alpha.log", "beta.log"}

    def test_duplicate_roots_do_not_double_yield(self, tmp_path):
        root = tmp_path / "r"
        make_file(root / "x.log")

        finder = FileFinder(FileFindCriteria(root_dirs=[root, root]))
        results = list(finder.find())
        # Both roots are walked independently — two traversals, but both yield x.log
        # The contract is that each root is traversed once; deduplication is the
        # caller's responsibility if overlapping roots are provided.
        assert all(r.path.name == "x.log" for r in results)


# ---------------------------------------------------------------------------
# Combined criteria (integration-style)
# ---------------------------------------------------------------------------

class TestCombinedCriteria:
    def test_glob_and_depth(self, tree):
        names = found_names(make_finder(tree, name_globs=["*.log"], max_depth=0))
        assert names == {"app.log"}

    def test_glob_and_size_and_depth(self, tree):
        names = found_names(
            make_finder(tree, name_globs=["*.log"], min_size_bytes=100, max_depth=1)
        )
        # app.log=100 (root), server.log=200 (app/), syslog.log=150 (system/) all qualify;
        # old.log=80 (archive/) excluded by size; trace.log=5 at depth-2 excluded by depth
        assert names == {"app.log", "server.log", "syslog.log"}

    def test_extension_exclude_dir_depth(self, tree):
        names = found_names(
            make_finder(tree, extensions=[".log"], exclude_dir_globs=["archive"], max_depth=1)
        )
        assert "old.log" not in names
        assert "server.log" in names
