#!/usr/bin/env python3
"""Feature Ideation — compare podcast insights against TalkingToad's feature inventory.

Reads analyzed podcasts from the PTD database (key_points, seo_entities, geo_signals)
and compares them against TalkingToad's feature inventory to suggest new features.

Pipeline:
  1. Load TalkingToad feature inventory from YAML
  2. Query PTD database for the most recent analyzed videos
  3. Build a prompt that describes each podcast insight alongside the feature inventory
  4. Call the LLM to identify gaps and suggest new feature ideas
  5. Output structured feature suggestions that can feed into spec generation
"""

import os
import re
import sys
import json
import sqlite3
import time
import urllib.request
import urllib.error
from pathlib import Path

import yaml

import profiles

_PROFILE = profiles.load()
DB_PATH = Path(_PROFILE["db_path"])

# Default path to TalkingToad feature inventory
_TT_REPO = os.environ.get("TT_REPO", str(Path.home() / "projectsmini1" / "talkingtoad"))
FEATURE_INVENTORY_PATH = Path(_TT_REPO) / "docs" / "feature-inventory.yaml"

# How many key points to include per video in the prompt
MAX_KEY_POINTS_PER_VIDEO = int(os.environ.get("FF_IDEATION_MAX_KP", 10))
# How many recent analyzed videos to consider
MAX_VIDEOS = int(os.environ.get("FF_IDEATION_MAX_VIDEOS", 10))
# Minimum key points a video must have to be worth analyzing
MIN_KEY_POINTS = int(os.environ.get("FF_IDEATION_MIN_KP", 3))

ENV_FILE = profiles.HERMES / ".env"


def load_env():
    if ENV_FILE.is_file():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def llm_config():
    """SYNTHESIS role — stronger model for ideation. Falls back to bulk config."""
    load_env()
    key = os.environ.get("PODCAST_SYNTH_KEY") or os.environ.get("PODCAST_LLM_KEY") or os.environ.get("OPENAI_API_KEY")
    base = os.environ.get("PODCAST_SYNTH_BASE") or os.environ.get("PODCAST_LLM_BASE", "https://api.openai.com/v1")
    base = base.rstrip("/")
    model = os.environ.get("PODCAST_SYNTH_MODEL") or os.environ.get("PODCAST_LLM_MODEL", "gpt-4o-mini")
    return key, base, model


def call_llm(prompt, retries=3):
    key, base, model = llm_config()
    if not key:
        print("ERROR: no LLM API key set. Check PODCAST_SYNTH_KEY or PODCAST_LLM_KEY.")
        return None

    body = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": "You are a product strategist analyzing podcast insights against a feature inventory. You identify gaps — ideas discussed by experts that the target product doesn't have but should. Be concrete and specific. Always reference which podcast episode inspired each suggestion."},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.3,
    }).encode()

    last = None
    for attempt in range(retries):
        req = urllib.request.Request(
            f"{base}/chat/completions", data=body,
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read())
            content = data["choices"][0]["message"]["content"]
            # Try to parse as JSON first (preferred response format)
            try:
                return json.loads(content)
            except (json.JSONDecodeError, TypeError):
                return {"raw": content}
        except urllib.error.HTTPError as e:
            last = e
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"  LLM HTTP {e.code}: {e.read()[:200]}")
            return None
        except (urllib.error.URLError, TimeoutError) as e:
            last = e
            if attempt < retries - 1:
                time.sleep(3 * (attempt + 1))
                continue
            print(f"  LLM error: {type(e).__name__}: {str(e)[:160]}")
            return None
    return None


def load_feature_inventory():
    """Load TalkingToad feature inventory, returning structured dict."""
    if not FEATURE_INVENTORY_PATH.is_file():
        print(f"WARNING: Feature inventory not found at {FEATURE_INVENTORY_PATH}")
        print("Set TT_REPO environment variable to your TalkingToad repo path.")
        return None
    
    with open(FEATURE_INVENTORY_PATH) as f:
        inventory = yaml.safe_load(f)
    return inventory


def summarize_inventory_for_prompt(inventory):
    """Condense the feature inventory into a compact prompt-friendly summary."""
    if not inventory:
        return "No feature inventory available."
    
    lines = ["# TalkingToad — Shipped Features", ""]
    
    for domain_key, domain in inventory.items():
        if not isinstance(domain, dict):
            continue
        name = domain.get("description", domain_key.replace("_", " ").title())
        status = domain.get("status", "shipped")
        lines.append(f"## {name} [{status}]")
        
        if "categories" in domain:
            for cat_key, cat in domain["categories"].items():
                if isinstance(cat, dict):
                    cat_desc = cat.get("description", cat_key.replace("_", " ").title())
                    codes = cat.get("codes", [])
                    if codes:
                        # Summarise codes succinctly
                        brief = ", ".join(c[:3] for c in codes[:5])  # first 5 abbrevs
                        lines.append(f"  - {cat_desc} ({len(codes)} checks)")
                    else:
                        lines.append(f"  - {cat_desc}")
        elif "subdomains" in domain:
            for sub_key, sub in domain["subdomains"].items():
                if isinstance(sub, dict):
                    sub_desc = sub.get("description", sub_key.replace("_", " ").title())
                    lines.append(f"  - {sub_desc}")
        elif "features" in domain:
            for feat in domain.get("features", []):
                lines.append(f"  - {feat}")
        elif "endpoints" in domain:
            lines.append(f"  - {len(domain['endpoints'])} endpoints")
        
        lines.append("")
    
    # Add the app mission context
    lines.extend([
        "# TalkingToad Mission",
        "",
        "SEO crawler for nonprofits. Crawls websites, detects 152 issue types across",
        "technical SEO, AI-readiness, GEO, images, security. Fixes issues in WordPress",
        "via REST API. Produces PDF/CSV/Excel reports. Integrates with Google Search Console",
        "for performance data. Generates llms.txt, FAQ schema, entity schemas.",
        "",
    ])
    
    return "\n".join(lines)


def get_recent_analyzed_videos():
    """Fetch most recent analyzed videos with their key points."""
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    
    # Videos with AI analysis, ordered by most recently analyzed
    videos = conn.execute("""
        SELECT v.id, v.video_title, v.channel_name, a.analyzed_at,
               a.seo_entities, a.geo_signals, a.best_quote
        FROM videos v
        JOIN ai_analysis a ON a.video_id = v.id
        ORDER BY a.analyzed_at DESC
        LIMIT ?
    """, (MAX_VIDEOS,)).fetchall()
    
    result = []
    for v in videos:
        # Get key points for this video
        kps = conn.execute("""
            SELECT point_text, category, timestamp_sec
            FROM key_points
            WHERE video_id = ?
            ORDER BY timestamp_sec
            LIMIT ?
        """, (v["id"], MAX_KEY_POINTS_PER_VIDEO)).fetchall()
        
        if len(kps) < MIN_KEY_POINTS:
            continue  # Skip videos without enough signal
        
        result.append({
            "id": v["id"],
            "title": v["video_title"] or "(no title)",
            "channel": v["channel_name"] or "(unknown)",
            "analyzed_at": v["analyzed_at"],
            "entities": json.loads(v["seo_entities"]) if v["seo_entities"] else [],
            "geo_signals": json.loads(v["geo_signals"]) if v["geo_signals"] else [],
            "best_quote": v["best_quote"] or "",
            "key_points": [{"text": kp["point_text"], "category": kp["category"]} for kp in kps],
        })
    
    conn.close()
    return result


def format_videos_for_prompt(videos):
    """Format analyzed videos for the prompt."""
    sections = []
    for v in videos:
        body = [
            f"## Video: \"{v['title']}\"",
            f"Channel: {v['channel']}",
            f"Analyzed: {v['analyzed_at']}",
            "",
            "### Key Points:",
        ]
        for kp in v["key_points"]:
            body.append(f"  [{kp['category']}] {kp['text']}")
        
        if v["entities"]:
            body.append(f"\n### Entities: {', '.join(v['entities'][:15])}")
        if v["geo_signals"]:
            body.append(f"\n### Topics: {', '.join(v['geo_signals'][:10])}")
        if v["best_quote"]:
            body.append(f"\n### Best Quote: \"{v['best_quote'][:200]}\"")
        
        sections.append("\n".join(body))
    
    return "\n---\n".join(sections)


def run_ideation(output_file=None):
    """Run the full feature ideation pipeline."""
    print("=== Feature Ideation Pipeline ===")
    
    # Step 1: Load feature inventory
    print("\n[1/4] Loading TalkingToad feature inventory...")
    inventory = load_feature_inventory()
    if inventory:
        print(f"  Loaded: {len(inventory)} top-level domains")
    
    # Step 2: Get recent analyzed videos
    print("\n[2/4] Querying PTD database for analyzed podcasts...")
    videos = get_recent_analyzed_videos()
    if not videos:
        print("  No recently analyzed videos with sufficient key points found.")
        print("  Run 'python3 analyze_transcripts.py' first, then retry.")
        return 1
    print(f"  Found {len(videos)} video(s) with analysis data")
    for v in videos:
        print(f"    - \"{v['title'][:60]}\" ({len(v['key_points'])} key points)")
    
    # Step 3: Build prompt
    print("\n[3/4] Building LLM prompt for feature ideation...")
    inventory_summary = summarize_inventory_for_prompt(inventory)
    videos_text = format_videos_for_prompt(videos)
    
    prompt = f"""{inventory_summary}

# Podcast Insights (Recent Analysis)

Below are key insights extracted from recent podcasts about SEO, AI, and digital marketing.
Each insight comes from a verified transcript.

{videos_text}

# Task

Analyze the podcast insights above against TalkingToad's feature inventory.
Your job is to identify IDEAS discussed in these podcasts that TalkingToad
does NOT currently ship.

For each gap you identify, produce an entry with:
- idea: short name for the feature idea
- source_video: which podcast video inspired this
- podcast_insight: the specific insight that triggered this idea
- problem: what problem would this feature solve for TalkingToad users
- suggested_feature: description of what TalkingToad should build
- priority: high/medium/low (based on how central this is to TalkingToad's mission)
- implementation_complexity: simple/moderate/complex

If none of the podcast insights suggest anything new that TalkingToad doesn't already do,
return {{"gaps": [], "summary": "All podcast insights are already covered by TalkingToad's feature set."}}

Otherwise return a JSON object with:
- gaps: [array of gap entries as described above]
- summary: one-paragraph overview of the most important finding

Return ONLY valid JSON.
"""
    
    # Step 4: Call LLM
    print("\n[4/4] Running feature ideation analysis...")
    result = call_llm(prompt)
    
    if result is None:
        print("ERROR: LLM call failed.")
        return 1
    
    # Display results
    if "raw" in result:
        print("\n--- Raw Response ---")
        print(result["raw"])
        output = result["raw"]
    else:
        gaps = result.get("gaps", [])
        summary = result.get("summary", "")
        
        print(f"\n{'='*60}")
        print(f"FEATURE IDEATION RESULTS")
        print(f"{'='*60}")
        print(f"\nSummary: {summary}\n")
        
        if not gaps:
            print("No new feature ideas identified — all podcast insights are covered.")
        else:
            print(f"Found {len(gaps)} potential feature gap(s):\n")
            for i, gap in enumerate(gaps, 1):
                print(f"  {i}. {gap.get('idea', 'Untitled')} — [{gap.get('priority', '?').upper()}]")
                print(f"     Source: {gap.get('source_video', '?')[:60]}")
                print(f"     Insight: {gap.get('podcast_insight', '?')[:120]}")
                print(f"     Problem: {gap.get('problem', '?')[:120]}")
                print(f"     Feature: {gap.get('suggested_feature', '?')[:200]}")
                print(f"     Complexity: {gap.get('implementation_complexity', '?')}")
                print()
        
        output = json.dumps(result, indent=2)
    
    # Save to file if requested
    if output_file:
        out_path = Path(output_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(output)
        print(f"Saved full results to {out_path}")
    
    return 0


if __name__ == "__main__":
    output_file = None
    for a in sys.argv[1:]:
        if a.startswith("--output=") or a.startswith("-o"):
            output_file = a.split("=", 1)[1] if "=" in a else None
    
    save_dir = Path.home() / ".hermes" / "ideations"
    save_dir.mkdir(parents=True, exist_ok=True)
    
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    output_file = str(save_dir / f"ideation_{timestamp}.json")
    
    sys.exit(run_ideation(output_file=output_file))
