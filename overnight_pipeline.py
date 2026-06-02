#!/usr/bin/env python3
"""Patient end-to-end runner: drain the transcript queue (riding out YouTube's
IP-wide 429 cooldown), analyze whatever lands, regenerate the digest, repeat.

Each round it promotes retryable 'error' rows back to 'requested', fetches,
analyzes, and rebuilds the digest, then sleeps. Stops when nothing is left to
fetch or after MAX_HOURS. Safe to run unattended overnight.

  python3 overnight_pipeline.py
"""
import time
import sqlite3
from pathlib import Path

import fetch_transcripts
import analyze_transcripts
import generate_digest

DB = Path.home() / ".hermes" / "podcast_tracker.db"
MAX_HOURS = 8
ROUND_SLEEP_SEC = 1200  # 20 min between rounds to let the 429 cooldown pass


def counts():
    c = sqlite3.connect(str(DB))
    c.row_factory = sqlite3.Row
    d = {r["transcript_status"]: r["c"] for r in c.execute(
        "SELECT transcript_status, COUNT(*) c FROM videos GROUP BY transcript_status")}
    c.close()
    return d


def promote_errors():
    c = sqlite3.connect(str(DB))
    n = c.execute("UPDATE videos SET transcript_status='requested' "
                  "WHERE transcript_status='error'").rowcount
    c.commit()
    c.close()
    return n


def write_digest():
    md, day = generate_digest.build_digest(days=None, limit=10)
    (generate_digest.DIGEST_DIR / f"digest_{day}.md").write_text(md, encoding="utf-8")
    (generate_digest.DIGEST_DIR / "latest.md").write_text(md, encoding="utf-8")


def main():
    start = time.time()
    rnd = 0
    while time.time() - start < MAX_HOURS * 3600:
        rnd += 1
        promoted = promote_errors()
        print(f"[round {rnd}] promoted {promoted} error->requested; before={counts()}", flush=True)

        fetch_transcripts.process_queue()
        try:
            analyze_transcripts.analyze_all()
        except Exception as e:
            print(f"  analyze error: {e}", flush=True)
        write_digest()

        st = counts()
        remaining = st.get("requested", 0) + st.get("error", 0)
        print(f"[round {rnd}] after={st}; remaining fetchable={remaining}", flush=True)
        if remaining == 0:
            print("Queue drained — done.", flush=True)
            break
        print(f"[round {rnd}] sleeping {ROUND_SLEEP_SEC}s before retry…", flush=True)
        time.sleep(ROUND_SLEEP_SEC)

    write_digest()
    print(f"Pipeline finished after {rnd} round(s). Final status: {counts()}", flush=True)


if __name__ == "__main__":
    main()
