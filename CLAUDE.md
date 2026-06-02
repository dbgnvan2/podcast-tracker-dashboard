# Podcast Tracker Dashboard

> **What this file is:** the lean, machine-readable rulebook for working in this repo. Rules, constraints, and file paths only — no version history, no feature narration.

---

## Project Overview

A small toolchain that discovers SEO/AI/GEO podcasts on YouTube, scores them by quality, queues the good ones for transcription, fetches transcripts, and surfaces everything in a local web dashboard. All state lives in one SQLite database.

- **GitHub:** https://github.com/dbgnvan2/podcast-tracker-dashboard
- **Data store:** SQLite — **one DB per investigation profile**. The default `seo-geo` profile uses `~/.hermes/podcast_tracker.db`; others use `~/.hermes/db/podcast_<name>.db`. The active profile is resolved via `profiles.py`.
- **Transcripts:** plain-text files in `~/.hermes/transcripts/` (shared across profiles; keyed by YouTube id)

### Investigation profiles (load-bearing)
All topic-specific criteria are externalized into **profiles** (`~/.hermes/profiles/<name>.json`), so the tool works for any topic (SEO/GEO, Zone 2 training, stock trading…). A profile bundles `search_queries`, `curated_channels`, `channel_bonus`, `keywords`, filters (`min_views`/`min_duration_sec`/`max_duration_sec`/`min_days_old`), `analysis_focus` (frames the LLM analysis + term suggestions), and `digest_title`. **Switching the active profile swaps the underlying DB**, so investigations are fully isolated. `profiles.py` is the single source of truth (active pointer in `~/.hermes/profiles/_active`); every script resolves `DB_PATH` from it. The dashboard refreshes the active DB per request, so switching is live. Each script accepts the active profile by default; `podcast_scraper.py --profile NAME` overrides, and `--test` previews a profile's reach without writing.

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
├── profiles.py                 # Investigation profiles: swappable search packages (queries/channels/keywords/filters/focus) — one DB per profile
├── podcast_scraper.py          # Search YouTube → score → upsert into videos table
├── fetch_transcripts.py        # Drain 'requested' queue → yt-dlp subs → transcripts (+segments)
├── analyze_transcripts.py      # AI Intelligence: transcript → key_points + ai_analysis (LLM)
├── generate_digest.py          # Weekly "best of" markdown digest from analyzed videos
├── overnight_pipeline.py       # Patient runner: fetch → analyze → digest, loops past 429 cooldown
├── dashboard_server.py         # Stdlib HTTP server + inline SPA + JSON API + --migrate/--reconcile
├── run.sh                      # Launch dashboard + open browser (GUI-first entry point)
└── test_app.py                 # Test suite (temp DBs, mocked LLM) — run: python3 test_app.py
```

### Data locations (outside the repo, NOT committed)
- `~/.hermes/podcast_tracker.db` — the SQLite database
- `~/.hermes/transcripts/` — `{id}.txt` (clean text) + `{id}.segments.json` (timestamped segments for analysis); VTT intermediates are deleted
- `~/.hermes/digests/` — generated `digest_<date>.md` + `latest.md`
- `~/.hermes/logs/` — background-job logs (`fetch_transcripts.log`, `analyze_transcripts.log`, etc.)

---

## Data Model (SQLite)

- **`videos`** — one row per discovered video. PK `id` (YouTube video id). Key columns: `quality_score`, `views`, `channel_name`, `publish_date`, and `transcript_status`.
- **`transcripts`** — `video_id` PK, `full_text`, `word_count`, `file_path`.
- **`key_points`** — extracted points per video (`video_id`, `timestamp_sec`, `point_text`, `category`). Currently empty — target of the unbuilt AI-analysis stage.
- **`ai_analysis`** — per-video AI extraction (`seo_entities`, `geo_signals`, `best_quote`, `analyzed_at`). Written by `analyze_transcripts.py`.
- **`channels`** — channel registry (`channel_id` PK, `channel_name`, `handle`, `channel_url`, `video_count`, `best_score`, `curated`, `suggested`). Drives authority monitoring + emerging detection + auto-promotion. Rebuilt each scrape by `sync_channels()`.
- **`suggested_terms`** — LLM-proposed search queries (`term` PK, `source`, `created_at`, `status` = pending|accepted|rejected). Accepted terms are merged into the query list on the next scrape.
- **`runs`** — scraper run log.

New `videos` columns for discovery: `channel_id`, `is_new_channel` (emerging flag), `discovered_via` (`search`|`channel`), `views_per_day` (velocity).

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

### Discovery model (two complementary arms — keep BOTH)
- **Keyword search** (`SEARCH_QUERIES`) — the wide net that finds *new/unknown* channels. Do **not** put "podcast" in queries (it filters out educational creators like Neil Patel and pulls in off-topic literal podcasts).
- **Channel monitoring** (`CURATED_CHANNELS` + DB `channels` where `curated=1`) — guarantees known authorities. Curated videos bypass the view/age gates.
- **Emerging detection** — a video from a `channel_id` never catalogued before is flagged `is_new_channel`; surfaced in the Discovery tab.
- **Velocity** — `views_per_day` feeds a 0.10-weight term in `quality_score` and powers the Candidates "🔥 Trending" sort, so breakouts surface early.
- **Auto-promotion** — `sync_channels()` flags a non-curated channel `suggested` once it has ≥2 videos scoring ≥0.6; the user promotes it (→ monitored) from the Discovery tab.
- **Query freshening** — `podcast_scraper.py --suggest-terms` asks an LLM to mine top titles for new search terms; the user accepts/dismisses them in the Discovery tab; accepted terms join the next scrape.

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

# AI-analyze obtained transcripts → key_points + ai_analysis
python3 analyze_transcripts.py            # add --force to re-analyze, --id=VIDEO for one

# Build the weekly "best of" digest
python3 generate_digest.py --days=7

# Patient overnight runner: fetch → analyze → digest, looping past 429 cooldown
python3 overnight_pipeline.py

# Dashboard (GUI-first launcher: migrates, starts server, opens browser)
./run.sh
# or: python3 dashboard_server.py   → http://localhost:9091
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
| GET | `/api/intelligence` | Analyzed videos with `seo_entities`, `geo_signals`, `best_quote`, `key_points` |
| GET | `/api/digest` | Latest digest markdown (`~/.hermes/digests/latest.md`) |
| GET | `/api/discovery` | Emerging videos (new channels), suggested channels, suggested search terms |
| POST | `/api/request_transcribe` | `{id}` → set status `requested` |
| POST | `/api/unrequest` | `{id}` → set status `not_requested` |
| POST | `/api/process_queue` | Background-launch `fetch_transcripts.py` (drain `requested`) |
| POST | `/api/analyze` | Background-launch `analyze_transcripts.py` (analyze obtained transcripts) |
| POST | `/api/generate_digest` | Background-launch `generate_digest.py` |
| POST | `/api/suggest_terms` | Background-launch `podcast_scraper.py --suggest-terms` (LLM query freshening) |
| POST | `/api/promote_channel` | `{channel_id}` → mark a suggested channel `curated` (start monitoring it) |
| POST | `/api/accept_term` / `/api/reject_term` | `{term}` → accept (use in future scrapes) or dismiss a suggested term |
| GET | `/api/profiles` | List investigation profiles + which is active |
| POST | `/api/set_profile` | `{name}` → switch active profile (migrates its DB) |
| POST | `/api/create_profile` | Create a profile from a JSON body (queries/channels/keywords/focus/filters) |
| POST | `/api/test_profile` | `{name}` → dry-run a profile's reach (counts + top channels + sample titles), no writes |

Any new endpoint the frontend calls must be added to the inline JS in the same change.

### AI Intelligence stage (`analyze_transcripts.py`)
LLM via OpenAI-compatible chat completions (stdlib `urllib`). Key/model from env or `~/.hermes/.env`: `PODCAST_LLM_KEY` (falls back to `OPENAI_API_KEY`), `PODCAST_LLM_BASE` (default OpenAI), `PODCAST_LLM_MODEL` (default `gpt-4o-mini`). **Reads only the on-disk transcript** (Working Rule 0). Timestamps come from `{id}.segments.json`.

### yt-dlp / transcription notes
yt-dlp is now the brew build at `/opt/homebrew/bin/yt-dlp` (2026.x) with `curl_cffi==0.10.0` for browser impersonation. YouTube gates captions behind PO tokens and rate-limits the timedtext endpoint (HTTP 429). `fetch_transcripts.py` handles this with the `android` player client, Chrome impersonation, and 429 backoff; truly-absent captions → `not_available`, blocked/gated → retryable `error`. A heavy testing session can trigger an **IP-wide 429 cooldown** (minutes–hours); `overnight_pipeline.py` rides it out by retrying rounds with a 20-min sleep.
