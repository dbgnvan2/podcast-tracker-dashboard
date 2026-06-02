# Podcast Tracker — Current-State Specification

> **Status of this document:** ground-truth spec of what is **actually in the repo** as of 2026-06-01, verified against the code and the live SQLite DB. Where the earlier Gemini "Command Center" spec described features as done that are **not** implemented, this document marks them as **PLANNED**, not built. See §7 for the divergence table.

---

## 1. What the project is

A small, mostly stdlib Python toolchain that discovers SEO/AI/GEO podcasts on YouTube, scores them for relevance + quality, lets the user queue good ones for transcription, fetches **verified** transcripts (no agent hallucination), and surfaces everything in a local web dashboard. Long-term goal: a weekly "best of" SEO/AI/GEO digest with key insights.

- **Repo:** https://github.com/dbgnvan2/podcast-tracker-dashboard
- **Host:** macOS (Darwin), Python 3.x (`python3` on PATH; not pinned to 3.14)
- **Data store:** SQLite at `~/.hermes/podcast_tracker.db`
- **Transcripts:** `.txt` files in `~/.hermes/transcripts/`

### Design principle (load-bearing)
**Data-first / verified-text-only.** Analysis must run off a real transcript file on disk, never off a video title or a browser snapshot. This rule exists because the predecessor agent ("Hermes") repeatedly fabricated transcripts/key-points; see CLAUDE.md "Working Rules".

---

## 2. File layout

The canonical code lives in this git repo (flat, 4 files at root). The Gemini spec's `~/.hermes/scripts/` paths refer to the **old prototype location**, not this repo.

```
podcast-tracker-dashboard/          # git repo
├── dashboard_server.py             # HTTP server + inline SPA + JSON API + --migrate
├── fetch_transcripts.py            # Transcription engine (drains the 'requested' queue)
├── podcast_scraper.py              # Discovery scanner + scorer
└── youtube_podcast_scanner.py      # ⚠ BYTE-IDENTICAL DUPLICATE of podcast_scraper.py

~/.hermes/                          # runtime data (NOT in repo)
├── podcast_tracker.db              # the database
└── transcripts/                    # verified .txt transcripts
```

---

## 3. Database schema (live, verified)

### `videos` — source of truth (21 rows live)
`id` (TEXT PK = YouTube id), `channel_name`, `channel_url`, `video_title`, `url`, `publish_date`, `duration_seconds`, `views`, `likes`, `comments`, `first_seen_date`, `last_updated_date`, `prev_views`, `view_change`, `view_change_pct`, `transcript_keywords_score`, `quality_score`, `selected`, `transcript_summary`, **`transcript_status`** (the state machine), `transcribed_date`.

**Legacy/dead columns still present:** `transcribed` (INT) and `transcribe_requested` (INT). These predate `transcript_status` and are **no longer read by the repo code** — both the server and the fetcher use `transcript_status`. Leave them for now; drop in a later migration.

### `transcript_status` state machine
```
not_requested  → requested  → obtained
                            ↘  not_available   (verified: no subtitles exist)
                            ↘  error           (transient: rate limit / network — retry next run)
```
- Dashboard tabs map 1:1: Candidates=`not_requested`, Requested=`requested`, Transcribed=`obtained`.
- `fetch_transcripts.py` processes only `requested` rows.

### `transcripts` (0 rows) — `video_id` PK (FK→videos), `file_path`, `full_text`, `word_count`.
### `key_points` (0 rows) — `id` PK, `video_id`, `timestamp_sec`, `point_text`, `category`.
### `ai_analysis` (0 rows) — `video_id` PK, `seo_entities`, `geo_signals`, `best_quote`, `analyzed_at`. **No code creates or writes this table** (it was created out-of-band; fold creation into `migrate()` when the analysis stage is built).
### `runs` (5 rows) — scraper run log: `run_date`, `videos_found`, `videos_new`, `errors`.

---

## 4. Backend components (as built)

### A. `dashboard_server.py` — web UI + API
Stdlib `http.server` on port `PORT` (default **9091**). Includes a `migrate()` routine run via `python3 dashboard_server.py --migrate` (additive, idempotent: adds `transcript_status`/`transcribed_date`, creates `transcripts` + `key_points`).

**Endpoints actually implemented:**
| Method | Path | Behavior |
|---|---|---|
| GET | `/` | Inline dark-theme SPA |
| GET | `/api/candidates` | Top 50 `not_requested` by `quality_score` |
| GET | `/api/requested` | Top 50 `requested` by `quality_score` |
| GET | `/api/transcribed` | `obtained` rows joined to `transcripts` + key-point count |
| GET | `/api/stats` | Totals, per-status counts, top 15 channels |
| POST | `/api/request_transcribe` | `{id}` → status `requested` |
| POST | `/api/unrequest` | `{id}` → status `not_requested` |

**NOT implemented** (claimed by Gemini spec): `POST /api/process_queue`, `GET /api/transcript/{id}`.

### B. `fetch_transcripts.py` — transcription engine
Run manually: `python3 fetch_transcripts.py`. Workflow:
1. `SELECT ... WHERE transcript_status = 'requested'`.
2. For each: `yt-dlp --skip-download --write-auto-subs --sub-langs en.*` → VTT.
3. `clean_vtt()` strips WEBVTT headers, timestamps, dup lines → plain text.
4. If text > 100 chars: write `~/.hermes/transcripts/{id}.txt`, upsert `transcripts` row, set status `obtained` + `transcribed_date`; else status `not_available`.
5. On exception: status `error`.

**Reliability layers:** Layer 1 = `yt-dlp` (the only engine implemented). **Firecrawl "Layer 2" is NOT present** — there is no Firecrawl code anywhere in the repo.

### C. `podcast_scraper.py` (== `youtube_podcast_scanner.py`) — discovery + scoring
Run: `python3 podcast_scraper.py`. Uses `yt-dlp` (`ytsearch`) across a list of SEO/AI/GEO queries, enriches each candidate with full metadata, scores, and upserts into `videos`.

- **Curated channel bonus:** `CHANNEL_BONUS` dict (Neil Patel, Google Search Central, Aleyda Solis, Lily Ray, Cyrus Shepard, etc.).
- **Filters:** `MIN_VIEWS=2000`, duration 8–90 min, `MIN_DAYS_OLD=7`.
- **`calculate_keyword_density()`** scores AI/GEO/SEO keyword hits (weighted) over `transcript+title+channel`, normalized 0–1.
- **`calculate_quality_score()`** composites log10(views), engagement, duration fit, keyword score, channel bonus.
- **`fetch_transcript()`** is a *best-effort* helper that shells out to **`youtube-transcript-api`** (third-party) for keyword scoring during discovery — wrapped in try/except, returns `None` on any failure. This is the project's one optional third-party Python dependency; everything else is stdlib + the `yt-dlp` binary.

---

## 5. Dashboard workflow (as built)

1. **Candidates tab** — review scored discoveries; "Transcribe" button → `request_transcribe`.
2. **Requested tab** — the queue; "Remove" button → `unrequest`. **There is no in-dashboard "Process Queue" button that works** — the queue is drained by running `fetch_transcripts.py` from a terminal.
3. **Transcribed tab** — lists `obtained` videos with status/date and a KP (key-point) count column. **No "View transcript" action** (no transcript endpoint yet).
4. **Stats tab** — status stat-cards + top-channels bar chart.

There is **no "Intelligence" tab** and no AI-analysis trigger in the UI.

---

## 6. Current live data state (known inconsistencies)

- `videos`: 21 rows — `not_requested`:14, `requested`:1, `obtained`:4, `error`:2.
- `transcripts`: **0 rows**, yet 4 videos are `obtained`. The 3 `.txt` files in `~/.hermes/transcripts/` do **not** match the 4 `obtained` ids. → The `obtained` status is currently **unbacked by real transcript rows** — a residue of aborted prototype runs. A code-driven reconciliation pass is needed (do **not** hand-edit rows).

---

## 7. Gemini spec vs. repo reality

| Gemini "Command Center" claim | Reality in repo |
|---|---|
| `POST /api/process_queue` triggers fetcher in background | **Not implemented.** Only `request_transcribe`/`unrequest`. Fetcher is run manually. |
| `GET /api/transcript/{id}` streams text to UI | **Not implemented.** No transcript-view endpoint. |
| "Intelligence" tab maps SEO entities / best quotes | **Not implemented.** 4 tabs only; `ai_analysis` table empty + unwired. |
| Reliability Layer 2 = Firecrawl headless bypass | **Absent.** Only `yt-dlp` (Layer 1). |
| Transcribed text "guaranteed 100% accurate" | True *by design* of the yt-dlp path — but currently 0 transcript rows exist; `obtained` flags are stale. |
| Python 3.14+ required | Runs on generic `python3`; the 3.14 issue was a prototype path bug, not a real requirement. |
| Files under `~/.hermes/scripts/` | That's the old prototype location; canonical code is this git repo (which also has the duplicate scanner file). |

---

## 8. Gap list to reach the Gemini target

1. **AI-analysis stage** — populate `key_points` + `ai_analysis` from saved transcript files only (Working Rule 0). Add `ai_analysis` creation to `migrate()`.
2. **Dashboard-triggered processing** — `POST /api/process_queue` to launch `fetch_transcripts.py` as a background subprocess (the "option A" the user chose).
3. **Transcript view** — `GET /api/transcript/{id}` + a "View" action in the Transcribed tab.
4. **Intelligence tab** — surface entities / best quote / key points.
5. **Weekly "best-of" digest** generator.
6. **Housekeeping** — resolve the duplicate scanner file; reconcile the stale `obtained` rows; eventually drop the dead `transcribed`/`transcribe_requested` columns.
