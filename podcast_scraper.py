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
import profiles

# All topic-specific criteria come from the active investigation profile.
# apply_profile() (called below + on --profile) populates these module globals.
DB_PATH = None
SEARCH_QUERIES = CURATED_CHANNELS = CHANNEL_BONUS = KEYWORDS = None
MIN_VIEWS = MIN_DURATION_SEC = MAX_DURATION_SEC = MIN_DAYS_OLD = MIN_PUBLISH_DATE = None
MAX_VIDEOS_PER_QUERY = MAX_VIDEOS_PER_CHANNEL = MAX_RESULTS_TO_RETURN = None
ANALYSIS_FOCUS = DIGEST_TITLE = ACTIVE_PROFILE = None


def apply_profile(name=None):
    """Load an investigation profile into the module globals used throughout."""
    global DB_PATH, SEARCH_QUERIES, CURATED_CHANNELS, CHANNEL_BONUS, KEYWORDS
    global MIN_VIEWS, MIN_DURATION_SEC, MAX_DURATION_SEC, MIN_DAYS_OLD, MIN_PUBLISH_DATE
    global MAX_VIDEOS_PER_QUERY, MAX_VIDEOS_PER_CHANNEL, MAX_RESULTS_TO_RETURN
    global ANALYSIS_FOCUS, DIGEST_TITLE, ACTIVE_PROFILE
    p = profiles.load(name)
    ACTIVE_PROFILE = p
    DB_PATH = Path(p["db_path"])
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    SEARCH_QUERIES = p["search_queries"]
    CURATED_CHANNELS = p["curated_channels"]
    CHANNEL_BONUS = p.get("channel_bonus", {})
    KEYWORDS = p["keywords"]
    MIN_VIEWS = p["min_views"]
    MIN_DURATION_SEC = p["min_duration_sec"]
    MAX_DURATION_SEC = p["max_duration_sec"]
    MIN_DAYS_OLD = p["min_days_old"]
    MIN_PUBLISH_DATE = p.get("min_publish_date", "2025-01-01")
    MAX_VIDEOS_PER_QUERY = p["max_videos_per_query"]
    MAX_VIDEOS_PER_CHANNEL = p["max_videos_per_channel"]
    MAX_RESULTS_TO_RETURN = 20
    ANALYSIS_FOCUS = p["analysis_focus"]
    DIGEST_TITLE = p["digest_title"]
    return p


apply_profile()

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

# All search criteria (SEARCH_QUERIES, CURATED_CHANNELS, CHANNEL_BONUS, KEYWORDS,
# filters) now come from the active investigation profile — see apply_profile().


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
            transcript_summary TEXT,
            channel_id TEXT,
            is_new_channel INTEGER DEFAULT 0,
            discovered_via TEXT DEFAULT 'search',
            views_per_day REAL DEFAULT 0,
            source_type TEXT DEFAULT 'youtube',
            source TEXT DEFAULT 'youtube',
            doi TEXT,
            citations INTEGER DEFAULT 0,
            venue TEXT
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
    conn.execute("""
        CREATE TABLE IF NOT EXISTS channels (
            channel_id TEXT PRIMARY KEY,
            channel_name TEXT,
            handle TEXT,
            channel_url TEXT,
            first_seen_date TEXT,
            last_seen_date TEXT,
            video_count INTEGER DEFAULT 0,
            best_score REAL DEFAULT 0,
            curated INTEGER DEFAULT 0,
            suggested INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS suggested_terms (
            term TEXT PRIMARY KEY,
            source TEXT,
            created_at TEXT,
            status TEXT DEFAULT 'pending'
        )
    """)
    conn.commit()
    return conn


# ── YouTube search via yt-dlp ──────────────────────────────────────────────
def search_youtube(query, max_results=20):
    # Same hardening as get_video_details: impersonation + 429 retry. A plain
    # call silently returns [] when rate-limited, so a whole query's worth of
    # discovery vanishes with no signal.
    cmd = [
        YT_DLP, "--flat-playlist", "--dump-json",
        "--extractor-args", "youtube:player_client=android",
        "--impersonate", "chrome", "--no-warnings",
        f"ytsearch{max_results}:{query}",
    ]
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        except subprocess.TimeoutExpired:
            continue
        videos = []
        for line in result.stdout.strip().split("\n"):
            line = line.strip()
            if not line:
                continue
            try:
                videos.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        if videos:
            return videos
        err = (result.stderr or "").lower()
        if "429" in err or "too many" in err:
            print(f"    429 on search '{query}', backoff {5*(attempt+1)}s")
            time.sleep(5 * (attempt + 1))
            continue
        return videos  # genuinely no results
    return []


# Channel tabs to scan. Long-form interviews/podcasts are often premiered, so
# they live on /streams (and sometimes /podcasts), NOT the main /videos grid —
# scanning only /videos silently misses an expert's best long-form content.
CHANNEL_TABS = ("/videos", "/streams", "/podcasts")


def _fetch_channel_tab(base, tab, max_results):
    cmd = [
        YT_DLP, "--flat-playlist", "--dump-json",
        "--playlist-end", str(max_results),
        "--extractor-args", "youtube:player_client=android",
        "--impersonate", "chrome", "--no-warnings",
        base + tab,
    ]
    out = []
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
            out.append(data)
    except (subprocess.TimeoutExpired, Exception):
        pass
    return out


def fetch_channel_videos(identifier, max_results=10):
    """Pull a channel's recent uploads from /videos, /streams and /podcasts.
    `identifier` is a handle (e.g. 'neilpatel') or a full channel URL.
    Returns video dicts tagged with `_curated=True` (downstream trusts them and
    bypasses view/age gates). Fault-tolerant: returns [] on failure."""
    base = identifier.rstrip("/") if identifier.startswith("http") else f"https://www.youtube.com/@{identifier}"
    for tab in CHANNEL_TABS:
        if base.endswith(tab):
            base = base[: -len(tab)]
            break
    seen, videos = set(), []
    for tab in CHANNEL_TABS:
        for v in _fetch_channel_tab(base, tab, max_results):
            vid = v.get("id")
            if vid and vid not in seen:
                seen.add(vid)
                videos.append(v)
    return videos


def get_video_details(video_id):
    """Fetch full metadata for a single video. Uses the android client + Chrome
    impersonation and retries on 429 — bulk runs enrich ~100+ videos rapidly and
    a plain call gets rate-limited, silently dropping videos (e.g. premiered
    long-form content)."""
    cmd = [
        YT_DLP, "--dump-json", "--no-download",
        "--extractor-args", "youtube:player_client=android",
        "--impersonate", "chrome", "--no-warnings",
        f"https://youtube.com/watch?v={video_id}",
    ]
    for attempt in range(3):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if result.stdout.strip():
                return json.loads(result.stdout.strip())
            err = (result.stderr or "").lower()
            if "429" in err or "too many" in err:
                time.sleep(5 * (attempt + 1))
                continue
            return None
        except subprocess.TimeoutExpired:
            continue
        except json.JSONDecodeError:
            return None
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
    keywords = KEYWORDS or []
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


def channel_authority(channel_id, channel_name, is_curated):
    """0–1 authority signal for the SOURCE. Known experts should rank high
    regardless of how keyword-dense their titles are. Monitored (curated)
    channels get a strong floor; channels in the bonus list get a tier-scaled
    score; everyone else gets 0."""
    ch_key = (channel_name or "Unknown").lower().replace(" ", "").replace("'", "")
    bonus = 1.0
    for key, b in CHANNEL_BONUS.items():
        if key.lower().replace(" ", "") in ch_key or (channel_id and channel_id.lower() in ch_key):
            bonus = b
            break
    if is_curated:
        return min(0.7 + (bonus - 1.0), 1.0)   # monitored authority: floor 0.7 + tier
    if bonus > 1.0:
        return min(bonus - 1.0, 1.0)            # known but not monitored
    return 0.0


def calculate_quality_score(views, likes, comments, duration, kw_score, channel_id,
                            channel_name, views_per_day=0, is_curated=False):
    """Composite quality score. Authority (known expertise) is a strong, additive
    term so genuine experts outrank keyword-stuffed titles; `views_per_day` adds a
    velocity signal so breakouts surface early. Title-only keyword density is
    down-weighted (it's gameable and transcripts aren't available at scrape time)."""
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

    # authority: known expertise of the source (strong, additive)
    authority = channel_authority(channel_id, channel_name, is_curated)

    # velocity: views/day since publish, saturating at ~3000/day
    velocity_score = min((views_per_day or 0) / 3000.0, 1.0)

    score = (
        authority * 0.30 +        # known expertise dominates — experts rank high
        views_score * 0.25 +
        velocity_score * 0.10 +
        kw_score * 0.15 +         # title-only at scrape time → down-weighted
        likes_score * 0.10 +
        duration_score * 0.05 +
        comments_score * 0.05
    )

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
def _norm(name):
    return (name or "").lower().replace(" ", "").replace("'", "")


def monitored_channels(conn=None):
    """Identifiers of channels we actively monitor: the profile's curated_channels
    (by display name) + any channel marked curated=1 in the DB (by channel_id).
    Authority should follow the *channel*, not how a given video was found."""
    names = {_norm(v) for v in CURATED_CHANNELS.values()}
    cur_ids = set()
    if conn is not None:
        try:
            cur_ids = {r[0] for r in conn.execute(
                "SELECT channel_id FROM channels WHERE curated=1 "
                "AND channel_id IS NOT NULL AND channel_id != ''")}
        except sqlite3.OperationalError:
            pass
    return names, cur_ids


def _is_monitored(channel_name, channel_id, discovered_via, names, cur_ids):
    return (discovered_via == "channel"
            or _norm(channel_name) in names
            or (channel_id and channel_id in cur_ids))


def add_video(conn, vid):
    """Fetch, score and upsert a single video via the real enrichment path.
    Useful to capture a specific video that bulk discovery missed (e.g. a
    premiered long-form upload). Never fabricates — uses real metadata."""
    d = get_video_details(vid)
    if not d:
        print(f"  Could not fetch details for {vid}.")
        return False
    up = parse_ymd_via_ytdlp(d.get("upload_date")) or ""
    dur = d.get("duration", 0) or 0
    views = d.get("view_count", 0) or 0
    likes = d.get("like_count", 0) or 0
    comments = d.get("comment_count", 0) or 0
    channel_name = d.get("channel") or "Unknown"
    channel_id = d.get("channel_id", "") or ""
    channel_url = d.get("channel_url", "") or ""
    title = d.get("title", "Untitled")

    names, cur_ids = monitored_channels(conn)
    is_cur = _is_monitored(channel_name, channel_id, None, names, cur_ids)
    kw = calculate_keyword_density("", title, channel_name)
    vpd = round(views / days_since(up), 1) if up else 0
    score = calculate_quality_score(views, likes, comments, dur, kw,
                                    channel_id, channel_name, vpd, is_curated=is_cur)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    cur = conn.cursor()
    exists = cur.execute("SELECT 1 FROM videos WHERE id=?", (vid,)).fetchone()
    if exists:
        cur.execute("UPDATE videos SET views=?, likes=?, comments=?, quality_score=?, "
                    "views_per_day=?, channel_id=?, discovered_via=? WHERE id=?",
                    (views, likes, comments, score, vpd, channel_id,
                     "channel" if is_cur else "search", vid))
    else:
        cur.execute("""INSERT INTO videos (id, channel_name, channel_url, video_title, url,
            publish_date, duration_seconds, views, likes, comments, first_seen_date,
            last_updated_date, prev_views, view_change, view_change_pct,
            transcript_keywords_score, quality_score, transcript_summary,
            channel_id, is_new_channel, discovered_via, views_per_day)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (vid, channel_name, channel_url, title, f"https://youtube.com/watch?v={vid}",
             up, dur, views, likes, comments, now, now, 0, 0, 0, kw, score, "",
             channel_id, 0, "channel" if is_cur else "search", vpd))
    conn.commit()
    print(f"  {'Updated' if exists else 'Added'} {vid}: {title[:50]} "
          f"(score {score}, curated={is_cur}, {views:,} views)")
    return True


def rescore_all(conn):
    """Recompute quality_score for every stored video using the current formula.
    Lets a scoring change take effect immediately without a full re-scrape."""
    conn.row_factory = sqlite3.Row
    names, cur_ids = monitored_channels(conn)
    rows = conn.execute(
        "SELECT id, views, likes, comments, duration_seconds, transcript_keywords_score, "
        "channel_id, channel_name, views_per_day, discovered_via FROM videos"
    ).fetchall()
    n = 0
    for r in rows:
        score = calculate_quality_score(
            r["views"] or 0, r["likes"] or 0, r["comments"] or 0,
            r["duration_seconds"] or 0, r["transcript_keywords_score"] or 0,
            r["channel_id"], r["channel_name"], r["views_per_day"] or 0,
            is_curated=_is_monitored(r["channel_name"], r["channel_id"],
                                     r["discovered_via"], names, cur_ids),
        )
        conn.execute("UPDATE videos SET quality_score=? WHERE id=?", (score, r["id"]))
        n += 1
    conn.commit()
    return n


def days_since(date_str):
    """Whole days from a YYYY-MM-DD date to now (>=1 to avoid div-by-zero)."""
    try:
        d = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        return max((datetime.now(timezone.utc) - d).days, 1)
    except (TypeError, ValueError):
        return 1


def get_db_curated_channels(conn):
    """Channels the user promoted in the dashboard (curated=1). Returns a list of
    (identifier, name) where identifier is a handle or channel URL."""
    out = []
    try:
        for r in conn.execute(
            "SELECT channel_name, handle, channel_url FROM channels WHERE curated=1"
        ).fetchall():
            ident = r[1] or r[2]  # handle preferred, else URL
            if ident:
                out.append((ident, r[0] or ident))
    except sqlite3.OperationalError:
        pass  # table not migrated yet
    return out


def sync_channels(conn):
    """Recompute the channels registry from the videos table and flag emerging
    channels worth promoting. A non-curated channel with >=2 videos and a best
    score >=0.6 becomes 'suggested'."""
    cur = conn.cursor()
    rows = cur.execute("""
        SELECT channel_id, MAX(channel_name), MAX(channel_url), COUNT(*),
               MAX(quality_score), MIN(first_seen_date), MAX(last_updated_date)
        FROM videos
        WHERE channel_id IS NOT NULL AND channel_id != ''
        GROUP BY channel_id
    """).fetchall()
    for cid, name, url, cnt, best, first, last in rows:
        cur.execute("""
            INSERT INTO channels (channel_id, channel_name, channel_url,
                                  first_seen_date, last_seen_date, video_count, best_score)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(channel_id) DO UPDATE SET
                channel_name = excluded.channel_name,
                channel_url  = excluded.channel_url,
                last_seen_date = excluded.last_seen_date,
                video_count  = excluded.video_count,
                best_score   = MAX(channels.best_score, excluded.best_score)
        """, (cid, name, url, first, last, cnt, best or 0))
    # Any channel we actively monitor (a video came in via channel-discovery) is
    # curated by definition — so it never shows up as a "suggestion".
    cur.execute("""
        UPDATE channels SET curated = 1 WHERE channel_id IN (
            SELECT DISTINCT channel_id FROM videos
            WHERE discovered_via = 'channel' AND channel_id IS NOT NULL AND channel_id != ''
        )
    """)
    # Suggest a non-curated channel once it has shown CONSISTENT quality:
    # at least 5 videos scoring over 0.5.
    cur.execute("""
        UPDATE channels SET suggested = 1
        WHERE curated = 0 AND suggested = 0 AND channel_id IN (
            SELECT channel_id FROM videos
            WHERE quality_score > 0.5 AND channel_id IS NOT NULL AND channel_id != ''
            GROUP BY channel_id HAVING COUNT(*) >= 5
        )
    """)
    conn.commit()


def suggest_search_terms(conn, limit=40):
    """Query-freshening: ask an LLM to mine recent high-scoring titles for new
    search terms/jargon, store them as 'pending' for the user to approve.
    Returns the list of newly proposed terms. Best-effort; needs an LLM key."""
    try:
        import analyze_transcripts as A
    except ImportError:
        return []
    key, base, model = A.llm_config()
    if not key:
        print("  No LLM key — skipping term suggestions.")
        return []

    titles = [r[0] for r in conn.execute(
        "SELECT video_title FROM videos ORDER BY quality_score DESC LIMIT ?", (limit,)
    ).fetchall()]
    if not titles:
        return []

    existing = set(q.lower() for q in SEARCH_QUERIES)
    prompt = (
        f"These are titles of the best-performing videos we've found about "
        f"{ANALYSIS_FOCUS}:\n\n"
        + "\n".join(f"- {t}" for t in titles)
        + "\n\nPropose 6-10 NEW YouTube search queries (3-6 words each) that would surface "
        f"more high-quality content on {ANALYSIS_FOCUS}. Favor emerging terminology. "
        "Avoid the word 'podcast'. "
        'Return ONLY JSON: {"terms": ["...", "..."]}'
    )
    try:
        result = A.call_llm(prompt, key, base, model)
        terms = result.get("terms", []) or []
    except Exception as e:
        print(f"  Term suggestion failed: {e}")
        return []

    added = []
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for t in terms:
        t = str(t).strip()
        if t and t.lower() not in existing:
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO suggested_terms (term, source, created_at, status) "
                    "VALUES (?, 'llm', ?, 'pending')", (t, now))
                added.append(t)
            except sqlite3.OperationalError:
                pass
    conn.commit()
    return added


def dry_run():
    """Test a profile's reach WITHOUT writing anything: run the searches +
    channel fetches (flat metadata only) and report what would be discovered.
    Returns a summary dict (also printed as JSON when called via --test --json)."""
    seen = {}
    channels = {}
    for q in SEARCH_QUERIES:
        for v in search_youtube(q, MAX_VIDEOS_PER_QUERY):
            vid = v.get("id")
            if vid and vid not in seen:
                seen[vid] = v.get("title", "")
                ch = v.get("channel") or v.get("uploader") or "?"
                channels[ch] = channels.get(ch, 0) + 1
        time.sleep(0.5)
    for handle, name in CURATED_CHANNELS.items():
        vids = fetch_channel_videos(handle, MAX_VIDEOS_PER_CHANNEL)
        for v in vids:
            vid = v.get("id")
            if vid and vid not in seen:
                seen[vid] = v.get("title", "")
                channels[name] = channels.get(name, 0) + 1
        time.sleep(0.5)
    top_channels = sorted(channels.items(), key=lambda x: -x[1])[:15]
    return {
        "profile": ACTIVE_PROFILE["name"],
        "label": ACTIVE_PROFILE["label"],
        "unique_videos": len(seen),
        "unique_channels": len(channels),
        "top_channels": [{"name": n, "count": c} for n, c in top_channels],
        "sample_titles": list(seen.values())[:15],
    }


def main():
    if not ACTIVE_PROFILE.get("youtube_enabled", True):
        print(f"YouTube arm disabled for profile '{ACTIVE_PROFILE['name']}' — skipping.")
        return
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
    # Base queries + any LLM-suggested terms the user accepted in the dashboard.
    accepted_terms = [r[0] for r in cursor.execute(
        "SELECT term FROM suggested_terms WHERE status = 'accepted'").fetchall()]
    queries = SEARCH_QUERIES + accepted_terms
    if accepted_terms:
        print(f"(+{len(accepted_terms)} accepted search terms)")
    for query in queries:
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
    # Defaults (code) + any channels the user promoted in the dashboard (DB).
    print()
    curated_targets = list(CURATED_CHANNELS.items()) + get_db_curated_channels(conn)
    for handle, name in curated_targets:
        print(f"Channel: {name} ({handle})")
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
        if upload_date < MIN_PUBLISH_DATE:
            continue  # profile's publish-date floor (default 2025-01-01)

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
            "views_per_day": round(views / days_since(upload_date), 1),
            "discovered_via": "channel" if curated else "search",
        })

        time.sleep(0.3)

    print(f"After filtering (curated bypass view/age; others views>={MIN_VIEWS}, "
          f"{MIN_DURATION_SEC//60}-{MAX_DURATION_SEC//60}min, {MIN_DAYS_OLD}+ days old): {len(enriched)}")

    # ── Step 3: Fetch transcripts & score (best-effort, skip if slow) ──
    mon_names, mon_ids = monitored_channels(conn)
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
            v["channel_id"], v["channel_name"], v["views_per_day"],
            is_curated=_is_monitored(v["channel_name"], v["channel_id"],
                                     v.get("discovered_via"), mon_names, mon_ids),
        )

    # ── Step 4: Sort by quality score ───────────────────────────────────
    enriched.sort(key=lambda x: x["quality_score"], reverse=True)
    top_n = enriched[:MAX_RESULTS_TO_RETURN]

    # ── Step 5: Update DB ────────────────────────────────────────────────
    # Channels seen before THIS run (for emerging detection). Empty on first run.
    known_channels = {r[0] for r in cursor.execute(
        "SELECT channel_id FROM channels WHERE channel_id IS NOT NULL AND channel_id != ''"
    ).fetchall()}

    new_count = 0
    for v in enriched:
        cursor.execute("SELECT id FROM videos WHERE id = ?", (v["id"],))
        existing = cursor.fetchone()
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        # Emerging = search-discovered channel we haven't catalogued before.
        is_new_channel = 1 if (
            v.get("discovered_via") != "channel"
            and v.get("channel_id")
            and v["channel_id"] not in known_channels
        ) else 0

        if existing:
            # Update with new view counts + velocity + channel metadata
            cursor.execute("""
                UPDATE videos SET
                    views = ?, likes = ?, comments = ?,
                    prev_views = views,
                    view_change = ? - views,
                    last_updated_date = ?,
                    quality_score = ?,
                    channel_id = ?, views_per_day = ?, discovered_via = ?
                WHERE id = ?
            """, (
                v["views"], v["likes"], v["comments"],
                v["views"], now, v["quality_score"],
                v["channel_id"], v["views_per_day"], v.get("discovered_via", "search"),
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
                    transcript_summary,
                    channel_id, is_new_channel, discovered_via, views_per_day
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                v["id"], v["channel_name"], v["channel_url"], v["title"], v["url"],
                v["publish_date"], v["duration"], v["views"], v["likes"], v["comments"],
                now, now,
                0, 0, 0.0,
                v["kw_score"], v["quality_score"],
                v.get("transcript_preview", ""),
                v["channel_id"], is_new_channel, v.get("discovered_via", "search"), v["views_per_day"],
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

    # ── Step 5b: Refresh channel registry + flag emerging channels ──────
    sync_channels(conn)
    emerging = conn.execute(
        "SELECT COUNT(*) FROM videos WHERE is_new_channel = 1").fetchone()[0]
    suggestions = conn.execute(
        "SELECT channel_name, best_score, video_count FROM channels "
        "WHERE suggested = 1 AND curated = 0 ORDER BY best_score DESC").fetchall()
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

    print(f"\nEmerging videos (new channels) this run: {emerging}")
    if suggestions:
        print("Suggested channels to monitor (review in dashboard → Discovery):")
        for name, best, cnt in suggestions:
            print(f"  • {name} — best score {best:.2f}, {cnt} videos")

    print(f"--- Run complete ---")


if __name__ == "__main__":
    # --profile NAME selects an investigation profile for this run.
    prof_name = None
    for a in sys.argv[1:]:
        if a.startswith("--profile="):
            prof_name = a.split("=", 1)[1]
    if "--profile" in sys.argv:
        i = sys.argv.index("--profile")
        if i + 1 < len(sys.argv):
            prof_name = sys.argv[i + 1]
    if prof_name:
        apply_profile(prof_name)

    add_id = None
    for a in sys.argv[1:]:
        if a.startswith("--add="):
            add_id = a.split("=", 1)[1]
    if add_id:
        c = init_db()
        add_video(c, add_id)
        c.close()
    elif "--rescore" in sys.argv:
        c = init_db()
        n = rescore_all(c)
        c.close()
        print(f"Re-scored {n} videos with the current formula.")
    elif "--test" in sys.argv:
        summary = dry_run()
        if "--json" in sys.argv:
            print(json.dumps(summary))
        else:
            print(f"[{summary['label']}] would discover {summary['unique_videos']} videos "
                  f"from {summary['unique_channels']} channels")
            for c in summary["top_channels"]:
                print(f"  {c['count']:>3}  {c['name']}")
    elif "--suggest-terms" in sys.argv:
        c = init_db()
        added = suggest_search_terms(c)
        c.close()
        print(f"Suggested {len(added)} new search term(s): {added}")
    else:
        main()
