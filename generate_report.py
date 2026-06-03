#!/usr/bin/env python3
"""Advisor Report — a factual, educational synthesis of the last N transcripts.

Unlike the per-video digest, this produces a single cohesive report across
multiple sources, led by an "Executive Key Ideas" section. Every idea and every
paragraph carries a citation back to the originating source video (deep-linked
to a timestamp where available).

Grounding (CLAUDE.md Working Rule 0): the report is built ONLY from material
already derived from verified transcripts — each source's extracted key points
(with timestamps), best quote, and a transcript excerpt. The LLM must attribute
every statement to a supplied source number; citations are validated against the
real source list, so they can't point at anything invented.

  python3 generate_report.py --n=8        # last 8 transcripts
"""
import os
import sys
import json
import sqlite3
import urllib.error
from pathlib import Path
from datetime import datetime

import profiles
import analyze_transcripts as A

_PROFILE = profiles.load()
DB_PATH = Path(_PROFILE["db_path"])
REPORTS_DIR = Path(_PROFILE["reports_dir"])
FOCUS = _PROFILE.get("analysis_focus", "this topic")
TITLE = _PROFILE.get("digest_title", "Advisor Report").replace("Best of", "Advisor Report:")

EXCERPT_CHARS = 1600       # transcript excerpt per source
MAX_SOURCES = 12           # hard cap to keep the prompt grounded + affordable


def fmt_ts(sec):
    sec = int(sec or 0)
    h, m, s = sec // 3600, (sec % 3600) // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def select_sources(n):
    """Last N analyzed transcripts (most recently transcribed first)."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT v.id, v.video_title, v.channel_name, v.transcribed_date,
               t.full_text, a.best_quote
        FROM videos v
        JOIN transcripts t ON t.video_id = v.id
        JOIN ai_analysis a ON a.video_id = v.id
        WHERE v.transcript_status = 'obtained'
        ORDER BY v.transcribed_date DESC, v.quality_score DESC
        LIMIT ?
    """, (min(n, MAX_SOURCES),)).fetchall()

    sources = []
    for i, r in enumerate(rows, 1):
        kps = conn.execute(
            "SELECT timestamp_sec, point_text FROM key_points WHERE video_id=? "
            "ORDER BY timestamp_sec LIMIT 8", (r["id"],)).fetchall()
        sources.append({
            "n": i,
            "id": r["id"],
            "title": r["video_title"],
            "channel": r["channel_name"],
            "best_quote": r["best_quote"] or "",
            "key_points": [{"t": k["timestamp_sec"], "text": k["point_text"]} for k in kps],
            "excerpt": (r["full_text"] or "")[:EXCERPT_CHARS],
            "first_ts": kps[0]["timestamp_sec"] if kps else 0,
        })
    conn.close()
    return sources


def build_prompt(sources):
    blocks = []
    for s in sources:
        kp = "\n".join(f"    - [{fmt_ts(k['t'])}] {k['text']}" for k in s["key_points"])
        blocks.append(
            f"SOURCE {s['n']}: \"{s['title']}\" — {s['channel']}\n"
            f"  Key points:\n{kp}\n"
            f"  Notable quote: {s['best_quote']}\n"
            f"  Transcript excerpt: {s['excerpt']}\n"
        )
    sources_text = "\n".join(blocks)
    return f"""You are an expert advisor writing a factual, educational report about
{FOCUS}. You are given {len(sources)} verified sources (video transcripts), each
numbered. Synthesize them into one cohesive report.

STRICT RULES:
- Use ONLY information contained in the supplied sources. Do not add outside facts.
- Every key idea and every paragraph MUST cite the source number(s) it came from,
  using the "sources" arrays. Never cite a source number that isn't provided.
- Be factual and educational, not promotional. Resolve agreements/disagreements
  across sources where relevant.

Produce JSON with this exact shape:
{{
  "overview": "2-4 sentence factual overview of what these sources collectively cover",
  "key_ideas": [
     {{"idea": "one substantive, self-contained key idea (2-4 sentences)", "sources": [1,3]}}
     // 5-8 of the most important, executive-level ideas
  ],
  "sections": [
     {{"heading": "Theme heading",
       "paragraphs": [ {{"text": "educational paragraph", "sources": [2]}} ]
     }}
     // 2-4 thematic sections that go deeper
  ]
}}

SOURCES:
{sources_text}
"""


def render(report, sources, today):
    by_n = {s["n"]: s for s in sources}

    def cites(nums):
        out = []
        for x in nums or []:
            s = by_n.get(int(x)) if str(x).isdigit() else None
            if not s:
                continue
            url = f"https://youtube.com/watch?v={s['id']}"
            if s["first_ts"]:
                url += f"&t={int(s['first_ts'])}s"
            out.append(f"[S{s['n']}]({url})")
        return " " + " ".join(out) if out else ""

    L = [f"# {TITLE} — {today}", ""]
    L.append(f"_Factual, educational synthesis of {len(sources)} verified transcripts. "
             f"Every idea is cited to its source._\n")
    if report.get("overview"):
        L += [report["overview"], ""]

    L += ["## Executive Key Ideas", ""]
    for i, k in enumerate(report.get("key_ideas", []), 1):
        L.append(f"{i}. {k.get('idea','').strip()}{cites(k.get('sources'))}")
    L.append("")

    for sec in report.get("sections", []):
        L += [f"## {sec.get('heading','').strip()}", ""]
        for para in sec.get("paragraphs", []):
            L.append(f"{para.get('text','').strip()}{cites(para.get('sources'))}\n")

    L += ["---", "", "## Sources", ""]
    for s in sources:
        L.append(f"- **S{s['n']}** — [{s['title']}](https://youtube.com/watch?v={s['id']}) · {s['channel']}")
    return "\n".join(L)


def build_report(n=8):
    sources = select_sources(n)
    today = datetime.now().strftime("%Y-%m-%d")
    if not sources:
        return (f"# {TITLE} — {today}\n\n_No analyzed transcripts yet. Transcribe and "
                "analyze some videos first (Intelligence tab), then generate a report._\n"), today

    key, base, model = A.llm_config()
    if not key:
        return (f"# {TITLE} — {today}\n\n_No LLM API key configured "
                "(set OPENAI_API_KEY or PODCAST_LLM_KEY)._\n"), today

    try:
        report = A.call_llm(build_prompt(sources), key, base, model)
    except urllib.error.HTTPError as e:
        return f"# {TITLE} — {today}\n\n_LLM error {e.code}._\n", today
    except Exception as e:
        return f"# {TITLE} — {today}\n\n_LLM error: {type(e).__name__}: {str(e)[:160]}._\n", today

    return render(report, sources, today), today


def main():
    n = 8
    for a in sys.argv[1:]:
        if a.startswith("--n="):
            n = int(a.split("=", 1)[1])
    md, today = build_report(n=n)
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / f"report_{today}.md"
    out.write_text(md, encoding="utf-8")
    (REPORTS_DIR / "latest.md").write_text(md, encoding="utf-8")
    print(f"Wrote {out}")
    print(md)


if __name__ == "__main__":
    main()
