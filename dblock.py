#!/usr/bin/env python3
"""Per-DB writer lock — one writer per profile database.

Purpose: stop two pipeline writers (weekly_run.py, overnight_pipeline.py, and the
         dashboard-spawned stage scripts) writing the SAME profile DB at once,
         without blocking a writer on a DIFFERENT profile's DB.
Spec:    QA gates docs/cycles/2026-09-16_weekly-runner-qa-gate.md F4/F5/F7 and
         docs/cycles/2026-09-16_weekly-runner-qa-gate-2.md F4/F5.
Tests:   test_app.py::TestWriterLock

The lock is an `flock` on `<db>.writer.lock`. The kernel grants it atomically and
releases it when the holder exits or is killed, so there is no create→write window
to steal and no stale file to clean up. The owner line written into the file is
diagnostics only — nothing decides from it. The file itself is never deleted
(unlinking a locked file would let a second process lock a new inode).

Re-entrant within one process: weekly_run holds the lock and calls stage functions
in-process, so a nested acquire in the same process succeeds.

Who takes it: weekly_run, overnight_pipeline, the CLI entries of podcast_scraper
(except --test), fetch_transcripts, analyze_transcripts, generate_digest,
ingest_literature, and `dashboard_server.py --migrate/--reconcile`.

Named exemptions (deliberately unlocked):
  - generate_report.py writes report files only, no DB rows.
  - the dashboard's in-request writes: request/unrequest transcription,
    dismiss/undismiss, promote channel, accept/reject term, and the additive
    migrate() run when switching to or creating a profile. Each is one short
    statement (or idempotent DDL) serialised by SQLite's busy timeout. They can
    change a row a running stage also reads (e.g. un-requesting a video mid-fetch),
    which is the same last-writer-wins behaviour the dashboard has always had.
    Blocking them would freeze the UI for the length of a weekly run.
"""
import fcntl
import os
import sys
import time
from datetime import datetime
from pathlib import Path

# How long a CLI stage script waits for another writer before giving up (seconds).
# A dashboard click during a weekly drain queues behind it instead of racing it.
WAIT_SEC = float(os.environ.get("PTD_LOCK_WAIT_SEC", 2 * 3600))
POLL_SEC = 2.0
EXIT_LOCK_TIMEOUT = 3

_held = {}  # lock path -> [fd, depth]


def lock_path(db):
    p = Path(db)
    return p.with_name(p.name + ".writer.lock")


def _read_owner(path):
    try:
        return Path(path).read_text().strip() or "unknown"
    except OSError:
        return "unknown"


def acquire(db, owner, wait_sec=0.0, poll_sec=POLL_SEC, on_wait=None):
    """Take the writer lock for `db`. Returns (True, None), or (False, holder)
    where holder is the holder's owner line ("<pid> <owner> <since>")."""
    path = str(lock_path(db))
    if path in _held:
        _held[path][1] += 1
        return True, None
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + wait_sec
    announced = False
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            holder = _read_owner(path)
            if time.monotonic() >= deadline:
                os.close(fd)
                return False, holder
            if on_wait and not announced:
                on_wait(holder)
                announced = True
            time.sleep(poll_sec)
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()} {owner} {datetime.now().isoformat(timespec='seconds')}\n"
             .encode())
    _held[path] = [fd, 1]
    return True, None


def release(db):
    """Release one level of this process's hold on `db`'s lock."""
    path = str(lock_path(db))
    entry = _held.get(path)
    if not entry:
        return
    entry[1] -= 1
    if entry[1] == 0:
        fcntl.flock(entry[0], fcntl.LOCK_UN)
        os.close(entry[0])
        del _held[path]


def holder(db):
    """Who holds `db`'s lock right now: None if free (or held by this process),
    else the holder's owner line. Probes without keeping the lock."""
    path = str(lock_path(db))
    if path in _held or not os.path.exists(path):
        return None
    fd = os.open(path, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return _read_owner(path)
    else:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    finally:
        os.close(fd)


def run_locked(db, owner, fn, wait_sec=None):
    """CLI entry wrapper for a DB-writing stage script: wait (up to WAIT_SEC) for
    any other writer, run `fn`, release. Exits EXIT_LOCK_TIMEOUT if the wait
    runs out — never runs unlocked."""
    wait = WAIT_SEC if wait_sec is None else wait_sec
    ok, who = acquire(db, owner, wait_sec=wait,
                      on_wait=lambda h: print(f"Waiting for another writer on {db}: {h}",
                                              flush=True))
    if not ok:
        print(f"Gave up after {wait:.0f}s: another writer still holds {db}: {who}",
              flush=True)
        sys.exit(EXIT_LOCK_TIMEOUT)
    try:
        return fn()
    finally:
        release(db)
