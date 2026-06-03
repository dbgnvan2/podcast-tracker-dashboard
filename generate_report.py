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

EXCERPT_CHARS = 2800       # transcript excerpt per source (richer grounding)
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
    return f"""You are an expert advisor writing a factual, educational briefing about
{FOCUS}. You are given {len(sources)} verified sources (video transcripts), each
numbered. Synthesize them into ONE cohesive report.

The report has two layers:
1. "Executive Key Ideas" = the SUMMARY: 5-7 of the most important takeaways, each a
   short title + a one-sentence summary.
2. A DETAILED expansion of each key idea — this is the bulk of the report. For each
   idea explain, in depth: why it matters (significance/consequences), the mechanism
   or reasoning behind it, and concretely what has to happen to implement it
   (actionable steps). Include specifics, examples, numbers, and nuances from the
   sources. Note where sources agree or disagree.

STRICT RULES:
- Use ONLY information contained in the supplied sources. Do not add outside facts.
- Every idea MUST cite the source number(s) it draws on via its "sources" array.
  Never cite a source number that wasn't provided.
- Be factual, specific and educational — not promotional or vague. Prefer concrete
  guidance ("do X by doing Y") over generalities.
- Do NOT write inline "(Source N)" references in your prose — citations are rendered
  automatically from the "sources" arrays. Just put the numbers in "sources".

Produce JSON with this EXACT shape:
{{
  "overview": "3-5 sentence factual overview of what these sources collectively cover and who should care",
  "key_ideas": [
     {{
       "title": "short imperative title (≤8 words)",
       "summary": "one-sentence executive summary of the idea",
       "why_it_matters": "1-2 substantial paragraphs: the significance, stakes, and consequences of getting this right or wrong",
       "how_to_implement": ["concrete actionable step", "another concrete step", "..."],
       "details": "1-2 paragraphs of deeper explanation: mechanism, evidence, examples, caveats, points of agreement/disagreement across sources",
       "sources": [1, 3]
     }}
     // 5-7 ideas, ordered most-important first
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

    ideas = report.get("key_ideas", [])

    L = [f"# {TITLE} — {today}", ""]
    L.append(f"_Factual, educational synthesis of {len(sources)} verified transcripts. "
             f"Every idea is cited to its source._\n")
    if report.get("overview"):
        L += [report["overview"].strip(), ""]

    # 1) Executive summary — the key ideas as a concise cited list
    L += ["## Executive Key Ideas", "",
          "_The summary. Each idea is expanded in Detailed Analysis below._", ""]
    for i, k in enumerate(ideas, 1):
        title = (k.get("title") or k.get("idea") or "").strip()
        summary = (k.get("summary") or "").strip()
        line = f"{i}. **{title}**" + (f" — {summary}" if summary else "")
        L.append(line + cites(k.get("sources")))
    L.append("")

    # 2) Detailed analysis — each idea expanded: why it matters + how to implement
    L += ["## Detailed Analysis", ""]
    for i, k in enumerate(ideas, 1):
        title = (k.get("title") or k.get("idea") or "").strip()
        L += [f"### {i}. {title}", ""]
        if k.get("why_it_matters"):
            L += [f"**Why it matters.** {k['why_it_matters'].strip()}", ""]
        steps = k.get("how_to_implement")
        if isinstance(steps, str):
            steps = [steps]
        if steps:
            L.append("**How to implement:**")
            for st in steps:
                if str(st).strip():
                    L.append(f"- {str(st).strip()}")
            L.append("")
        if k.get("details"):
            L += [f"**Details.** {k['details'].strip()}", ""]
        srcline = cites(k.get("sources")).strip()
        if srcline:
            L += [f"_Sources: {srcline}_", ""]

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

    key, base, model = A.synth_config()  # synthesis role (stronger model)
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
