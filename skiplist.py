#!/usr/bin/env python3
"""Skip list — permanent, title-based skipping of discovered videos.

Purpose: A "skipped" video is one the user never wants to see again, even if it
         is re-discovered under a new video ID or re-uploaded. Skipping is keyed
         by *normalized title* (not video ID) and persisted to a per-profile
         text file that survives DB rebuilds. The file is human-editable:
         delete a line to un-skip.
Spec:    session spec S1 (Skip Forever) — see CLAUDE.md / this file's tests.
Tests:   test_app.py::TestSkipList

The file stores the *original* title (one per line) for readability; matching is
done on the normalized form, so casing/whitespace differences still match.
"""
from pathlib import Path


def normalize_title(title):
    """Lowercase, collapse internal whitespace, strip — the key used for matching."""
    return " ".join((title or "").lower().split())


def skip_file_for(db_path):
    """Path to the skip file sitting beside the given profile DB."""
    p = Path(db_path)
    return p.with_name(p.stem + "_skip.txt")


def load_skip_set(db_path):
    """Return the set of *normalized* skipped titles for this profile (empty if none)."""
    f = skip_file_for(db_path)
    if not f.exists():
        return set()
    out = set()
    for line in f.read_text(encoding="utf-8").splitlines():
        n = normalize_title(line)
        if n:
            out.add(n)
    return out


def is_skipped(db_path, title):
    """True if `title` (normalized) is in this profile's skip file."""
    return normalize_title(title) in load_skip_set(db_path)


def add_skip_title(db_path, title):
    """Append `title` to the skip file, deduped on the normalized form.

    Stores the original title text for readability. Returns True if it was newly
    added, False if blank or already present (idempotent — safe to call twice).
    """
    n = normalize_title(title)
    if not n:
        return False
    # Non-atomic read-then-append: safe because the only writer is the
    # single-threaded dashboard (dismiss handler); discovery subprocesses only
    # *read* this file. If a future path writes it concurrently, add a lock.
    if n in load_skip_set(db_path):
        return False
    f = skip_file_for(db_path)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a", encoding="utf-8") as fh:
        fh.write((title or "").strip() + "\n")
    return True
