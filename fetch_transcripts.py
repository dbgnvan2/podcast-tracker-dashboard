#!/usr/bin/env python3
"""Transcription engine — drains the 'requested' queue into verified transcripts.

Data-first: a video only becomes 'obtained' when a real caption file is fetched
and saved. Captions are obtained programmatically via yt-dlp only — never from a
browser snapshot or a model's recollection (see CLAUDE.md Working Rule 0).

For each video it saves:
  ~/.hermes/transcripts/{id}.txt            clean full text
  ~/.hermes/transcripts/{id}.segments.json  [{start, text}] for timestamped analysis
"""
import os
import re
import json
import time
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime

import profiles
DB_PATH = Path(profiles.load()["db_path"])  # active investigation profile's DB
TRANSCRIPTS_DIR = profiles.HERMES / "transcripts"
TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

# yt-dlp: prefer a modern brew/standalone build; fall back to PATH.
YT_DLP = None
for p in [
    "/opt/homebrew/bin/yt-dlp",
    "/usr/local/bin/yt-dlp",
    os.path.expanduser("~/Library/Python/3.9/bin/yt-dlp"),
]:
    if os.path.isfile(p):
        YT_DLP = p
        break
if not YT_DLP:
    res = subprocess.run(["which", "yt-dlp"], capture_output=True, text=True)
    YT_DLP = res.stdout.strip()

# YouTube now gates auto-captions behind PO tokens for some clients and
# rate-limits the timedtext endpoint. The android client exposes the captions;
# browser impersonation (curl_cffi) reduces 429s. These are tried in order.
# android exposes the captions; web is a fallback. Keep per-video backoff short —
# an outer overnight loop handles the long IP-wide 429 cooldown.
PLAYER_CLIENTS = ["android", "web"]
MAX_429_RETRIES = 2
BACKOFF_BASE_SEC = 30  # 30, 60
MIN_CAPTION_CHARS = 100  # below this, treat as no usable captions

# Markers meaning "captions exist but were blocked / couldn't fetch right now" ->
# retryable 'error', as opposed to "genuinely no captions" -> 'not_available'.
# "timeout" is included: a 120s yt-dlp timeout is a transient/slow-network/IP-
# throttle condition, NOT proof captions are absent (LEARNINGS P1).
BLOCKED_MARKERS = (
    "429", "too many requests", "po token", "sabr",
    "missing subtitles languages", "sign in to confirm", "rate", "timeout",
)


def _ts_to_sec(ts):
    """'00:01:23.456' -> 83 (int seconds)."""
    h, m, s = ts.split(":")
    return int(float(h) * 3600 + float(m) * 60 + float(s))


def parse_vtt(vtt_path):
    """Return (clean_text, segments) where segments=[{'start':int,'text':str}]."""
    if not os.path.exists(vtt_path):
        return "", []
    segments = []
    cur_start = None
    cur_lines = []
    tag_re = re.compile(r"<[^>]+>")
    with open(vtt_path, "r", encoding="utf-8", errors="ignore") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if "-->" in line:
                # flush previous cue
                if cur_start is not None and cur_lines:
                    segments.append({"start": cur_start, "text": " ".join(cur_lines)})
                cur_start = _ts_to_sec(line.split("-->")[0].strip().split(" ")[0])
                cur_lines = []
            elif not line.strip() or line.startswith("WEBVTT") or line.startswith(("Kind:", "Language:", "NOTE")):
                continue
            else:
                txt = tag_re.sub("", line).strip()
                if txt:
                    cur_lines.append(txt)
    if cur_start is not None and cur_lines:
        segments.append({"start": cur_start, "text": " ".join(cur_lines)})

    deduped = dedup_rolling(segments)
    clean_text = " ".join(s["text"] for s in deduped)
    return clean_text, deduped


def dedup_rolling(segments):
    """Collapse YouTube rolling captions, where each cue repeats the tail of the
    previous one plus a few new words. For each cue we keep only the words that
    aren't already the running tail, preserving the timestamp of the new words."""
    out = []
    acc = []  # accumulated word list
    for seg in segments:
        words = seg["text"].split()
        if not words:
            continue
        max_k = min(len(acc), len(words))
        k = 0
        for cand in range(max_k, 0, -1):
            if acc[-cand:] == words[:cand]:
                k = cand
                break
        new_words = words[k:]
        if new_words:
            out.append({"start": seg["start"], "text": " ".join(new_words)})
            acc.extend(new_words)
    return out


def _run_ytdlp(vid, url):
    """Try player clients; return (returncode, combined_output). Retries on 429."""
    last_output = ""
    for client in PLAYER_CLIENTS:
        for attempt in range(MAX_429_RETRIES):
            cmd = [
                YT_DLP, "--skip-download", "--write-auto-subs", "--write-subs",
                "--sub-langs", "en.*", "--sub-format", "vtt",
                "--extractor-args", f"youtube:player_client={client}",
                "--impersonate", "chrome", "--no-warnings",
                "--sleep-requests", "2",
                "--output", str(TRANSCRIPTS_DIR / vid), url,
            ]
            try:
                proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
            except subprocess.TimeoutExpired:
                last_output = "timeout"
                continue
            out = (proc.stdout or "") + (proc.stderr or "")
            last_output = out
            files = list(TRANSCRIPTS_DIR.glob(f"{vid}*.vtt"))
            if files:
                return 0, out
            # Back off and retry on ANY transient block, not just 429 — a PO-token
            # / SABR / rate block is equally retryable (LEARNINGS P5: the in-loop
            # retry must use the same signal set as the status classification).
            if any(m in out.lower() for m in BLOCKED_MARKERS):
                wait = BACKOFF_BASE_SEC * (2 ** attempt)
                print(f"    blocked on client={client}, backoff {wait}s (attempt {attempt+1})")
                time.sleep(wait)
                continue
            break  # non-transient failure for this client; try next client
    return 1, last_output


def process_queue():
    if not YT_DLP:
        print("Error: yt-dlp not found.")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    videos = conn.execute(
        "SELECT id, url, video_title FROM videos WHERE transcript_status = 'requested'"
    ).fetchall()

    if not videos:
        print("No videos in requested queue.")
        conn.close()
        return

    total = len(videos)
    success_count = 0
    print(f"Processing {total} videos...")
    for i, v in enumerate(videos, 1):
        vid, url = v["id"], v["url"]
        print(f"[{i}/{total}] Fetching: {v['video_title']}")

        # Clear any stale vtt for this id
        for stale in TRANSCRIPTS_DIR.glob(f"{vid}*.vtt"):
            stale.unlink()

        rc, output = _run_ytdlp(vid, url)
        found = sorted(TRANSCRIPTS_DIR.glob(f"{vid}*.vtt"))

        if found:
            text, segments = parse_vtt(found[0])
            if len(text) > MIN_CAPTION_CHARS:
                txt_path = TRANSCRIPTS_DIR / f"{vid}.txt"
                txt_path.write_text(text, encoding="utf-8")
                (TRANSCRIPTS_DIR / f"{vid}.segments.json").write_text(
                    json.dumps(segments), encoding="utf-8"
                )
                conn.execute(
                    "UPDATE videos SET transcript_status='obtained', transcribed_date=? WHERE id=?",
                    (datetime.now().strftime("%Y-%m-%d"), vid),
                )
                conn.execute(
                    "INSERT OR REPLACE INTO transcripts (video_id, file_path, full_text, word_count) VALUES (?,?,?,?)",
                    (vid, str(txt_path), text, len(text.split())),
                )
                success_count += 1
                print(f"  Success: {len(text.split())} words, {len(segments)} segments.")
                for f in found:
                    f.unlink()
            elif any(m in output.lower() for m in BLOCKED_MARKERS):
                # A short/partial caption produced DURING a block (e.g. the
                # timedtext endpoint 429'd mid-download) is not proof captions are
                # absent — keep it retryable (LEARNINGS P1).
                print("  Captions truncated during a block -> error (will retry).")
                conn.execute("UPDATE videos SET transcript_status='error' WHERE id=?", (vid,))
                for f in found:
                    f.unlink()
            else:
                print("  Captions too short -> not_available.")
                conn.execute("UPDATE videos SET transcript_status='not_available' WHERE id=?", (vid,))
                for f in found:
                    f.unlink()
        else:
            lowered = output.lower()
            if any(m in lowered for m in BLOCKED_MARKERS):
                print("  Blocked (PO token / SABR / rate limit) -> error (will retry).")
                conn.execute("UPDATE videos SET transcript_status='error' WHERE id=?", (vid,))
            else:
                print("  No captions available -> not_available.")
                conn.execute("UPDATE videos SET transcript_status='not_available' WHERE id=?", (vid,))

        conn.commit()

    print(f"--- Done: {success_count}/{total} transcribed ---")
    conn.close()


if __name__ == "__main__":
    process_queue()
