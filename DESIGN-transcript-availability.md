# Design Spec — Transcript Availability vs Fetchability

> **Status:** proposal, ready to implement. Five phases, ordered by evidence-strength and blast
> radius. Phases 1–2 are ~a day; 3–5 are the feature. Nothing here adds a Python dependency or
> touches the `transcript_status` value set (so the fetcher query, dashboard queries, and
> `get_stats()` contract stay intact).
>
> **Origin:** the 2026-09-11 post-gap discovery run surfaced 179 qualified videos and an **81%**
> per-video caption-miss rate, and an audit of `~/.hermes/logs/fetch_transcripts.log` showed
> transient blocks being written as the terminal `not_available`. That is the **same class** as the
> 2026-06-02 fix log entry ("Curated channels falsely marked `not_available`") — i.e. **that fix is
> incomplete**, not missing. This spec finishes it and then makes the status mean something.

---

## 0. The problem, in one paragraph

`transcript_status` currently conflates two independent facts:

1. **Availability** — does a caption track exist for this video at all? A stable property of the video.
2. **Fetchability** — did *our* automated fetch succeed right now? A volatile property of our IP,
   the current throttle, and which yt-dlp client we used.

Because they share one column, `not_available` means "we couldn't get it", which the user cannot
action: it might mean *nothing to get* (skip forever) or *you could paste this by hand in 30
seconds* (do it now). Splitting the facts gives both a trustworthy skip decision and a worklist.

---

## 1. Evidence

### 1.1 A transient block is laundered into a terminal negative

`~/.hermes/logs/fetch_transcripts.log` (repeated ~12× in one run):

```
    blocked on client=android, backoff 30s (attempt 1)
    blocked on client=android, backoff 60s (attempt 2)
  No captions available -> not_available.
```

**Mechanism** — `fetch_transcripts.py`:

- `_run_ytdlp` (L121–153) loops `PLAYER_CLIENTS` × `MAX_429_RETRIES` (=2). On a
  `BLOCKED_MARKERS` hit it backs off and continues; it keeps only **`last_output`** — the output of
  the **final** attempt.
- `_process_queue` (L225–234) then classifies from `output` alone. When the last client's failure
  carries no marker string, control falls to `else` → `not_available`.

The blocked evidence from the earlier attempts is discarded at the moment it would have mattered.
The code's own comment at L145 already states the rule it breaks ("the in-loop retry must use the
same signal set as the status classification") — the classification just doesn't use the *sticky*
signal.

### 1.2 The population is therefore untrustworthy and unclassifiable

Live DB (`~/.hermes/podcast_tracker.db`, 2026-09-11), `transcript_status` counts:

| status | n |
|---|---|
| `not_requested` | 355 |
| `not_available` | **84** |
| `obtained` | 16 |
| `requested` | 12 |

- Breakdown of the 84 by `discovered_via`: **channel 50 / search 34**.
- `--reconcile` (`dashboard_server.py:248–296`) rescues only `discovered_via='channel'`
  (L287–291), so the **34 search-sourced rows have no re-check path at all**.
- `fetch_attempts` is **0 on all 467 rows** (including the 16 `obtained`) — the column is written
  only by `overnight_pipeline.promote_errors()`, which has never run. There is no attempt history
  to reason from.
- 210 lines in the fetcher log match 429/rate-limit markers, alongside the "No captions" verdicts.

---

## 2. Model change (the core of the spec)

Add **two columns**. Do *not* add or rename any `transcript_status` value.

| column | values | written by | meaning |
|---|---|---|---|
| `caption_availability` | `unknown` (default) \| `exists` \| `none` | **only** the availability probe | does a caption track exist, independent of whether we can fetch it |
| `transcript_source` | `ytdlp` \| `manual_panel` \| `manual_panel_partial` \| `literature_abstract` | the stage that wrote the content | provenance, so a padded or human-sourced transcript can never masquerade as a verified fetch |

> **Naming note:** `source` and `source_type` already mean something else (the literature arm's
> DOI/venue — CLAUDE.md "Multi-source documents"). Hence `transcript_source`.

### 2.1 The tightened contract (the whole point)

1. **`not_available` may only be written when `caption_availability = 'none'`.**
   When availability is `unknown`, a failed fetch is `error` (retryable) — never terminal. Unknown
   must never be treated as a negative (P1).
2. **Availability is never inferred from a failed fetch.** Only a *successful* probe that returns a
   positive/negative answer writes the column; a blocked probe leaves `unknown`.
3. Resulting matrix:

| `caption_availability` | fetch | `transcript_status` | meaning / action |
|---|---|---|---|
| `none` | — | `not_available` | skip forever — nothing exists to paste by hand either |
| `exists` | blocked | `error` | **the hand-grab queue** (dashboard view, §7) |
| `exists` | ok | `obtained` | normal (+`transcript_source`) |
| `unknown` | failed | `error` | unprobed; a probe run resolves it |

Churn is bounded by the *existing* mechanisms: `fetch_transcripts` only ever processes
`requested`, and `overnight_pipeline.promote_errors()` caps re-promotion at `MAX_FETCH_ATTEMPTS`
(=4). No new loop is introduced (P8).

### 2.2 Migration

Per Hard Constraint 3, additive + idempotent only, added to the `migrate()` list
(`dashboard_server.py`, same shape as the `fetch_attempts` entry at L179) **and** mirrored in
`podcast_scraper.init_db()`:

```
("caption_availability", "ALTER TABLE videos ADD COLUMN caption_availability TEXT DEFAULT 'unknown'"),
("transcript_source",     "ALTER TABLE videos ADD COLUMN transcript_source TEXT"),
```

Every reader must degrade on a not-yet-migrated DB via the existing `OperationalError` idiom (as
`fetch_attempts` already does) — a stale DB must never crash a run. Do not touch the legacy unused
columns (`selected`, `transcribed`, `transcribe_requested`, `transcript_summary`).

---

## 3. Phase 1 — stop laundering the transient signal

**Files:** `fetch_transcripts.py`, `test_app.py`

- `_run_ytdlp` returns `(rc, output, saw_block)` where `saw_block` is **sticky across every client
  and every attempt** within that video's fetch.
- Extract the verdict into one testable function, mirroring the `classify_discovered_video`
  precedent from the 2026-07-09 fix:

```
classify_fetch_result(saw_block, output, caption_chars, availability) -> "obtained" | "error" | "not_available"
```

Rules, in order:

1. usable captions present (and long enough) → `obtained`
2. `saw_block` **or** a `BLOCKED_MARKERS` hit in `output` → `error` (retryable)
3. captions present but shorter than `MIN_CAPTION_CHARS` → `error` (not proof of absence, L210)
4. genuinely nothing, **and** `availability == 'none'` → `not_available`
5. genuinely nothing, availability `unknown`/`exists` → `error` + print that a probe is needed

`_process_queue` calls it; both verdict branches (found-but-short and no-vtt) now route through the
same function so they cannot drift (P5).

**Acceptance:** a fixture reproducing the exact observed sequence (two `blocked` attempts, then a
final client whose output carries no marker) classifies `error`, not `not_available`.

---

## 4. Phase 2 — measure the clients before believing them

> **DONE (2026-09-14, yt-dlp 2026.08.19).** Measured with `spike_clients.py`. The hypothesis below
> was **refuted for our content**: `tv`/`ios`/`tv,ios` returned **0/12**; `android,web` / `android` /
> `default,-web` each returned **12/12 (100%)**. Chosen `PLAYER_CLIENTS = ["android","web"]` (kept over
> the faster `["android"]` for the web fallback). Full table + rationale in `LEARNINGS.md` (Phase-2 entry).

**Files:** new `spike_clients.py` (throwaway, in the spirit of `spike_multisource.py`);
`LEARNINGS.md` fix-log entry.

**Prerequisite:** `yt-dlp -U` first. Current binary is **2026.03.17** (~6 months old); PO-token and
SABR behavior in this area moves monthly, so an un-upgraded A/B measures the past. Record the
version with the results.

**Why:** public guidance (yt-dlp issue #13075, maintainer) says the subs PO-token requirement only
affects clients that already require one (`web`), and that **`ios`/`tv` subs are unaffected**;
their suggested workaround is `--extractor-args "youtube:player_client=default,-web"`.
`fetch_transcripts.py:46` currently hardcodes `["android", "web"]` — it tries the affected client
by construction. Per the PO Token Guide, for `web`/`mweb` the Subs PO token is **bound to the video
ID**, so hand-supplying tokens cannot scale to 179 videos. The only scalable lever is the client set.

**Method (matters — a biased A/B is worse than none):**

- Configs: `["android","web"]` (baseline), `["android"]`, `["default","-web"]`, `["tv"]`,
  `["ios"]`, `["tv","ios"]`.
- Videos: ~12 spanning three known groups — captioned (a handful of the 16 `obtained`),
  known-caption-less, and unknown.
- **Interleave configs per video, round-robin, and randomize the video order.** A single global
  cooldown partway through would otherwise hit whichever config ran during it and pick the winner
  for us.
- Per (video, config) record: hit/miss, seconds elapsed, and which marker appeared.
- **Abort and re-run if a `BLOCKED_MARKERS` hit appears in the baseline** — the whole test is
  invalid inside a cooldown.
- Report a table; pick the config with the highest hit rate, tie-broken on seconds/video.

**Acceptance:** a results table committed to `LEARNINGS.md` with the yt-dlp version, the chosen
`PLAYER_CLIENTS`, and the before/after hit rate on the 12 videos. This is **network-dependent, so
it cannot be covered by `test_app.py`** — flag it explicitly as such rather than implying coverage
(P10).

---

## 5. Phase 3 — the availability probe

**Files:** `podcast_scraper.py` (probe on the enrichment path), new helper module if it grows,
`test_app.py`, `dashboard_server.py` (migrate + stats).

Two tiers, cheapest first, on the discovery path where the data is already being fetched:

### Tier 1 — free (no new request)

`get_video_details()` (`podcast_scraper.py:245–269`) already runs `yt-dlp --dump-json --no-download`
for every enriched video, and that JSON carries `subtitles` and `automatic_captions`
(documentedly part of the info dict — `--list-subs` is defined as
`--print subtitles_table --print automatic_captions_table`).

- non-empty (for `--sub-langs en.*`, or any language) → `exists`
- empty → **candidate** `none`, escalate to Tier 2

> **Critical caveat:** with a PO-token client, yt-dlp *discards* gated subs ("will be discarded
> since they are not downloadable as-is") and under-reports, regenerating exactly the false
> negatives we are removing. Tier 1 is only honest when read through the client chosen in Phase 2.
> That is why Phase 2 blocks Phase 3.

### Tier 2 — panel-faithful (only for Tier-1 `none` candidates)

The watch page's initial data contains the transcript panel renderer / `getTranscriptEndpoint`
**only when a transcript exists** — literally what the "…more → Show transcript" button is wired
to. Absent → the panel would not show one either → `none`.

- Obtain the page HTML via the hardened transport, not a new one: **do not add an HTTP client**.
  Use `yt-dlp --skip-download --write-pages --output <tmp>/<id> <url>` and read the dumped page(s),
  keeping our own code stdlib-only (Hard Constraint 1).
- **Calibrate before trusting the signature** (this repo has been burned by plausible guesses):
  verify the marker on at least one known-captioned video and one known-caption-less video, and
  record both in the fix log. A marker string that has not been calibrated is a guess.
- Cost is bounded: Tier 2 runs only for videos whose Tier-1 read came back empty.

### Writing rules

- `exists` / `none` only from a **successful** probe. A failed or blocked probe leaves `unknown` —
  never write a negative from a failure (P1).
- Announce per run (P2): `Availability: N exists / M none / K unknown (probe failed on J)`.
- Reuse the same client selection as the fetcher (P5 — one client decision, not two).

### Deferred (explicitly out of scope)

**Tier 3 — `youtubei/v1/get_transcript`**, the panel's own data source (the call the browser makes
when the button is clicked; a different endpoint from the throttled `api/timedtext`). It is the
definitive "would the side panel populate?" check and the likely fix if Tier 1+2 prove unreliable.
Deferred because it needs the `params` + visitor/session plumbing and an impersonating transport.
Document it as the named escalation path so the next person doesn't rediscover it.

---

## 6. Phase 4 — the manual panel import (`ingest_manual.py`)

New file, stdlib-only, for the one case the automated path cannot win: availability exists, fetch
is blocked, and a human can copy the side panel in seconds. The user's manual method is a
**legitimate source** (the panel renders the same caption track) — and the panel is virtualized, so
it is also easy to capture **partially**. Both facts must be handled.

```
python3 ingest_manual.py --id=VIDEO_ID --file=pasted.txt [--replace] [--allow-partial]
```

- **Preconditions:** the video row exists; `caption_availability = 'exists'` (refuse otherwise, and
  say why — this is the honesty gate, not a formality); no existing `{id}.txt` unless `--replace`
  (Hard Constraint 4: never overwrite transcripts).
- **Parse** the panel paste line-wise with `^\s*(\d{1,2}:\d{2}(?::\d{2})?)\s*(.*)$`. Handle both
  paste shapes: a timestamp line with the text on the **following** line, and timestamp+text on one
  line. Strip zero-width characters, collapse whitespace, drop exact-duplicate adjacent lines.
- **Normalize with the existing rolling-caption logic** (`dedup_rolling`, `fetch_transcripts.py:98`)
  rather than inventing a second cleaning path — one normalization means downstream key-point
  timestamps stay consistent (P5).
- **Validate completeness mechanically:** `last_timestamp / duration_seconds >= 0.95`
  (`duration_seconds` is already on the row). If it fails, write nothing and print both numbers so
  the user can decide. `--allow-partial` proceeds but records
  `transcript_source = 'manual_panel_partial'` — never silently.
- **Write** exactly what the fetcher writes, so nothing downstream is special-cased:
  `{id}.txt` (plain text), `{id}.segments.json` (`[{start, text}]`), the `transcripts` row
  (`video_id, file_path, full_text, word_count`), and the video update
  (`transcript_status='obtained'`, `transcribed_date`, `transcript_source='manual_panel'`).
- **Announce** (P2): words, segment count, first/last timestamp, duration ratio, files written.
- **Idempotent** (P8): same paste twice → same result, no duplicate rows.

**Governance:** this amends Working Rule 0. The rule's intent — *never fabricate, never let a
machine-scraped snapshot stand in as a verified transcript* — is preserved exactly. A human-pasted,
duration-validated transcript is permitted **with provenance and a completeness check**; the
machine-read-browser-DOM path stays prohibited. Record the amendment in CLAUDE.md; do not quietly
violate the rule.

---

## 7. Phase 5 — classify the 84 (one-time, then measured)

**Order matters: probe first, then reconcile.**

1. Run the Phase-3 probe over the 84 `not_available` rows (+ the 34 stranded search ones).
2. **Then** re-queue — a new flag, e.g. `python3 dashboard_server.py --reconcile --include-search-unavailable`,
   which widens the existing re-queue (L287–291) from `discovered_via='channel'` to any row where
   `caption_availability != 'none'`, incrementing `fetch_attempts` and respecting
   `MAX_FETCH_ATTEMPTS` so it cannot loop (P8).
3. Report the split: how many are `none` (honest skips) vs `exists` (false negatives the code
   produced). That number is the empirical answer to "were these real?", and it belongs in the
   fix-log entry.

### Dashboard (same change — CLAUDE.md requires the inline JS to ship with any new endpoint)

- **New "Needs hand-grab" view:** `caption_availability='exists' AND transcript_status IN ('error','not_available')`
  — the paste worklist, ordered by `quality_score`.
- Show `transcript_source` as a badge on the Transcribed tab (yt-dlp / manual / paper) so
  provenance is visible in the UI, not just the DB.
- `get_stats()`: add an `availability_unknown` count so the unprobed fraction of the corpus is
  visible rather than implied.

---

## 8. Test plan (P10 fix→test map)

| phase | test | in `test_app.py`? |
|---|---|---|
| 1 (highest impact — most likely to regress) | `classify_fetch_result`: blocked-then-clean → `error`; clean-no-vtt + `none` → `not_available`; clean-no-vtt + `unknown` → `error`; short-but-blocked → `error`; **regression fixture reproducing the observed log sequence** | yes |
| 2 | live A/B on 12 videos — results table in `LEARNINGS.md` | **no — flag as network-dependent, never imply coverage** |
| 3 | probe with mocked yt-dlp output: `subtitles` non-empty → `exists`; empty + panel absent → `none`; page read failed → `unknown` (never `none`) | yes |
| 4 | importer fixtures: both paste shapes; partial rejected at the 0.95 boundary; `--replace` guard; refuses when availability ≠ `exists`; idempotent re-run | yes |
| 5 | reconcile only re-queues when `availability != 'none'`; respects the attempt cap | yes |

Also re-run the checklist items this touches: **1** (all external calls hardened — the client change
applies to every `yt-dlp` sibling, P5), **2** (availability/per-run drop counts announced),
**3** (transient can no longer reach a terminal verdict anywhere in the ladder), **6** (ground-truth:
`obtained` still requires a real transcript row + file), **8** (dirty-state: probe twice, re-import
twice), **10** (this table).

---

## 9. Risks & non-goals

**Risks**
- *Probe returns a false `none`* → the worst outcome, since it makes a captioned video skippable
  forever. Mitigated by Tier-2 calibration against two ground-truth videos, and by Tier 1 being
  read only through the Phase-2 client.
- *The `error` pool grows* → bounded by the existing attempt caps; the "Needs hand-grab" view makes
  it a worklist instead of an invisible failure.
- *Tier 2 cost* → only for Tier-1 `none` candidates, not every video.
- *A partial manual paste is worse than none* → the 0.95 duration gate + explicit provenance.

**Non-goals (deliberately excluded)**
- Proxies, and **never account cookies** — the `youtube-transcript-api` docs state YouTube will
  eventually permanently ban an account used that way (also documented in issue #511).
- Hosted transcript APIs (TranscriptAPI.com / Supadata / Dumpling AI / SerpApi) — a real option,
  HTTP-only so stdlib-compatible, but a paid dependency. Revisit **after** Phase 2's numbers.
- Automated panel scraping / browser-DOM reads — stays prohibited by Working Rule 0, for the reason
  the public `steipete/summarize` changelog documents: the panel path silently yields another
  video's or a truncated transcript.
- Tier 3 (`get_transcript`) — named as the escalation, not built here.

---

## 10. Rollout order

1. **Phase 1** — sticky block flag. Smallest change, fixes an active data-corruption path. Ship first.
2. **Phase 2** — `yt-dlp` upgrade + client A/B. Blocks Phase 3 (probe honesty).
3. **Phase 3** — availability probe + migration + stats.
4. **Phase 4** — `ingest_manual.py` + Working Rule 0 amendment.
5. **Phase 5** — probe the 84, re-queue with `--include-search-unavailable`, record the split.
6. **Governance close-out:** CLAUDE.md (state machine + Rule 0 amendment + new commands in
   `help.md`), `LEARNINGS.md` (fix-log entry per the house format: Issue → Root cause → What would
   have caught it → Fix → Pattern; pattern P1/P5, extending checklist item 3 to *"can a transient
   signal reach a terminal verdict anywhere in the attempt ladder, not just on the final attempt?"*).

---

## Appendix A — how to reproduce the finding

```bash
# the laundering sequence
grep -nE "blocked on client|No captions available" ~/.hermes/logs/fetch_transcripts.log | tail -35

# the population
sqlite3 ~/.hermes/podcast_tracker.db \
  "SELECT transcript_status, COUNT(*) FROM videos GROUP BY 1;"
sqlite3 ~/.hermes/podcast_tracker.db \
  "SELECT discovered_via, COUNT(*) FROM videos WHERE transcript_status='not_available' GROUP BY 1;"
sqlite3 ~/.hermes/podcast_tracker.db \
  "SELECT COALESCE(fetch_attempts,0), COUNT(*) FROM videos GROUP BY 1;"
```

## Appendix B — the run that prompted this (2026-09-11)

14 queries × `max_videos_per_query=20` + 24 channel fetches → 525 unique found → 416 enriched
(109 cached) → **179 passed filters** → all 179 scored. Per-video cost in the scoring phase averaged
~22–25 s, of which the **145 that failed (81%)** accounted for nearly all of it (a cache hit is
~0 s, a live hit ~4 s; a miss burns the full retry ladder). Network-miss rate *by run*:

| run | qualified | title-only (miss) | got transcript |
|---|---|---|---|
| 2026-07-07 | 281 | 223 (79%) | 58 |
| 2026-07-08 | 95 | 83 (87%) | 12 |
| 2026-09-11 | 179 | **145 (81%)** | 34 |

**Sampling note (worth keeping):** the first ~34 of the 179 were the videos that already had a local
transcript, so they were instant cache hits and nearly all succeeded. An early-window sample mid-run
therefore looked like a ~48% miss rate. The steady-state figure once the uncached majority is
reached is **81%** — i.e. this run is squarely in line with July, and much worse than the mid-run
sample suggested. Never quote a rate from a partial run; the cache-hit prefix biases it.

So the miss rate is **systematic, not a bad day** — and per Phase 1, a share of those misses are
being recorded as permanent when they are not.
