#!/usr/bin/env python3
"""Investigation profiles — externalized, swappable search packages.

Each profile is a JSON file under ~/.hermes/profiles/ that bundles everything
that makes an investigation topic-specific: search queries, curated channels,
scoring keywords, filters, and the analysis/digest framing. Swapping the active
profile swaps the underlying SQLite DB, so investigations stay fully isolated
(e.g. "SEO/AI/GEO" vs "Zone 2 training" vs "stock trading").

This module is the single source of truth for:
  - which profile is active
  - that profile's config (queries/channels/keywords/filters/focus)
  - the DB path + digest dir for the active (or a named) profile

Used by podcast_scraper, fetch_transcripts, analyze_transcripts, generate_digest
and dashboard_server.
"""
import os
import re
import json
from pathlib import Path

HERMES = Path(os.environ.get("HERMES_DIR") or (Path.home() / ".hermes"))
PROFILES_DIR = HERMES / "profiles"
DB_DIR = HERMES / "db"
DIGESTS_DIR = HERMES / "digests"
ACTIVE_FILE = PROFILES_DIR / "_active"
LEGACY_DB = HERMES / "podcast_tracker.db"  # the original SEO/GEO dataset

# Filled-in defaults for any field a profile omits.
DEFAULTS = {
    "min_views": 1000,
    "min_duration_sec": 300,
    "max_duration_sec": 5400,
    "min_days_old": 7,
    "min_publish_date": "2025-01-01",   # include evergreen content, not just the current year
    "max_videos_per_query": 20,
    "max_videos_per_channel": 15,
    "channel_bonus": {},
    "analysis_focus": "the topic of this investigation",
    "digest_title": "Weekly Digest",
    # Enrichment cache thresholds — skip re-fetching video details if recently updated
    "enrich_recent_days": 30,    # videos published within N days are "new"
    "enrich_recent_hours": 24,   # re-enrich new videos after N hours
    "enrich_older_days": 7,      # re-enrich older videos after N days
}

# The built-in SEO/AI/GEO profile — seeds on first run. Points at the legacy DB
# so existing data carries over unchanged.
DEFAULT_PROFILE = {
    "name": "seo-geo",
    "label": "SEO / AI / GEO",
    "analysis_focus": "SEO, AI search / GEO (generative engine optimization), "
                      "answer-engine optimization, and driving website/video traffic",
    "digest_title": "Best of GEO/AI Search",
    "search_queries": [
        "how to rank in AI Overviews 2026",
        "get recommended by ChatGPT SEO",
        "generative engine optimization GEO 2026",
        "answer engine optimization AEO",
        "Google AI Mode SEO strategy 2026",
        "brand entity SEO knowledge graph",
        "how to get cited by AI search",
        "SEO to drive website traffic 2026",
        "AI search visibility for brands",
        "AEO vs GEO vs SEO",
        "GEO vs SEO what is the difference",
        "AI search optimization explained",
        "what is answer engine optimization",
        "is GEO just rebranded SEO",
    ],
    "curated_channels": {
        "neilpatel": "Neil Patel",
        "AhrefsCom": "Ahrefs",
        "surferseo": "Surfer SEO",
        "HubSpotMarketing": "HubSpot Marketing",
        "GoogleSearchCentral": "Google Search Central",
        "Semrush": "Semrush",
        "searchenginejournal": "Search Engine Journal",
        "searchengineland": "Search Engine Land",
        "LevelingUpOfficial": "Leveling Up with Eric Siu",
        "https://www.youtube.com/channel/UCHbtzKwhxTCv2b85um7880A": "James Dooley",
    },
    "channel_bonus": {
        "neilpatel": 1.5, "SearchOffTheRecord": 1.5, "GoogleSearchCentral": 1.5,
        "SEOFOMO": 1.2, "Niche Pursuits": 1.2, "Experts on the Wire": 1.1,
        "Semantic Mastery": 1.1, "Marketing Oops": 1.0, "RustyBrick GEO": 1.2,
        "Cyrus Shepard": 1.3, "Aleyda Solis": 1.3, "Lily Ray": 1.3,
        "Mordy Oberstein": 1.1, "Kevin Indig": 1.1, "James Dooley": 1.3,
    },
    "keywords": [
        "generative engine optimization", "GEO", "AI overviews", "AI mode",
        "search generative experience", "SGE", "brand entity", "entity SEO",
        "knowledge graph", "AI search", "AI SEO", "ChatGPT", "ChatGPT search",
        "recommended by ChatGPT", "recommended by AI", "Perplexity",
        "Google AI", "Gemini", "Copilot", "large language model", "LLM", "RAG",
        "zero-click", "zero click search", "answer engine", "answer engine optimization",
        "AEO", "featured snippet", "get cited", "cited by AI", "AI citations",
        "E-E-A-T", "EEAT", "topical authority", "AI-generated content", "AIO",
        "AI optimization", "AI visibility", "organic traffic", "website traffic",
        "drive traffic", "rank on Google", "google rank", "search rankings",
        "search traffic", "AI tools", "AI Mode", "llms.txt",
    ],
    "min_views": 2000,
    "min_duration_sec": 300,
    "max_duration_sec": 5400,
    "min_days_old": 7,
    "min_publish_date": "2025-01-01",
    "max_videos_per_query": 20,
    "max_videos_per_channel": 15,
}


def slug(name):
    return re.sub(r"[^a-z0-9_-]+", "-", str(name).strip().lower()).strip("-") or "profile"


def _ensure_dirs():
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    DB_DIR.mkdir(parents=True, exist_ok=True)
    DIGESTS_DIR.mkdir(parents=True, exist_ok=True)


def _seed_default():
    _ensure_dirs()
    p = PROFILES_DIR / "seo-geo.json"
    if not p.exists():
        p.write_text(json.dumps(DEFAULT_PROFILE, indent=2), encoding="utf-8")
    if not ACTIVE_FILE.exists():
        ACTIVE_FILE.write_text("seo-geo", encoding="utf-8")


def db_path_for(name):
    """Each profile gets its own DB. The default 'seo-geo' keeps the legacy file
    so existing data is preserved."""
    if slug(name) == "seo-geo":
        return str(LEGACY_DB)
    return str(DB_DIR / f"podcast_{slug(name)}.db")


def digest_dir_for(name):
    d = DIGESTS_DIR / slug(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def reports_dir_for(name):
    d = HERMES / "reports" / slug(name)
    d.mkdir(parents=True, exist_ok=True)
    return d


def active_name():
    _seed_default()
    # A spawned background job is pinned to the profile that launched it via the
    # PTD_PROFILE env var, so a profile switch mid-run can't redirect the job to
    # another profile's DB (LEARNINGS: spawned jobs read the active profile at
    # import). The dashboard process never sets this, so it reads _active below.
    env = os.environ.get("PTD_PROFILE")
    if env:
        env = slug(env)
        if (PROFILES_DIR / f"{env}.json").exists():
            return env
    try:
        return ACTIVE_FILE.read_text(encoding="utf-8").strip() or "seo-geo"
    except OSError:
        return "seo-geo"


def set_active(name):
    _seed_default()
    name = slug(name)
    if not (PROFILES_DIR / f"{name}.json").exists():
        raise FileNotFoundError(f"No profile named '{name}'")
    ACTIVE_FILE.write_text(name, encoding="utf-8")
    return name


def list_profiles():
    _seed_default()
    out = []
    active = active_name()
    for f in sorted(PROFILES_DIR.glob("*.json")):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({
            "name": d.get("name", f.stem),
            "label": d.get("label", d.get("name", f.stem)),
            "active": d.get("name", f.stem) == active,
            "queries": len(d.get("search_queries", [])),
            "channels": len(d.get("curated_channels", {})),
        })
    return out


def load(name=None):
    """Load a profile (active if name is None), with defaults filled in and
    resolved db_path / digest_dir."""
    _seed_default()
    name = slug(name or active_name())
    path = PROFILES_DIR / f"{name}.json"
    if not path.exists():
        path = PROFILES_DIR / "seo-geo.json"
        name = "seo-geo"
    cfg = dict(DEFAULTS)
    cfg.update(json.loads(path.read_text(encoding="utf-8")))
    cfg["name"] = cfg.get("name", name)
    cfg["label"] = cfg.get("label", cfg["name"])
    cfg["db_path"] = db_path_for(cfg["name"])
    cfg["digest_dir"] = str(digest_dir_for(cfg["name"]))
    cfg["reports_dir"] = str(reports_dir_for(cfg["name"]))
    cfg.setdefault("search_queries", [])
    cfg.setdefault("curated_channels", {})
    cfg.setdefault("keywords", [])
    return cfg


def save(profile):
    """Validate and persist a profile dict. Returns its slug name."""
    _seed_default()
    name = slug(profile.get("name") or profile.get("label") or "profile")
    profile["name"] = name
    profile.setdefault("label", name)
    # normalize list/dict fields
    profile.setdefault("search_queries", [])
    profile.setdefault("curated_channels", {})
    profile.setdefault("keywords", [])
    (PROFILES_DIR / f"{name}.json").write_text(json.dumps(profile, indent=2), encoding="utf-8")
    return name


EDITABLE_SETTINGS = ("min_views", "min_duration_sec", "max_duration_sec",
                     "min_days_old", "min_publish_date", "max_videos_per_channel",
                     "analysis_focus", "digest_title",
                     "enrich_recent_days", "enrich_recent_hours", "enrich_older_days")


def update(name, fields):
    """Update specific fields of an existing profile (raw JSON, defaults untouched)."""
    name = slug(name)
    path = PROFILES_DIR / f"{name}.json"
    p = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"name": name}
    for k, v in fields.items():
        if v is not None and v != "":
            p[k] = v
    return save(p)


def create(name, label=None, search_queries=None, curated_channels=None,
           keywords=None, analysis_focus=None, digest_title=None, **filters):
    """Scaffold a new profile (topic-agnostic skeleton) and save it."""
    prof = {
        "name": slug(name),
        "label": label or name,
        "search_queries": search_queries or [],
        "curated_channels": curated_channels or {},
        "keywords": keywords or [],
        "channel_bonus": {},
        "analysis_focus": analysis_focus or name,
        "digest_title": digest_title or f"Best of {label or name}",
    }
    for k in ("min_views", "min_duration_sec", "max_duration_sec", "min_days_old"):
        if k in filters and filters[k] is not None:
            prof[k] = filters[k]
    return save(prof)


# ── CLI ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if not args or args[0] == "list":
        for p in list_profiles():
            star = "*" if p["active"] else " "
            print(f" {star} {p['name']:<18} {p['label']:<28} "
                  f"({p['queries']} queries, {p['channels']} channels)")
    elif args[0] == "use" and len(args) > 1:
        print("Active profile:", set_active(args[1]))
    elif args[0] == "show":
        print(json.dumps(load(args[1] if len(args) > 1 else None), indent=2))
    elif args[0] == "path":
        print(load(args[1] if len(args) > 1 else None)["db_path"])
    else:
        print("usage: profiles.py [list | use <name> | show [name] | path [name]]")
