"""Shared pytest fixtures."""

from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

import pytest


def make_file(path: Path, content: str = "", mtime: datetime | None = None) -> Path:
    """Create a file with optional content and mtime."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    if mtime is not None:
        ts = mtime.timestamp()
        os.utime(path, (ts, ts))
    return path


@pytest.fixture
def tree(tmp_path: Path):
    """
    Creates a directory tree used across multiple FileFinder tests:

        root/
          app.log          (100 bytes)
          app.txt          (50  bytes)
          readme.md        (10  bytes)
          app/
            server.log     (200 bytes)
            debug.log      (30  bytes)
            cache/
              trace.log    (5   bytes)
          system/
            syslog.log     (150 bytes)
          archive/
            old.log        (80  bytes)
    """
    root = tmp_path / "root"
    make_file(root / "app.log",                "x" * 100)
    make_file(root / "app.txt",                "x" * 50)
    make_file(root / "readme.md",              "x" * 10)
    make_file(root / "app"     / "server.log", "x" * 200)
    make_file(root / "app"     / "debug.log",  "x" * 30)
    make_file(root / "app"     / "cache" / "trace.log", "x" * 5)
    make_file(root / "system"  / "syslog.log", "x" * 150)
    make_file(root / "archive" / "old.log",    "x" * 80)
    return root
