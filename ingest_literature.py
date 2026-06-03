#!/usr/bin/env python3
"""Literature ingestion — the scholarly arm of discovery.

Runs the active profile's `literature` arm through the ScholarlyAdapter and
upserts each paper into the shared `documents` table (physically `videos`) as a
first-class document: the abstract is stored as the content (transcripts row) and
the row goes straight to content_status='obtained' — papers need no transcription.
The existing analyze → digest → report layers then treat papers and videos
identically.

  python3 ingest_literature.py                 # active profile
  python3 ingest_literature.py --profile NAME
"""
import sys
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import profiles
from sources.scholarly import ScholarlyAdapter


def _norm(s):
    return (s or "").lower()


def relevance(text, keywords):
    if not keywords:
        return 0.0
    t = _norm(text)
    hits = sum(1 for k in keywords if k and _norm(k) in t)
    return min(hits / max(len(keywords) * 0.4, 1), 1.0)


def recency(date_str):
    try:
        d = datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        days = (datetime.now(timezone.utc) - d).days
    except (TypeError, ValueError):
        return 0.3
    if days <= 90:
        return 1.0
    if days >= 730:
        return 0.3
    return round(1.0 - 0.7 * (days - 90) / (730 - 90), 3)  # linear decay 1.0→0.3


def score_paper(doc, keywords):
    """Literature quality score (0-1): authority (citations) + relevance + recency.
    Preprints start with 0 citations, so recency + relevance carry the weight —
    mirror of the 'don't bury low-view authorities' lesson (LEARNINGS P4/P7)."""
    auth = doc.get("authority", 0.0)  # log-scaled citations from the adapter
    rel = relevance(f"{doc['title']} {doc['text']}", keywords)
    rec = recency(doc["published_date"])
    return round(auth * 0.40 + rel * 0.30 + rec * 0.30, 3)


def ingest(profile_name=None):
    p = profiles.load(profile_name)
    arm = p.get("literature")
    if not arm or not arm.get("enabled", False):
        print(f"Profile '{p['name']}' has no enabled literature arm — nothing to ingest.")
        return 0
    db = p["db_path"]
    keywords = p.get("keywords", [])

    print(f"Discovering literature for '{p['label']}' via {arm.get('sources', ['europepmc'])}...")
    docs = ScholarlyAdapter().discover(arm)
    print(f"  {len(docs)} unique papers (deduped by DOI).")
    if not docs:
        return 0

    conn = sqlite3.connect(db)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    n_new = 0
    for d in docs:
        vid = d["id"]
        score = score_paper(d, keywords)
        cites = d["raw"].get("citations", 0) or 0
        exists = conn.execute("SELECT 1 FROM videos WHERE id=?", (vid,)).fetchone()
        if exists:
            conn.execute(
                "UPDATE videos SET views=?, citations=?, quality_score=?, last_updated_date=? WHERE id=?",
                (cites, cites, score, now, vid))
        else:
            n_new += 1
            conn.execute("""INSERT INTO videos (
                id, channel_name, channel_url, video_title, url, publish_date,
                duration_seconds, views, likes, comments, first_seen_date, last_updated_date,
                prev_views, view_change, view_change_pct, transcript_keywords_score,
                quality_score, transcript_summary, channel_id, is_new_channel,
                discovered_via, views_per_day, source_type, source, doi, citations, venue,
                transcript_status, transcribed_date)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (vid, d["byline"], "", d["title"], d["url"], d["published_date"],
                 0, cites, 0, 0, now, now, 0, 0, 0, relevance(d["title"] + " " + d["text"], keywords),
                 score, "", "", 0, "literature", 0,
                 "literature", d["source"], d["raw"].get("doi", ""), cites, d["raw"].get("venue", ""),
                 "obtained", today))
        # store the abstract as the analyzable content
        conn.execute(
            "INSERT OR REPLACE INTO transcripts (video_id, file_path, full_text, word_count) VALUES (?,?,?,?)",
            (vid, "", d["text"], len((d["text"] or "").split())))
    conn.commit()
    conn.close()
    print(f"Ingested {len(docs)} papers ({n_new} new). They are 'obtained' — run analyze next.")
    return len(docs)


if __name__ == "__main__":
    prof = None
    for a in sys.argv[1:]:
        if a.startswith("--profile="):
            prof = a.split("=", 1)[1]
        elif a == "--profile" and sys.argv.index(a) + 1 < len(sys.argv):
            prof = sys.argv[sys.argv.index(a) + 1]
    ingest(prof)
