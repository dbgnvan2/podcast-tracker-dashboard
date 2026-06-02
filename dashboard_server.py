#!/usr/bin/env python3
import http.server
import json
import sqlite3
import os
import urllib.parse
import sys
import argparse
import subprocess

DB_PATH = os.path.expanduser("~/.hermes/podcast_tracker.db")
TRANSCRIPTS_DIR = os.path.expanduser("~/.hermes/transcripts")
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
FETCH_SCRIPT = os.path.join(SCRIPT_DIR, "fetch_transcripts.py")
ANALYZE_SCRIPT = os.path.join(SCRIPT_DIR, "analyze_transcripts.py")
DIGEST_SCRIPT = os.path.join(SCRIPT_DIR, "generate_digest.py")
SCRAPER_SCRIPT = os.path.join(SCRIPT_DIR, "podcast_scraper.py")
DIGEST_LATEST = os.path.expanduser("~/.hermes/digests/latest.md")

def migrate():
    print(f"Running migration on {DB_PATH}...")
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    
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
            --bg-color: #0f172a;
            --card-bg: #1e293b;
            --text-main: #f8fafc;
            --text-dim: #94a3b8;
            --accent: #38bdf8;
            --accent-hover: #7dd3fc;
            --border: #334155;
            --success: #22c55e;
            --warning: #f59e0b;
            --error: #ef4444;
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
        .tab-btn.active { background: var(--accent); color: var(--bg-color); font-weight: 600; }
        
        .tab-content { display: none; }
        .tab-content.active { display: block; }
        
        .search-container { margin-bottom: 20px; }
        .search-box {
            width: 100%; padding: 10px 15px; border-radius: 8px; border: 1px solid var(--border);
            background: var(--card-bg); color: var(--text-main); font-size: 1rem;
        }
        
        table { width: 100%; border-collapse: collapse; background: var(--card-bg); border-radius: 8px; overflow: hidden; }
        th { text-align: left; padding: 12px 15px; background: #2d3748; color: var(--text-dim); font-size: 0.85rem; text-transform: uppercase; letter-spacing: 0.05em; }
        td { padding: 12px 15px; border-bottom: 1px solid var(--border); }
        tr:hover td { background: #2d3748; }
        
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
        .btn-primary { background: var(--accent); color: var(--bg-color); padding: 8px 16px; font-size: 0.9rem; }
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
        .chip { display: inline-block; background: #2d3748; color: var(--accent-hover); border-radius: 12px; padding: 3px 10px; margin: 3px 4px 3px 0; font-size: 0.78rem; }
        .chip-geo { color: var(--success); }
        .kp-list { list-style: none; padding: 0; margin: 10px 0 0; }
        .kp-list li { padding: 5px 0; border-top: 1px solid var(--border); font-size: 0.9rem; }
        .kp-time { font-family: monospace; color: var(--accent); margin-right: 8px; }
        .kp-cat { color: var(--text-dim); font-size: 0.78rem; }

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
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>Podcast Tracker</h1>
            <div id="last-updated" style="font-size: 0.8rem; color: var(--text-dim);"></div>
        </header>
        
        <div class="tabs">
            <button class="tab-btn active" onclick="showTab('candidates', this)">Candidates</button>
            <button class="tab-btn" onclick="showTab('requested', this)">Requested</button>
            <button class="tab-btn" onclick="showTab('transcribed', this)">Transcribed</button>
            <button class="tab-btn" onclick="showTab('intelligence', this)">Intelligence</button>
            <button class="tab-btn" onclick="showTab('digest', this)">Digest</button>
            <button class="tab-btn" onclick="showTab('discovery', this)">Discovery</button>
            <button class="tab-btn" onclick="showTab('stats', this)">Stats</button>
        </div>
        
        <div id="loading">Loading data...</div>

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
            <table id="table-candidates">
                <thead><tr><th width="50">Score</th><th>Title</th><th>Channel</th><th>Views</th><th>Views/day</th><th width="100">Action</th></tr></thead>
                <tbody></tbody>
            </table>
        </div>

        <div id="requested" class="tab-content">
            <div style="display:flex; gap:12px; align-items:center; margin-bottom:15px;">
                <button class="btn btn-primary" onclick="processQueue()">⚙ Process Queue</button>
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
                <button class="btn btn-primary" onclick="runAnalyze()">🧠 Analyze Transcripts</button>
                <span id="analyze-status" style="color: var(--text-dim); font-size: 0.85rem;"></span>
            </div>
            <div id="intel-list"></div>
        </div>

        <div id="digest" class="tab-content">
            <div style="display:flex; gap:12px; align-items:center; margin-bottom:15px;">
                <button class="btn btn-primary" onclick="generateDigest()">📰 Generate Weekly Digest</button>
                <span id="digest-status" style="color: var(--text-dim); font-size: 0.85rem;"></span>
            </div>
            <div class="chart-section" id="digest-content" style="white-space: pre-wrap; line-height: 1.6;"></div>
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
                const [cRes, rRes, tRes, sRes, iRes, dRes] = await Promise.all([
                    fetch('/api/candidates').then(r => r.json()),
                    fetch('/api/requested').then(r => r.json()),
                    fetch('/api/transcribed').then(r => r.json()),
                    fetch('/api/stats').then(r => r.json()),
                    fetch('/api/intelligence').then(r => r.json()),
                    fetch('/api/discovery').then(r => r.json())
                ]);
                data.candidates = cRes;
                data.requested = rRes;
                data.transcribed = tRes;
                data.stats = sRes;
                data.intelligence = iRes;
                data.discovery = dRes;
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
                    actionHtml = `<td><button class="btn btn-mark" onclick="postAction('/api/request_transcribe', '${v.id}')">Transcribe</button></td>`;
                } else if (type === 'requested') {
                    actionHtml = `<td><button class="btn btn-unreq" onclick="postAction('/api/unrequest', '${v.id}')">Remove</button></td>`;
                } else if (type === 'transcribed') {
                    actionHtml = `<td style="text-align:center; white-space:nowrap">${v.key_point_count || 0} &nbsp;<button class="btn btn-view" onclick="viewTranscript('${v.id}')">View</button></td>`;
                }

                let dateCol = v.publish_date || '';
                if (type === 'transcribed') {
                    dateCol = v.transcript_status || 'obtained';
                    if (v.transcribed_date) dateCol += ' - ' + v.transcribed_date;
                } else if (v.views_per_day) {
                    dateCol = `${(v.views_per_day).toLocaleString()}/day`;
                }
                const badge = v.is_new_channel ? ' <span class="chip" style="background:var(--warning);color:#000">🆕 new</span>' : '';

                tr.innerHTML = `
                    <td class="score">${(v.quality_score || 0).toFixed(2)}</td>
                    <td><a href="https://youtube.com/watch?v=${v.id}" target="_blank">${v.video_title}</a>${badge}</td>
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

        async function runDiscovery() {
            const spans = document.querySelectorAll('.discovery-status');
            spans.forEach(s => s.innerText = 'Starting…');
            try {
                const res = await fetch('/api/run_discovery', { method: 'POST' });
                const d = await res.json();
                spans.forEach(s => s.innerText = d.message || 'Running…');
                // Discovery takes minutes; refresh periodically so results appear as they land.
                let ticks = 0;
                const timer = setInterval(() => {
                    fetchData();
                    if (++ticks >= 20) { clearInterval(timer); spans.forEach(s => s.innerText = 'Finished refreshing.'); }
                }, 15000);
            } catch (err) {
                spans.forEach(s => s.innerText = 'Failed to start: ' + err);
            }
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

        async function processQueue() {
            const status = document.getElementById('queue-status');
            status.innerText = 'Starting…';
            try {
                const res = await fetch('/api/process_queue', { method: 'POST' });
                const data = await res.json();
                status.innerText = data.message || '';
                if (data.started) {
                    // Poll for a while so rows move out of the queue as they complete.
                    let ticks = 0;
                    const timer = setInterval(() => {
                        fetchData();
                        if (++ticks >= 15) { clearInterval(timer); }
                    }, 4000);
                }
            } catch (err) {
                status.innerText = 'Failed to start: ' + err;
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
                const ents = (v.seo_entities||[]).map(e => `<span class="chip">${e}</span>`).join('');
                const geos = (v.geo_signals||[]).map(g => `<span class="chip chip-geo">${g}</span>`).join('');
                const kps = (v.key_points||[]).map(k => {
                    const url = `https://youtube.com/watch?v=${v.id}&t=${parseInt(k.timestamp_sec||0)}s`;
                    const cat = k.category ? ` <span class="kp-cat">(${k.category})</span>` : '';
                    return `<li><a class="kp-time" href="${url}" target="_blank">${fmtTime(k.timestamp_sec)}</a>${k.point_text}${cat}</li>`;
                }).join('');
                return `<div class="intel-card">
                    <h3><a href="https://youtube.com/watch?v=${v.id}" target="_blank">${v.video_title}</a></h3>
                    <div class="intel-meta">${v.channel_name} · ${(v.views||0).toLocaleString()} views · score ${(v.quality_score||0).toFixed(2)}</div>
                    ${v.best_quote ? `<div class="intel-quote">“${v.best_quote}”</div>` : ''}
                    <div>${ents}${geos}</div>
                    ${kps ? `<ul class="kp-list">${kps}</ul>` : ''}
                </div>`;
            }).join('');
        }

        async function runAnalyze() {
            const s = document.getElementById('analyze-status');
            s.innerText = 'Starting…';
            try {
                const res = await fetch('/api/analyze', { method: 'POST' });
                const d = await res.json();
                s.innerText = d.message || '';
                if (d.started) {
                    let t = 0;
                    const timer = setInterval(() => { fetchData(); if (++t >= 20) clearInterval(timer); }, 5000);
                }
            } catch (err) { s.innerText = 'Failed: ' + err; }
        }

        async function generateDigest() {
            const s = document.getElementById('digest-status');
            s.innerText = 'Generating…';
            try {
                await fetch('/api/generate_digest', { method: 'POST' });
                setTimeout(loadDigest, 2500);
                s.innerText = 'Done.';
            } catch (err) { s.innerText = 'Failed: ' + err; }
        }

        async function loadDigest() {
            try {
                const res = await fetch('/api/digest');
                const d = await res.json();
                document.getElementById('digest-content').innerText = d.markdown || 'No digest yet. Click “Generate Weekly Digest”.';
            } catch (err) {
                document.getElementById('digest-content').innerText = 'Error loading digest: ' + err;
            }
        }
        loadDigest();

        fetchData();
    </script>
</body>
</html>
"""

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p == "/":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(HTML.encode())
        elif p == "/api/candidates":
            self.json(self.query("SELECT * FROM videos WHERE transcript_status = 'not_requested' ORDER BY quality_score DESC LIMIT 50"))
        elif p == "/api/requested":
            self.json(self.query("SELECT * FROM videos WHERE transcript_status = 'requested' ORDER BY quality_score DESC LIMIT 50"))
        elif p == "/api/transcribed":
            sql = """
                SELECT v.*, t.word_count, 
                       (SELECT COUNT(*) FROM key_points k WHERE k.video_id = v.id) as key_point_count
                FROM videos v
                LEFT JOIN transcripts t ON v.id = t.video_id
                WHERE v.transcript_status = 'obtained'
                ORDER BY v.transcribed_date DESC
                LIMIT 50
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
        elif p == "/api/discovery":
            self.json(self.get_discovery())
        elif p == "/api/digest":
            md = ""
            if os.path.isfile(DIGEST_LATEST):
                with open(DIGEST_LATEST, encoding="utf-8") as fh:
                    md = fh.read()
            self.json({"markdown": md})
        else:
            self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length)) if length else {}

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
            logfile = self._job_log("generate_digest")
            subprocess.Popen([sys.executable, DIGEST_SCRIPT, "--days=7"],
                             stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
            self.json({"started": True, "message": "Generating digest…"})
            return
        if self.path == "/api/run_discovery":
            logfile = self._job_log("podcast_scraper")
            subprocess.Popen([sys.executable, SCRAPER_SCRIPT],
                             stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
            self.json({"started": True,
                       "message": "Discovery running in the background (this takes a few minutes)…"})
            return
        if self.path == "/api/suggest_terms":
            logfile = self._job_log("suggest_terms")
            subprocess.Popen([sys.executable, SCRAPER_SCRIPT, "--suggest-terms"],
                             stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
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

        video_id = body.get("id")
        if not video_id:
            self.send_error(400, "Missing ID")
            return

        conn = sqlite3.connect(DB_PATH)
        if self.path == "/api/request_transcribe":
            conn.execute("UPDATE videos SET transcript_status = 'requested' WHERE id = ?", (video_id,))
            conn.commit()
            self.json({"ok": True})
        elif self.path == "/api/unrequest":
            conn.execute("UPDATE videos SET transcript_status = 'not_requested' WHERE id = ?", (video_id,))
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
        log_dir = os.path.expanduser("~/.hermes/logs")
        os.makedirs(log_dir, exist_ok=True)
        return open(os.path.join(log_dir, f"{name}.log"), "a")

    def spawn_job(self, script, count_sql, name, noun):
        """Launch a background script if there's pending work. Non-blocking."""
        n = self.query(count_sql)[0]["n"]
        if n == 0:
            self.json({"started": False, "queued": 0, "message": "Nothing pending."})
            return
        if not os.path.isfile(script):
            self.json({"started": False, "queued": n, "message": f"Missing {script}"})
            return
        logfile = self._job_log(name)
        subprocess.Popen([sys.executable, script],
                         stdout=logfile, stderr=subprocess.STDOUT, start_new_session=True)
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
