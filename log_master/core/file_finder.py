"""File discovery with configurable filter criteria."""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Iterator


@dataclass
class FileInfo:
    """Metadata for a discovered file."""

    path: Path
    root: Path       # root_dir under which this file was discovered
    size_bytes: int
    mtime: datetime  # local time, timezone-naive


@dataclass
class FileFindCriteria:
    """
    Criteria for selecting files during discovery.

    Within a single criterion type that accepts multiple values (e.g. name_globs,
    extensions) the logic is OR — a file passes if it matches any value.
    Across different criterion types the logic is AND — all active criteria must pass.

    Depth is measured from each root_dir: depth=0 yields only files directly inside
    the root, depth=1 includes one level of subdirectories, None means unlimited.
    """

    root_dirs: list[Path] = field(default_factory=list)
    name_globs: list[str] = field(default_factory=list)
    extensions: list[str] = field(default_factory=list)
    modified_after: datetime | None = None
    modified_before: datetime | None = None
    min_size_bytes: int | None = None
    max_size_bytes: int | None = None
    max_depth: int | None = None
    include_dir_globs: list[str] = field(default_factory=list)
    exclude_dir_globs: list[str] = field(default_factory=list)


class FileFinder:
    """
    Yields FileInfo for every file reachable from criteria.root_dirs that passes
    all active criteria.  Results are produced incrementally — no directory listing
    is held in memory beyond the entries of the directory currently being scanned.
    Each file is visited exactly once.
    """

    def __init__(self, criteria: FileFindCriteria) -> None:
        self._c = criteria
        # Normalise extensions into glob form and merge with name_globs so the
        # filename match is a single OR over one list.
        combined: list[str] = list(criteria.name_globs)
        for ext in criteria.extensions:
            normalized = ext if ext.startswith(".") else f".{ext}"
            combined.append(f"*{normalized}")
        self._name_globs: list[str] = combined

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def find(self) -> Iterator[FileInfo]:
        """Yield matching FileInfo objects. Unreadable directories are silently skipped."""
        for root in self._c.root_dirs:
            resolved = Path(root).resolve()
            if resolved.is_dir():
                yield from self._walk(resolved, resolved, depth=0)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _walk(self, root: Path, directory: Path, depth: int) -> Iterator[FileInfo]:
        try:
            entries = list(directory.iterdir())
        except PermissionError:
            return

        # Yield files in this directory first, then recurse into subdirectories.
        # Sorting within each group keeps discovery order deterministic.
        files = sorted((e for e in entries if e.is_file()), key=lambda e: e.name.lower())
        dirs = sorted((e for e in entries if e.is_dir()), key=lambda e: e.name.lower())

        for entry in files:
            info = self._stat(entry, root)
            if info is not None and self._file_passes(info):
                yield info

        for entry in dirs:
            if self._c.max_depth is not None and depth >= self._c.max_depth:
                continue
            if self._dir_passes(entry):
                yield from self._walk(root, entry, depth + 1)

    def _stat(self, path: Path, root: Path) -> FileInfo | None:
        try:
            s = path.stat()
            return FileInfo(
                path=path,
                root=root,
                size_bytes=s.st_size,
                mtime=datetime.fromtimestamp(s.st_mtime),
            )
        except OSError:
            return None

    def _file_passes(self, info: FileInfo) -> bool:
        c = self._c

        if self._name_globs and not any(
            fnmatch.fnmatch(info.path.name, g) for g in self._name_globs
        ):
            return False

        if c.modified_after is not None and info.mtime < c.modified_after:
            return False

        if c.modified_before is not None and info.mtime > c.modified_before:
            return False

        if c.min_size_bytes is not None and info.size_bytes < c.min_size_bytes:
            return False

        if c.max_size_bytes is not None and info.size_bytes > c.max_size_bytes:
            return False

        return True

    def _dir_passes(self, directory: Path) -> bool:
        name = directory.name
        c = self._c

        if c.exclude_dir_globs and any(
            fnmatch.fnmatch(name, g) for g in c.exclude_dir_globs
        ):
            return False

        if c.include_dir_globs and not any(
            fnmatch.fnmatch(name, g) for g in c.include_dir_globs
        ):
            return False

        return True
