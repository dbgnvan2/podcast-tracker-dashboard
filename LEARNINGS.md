# Learnings — Podcast Tracker Dashboard (failure patterns & fix log)

> **What this is:** institutional memory of how *this* codebase has failed, so the same
> **class** of bug is caught in review instead of production. After fixing any real bug,
> add a **Fix log** entry (below) and, if it's a new generic pattern, fold it into the
> global catalogue.
>
> **Generic pattern catalogue (P1–P16) + full reasoning live globally** at
> `~/.claude/standards/learnings.md` (auto-loaded by Claude Code for every repo). This file
> keeps the **podcast-tracker-specific** checklist, open risks, and fix log. Read the
> checklist before reviewing or writing any **discovery, fetch, scoring, analyze, or
> report/digest** code.

---

## Review checklist (run before merging discovery / fetch / scoring / analyze / report changes)

1. **External calls (P5):** does every `yt-dlp`/HTTP/LLM call impersonate + retry + back off
   on 429/timeout? Are *all siblings* hardened, not just the one in scope? (`_fetch_channel_tab`,
   `get_video_details`, `search_youtube`, and `call_llm` must all be robust.)
2. **Failure visibility (P2):** on partial failure, is anything logged/counted, or does data
   silently disappear? Can "found nothing" be told apart from "the call failed"? A run that
   drops N of M items must say so.
3. **Transient vs terminal (P1):** is a retryable failure (429, PO-token block, timeout) being
   written as a permanent negative (`not_available`, deleted)? Keep the retryable `error` path
   and a `reconcile` to re-check terminal negatives.
4. **Scope completeness (P3):** have all sources/tabs/fields been enumerated? (`/videos` **and**
   `/streams` **and** `/podcasts`; transcript body not just title; channel monitoring **and**
   keyword search; every literature source.) **Identity/guard keys:** does an "already-handled"
   guard key on the *natural identity* of the thing? Too narrow (id, when a re-upload gets a new
   id) and too broad (title alone, colliding across creators) are the **same P3 bug** — a
   re-upload is the *same channel* re-posting, so key on `(channel_id, normalized_title)` (P3/P5).
5. **Hardcoded assumptions (P4):** any literal date/year/topic-word/threshold in logic that
   belongs in a profile/config? (Grep for literal years, `min_views`, score weights, the word
   "podcast" in queries.)
6. **Ground-truth check (P6):** is a status (`obtained`, `transcribed`, `analyzed`) trusted
   without verifying the artifact (transcript row/file, key_points) exists *now*? Is there a
   `reconcile` path? Never hand-edit rows to fake state (Working Rule 0).
7. **Scoring adversarial test (P7):** what input scores high for the *wrong* reason? Is a
   compute-time proxy (title-only keyword density) over-weighted vs. signals you trust
   (authority, engagement)? Does more-of-the-right-thing raise the score monotonically?
8. **Dirty-state / second-run (P8):** does this read state that persists between runs (prior
   DB rows, digest history, `{id}.segments.json`)? Is there a test that pre-populates it and
   asserts prior-run content is ignored / the step is idempotent?
9. **Input starvation / size caps (P9):** for every cap in a data path (`max_videos_per_channel`,
   result limits, AI excerpt/token budgets, digest item count): on real, large data what fraction
   survives? Is the drop announced? Are fixtures big enough to make the cap bite?
10. **Fix→test map (P10):** does each fix map to a test? Is the highest-impact / most-likely-to-
    regress fix tested FIRST? Are integration-only paths (live yt-dlp / OpenAI) flagged untested
    rather than implied covered?
11. **Concurrency:** could a background script writing the DB collide with a dashboard read
    (SQLite "database is locked")? Are transactions short and is `timeout=` sane? (see Open risks)
12. **Background worker guard (P15):** does every background thread/subprocess that writes a
    shared status wrap its body in `try/except` and write an `error` status before exiting, so a
    crash can't leave the UI stuck on "running"?

> Pattern definitions (P1–P16) and the reasoning behind each item: `~/.claude/standards/learnings.md`.

---

## Open risks (found by review, not yet bitten)

- **SQLite concurrency:** the dashboard reads while background subprocesses write the *same* DB.
  Mitigated by `sqlite3.connect(timeout=5.0)` (5s busy-wait) and short writes — low risk. Only a
  >5s write transaction would surface "database is locked". Raise `timeout=` if scrapes ever batch
  writes. (Verified: less severe than first flagged — confirm claims, don't assume.)
- **Recent-N-per-channel window (P3 tradeoff):** monitoring fetches only the most recent
  `max_videos_per_channel` per tab; a channel that posts many Shorts could push an evergreen
  long-form out of the window. Logged so it's not silent.
- **Broad `except (..., Exception)`** in `_fetch_channel_tab` / `fetch_transcript`: intentional
  fault-tolerance, but swallows *all* errors including bugs. Log the swallowed exception (P2).
- **Persistent "processed" banner depends on a Python-level exit (P15 corollary).** The banner
  file is written by `process_queue`'s `try/except`. A hard `SIGKILL`/OOM of the subprocess writes
  nothing, so the dashboard keeps showing the *previous* run's banner rather than an error. Minor
  edge; would need a heartbeat/"started" marker to detect a vanished subprocess.

---

## Fix log

Newest first. Format: **Issue → Root cause → What would have caught it → Fix → Pattern.**

### 2026-07-09 — Re-uploaded transcribed talk reappeared as a fresh candidate
- **Issue:** the "don't re-process what I've transcribed" guard was keyed only by video **id**
  (`transcript_status='obtained'` on that row). A channel re-uploading the same talk under a *new*
  YouTube id produced a brand-new row that discovery happily admitted to Candidates, so the user
  would be asked to transcribe content already transcribed. (Skip, by contrast, was already
  title-keyed and immune.)
- **Root cause:** narrow-scope identity assumption — "the id is the video." An id-only guard
  silently excludes the re-upload case (P3). The two "already handled" guards were also
  inconsistent: Skip matched by title, Transcribe matched by id (P5).
- **What would have caught it:** asking "what does this guard silently let through?" and the P5
  sweep — "Skip and Transcribe are the same *class* of already-handled guard; do they match on the
  same key?" They didn't.
- **First fix over-corrected (caught by learning-qa review, pre-production):** the initial fix
  keyed the guard on **title alone**. That swung from id-too-narrow to title-**too-broad**: a
  *different* creator posting a video with a colliding generic title ("SEO in 2025", "Q&A") would
  be silently dropped from Candidates, and the drop was only an aggregate count with no per-item
  trace. **A too-narrow key and a too-broad key are the same P3 bug from opposite sides** — the
  right key is the natural identity of the thing: a re-upload is the *same channel* re-posting.
- **Final fix:** guard keyed on `(channel_id, normalized_title)` (`load_obtained_keys`), and the
  loop's per-video decision is a single testable function `classify_discovered_video` returning
  `"update"` (same id → refresh stats) / `"reupload"` (new id + channel+title already obtained →
  drop) / `"insert"`. Every drop is counted (`Re-upload dup: N`) **and logged per item** (title +
  url), so a wrongful exclusion is auditable (P2). Degrades to empty on a not-yet-migrated DB
  (`OperationalError` idiom) — never crashes a run. Tests: `TestTranscribedReupload` — same-channel
  new-id dropped, **different-channel same-title kept** (regression for the over-broad key),
  same-id → `"update"` (loop's real branch), normalization, missing-column, wiring.
- **Pattern:** P3 (narrow *and* broad scope are the same bug — key on natural identity) / P5
  (sibling guards consistent) / P2 (drop surfaced *and* per-item logged) / P10 (test the loop's
  actual decision fn, not a source-grep) → checklist 2, 4, 6, 10.

### 2026-07-08 — Spawned jobs followed a mid-run profile switch (found by learning-qa review)
- **Issue:** every background job (`podcast_scraper`, `fetch_transcripts`, `analyze_transcripts`,
  `generate_digest`, `generate_report`, `ingest_literature`) was spawned with no profile argument
  and resolved `profiles.load()` at import. Switching the active profile between clicking a run
  button and the subprocess importing would silently send the job — and the new persistent
  transcription banner — to the *wrong* profile's DB, leaving the intended queue unprocessed.
- **Root cause:** the active profile is global shared state (`profiles/_active`); the spawn
  contract never pinned the child to the profile that launched it (P3 narrow-scope / P6 trusting
  a mutable global as ground truth).
- **What would have caught it:** asking "does this background job stay scoped to the profile that
  launched it, or to whatever is active when it happens to import?" (P3/P6). Found pre-emptively
  by the learning-qa review of the feature-#2 diff, not by a production mixup.
- **Fix:** the dashboard's single spawn choke point (`Handler._spawn`) now injects
  `PTD_PROFILE=<active-at-spawn>` into every child's env; `profiles.active_name()` honors it. Fixed
  as a **class** (P5) — one place covers all six jobs — without mutating `_active`, so the
  dashboard's own view is unaffected. Tests: `TestProfiles::test_active_name_honors_ptd_profile_env`,
  `::test_spawn_pins_profile_env`.
- **Pattern:** P3/P6/P5 → checklist 1, 4, 6.

### 2026-06-03 — LLM call not hardened like the yt-dlp calls (found by review, pre-emptive)
- **Issue:** `call_llm` (analyze/report) made a single `urlopen` with no retry; a transient
  OpenAI 429/5xx or network blip would fail the call mid-run.
- **Root cause:** inconsistent robustness across external-call classes — every `yt-dlp` call was
  hardened (retry/backoff/impersonation) but the LLM call wasn't (P5).
- **What would have caught it:** the P5 sweep — "are ALL external calls of this kind hardened, or
  just the one that broke?" Found by the learning-qa review, not a production failure.
- **Fix:** `call_llm` retries with backoff on 429/5xx/network (3 attempts), raising on
  non-retryable errors so callers' handling is unchanged.
- **Pattern:** P5 → checklist 1. A class of external call is only as reliable as its weakest member.

### 2026-06-03 — Premiered/long-form video silently missing from discovery
- **Issue:** A monitored channel's video (Neil Patel's 64-min "AEO/GEO vs SEO") never appeared
  despite re-running discovery.
- **Root cause (two layered):** (a) channel monitoring scanned only `/videos`, but the video was a
  premiere on `/streams` (P3); (b) after that fix it *still* dropped because `get_video_details`
  had no impersonation/retry and got 429'd during the ~125-video bulk enrichment, returning `None`
  → silently skipped (P2, P5).
- **What would have caught it:** "what does scanning only `/videos` exclude?" (P3); "is
  `get_video_details` hardened like the channel fetch?" (P5); a run summary logging "dropped N
  videos during enrichment" (P2).
- **Fix:** multi-tab fetch (`/videos`+`/streams`+`/podcasts`, deduped); hardened `get_video_details`
  (android client + impersonation + 429 backoff); `--add=VIDEO_ID` to capture a specific video.
  Then found and fixed the same weakness in `search_youtube` during review (P5).
- **Pattern:** P3, P5 → checklist 1, 4.

### 2026-06-03 — Authorities (Neil Patel) ranked below keyword-stuffers
- **Issue:** A genuine expert scored ~0.66 while a keyword-dense interview channel topped the list.
- **Root cause:** `kw_score` was computed from the **title only** at scrape time (transcripts
  unavailable then) and weighted 0.25; channel authority was a weak multiplier that couldn't
  overcome a stuffed title (P7).
- **What would have caught it:** the adversarial question "what scores high for the wrong reason?"
  — a keyword-stuffed title with no real depth (P7).
- **Fix:** authority became a strong additive term (0.30, floor for monitored channels); kw_score
  cut to 0.15; authority follows channel membership, not how a video was found; `--rescore` to
  recompute. Test: authority beats keyword-stuffing.
- **Pattern:** P7 → checklist 7.

### 2026-06-03 — High-value 2025 content excluded
- **Issue:** Only 1 of 20 results for "AEO/GEO vs SEO" were captured; 100k+-view 2025 explainers
  were absent.
- **Root cause:** a hardcoded `upload_date < "2026-01-01": continue` discarded the entire prior
  year; queries lacked the "X vs Y" comparison phrasing of the genre (P3, P4).
- **What would have caught it:** "does this literal date encode an assumption that's silently
  wrong?" (P4); validating coverage against a real search.
- **Fix:** `min_publish_date` became a profile setting (default 2025-01-01); added comparison
  queries; lowered `min_views`; raised channel depth.
- **Pattern:** P4 → checklist 5. Also: **validate coverage against an external search**, don't
  assume the pipeline is complete.

### 2026-06-02 — Curated channels falsely marked `not_available`
- **Issue:** 8 monitored-channel videos (with real captions) were hidden as "no captions".
- **Root cause:** a transient PO-token/429 block during fetch was recorded as the terminal
  `not_available` status (P1).
- **What would have caught it:** "is this 'no captions' actually 'blocked right now'?" (P1).
- **Fix:** fetcher distinguishes blocked (→ retryable `error`) from truly absent
  (→ `not_available`); `reconcile` re-queues curated false negatives.
- **Pattern:** P1 → checklist 3.

### (earlier) — Fabricated transcripts / fake `obtained` rows (the prototype)
- **Issue:** invented key points; rows marked transcribed with no transcript.
- **Root cause:** trusting/forcing a status without the underlying artifact; analyzing from titles
  not transcripts (P6 + the #0 no-fabrication rule).
- **Fix:** analysis reads only on-disk transcripts; report citations validated against the real
  source list; `reconcile` resets unbacked `obtained`.
- **Pattern:** P6 → checklist 6, and CLAUDE.md Working Rule 0.
