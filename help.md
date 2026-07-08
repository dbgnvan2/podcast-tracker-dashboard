# Podcast Tracker Dashboard — Help

## Overview

The Podcast Tracker Dashboard discovers, transcribes, analyzes, and synthesizes YouTube videos (and academic literature) related to a topic you care about. You define the topic via a **Profile**, and the app runs a pipeline that ends in two outputs:

- **Digest** — a ranked markdown list of every new analyzed video, with key quotes and key points. Think of it as a curated newsletter you generate on demand.
- **Advisor Report** — an LLM-synthesized, cited briefing across multiple transcripts. Every claim traces back to a real source timestamp.

The app runs locally. You start it with `python3 dashboard_server.py` and open the dashboard in your browser (default: `http://localhost:8765`).

---

## The Pipeline

Each tab in the dashboard corresponds to a stage:

```
Discovery → Candidates → Transcribe → Intelligence → Digest → Report
```

| Stage | Tab | What happens |
|---|---|---|
| 1 | **Discovery** | Searches YouTube (and optionally academic sources) for videos matching your keywords |
| 2 | **Candidates** | Ranked list of found videos; you review and skip/approve |
| 3 | **Requested** | Videos queued for transcription |
| 4 | **Transcribed** | Videos with transcripts obtained; ready to analyze |
| 5 | **Intelligence** | AI analysis: key points, entities, GEO signals, best quote extracted |
| 6 | **Digest** | Generate a markdown digest of undigested analyzed videos |
| 7 | **Report** | Generate a full LLM-synthesized advisor report from transcripts |

---

## Each Step Explained

### 1. Discovery — Run Discovery

**What it does:** Runs the scraper against YouTube (and optionally EuropePMC for literature). New videos are scored by quality (views, velocity, channel authority, keyword match) and added to the Candidates list.

**What to watch:** The discovery log streams in the UI. If no new videos appear, check your profile's `search_queries` and `min_views` threshold — they may be too restrictive.

**Trigger:** Click **Run Discovery** in the Discovery tab.

---

### 2. Candidates

The scored, ranked list of discovered videos. Each row shows quality score, title, channel, views, and publish date.

- **Skip** — permanently hides the video from future runs. Use it for off-topic or low-quality content. See [Skip / Dismiss](#skip--dismiss) below.
- Videos approved (not skipped) flow into the queue automatically.

---

### 3. Transcribe — Process Queue

**What it does:** Downloads audio and extracts transcripts for all queued videos. Uses yt-dlp + Whisper (or YouTube's own captions when available).

**What to watch:** Transcription is the slowest step. A 60-minute video can take 5–15 minutes on CPU. The log streams live. Don't click Process Queue again while it's running — the button disables itself to prevent duplicate jobs.

**After completion:** Videos move from Requested → Transcribed.

---

### 4. Intelligence — Analyze Transcripts

**What it does:** Sends each transcript to your configured LLM. Extracts:
- Key points with timestamps
- SEO entities (brands, tools, concepts mentioned)
- GEO signals (AI overview mentions, search feature names)
- Best quote

**What to watch:** Requires a valid LLM API key configured in the LLM Providers panel. Each video costs one LLM call. Only videos with `transcript_status='obtained'` AND no existing `ai_analysis` row are processed.

---

### 5. Digest

**What it does:** Builds a markdown digest from analyzed videos that have **not yet been digested**. After generating, each included video is stamped `digested_at` so it won't appear in future digests.

**Pending count:** The digest tab shows how many new analyzed videos are waiting (e.g., "12 new since last digest"). This updates on page load and after each digest run.

**Trigger:** Click **Generate Digest**.

**Force mode:** To re-include already-digested videos (e.g., to regenerate after editing the template), run from the command line:

```bash
python3 generate_digest.py --force
```

**From the command line:**
```bash
python3 generate_digest.py              # undigested only (default)
python3 generate_digest.py --force      # include all analyzed videos
python3 generate_digest.py --limit=20   # cap at 20 videos
```

Output is written to `~/.hermes/digests/digest_YYYY-MM-DD.md` and `latest.md`.

---

### 6. Report

**What it does:** Sends transcript content (key points + excerpts) to a synthesis LLM and produces a structured report with:
- Executive Key Ideas (5–7 takeaways)
- Detailed Analysis per idea (why it matters, how to implement, details)
- Citations linking every claim back to a specific source video and timestamp

**Filters available in the UI:**
- **From / To** — restrict to videos transcribed within a date range
- **Channel** — restrict to a single channel (populated from your analyzed content)
- **Max sources** — cap how many transcripts feed the report (2–12)

**From the command line:**
```bash
python3 generate_report.py --n=8
python3 generate_report.py --from=2026-06-01 --to=2026-06-30
python3 generate_report.py --channel="Neil Patel"
```

Output is written to `~/.hermes/reports/report_YYYY-MM-DD[_suffix].md` and `latest.md`.

**What to watch:** Report generation requires the synthesis LLM (configured separately from the bulk LLM in the LLM Providers panel). It takes 20–60 seconds for a typical 8-source report. If the report shows a key idea without a citation, the LLM ignored the rules — this is rare but re-running usually fixes it.

---

## Profiles

A **profile** defines one investigation topic. Each profile has its own:
- SQLite database (no cross-contamination between topics)
- Search queries, keyword filters, and view thresholds
- Digest and report directories

**Switching topics:** Open the Profiles panel in the dashboard. Click the profile name to switch. The page reloads and all data reflects the selected topic.

**Creating a new profile:** Use the dashboard or the command line:
```bash
python3 profiles.py create my-topic --label="My Topic" --queries="query one,query two"
```

**Editing settings:** In the dashboard, open Settings for the active profile. You can tune:
- `min_views` — minimum view count for a video to appear as a candidate
- `min_publish_date` — ignore older videos
- `search_queries` — comma-separated queries run against YouTube
- `analysis_focus` — text injected into the LLM prompt to focus analysis

---

## Enrichment Cache

When the scraper visits a channel or video, it caches metadata to avoid re-fetching on every run. The cache has two time thresholds:

- **Recently seen videos** (updated within ~7 days): skipped to stay under YouTube rate limits.
- **Stale videos** (not updated in 30+ days): re-fetched on the next discovery run to capture view count changes.

You don't need to manage this manually. If you want to force a full re-fetch, delete the database and re-run discovery (this loses all transcript and analysis data).

---

## Skip / Dismiss

**Skip** (the button on the Candidates tab) sets `dismissed=1` on the video row. Dismissed videos:
- Are hidden from the Candidates list.
- Are never included in transcription, analysis, digest, or report.
- Are not re-discovered on future runs (the scraper skips known-dismissed IDs).

There is no "un-skip" button in the UI currently. To restore a dismissed video, update it directly in the database:
```bash
sqlite3 ~/.hermes/podcast_tracker.db "UPDATE videos SET dismissed=0 WHERE id='VIDEO_ID'"
```

---

## Troubleshooting

**"No LLM API key configured"** — Go to LLM Providers in the dashboard and add a provider (OpenAI, Anthropic, or any OpenAI-compatible endpoint). Set both a Bulk provider (for per-video analysis) and a Synth provider (for the advisor report). They can be the same provider.

**Transcription hangs or produces garbage** — Check available disk space and RAM. Whisper large-v3 needs ~10 GB RAM. Switch to `whisper-base` or `whisper-small` in your profile settings for faster (less accurate) transcription.

**Discovery finds nothing new** — Check `min_views` (may be set too high), `min_publish_date` (may exclude recent content), and whether your search queries are specific enough. YouTube's search API returns at most ~50 results per query.

**Report cites source numbers that don't exist** — The LLM invented a citation. The renderer drops invalid source numbers automatically, so the report is still safe to read. Re-generate to get a cleaner run.

**"No new analyzed videos since last digest"** — All analyzed videos have already been included in a previous digest. Either analyze new videos, or use `--force` to re-digest all.

**The digest or report file is empty / shows an error** — Check the job log (visible in the dashboard after clicking Generate) for Python errors. Common causes: database locked by another process, LLM key expired, or a write permission issue on the digest/reports directory.

**Tests failing after a schema change** — Run `python3 test_app.py` from the project directory. If you see column count mismatches in INSERT statements, the `SCHEMA` constant in `test_app.py` may be out of sync with the migration in `dashboard_server.py`.
