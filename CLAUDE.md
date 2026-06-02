# Podcast Tracker Dashboard

> **What this file is:** the lean, machine-readable rulebook for working in this repo. Rules, constraints, and file paths only — no version history, no feature narration.

---

## Project Overview

A small toolchain that discovers SEO/AI/GEO podcasts on YouTube, scores them by quality, queues the good ones for transcription, fetches transcripts, and surfaces everything in a local web dashboard. All state lives in one SQLite database.

- **GitHub:** https://github.com/dbgnvan2/podcast-tracker-dashboard
- **Data store:** SQLite at `~/.hermes/podcast_tracker.db` (single source of truth for all four scripts)
- **Transcripts:** plain-text files in `~/.hermes/transcripts/`

> **Origin & status:** This codebase was prototyped by an agent ("Hermes") under `~/.hermes/scripts/`, then handed off to a real coding agent (this repo) because the prototype stalled — file writes failed, the server wouldn't launch, and at one point the prototype manually edited the DB instead of fixing code (a mistake the user was emphatic about never repeating — see Working Rules). The scripts here run, but the **AI-analysis stage is unbuilt** and the **live DB is in a partly-inconsistent state** from the aborted prototype runs. See "Product Vision & Roadmap" below.

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3 (stdlib-only; no third-party packages) |
| Web server | `http.server` (stdlib) — no framework |
| Frontend | Single inline HTML/CSS/JS string in `dashboard_server.py` (no build step) |
| Data store | SQLite (`sqlite3` stdlib) |
| YouTube access | `yt-dlp` invoked as a subprocess |

There is **no virtualenv, no `requirements.txt`, no `package.json`**. The only external dependency is the `yt-dlp` binary on `PATH` (or one of the hardcoded Homebrew/pip paths).

---

## Directory Structure

```
podcast-tracker-dashboard/
├── podcast_scraper.py          # Search YouTube → score → upsert into videos table
├── youtube_podcast_scanner.py  # ⚠️ BYTE-IDENTICAL DUPLICATE of podcast_scraper.py
├── fetch_transcripts.py        # Drain 'requested' queue → yt-dlp subs → transcripts table
└── dashboard_server.py         # Stdlib HTTP server + inline SPA + JSON API + DB migration
```

### Data locations (outside the repo, NOT committed)
- `~/.hermes/podcast_tracker.db` — the SQLite database
- `~/.hermes/transcripts/` — fetched `.txt` transcripts (VTT intermediates are deleted)

---

## Data Model (SQLite)

- **`videos`** — one row per discovered video. PK `id` (YouTube video id). Key columns: `quality_score`, `views`, `channel_name`, `publish_date`, and `transcript_status`.
- **`transcripts`** — `video_id` PK, `full_text`, `word_count`, `file_path`.
- **`key_points`** — extracted points per video (`video_id`, `timestamp_sec`, `point_text`, `category`). Currently empty — target of the unbuilt AI-analysis stage.
- **`ai_analysis`** — per-video AI extraction (`seo_entities`, `geo_signals`, `best_quote`, `analyzed_at`). Exists in the live DB but **no script creates or writes it**; fold its creation into `migrate()` when you build the analysis stage.
- **`runs`** — scraper run log.

### `transcript_status` state machine (load-bearing)
The dashboard tabs and the fetcher queue both key off this single column. Valid values:

`not_requested` → `requested` → `obtained` | `not_available` | `error`

- The dashboard's **Candidates** tab = `not_requested`; **Requested** = `requested`; **Transcribed** = `obtained`.
- `fetch_transcripts.py` only processes rows where `transcript_status = 'requested'`.
- Do not introduce new status values without updating: the fetcher query, all dashboard queries, and the stats counters in `get_stats()`.

---

## Product Vision & Roadmap

The end goal (user's words across the build): a publishable **weekly "best of" SEO/AI/GEO podcast digest with key insights**. The intended end-to-end pipeline:

```
discover (cron) → score → mark for transcription → fetch transcript
   → AI-analyze transcript → surface in dashboard → weekly "best of" digest
```

### Built and working
- Discovery + quality scoring (`podcast_scraper.py`)
- Transcript fetching for the `requested` queue (`fetch_transcripts.py`)
- Dashboard with Candidates / Requested / Transcribed / Stats tabs + mark-for-transcribe actions

### Designed but NOT built (the stall point)
1. **AI-analysis stage.** The DB already has the target tables, but nothing populates them:
   - `ai_analysis(video_id, seo_entities, geo_signals, best_quote, analyzed_at)` — created out-of-band, **not in any script**, 0 rows.
   - `key_points(video_id, timestamp_sec, point_text, category)` — 0 rows; the dashboard's "KP" column counts these.
   - `videos.transcript_summary` — unused.
   The intent: run each **already-fetched, on-disk** transcript through an LLM to extract **key points with timestamps**, SEO entities, GEO signals, and a best quote, then store them so the dashboard and the weekly digest can use them. **Hard requirement:** this stage reads the saved transcript file as its only input — it must never analyze a video from its title/metadata or a browser snapshot (see Working Rule 0; this is exactly the failure that wrecked the prototype).
2. **Dashboard-triggered processing.** The user chose "option A": a dashboard button should be able to **fire a backend action** that fetches → analyzes → saves, so they never touch a terminal. Currently the dashboard only flips `transcript_status`; `fetch_transcripts.py` must be run manually.
3. **Cron-driven daily channel monitoring + weekly "best of" output.** A run is meant to also emit a top-10 transcribe-candidates list. (A `runs` table logs scraper runs; the digest generator does not exist yet.)

### Known data inconsistencies in the live DB (from aborted prototype runs)
- 4 videos are `transcript_status='obtained'` but the `transcripts` table is **empty**, and the 3 `.txt` files in `~/.hermes/transcripts/` **don't match** those video IDs. Treat current "obtained" rows as suspect; a reconciliation pass may be needed before trusting transcript state. Do **not** paper over this by editing rows by hand — fix the code path that caused it.

---

## Hard Constraints (read before changing anything)

1. **Stdlib only.** Do not add third-party Python dependencies, a web framework, or a frontend build step. If a change seems to need one, stop and ask first.
2. **The two scraper files are duplicates.** `podcast_scraper.py` and `youtube_podcast_scanner.py` are byte-identical. Any change to scoring/search logic must be applied to BOTH, or the duplication must be resolved first (ask before deleting one — a cron/launchd job may reference either name).
3. **Schema changes go through `dashboard_server.py:migrate()`.** It is additive and idempotent (`ALTER TABLE ... ADD COLUMN`, `CREATE TABLE IF NOT EXISTS`). Never write a destructive migration against `~/.hermes/podcast_tracker.db` — it holds real, non-reproducible scrape history. Run via `python3 dashboard_server.py --migrate`.
4. **Never delete or overwrite the database or the transcripts directory.** They live in `~/.hermes/` and are the only copy.
5. **`transcript_status` is the contract** between all four scripts (see state machine above).
6. **SQL safety:** all user/POST-driven queries use parameterized statements (`?`). Keep it that way — never string-format video ids into SQL.

---

## Working Rules (learned from the build history — these are load-bearing)

0. **NEVER fabricate transcript content, key points, summaries, timestamps, or quotes. This is the #1 way the prototype destroyed trust.** The Hermes prototype repeatedly browser-scraped YouTube, got truncated/empty text, and then *invented* "key points" from the video title — wrong content, wrong video, made-up timestamps, over and over. Every `key_points` / `ai_analysis` / `transcript_summary` row MUST be derived from an actual saved transcript file on disk. If a transcript can't be obtained, set status `not_available`/`error` and write nothing — never guess. **Transcripts are obtained programmatically only** (`yt-dlp --write-auto-subs`, as `fetch_transcripts.py` already does, or `youtube-transcript-api`) — never by reading a browser snapshot, and never from the model's own recollection of the video.
1. **NEVER hand-edit the database to make the UI look right. Fix the code.** The prototype was caught manually patching rows to fake a working dashboard; the user's response was unambiguous ("a bad lazy hack… NEVER do this again"). Any state in the DB must be the result of a code path that produced it.
2. **Read the DB correctly — don't rely on the user to hand you IDs/titles.** The transcription workflow is: *get the `requested` list → filter out `obtained` / `not_available` → process what remains.* The user expects the code to find the right rows itself.
3. **Don't fake success.** When something can't be transcribed/analyzed, set the honest status (`not_available` / `error`) — never report "done" optimistically. The user explicitly distrusts "8 were transcribed" claims that don't reconcile with the data.
4. **The user works from the GUI, not the terminal.** They avoid terminal sessions and won't run multi-line shell one-liners reliably. Prefer: a single script + a one-word command, or a dashboard button. This is *why* "option A" (dashboard fires backend processing) is the chosen direction.
5. **When the prototype's approach was wrong, prefer a proper fix over a patch**, and surface a clear spec before large changes.

---

## Coding Standards

- Match the existing terse, single-file style. No premature abstraction — this is a 4-file utility, not a framework.
- Subprocess calls to `yt-dlp` must keep a `timeout=` and handle `CalledProcessError` / `JSONDecodeError` gracefully (the scraper already does — follow that pattern).
- DB access: open a connection, do the work, `commit()` if writing, `close()`. Use `conn.row_factory = sqlite3.Row` when reading rows that become JSON.
- The dashboard frontend is plain `fetch()` + DOM building inside the HTML string in `dashboard_server.py`. No framework, no bundler.

---

## Running Locally

```bash
# One-time / after schema edits: create or migrate the DB
python3 dashboard_server.py --migrate

# Make transcript_status honest (reset fake 'obtained', drop stub transcript files)
python3 dashboard_server.py --reconcile

# Discover + score new videos (writes to the videos table)
python3 podcast_scraper.py

# Fetch transcripts for everything marked 'requested'
python3 fetch_transcripts.py

# Dashboard (defaults to PORT 9091)
python3 dashboard_server.py
# → http://localhost:9091
```

Requires `yt-dlp` on `PATH` (or installed at a Homebrew/pip path the scripts probe).

---

## HTTP API (served by `dashboard_server.py`)

| Method | Path | Purpose |
|---|---|---|
| GET | `/` | The dashboard SPA (inline HTML) |
| GET | `/api/candidates` | Top 50 `not_requested` by `quality_score` |
| GET | `/api/requested` | Top 50 `requested` by `quality_score` |
| GET | `/api/transcribed` | `obtained` rows joined with `transcripts` + key-point count |
| GET | `/api/stats` | Totals, per-status counts, top channels |
| GET | `/api/transcript/{id}` | Verified `full_text` + word count for the Transcribed-tab "View" modal |
| POST | `/api/request_transcribe` | `{id}` → set status `requested` |
| POST | `/api/unrequest` | `{id}` → set status `not_requested` |
| POST | `/api/process_queue` | Launches `fetch_transcripts.py` as a background subprocess to drain the `requested` queue; returns `{started, queued, message}` |

Any new endpoint the frontend calls must be added to the inline JS in the same change.

### Known environment blocker (transcription)
`yt-dlp` on this machine is **outdated** (at `~/.hermes/Library/Python/3.9/bin/yt-dlp`, deprecated Python 3.9). YouTube now gates auto-captions behind a **PO token**, so this yt-dlp returns "no captions" even when captions exist. `fetch_transcripts.py` now classifies those gated/blocked responses as retryable `error` (not `not_available`). **To actually produce transcripts, upgrade yt-dlp** (`pip install -U yt-dlp`, ideally on Python 3.10+) and re-run the queue. This is the only thing blocking the end-to-end pipeline.
