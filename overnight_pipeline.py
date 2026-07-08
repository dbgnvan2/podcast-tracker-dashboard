#!/usr/bin/env python3
"""Patient end-to-end runner: drain the transcript queue (riding out YouTube's
IP-wide 429 cooldown), analyze whatever lands, regenerate the digest, repeat.

Each round it promotes retryable 'error' rows back to 'requested', fetches,
analyzes, and rebuilds the digest, then sleeps. Stops when nothing is left to
fetch or after MAX_HOURS. Safe to run unattended overnight.

  python3 overnight_pipeline.py
"""
import os
import sys
import time
import sqlite3
import subprocess
from pathlib import Path

import fetch_transcripts
import analyze_transcripts
import generate_digest
import profiles

MAX_HOURS = 8
ROUND_SLEEP_SEC = 1200  # 20 min between rounds to let the 429 cooldown pass
MAX_FETCH_ATTEMPTS = 4  # stop re-promoting a stubbornly-failing 'error' row after N rounds


def get_db():
    """Resolve the active profile's DB on every call — never capture at import
    time, or a runtime profile switch would silently target the wrong DB."""
    return Path(profiles.load()["db_path"])


def _ensure_attempts_column(db):
    conn = sqlite3.connect(str(db))
    try:
        conn.execute("ALTER TABLE videos ADD COLUMN fetch_attempts INTEGER DEFAULT 0")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # column already exists
    finally:
        conn.close()


def counts(db=None):
    db = db or get_db()
    c = sqlite3.connect(str(db))
    c.row_factory = sqlite3.Row
    d = {r["transcript_status"]: r["c"] for r in c.execute(
        "SELECT transcript_status, COUNT(*) c FROM videos GROUP BY transcript_status")}
    c.close()
    return d


def promote_errors(db=None):
    """Re-queue retryable 'error' rows, but cap retries per row so a genuinely
    dead video (no captions, deleted) isn't re-fetched every round forever (P8).
    Returns (promoted, still_capped)."""
    db = db or get_db()
    c = sqlite3.connect(str(db))
    promoted = c.execute(
        "UPDATE videos SET transcript_status='requested', "
        "fetch_attempts=COALESCE(fetch_attempts,0)+1 "
        "WHERE transcript_status='error' AND COALESCE(fetch_attempts,0) < ?",
        (MAX_FETCH_ATTEMPTS,),
    ).rowcount
    capped = c.execute(
        "SELECT COUNT(*) FROM videos WHERE transcript_status='error' "
        "AND COALESCE(fetch_attempts,0) >= ?", (MAX_FETCH_ATTEMPTS,),
    ).fetchone()[0]
    c.commit()
    c.close()
    return promoted, capped


def write_digest():
    # force=True so each round rebuilds the FULL accumulated digest (not just the
    # newly-obtained tail). Write the file FIRST, then mark digested — never the
    # reverse (a failed write before marking loses videos forever; see
    # generate_digest.build_digest).
    md, day, ids = generate_digest.build_digest(force=True, limit=50)
    generate_digest.DIGEST_DIR.mkdir(parents=True, exist_ok=True)
    (generate_digest.DIGEST_DIR / f"digest_{day}.md").write_text(md, encoding="utf-8")
    (generate_digest.DIGEST_DIR / "latest.md").write_text(md, encoding="utf-8")
    generate_digest.mark_digested(ids, day)


def _already_running():
    """Refuse to start a second overnight pipeline against the same DB — two
    concurrent runners interleave status writes and race the 429 cooldown."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", "overnight_pipeline.py"],
            capture_output=True, text=True,
        ).stdout.split()
    except FileNotFoundError:
        return False  # no pgrep (non-mac/linux) — best effort, don't block
    # Exclude our own PID.
    others = [pid for pid in out if pid and int(pid) != os.getpid()]
    return bool(others)


def main():
    if _already_running():
        print("Another overnight_pipeline.py is already running — exiting.", flush=True)
        return

    db = get_db()
    _ensure_attempts_column(db)
    start = time.time()
    rnd = 0
    while time.time() - start < MAX_HOURS * 3600:
        rnd += 1
        promoted, capped = promote_errors(db)
        print(f"[round {rnd}] promoted {promoted} error->requested "
              f"({capped} capped out); before={counts(db)}", flush=True)

        fetch_transcripts.process_queue()
        try:
            analyze_transcripts.analyze_all()
        except Exception as e:
            print(f"  analyze error: {e}", flush=True)
        try:
            write_digest()
        except Exception as e:
            print(f"  digest error: {e}", flush=True)

        st = counts(db)
        remaining = st.get("requested", 0)  # promotable errors are now 'requested'
        print(f"[round {rnd}] after={st}; remaining fetchable={remaining}", flush=True)
        if remaining == 0:
            print("Queue drained — done.", flush=True)
            break
        print(f"[round {rnd}] sleeping {ROUND_SLEEP_SEC}s before retry…", flush=True)
        time.sleep(ROUND_SLEEP_SEC)

    try:
        write_digest()
    except Exception as e:
        print(f"  final digest error: {e}", flush=True)
    print(f"Pipeline finished after {rnd} round(s). Final status: {counts(db)}", flush=True)


if __name__ == "__main__":
    main()
