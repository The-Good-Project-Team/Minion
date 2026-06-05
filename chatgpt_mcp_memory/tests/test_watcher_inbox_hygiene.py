"""Inbox hygiene: the watcher must never descend into opaque bundles or
dependency/cache trees, and must skip pathologically large files.

Regression for the production incident where a machine archive containing
`.app` bundles (each holding tens of thousands of vendored text files) was
dropped into the inbox. With no walk-level filter the watcher queued every
file for embedding, pinning all cores and driving a SQLite corruption /
re-ingest loop.
"""
from __future__ import annotations

from pathlib import Path

import watcher
from watcher import (
    _is_excluded_dir_name,
    _is_excluded_path,
    _iter_inbox_files,
)


def test_excluded_dir_names() -> None:
    # macOS bundles (opaque to Finder) — skipped by suffix.
    for name in ("Kindle Previewer 3.app", "Foo.framework", "Bar.bundle",
                 "en.lproj", "Thing.xcodeproj"):
        assert _is_excluded_dir_name(name), name
    # Dependency / cache / VCS trees.
    for name in ("node_modules", "__pycache__", "site-packages", "venv",
                 "Pods", ".git", ".cache", ".venv"):
        assert _is_excluded_dir_name(name), name
    # Ordinary knowledge folders are kept.
    for name in ("Notes", "Practice of Life", "2026", "research", "app"):
        assert not _is_excluded_dir_name(name), name


def test_iter_inbox_files_prunes_bundles_and_deps(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    # Real knowledge the user dropped.
    keep = inbox / "Notes" / "journal.md"
    keep.parent.mkdir(parents=True)
    keep.write_text("real note", encoding="utf-8")

    # A bundle full of junk text files — must be pruned wholesale.
    jre_legal = inbox / "Kindle Previewer 3.app" / "Contents" / "lib" / "jre" / "legal"
    jre_legal.mkdir(parents=True)
    for i in range(50):
        (jre_legal / f"license_{i}.md").write_text("LICENSE", encoding="utf-8")

    # A dependency tree, also pruned.
    nm = inbox / "project" / "node_modules" / "left-pad"
    nm.mkdir(parents=True)
    (nm / "readme.md").write_text("vendored", encoding="utf-8")

    found = {p.name for p in _iter_inbox_files(inbox)}
    assert "journal.md" in found
    assert not any(name.startswith("license_") for name in found)
    assert "readme.md" not in found


def test_iter_inbox_files_skips_oversized(tmp_path: Path, monkeypatch) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    small = inbox / "small.md"
    small.write_text("hi", encoding="utf-8")
    big = inbox / "big.md"
    big.write_text("x" * 5000, encoding="utf-8")

    monkeypatch.setenv("MINION_MAX_INGEST_BYTES", "1000")
    names = {p.name for p in _iter_inbox_files(inbox)}
    assert "small.md" in names
    assert "big.md" not in names


def test_is_excluded_path_live(tmp_path: Path) -> None:
    inbox = (tmp_path / "inbox").resolve()
    inbox.mkdir()
    inside_bundle = inbox / "Some.app" / "Contents" / "x.md"
    normal = inbox / "Notes" / "x.md"
    assert _is_excluded_path(inside_bundle, inbox)
    assert not _is_excluded_path(normal, inbox)
    # The inbox's own ancestry must never trip the filter even if a parent
    # directory happens to look bundle-ish.
    assert not _is_excluded_path(inbox / "plain.md", inbox)
