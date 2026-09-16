#!/usr/bin/env python3
"""Per-DB writer lock — one writer per profile database.

Purpose: stop two pipeline runners (weekly_run.py, overnight_pipeline.py) writing
         the SAME profile DB at once, without blocking a run on a DIFFERENT
         profile's DB. Replaces the `pgrep -f <script name>` guards, which matched
         any process whose argv merely mentioned the name, ignored which DB it
         wrote, and were one-directional (overnight never looked for weekly).
Spec:    QA gate docs/cycles/2026-09-16_weekly-runner-qa-gate.md F4, F5, F7.
Tests:   test_app.py::TestWriterLock

The lock is a file beside the DB (`<db>.writer.lock`) holding the owner's PID.
A lock whose PID is no longer alive (a crashed/killed run) is stale and is taken
over, so a SIGKILL cannot wedge the weekly job forever.
"""
import os
from datetime import datetime
from pathlib import Path


def lock_path(db):
    p = Path(db)
    return p.with_name(p.name + ".writer.lock")


def _pid_alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by another user
    return True


def lock_holder(content, mypid, alive=_pid_alive):
    """Pure decision: the PID that blocks us, or None if the lock is ours/stale.

    Unreadable content counts as stale — a half-written lock from a crash must not
    block every future run."""
    try:
        pid = int(str(content).split()[0])
    except (ValueError, IndexError):
        return None
    if pid == mypid or not alive(pid):
        return None
    return pid


def acquire(db, owner):
    """Take the writer lock for `db`. Returns (True, None) on success, or
    (False, holder) where holder is the live PID holding it ("unknown" if the
    file kept changing under us)."""
    path = lock_path(db)
    path.parent.mkdir(parents=True, exist_ok=True)
    for _ in range(3):
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            try:
                content = path.read_text()
            except OSError:
                content = ""
            holder = lock_holder(content, os.getpid())
            if holder is not None:
                return False, holder
            try:
                path.unlink()  # stale or ours from an earlier call — take it over
            except FileNotFoundError:
                pass
            continue
        with os.fdopen(fd, "w") as f:
            f.write(f"{os.getpid()} {owner} {datetime.now().isoformat(timespec='seconds')}\n")
        return True, None
    return False, "unknown"


def release(db):
    """Remove the lock only if this process holds it."""
    path = lock_path(db)
    try:
        pid = int(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return
    if pid == os.getpid():
        try:
            path.unlink()
        except FileNotFoundError:
            pass
