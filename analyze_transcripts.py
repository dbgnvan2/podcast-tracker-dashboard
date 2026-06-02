#!/usr/bin/env python3
"""AI Intelligence stage — extract structured insight from VERIFIED transcripts.

Reads each 'obtained' video's saved transcript (and timestamped segments) and
asks an LLM to extract key points (with timestamps), SEO entities, GEO signals,
and the single best quote. Results go to the key_points and ai_analysis tables.

HARD RULE (CLAUDE.md Working Rule 0): the only input is the on-disk transcript.
Never analyze a video from its title/metadata. If no transcript file exists,
the video is skipped — never invent content.

LLM: OpenAI-compatible chat completions over stdlib urllib. Key + model read
from the environment or ~/.hermes/.env:
  PODCAST_LLM_KEY      (falls back to OPENAI_API_KEY)
  PODCAST_LLM_BASE     (default https://api.openai.com/v1)
  PODCAST_LLM_MODEL    (default gpt-4o-mini)
"""
import os
import re
import sys
import json
import sqlite3
import urllib.request
import urllib.error
from pathlib import Path

DB_PATH = Path.home() / ".hermes" / "podcast_tracker.db"
TRANSCRIPTS_DIR = Path.home() / ".hermes" / "transcripts"
ENV_FILE = Path.home() / ".hermes" / ".env"

MAX_TRANSCRIPT_CHARS = 24000  # keep prompt within a sane token budget


def load_env():
    """Load ~/.hermes/.env into os.environ for any keys not already set."""
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def llm_config():
    load_env()
    key = os.environ.get("PODCAST_LLM_KEY") or os.environ.get("OPENAI_API_KEY")
    base = os.environ.get("PODCAST_LLM_BASE", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("PODCAST_LLM_MODEL", "gpt-4o-mini")
    return key, base, model


def downsample_segments(segments, window=25):
    """Group segments into ~`window`-second buckets to shrink the prompt while
    keeping timestamps. Returns a string of '[<sec>] text' lines."""
    if not segments:
        return ""
    lines = []
    bucket_start = segments[0]["start"]
    bucket_text = []
    for seg in segments:
        if seg["start"] - bucket_start >= window and bucket_text:
            lines.append(f"[{bucket_start}] " + " ".join(bucket_text))
            bucket_start = seg["start"]
            bucket_text = []
        bucket_text.append(seg["text"])
    if bucket_text:
        lines.append(f"[{bucket_start}] " + " ".join(bucket_text))
    return "\n".join(lines)


PROMPT = """You are an analyst building a weekly "best of" digest of SEO / AI / GEO
(generative engine optimization) podcasts. You are given a VERIFIED transcript of
one video, with [seconds] timestamp markers.

Extract, using ONLY what the transcript actually says (never invent):
- key_points: 4-8 of the most important, specific, actionable insights. Each has:
    - timestamp_sec: integer seconds, taken from the nearest [seconds] marker where the point is made
    - point_text: one clear sentence
    - category: one of insight | strategy | tactic | tool | stat | prediction
- seo_entities: distinct brands, tools, people, and products mentioned (array of strings)
- geo_signals: concrete GEO / AI-search signals discussed — e.g. AI Overviews, entity SEO,
    knowledge graph, zero-click, citations, llms.txt (array of strings)
- best_quote: the single most quotable, high-signal sentence, verbatim from the transcript

Return ONLY a JSON object with keys: key_points, seo_entities, geo_signals, best_quote.

Video title: {title}
Channel: {channel}

Transcript:
{transcript}
"""


def call_llm(prompt, key, base, model):
    body = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "response_format": {"type": "json_object"},
    }).encode()
    req = urllib.request.Request(
        f"{base}/chat/completions", data=body,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    content = data["choices"][0]["message"]["content"]
    return json.loads(content)


def load_transcript(vid, full_text):
    seg_path = TRANSCRIPTS_DIR / f"{vid}.segments.json"
    if seg_path.is_file():
        segments = json.loads(seg_path.read_text())
        text = downsample_segments(segments)
        if text:
            return text[:MAX_TRANSCRIPT_CHARS]
    # Fallback: plain text without timestamps (key points get timestamp_sec 0)
    return (full_text or "")[:MAX_TRANSCRIPT_CHARS]


def analyze_video(conn, row, key, base, model):
    vid = row["id"]
    transcript = load_transcript(vid, row["full_text"])
    if not transcript or len(transcript) < 100:
        print(f"  Skip {vid}: no usable transcript on disk.")
        return False
    prompt = PROMPT.format(
        title=row["video_title"] or "", channel=row["channel_name"] or "",
        transcript=transcript,
    )
    try:
        result = call_llm(prompt, key, base, model)
    except urllib.error.HTTPError as e:
        print(f"  LLM HTTP {e.code} for {vid}: {e.read()[:200]}")
        return False
    except Exception as e:
        print(f"  LLM error for {vid}: {type(e).__name__}: {str(e)[:160]}")
        return False

    key_points = result.get("key_points", []) or []
    seo_entities = result.get("seo_entities", []) or []
    geo_signals = result.get("geo_signals", []) or []
    best_quote = result.get("best_quote", "") or ""

    conn.execute("DELETE FROM key_points WHERE video_id=?", (vid,))
    for kp in key_points:
        try:
            ts = int(kp.get("timestamp_sec") or 0)
        except (TypeError, ValueError):
            ts = 0
        conn.execute(
            "INSERT INTO key_points (video_id, timestamp_sec, point_text, category) VALUES (?,?,?,?)",
            (vid, ts, str(kp.get("point_text", "")).strip(), str(kp.get("category", "")).strip()),
        )
    conn.execute(
        "INSERT OR REPLACE INTO ai_analysis (video_id, seo_entities, geo_signals, best_quote, analyzed_at) "
        "VALUES (?,?,?,?,datetime('now'))",
        (vid, json.dumps(seo_entities), json.dumps(geo_signals), best_quote),
    )
    conn.commit()
    print(f"  Analyzed {vid}: {len(key_points)} key points, {len(seo_entities)} entities.")
    return True


def analyze_all(only_id=None, force=False):
    key, base, model = llm_config()
    if not key:
        print("ERROR: no LLM API key. Set OPENAI_API_KEY or PODCAST_LLM_KEY (or ~/.hermes/.env).")
        return 0

    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    sql = ("SELECT v.id, v.video_title, v.channel_name, t.full_text "
           "FROM videos v JOIN transcripts t ON t.video_id = v.id "
           "WHERE v.transcript_status='obtained'")
    params = []
    if only_id:
        sql += " AND v.id=?"
        params.append(only_id)
    elif not force:
        sql += " AND v.id NOT IN (SELECT video_id FROM ai_analysis)"
    rows = conn.execute(sql, params).fetchall()

    if not rows:
        print("Nothing to analyze (no obtained transcripts pending).")
        conn.close()
        return 0

    print(f"Analyzing {len(rows)} transcript(s) with {model}...")
    done = 0
    for row in rows:
        print(f"Analyzing: {row['video_title']} ({row['id']})")
        if analyze_video(conn, row, key, base, model):
            done += 1
    conn.close()
    print(f"Done. Analyzed {done}/{len(rows)}.")
    return done


if __name__ == "__main__":
    only = None
    force = "--force" in sys.argv
    for a in sys.argv[1:]:
        if a.startswith("--id="):
            only = a.split("=", 1)[1]
    analyze_all(only_id=only, force=force)
