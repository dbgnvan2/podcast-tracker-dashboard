#!/usr/bin/env python3
"""
Podcast Quality Scraper — SEO / AI / GEO
Searches YouTube, scores by quality, tracks in SQLite, reports new finds.
"""

import json
import sqlite3
import subprocess
import sys
import re
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────────
DB_PATH = Path.home() / ".hermes" / "podcast_tracker.db"
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

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
    result = subprocess.run(["which", "yt-dlp"], capture_output=True, text=True)
    if result.stdout.strip():
        YT_DLP = result.stdout.strip()
    else:
        print("ERROR: yt-dlp not found")
        sys.exit(1)

# Content-focused queries reflecting the real goal: getting people to a site /
# video / podcast via SEO + AI GEO. NOTE: do NOT hard-code "podcast" — it filters
# out the best educational creators (e.g. Neil Patel) and pulls in off-topic
# literal podcasts. Channel monitoring (below) is the reliable path for authorities.
SEARCH_QUERIES = [
    "how to rank in AI Overviews 2026",
    "get recommended by ChatGPT SEO",
    "generative engine optimization GEO 2026",
    "answer engine optimization AEO",
    "Google AI Mode SEO strategy 2026",
    "brand entity SEO knowledge graph",
    "how to get cited by AI search",
    "SEO to drive website traffic 2026",
    "AI search visibility for brands",
]

# Authority channels to monitor directly (handle -> display name). Their recent
# uploads are pulled regardless of keyword match, and bypass the view/age gates.
# This is what guarantees creators like Neil Patel are captured.
CURATED_CHANNELS = {
    "neilpatel": "Neil Patel",
    "AhrefsCom": "Ahrefs",
    "surferseo": "Surfer SEO",
    "HubSpotMarketing": "HubSpot Marketing",
    "GoogleSearchCentral": "Google Search Central",
    "Semrush": "Semrush",
    "searchenginejournal": "Search Engine Journal",
    "searchengineland": "Search Engine Land",
    "LevelingUpOfficial": "Leveling Up with Eric Siu",
}

MAX_VIDEOS_PER_QUERY = 20
MAX_VIDEOS_PER_CHANNEL = 10
MIN_VIEWS = 2000
MIN_DURATION_SEC = 300   # 5 minutes (concise SEO/GEO tips count too)
MAX_DURATION_SEC = 5400  # 90 minutes
MIN_DAYS_OLD = 7  # let views accumulate (skipped for curated channels)
MAX_RESULTS_TO_RETURN = 20

# ── Known high-quality channels (bonus) ────────────────────────────────────
CHANNEL_BONUS = {
    "neilpatel": 1.5,
    "SearchOffTheRecord": 1.5,
    "GoogleSearchCentral": 1.5,
    "SEOFOMO": 1.2,
    "Niche Pursuits": 1.2,
    "Experts on the Wire": 1.1,
    "Semantic Mastery": 1.1,
    "Marketing Oops": 1.0,
    "RustyBrick GEO": 1.2,
    "Cyrus Shepard": 1.3,
    "Aleyda Solis": 1.3,
    "Lily Ray": 1.3,
    "Mordy Oberstein": 1.1,
    "Kevin Indig": 1.1,
}


# ── DB setup ────────────────────────────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY,
            channel_name TEXT,
            channel_url TEXT,
            video_title TEXT,
            url TEXT,
            publish_date TEXT,
            duration_seconds INTEGER,
            views INTEGER,
            likes INTEGER,
            comments INTEGER,
            first_seen_date TEXT,
            last_updated_date TEXT,
            prev_views INTEGER,
            view_change INTEGER,
            view_change_pct REAL,
            transcript_keywords_score REAL,
            quality_score REAL,
            selected INTEGER DEFAULT 0,
            transcript_summary TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_date TEXT,
            videos_found INTEGER,
            videos_new INTEGER,
            errors TEXT
        )
    """)
    conn.commit()
    return conn


# ── YouTube search via yt-dlp ──────────────────────────────────────────────
def search_youtube(query, max_results=20):
    cmd = [
        YT_DLP,
        "--flat-playlist",
        "--dump-json",
        f"ytsearch{max_results}:{query}",
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    videos = []
    for line in result.stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            data = json.loads(line)
        except json.JSONDecodeError:
            continue
        videos.append(data)

    return videos


def fetch_channel_videos(handle, max_results=10):
    """Pull a channel's most recent uploads directly (flat playlist).
    Returns video dicts tagged with `_curated=True` so downstream logic can
    trust them (bypass view/age gates). Fault-tolerant: returns [] on failure."""
    cmd = [
        YT_DLP, "--flat-playlist", "--dump-json",
        "--playlist-end", str(max_results),
        "--extractor-args", "youtube:player_client=android",
        "--impersonate", "chrome", "--no-warnings",
        f"https://www.youtube.com/@{handle}/videos",
    ]
    videos = []
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                continue
            data["_curated"] = True
            videos.append(data)
    except (subprocess.TimeoutExpired, Exception):
        pass
    return videos


def get_video_details(video_id):
    """Fetch full metadata for a single video."""
    cmd = [
        YT_DLP,
        "--dump-json",
        "--no-download",
        f"https://youtube.com/watch?v={video_id}",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.stdout.strip():
            return json.loads(result.stdout.strip())
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        pass
    return None


def fetch_transcript(video_id):
    """Fetch transcript using youtube-transcript-api (best-effort)."""
    cmd = [
        sys.executable, "-c", f"""
import sys
try:
    from youtube_transcript_api import YouTubeTranscriptApi
    api = YouTubeTranscriptApi()
    transcript = api.fetch("{video_id}")
    import json
    transcript_list = [{{"text": seg.text, "start": seg.start, "duration": seg.duration}} for seg in transcript]
    print(json.dumps(transcript_list))
except Exception:
    sys.exit(1)
"""
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (subprocess.TimeoutExpired, Exception):
        pass
    return None


def calculate_keyword_density(transcript_text, video_title, channel_name=""):
    """Score a video for AI/GEO/SEO relevance based on transcript + title + channel."""
    keywords = [
        "generative engine optimization", "GEO", "AI overviews",
        "AI mode", "search generative experience", "SGE",
        "brand entity", "entity SEO", "knowledge graph",
        "AI search", "AI SEO", "ChatGPT search", "Perplexity", "Google AI",
        "large language model", "LLM", "RAG",
        "zero-click", "zero click search",
        "answer engine", "answer engine optimization", "AEO",
        "featured snippet", "get cited", "cited by AI", "AI citations",
        "E-E-A-T", "EEAT", "topical authority",
        "AI-generated content", "AIO", "AI optimization", "AI visibility",
        "organic traffic", "website traffic", "drive traffic",
        "rank on Google", "search rankings", "llms.txt",
    ]
    search_text = (transcript_text + " " + video_title + " " + channel_name).lower()
    words = len(search_text.split())
    if words == 0:
        return 0.0

    score = 0.0
    for kw in keywords:
        count = search_text.count(kw.lower())
        score += count * (1.5 if " " in kw else 1.0)

    # Normalize to 0-1 range
    normalized = min(score / (words * 0.03), 1.0)
    return round(normalized, 3)


def calculate_quality_score(views, likes, comments, duration, kw_score, channel_id, channel_name):
    """Composite quality score."""
    if views <= 0:
        return 0.0

    # log10 views — diminishing returns
    views_score = min((views ** 0.15) / 3.5, 1.0)

    # like ratio — capped at 10%
    like_ratio = min(likes / views if views > 0 else 0, 0.10)
    likes_score = (like_ratio * 100) / 10.0

    # comment ratio
    comment_ratio = min(comments / views if views > 0 else 0, 0.05)
    comments_score = (comment_ratio * 100) / 5.0

    # duration sweet spot: 15-60 min
    dur = duration / 60
    if 20 <= dur <= 50:
        duration_score = 1.0
    elif 15 <= dur <= 60:
        duration_score = 0.8
    elif 10 <= dur <= 75:
        duration_score = 0.5
    else:
        duration_score = 0.2

    # channel bonus
    channel_lookup = (channel_name or "Unknown").lower().replace(" ", "").replace("'", "")
    ch_bonus = 1.0
    for key, bonus in CHANNEL_BONUS.items():
        if key.lower() in channel_lookup or (channel_id and channel_id.lower() in channel_lookup):
            ch_bonus = bonus
            break

    score = (
        views_score * 0.35 +
        likes_score * 0.20 +
        comments_score * 0.10 +
        duration_score * 0.10 +
        kw_score * 0.25
    ) * ch_bonus

    return round(score, 3)


def parse_ymd_via_ytdlp(date_str):
    """Parse a date string from yt-dlp into YYYY-MM-DD."""
    if not date_str:
        return None
    # yt-dlp sometimes returns ISO 8601, sometimes YYYYMMDD
    if len(date_str) == 8 and date_str.isdigit():
        return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]}"
    try:
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d")
    except (ValueError, AttributeError):
        return date_str[:10] if date_str else None


# ── Main ────────────────────────────────────────────────────────────────────
def main():
    conn = init_db()
    cursor = conn.cursor()
    run_date = datetime.now(timezone.utc).isoformat()
    errors = []
    all_videos = {}

    print(f"=== Podcast Quality Scraper ===")
    print(f"Database: {DB_PATH}")
    print(f"Date: {run_date}")
    print()

    # ── Step 1: Search ──────────────────────────────────────────────────
    for query in SEARCH_QUERIES:
        print(f"Searching: {query}")
        try:
            results = search_youtube(query, MAX_VIDEOS_PER_QUERY)
            for v in results:
                vid = v.get("id")
                if vid and vid not in all_videos:
                    all_videos[vid] = v
            print(f"  Found {len(results)} videos (total unique: {len(all_videos)})")
        except Exception as e:
            msg = f"Search failed for '{query}': {e}"
            print(f"  ERROR: {msg}")
            errors.append(msg)
        time.sleep(1)  # be polite

    # ── Step 1b: Monitor curated authority channels directly ────────────
    print()
    for handle, name in CURATED_CHANNELS.items():
        print(f"Channel: {name} (@{handle})")
        try:
            results = fetch_channel_videos(handle, MAX_VIDEOS_PER_CHANNEL)
            added = 0
            for v in results:
                vid = v.get("id")
                if vid and vid not in all_videos:
                    all_videos[vid] = v
                    added += 1
                elif vid:
                    all_videos[vid]["_curated"] = True  # mark even if also found via search
            print(f"  Found {len(results)} uploads (+{added} new, total unique: {len(all_videos)})")
        except Exception as e:
            msg = f"Channel fetch failed for @{handle}: {e}"
            print(f"  ERROR: {msg}")
            errors.append(msg)
        time.sleep(1)

    print(f"\nTotal unique videos found: {len(all_videos)}")

    # ── Step 2: Get full details for each ───────────────────────────────
    enriched = []
    cutoff_date = (datetime.now(timezone.utc) - timedelta(days=MIN_DAYS_OLD)).strftime("%Y-%m-%d")

    for vid, v in all_videos.items():
        # Fetch full details first (flat search doesn't have dates/views)
        details = get_video_details(vid)
        if not details:
            continue

        upload_date = parse_ymd_via_ytdlp(details.get("upload_date"))
        if not upload_date:
            continue
        if upload_date > datetime.now(timezone.utc).strftime("%Y-%m-%d"):
            continue  # future dates
        if upload_date < "2026-01-01":
            continue  # only 2026+

        duration = details.get("duration", 0)
        views = details.get("view_count", 0) or 0
        likes = details.get("like_count", 0) or 0
        comments = details.get("comment_count", 0) or 0
        channel_name = (details.get("channel") or v.get("channel") or "Unknown")
        channel_id = details.get("channel_id", "")
        title = details.get("title", v.get("title", "Untitled"))
        channel_url = details.get("channel_url", "")
        curated = bool(v.get("_curated"))

        # Curated authority channels are trusted: they bypass the view-count and
        # recency gates (we want their content even if fresh / still accruing views).
        if not curated and views < MIN_VIEWS:
            continue
        if duration < MIN_DURATION_SEC or duration > MAX_DURATION_SEC:
            continue
        if not curated and upload_date > cutoff_date:
            continue

        enriched.append({
            "id": vid,
            "title": title,
            "url": f"https://youtube.com/watch?v={vid}",
            "channel_name": channel_name,
            "channel_url": channel_url,
            "channel_id": channel_id,
            "publish_date": upload_date,
            "duration": duration,
            "views": views,
            "likes": likes,
            "comments": comments,
        })

        time.sleep(0.3)

    print(f"After filtering (curated bypass view/age; others views>={MIN_VIEWS}, "
          f"{MIN_DURATION_SEC//60}-{MAX_DURATION_SEC//60}min, {MIN_DAYS_OLD}+ days old): {len(enriched)}")

    # ── Step 3: Fetch transcripts & score (best-effort, skip if slow) ──
    for v in enriched:
        print(f"  Score: {v['title'][:60]}...", end=" ")
        transcript_text = fetch_transcript(v["id"])
        if transcript_text:
            kw_score = calculate_keyword_density(transcript_text, v["title"], v["channel_name"])
            v["kw_score"] = kw_score
            v["transcript_preview"] = transcript_text[:200]
            print(f"kw_score={kw_score}")
        else:
            v["kw_score"] = calculate_keyword_density("", v["title"], v["channel_name"])
            v["transcript_preview"] = ""
            print(f"title_score={v['kw_score']}")

        v["quality_score"] = calculate_quality_score(
            v["views"], v["likes"], v["comments"],
            v["duration"], v["kw_score"],
            v["channel_id"], v["channel_name"]
        )

    # ── Step 4: Sort by quality score ───────────────────────────────────
    enriched.sort(key=lambda x: x["quality_score"], reverse=True)
    top_n = enriched[:MAX_RESULTS_TO_RETURN]

    # ── Step 5: Update DB ────────────────────────────────────────────────
    new_count = 0
    for v in enriched:
        cursor.execute("SELECT id FROM videos WHERE id = ?", (v["id"],))
        existing = cursor.fetchone()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        if existing:
            # Update with new view counts
            cursor.execute("""
                UPDATE videos SET
                    views = ?, likes = ?, comments = ?,
                    prev_views = views,
                    view_change = ? - views,
                    last_updated_date = ?,
                    quality_score = ?
                WHERE id = ?
            """, (
                v["views"], v["likes"], v["comments"],
                v["views"], now, v["quality_score"],
                v["id"],
            ))
        else:
            new_count += 1
            cursor.execute("""
                INSERT INTO videos (
                    id, channel_name, channel_url, video_title, url,
                    publish_date, duration_seconds, views, likes, comments,
                    first_seen_date, last_updated_date,
                    prev_views, view_change, view_change_pct,
                    transcript_keywords_score, quality_score,
                    transcript_summary
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                v["id"], v["channel_name"], v["channel_url"], v["title"], v["url"],
                v["publish_date"], v["duration"], v["views"], v["likes"], v["comments"],
                now, now,
                0, 0, 0.0,
                v["kw_score"], v["quality_score"],
                v.get("transcript_preview", ""),
            ))

    # Calculate view change % for existing videos
    cursor.execute("""
        UPDATE videos SET
            view_change_pct = CASE
                WHEN prev_views > 0 THEN ROUND((CAST(views AS REAL) - prev_views) / prev_views * 100, 2)
                ELSE 0.0
            END
        WHERE last_updated_date = ?
    """, (datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),))

    # Log run
    cursor.execute(
        "INSERT INTO runs (run_date, videos_found, videos_new, errors) VALUES (?, ?, ?, ?)",
        (run_date, len(enriched), new_count, "; ".join(errors[:5]) if errors else "")
    )
    conn.commit()
    conn.close()

    # ── Step 6: Print report ────────────────────────────────────────────
    print(f"\n{'='*70}")
    print(f"TOP {len(top_n)} PODCASTS — SEO / AI / GEO")
    print(f"{'='*70}")
    print(f"New videos this run: {new_count}")
    print(f"Total tracked in DB: check ~/.hermes/podcast_tracker.db")
    print()

    for i, v in enumerate(top_n, 1):
        print(f"{i:2d}. {v['title']}")
        print(f"    Channel: {v['channel_name']}  |  {v['publish_date']}")
        print(f"    Views: {v['views']:,}  |  Likes: {v['likes']:,}  |  Duration: {v['duration']//60}m")
        print(f"    Quality Score: {v['quality_score']:.2f}  |  Keywords: {v['kw_score']:.2f}")
        print(f"    {v['url']}")
        print()

    print(f"--- Run complete ---")


if __name__ == "__main__":
    main()
