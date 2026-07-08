#!/usr/bin/env python3
"""Weekly "Best of GEO/AI Search" digest generator.

Builds a markdown digest from VERIFIED, analyzed videos: top picks ranked by
quality score, each with its best quote and key points, plus aggregated trending
entities and GEO signals. Writes to ~/.hermes/digests/ and prints the path.

Only videos that are 'obtained' AND have an ai_analysis row are included — so the
digest never contains unverified or unanalyzed content.
"""
import os
import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime
from collections import Counter

import profiles
_PROFILE = profiles.load()  # active investigation profile
DB_PATH = Path(_PROFILE["db_path"])
DIGEST_DIR = Path(_PROFILE["digest_dir"])
DIGEST_DIR.mkdir(parents=True, exist_ok=True)
DIGEST_TITLE = _PROFILE.get("digest_title", "Weekly Digest")


def fmt_ts(sec):
    sec = int(sec or 0)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_digest(force=False, limit=10):
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row

    where = "v.transcript_status='obtained' AND a.video_id IS NOT NULL"
    if not force:
        where += " AND v.digested_at IS NULL"

    rows = conn.execute(
        f"""SELECT v.id, v.video_title, v.channel_name, v.views, v.quality_score,
                   v.url, v.source_type, a.seo_entities, a.geo_signals, a.best_quote
            FROM videos v JOIN ai_analysis a ON a.video_id = v.id
            WHERE {where}
            ORDER BY v.quality_score DESC LIMIT ?""",
        [limit],
    ).fetchall()

    today = datetime.now().strftime("%Y-%m-%d")
    if not rows:
        md = (f"# {DIGEST_TITLE} — {today}\n\n"
              "_No new analyzed videos since last digest. Use --force to re-digest all._\n")
        conn.close()
        return md, today, []

    ids = [r["id"] for r in rows]

    ent_counter, geo_counter = Counter(), Counter()
    lines = [f"# {DIGEST_TITLE} — {today}", ""]
    lines.append(f"**{len(rows)} verified, analyzed picks**, ranked by quality score.\n")

    for i, r in enumerate(rows, 1):
        entities = json.loads(r["seo_entities"] or "[]")
        geos = json.loads(r["geo_signals"] or "[]")
        ent_counter.update(e for e in entities if e)
        geo_counter.update(g for g in geos if g)

        kps = conn.execute(
            "SELECT timestamp_sec, point_text, category FROM key_points "
            "WHERE video_id=? ORDER BY timestamp_sec LIMIT 3",
            (r["id"],),
        ).fetchall()

        is_yt = (r["source_type"] or "youtube") == "youtube"
        doc_url = r["url"] or (f"https://youtube.com/watch?v={r['id']}" if is_yt else "")
        reach = f"{int(r['views'] or 0):,} " + ("views" if is_yt else "citations")
        lines.append(f"## {i}. {r['video_title']}")
        lines.append(f"**{r['channel_name']}** · {reach} · score {r['quality_score']:.2f}")
        lines.append(f"{doc_url}\n")
        if r["best_quote"]:
            lines.append(f"> {r['best_quote']}\n")
        if kps:
            lines.append("**Key points:**")
            for kp in kps:
                if is_yt:
                    url = f"https://youtube.com/watch?v={r['id']}&t={int(kp['timestamp_sec'] or 0)}s"
                    label = f"[{fmt_ts(kp['timestamp_sec'])}]({url}) "
                else:
                    label = ""  # papers have no timestamps
                cat = f" _({kp['category']})_" if kp["category"] else ""
                lines.append(f"- {label}{kp['point_text']}{cat}")
            lines.append("")

    if ent_counter:
        top = ", ".join(f"{e} ({c})" for e, c in ent_counter.most_common(10))
        lines += ["---", "", "### Trending entities", top, ""]
    if geo_counter:
        top = ", ".join(f"{g} ({c})" for g, c in geo_counter.most_common(10))
        lines += ["### Trending GEO signals", top, ""]

    # NOTE: we deliberately do NOT mark videos digested here. Marking before the
    # digest file is durably written would lose those videos forever if the write
    # fails (digested_at != NULL excludes them from every future run, recoverable
    # only via --force). The caller must write the file, THEN call mark_digested().
    conn.close()
    return "\n".join(lines), today, ids


def mark_digested(ids, day):
    """Record that these video ids were included in a successfully-written digest.
    Call ONLY after the digest file is durably on disk (see build_digest note)."""
    if not ids:
        return
    conn = sqlite3.connect(str(DB_PATH))
    placeholders = ",".join("?" * len(ids))
    conn.execute(
        f"UPDATE videos SET digested_at=? WHERE id IN ({placeholders})",
        [day] + list(ids),
    )
    conn.commit()
    conn.close()


def main():
    force = False
    limit = 50
    for a in sys.argv[1:]:
        if a == "--force":
            force = True
        elif a.startswith("--limit="):
            limit = int(a.split("=", 1)[1])
    md, today, ids = build_digest(force=force, limit=limit)
    # Avoid overwriting an earlier digest from the same day
    out = DIGEST_DIR / f"digest_{today}.md"
    if out.exists() and ids:
        seq = 2
        while (DIGEST_DIR / f"digest_{today}_{seq}.md").exists():
            seq += 1
        out = DIGEST_DIR / f"digest_{today}_{seq}.md"
    out.write_text(md, encoding="utf-8")
    (DIGEST_DIR / "latest.md").write_text(md, encoding="utf-8")
    # Mark digested only AFTER the file is durably written.
    mark_digested(ids, today)
    print(f"Wrote {out}")
    print(f"Newly digested: {len(ids)} video(s)")
    print(md)


if __name__ == "__main__":
    main()
