#!/usr/bin/env python3
import os
import json
import sqlite3
import subprocess
from pathlib import Path
from datetime import datetime

# Config
DB_PATH = Path.home() / ".hermes" / "podcast_tracker.db"
TRANSCRIPTS_DIR = Path.home() / ".hermes" / "transcripts"
TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)

# Find yt-dlp (using your existing logic)
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

def clean_vtt(vtt_path):
    """Simple parser to turn VTT into clean text."""
    lines = []
    if not os.path.exists(vtt_path):
        return ""
    with open(vtt_path, 'r') as f:
        for line in f:
            if '-->' in line or line.startswith('WEBVTT') or not line.strip():
                continue
            # Remove simple HTML tags and duplicates
            clean = line.strip()
            if clean and (not lines or lines[-1] != clean):
                lines.append(clean)
    return " ".join(lines)

def process_queue():
    if not YT_DLP:
        print("Error: yt-dlp not found.")
        return

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    videos = conn.execute("SELECT id, url, video_title FROM videos WHERE transcript_status = 'requested'").fetchall()
    
    if not videos:
        print("No videos in requested queue.")
        return

    print(f"Processing {len(videos)} videos...")

    for v in videos:
        vid = v['id']
        url = v['url']
        print(f"Fetching: {v['video_title']} ({vid})")
        
        # Temp vtt path
        vtt_out = TRANSCRIPTS_DIR / f"{vid}.en"
        
        # yt-dlp command to get auto-subs as vtt
        cmd = [
            YT_DLP,
            "--skip-download",
            "--write-auto-subs",
            "--sub-langs", "en.*",
            "--output", str(TRANSCRIPTS_DIR / vid),
            url
        ]
        
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
            yt_output = (proc.stdout or "") + (proc.stderr or "")

            # Find the actual file (yt-dlp appends .en.vtt or similar)
            found_files = list(TRANSCRIPTS_DIR.glob(f"{vid}.*.vtt"))
            if found_files:
                vtt_file = found_files[0]
                text = clean_vtt(vtt_file)
                
                if len(text) > 100:
                    txt_path = TRANSCRIPTS_DIR / f"{vid}.txt"
                    with open(txt_path, 'w') as f:
                        f.write(text)
                    
                    word_count = len(text.split())
                    
                    # Update DB
                    conn.execute("""
                        UPDATE videos 
                        SET transcript_status = 'obtained', 
                            transcribed_date = ? 
                        WHERE id = ?
                    """, (datetime.now().strftime("%Y-%m-%d"), vid))
                    
                    # Add to transcripts table
                    conn.execute("""
                        INSERT OR REPLACE INTO transcripts (video_id, file_path, full_text, word_count)
                        VALUES (?, ?, ?, ?)
                    """, (vid, str(txt_path), text, word_count))
                    
                    print(f"  Success: {word_count} words.")
                    # Cleanup VTT
                    for f in found_files: os.remove(f)
                else:
                    print("  Warning: Transcript too short.")
                    conn.execute("UPDATE videos SET transcript_status = 'not_available' WHERE id = ?", (vid,))
            else:
                # Distinguish "captions truly absent" from "captions gated/blocked".
                # YouTube now gates auto-captions behind a PO token; an outdated
                # yt-dlp (or SABR streaming) reports them as missing even though
                # they exist. Treat those as retryable 'error', not 'not_available'.
                blocked_markers = (
                    "po token", "sabr", "missing subtitles languages",
                    "sign in to confirm", "rate", "http error 429",
                )
                lowered = yt_output.lower()
                if any(m in lowered for m in blocked_markers):
                    print("  Blocked: captions gated (PO token / SABR / rate limit) -> error (will retry).")
                    conn.execute("UPDATE videos SET transcript_status = 'error' WHERE id = ?", (vid,))
                else:
                    print("  No captions available for this video -> not_available.")
                    conn.execute("UPDATE videos SET transcript_status = 'not_available' WHERE id = ?", (vid,))

        except Exception as e:
            print(f"  Failed: {e}")
            conn.execute("UPDATE videos SET transcript_status = 'error' WHERE id = ?", (vid,))
        
        conn.commit()

    conn.close()

if __name__ == "__main__":
    process_queue()
