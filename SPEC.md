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

---

## 9. v1 status (shipped 2026-06-01)

v1 closes the non-LLM gaps, turning the scripts into one GUI-first app:

- **`POST /api/process_queue`** + a **"⚙ Process Queue"** button on the Requested tab — launches `fetch_transcripts.py` in the background (logs to `~/.hermes/logs/fetch_transcripts.log`) and polls so rows leave the queue as they finish. (Gap #2 ✅)
- **`GET /api/transcript/{id}`** + a **"View"** button + modal on the Transcribed tab to read the verified text. (Gap #3 ✅)
- **`--reconcile`** CLI: resets fake `obtained` rows (no backing transcript) to `requested` and deletes stub transcript files. Ran once — fixed 4 fake rows + removed 3 stub files. (Gap #6 partial ✅)
- **Removed** the duplicate `youtube_podcast_scanner.py`. (Gap #6 ✅)
- **`fetch_transcripts.py` hardened**: distinguishes captions *gated/blocked* (PO token / SABR / rate limit → retryable `error`) from captions *genuinely absent* (`not_available`), so good videos are no longer silently killed.

---

## 10. v1.1 status (shipped 2026-06-01, overnight)

The full intelligence pipeline is now built and tested:

- **`analyze_transcripts.py`** — AI Intelligence stage. Reads each obtained video's verified transcript + timestamped segments, calls an LLM (OpenAI `gpt-4o-mini` by default, key from `~/.hermes/.env`), and writes `key_points` (with timestamps) + `ai_analysis` (seo_entities, geo_signals, best_quote). Reads only on-disk transcripts (Working Rule 0). (Gap #1 ✅)
- **`generate_digest.py`** — weekly "best of" markdown from analyzed videos: ranked picks with best quote + timestamped key points (deep-linked to YouTube), plus aggregated trending entities/GEO signals. Writes `~/.hermes/digests/`. (Gap #5 ✅)
- **Dashboard Intelligence + Digest tabs** — `GET /api/intelligence`, `GET /api/digest`, `POST /api/analyze`, `POST /api/generate_digest`, with "🧠 Analyze Transcripts" and "📰 Generate Weekly Digest" buttons. `migrate()` now creates `ai_analysis`.
- **`overnight_pipeline.py`** — patient runner that loops fetch → analyze → digest, promoting retryable `error`→`requested` each round and sleeping 20 min to ride out the 429 cooldown.
- **`run.sh`** — GUI-first launcher (migrate + serve + open browser).
- **`test_app.py`** — 5 tests (temp DBs, mocked LLM): VTT parse/dedup, downsample, analyze inserts, digest build, reconcile. All pass.
- **yt-dlp hardened** — brew build 2026.x at `/opt/homebrew/bin/yt-dlp` + `curl_cffi==0.10.0` impersonation; `android` client + 429 backoff; saves `{id}.segments.json` for timestamps.

**Remaining (minor):** drop the dead `transcribed`/`transcribe_requested` columns (needs a table rebuild; left for safety).

**Live blocker (transient):** YouTube returned an **IP-wide 429 cooldown** on the caption endpoint after heavy testing — captions are confirmed present (`en-orig, en` listed) but can't download until the cooldown lifts. `overnight_pipeline.py` is running to drain the queue once it clears. Current statuses: 14 `not_requested`, 7 `error` (Brand Entity SEO series + 2, all retryable).

---

## 11. v1.2 status — reliable discovery (shipped 2026-06-02)

Fixes "we only find strangers / Neil Patel never appears" **and** "channel monitoring alone misses emerging creators." Discovery is now a two-arm system with a feedback loop:

- **Channel monitoring** (`fetch_channel_videos` + `CURATED_CHANNELS`) — pulls authority uploads directly (Neil Patel, Ahrefs, Surfer, HubSpot, Google Search Central, Semrush, SEJ, Search Engine Land, Eric Siu). Curated videos bypass view/age gates. All 9 handles verified.
- **Keyword search** kept as the net for unknowns; queries de-"podcast"ed and re-pointed at AI-GEO/traffic terms.
- **Emerging detection** — `videos.is_new_channel` flags videos from never-before-seen channels (`channel_id`).
- **Velocity** — `views_per_day` adds a 0.10 term to `quality_score` (verified monotonic) and powers a Candidates "🔥 Trending" sort.
- **Auto-promotion** — `channels` registry + `sync_channels()` flag non-curated channels with ≥2 videos ≥0.6 as `suggested`; monitored channels auto-mark `curated`.
- **Query freshening** — `--suggest-terms` LLM-mines top titles for new queries → `suggested_terms` (pending → accept/reject); accepted terms join the next scrape.
- **Dashboard "Discovery" tab** — emerging videos, suggested channels (+ Monitor button), suggested terms (Accept/Dismiss + Generate). Endpoints: `/api/discovery`, `/api/promote_channel`, `/api/accept_term`, `/api/reject_term`, `/api/suggest_terms`.
- **Schema** — new `channels` + `suggested_terms` tables; new `videos` columns (`channel_id`, `is_new_channel`, `discovered_via`, `views_per_day`). All additive in `migrate()` and in the scraper's `init_db()`.
- **Tests** — 9 total (added velocity monotonicity, suggested/auto-curate, days_since). All pass.

---

## 12. v1.3 status — investigation profiles (shipped 2026-06-02)

The tool is now **topic-agnostic**. All search criteria are externalized into swappable "investigation profiles," so the same tooling runs separate investigations (SEO/GEO, Zone 2 training, stock trading, …) with full data isolation.

- **`profiles.py`** — single source of truth. Profiles are JSON under `~/.hermes/profiles/<name>.json`, bundling `search_queries`, `curated_channels`, `channel_bonus`, `keywords`, filters, `analysis_focus`, `digest_title`. Active pointer in `~/.hermes/profiles/_active`. Seeds the built-in `seo-geo` profile from the former hardcoded values.
- **One DB per profile** — `db_path_for()` maps `seo-geo` → legacy `~/.hermes/podcast_tracker.db` (data preserved) and others → `~/.hermes/db/podcast_<name>.db`. Switching profiles swaps the dataset (verified: 0 vs 104 videos across a switch, then back).
- **All scripts refactored** — `podcast_scraper.py` (criteria + `--profile` + `--test` dry-run), `fetch_transcripts.py`, `analyze_transcripts.py` (prompt framed by `analysis_focus`), `generate_digest.py` (per-profile dir + title) read the active profile. The dashboard refreshes `DB_PATH` per request, so switching is live without a restart.
- **Dashboard** — header profile dropdown (live switch), "+ New" investigation modal (queries/channels/keywords/focus + **Test** preview + **Create & switch**). Endpoints: `/api/profiles`, `/api/set_profile`, `/api/create_profile`, `/api/test_profile`. `migrate(db)` self-creates a fresh profile's schema.
- **Tests** — 12 total (added seed/active, create-switch-isolation, defaults). All pass.

---

## 13. v1.4 status — Advisor Report (shipped 2026-06-02)

A new cross-transcript deliverable: a factual, educational **advisor report** synthesized from the last N transcripts, led by an **Executive Key Ideas** section where every idea is cited to its originating source.

- **`generate_report.py`** — selects the last N analyzed transcripts, feeds the LLM each source's extracted key points (timestamped), best quote, and a transcript excerpt — all already grounded in real transcripts (Working Rule 0). The model must attribute every idea/paragraph to a supplied source number; `render()` validates those numbers against the real source list and drops any out-of-range citation, so citations can never point at something invented. Deep-links each citation to the source video at a representative timestamp. Writes to `~/.hermes/reports/<profile>/`.
- **Report structure** — overview → Executive Key Ideas (each cited) → thematic sections (each paragraph cited) → Sources list.
- **Dashboard "Report" tab** — N selector + "Generate Advisor Report" button; rendered as HTML (clickable citations) via a small markdown renderer that also upgraded the Digest tab. Endpoints `/api/report`, `/api/generate_report`.
- **Profile-aware** — framed by `analysis_focus`, per-profile reports dir.
- **Validated on real data** — produced a 5-source SEO report with 8 cited Executive Key Ideas, all citations resolving to the real James Dooley videos.
- **Tests** — 13 total (added report citation-integrity test: valid sources deep-link, invalid source numbers are dropped). All pass.
