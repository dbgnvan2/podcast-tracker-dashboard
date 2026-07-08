#!/usr/bin/env python3
"""Run state — the persisted result of the last transcript-processing run.

Purpose: Feature #2 — after a Process Queue run finishes, show a message that
         survives page reloads and remains until the user queues new work. The
         fetcher (producer) writes its own summary here; the dashboard reads it
         and clears it when a new video is requested for transcription.
Spec:    session spec — feature #2 (persistent "processed" message).
Tests:   test_app.py::TestTranscribeRunState, TestAPISmoke

Stored beside the profile DB (per-profile), so two profiles don't clobber each
other and the message survives a DB rebuild independent of the videos table.
"""
import json
from datetime import datetime
from pathlib import Path


def transcribe_state_file(db_path):
    """Path to the last-run state file sitting beside the given profile DB."""
    p = Path(db_path)
    return p.with_name(p.stem + "_last_transcribe.json")


def write_transcribe_result(db_path, ok, total, unavailable=0, retryable=0,
                            status="done", error=None):
    """Record the outcome of a processing run (date stamped here). Returns the dict.

    Splits non-successes into `unavailable` (terminal — no captions) and
    `retryable` (blocked/429 — will re-fetch when the block clears). These are
    kept distinct so the UI never labels a P1-retryable block as a terminal
    "failure" (LEARNINGS P1).
    """
    data = {
        "status": status,                       # "done" | "error"
        "ok": int(ok),
        "total": int(total),
        "unavailable": int(unavailable),        # terminal: no captions
        "retryable": int(retryable),            # transient: blocked, will retry (P1)
        "date": datetime.now().strftime("%Y-%m-%d"),
        "error": error,
    }
    f = transcribe_state_file(db_path)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(data), encoding="utf-8")
    return data


def read_transcribe_result(db_path):
    """Return the last run's result dict, or None if there is no active message."""
    f = transcribe_state_file(db_path)
    if not f.exists():
        return None
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except Exception:
        return None


def clear_transcribe_result(db_path):
    """Remove the message — called when the user queues new work for transcription."""
    try:
        transcribe_state_file(db_path).unlink()
    except FileNotFoundError:
        pass
