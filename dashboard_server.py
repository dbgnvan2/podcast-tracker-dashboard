#!/usr/bin/env python3
import http.server
import json
import sqlite3
import os
import urllib.parse
import sys
import argparse
import subprocess
import urllib.request
from pathlib import Path

import profiles
import skiplist
import runstate

ENV_FILE = profiles.HERMES / ".env"
LLM_KEYS = ("PODCAST_LLM_KEY", "PODCAST_LLM_BASE", "PODCAST_LLM_MODEL", "PODCAST_SYNTH_MODEL")


def _load_env_file():
    if not ENV_FILE.is_file():
        return {}
    out = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_env_file(updates: dict):
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.is_file() else []
    written = set()
    new_lines = []
    for line in lines:
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and "=" in stripped:
            k = stripped.split("=", 1)[0].strip()
            if k in updates:
                if updates[k]:
                    new_lines.append(f'{k}={updates[k]}')
                written.add(k)
                continue
        new_lines.append(line)
    for k, v in updates.items():
        if k not in written and v:
            new_lines.append(f'{k}={v}')
    ENV_FILE.write_text("\n".join(new_lines) + "\n")

PROVIDERS_FILE = profiles.HERMES / "llm_providers.json"

def _load_providers():
    if not PROVIDERS_FILE.is_file():
        return {"providers": [], "bulk": {}, "synth": {}}
    try:
        return json.loads(PROVIDERS_FILE.read_text())
    except Exception:
        return {"providers": [], "bulk": {}, "synth": {}}

def _save_providers(data):
    PROVIDERS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROVIDERS_FILE.write_text(json.dumps(data, indent=2))

def _provider_key(pid):
    """Return the API key for a provider id from ~/.hermes/.env."""
    return _load_env_file().get(f"LLM_KEY_{pid}", "")

def _make_provider_id():
    import time
    return f"p{int(time.time() * 1000) % 10000000}"

def _migrate_legacy_llm():
    """One-time migration: if old PODCAST_LLM_KEY exists but no providers, create one."""
    data = _load_providers()
    if data["providers"]:
        return
    env = _load_env_file()
    key = env.get("PODCAST_LLM_KEY") or env.get("OPENAI_API_KEY", "")
    base = env.get("PODCAST_LLM_BASE", "https://api.openai.com/v1")
    model = env.get("PODCAST_LLM_MODEL", "gpt-4o-mini")
    synth_model = env.get("PODCAST_SYNTH_MODEL", "")
    if not key:
        return
    pid = _make_provider_id()
    label = "OpenAI" if "openai" in base else "LLM"
    data["providers"].append({"id": pid, "label": label, "base": base.rstrip("/")})
    data["bulk"] = {"provider_id": pid, "model": model, "thinking": "low"}
    if synth_model:
        data["synth"] = {"provider_id": pid, "model": synth_model, "thinking": "medium"}
    _write_env_file({f"LLM_KEY_{pid}": key})
    _save_providers(data)

_migrate_legacy_llm()

# DB_PATH is refreshed from the active investigation profile at the start of
# every request (the server is single-threaded, so this is safe). This is what
# makes profile-switching show a different dataset without restarting.
DB_PATH = profiles.load()["db_path"]
TRANSCRIPTS_DIR = str(profiles.HERMES / "transcripts")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FETCH_SCRIPT = os.path.join(SCRIPT_DIR, "fetch_transcripts.py")
ANALYZE_SCRIPT = os.path.join(SCRIPT_DIR, "analyze_transcripts.py")
DIGEST_SCRIPT = os.path.join(SCRIPT_DIR, "generate_digest.py")
REPORT_SCRIPT = os.path.join(SCRIPT_DIR, "generate_report.py")
SCRAPER_SCRIPT = os.path.join(SCRIPT_DIR, "podcast_scraper.py")
INGEST_LIT_SCRIPT = os.path.join(SCRIPT_DIR, "ingest_literature.py")


def refresh_active_db():
    """Point DB_PATH at the active profile's database for this request."""
    global DB_PATH
    DB_PATH = profiles.load()["db_path"]
    return DB_PATH


def migrate(db=None):
    target = db or DB_PATH
    print(f"Running migration on {target}...")
    conn = sqlite3.connect(target)
    cursor = conn.cursor()

    # Ensure the base videos table exists (fresh profile DBs start empty).
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id TEXT PRIMARY KEY, channel_name TEXT, channel_url TEXT, video_title TEXT,
            url TEXT, publish_date TEXT, duration_seconds INTEGER, views INTEGER,
            likes INTEGER, comments INTEGER, first_seen_date TEXT, last_updated_date TEXT,
            prev_views INTEGER, view_change INTEGER, view_change_pct REAL,
            transcript_keywords_score REAL, quality_score REAL, selected INTEGER DEFAULT 0,
            transcript_summary TEXT, transcript_status TEXT DEFAULT 'not_requested',
            transcribed_date TEXT, channel_id TEXT, is_new_channel INTEGER DEFAULT 0,
            discovered_via TEXT DEFAULT 'search', views_per_day REAL DEFAULT 0,
            source_type TEXT DEFAULT 'youtube', source TEXT DEFAULT 'youtube',
            doi TEXT, citations INTEGER DEFAULT 0, venue TEXT
        )
    """)
    cursor.execute("CREATE TABLE IF NOT EXISTS runs (id INTEGER PRIMARY KEY AUTOINCREMENT, run_date TEXT, videos_found INTEGER, videos_new INTEGER, errors TEXT)")

    # Ensure transcript_status exists
    cursor.execute("PRAGMA table_info(videos)")
    columns = [row[1] for row in cursor.fetchall()]
    
    if 'transcript_status' not in columns:
        print("Adding 'transcript_status' column...")
        cursor.execute("ALTER TABLE videos ADD COLUMN transcript_status TEXT DEFAULT 'not_requested'")
        
        # Initial migration from old booleans if they exist
        if 'transcribed' in columns:
            cursor.execute("UPDATE videos SET transcript_status = 'obtained' WHERE transcribed = 1")
        if 'transcribe_requested' in columns:
            cursor.execute("UPDATE videos SET transcript_status = 'requested' WHERE transcribe_requested = 1 AND transcript_status != 'obtained'")
            
    if 'transcribed_date' not in columns:
        print("Adding 'transcribed_date' column...")
        cursor.execute("ALTER TABLE videos ADD COLUMN transcribed_date TEXT")

    # Emerging-discovery columns (additive, idempotent)
    for col, ddl in [
        ("channel_id", "ALTER TABLE videos ADD COLUMN channel_id TEXT"),
        ("is_new_channel", "ALTER TABLE videos ADD COLUMN is_new_channel INTEGER DEFAULT 0"),
        ("discovered_via", "ALTER TABLE videos ADD COLUMN discovered_via TEXT DEFAULT 'search'"),
        ("views_per_day", "ALTER TABLE videos ADD COLUMN views_per_day REAL DEFAULT 0"),
        # Multi-source ("documents") columns — NULL/defaults for existing video rows.
        ("source_type", "ALTER TABLE videos ADD COLUMN source_type TEXT DEFAULT 'youtube'"),
        ("source", "ALTER TABLE videos ADD COLUMN source TEXT DEFAULT 'youtube'"),
        ("doi", "ALTER TABLE videos ADD COLUMN doi TEXT"),
        ("citations", "ALTER TABLE videos ADD COLUMN citations INTEGER DEFAULT 0"),
        ("venue", "ALTER TABLE videos ADD COLUMN venue TEXT"),
        # User-initiated dismiss — hide permanently without affecting transcript pipeline
        ("dismissed", "ALTER TABLE videos ADD COLUMN dismissed INTEGER DEFAULT 0"),
        # Track which videos have been included in a digest
        ("digested_at", "ALTER TABLE videos ADD COLUMN digested_at TEXT"),
        # Count fetch attempts so the overnight runner stops re-promoting a
        # stubbornly-failing 'error' row forever (see overnight_pipeline.py).
        ("fetch_attempts", "ALTER TABLE videos ADD COLUMN fetch_attempts INTEGER DEFAULT 0"),
    ]:
        if col not in columns:
            print(f"Adding '{col}' column...")
            cursor.execute(ddl)

    # Tables for transcript data
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transcripts (
            video_id TEXT PRIMARY KEY,
            file_path TEXT,
            full_text TEXT,
            word_count INTEGER,
            FOREIGN KEY(video_id) REFERENCES videos(id)
        )
    """)
    
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS key_points (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            video_id TEXT,
            timestamp_sec INTEGER,
            point_text TEXT,
            category TEXT,
            FOREIGN KEY(video_id) REFERENCES videos(id)
        )
    """)

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS ai_analysis (
            video_id TEXT PRIMARY KEY,
            seo_entities TEXT,
            geo_signals TEXT,
            best_quote TEXT,
            analyzed_at TEXT,
            FOREIGN KEY(video_id) REFERENCES videos(id)
        )
    """)

    # Channel registry — drives both authority monitoring and emerging detection.
    cursor.execute("""
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

    # Query-freshening: LLM-proposed search terms awaiting review.
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS suggested_terms (
            term TEXT PRIMARY KEY,
            source TEXT,
            created_at TEXT,
            status TEXT DEFAULT 'pending'
        )
    """)

    conn.commit()
    conn.close()
    print("Migration complete.")

def reconcile():
    """Make transcript_status honest: a video is only 'obtained' if a real
    transcript row backs it. Resets fakes and removes stub transcript files.
    Code-driven only — never hand-edit rows to paper over this."""
    print(f"Reconciling {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # 1. 'obtained' videos with no transcripts row are not really transcribed.
    #    Send them back to 'requested' so Process Queue fetches them for real.
    orphan_obtained = conn.execute("""
        SELECT v.id FROM videos v
        LEFT JOIN transcripts t ON t.video_id = v.id
        WHERE v.transcript_status = 'obtained' AND t.video_id IS NULL
    """).fetchall()
    for row in orphan_obtained:
        conn.execute(
            "UPDATE videos SET transcript_status = 'requested', transcribed_date = NULL WHERE id = ?",
            (row["id"],),
        )
    print(f"  Reset {len(orphan_obtained)} fake 'obtained' video(s) -> 'requested'.")

    # 2. Remove transcript files that have no backing transcripts row (stubs).
    backed = {r["video_id"] for r in conn.execute("SELECT video_id FROM transcripts")}
    removed = 0
    if os.path.isdir(TRANSCRIPTS_DIR):
        for fname in os.listdir(TRANSCRIPTS_DIR):
            if not fname.endswith(".txt"):
                continue
            vid = fname[:-4]
            if vid not in backed:
                os.remove(os.path.join(TRANSCRIPTS_DIR, fname))
                removed += 1
    print(f"  Removed {removed} unbacked transcript file(s).")

    # 3. Curated-channel videos marked 'not_available' are suspect: YouTube blocks
    #    (PO token / 429) are transient and were sometimes mis-recorded as "no
    #    captions". Re-queue monitored authorities' false negatives for an honest
    #    re-check (they have no transcript anyway).
    requeued = conn.execute("""
        UPDATE videos SET transcript_status = 'not_requested'
        WHERE transcript_status = 'not_available' AND discovered_via = 'channel'
          AND id NOT IN (SELECT video_id FROM transcripts)
    """).rowcount
    print(f"  Re-queued {requeued} curated 'not_available' video(s) for recheck.")

    conn.commit()
    conn.close()
    print("Reconcile complete.")

HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Podcast Tracker Dashboard</title>
    <style>
        :root {
            --bg-color: #f7f8fa;
            --card-bg: #ffffff;
            --text-main: #1a1d21;
            --text-dim: #5b6470;
            --accent: #2563eb;
            --accent-hover: #1d4ed8;
            --border: #e2e6ea;
            --row-alt: #f1f3f5;
            --success: #16a34a;
            --warning: #d97706;
            --error: #dc2626;
        }
        body.dark {
            --bg-color: #0f172a; --card-bg: #1e293b; --text-main: #f8fafc;
            --text-dim: #94a3b8; --accent: #38bdf8; --accent-hover: #7dd3fc;
            --border: #334155; --row-alt: #2d3748;
        }
        * { box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            margin: 0; padding: 20px; line-height: 1.5;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 30px; }
        h1 { margin: 0; font-size: 1.5rem; font-weight: 700; color: var(--accent); }
        
        .tabs { display: flex; gap: 10px; margin-bottom: 20px; border-bottom: 1px solid var(--border); padding-bottom: 10px; }
        .tab-btn {
            background: none; border: none; color: var(--text-dim); padding: 8px 16px; cursor: pointer;
            font-size: 1rem; border-radius: 6px; transition: all 0.2s;
        }
        .tab-btn:hover { background: var(--card-bg); color: var(--text-main); }
        .tab-btn.active { background: var(--accent); color: #fff; font-weight: 600; }
        
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        
        .search-container { margin-bottom: 20px; }
        .search-box {
            width: 100%; padding: 10px 15px; border-radius: 8px; border: 1px solid var(--border);
            background: var(--card-bg); color: var(--text-main); font-size: 1rem;
        }
        
        table { width: 100%; border-collapse: collapse; background: var(--card-bg); border-radius: 8px; overflow: hidden; border: 1px solid var(--border); }
        th { text-align: left; padding: 12px 15px; background: var(--row-alt); color: var(--text-dim); font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; }
        td { padding: 12px 15px; border-bottom: 1px solid var(--border); }
        tr:hover td { background: var(--row-alt); }
        
        .score { font-weight: bold; color: var(--accent); }
        .channel { color: var(--text-dim); font-size: 0.9rem; }
        .views { font-family: monospace; font-size: 0.9rem; }
        a { color: var(--accent); text-decoration: none; }
        a:hover { text-decoration: underline; }
        
        .btn {
            padding: 6px 12px; border-radius: 4px; border: none; cursor: pointer; font-size: 0.85rem; font-weight: 600;
            transition: opacity 0.2s;
        }
        .btn-mark { background: var(--success); color: white; }
        .btn-unreq { background: var(--error); color: white; }
        .btn-primary { background: var(--accent); color: #fff; padding: 8px 16px; font-size: 0.9rem; }
        .btn-view { background: var(--border); color: var(--text-main); }
        .btn:hover { opacity: 0.8; }
        .btn:disabled { opacity: 0.5; cursor: not-allowed; }

        .modal-overlay {
            display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.7);
            z-index: 100; align-items: center; justify-content: center; padding: 20px;
        }
        .modal-overlay.active { display: flex; }
        .modal {
            background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px;
            max-width: 800px; width: 100%; max-height: 85vh; display: flex; flex-direction: column;
        }
        .modal-header { display: flex; justify-content: space-between; align-items: flex-start; gap: 15px; padding: 20px; border-bottom: 1px solid var(--border); }
        .modal-title { font-size: 1.1rem; font-weight: 600; margin: 0; }
        .modal-sub { color: var(--text-dim); font-size: 0.85rem; margin-top: 4px; }
        .modal-close { background: none; border: none; color: var(--text-dim); font-size: 1.5rem; cursor: pointer; line-height: 1; }
        .modal-body { padding: 20px; overflow-y: auto; white-space: pre-wrap; line-height: 1.6; color: var(--text-main); }

        .intel-card { background: var(--card-bg); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 16px; }
        .intel-card h3 { margin: 0 0 6px; font-size: 1.05rem; }
        .intel-meta { color: var(--text-dim); font-size: 0.85rem; margin-bottom: 12px; }
        .intel-quote { border-left: 3px solid var(--accent); padding: 6px 14px; margin: 10px 0; color: var(--text-main); font-style: italic; }
        .chip { display: inline-block; background: var(--row-alt); color: var(--accent-hover); border: 1px solid var(--border); border-radius: 12px; padding: 3px 10px; margin: 3px 4px 3px 0; font-size: 0.78rem; }
        .chip-geo { color: var(--success); }
        .kp-list { list-style: none; padding: 0; margin: 10px 0 0; }
        .kp-list li { padding: 5px 0; border-top: 1px solid var(--border); font-size: 0.9rem; }
        .kp-time { font-family: monospace; color: var(--accent); margin-right: 8px; }
        .kp-cat { color: var(--text-dim); font-size: 0.78rem; }

        #print-header { display: none; }
        /* Print / Save-as-PDF: show only the active tab's content, always black on
           white (even if the dark theme is on). */
        @media print {
            body, body.dark { background: #fff !important; color: #000 !important; padding: 0; }
            header, .tabs, .btn, .search-container, .modal-overlay, #loading,
            #report-n, [id$="-status"], #last-updated, #profile-select,
            #table-emerging, #suggested-channels, #suggested-terms { display: none !important; }
            .tab-content:not(.active) { display: none !important; }
            .container { max-width: 100%; margin: 0; }
            .chart-section, .intel-card, table, td, th {
                background: #fff !important; color: #000 !important;
                border-color: #ccc !important; box-shadow: none !important; }
            .intel-meta, .kp-cat, .channel { color: #333 !important; }
            #report-content, #digest-content { white-space: normal; line-height: 1.5; }
            a { color: #000; text-decoration: underline; }
            #print-header { display: block; font-size: 0.85rem; color: #555;
                border-bottom: 1px solid #ccc; margin-bottom: 14px; padding-bottom: 6px; }
        }

        .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 15px; margin-bottom: 30px; }
        .stat-card { background: var(--card-bg); padding: 20px; border-radius: 12px; border: 1px solid var(--border); }
        .stat-label { color: var(--text-dim); font-size: 0.85rem; margin-bottom: 5px; }
        .stat-value { font-size: 1.8rem; font-weight: 700; color: var(--accent); }
        
        .chart-section { background: var(--card-bg); padding: 25px; border-radius: 12px; border: 1px solid var(--border); }
        .bar-container { display: flex; flex-direction: column; gap: 12px; margin-top: 20px; }
        .bar-row { display: flex; align-items: center; gap: 15px; }
        .bar-label { width: 180px; text-align: right; font-size: 0.85rem; color: var(--text-dim); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .bar-wrapper { flex-grow: 1; background: var(--border); height: 12px; border-radius: 6px; overflow: hidden; }
        .bar-fill { height: 100%; background: var(--accent); }
        .bar-count { width: 50px; font-size: 0.85rem; font-family: monospace; }
        
        #loading { text-align: center; padding: 50px; color: var(--text-dim); }
        .thinking-btn { padding:4px 12px; border:1px solid var(--border); background:var(--card-bg); color:var(--text-main); border-radius:4px; cursor:pointer; font-size:0.8rem; }
        .thinking-btn.active { background:var(--accent); color:#fff; border-color:var(--accent); }
        .llm-provider-row { display:flex; align-items:center; gap:10px; padding:8px 10px; border:1px solid var(--border); border-radius:6px; margin-bottom:6px; background:var(--card-bg); }
        .llm-provider-row .prov-label { font-weight:600; font-size:0.9rem; min-width:100px; }
        .llm-provider-row .prov-base { font-size:0.8rem; color:var(--text-dim); flex:1; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }
        .llm-provider-row .prov-hint { font-size:0.8rem; color:var(--text-dim); min-width:70px; text-align:right; }
        .icon-btn { background:none; border:none; cursor:pointer; font-size:1rem; padding:2px 6px; border-radius:4px; color:var(--text-dim); }
        .icon-btn:hover { background:var(--border); color:var(--text-main); }
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Podcast Tracker</h1>
            <div style="display:flex; gap:10px; align-items:center;">
                <span style="color:var(--text-dim);font-size:0.85rem">Investigation:</span>
                <select id="profile-select" onchange="switchProfile(this.value)"
                        style="background:var(--card-bg);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:0.9rem;"></select>
                <button class="btn btn-view" onclick="toggleTheme()" title="Light / dark">🌓</button>
                <button class="btn btn-view" onclick="openSettings()">⚙ Settings</button>
                <button class="btn btn-primary" onclick="openNewProfile()">+ New</button>
                <div id="last-updated" style="font-size: 0.8rem; color: var(--text-dim); margin-left:10px;"></div>
            </div>
        </header>
        <div id="print-header"></div>
        
        <div class="tabs">
            <button class="tab-btn active" onclick="showTab('candidates', this)">Candidates</button>
            <button class="tab-btn" onclick="showTab('requested', this)">Requested</button>
            <button class="tab-btn" onclick="showTab('transcribed', this)">Transcribed</button>
            <button class="tab-btn" onclick="showTab('intelligence', this)">Intelligence</button>
            <button class="tab-btn" onclick="showTab('digest', this)">Digest</button>
            <button class="tab-btn" onclick="showTab('report', this)">Report</button>
            <button class="tab-btn" onclick="showTab('discovery', this)">Discovery</button>
            <button class="tab-btn" onclick="showTab('stats', this)">Stats</button>
        </div>
        
        <div id="loading">Loading data...</div>

        <div id="discovery-log-panel" style="display:none;margin-bottom:16px;border:1px solid var(--border);border-radius:8px;overflow:hidden">
            <div style="display:flex;align-items:center;justify-content:space-between;padding:6px 12px;background:var(--card-bg);border-bottom:1px solid var(--border)">
                <span style="font-weight:600;font-size:0.85rem">Discovery log</span>
                <div style="display:flex;align-items:center;gap:10px">
                    <span id="discovery-log-status" style="font-size:0.8rem;color:var(--text-dim)"></span>
                    <button class="btn" onclick="toggleLogPanel()" style="font-size:0.75rem;padding:2px 8px">Hide</button>
                </div>
            </div>
            <pre id="discovery-log-lines" style="margin:0;padding:8px 12px;font-size:0.72rem;line-height:1.45;max-height:160px;overflow-y:auto;background:var(--bg);color:var(--text-main);white-space:pre-wrap;word-break:break-word"></pre>
        </div>

        <div id="candidates" class="tab-content active">
            <div style="display:flex; gap:8px; align-items:center; margin-bottom:12px;">
                <button class="btn btn-primary" onclick="runDiscovery()">🔄 Run Discovery</button>
                <span class="discovery-status" style="color:var(--text-dim);font-size:0.85rem;"></span>
                <span style="flex:1"></span>
                <span style="color:var(--text-dim);font-size:0.85rem">Sort:</span>
                <button id="sort-quality" class="tab-btn active" onclick="setCandidateSort('quality')">Quality</button>
                <button id="sort-trending" class="tab-btn" onclick="setCandidateSort('trending')">🔥 Trending</button>
            </div>
            <div class="search-container"><input type="text" class="search-box" placeholder="Search candidates..." oninput="filterTable('candidates')"></div>
            <div id="candidates-count" style="color:var(--text-dim);font-size:0.8rem;margin-bottom:6px"></div>
            <table id="table-candidates">
                <thead><tr><th width="50">Score</th><th>Title</th><th>Channel</th><th>Views</th><th>Views/day</th><th width="100">Action</th></tr></thead>
                <tbody></tbody>
            </table>
        </div>

        <div id="requested" class="tab-content">
            <div id="transcribe-banner" style="display:none;margin-bottom:15px;padding:10px 14px;border-radius:8px;border:1px solid var(--border);font-size:0.9rem;"></div>
            <div style="display:flex; gap:12px; align-items:center; margin-bottom:15px;">
                <button id="queue-btn" class="btn btn-primary" onclick="processQueue()">⚙ Process Queue</button>
                <span id="queue-status" style="color: var(--text-dim); font-size: 0.85rem;"></span>
            </div>
            <div class="search-container"><input type="text" class="search-box" placeholder="Search requested..." oninput="filterTable('requested')"></div>
            <table id="table-requested">
                <thead><tr><th width="50">Score</th><th>Title</th><th>Channel</th><th>Views</th><th>Date</th><th width="100">Action</th></tr></thead>
                <tbody></tbody>
            </table>
        </div>

        <div id="transcribed" class="tab-content">
            <div class="search-container"><input type="text" class="search-box" placeholder="Search transcribed..." oninput="filterTable('transcribed')"></div>
            <table id="table-transcribed">
                <thead><tr><th width="50">Score</th><th>Title</th><th>Channel</th><th>Views</th><th>Transcribed</th><th title="Key Points Count">KP</th></tr></thead>
                <tbody></tbody>
            </table>
        </div>

        <div id="intelligence" class="tab-content">
            <div style="display:flex; gap:12px; align-items:center; margin-bottom:15px;">
                <button id="analyze-btn" class="btn btn-primary" onclick="runAnalyze()">🧠 Analyze Transcripts</button>
                <span id="analyze-status" style="color: var(--text-dim); font-size: 0.85rem;"></span>
            </div>
            <div id="intel-list"></div>
        </div>

        <div id="digest" class="tab-content">
            <div style="display:flex; gap:12px; align-items:center; flex-wrap:wrap; margin-bottom:15px;">
                <button id="digest-btn" class="btn btn-primary" onclick="generateDigest()">📰 Generate Digest</button>
                <span id="digest-pending" style="color:var(--text-dim);font-size:0.85rem;margin-left:8px"></span>
                <button class="btn" onclick="toggleDigestList(this)">📋 Past Digests</button>
                <button class="btn btn-view" onclick="printDoc('Digest')">🖨 Save as PDF</button>
                <span id="digest-status" style="color: var(--text-dim); font-size: 0.85rem;"></span>
            </div>
            <div id="digest-list-panel" style="display:none;background:var(--card-bg);border:1px solid var(--border);border-radius:8px;padding:12px 16px;margin-bottom:15px;">
                <div id="digest-list-rows"></div>
            </div>
            <div class="chart-section" id="digest-content" style="white-space: pre-wrap; line-height: 1.6;"></div>
        </div>

        <div id="report" class="tab-content">
            <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:10px">
                <label style="color:var(--text-dim);font-size:0.85rem">From<input type="date" id="report-from" style="background:var(--card-bg);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px;margin-left:4px"></label>
                <label style="color:var(--text-dim);font-size:0.85rem">To<input type="date" id="report-to" style="background:var(--card-bg);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px;margin-left:4px"></label>
                <label style="color:var(--text-dim);font-size:0.85rem">Max sources<input id="report-n" type="number" value="12" min="2" max="24" style="width:60px;background:var(--card-bg);color:var(--text-main);border:1px solid var(--border);border-radius:6px;padding:6px;margin-left:4px"></label>
                <span id="report-source-count" style="color:var(--text-dim);font-size:0.85rem;min-width:90px"></span>
                <button id="report-btn" class="btn btn-primary" onclick="generateReport()">📋 Generate Advisor Report</button>
                <span id="report-status" style="color:var(--text-dim);font-size:0.85rem;"></span>
            </div>
            <div style="margin-bottom:14px">
                <div style="font-size:0.8rem;color:var(--text-dim);margin-bottom:6px">Filter by channel <span style="font-style:italic">(click to toggle; pick any number — none = all channels)</span></div>
                <div id="report-channel-list" style="display:flex;flex-wrap:wrap;gap:6px;max-height:120px;overflow-y:auto;padding:2px"></div>
            </div>
            <button class="btn btn-view" onclick="printDoc('Advisor Report')">🖨 Save as PDF</button>
            <div class="chart-section" id="report-content" style="white-space: pre-wrap; line-height: 1.65;"></div>
        </div>

        <div id="discovery" class="tab-content">
            <div style="display:flex; gap:12px; align-items:center; margin-bottom:18px;">
                <button class="btn btn-primary" onclick="runDiscovery()">🔄 Run Discovery</button>
                <span class="discovery-status" style="color:var(--text-dim);font-size:0.85rem;"></span>
            </div>
            <h3 style="margin-top:0">🆕 Emerging videos <span style="color:var(--text-dim);font-size:0.8rem;font-weight:400">— from channels we hadn't seen before</span></h3>
            <table id="table-emerging">
                <thead><tr><th width="50">Score</th><th>Title</th><th>Channel</th><th>Views/day</th><th width="110">Action</th></tr></thead>
                <tbody></tbody>
            </table>

            <h3 style="margin-top:28px">📡 Suggested channels to monitor <span style="color:var(--text-dim);font-size:0.8rem;font-weight:400">— strangers who keep producing winners</span></h3>
            <div id="suggested-channels"></div>

            <div style="display:flex; gap:12px; align-items:center; margin:28px 0 12px;">
                <h3 style="margin:0">🔎 Suggested search terms</h3>
                <button class="btn btn-primary" onclick="suggestTerms()">Generate from top content</button>
                <span id="terms-status" style="color:var(--text-dim);font-size:0.85rem;"></span>
            </div>
            <div id="suggested-terms"></div>
        </div>

        <div id="stats" class="tab-content">
            <div class="stats-grid" id="stats-summary"></div>
            <div class="chart-section">
                <h3>Top Channels by Video Count</h3>
                <div class="bar-container" id="channel-chart"></div>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="settings-modal" onclick="if(event.target===this)closeSettings()">
        <div class="modal" style="max-width:900px">
            <div class="modal-header">
                <div><h3 class="modal-title">Settings — <span id="settings-profile"></span></h3>
                <div class="modal-sub">Discovery filters for the active investigation. Applied on the next scrape.</div></div>
                <button class="modal-close" onclick="closeSettings()">&times;</button>
            </div>
            <div class="modal-body" style="max-height:80vh;overflow-y:auto">
                <!-- Discovery Filters -->
                <h4 style="margin:0 0 12px;font-size:0.9rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text-dim)">Discovery Filters</h4>
                <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px">
                    <label>Min views<input id="set-min_views" type="number" class="search-box" min="0" step="100"></label>
                    <label>Published on/after<input id="set-min_publish_date" class="search-box" placeholder="2025-01-01"></label>
                    <label>Min duration (sec)<input id="set-min_duration_sec" type="number" class="search-box" min="0"></label>
                    <label>Max duration (sec)<input id="set-max_duration_sec" type="number" class="search-box" min="0"></label>
                    <label>Min days old<input id="set-min_days_old" type="number" class="search-box" min="0"></label>
                    <label>Videos per channel<input id="set-max_videos_per_channel" type="number" class="search-box" min="1"></label>
                </div>
                <h4 style="margin:16px 0 8px;font-size:0.85rem;color:var(--text-dim)">Enrichment Cache — skip re-fetching recently updated videos</h4>
                <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px">
                    <label title="Videos published within this many days are treated as new">"New" if published within (days)<input id="set-enrich_recent_days" type="number" class="search-box" min="1"></label>
                    <label title="Re-fetch new videos after this many hours">Re-enrich new videos after (hours)<input id="set-enrich_recent_hours" type="number" class="search-box" min="1"></label>
                    <label title="Re-fetch older videos after this many days">Re-enrich older videos after (days)<input id="set-enrich_older_days" type="number" class="search-box" min="1"></label>
                </div>
                <div style="display:flex;gap:10px;margin-top:12px;align-items:center">
                    <button class="btn btn-primary" onclick="saveSettings()">Save filters</button>
                    <span id="set-status" style="color:var(--text-dim);font-size:0.85rem"></span>
                </div>

                <hr style="border:none;border-top:1px solid var(--border);margin:20px 0">

                <!-- LLM Providers -->
                <div style="display:flex;align-items:center;justify-content:space-between;margin-bottom:12px">
                    <h4 style="margin:0;font-size:0.9rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text-dim)">LLM Providers</h4>
                    <button class="btn btn-primary" onclick="openAddProvider()" style="font-size:0.8rem;padding:4px 12px">+ Add LLM API</button>
                </div>
                <div id="llm-providers-list" style="margin-bottom:16px">
                    <div style="color:var(--text-dim);font-size:0.85rem">Loading…</div>
                </div>

                <hr style="border:none;border-top:1px solid var(--border);margin:20px 0">

                <!-- Active Model Selection -->
                <h4 style="margin:0 0 12px;font-size:0.9rem;text-transform:uppercase;letter-spacing:.05em;color:var(--text-dim)">Active Model Selection</h4>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:16px">
                    <div>
                        <div style="font-size:0.8rem;font-weight:600;color:var(--text-main);margin-bottom:6px">Bulk — per-transcript analysis</div>
                        <label style="font-size:0.8rem">Provider
                            <select id="sel-bulk-provider" class="search-box" onchange="onProviderChange('bulk')"></select>
                        </label>
                        <label style="font-size:0.8rem;margin-top:6px;display:block">Model
                            <div style="display:flex;gap:6px">
                                <select id="sel-bulk-model" class="search-box" style="flex:1"></select>
                                <button class="btn" onclick="fetchModelsFor('bulk')" title="Refresh models" style="padding:4px 8px">⟳</button>
                            </div>
                        </label>
                        <div style="margin-top:8px;font-size:0.8rem;font-weight:600;color:var(--text-main)">Thinking level</div>
                        <div style="display:flex;gap:6px;margin-top:4px" id="bulk-thinking-btns">
                            <button class="thinking-btn" data-role="bulk" data-level="low" onclick="setThinking('bulk','low')">Low</button>
                            <button class="thinking-btn" data-role="bulk" data-level="medium" onclick="setThinking('bulk','medium')">Medium</button>
                            <button class="thinking-btn" data-role="bulk" data-level="high" onclick="setThinking('bulk','high')">High</button>
                        </div>
                    </div>
                    <div>
                        <div style="font-size:0.8rem;font-weight:600;color:var(--text-main);margin-bottom:6px">Synth — digests &amp; reports</div>
                        <label style="font-size:0.8rem">Provider
                            <select id="sel-synth-provider" class="search-box" onchange="onProviderChange('synth')"></select>
                        </label>
                        <label style="font-size:0.8rem;margin-top:6px;display:block">Model
                            <div style="display:flex;gap:6px">
                                <select id="sel-synth-model" class="search-box" style="flex:1"></select>
                                <button class="btn" onclick="fetchModelsFor('synth')" title="Refresh models" style="padding:4px 8px">⟳</button>
                            </div>
                        </label>
                        <div style="margin-top:8px;font-size:0.8rem;font-weight:600;color:var(--text-main)">Thinking level</div>
                        <div style="display:flex;gap:6px;margin-top:4px" id="synth-thinking-btns">
                            <button class="thinking-btn" data-role="synth" data-level="low" onclick="setThinking('synth','low')">Low</button>
                            <button class="thinking-btn" data-role="synth" data-level="medium" onclick="setThinking('synth','medium')">Medium</button>
                            <button class="thinking-btn" data-role="synth" data-level="high" onclick="setThinking('synth','high')">High</button>
                        </div>
                    </div>
                </div>
                <div style="display:flex;gap:10px;margin-top:16px;align-items:center">
                    <button class="btn btn-primary" onclick="saveModelSelection()">Save model selection</button>
                    <span id="llm-sel-status" style="font-size:0.85rem;color:var(--text-dim)"></span>
                </div>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="llm-provider-modal" onclick="if(event.target===this)closeProviderModal()">
        <div class="modal" style="max-width:480px">
            <div class="modal-header">
                <div><h3 class="modal-title" id="llm-prov-modal-title">Add LLM Provider</h3></div>
                <button class="modal-close" onclick="closeProviderModal()">&times;</button>
            </div>
            <div class="modal-body">
                <input type="hidden" id="llm-prov-id">
                <div style="display:flex;flex-direction:column;gap:12px">
                    <label>Label (e.g. "OpenAI", "Anthropic")
                        <input id="llm-prov-label" class="search-box" placeholder="OpenAI">
                    </label>
                    <label>API Base URL
                        <input id="llm-prov-base" class="search-box" placeholder="https://api.openai.com/v1">
                    </label>
                    <label>API Key <span id="llm-prov-key-hint" style="font-size:0.8rem;color:var(--text-dim)"></span>
                        <input id="llm-prov-key" type="password" class="search-box" placeholder="sk-…">
                    </label>
                </div>
                <div style="display:flex;gap:10px;margin-top:16px;align-items:center">
                    <button class="btn btn-primary" onclick="saveProvider()">Save</button>
                    <button class="btn" onclick="closeProviderModal()">Cancel</button>
                    <span id="llm-prov-status" style="font-size:0.85rem;color:var(--text-dim)"></span>
                </div>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="profile-modal" onclick="if(event.target===this)closeProfileModal()">
        <div class="modal" style="max-width:680px">
            <div class="modal-header">
                <div><h3 class="modal-title">New investigation profile</h3>
                <div class="modal-sub">Define a search package for a different topic (e.g. Zone 2 training, stock trading).</div></div>
                <button class="modal-close" onclick="closeProfileModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
                    <label>Name (id)<input id="np-name" class="search-box" placeholder="interstitium"></label>
                    <label>Label<input id="np-label" class="search-box" placeholder="Interstitium"></label>
                </div>
                <label>Analysis focus (for AI extraction + digest framing)
                    <input id="np-focus" class="search-box" placeholder="the human interstitium — anatomy, interstitial fluid, lymphatics, disease"></label>
                <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px">
                    <label>Digest title<input id="np-digest" class="search-box" placeholder="Best of Interstitium Research"></label>
                    <label>Scoring keywords (comma-separated)<input id="np-keywords" class="search-box" placeholder="interstitium, interstitial fluid, collagen, lymphatic"></label>
                </div>

                <fieldset style="border:1px solid var(--border);border-radius:8px;padding:12px;margin-top:14px">
                    <legend style="padding:0 6px"><label style="display:inline"><input type="checkbox" id="np-yt-enabled" checked> ▶ Search YouTube</label></legend>
                    <label>Video search queries (one per line)
                        <textarea id="np-queries" class="search-box" rows="3" placeholder="interstitium explained&#10;interstitial system anatomy"></textarea></label>
                    <label>Channels to monitor — <span style="color:var(--text-dim);font-size:0.8rem">one per line, <code>handle = Name</code></span>
                        <textarea id="np-channels" class="search-box" rows="2" placeholder="nucleusmedicalmedia = Nucleus Medical Media"></textarea></label>
                </fieldset>

                <fieldset style="border:1px solid var(--border);border-radius:8px;padding:12px;margin-top:12px">
                    <legend style="padding:0 6px"><label style="display:inline"><input type="checkbox" id="np-lit-enabled"> 📄 Search literature (papers)</label></legend>
                    <label>Paper search queries (one per line)
                        <textarea id="np-lit-queries" class="search-box" rows="3" placeholder="human interstitium anatomy&#10;interstitial fluid connective tissue&#10;interstitium fibrosis disease"></textarea></label>
                    <label style="display:block;margin-top:6px">Published since <input id="np-lit-since" class="search-box" style="max-width:140px" placeholder="2018-01-01">
                        <span style="color:var(--text-dim);font-size:0.8rem"> · source: EuropePMC (PubMed + preprints + PMC)</span></label>
                </fieldset>

                <div style="display:flex;gap:10px;margin-top:14px;align-items:center">
                    <button class="btn btn-view" onclick="testNewProfile()">Test (preview reach)</button>
                    <button class="btn btn-primary" onclick="createProfile()">Create &amp; switch</button>
                    <span id="np-status" style="color:var(--text-dim);font-size:0.85rem"></span>
                </div>
                <div id="np-test-result" style="margin-top:12px"></div>
            </div>
        </div>
    </div>

    <div class="modal-overlay" id="transcript-modal" onclick="if(event.target===this)closeModal()">
        <div class="modal">
            <div class="modal-header">
                <div>
                    <h3 class="modal-title" id="modal-title">Transcript</h3>
                    <div class="modal-sub" id="modal-sub"></div>
                </div>
                <button class="modal-close" onclick="closeModal()">&times;</button>
            </div>
            <div class="modal-body" id="modal-body">Loading…</div>
        </div>
    </div>

    <script>
        let data = { candidates: [], requested: [], transcribed: [], stats: {}, intelligence: [], discovery: {} };
        let candidateSort = 'quality';  // 'quality' | 'trending'

        async function fetchData() {
            document.getElementById('loading').style.display = 'block';
            try {
                const [cRes, rRes, tRes, sRes, iRes, dRes, txRes] = await Promise.all([
                    fetch('/api/candidates').then(r => r.json()),
                    fetch('/api/requested').then(r => r.json()),
                    fetch('/api/transcribed').then(r => r.json()),
                    fetch('/api/stats').then(r => r.json()),
                    fetch('/api/intelligence').then(r => r.json()),
                    fetch('/api/discovery').then(r => r.json()),
                    fetch('/api/transcribe_status').then(r => r.json())
                ]);
                data.candidates = cRes;
                data.requested = rRes;
                data.transcribed = tRes;
                data.stats = sRes;
                data.intelligence = iRes;
                data.discovery = dRes;
                data.transcribeStatus = txRes;
                renderAll();
            } catch (err) {
                console.error('Fetch error:', err);
            } finally {
                document.getElementById('loading').style.display = 'none';
            }
        }

        function renderAll() {
            renderTable('candidates', data.candidates);
            renderTable('requested', data.requested);
            renderTable('transcribed', data.transcribed);
            renderIntelligence();
            renderDiscovery();
            renderStats();
            renderTranscribeBanner();
            // Surface list truncation: the endpoints cap at 200, but stats has the
            // true totals — never let a cap silently hide rows (LEARNINGS P9).
            const cc = document.getElementById('candidates-count');
            if (cc) {
                const shown = (data.candidates || []).length;
                const total = (data.stats && data.stats.not_requested) || shown;
                cc.innerText = total > shown
                    ? `Showing top ${shown} of ${total} candidates by quality score — transcribe or dismiss some to surface the rest.`
                    : `${shown} candidate${shown === 1 ? '' : 's'}.`;
            }
            document.getElementById('last-updated').innerText = 'Last updated: ' + new Date().toLocaleTimeString();
        }

        function renderTable(type, items) {
            const tbody = document.querySelector(`#table-${type} tbody`);
            tbody.innerHTML = '';

            let rows = items.slice();
            if (type === 'candidates' && candidateSort === 'trending') {
                rows.sort((a, b) => (b.views_per_day || 0) - (a.views_per_day || 0));
            }

            rows.forEach(v => {
                const tr = document.createElement('tr');
                tr.setAttribute('data-id', v.id);

                let actionHtml = '';
                if (type === 'candidates') {
                    actionHtml = `<td style="white-space:nowrap">
                        <button class="btn btn-mark" onclick="postAction('/api/request_transcribe', '${v.id}')">Transcribe</button>
                        <button class="btn" style="background:var(--border);color:var(--text-dim);margin-left:4px" title="Skip forever — never show this title again, even if re-uploaded" onclick="dismissVideo('${v.id}', this)">Skip</button>
                    </td>`;
                } else if (type === 'requested') {
                    actionHtml = `<td><button class="btn btn-unreq" onclick="postAction('/api/unrequest', '${v.id}')">Remove</button></td>`;
                } else if (type === 'transcribed') {
                    actionHtml = `<td style="text-align:center; white-space:nowrap">${v.key_point_count || 0} &nbsp;<button class="btn btn-view" onclick="viewTranscript('${v.id}')">View</button></td>`;
                }

                let dateCol = v.publish_date || '';
                if (type === 'transcribed') {
                    // Feature #3: dedicated "Transcribed" date column — just the
                    // date (all rows here are already status 'obtained'); older
                    // rows with no stored date show an em-dash.
                    dateCol = v.transcribed_date || '—';
                } else if (v.views_per_day) {
                    dateCol = `${(v.views_per_day).toLocaleString()}/day`;
                }
                const newBadge = v.is_new_channel ? ' <span class="chip" style="background:var(--warning);color:#000">🆕 new</span>' : '';
                const srcBadge = (v.source_type === 'literature') ? '📄 ' : '';
                const docUrl = v.url || ('https://youtube.com/watch?v=' + v.id);

                tr.innerHTML = `
                    <td class="score">${(v.quality_score || 0).toFixed(2)}</td>
                    <td>${srcBadge}<a href="${docUrl}" target="_blank">${v.video_title}</a>${newBadge}</td>
                    <td class="channel">${v.channel_name}</td>
                    <td class="views">${(v.views || 0).toLocaleString()}</td>
                    <td class="channel" style="font-size:0.8rem">${dateCol}</td>
                    ${actionHtml}
                `;
                tbody.appendChild(tr);
            });
        }

        function setCandidateSort(mode) {
            candidateSort = mode;
            document.getElementById('sort-quality').classList.toggle('active', mode === 'quality');
            document.getElementById('sort-trending').classList.toggle('active', mode === 'trending');
            renderTable('candidates', data.candidates);
        }

        function renderTranscribeBanner() {
            // Feature #2: persistent result of the last Process Queue run. Stays
            // visible (across reloads) until new candidates are queued, which the
            // server clears on /api/request_transcribe.
            const el = document.getElementById('transcribe-banner');
            if (!el) return;
            const st = data.transcribeStatus || {};
            if (!st.status) { el.style.display = 'none'; return; }
            let msg, bd;
            if (st.status === 'error') {
                msg = '⚠️ Last transcription run failed'
                    + (st.error ? ': ' + st.error : '')
                    + (st.date ? ' (' + st.date + ')' : '')
                    + '. Queue new candidates to clear this.';
                el.style.background = 'rgba(240,170,0,0.12)';
                bd = 'var(--warning)';
            } else {
                // Keep terminal (no captions) separate from retryable (blocked) so
                // a P1-retryable video is never presented as a permanent failure.
                const parts = [];
                if (st.unavailable > 0) parts.push(st.unavailable + ' had no captions');
                if (st.retryable > 0) parts.push(st.retryable + ' blocked (can retry)');
                msg = '✅ Last transcription: ' + (st.ok || 0) + ' of ' + (st.total || 0)
                    + ' transcribed' + (st.date ? ' on ' + st.date : '')
                    + (parts.length ? ' — ' + parts.join(', ') : '')
                    + '. Queue new candidates to clear this.';
                el.style.background = 'rgba(80,200,120,0.12)';
                bd = 'rgba(80,200,120,0.6)';
            }
            el.innerHTML = msg;
            el.style.borderColor = bd;
            el.style.color = 'var(--text-main)';
            el.style.display = 'block';
        }

        function renderDiscovery() {
            const d = data.discovery || {};
            // Emerging videos
            const tb = document.querySelector('#table-emerging tbody');
            tb.innerHTML = (d.emerging || []).map(v => `
                <tr><td class="score">${(v.quality_score||0).toFixed(2)}</td>
                <td><a href="https://youtube.com/watch?v=${v.id}" target="_blank">${v.video_title}</a></td>
                <td class="channel">${v.channel_name}</td>
                <td class="views">${(v.views_per_day||0).toLocaleString()}/day</td>
                <td><button class="btn btn-mark" onclick="postAction('/api/request_transcribe','${v.id}')">Transcribe</button></td></tr>
            `).join('') || '<tr><td colspan="5" style="color:var(--text-dim)">No new-channel videos yet. Run discovery to populate.</td></tr>';

            // Suggested channels
            const sc = document.getElementById('suggested-channels');
            sc.innerHTML = (d.suggested_channels || []).map(c => `
                <div class="intel-card" style="display:flex;justify-content:space-between;align-items:center;padding:14px 20px">
                    <div><a href="${c.channel_url||'#'}" target="_blank">${c.channel_name}</a>
                    <span style="color:var(--text-dim);font-size:0.85rem"> — best ${(c.best_score||0).toFixed(2)}, ${c.video_count} videos</span></div>
                    <button class="btn btn-primary" onclick="promoteChannel('${c.channel_id}')">+ Monitor</button>
                </div>
            `).join('') || '<div style="color:var(--text-dim)">No channel suggestions yet — they appear once a new channel lands ≥2 strong videos.</div>';

            // Suggested terms
            const st = document.getElementById('suggested-terms');
            st.innerHTML = (d.suggested_terms || []).map(t => `
                <div class="intel-card" style="display:flex;justify-content:space-between;align-items:center;padding:12px 20px">
                    <div>“${t.term}”</div>
                    <div><button class="btn btn-mark" onclick="acceptTerm('${t.term.replace(/'/g,"\\'")}')">Accept</button>
                    &nbsp;<button class="btn btn-unreq" onclick="rejectTerm('${t.term.replace(/'/g,"\\'")}')">Dismiss</button></div>
                </div>
            `).join('') || '<div style="color:var(--text-dim)">No pending terms. Click “Generate from top content”.</div>';
        }

        async function promoteChannel(cid) {
            await fetch('/api/promote_channel', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({channel_id: cid})});
            fetchData();
        }
        async function acceptTerm(term) {
            await fetch('/api/accept_term', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({term})});
            fetchData();
        }
        async function rejectTerm(term) {
            await fetch('/api/reject_term', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({term})});
            fetchData();
        }
        async function suggestTerms() {
            const s = document.getElementById('terms-status');
            s.innerText = 'Generating…';
            await fetch('/api/suggest_terms', {method:'POST'});
            setTimeout(() => { fetchData(); s.innerText = 'Done — review below.'; }, 4000);
        }

        let _discoveryTimer = null;
        let _logTimer = null;
        let _lastLogLine = 0;
        let _logOffset = 0;

        function toggleLogPanel() {
            const p = document.getElementById('discovery-log-panel');
            p.style.display = p.style.display === 'none' ? 'block' : 'none';
        }

        async function pollDiscoveryLog(scrollToBottom) {
            try {
                const res = await fetch(`/api/job_log?name=podcast_scraper&lines=80&offset=${_logOffset}`);
                const d = await res.json();
                if (!d.exists) return;
                if (d.total_lines === _lastLogLine) return;  // no new lines
                _lastLogLine = d.total_lines;
                const pre = document.getElementById('discovery-log-lines');
                pre.textContent = d.lines.join('\\n');
                if (scrollToBottom) pre.scrollTop = pre.scrollHeight;
                // Parse lines for a live progress summary
                const all = d.lines;
                let query = '', totalUnique = 0, newCount = '', filtered = '', enriching = '', scoreCount = 0;
                for (const line of all) {
                    const mSearch = line.match(/^Searching: (.+)/);
                    if (mSearch) query = mSearch[1].length > 40 ? mSearch[1].slice(0, 40) + '…' : mSearch[1];
                    const mUnique = line.match(/total unique: (\\d+)/);
                    if (mUnique) totalUnique = parseInt(mUnique[1]);
                    const mEnrich = line.match(/Enriching (\\d+\\/(\\d+))/);
                    if (mEnrich) enriching = mEnrich[1];
                    if (line.match(/^  Score:/)) scoreCount++;
                    const mNew = line.match(/New videos this run: (\\d+)/);
                    if (mNew) newCount = mNew[1];
                    const mFilter = line.match(/After filtering[^:]*:\\s*(\\d+)/);
                    if (mFilter) filtered = mFilter[1];
                }
                let summary = '';
                if (newCount) summary = `${newCount} new videos saved`;
                else if (scoreCount > 0 && filtered) summary = `Scoring ${scoreCount}/${filtered}…`;
                else if (scoreCount > 0) summary = `Scoring ${scoreCount} videos…`;
                else if (filtered) summary = `${filtered} kept after filters`;
                else if (enriching) summary = `Enriching ${enriching} videos…`;
                else if (totalUnique) summary = `${totalUnique} unique found` + (query ? ` · ${query}` : '');
                else if (query) summary = `Searching: ${query}`;
                if (summary) {
                    document.querySelectorAll('.discovery-status').forEach(s => s.innerText = summary);
                    document.getElementById('discovery-log-status').innerText = summary;
                }
            } catch(e) {}
        }

        async function checkDiscoveryDone(spans) {
            try {
                const res = await fetch('/api/job_status?name=podcast_scraper');
                const d = await res.json();
                if (!d.running) {
                    clearInterval(_logTimer);
                    _logTimer = null;
                    // Wait briefly for any final buffered output to reach disk, then read twice
                    await new Promise(r => setTimeout(r, 500));
                    await pollDiscoveryLog(true);
                    await new Promise(r => setTimeout(r, 500));
                    await pollDiscoveryLog(true);
                    setDiscoveryButtons(false);
                    spans.forEach(s => s.innerText = 'Done — results updated.');
                    const logStatus = document.getElementById('discovery-log-status');
                    logStatus.innerText = '✓ Complete';
                    logStatus.style.color = 'var(--accent)';
                    fetchData();
                    // Auto-collapse after 8s
                    setTimeout(() => {
                        document.getElementById('discovery-log-panel').style.display = 'none';
                        logStatus.style.color = '';
                    }, 8000);
                }
            } catch(e) {}
        }

        function setDiscoveryButtons(disabled) {
            document.querySelectorAll('button[onclick="runDiscovery()"]').forEach(b => {
                b.disabled = disabled;
                b.style.opacity = disabled ? '0.5' : '';
                b.style.cursor = disabled ? 'not-allowed' : '';
            });
        }

        function startDiscoveryPolling(spans) {
            if (_logTimer) clearInterval(_logTimer);
            if (_discoveryTimer) clearInterval(_discoveryTimer);
            setDiscoveryButtons(true);
            _logTimer = setInterval(async () => {
                await pollDiscoveryLog(true);
                await checkDiscoveryDone(spans);
            }, 2000);
            _discoveryTimer = setInterval(() => fetchData(), 30000);
            // Safety stop after 45 min
            setTimeout(() => {
                if (_logTimer) { clearInterval(_logTimer); _logTimer = null; }
                if (_discoveryTimer) { clearInterval(_discoveryTimer); _discoveryTimer = null; }
                setDiscoveryButtons(false);
                spans.forEach(s => s.innerText = 'Timed out — check log.');
            }, 45 * 60 * 1000);
        }

        async function runDiscovery() {
            const spans = document.querySelectorAll('.discovery-status');
            spans.forEach(s => s.innerText = 'Starting…');
            setDiscoveryButtons(true);

            // Snapshot current log size so we only show output from THIS run
            try {
                const snap = await fetch('/api/job_log?name=podcast_scraper&lines=1');
                const sd = await snap.json();
                _logOffset = sd.total_lines || 0;
            } catch(e) { _logOffset = 0; }
            _lastLogLine = _logOffset;

            // Show log panel
            const panel = document.getElementById('discovery-log-panel');
            panel.style.display = 'block';
            document.getElementById('discovery-log-lines').textContent = '';
            document.getElementById('discovery-log-status').innerText = 'Starting…';

            try {
                const res = await fetch('/api/run_discovery', { method: 'POST' });
                const d = await res.json();
                spans.forEach(s => s.innerText = d.message || 'Running…');

                if (!d.started) {
                    document.getElementById('discovery-log-status').innerText = d.message;
                    document.getElementById('discovery-log-lines').textContent = d.message;
                    setDiscoveryButtons(false);
                    return;
                }

                startDiscoveryPolling(spans);

            } catch (err) {
                spans.forEach(s => s.innerText = 'Failed to start: ' + err);
                document.getElementById('discovery-log-status').innerText = 'Error';
                setDiscoveryButtons(false);
            }
        }

        async function checkDiscoveryOnLoad() {
            try {
                const res = await fetch('/api/job_status?name=podcast_scraper');
                const d = await res.json();
                if (d.running) {
                    const spans = document.querySelectorAll('.discovery-status');
                    spans.forEach(s => s.innerText = 'Discovery already running…');
                    _logOffset = 0; _lastLogLine = 0;
                    document.getElementById('discovery-log-panel').style.display = 'block';
                    document.getElementById('discovery-log-status').innerText = 'Running…';
                    startDiscoveryPolling(spans);
                }
            } catch(e) {}
        }

        function renderStats() {
            const s = data.stats;
            const container = document.getElementById('stats-summary');
            const statuses = [
                { key: 'total', label: 'Total', color: 'var(--text-main)' },
                { key: 'not_requested', label: 'Candidates', color: 'var(--accent)' },
                { key: 'requested', label: 'Requested', color: 'var(--warning)' },
                { key: 'obtained', label: 'Obtained', color: 'var(--success)' },
                { key: 'not_available', label: 'N/A', color: 'var(--text-dim)' },
                { key: 'error', label: 'Error', color: 'var(--error)' }
            ];
            container.innerHTML = statuses.map(st => `
                <div class="stat-card">
                    <div class="stat-label">${st.label}</div>
                    <div class="stat-value" style="color: ${st.color}">${s[st.key] || 0}</div>
                </div>
            `).join('');

            const chart = document.getElementById('channel-chart');
            chart.innerHTML = '';
            if (s.channels && s.channels.length > 0) {
                const max = Math.max(...s.channels.map(c => c.count));
                s.channels.forEach(c => {
                    const pct = (c.count / max) * 100;
                    const row = document.createElement('div');
                    row.className = 'bar-row';
                    row.innerHTML = `
                        <div class="bar-label" title="${c.name}">${c.name}</div>
                        <div class="bar-wrapper"><div class="bar-fill" style="width: ${pct}%"></div></div>
                        <div class="bar-count">${c.count}</div>
                    `;
                    chart.appendChild(row);
                });
            }
        }

        function showTab(tabId, btn) {
            document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
            document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
            document.getElementById(tabId).classList.add('active');
            if (btn) btn.classList.add('active');
        }

        function filterTable(type) {
            const query = document.querySelector(`#${type} .search-box`).value.toLowerCase();
            const rows = document.querySelectorAll(`#table-${type} tbody tr`);
            rows.forEach(row => {
                row.style.display = row.innerText.toLowerCase().includes(query) ? '' : 'none';
            });
        }

        async function postAction(url, id) {
            try {
                const res = await fetch(url, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id })
                });
                if (res.ok) fetchData();
            } catch (err) {
                console.error('Action error:', err);
            }
        }

        async function dismissVideo(id, btn) {
            const row = btn.closest('tr');
            if (row) { row.style.opacity = '0.3'; row.style.pointerEvents = 'none'; }
            try {
                await fetch('/api/dismiss_video', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ id })
                });
                if (row) row.remove();
            } catch (err) {
                if (row) { row.style.opacity = ''; row.style.pointerEvents = ''; }
            }
        }

        let _queueLogOffset = 0;
        let _queuePollTimer = null;

        async function pollQueueLog() {
            try {
                const res = await fetch(`/api/job_log?name=fetch_transcripts&lines=100&offset=${_queueLogOffset}`);
                const d = await res.json();
                if (!d.exists) return;
                const lines = d.lines || [];
                const status = document.getElementById('queue-status');
                let total = 0, current = 0, currentTitle = '';
                for (const line of lines) {
                    const mTotal = line.match(/^Processing (\\d+) videos/);
                    if (mTotal) total = parseInt(mTotal[1]);
                    const mFetch = line.match(/^\\[(\\d+)\\/(\\d+)\\] Fetching: (.+)/);
                    if (mFetch) {
                        current = parseInt(mFetch[1]);
                        total = parseInt(mFetch[2]);
                        currentTitle = mFetch[3].trim();
                    }
                    const mDone = line.match(/^--- Done: (\\d+)\\/(\\d+) transcribed ---/);
                    if (mDone) {
                        const ok = parseInt(mDone[1]), n = parseInt(mDone[2]);
                        status.innerText = `Processing complete — ${ok} of ${n} videos transcribed`;
                        clearInterval(_queuePollTimer);
                        setJobBtn('queue-btn', false);  // re-enable on completion
                        fetchData();
                        return;
                    }
                }
                if (current && total) {
                    const titleStr = currentTitle ? ` — ${currentTitle}` : '';
                    status.innerText = `Processing ${current} of ${total}${titleStr}`;
                } else if (total) {
                    status.innerText = `Processing 0 of ${total}…`;
                }
            } catch (e) { /* ignore transient errors */ }
        }

        async function processQueue() {
            const status = document.getElementById('queue-status');
            status.innerText = 'Starting…';
            setJobBtn('queue-btn', true);  // disable on click (layer 1 of the guard)
            if (_queuePollTimer) { clearInterval(_queuePollTimer); _queuePollTimer = null; }
            try {
                // Snapshot log position before launch so we only see new output
                try {
                    const snap = await fetch('/api/job_log?name=fetch_transcripts&lines=1');
                    const sd = await snap.json();
                    _queueLogOffset = sd.total_lines || 0;
                } catch(e) { _queueLogOffset = 0; }

                const res = await fetch('/api/process_queue', { method: 'POST' });
                const data = await res.json();
                if (!data.started) {
                    status.innerText = data.message || '';
                    if (!data.running) setJobBtn('queue-btn', false);  // nothing pending — re-enable
                    return;
                }
                status.innerText = `Processing 0 of ${data.queued}…`;
                _queuePollTimer = setInterval(pollQueueLog, 2000);
            } catch (err) {
                status.innerText = 'Failed to start: ' + err;
                setJobBtn('queue-btn', false);
            }
        }

        async function viewTranscript(id) {
            const modal = document.getElementById('transcript-modal');
            const body = document.getElementById('modal-body');
            const title = document.getElementById('modal-title');
            const sub = document.getElementById('modal-sub');
            title.innerText = 'Transcript';
            sub.innerText = '';
            body.innerText = 'Loading…';
            modal.classList.add('active');
            try {
                const res = await fetch('/api/transcript/' + encodeURIComponent(id));
                const data = await res.json();
                if (data.full_text) {
                    title.innerText = data.video_title || 'Transcript';
                    sub.innerText = (data.channel_name || '') + ' · ' + (data.word_count || 0).toLocaleString() + ' words';
                    body.innerText = data.full_text;
                } else {
                    body.innerText = 'No transcript stored for this video yet.';
                }
            } catch (err) {
                body.innerText = 'Error loading transcript: ' + err;
            }
        }

        function closeModal() {
            document.getElementById('transcript-modal').classList.remove('active');
        }
        document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

        function fmtTime(sec) {
            sec = parseInt(sec || 0);
            const h = Math.floor(sec/3600), m = Math.floor((sec%3600)/60), s = sec%60;
            const mm = (h ? String(m).padStart(2,'0') : m);
            return (h ? h+':' : '') + mm + ':' + String(s).padStart(2,'0');
        }

        function renderIntelligence() {
            const el = document.getElementById('intel-list');
            const items = data.intelligence || [];
            if (!items.length) {
                el.innerHTML = '<div class="intel-card" style="color:var(--text-dim)">No analyzed videos yet. Transcribe some candidates, then click “Analyze Transcripts”.</div>';
                return;
            }
            el.innerHTML = items.map(v => {
                const isLit = v.source_type === 'literature';
                const docUrl = v.url || ('https://youtube.com/watch?v=' + v.id);
                const ents = (v.seo_entities||[]).map(e => `<span class="chip">${e}</span>`).join('');
                const geos = (v.geo_signals||[]).map(g => `<span class="chip chip-geo">${g}</span>`).join('');
                const kps = (v.key_points||[]).map(k => {
                    const cat = k.category ? ` <span class="kp-cat">(${k.category})</span>` : '';
                    if (isLit) return `<li>${k.point_text}${cat}</li>`;  // papers: no timestamps
                    const url = `https://youtube.com/watch?v=${v.id}&t=${parseInt(k.timestamp_sec||0)}s`;
                    return `<li><a class="kp-time" href="${url}" target="_blank">${fmtTime(k.timestamp_sec)}</a>${k.point_text}${cat}</li>`;
                }).join('');
                const reach = isLit ? `${(v.citations||0).toLocaleString()} citations` : `${(v.views||0).toLocaleString()} views`;
                return `<div class="intel-card">
                    <h3>${isLit?'📄 ':''}<a href="${docUrl}" target="_blank">${v.video_title}</a></h3>
                    <div class="intel-meta">${v.channel_name} · ${reach} · score ${(v.quality_score||0).toFixed(2)}</div>
                    ${v.best_quote ? `<div class="intel-quote">“${v.best_quote}”</div>` : ''}
                    <div>${ents}${geos}</div>
                    ${kps ? `<ul class="kp-list">${kps}</ul>` : ''}
                </div>`;
            }).join('');
        }

        let _analyzeLogOffset = 0;
        let _analyzePollTimer = null;

        async function pollAnalyzeLog() {
            try {
                const res = await fetch(`/api/job_log?name=analyze_transcripts&lines=100&offset=${_analyzeLogOffset}`);
                const d = await res.json();
                if (!d.exists) return;
                const lines = d.lines || [];
                const s = document.getElementById('analyze-status');
                let total = 0, current = 0, currentTitle = '';
                for (const line of lines) {
                    const mTotal = line.match(/^Analyzing (\\d+) transcript/);
                    if (mTotal) total = parseInt(mTotal[1]);
                    const mItem = line.match(/^\\[(\\d+)\\/(\\d+)\\] Analyzing: (.+)/);
                    if (mItem) {
                        current = parseInt(mItem[1]);
                        total = parseInt(mItem[2]);
                        currentTitle = mItem[3].trim();
                    }
                    const mDone = line.match(/^--- Done: (\\d+)\\/(\\d+) analyzed ---/);
                    if (mDone) {
                        const ok = parseInt(mDone[1]), n = parseInt(mDone[2]);
                        s.innerText = `Analysis complete — ${ok} of ${n} transcripts analyzed`;
                        clearInterval(_analyzePollTimer);
                        setJobBtn('analyze-btn', false);  // re-enable on completion
                        fetchData();
                        return;
                    }
                }
                if (current && total) {
                    const titleStr = currentTitle ? ` — ${currentTitle}` : '';
                    s.innerText = `Analyzing ${current} of ${total}${titleStr}`;
                } else if (total) {
                    s.innerText = `Analyzing 0 of ${total}…`;
                }
            } catch (e) { /* ignore transient errors */ }
        }

        async function runAnalyze() {
            const s = document.getElementById('analyze-status');
            s.innerText = 'Starting…';
            setJobBtn('analyze-btn', true);  // disable on click (layer 1 of the guard)
            if (_analyzePollTimer) { clearInterval(_analyzePollTimer); _analyzePollTimer = null; }
            try {
                try {
                    const snap = await fetch('/api/job_log?name=analyze_transcripts&lines=1');
                    const sd = await snap.json();
                    _analyzeLogOffset = sd.total_lines || 0;
                } catch(e) { _analyzeLogOffset = 0; }

                const res = await fetch('/api/analyze', { method: 'POST' });
                const d = await res.json();
                if (!d.started) {
                    s.innerText = d.message || '';
                    if (!d.running) setJobBtn('analyze-btn', false);
                    return;
                }
                s.innerText = `Analyzing 0 of ${d.queued}…`;
                _analyzePollTimer = setInterval(pollAnalyzeLog, 2000);
            } catch (err) { s.innerText = 'Failed: ' + err; setJobBtn('analyze-btn', false); }
        }

        // ── Background-job button guard (CLAUDE.md three-layer rule) ──────────
        // Layer 1: disable on click (in each handler). Layer 2: server-side
        // already-running guard (returns started:false, running:true). Layer 3:
        // on page load, re-disable any button whose job is still running.
        function setJobBtn(id, disabled) {
            const b = document.getElementById(id);
            if (b) b.disabled = disabled;
        }

        function watchJobButton(statusName, btnId, onDone) {
            setJobBtn(btnId, true);
            const t = setInterval(async () => {
                try {
                    const r = await fetch('/api/job_status?name=' + statusName);
                    const d = await r.json();
                    if (!d.running) {
                        clearInterval(t);
                        setJobBtn(btnId, false);
                        if (onDone) onDone();
                    }
                } catch (e) {}
            }, 2000);
            return t;
        }

        async function checkJobButtonsOnLoad() {
            const jobs = [
                ['fetch_transcripts', 'queue-btn', null],
                ['analyze_transcripts', 'analyze-btn', () => fetchData()],
                ['generate_digest', 'digest-btn', () => { loadDigest(); loadDigestPending(); }],
                ['generate_report', 'report-btn', () => loadReport()],
            ];
            for (const [name, btn, onDone] of jobs) {
                try {
                    const r = await fetch('/api/job_status?name=' + name);
                    const d = await r.json();
                    if (d.running) watchJobButton(name, btn, onDone);
                } catch (e) {}
            }
        }

        async function generateDigest() {
            const s = document.getElementById('digest-status');
            s.innerText = 'Generating…';
            setJobBtn('digest-btn', true);  // disable on click
            try {
                const res = await fetch('/api/generate_digest', { method: 'POST' });
                const d = await res.json();
                if (d && d.started === false) {
                    s.innerText = d.message || '';
                    if (!d.running) setJobBtn('digest-btn', false);
                    return;
                }
                s.innerText = 'Generating…';
                // Re-enable + refresh when the digest subprocess actually finishes.
                watchJobButton('generate_digest', 'digest-btn', () => {
                    s.innerText = 'Done.'; loadDigest(); loadDigestPending();
                });
            } catch (err) { s.innerText = 'Failed: ' + err; setJobBtn('digest-btn', false); }
        }

        async function loadDigestPending() {
            try {
                const res = await fetch('/api/digest_pending');
                const d = await res.json();
                const el = document.getElementById('digest-pending');
                if (el) el.innerText = d.count > 0 ? d.count + ' new since last digest' : 'All caught up';
            } catch(e) {}
        }

        let _digestListOpen = false;

        async function toggleDigestList(btn) {
            _digestListOpen = !_digestListOpen;
            document.getElementById('digest-list-panel').style.display = _digestListOpen ? '' : 'none';
            if (btn) btn.style.background = _digestListOpen ? 'var(--accent)' : '';
            if (_digestListOpen) await refreshDigestList();
        }

        async function refreshDigestList() {
            const el = document.getElementById('digest-list-rows');
            el.innerText = 'Loading…';
            try {
                const res = await fetch('/api/digest_list');
                const d = await res.json();
                const items = d.digests || [];
                if (!items.length) { el.innerText = 'No saved digests.'; return; }
                el.innerHTML = items.map(dg => `
                    <div style="display:flex;align-items:center;gap:10px;padding:8px 0;border-bottom:1px solid var(--border)">
                        <span style="flex:1;font-weight:500">${dg.label}</span>
                        <span style="color:var(--text-dim);font-size:0.8rem">${dg.size_kb} KB</span>
                        <button class="btn btn-view" onclick="viewDigestFile('${dg.filename}')">View</button>
                        <button class="btn" style="background:#c0392b;color:#fff;border:none" onclick="deleteDigest('${dg.filename}')">Delete</button>
                    </div>`).join('');
            } catch(e) { el.innerText = 'Error loading list.'; }
        }

        async function viewDigestFile(filename) {
            const el = document.getElementById('digest-content');
            el.innerText = 'Loading…';
            _digestListOpen = false;
            document.getElementById('digest-list-panel').style.display = 'none';
            document.querySelectorAll('#digest .btn').forEach(b => b.style.background = '');
            try {
                const res = await fetch('/api/digest_file?name=' + encodeURIComponent(filename));
                const d = await res.json();
                el.innerHTML = d.markdown ? renderMd(d.markdown) : 'Empty digest.';
            } catch(e) { el.innerText = 'Error loading digest.'; }
        }

        async function deleteDigest(filename) {
            if (!confirm('Delete this digest? This cannot be undone.')) return;
            try {
                await fetch('/api/delete_digest', {
                    method: 'POST', headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({filename})
                });
                await refreshDigestList();
                loadDigest();
            } catch(e) { alert('Error deleting digest.'); }
        }

        function renderMd(md) {
            return (md || '')
                .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;')
                .replace(/^### (.*)$/gm,'<h4 style="margin:12px 0 4px">$1</h4>')
                .replace(/^## (.*)$/gm,'<h3 style="margin:16px 0 6px;color:var(--accent)">$1</h3>')
                .replace(/^# (.*)$/gm,'<h2 style="margin:0 0 8px">$1</h2>')
                .replace(/^&gt; (.*)$/gm,'<blockquote style="border-left:3px solid var(--accent);margin:6px 0;padding:4px 12px;color:var(--text-dim)">$1</blockquote>')
                .replace(/\\*\\*(.+?)\\*\\*/g,'<strong>$1</strong>')
                .replace(/\\[([^\\]]+)\\]\\((https?:[^)]+)\\)/g,'<a href="$2" target="_blank">$1</a>');
        }

        async function loadDigest() {
            try {
                const res = await fetch('/api/digest');
                const d = await res.json();
                document.getElementById('digest-content').innerHTML = d.markdown ? renderMd(d.markdown) : 'No digest yet. Click “Generate Weekly Digest”.';
            } catch (err) {
                document.getElementById('digest-content').innerText = 'Error loading digest: ' + err;
            }
        }
        loadDigest();
        loadDigestPending();

        async function loadReport() {
            try {
                const res = await fetch('/api/report');
                const d = await res.json();
                document.getElementById('report-content').innerHTML = d.markdown ? renderMd(d.markdown) : 'No report yet. Choose how many transcripts and click “Generate Advisor Report”.';
            } catch (err) {
                document.getElementById('report-content').innerText = 'Error loading report: ' + err;
            }
        }
        loadReport();
        loadReportChannels();

        let _selectedChannels = [];
        let _reportChannels = [];
        let _reportCountTimer = null;

        async function loadReportChannels() {
            await refreshReportChannelList();
        }

        async function refreshReportChannelList() {
            const date_from = document.getElementById('report-from').value;
            const date_to   = document.getElementById('report-to').value;
            const params = new URLSearchParams();
            if (date_from) params.set('from', date_from);
            if (date_to)   params.set('to', date_to);
            try {
                const res = await fetch('/api/report_channels_with_counts?' + params);
                const d = await res.json();
                const list = document.getElementById('report-channel-list');
                if (!list) return;
                const channels = d.channels || [];
                if (!channels.length) {
                    list.innerHTML = '<span style="color:var(--text-dim);font-size:0.85rem">No analyzed transcripts yet.</span>';
                    return;
                }
                _reportChannels = channels;
                const esc = s => String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
                list.innerHTML = channels.map((ch, i) => {
                    const sel     = _selectedChannels.includes(ch.name);
                    const dimmed  = !sel && ch.count === 0;
                    const bg      = sel ? '#2980b9' : 'var(--card-bg)';
                    const border  = sel ? '#2980b9' : 'var(--border)';
                    const color   = sel ? '#fff' : (dimmed ? 'var(--text-dim)' : 'var(--text-main)');
                    const label   = ch.count === ch.total
                        ? ch.count
                        : `${ch.count}<span style="opacity:0.5">/${ch.total}</span>`;
                    return `<div data-idx="${i}" class="report-channel-chip" style="cursor:pointer;padding:4px 10px;border-radius:14px;font-size:0.82rem;display:flex;gap:6px;align-items:center;border:1px solid ${border};background:${bg};color:${color};white-space:nowrap;opacity:${dimmed ? '0.45' : '1'}">
                        <span>${esc(ch.name)}</span><span style="font-size:0.75rem">${label}</span>
                    </div>`;
                }).join('');
            } catch(e) {}
            updateReportCount();
        }

        function selectReportChannel(name) {
            const i = _selectedChannels.indexOf(name);
            if (i === -1) _selectedChannels.push(name);
            else _selectedChannels.splice(i, 1);
            refreshReportChannelList();
        }

        // Delegated click — robust against quotes/apostrophes in channel names.
        document.getElementById('report-channel-list').addEventListener('click', (e) => {
            const chip = e.target.closest('.report-channel-chip');
            if (!chip) return;
            const ch = _reportChannels[parseInt(chip.dataset.idx, 10)];
            if (ch) selectReportChannel(ch.name);
        });

        async function updateReportCount() {
            const date_from = document.getElementById('report-from').value;
            const date_to   = document.getElementById('report-to').value;
            const n = parseInt(document.getElementById('report-n').value) || 8;
            const el = document.getElementById('report-source-count');
            if (!el) return;
            const params = new URLSearchParams();
            if (date_from)      params.set('from', date_from);
            if (date_to)        params.set('to', date_to);
            _selectedChannels.forEach(c => params.append('channel', c));
            try {
                const res = await fetch('/api/report_source_count?' + params);
                const d = await res.json();
                const count = d.count || 0;
                const capped = Math.min(count, n);
                if (count === 0) {
                    el.style.color = 'var(--error,#c0392b)';
                    el.innerText = 'No sources match';
                } else if (count > n) {
                    el.style.color = 'var(--text-dim)';
                    el.innerText = capped + ' of ' + count + ' sources';
                } else {
                    el.style.color = 'var(--success,#27ae60)';
                    el.innerText = count + ' source' + (count === 1 ? '' : 's');
                }
            } catch(e) {}
        }

        function scheduleReportRefresh() {
            clearTimeout(_reportCountTimer);
            _reportCountTimer = setTimeout(refreshReportChannelList, 300);
        }

        document.getElementById('report-from').addEventListener('change', scheduleReportRefresh);
        document.getElementById('report-to').addEventListener('change', scheduleReportRefresh);
        document.getElementById('report-n').addEventListener('input', updateReportCount);

        async function generateReport() {
            const s = document.getElementById('report-status');
            const n = parseInt(document.getElementById('report-n').value) || 8;
            const date_from = document.getElementById('report-from').value;
            const date_to   = document.getElementById('report-to').value;
            const channels  = _selectedChannels.slice();
            s.innerText = 'Generating…';
            setJobBtn('report-btn', true);  // disable on click
            try {
                const res = await fetch('/api/generate_report', {
                    method:'POST', headers:{'Content-Type':'application/json'},
                    body: JSON.stringify({n, date_from, date_to, channels})
                });
                const d = await res.json();
                if (d && d.started === false) {
                    s.innerText = d.message || '';
                    if (!d.running) setJobBtn('report-btn', false);
                    return;
                }
                // Re-enable + reload when the report subprocess actually finishes.
                watchJobButton('generate_report', 'report-btn', () => {
                    s.innerText = 'Done.'; loadReport();
                });
            } catch (err) { s.innerText = 'Failed: ' + err; setJobBtn('report-btn', false); }
        }

        let activeSettings = {};
        let activeProfileName = 'topic';
        async function loadProfiles() {
            try {
                const res = await fetch('/api/profiles');
                const d = await res.json();
                activeSettings = d.settings || {};
                activeProfileName = d.active || activeProfileName;
                const sel = document.getElementById('profile-select');
                sel.innerHTML = (d.profiles || []).map(p =>
                    `<option value="${p.name}" ${p.active?'selected':''}>${p.label} (${p.queries}q/${p.channels}ch)</option>`
                ).join('');
                document.getElementById('settings-profile').innerText = d.active || '';
            } catch (err) { console.error('profiles', err); }
        }

        // Save-as-PDF: the browser uses document.title as the default PDF filename.
        function printDoc(kind) {
            const date = new Date().toISOString().slice(0, 10);
            const safe = (activeProfileName || 'topic').replace(/[^a-z0-9-]+/gi, '-');
            const fname = `${kind.replace(/ /g, '-')}_${safe}_${date}`;
            const prevTitle = document.title;
            document.title = fname;
            document.getElementById('print-header').innerText = `${kind} · ${activeProfileName} · ${date}`;
            const restore = () => { document.title = prevTitle; window.removeEventListener('afterprint', restore); };
            window.addEventListener('afterprint', restore);
            window.print();
        }

        function applyTheme(t) { document.body.classList.toggle('dark', t === 'dark'); }
        function toggleTheme() {
            const t = document.body.classList.contains('dark') ? 'light' : 'dark';
            localStorage.setItem('pt_theme', t);
            applyTheme(t);
        }
        applyTheme(localStorage.getItem('pt_theme') || 'light');

        let llmProvidersData = { providers: [], bulk: {}, synth: {} };
        let llmModelCache = {};  // pid -> [model, ...]
        let llmThinking = { bulk: 'low', synth: 'medium' };

        async function openSettings() {
            const f = ['min_views','min_publish_date','min_duration_sec','max_duration_sec','min_days_old','max_videos_per_channel','enrich_recent_days','enrich_recent_hours','enrich_older_days'];
            f.forEach(k => { const el = document.getElementById('set-'+k); if (el) el.value = activeSettings[k] ?? ''; });
            document.getElementById('set-status').innerText = '';
            document.getElementById('settings-modal').classList.add('active');
            await reloadProviders();
        }
        function closeSettings() { document.getElementById('settings-modal').classList.remove('active'); }

        async function reloadProviders() {
            try {
                const res = await fetch('/api/llm_providers');
                llmProvidersData = await res.json();
                renderProviderList();
                renderProviderSelects();
            } catch(e) { console.error('llm_providers', e); }
        }

        function renderProviderList() {
            const el = document.getElementById('llm-providers-list');
            if (!llmProvidersData.providers.length) {
                el.innerHTML = '<div style="color:var(--text-dim);font-size:0.85rem">No providers configured. Add one above.</div>';
                return;
            }
            el.innerHTML = llmProvidersData.providers.map(p => `
                <div class="llm-provider-row">
                    <span class="prov-label">${p.label}</span>
                    <span class="prov-base" title="${p.base}">${p.base}</span>
                    <span class="prov-hint">${p.key_set ? p.key_hint : '⚠ no key'}</span>
                    <button class="icon-btn" onclick="openEditProvider('${p.id}')" title="Edit">✏</button>
                    <button class="icon-btn" onclick="testProvider('${p.id}', this)" title="Test">⚡</button>
                    <button class="icon-btn" onclick="deleteProvider('${p.id}')" title="Delete" style="color:#e55">🗑</button>
                </div>
            `).join('');
        }

        function renderProviderSelects() {
            const opts = llmProvidersData.providers.map(p =>
                `<option value="${p.id}">${p.label}</option>`
            ).join('');
            ['bulk','synth'].forEach(role => {
                const sel = document.getElementById(`sel-${role}-provider`);
                if (!sel) return;
                sel.innerHTML = opts;
                const pid = (llmProvidersData[role] || {}).provider_id;
                if (pid) sel.value = pid;
            });
            // Populate model dropdowns from cache or saved selection
            ['bulk','synth'].forEach(role => {
                const pid = document.getElementById(`sel-${role}-provider`).value;
                const saved = (llmProvidersData[role] || {}).model;
                const cached = llmModelCache[pid] || [];
                populateModelSelect(role, cached, saved);
                const thinking = (llmProvidersData[role] || {}).thinking || (role === 'bulk' ? 'low' : 'medium');
                setThinking(role, thinking);
            });
        }

        function populateModelSelect(role, models, current) {
            const sel = document.getElementById(`sel-${role}-model`);
            if (!sel) return;
            const all = models.length ? models : (current ? [current] : []);
            sel.innerHTML = all.map(m => `<option value="${m}" ${m===current?'selected':''}>${m}</option>`).join('');
            if (current && !models.includes(current)) {
                sel.innerHTML = `<option value="${current}" selected>${current}</option>` + sel.innerHTML;
            }
        }

        function setThinking(role, level) {
            llmThinking[role] = level;
            document.querySelectorAll(`#${role}-thinking-btns .thinking-btn`).forEach(b => {
                b.classList.toggle('active', b.dataset.level === level);
            });
        }

        async function onProviderChange(role) {
            const pid = document.getElementById(`sel-${role}-provider`).value;
            if (llmModelCache[pid]) {
                populateModelSelect(role, llmModelCache[pid], null);
            } else {
                await fetchModelsFor(role);
            }
        }

        async function fetchModelsFor(role) {
            const pid = document.getElementById(`sel-${role}-provider`).value;
            if (!pid) return;
            const statusEl = document.getElementById('llm-sel-status');
            if (statusEl) statusEl.innerText = 'Fetching models…';
            try {
                const res = await fetch(`/api/llm_models?provider=${encodeURIComponent(pid)}`);
                const d = await res.json();
                if (d.ok && d.models.length) {
                    llmModelCache[pid] = d.models;
                    const cur = document.getElementById(`sel-${role}-model`).value;
                    populateModelSelect(role, d.models, cur);
                    if (statusEl) statusEl.innerText = `${d.models.length} models loaded`;
                } else {
                    if (statusEl) statusEl.innerText = d.error || 'No models returned';
                }
            } catch(e) {
                if (statusEl) statusEl.innerText = 'Error: ' + e.message;
            }
        }

        async function saveModelSelection() {
            const s = document.getElementById('llm-sel-status');
            const body = {
                bulk: {
                    provider_id: document.getElementById('sel-bulk-provider').value,
                    model: document.getElementById('sel-bulk-model').value,
                    thinking: llmThinking.bulk,
                },
                synth: {
                    provider_id: document.getElementById('sel-synth-provider').value,
                    model: document.getElementById('sel-synth-model').value,
                    thinking: llmThinking.synth,
                },
            };
            s.innerText = 'Saving…';
            try {
                const res = await fetch('/api/llm_selection', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
                const d = await res.json();
                s.innerText = d.ok ? 'Saved.' : ('Failed: ' + (d.error || ''));
            } catch(e) { s.innerText = 'Error: ' + e.message; }
        }

        function openAddProvider() {
            document.getElementById('llm-prov-id').value = '';
            document.getElementById('llm-prov-label').value = '';
            document.getElementById('llm-prov-base').value = 'https://api.openai.com/v1';
            document.getElementById('llm-prov-key').value = '';
            document.getElementById('llm-prov-key-hint').innerText = '';
            document.getElementById('llm-prov-status').innerText = '';
            document.getElementById('llm-prov-modal-title').innerText = 'Add LLM Provider';
            document.getElementById('llm-provider-modal').classList.add('active');
        }

        function openEditProvider(pid) {
            const p = llmProvidersData.providers.find(x => x.id === pid);
            if (!p) return;
            document.getElementById('llm-prov-id').value = pid;
            document.getElementById('llm-prov-label').value = p.label;
            document.getElementById('llm-prov-base').value = p.base;
            document.getElementById('llm-prov-key').value = '';
            document.getElementById('llm-prov-key-hint').innerText = p.key_set ? `(current: ${p.key_hint}, leave blank to keep)` : '(no key set)';
            document.getElementById('llm-prov-status').innerText = '';
            document.getElementById('llm-prov-modal-title').innerText = 'Edit LLM Provider';
            document.getElementById('llm-provider-modal').classList.add('active');
        }

        function closeProviderModal() { document.getElementById('llm-provider-modal').classList.remove('active'); }

        async function saveProvider() {
            const pid = document.getElementById('llm-prov-id').value;
            const body = {
                id: pid,
                label: document.getElementById('llm-prov-label').value.trim(),
                base: document.getElementById('llm-prov-base').value.trim(),
                key: document.getElementById('llm-prov-key').value.trim(),
            };
            const s = document.getElementById('llm-prov-status');
            s.innerText = 'Saving…';
            const url = pid ? '/api/llm_providers/edit' : '/api/llm_providers/add';
            try {
                const res = await fetch(url, {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
                const d = await res.json();
                if (d.ok) {
                    closeProviderModal();
                    await reloadProviders();
                } else { s.innerText = 'Failed: ' + (d.error || ''); }
            } catch(e) { s.innerText = 'Error: ' + e.message; }
        }

        async function deleteProvider(pid) {
            if (!confirm('Delete this provider? Any active model selections using it will be cleared.')) return;
            await fetch('/api/llm_providers/delete', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id: pid})});
            await reloadProviders();
        }

        async function testProvider(pid, btn) {
            btn.disabled = true;
            btn.innerText = '…';
            try {
                const res = await fetch('/api/llm_test', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({provider_id: pid})});
                const d = await res.json();
                btn.innerText = d.ok ? '✓' : '✗';
                btn.title = d.ok ? (`${d.model}: "${d.reply}"`) : ('Error: ' + d.error);
                setTimeout(() => { btn.innerText = '⚡'; btn.disabled = false; btn.title = 'Test'; }, 3000);
            } catch(e) {
                btn.innerText = '✗';
                setTimeout(() => { btn.innerText = '⚡'; btn.disabled = false; }, 3000);
            }
        }

        async function saveSettings() {
            const s = document.getElementById('set-status');
            const body = {};
            ['min_views','min_publish_date','min_duration_sec','max_duration_sec','min_days_old','max_videos_per_channel','enrich_recent_days','enrich_recent_hours','enrich_older_days']
                .forEach(k => { body[k] = document.getElementById('set-'+k).value; });
            s.innerText = 'Saving…';
            const res = await fetch('/api/update_profile', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body)});
            const d = await res.json();
            if (d.ok) { s.innerText = 'Saved. Applies on the next Run Discovery.'; await loadProfiles(); }
            else s.innerText = 'Failed: ' + (d.error||'');
        }

        async function switchProfile(name) {
            await fetch('/api/set_profile', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
            fetchData(); loadDigest();
        }

        function openNewProfile() { document.getElementById('profile-modal').classList.add('active'); }
        function closeProfileModal() { document.getElementById('profile-modal').classList.remove('active'); }

        function collectProfile() {
            const lines = id => document.getElementById(id).value.split('\\n').map(s=>s.trim()).filter(Boolean);
            const channels = {};
            lines('np-channels').forEach(l => { const [h,...n] = l.split('='); if(h&&n.length) channels[h.trim()] = n.join('=').trim(); });
            return {
                name: document.getElementById('np-name').value.trim(),
                label: document.getElementById('np-label').value.trim(),
                search_queries: lines('np-queries'),
                curated_channels: channels,
                keywords: document.getElementById('np-keywords').value.split(',').map(s=>s.trim()).filter(Boolean),
                analysis_focus: document.getElementById('np-focus').value.trim(),
                digest_title: document.getElementById('np-digest').value.trim(),
                youtube_enabled: document.getElementById('np-yt-enabled').checked,
                literature_enabled: document.getElementById('np-lit-enabled').checked,
                literature_queries: lines('np-lit-queries'),
                literature_since: document.getElementById('np-lit-since').value.trim(),
            };
        }

        async function createProfile() {
            const s = document.getElementById('np-status');
            const prof = collectProfile();
            if (!prof.name) { s.innerText = 'Name is required.'; return; }
            s.innerText = 'Creating…';
            const res = await fetch('/api/create_profile', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(prof)});
            const d = await res.json();
            if (d.ok) { await switchProfile(d.name); await loadProfiles(); closeProfileModal(); }
            else s.innerText = 'Failed: ' + (d.error||'unknown');
        }

        async function testNewProfile() {
            const s = document.getElementById('np-status');
            const prof = collectProfile();
            if (!prof.name) { s.innerText = 'Name is required to test.'; return; }
            s.innerText = 'Saving + testing (≈1-2 min, hits YouTube)…';
            // Persist first (so --profile can load it), without switching the active view.
            await fetch('/api/create_profile', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(prof)});
            await loadProfiles();
            const res = await fetch('/api/test_profile', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name: prof.name})});
            const d = await res.json();
            if (!d.ok) { s.innerText = 'Test failed: ' + (d.error||''); return; }
            s.innerText = 'Test complete.';
            const r = d.summary;
            document.getElementById('np-test-result').innerHTML =
                `<div class="intel-card"><b>${r.unique_videos}</b> videos from <b>${r.unique_channels}</b> channels.<br>
                 <span style="color:var(--text-dim)">Top channels:</span> ${(r.top_channels||[]).map(c=>c.name+' ('+c.count+')').join(', ')}<br>
                 <span style="color:var(--text-dim)">Sample:</span> ${(r.sample_titles||[]).slice(0,6).join(' · ')}</div>`;
        }

        loadProfiles();
        fetchData();
        checkDiscoveryOnLoad();
        checkJobButtonsOnLoad();
    </script>
</body>
</html>
"""

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        refresh_active_db()
        p = urllib.parse.urlparse(self.path).path
        if p == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif p == "/api/candidates":
            rows = self.query("SELECT * FROM videos WHERE transcript_status = 'not_requested' AND (dismissed IS NULL OR dismissed = 0) ORDER BY quality_score DESC LIMIT 200")
            # Skip Forever (S1.D): drop any candidate whose title is in the skip
            # file — a safety net for rows that slipped past the discovery guard
            # (e.g. re-discovered under a new id before being re-dismissed).
            skips = skiplist.load_skip_set(DB_PATH)
            if skips:
                rows = [r for r in rows
                        if skiplist.normalize_title(r.get("video_title")) not in skips]
            self.json(rows)
        elif p == "/api/requested":
            self.json(self.query("SELECT * FROM videos WHERE transcript_status = 'requested' ORDER BY quality_score DESC LIMIT 200"))
        elif p == "/api/transcribed":
            sql = """
                SELECT v.*, t.word_count,
                       (SELECT COUNT(*) FROM key_points k WHERE k.video_id = v.id) as key_point_count
                FROM videos v
                LEFT JOIN transcripts t ON v.id = t.video_id
                WHERE v.transcript_status = 'obtained'
                ORDER BY v.transcribed_date DESC
                LIMIT 200
            """
            self.json(self.query(sql))
        elif p == "/api/stats":
            self.json(self.get_stats())
        elif p.startswith("/api/transcript/"):
            video_id = urllib.parse.unquote(p[len("/api/transcript/"):])
            rows = self.query(
                "SELECT t.full_text, t.word_count, v.video_title, v.channel_name "
                "FROM transcripts t JOIN videos v ON v.id = t.video_id WHERE t.video_id = ?",
                (video_id,),
            )
            if rows:
                self.json(rows[0])
            else:
                self.json({"error": "no transcript", "full_text": None})
        elif p == "/api/intelligence":
            self.json(self.get_intelligence())
        elif p == "/api/transcribe_status":
            # Feature #2: last Process Queue outcome — persists until new work is
            # queued. None → no active message.
            self.json(runstate.read_transcribe_result(DB_PATH) or {})
        elif p == "/api/discovery":
            self.json(self.get_discovery())
        elif p == "/api/digest":
            md = ""
            latest = os.path.join(profiles.load()["digest_dir"], "latest.md")
            if os.path.isfile(latest):
                with open(latest, encoding="utf-8") as fh:
                    md = fh.read()
            self.json({"markdown": md})
        elif p == "/api/digest_list":
            import re as _re
            digest_dir = Path(profiles.load()["digest_dir"])
            files = sorted(digest_dir.glob("digest_*.md"), reverse=True)
            result = []
            for f in files:
                stem = f.stem  # e.g. "digest_2026-06-21" or "digest_2026-06-21_2"
                date_part = stem[len("digest_"):]  # "2026-06-21" or "2026-06-21_2"
                # Build a human label
                m = _re.match(r"(\d{4}-\d{2}-\d{2})(?:_(\d+))?$", date_part)
                if m:
                    label = m.group(1) + (f" (run {m.group(2)})" if m.group(2) else "")
                else:
                    label = date_part
                result.append({
                    "filename": f.name,
                    "label": label,
                    "size_kb": round(f.stat().st_size / 1024, 1),
                })
            self.json({"digests": result})
            return
        elif p.startswith("/api/digest_file"):
            import re as _re
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (qs.get("name") or [""])[0]
            if not _re.match(r'^digest_[\w-]+\.md$', name):
                self.send_response(400); self.end_headers(); return
            digest_dir = Path(profiles.load()["digest_dir"])
            fpath = (digest_dir / name).resolve()
            if not str(fpath).startswith(str(digest_dir.resolve())):
                self.send_response(403); self.end_headers(); return
            self.json({"markdown": fpath.read_text(encoding="utf-8") if fpath.exists() else ""})
        elif p == "/api/digest_pending":
            n = self.query(
                "SELECT COUNT(*) AS n FROM videos v "
                "JOIN ai_analysis a ON a.video_id=v.id "
                "WHERE v.transcript_status='obtained' AND v.digested_at IS NULL"
            )[0]["n"]
            self.json({"count": n})
            return
        elif p.startswith("/api/report_source_count"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            date_from = (qs.get("from") or [""])[0]
            date_to   = (qs.get("to")   or [""])[0]
            channels  = [c for c in (qs.get("channel") or []) if c]
            filters = [
                "v.transcript_status='obtained'",
                "t.video_id IS NOT NULL",
                "a.video_id IS NOT NULL",
            ]
            params = []
            if date_from:
                filters.append("v.transcribed_date >= ?"); params.append(date_from)
            if date_to:
                filters.append("v.transcribed_date <= ?"); params.append(date_to)
            if channels:
                ph = ",".join("?" * len(channels))
                filters.append(f"v.channel_name IN ({ph})"); params.extend(channels)
            sql = (
                "SELECT COUNT(*) AS n FROM videos v "
                "JOIN transcripts t ON t.video_id=v.id "
                "JOIN ai_analysis a ON a.video_id=v.id "
                f"WHERE {' AND '.join(filters)}"
            )
            n = self.query(sql, tuple(params))[0]["n"]
            self.json({"count": n})
            return
        elif p == "/api/report_channels":
            rows = self.query(
                "SELECT DISTINCT v.channel_name FROM videos v "
                "JOIN transcripts t ON t.video_id=v.id "
                "JOIN ai_analysis a ON a.video_id=v.id "
                "WHERE v.transcript_status='obtained' "
                "ORDER BY v.channel_name"
            )
            self.json({"channels": [r["channel_name"] for r in rows if r["channel_name"]]})
            return
        elif p.startswith("/api/report_channels_with_counts"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            date_from = (qs.get("from") or [""])[0]
            date_to   = (qs.get("to")   or [""])[0]
            # Always list ALL channels that have any analyzed transcripts (sorted by
            # total desc). The filtered_count reflects only the current date window so
            # channels outside the range show 0 rather than disappearing.
            date_conds, params = [], []
            if date_from:
                date_conds.append("v.transcribed_date >= ?"); params.append(date_from)
            if date_to:
                date_conds.append("v.transcribed_date <= ?"); params.append(date_to)
            # CASE counts only videos matching the date window; outer query is unfiltered
            # so all channels always appear (channels outside the window show 0).
            case_when = (" AND ".join(date_conds)) if date_conds else "1"
            rows = self.query(
                "SELECT v.channel_name, "
                f"  SUM(CASE WHEN {case_when} THEN 1 ELSE 0 END) AS filtered_cnt, "
                "  COUNT(*) AS total_cnt "
                "FROM videos v "
                "JOIN transcripts t ON t.video_id=v.id "
                "JOIN ai_analysis a ON a.video_id=v.id "
                "WHERE v.transcript_status='obtained' "
                "GROUP BY v.channel_name ORDER BY total_cnt DESC, v.channel_name",
                tuple(params)
            )
            self.json({"channels": [
                {"name": r["channel_name"], "count": r["filtered_cnt"], "total": r["total_cnt"]}
                for r in rows if r["channel_name"]
            ]})
            return
        elif p == "/api/report":
            md = ""
            latest = os.path.join(profiles.load()["reports_dir"], "latest.md")
            if os.path.isfile(latest):
                with open(latest, encoding="utf-8") as fh:
                    md = fh.read()
            self.json({"markdown": md})
        elif p.startswith("/api/job_log"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (qs.get("name") or ["podcast_scraper"])[0]
            n_lines = int((qs.get("lines") or ["80"])[0])
            offset = int((qs.get("offset") or ["0"])[0])
            log_path = profiles.HERMES / "logs" / f"{name}.log"
            if not log_path.is_file():
                self.json({"lines": [], "exists": False, "total_lines": 0})
                return
            with open(log_path, "r", errors="replace") as fh:
                all_lines = fh.readlines()
            total = len(all_lines)
            if offset:
                window = all_lines[offset:][-n_lines:]
            else:
                window = all_lines[-n_lines:]
            self.json({"lines": [l.rstrip() for l in window], "exists": True,
                       "total_lines": total})
        elif p.startswith("/api/job_status"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            name = (qs.get("name") or ["podcast_scraper"])[0]
            script_map = {
                "podcast_scraper": SCRAPER_SCRIPT,
                "fetch_transcripts": FETCH_SCRIPT,
                "analyze_transcripts": ANALYZE_SCRIPT,
                "generate_digest": DIGEST_SCRIPT,
                "generate_report": REPORT_SCRIPT,
            }
            script = os.path.basename(script_map.get(name, name + ".py"))
            try:
                out = subprocess.run(
                    ["pgrep", "-f", script], capture_output=True, text=True
                )
                running = bool(out.stdout.strip())
            except Exception:
                running = False
            self.json({"running": running, "name": name})
        elif p == "/api/profiles":
            active = profiles.load()
            settings = {k: active.get(k) for k in profiles.EDITABLE_SETTINGS}
            self.json({"active": profiles.active_name(),
                       "profiles": profiles.list_profiles(),
                       "settings": settings})
        elif p == "/api/llm_providers":
            data = _load_providers()
            result = []
            for prov in data.get("providers", []):
                key = _provider_key(prov["id"])
                result.append({
                    "id": prov["id"],
                    "label": prov["label"],
                    "base": prov["base"],
                    "key_set": bool(key),
                    "key_hint": ("..." + key[-4:]) if len(key) > 4 else ("*" * len(key)),
                })
            self.json({
                "providers": result,
                "bulk": data.get("bulk", {}),
                "synth": data.get("synth", {}),
            })
        elif p.startswith("/api/llm_models"):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            pid = (qs.get("provider") or [""])[0]
            data = _load_providers()
            prov = next((pv for pv in data.get("providers", []) if pv["id"] == pid), None)
            if not prov:
                self.json({"ok": False, "error": "Provider not found", "models": []})
                return
            key = _provider_key(pid)
            base = prov["base"].rstrip("/")
            if not key:
                self.json({"ok": False, "error": "No API key for this provider", "models": []})
                return
            try:
                req = urllib.request.Request(
                    f"{base}/models",
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=10) as r:
                    mdata = json.loads(r.read())
                models = sorted([m["id"] for m in mdata.get("data", []) if isinstance(m, dict) and "id" in m])
                self.json({"ok": True, "models": models})
            except Exception as e:
                self.json({"ok": False, "error": str(e), "models": []})
        else:
            self.send_error(404)

    def do_POST(self):
        refresh_active_db()
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

        # Profile management (create / switch / test investigation packages)
        if self.path == "/api/profiles" or self.path == "/api/set_profile":
            name = body.get("name")
            try:
                profiles.set_active(name)
                migrate(profiles.db_path_for(name))  # ensure the target DB exists/migrated
                self.json({"ok": True, "active": profiles.active_name()})
            except Exception as e:
                self.json({"ok": False, "error": str(e)})
            return
        if self.path == "/api/create_profile":
            try:
                nm = profiles.create(
                    body.get("name", "new-investigation"),
                    label=body.get("label"),
                    search_queries=body.get("search_queries") or [],
                    curated_channels=body.get("curated_channels") or {},
                    keywords=body.get("keywords") or [],
                    analysis_focus=body.get("analysis_focus"),
                    digest_title=body.get("digest_title"),
                )
                # Per-arm enable + the literature arm
                extra = {"youtube_enabled": bool(body.get("youtube_enabled", True))}
                if body.get("literature_enabled"):
                    extra["literature"] = {
                        "enabled": True,
                        "queries": body.get("literature_queries") or [],
                        "sources": ["europepmc"],
                        "since_date": body.get("literature_since") or "2020-01-01",
                        "min_citations": 0, "include_preprints": True,
                        "max_results_per_source": 8,
                    }
                profiles.update(nm, extra)
                migrate(profiles.db_path_for(nm))
                self.json({"ok": True, "name": nm})
            except Exception as e:
                self.json({"ok": False, "error": str(e)})
            return
        if self.path == "/api/update_profile":
            name = body.get("name") or profiles.active_name()
            fields = {}
            for k in ("min_views", "min_duration_sec", "max_duration_sec",
                      "min_days_old", "max_videos_per_channel",
                      "enrich_recent_days", "enrich_recent_hours", "enrich_older_days"):
                if body.get(k) not in (None, ""):
                    try:
                        fields[k] = int(body[k])
                    except (ValueError, TypeError):
                        pass
            for k in ("min_publish_date", "analysis_focus", "digest_title"):
                if body.get(k) not in (None, ""):
                    fields[k] = body[k]
            try:
                profiles.update(name, fields)
                self.json({"ok": True})
            except Exception as e:
                self.json({"ok": False, "error": str(e)})
            return
        if self.path == "/api/test_profile":
            name = body.get("name")
            try:
                out = subprocess.run(
                    [sys.executable, SCRAPER_SCRIPT, "--test", "--json", "--profile", name],
                    capture_output=True, text=True, timeout=150)
                line = (out.stdout or "").strip().splitlines()[-1] if out.stdout.strip() else "{}"
                self.json({"ok": True, "summary": json.loads(line)})
            except Exception as e:
                self.json({"ok": False, "error": str(e), "raw": (out.stdout if 'out' in dir() else '')[:500]})
            return

        if self.path == "/api/llm_providers/add":
            label = body.get("label", "").strip()
            base = body.get("base", "https://api.openai.com/v1").rstrip("/")
            key = body.get("key", "").strip()
            if not label or not key:
                self.json({"ok": False, "error": "label and key are required"})
                return
            pid = _make_provider_id()
            data = _load_providers()
            data["providers"].append({"id": pid, "label": label, "base": base})
            _write_env_file({f"LLM_KEY_{pid}": key})
            _save_providers(data)
            os.environ[f"LLM_KEY_{pid}"] = key
            self.json({"ok": True, "id": pid})
            return
        if self.path == "/api/llm_providers/edit":
            pid = body.get("id", "")
            data = _load_providers()
            prov = next((p for p in data["providers"] if p["id"] == pid), None)
            if not prov:
                self.json({"ok": False, "error": "Not found"})
                return
            if body.get("label"):
                prov["label"] = body["label"].strip()
            if body.get("base"):
                prov["base"] = body["base"].rstrip("/")
            if body.get("key"):
                _write_env_file({f"LLM_KEY_{pid}": body["key"].strip()})
                os.environ[f"LLM_KEY_{pid}"] = body["key"].strip()
            _save_providers(data)
            self.json({"ok": True})
            return
        if self.path == "/api/llm_providers/delete":
            pid = body.get("id", "")
            data = _load_providers()
            data["providers"] = [p for p in data["providers"] if p["id"] != pid]
            for role in ("bulk", "synth"):
                if data.get(role, {}).get("provider_id") == pid:
                    data[role] = {}
            _save_providers(data)
            self.json({"ok": True})
            return
        if self.path == "/api/llm_test":
            pid = body.get("provider_id", "")
            data = _load_providers()
            prov = next((p for p in data.get("providers", []) if p["id"] == pid), None)
            if not prov:
                self.json({"ok": False, "error": "Provider not found"})
                return
            key = _provider_key(pid)
            if not key:
                self.json({"ok": False, "error": "No API key set"})
                return
            base = prov["base"].rstrip("/")
            # Use model explicitly passed, or whichever role uses this provider, or first from /models
            model = body.get("model", "")
            if not model:
                for role in ("bulk", "synth"):
                    if data.get(role, {}).get("provider_id") == pid:
                        model = data[role].get("model", "")
                        break
            if not model:
                try:
                    mreq = urllib.request.Request(
                        f"{base}/models",
                        headers={"Authorization": f"Bearer {key}"},
                    )
                    with urllib.request.urlopen(mreq, timeout=8) as mr:
                        mdata = json.loads(mr.read())
                    models = [m["id"] for m in mdata.get("data", []) if isinstance(m, dict)]
                    model = models[0] if models else ""
                except Exception:
                    pass
            if not model:
                self.json({"ok": False, "error": "No model configured for this provider — select one in Active Model Selection first"})
                return
            try:
                payload = json.dumps({
                    "model": model,
                    "messages": [{"role": "user", "content": "Say 'ok' in one word."}],
                    "max_tokens": 10,
                }).encode()
                req = urllib.request.Request(
                    f"{base}/chat/completions",
                    data=payload,
                    headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
                )
                with urllib.request.urlopen(req, timeout=15) as r:
                    resp = json.loads(r.read())
                reply = resp["choices"][0]["message"]["content"].strip()
                self.json({"ok": True, "reply": reply, "model": model})
            except urllib.error.HTTPError as e:
                body_text = e.read().decode(errors="replace")[:300]
                self.json({"ok": False, "error": f"HTTP {e.code}: {body_text}"})
            except Exception as e:
                self.json({"ok": False, "error": str(e)})
            return
        if self.path == "/api/llm_selection":
            data = _load_providers()
            bulk = body.get("bulk", {})
            synth = body.get("synth", {})
            if bulk:
                data["bulk"] = bulk
            if synth:
                data["synth"] = synth
            _save_providers(data)
            # Sync active vars into .env so analyze_transcripts picks them up
            env_updates = {}
            if bulk.get("provider_id"):
                bp = next((p for p in data["providers"] if p["id"] == bulk["provider_id"]), None)
                if bp:
                    env_updates["PODCAST_LLM_BASE"] = bp["base"]
                    env_updates["PODCAST_LLM_MODEL"] = bulk.get("model", "")
                    bkey = _provider_key(bulk["provider_id"])
                    if bkey:
                        env_updates["PODCAST_LLM_KEY"] = bkey
            if synth.get("provider_id"):
                sp = next((p for p in data["providers"] if p["id"] == synth["provider_id"]), None)
                if sp:
                    env_updates["PODCAST_SYNTH_BASE"] = sp["base"]
                    env_updates["PODCAST_SYNTH_MODEL"] = synth.get("model", "")
                    skey = _provider_key(synth["provider_id"])
                    if skey:
                        env_updates["PODCAST_SYNTH_KEY"] = skey
            _write_env_file(env_updates)
            for k, v in env_updates.items():
                if v:
                    os.environ[k] = v
            self.json({"ok": True})
            return

        # These take no id — launch a background script.
        if self.path == "/api/process_queue":
            self.spawn_job(FETCH_SCRIPT,
                           "SELECT COUNT(*) AS n FROM videos WHERE transcript_status='requested'",
                           "fetch_transcripts", "video(s) to transcribe")
            return
        if self.path == "/api/analyze":
            self.spawn_job(ANALYZE_SCRIPT,
                           "SELECT COUNT(*) AS n FROM videos WHERE transcript_status='obtained' "
                           "AND id NOT IN (SELECT video_id FROM ai_analysis)",
                           "analyze_transcripts", "transcript(s) to analyze")
            return
        if self.path == "/api/generate_digest":
            if self._job_running(DIGEST_SCRIPT):
                self.json({"started": False, "running": True,
                           "message": "Digest is already generating — wait for it to finish."})
                return
            self._spawn([DIGEST_SCRIPT], "generate_digest")
            self.json({"started": True, "message": "Generating digest…"})
            return
        if self.path == "/api/generate_report":
            if self._job_running(REPORT_SCRIPT):
                self.json({"started": False, "running": True,
                           "message": "A report is already generating — wait for it to finish."})
                return
            n = int(body.get("n") or 8)
            date_from = body.get("date_from") or ""
            date_to = body.get("date_to") or ""
            channels = body.get("channels") or ([body["channel"]] if body.get("channel") else [])
            args = [REPORT_SCRIPT, f"--n={n}"]
            if date_from: args.append(f"--from={date_from}")
            if date_to: args.append(f"--to={date_to}")
            for c in channels:
                if c: args.append(f"--channel={c}")
            self._spawn(args, "generate_report")
            self.json({"started": True, "message": "Generating report…"})
            return
        if self.path == "/api/run_discovery":
            # Guard: refuse to start if a scraper is already running
            already = subprocess.run(
                ["pgrep", "-f", os.path.basename(SCRAPER_SCRIPT)],
                capture_output=True, text=True,
            )
            if already.stdout.strip():
                self.json({"started": False,
                           "message": "Discovery is already running — wait for it to finish."})
                return
            prof = profiles.load()
            launched = []
            if prof.get("youtube_enabled", True):
                self._spawn([SCRAPER_SCRIPT], "podcast_scraper")
                launched.append("video")
            if prof.get("literature", {}).get("enabled", False):
                self._spawn([INGEST_LIT_SCRIPT], "ingest_literature")
                launched.append("literature")
            self.json({"started": True,
                       "message": f"Discovery running ({' + '.join(launched) or 'video'}) in the background…"})
            return
        if self.path == "/api/suggest_terms":
            if self._job_running(SCRAPER_SCRIPT):
                self.json({"started": False, "running": True,
                           "message": "The scraper is busy — wait for it to finish."})
                return
            self._spawn([SCRAPER_SCRIPT, "--suggest-terms"], "suggest_terms")
            self.json({"started": True, "message": "Generating term suggestions…"})
            return
        if self.path == "/api/promote_channel":
            cid = body.get("channel_id")
            if not cid:
                self.send_error(400, "Missing channel_id")
                return
            conn = sqlite3.connect(DB_PATH)
            conn.execute("UPDATE channels SET curated = 1, suggested = 0 WHERE channel_id = ?", (cid,))
            conn.commit(); conn.close()
            self.json({"ok": True})
            return
        if self.path in ("/api/accept_term", "/api/reject_term"):
            term = body.get("term")
            if not term:
                self.send_error(400, "Missing term")
                return
            status = "accepted" if self.path.endswith("accept_term") else "rejected"
            conn = sqlite3.connect(DB_PATH)
            conn.execute("UPDATE suggested_terms SET status = ? WHERE term = ?", (status, term))
            conn.commit(); conn.close()
            self.json({"ok": True})
            return

        if self.path == "/api/delete_digest":
            import re as _re
            name = body.get("filename", "")
            if not _re.match(r'^digest_[\w-]+\.md$', name):
                self.json({"ok": False, "error": "invalid filename"})
            else:
                digest_dir = Path(profiles.load()["digest_dir"])
                fpath = (digest_dir / name).resolve()
                if str(fpath).startswith(str(digest_dir.resolve())) and fpath.exists():
                    fpath.unlink()
                remaining = sorted(digest_dir.glob("digest_*.md"), reverse=True)
                latest = digest_dir / "latest.md"
                if remaining:
                    latest.write_text(remaining[0].read_text(encoding="utf-8"), encoding="utf-8")
                elif latest.exists():
                    latest.unlink()
                self.json({"ok": True})
            return

        video_id = body.get("id")
        if not video_id:
            self.send_error(400, "Missing ID")
            return

        conn = sqlite3.connect(DB_PATH)
        if self.path == "/api/request_transcribe":
            conn.execute("UPDATE videos SET transcript_status = 'requested' WHERE id = ?", (video_id,))
            conn.commit()
            # Feature #2: queuing new work clears the last-run "processed" message.
            runstate.clear_transcribe_result(DB_PATH)
            self.json({"ok": True})
        elif self.path == "/api/unrequest":
            conn.execute("UPDATE videos SET transcript_status = 'not_requested' WHERE id = ?", (video_id,))
            conn.commit()
            self.json({"ok": True})
        elif self.path == "/api/dismiss_video":
            # Skip Forever (S1.A/S1.C): record the title so it never returns
            # (even re-uploaded under a new id), and hide this row immediately.
            row = conn.execute("SELECT video_title FROM videos WHERE id = ?", (video_id,)).fetchone()
            conn.execute("UPDATE videos SET dismissed = 1 WHERE id = ?", (video_id,))
            conn.commit()
            if row and row[0]:
                skiplist.add_skip_title(DB_PATH, row[0])
            self.json({"ok": True})
        elif self.path == "/api/undismiss_video":
            conn.execute("UPDATE videos SET dismissed = 0 WHERE id = ?", (video_id,))
            conn.commit()
            self.json({"ok": True})
        else:
            self.send_error(404)
        conn.close()

    def json(self, data):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode())

    def query(self, sql, params=()):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(sql, params)
        rows = [dict(r) for r in cursor.fetchall()]
        conn.close()
        return rows

    def get_stats(self):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        total = conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0]
        
        status_counts = {"not_requested": 0, "requested": 0, "obtained": 0, "not_available": 0, "error": 0}
        rows = conn.execute("SELECT transcript_status, COUNT(*) as count FROM videos GROUP BY transcript_status").fetchall()
        for r in rows:
            if r['transcript_status'] in status_counts:
                status_counts[r['transcript_status']] = r['count']
            
        channels = conn.execute("""
            SELECT COALESCE(NULLIF(channel_name, ''), 'Unknown') as name, COUNT(*) as count 
            FROM videos GROUP BY name ORDER BY count DESC LIMIT 15
        """).fetchall()
        conn.close()
        
        stats = {"total": total, "channels": [dict(r) for r in channels]}
        stats.update(status_counts)
        return stats

    def _job_log(self, name):
        log_dir = str(profiles.HERMES / "logs")
        os.makedirs(log_dir, exist_ok=True)
        return open(os.path.join(log_dir, f"{name}.log"), "a")

    def _spawn(self, args, log_name, **kwargs):
        """Launch a background Python script with unbuffered output (-u) so
        log lines appear in the file immediately, not after the buffer fills."""
        logfile = self._job_log(log_name)
        # Pin the child to the profile active NOW (spawn time). Every spawned job
        # resolves its DB via profiles.load() at import; without this, a profile
        # switch between click and import would silently send the job to another
        # profile's DB and banner (LEARNINGS P3/P6). The parent never sets
        # PTD_PROFILE, so its own per-request view is unaffected.
        env = kwargs.pop("env", None) or os.environ.copy()
        env["PTD_PROFILE"] = profiles.active_name()
        subprocess.Popen([sys.executable, "-u"] + args,
                         stdout=logfile, stderr=subprocess.STDOUT,
                         start_new_session=True, env=env, **kwargs)

    def _job_running(self, script):
        """True if a background job for this script is already running. The
        server-side half of the double-submit guard (CLAUDE.md): a second click
        or page reload must not spawn a duplicate that races/corrupts shared
        state (e.g. two writers interleaving into one log)."""
        try:
            out = subprocess.run(
                ["pgrep", "-f", os.path.basename(script)],
                capture_output=True, text=True,
            )
            return bool(out.stdout.strip())
        except Exception:
            return False

    def spawn_job(self, script, count_sql, name, noun):
        """Launch a background script if there's pending work. Non-blocking."""
        if self._job_running(script):
            self.json({"started": False, "running": True,
                       "message": f"Already running — wait for it to finish."})
            return
        n = self.query(count_sql)[0]["n"]
        if n == 0:
            self.json({"started": False, "queued": 0, "message": "Nothing pending."})
            return
        if not os.path.isfile(script):
            self.json({"started": False, "queued": n, "message": f"Missing {script}"})
            return
        self._spawn([script], name)
        self.json({"started": True, "queued": n,
                   "message": f"Processing {n} {noun} in the background."})

    def get_discovery(self):
        """Emerging signal: new-channel videos, channels worth monitoring, and
        LLM-suggested search terms awaiting review."""
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        emerging = [dict(r) for r in conn.execute("""
            SELECT id, video_title, channel_name, views, quality_score,
                   views_per_day, transcript_status
            FROM videos WHERE is_new_channel = 1
            ORDER BY quality_score DESC LIMIT 50
        """).fetchall()]
        channels = [dict(r) for r in conn.execute("""
            SELECT channel_id, channel_name, channel_url, video_count, best_score
            FROM channels WHERE suggested = 1 AND curated = 0
            ORDER BY best_score DESC LIMIT 50
        """).fetchall()]
        terms = [dict(r) for r in conn.execute("""
            SELECT term, source, created_at FROM suggested_terms
            WHERE status = 'pending' ORDER BY created_at DESC LIMIT 50
        """).fetchall()]
        conn.close()
        return {"emerging": emerging, "suggested_channels": channels, "suggested_terms": terms}

    def get_intelligence(self):
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("""
            SELECT v.id, v.video_title, v.channel_name, v.views, v.quality_score,
                   v.url, v.source_type, v.citations,
                   a.seo_entities, a.geo_signals, a.best_quote, a.analyzed_at
            FROM videos v JOIN ai_analysis a ON a.video_id = v.id
            ORDER BY v.quality_score DESC LIMIT 100
        """).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            try:
                d["seo_entities"] = json.loads(d["seo_entities"] or "[]")
            except Exception:
                d["seo_entities"] = []
            try:
                d["geo_signals"] = json.loads(d["geo_signals"] or "[]")
            except Exception:
                d["geo_signals"] = []
            d["key_points"] = [dict(k) for k in conn.execute(
                "SELECT timestamp_sec, point_text, category FROM key_points "
                "WHERE video_id=? ORDER BY timestamp_sec", (r["id"],)).fetchall()]
            out.append(d)
        conn.close()
        return out

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--migrate", action="store_true", help="Run database migrations")
    parser.add_argument("--reconcile", action="store_true",
                        help="Make transcript_status honest (reset fake 'obtained', drop stub files)")
    args = parser.parse_args()

    if args.migrate:
        migrate()
        sys.exit(0)

    if args.reconcile:
        reconcile()
        sys.exit(0)

    port = int(os.environ.get("PORT", 9091))
    try:
        server = http.server.HTTPServer(("0.0.0.0", port), Handler)
    except OSError as e:
        if e.errno == 48:  # Address already in use
            print(f"Port {port} is already in use.")
            print(f"  • If the dashboard is already open, just visit http://localhost:{port}")
            print(f"  • Otherwise start on another port:  PORT={port + 1} python3 dashboard_server.py")
            sys.exit(1)
        raise
    print(f"Podcast Tracker Dashboard running at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down.")
        sys.exit(0)
