#!/usr/bin/env python3
"""Phase-1 spike — a CITED briefing synthesized across sources.

Proves the source-adapter seam end to end: discover real literature via the
ScholarlyAdapter (EuropePMC), normalize to `document`s, and synthesize a
two-layer advisor report (Executive Key Ideas + detailed analysis) where every
idea is cited to its source — validated against the real source list so a
citation can never point at something invented (LEARNINGS P6 / Working Rule 0).

The `sources` list is source-agnostic: video documents (transcript text + watch
URL) slot in identically — mixing modalities is just appending to the list.

  python3 spike_multisource.py --q "zone 2 training lactate threshold" --n 8
  python3 spike_multisource.py --profile zone2-training        # uses its literature arm
"""
import sys
import json
import urllib.error
from datetime import datetime
from pathlib import Path

import profiles
import analyze_transcripts as A
from sources.scholarly import ScholarlyAdapter

REPORTS = Path.home() / ".hermes" / "reports" / "_spike"
REPORTS.mkdir(parents=True, exist_ok=True)


def to_sources(documents, limit):
    documents = sorted(documents, key=lambda d: (d.get("authority", 0), d.get("published_date", "")),
                       reverse=True)[:limit]
    out = []
    for i, d in enumerate(documents, 1):
        out.append({
            "n": i, "id": d["id"], "title": d["title"], "byline": d["byline"],
            "url": d["url"], "date": d["published_date"], "kind": d["source_type"],
            "text": (d["text"] or "")[:2400],
            "meta": (f"{d['raw'].get('citations',0)} citations · {d['raw'].get('venue','')}"
                     if d["source_type"] == "literature" else d["byline"]),
        })
    return out


PROMPT = """You are an expert advisor writing a factual, educational briefing about
{focus}. You are given {n} verified sources (research papers and/or videos), each
numbered, with title, byline, and content (abstract or transcript). Synthesize them
into ONE cohesive briefing.

Two layers:
1. "Executive Key Ideas" = the summary: 5-7 short titled takeaways.
2. A detailed expansion of each — why it matters, what to do / what it implies, and
   specifics from the sources.

STRICT: use ONLY the supplied sources; cite every idea by source number in "sources";
never cite a number not provided; be factual, not promotional; note agreement/
disagreement across sources; do NOT write inline "(Source N)" prose.

Return JSON: {{"overview": "...",
  "key_ideas": [{{"title": "...", "summary": "...", "why_it_matters": "...",
                  "how_to_apply": ["..."], "details": "...", "sources": [1,3]}}]}}

SOURCES:
{sources}
"""


def render(report, sources, focus, today):
    by_n = {s["n"]: s for s in sources}

    def cites(nums):
        out = []
        for x in nums or []:
            s = by_n.get(int(x)) if str(x).isdigit() else None
            if s:
                out.append(f"[S{s['n']}]({s['url']})")
        return " " + " ".join(out) if out else ""

    L = [f"# Advisor Briefing — {focus}", f"_{today} · {len(sources)} verified sources · "
         "every idea cited; citations validated against the source list._\n"]
    if report.get("overview"):
        L += [report["overview"].strip(), ""]
    ideas = report.get("key_ideas", [])
    L += ["## Executive Key Ideas", ""]
    for i, k in enumerate(ideas, 1):
        L.append(f"{i}. **{(k.get('title') or '').strip()}** — {(k.get('summary') or '').strip()}{cites(k.get('sources'))}")
    L += ["", "## Detailed Analysis", ""]
    for i, k in enumerate(ideas, 1):
        L += [f"### {i}. {(k.get('title') or '').strip()}", ""]
        if k.get("why_it_matters"):
            L += [f"**Why it matters.** {k['why_it_matters'].strip()}", ""]
        steps = k.get("how_to_apply") or []
        if isinstance(steps, str):
            steps = [steps]
        if steps:
            L.append("**What it implies / what to do:**")
            L += [f"- {str(s).strip()}" for s in steps if str(s).strip()]
            L.append("")
        if k.get("details"):
            L += [f"**Details.** {k['details'].strip()}", ""]
        sc = cites(k.get("sources")).strip()
        if sc:
            L += [f"_Sources: {sc}_", ""]
    L += ["---", "", "## Sources", ""]
    for s in sources:
        tag = "📄" if s["kind"] == "literature" else "▶"
        L.append(f"- **S{s['n']}** {tag} [{s['title']}]({s['url']}) · {s['meta']} · {s['date']}")
    return "\n".join(L)


def main():
    q = []
    n = 8
    prof = None
    for a in sys.argv[1:]:
        if a.startswith("--q="):
            q = [a.split("=", 1)[1]]
        elif a == "--q" and sys.argv.index(a) + 1 < len(sys.argv):
            q = [sys.argv[sys.argv.index(a) + 1]]
        elif a.startswith("--n="):
            n = int(a.split("=", 1)[1])
        elif a == "--n":
            n = int(sys.argv[sys.argv.index(a) + 1])
        elif a.startswith("--profile="):
            prof = a.split("=", 1)[1]
        elif a == "--profile":
            prof = sys.argv[sys.argv.index(a) + 1]

    # Build the literature arm (from a profile, or from --q)
    if prof:
        p = profiles.load(prof)
        arm = p.get("literature") or {"queries": q, "max_results_per_source": 25}
        focus = p.get("analysis_focus", q[0] if q else "this topic")
    else:
        arm = {"enabled": True, "queries": q or ["zone 2 training lactate threshold"],
               "max_results_per_source": 25, "since_date": "2024-01-01"}
        focus = q[0] if q else "Zone 2 training & lactate metabolism"

    print(f"Discovering literature for: {focus!r} ...")
    docs = ScholarlyAdapter().discover(arm)
    print(f"  {len(docs)} unique papers (deduped by DOI).")
    if not docs:
        print("No sources found."); return

    sources = to_sources(docs, n)
    blocks = "\n".join(
        f"SOURCE {s['n']} [{s['kind']}]: \"{s['title']}\" — {s['byline']}\n"
        f"  ({s['meta']}, {s['date']})\n  {s['text']}\n" for s in sources)
    prompt = PROMPT.format(focus=focus, n=len(sources), sources=blocks)

    key, base, model = A.synth_config()
    if not key:
        print("No LLM key (set OPENAI_API_KEY / ~/.hermes/.env)."); return
    print(f"Synthesizing with {model} across {len(sources)} sources ...")
    try:
        report = A.call_llm(prompt, key, base, model)
    except urllib.error.HTTPError as e:
        print(f"LLM HTTP {e.code}: {e.read()[:200]}"); return

    today = datetime.now().strftime("%Y-%m-%d")
    md = render(report, sources, focus, today)
    out = REPORTS / f"briefing_{today}.md"
    out.write_text(md, encoding="utf-8")
    print(f"\nWrote {out}\n")
    print(md)


if __name__ == "__main__":
    main()
