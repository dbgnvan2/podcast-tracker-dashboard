#!/usr/bin/env python3
"""Throwaway spike — phase 2 of DESIGN-transcript-availability.md.

Question: which yt-dlp player client actually yields captions for our videos, and
at what cost?

Why this exists: yt-dlp gates some subtitle requests behind a PO Token (POT), and
only for clients that already require one. `fetch_transcripts.PLAYER_CLIENTS`
currently tries ["android", "web"] — i.e. it includes the affected client by
construction, on every one of ~179 videos per run.

Public evidence (yt-dlp issue #13075, maintainer bashonly):
  "This seems to only affect subtitles extracted with clients that already
   require a PO token (e.g. `web`). `ios` and `tv` subs do not seem to be
   affected."
  Workaround: --extractor-args "youtube:player_client=default,-web"

Per the yt-dlp PO Token Guide, for web/mweb the Subs PO Token is bound to the
video ID, so hand-supplying tokens cannot scale — the client set is the only
scalable lever. This measures which set to use.

Method (a biased A/B is worse than no A/B):
  * ONE attempt per (video, config) — no retries, no backoff. Retrying would mask
    the very throttling being measured, and would stretch the run.
  * Configs are interleaved round-robin and the video order is shuffled, so a
    cooldown beginning mid-test cannot pick the winner for us.
  * If the BASELINE records a block marker, the test is invalid — it says so and
    exits non-zero so a cooldown can never be mistaken for a verdict.
  * Read-only: nothing is written to the DB, no transcript files are kept.

Usage:
  python3 spike_clients.py                    # 12 videos, all configs
  python3 spike_clients.py --n=24
  python3 spike_clients.py --group=obtained   # obtained | not_available | mixed
"""
import os
import re
import sys
import time
import random
import sqlite3
import subprocess
from pathlib import Path

import profiles

# (label, player_client list). "-web" is yt-dlp's exclusion syntax.
CONFIGS = [
    ("android,web (current)", ["android", "web"]),
    ("android only", ["android"]),
    ("default,-web", ["default", "-web"]),
    ("tv", ["tv"]),
    ("ios", ["ios"]),
    ("tv,ios", ["tv", "ios"]),
]

# Mirrors fetch_transcripts.BLOCKED_MARKERS — one signal set, not two (P5).
BLOCK_MARKERS = (
    "429", "too many requests", "po token", "sabr",
    "missing subtitles languages", "sign in to confirm",
    "rate limit", "rate-limit", "ratelimit", "timeout", "timed out",
)
# The validity gate cares ONLY about transient throttling (a 429/IP cooldown),
# never persistent per-client PO-token gating: "po token" / "sabr" / "missing
# subtitles languages" appear on android/web even OUTSIDE a cooldown, so counting
# them as "baseline blocked" would make every A/B permanently INVALID and Part 2
# could never complete. Abort the comparison only on one of these.
THROTTLE_MARKERS = (
    "429", "too many requests", "rate limit", "rate-limit", "ratelimit",
    "sign in to confirm", "timeout", "timed out",
)

SUBS_VER_RE = re.compile(r"^(en|en-)\S*$", re.I)


def ytdlp_bin():
    """Same probing order as fetch_transcripts, so the test measures the real binary."""
    for p in ["/opt/homebrew/bin/yt-dlp", "/usr/local/bin/yt-dlp",
              os.path.expanduser("~/Library/Python/3.9/bin/yt-dlp")]:
        if os.path.isfile(p):
            return p
    return "yt-dlp"


def ytdlp_version(ytdlp):
    try:
        r = subprocess.run([ytdlp, "--version"], capture_output=True, text=True, timeout=30)
        return (r.stdout or r.stderr or "?").strip().splitlines()[0]
    except Exception as e:
        return "unknown (%s)" % type(e).__name__


def sample_from_db(n, group):
    """Pick ids to test. Groups exist so the table spans known-captioned,
    known-absent and unprobed videos — a single group would bias the result."""
    db = Path(profiles.load()["db_path"])
    conn = sqlite3.connect(str(db))
    conn.row_factory = sqlite3.Row

    allrows = [dict(r) for r in conn.execute(
        "SELECT id, url, video_title, quality_score, transcript_status FROM videos "
        "WHERE COALESCE(dismissed,0)=0 AND id IS NOT NULL AND url IS NOT NULL "
        "ORDER BY quality_score DESC").fetchall()]
    conn.close()

    if group == "mixed":
        per = max(1, n // 3)
        chunk = ([r for r in allrows if r["transcript_status"] == "obtained"][:per]
                 + [r for r in allrows if r["transcript_status"] == "not_available"][:per]
                 + [r for r in allrows if r["transcript_status"] == "not_requested"][:per])
        # top up to n from the remaining pool, best score first
        seen = {r["id"] for r in chunk}
        for r in allrows:
            if len(chunk) >= n:
                break
            if r["id"] not in seen:
                chunk.append(r)
        return chunk
    want = {"obtained": "obtained", "not_available": "not_available"}[group]
    return [r for r in allrows if r["transcript_status"] == want][:n]


def try_one(ytdlp, vid, url, clients, tmpdir):
    """ONE attempt. Returns (hit, seconds, marker_note, chars)."""
    cmd = [
        ytdlp, "--skip-download", "--write-auto-subs", "--write-subs",
        "--sub-langs", "en.*", "--sub-format", "vtt",
        "--extractor-args", "youtube:player_client=%s" % ",".join(clients),
        "--impersonate", "chrome", "--no-warnings",
        "--output", str(Path(tmpdir) / vid), url,
    ]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, time.time() - t0, "timeout", 0
    secs = time.time() - t0
    out = ((proc.stdout or "") + (proc.stderr or "")).lower()
    hit_files = sorted(Path(tmpdir).glob("%s*.vtt" % vid))
    chars = 0
    for f in hit_files:
        try:
            chars += len(f.read_text(errors="replace"))
        except Exception:
            pass
        f.unlink()
    marker = ""
    for m in BLOCK_MARKERS:
        if m in out:
            marker = m
            break
    return bool(hit_files), secs, marker, chars


def main():
    n = 12
    group = "mixed"
    for a in sys.argv[1:]:
        if a.startswith("--n="):
            n = int(a.split("=", 1)[1])
        elif a.startswith("--group="):
            group = a.split("=", 1)[1]

    ytdlp = ytdlp_bin()
    ver = ytdlp_version(ytdlp)
    rows = sample_from_db(n, group)
    if not rows:
        print("No videos matched (group=%s). Aborting." % group)
        return 2

    tmpdir = Path(profiles.HERMES) / "spike_clients_tmp"
    tmpdir.mkdir(parents=True, exist_ok=True)

    order = list(rows)
    random.shuffle(order)
    print("=" * 78)
    print("player-client A/B — yt-dlp %s" % ver)
    print("binary: %s" % ytdlp)
    print("videos: %d (group=%s), configs: %d, attempts: %d, retries: NONE"
          % (len(order), group, len(CONFIGS), len(order) * len(CONFIGS)))
    print("=" * 78)

    results = {label: {"hit": 0, "n": 0, "secs": 0.0, "blocked": 0, "chars": 0}
               for label, _ in CONFIGS}
    baseline_label = CONFIGS[0][0]
    baseline_blocked = False
    table = []

    # Round-robin over configs per video: a cooldown starting mid-test hits every
    # config roughly equally instead of whichever one happened to run.
    for i, v in enumerate(order, 1):
        for label, clients in CONFIGS:
            hit, secs, marker, chars = try_one(ytdlp, v["id"], v["url"], clients, tmpdir)
            r = results[label]
            r["n"] += 1
            r["secs"] += secs
            if hit:
                r["hit"] += 1
            r["chars"] += chars
            if marker:
                r["blocked"] += 1
                if label == baseline_label and marker in THROTTLE_MARKERS:
                    baseline_blocked = True   # transient throttle, not PO-token gating
            table.append((v["id"], label, hit, secs, marker, chars))
            print("  [%2d/%2d] %-22s %-22s %s %5.1fs %s"
                  % (i, len(order), v["id"], label,
                     "HIT " if hit else "miss", secs, marker))
        # small courtesy pause between videos (not between configs — that would
        # change the token bucket between configs and bias the comparison)
        time.sleep(1)

    print()
    print("%-24s %6s %8s %10s %9s" % ("config", "hit", "rate", "avg s", "blocked"))
    print("-" * 62)
    ranked = []
    for label, _ in CONFIGS:
        r = results[label]
        rate = (r["hit"] / r["n"]) if r["n"] else 0.0
        avg = (r["secs"] / r["n"]) if r["n"] else 0.0
        ranked.append((rate, -avg, label, r, avg))
        print("%-24s %6s %7.0f%% %10.2f %9d"
              % (label, "%d/%d" % (r["hit"], r["n"]), rate * 100, avg, r["blocked"]))

    print()
    if baseline_blocked:
        print("INVALID: the baseline (android,web) hit a throttle marker (429 / rate")
        print("limit / timeout), so this window is throttled and the comparison means")
        print("nothing. Re-run later, outside the cooldown.")
    else:
        ranked.sort(reverse=True)
        rate, negavg, label, r, avg = ranked[0]
        print("WINNER: %s — %d/%d hit (%.0f%%), %.2fs/video avg"
              % (label, r["hit"], r["n"], rate * 100, avg))
        print("Tie-break on seconds/video if hit rates are equal.")
        print()
        print("Next: set PLAYER_CLIENTS in fetch_transcripts.py to the winner's list,")
        print("then record this table in LEARNINGS.md (phase 2 acceptance).")
    try:
        tmpdir.rmdir()
    except OSError:
        pass
    return 1 if baseline_blocked else 0


if __name__ == "__main__":
    sys.exit(main())
