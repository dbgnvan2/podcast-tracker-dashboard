# Learnings from Fixes — failure patterns & review checklist

> **What this is:** institutional memory of *how this codebase has failed*, so the
> same **class** of bug gets caught in review instead of in production. Every time
> a real bug is fixed, add an entry under "Fix log" and fold any new rule into the
> "Review checklist". Read the checklist before reviewing or writing discovery/
> fetch/scoring code.

---

## The recurring failure patterns

Most bugs here are instances of a few patterns. When reviewing, actively ask:
**"How might this fail? Which of these patterns applies?"**

### P1 — Transient failure recorded as a permanent negative
A temporary block (HTTP 429, missing PO token, rate-limit) gets written as a
*definitive* "no". Then the bad verdict sticks forever.
- **Ask:** Is this "no result" actually "couldn't get a result *right now*"? Could a
  retry succeed? Am I about to persist a terminal status from a transient error?
- **Rule:** Transient failures → a *retryable* state (`error`), never a terminal one
  (`not_available`/deleted). Provide a way to re-check terminal negatives.

### P2 — Silent drop on failure
A function returns `None`/`[]` on failure and the caller silently `continue`s, so
data vanishes with **no signal**. "Returned empty" is indistinguishable from
"genuinely nothing there".
- **Ask:** If this call fails mid-batch, what happens? Would I ever know? Can I tell
  "found nothing" apart from "the call failed"?
- **Rule:** Harden the call (see P5) *and* surface drops — log/count what was
  excluded and why. A run that drops 40 of 125 items must say so.

### P3 — Narrow-scope assumption that silently excludes
Only one source/tab/field/path is consulted, silently missing the rest
(only `/videos` not `/streams`; only the title not the transcript; only keyword
search not channel monitoring).
- **Ask:** What does this silently exclude? Is this the *only* place the data can
  live? Have I enumerated the sources/tabs/fields, or assumed one is complete?
- **Rule:** Enumerate sources explicitly; dedupe across them. Treat "I only looked
  in one place" as a bug until proven complete.

### P4 — Hardcoded constant encoding a topic/time/scope assumption
A magic literal bakes in a decision that should be configuration
(`"2026-01-01"` cutoff, the word `"podcast"` in queries, view/score thresholds,
all the SEO-specific values before profiles existed).
- **Ask:** Does this literal encode a domain/time/business decision? Will it be
  silently wrong next year or for another topic?
- **Rule:** Promote such constants to profile/config. Grep for literal years,
  dates, topic words, and thresholds embedded in logic.

### P5 — Inconsistent robustness across sibling calls
One external call is hardened (impersonation + retry + backoff) but its siblings
aren't, so load/rate-limits hit the weak one.
- **Ask:** Are there other functions of the *same kind* as the one I just fixed?
  Do they all share the hardening?
- **Rule:** A fix to one external call reveals a **class**. Apply it class-wide in
  the same change. (Here: `_fetch_channel_tab`, `get_video_details`,
  `search_youtube` must all impersonate + retry on 429.)

### P6 — Trusting a derived/status field without reconciling to ground truth
A status flag is believed without checking the artifact it claims
(`obtained` with no transcript row; "8 transcribed" that doesn't reconcile).
- **Ask:** Does this status reflect a real artifact (file/row) that exists *now*?
  What proves it?
- **Rule:** Verify status against the artifact. Provide a `reconcile` that
  re-derives truth from reality and never hand-edit rows to fake state.

### P7 — A ranking/scoring proxy that's gameable or misaligned
A signal that's a proxy at computation time dominates the score and rewards the
wrong thing (title-only keyword density rewarded keyword-stuffing over genuine
authority).
- **Ask:** What input scores *high for the wrong reason*? Is a proxy (computed
  before the real data exists) over-weighted? Does more-of-the-right-thing
  monotonically increase the score?
- **Rule:** Down-weight proxies; weight signals you trust (authority, engagement).
  Write an adversarial test: a "looks-good-but-wrong" input must score lower.

---

## Review checklist (run before merging discovery/fetch/scoring changes)

1. **External calls:** Does every `yt-dlp`/HTTP/LLM call impersonate + retry +
   back off on 429/timeout? Are *all siblings* hardened, not just the one in scope? (P5)
2. **Failure visibility:** On partial failure, is anything logged/counted, or does
   data silently disappear? Can "empty" be told apart from "failed"? (P2)
3. **Transient vs terminal:** Is any retryable failure being written as a permanent
   negative status? (P1)
4. **Scope completeness:** Have all sources/tabs/fields been enumerated, or is one
   path silently assumed complete? (P3)
5. **Hardcoded assumptions:** Any literal date/year/topic-word/threshold in logic
   that should be config? (P4)
6. **Ground-truth check:** Is a status flag trusted without verifying the artifact
   exists? Is there a reconcile path? (P6)
7. **Scoring adversarial test:** What scores high for the wrong reason? Is a
   compute-time proxy over-weighted? (P7)
8. **Concurrency:** Could a background script writing the DB collide with a
   dashboard read (SQLite "database is locked")? (see Open risks)

---

## Open risks (found by review, not yet bitten)

- **SQLite concurrency:** the dashboard reads while background subprocesses write
  the *same* DB. Mitigated by Python's default `sqlite3.connect(timeout=5.0)` (a 5s
  busy-wait), and writes here are short — so low risk. Only a >5s write transaction
  would surface "database is locked". Raise the `timeout=` if scrapes ever batch
  writes. (verified: not as severe as first flagged — confirm claims, don't assume.)
- **Recent-N-per-channel window:** monitoring fetches only the most recent
  `max_videos_per_channel` per tab; a channel that posts many Shorts could push an
  evergreen long-form out of the window. (P3 tradeoff — logged here so it's not silent.)
- **Broad `except (..., Exception)`** in `_fetch_channel_tab`/`fetch_transcript`:
  intentional fault-tolerance, but swallows *all* errors including bugs. Consider
  logging the swallowed exception. (P2)

---

## Fix log

Newest first. Format: **Issue → Root cause → What would have caught it → Fix → Rule.**

### 2026-06-03 — LLM call not hardened like the yt-dlp calls (found by review, pre-emptive)
- **Issue:** `call_llm` (analyze/report) made a single `urlopen` with no retry; a
  transient OpenAI 429/5xx or network blip would fail the call mid-run.
- **Root cause:** inconsistent robustness across external-call classes — every
  `yt-dlp` call was hardened (retry/backoff/impersonation) but the LLM call wasn't (P5).
- **What would have caught it:** the P5 sweep — "are ALL external calls of this kind
  hardened, or just the one that broke?" This was found by the learning-qa review, not
  by a production failure.
- **Fix:** `call_llm` retries with backoff on 429/5xx/network (3 attempts), raising
  on non-retryable errors so callers' handling is unchanged.
- **Rule:** P5 → checklist 1. A class of external call is only as reliable as its
  weakest member.

### 2026-06-03 — Premiered/long-form video silently missing from discovery
- **Issue:** A monitored channel's video (Neil Patel's 64-min "AEO/GEO vs SEO")
  never appeared despite re-running discovery.
- **Root cause (two layered):** (a) channel monitoring scanned only `/videos`, but
  the video was a premiere on `/streams` (P3); (b) after that was fixed, it *still*
  dropped because `get_video_details` had no impersonation/retry and got 429'd
  during the ~125-video bulk enrichment, returning `None` → silently skipped (P2, P5).
- **What would have caught it:** asking "what does scanning only `/videos` exclude?"
  (P3) and "is `get_video_details` hardened like the channel fetch?" (P5); a run
  summary that logged "dropped N videos during enrichment" (P2).
- **Fix:** multi-tab fetch (`/videos`+`/streams`+`/podcasts`, deduped); hardened
  `get_video_details` (android client + impersonation + 429 backoff); added
  `--add=VIDEO_ID` to capture a specific video via the real path. Then found and
  fixed the same weakness in `search_youtube` during review (P5).
- **Rule:** P3, P5 → see checklist 1, 4.

### 2026-06-03 — Authorities (Neil Patel) ranked below keyword-stuffers
- **Issue:** A genuine expert scored ~0.66 while a keyword-dense interview channel
  topped the list.
- **Root cause:** `kw_score` was computed from the **title only** at scrape time
  (transcripts unavailable then) and weighted 0.25; channel authority was a weak
  multiplier that couldn't overcome a stuffed title (P7).
- **What would have caught it:** the adversarial question "what scores high for the
  wrong reason?" — a keyword-stuffed title with no real depth (P7).
- **Fix:** authority became a strong additive term (0.30, floor for monitored
  channels); kw_score cut to 0.15; authority follows channel membership, not how a
  video was found; `--rescore` to recompute. Test: authority beats keyword-stuffing.
- **Rule:** P7 → see checklist 7.

### 2026-06-03 — High-value 2025 content excluded
- **Issue:** Only 1 of 20 results for "AEO/GEO vs SEO" were captured; 100k+-view
  2025 explainers were absent.
- **Root cause:** a hardcoded `upload_date < "2026-01-01": continue` discarded the
  entire prior year; queries lacked the "X vs Y" comparison phrasing of the genre (P3, P4).
- **What would have caught it:** "does this literal date encode an assumption that's
  silently wrong?" (P4); validating coverage against a real search (the exercise
  that found it).
- **Fix:** `min_publish_date` became a profile setting (default 2025-01-01); added
  comparison queries; lowered `min_views`; raised channel depth.
- **Rule:** P4 → see checklist 5. Also: **validate coverage against an external
  search**, don't assume the pipeline is complete.

### 2026-06-02 — Curated channels falsely marked `not_available`
- **Issue:** 8 monitored-channel videos (with real captions) were hidden as
  "no captions".
- **Root cause:** a transient PO-token/429 block during fetch was recorded as the
  terminal `not_available` status (P1).
- **What would have caught it:** "is this 'no captions' actually 'blocked right now'?"
  (P1).
- **Fix:** fetcher distinguishes blocked (→ retryable `error`) from truly absent
  (→ `not_available`); `reconcile` re-queues curated false negatives.
- **Rule:** P1 → see checklist 3.

### (earlier) — Fabricated transcripts / fake `obtained` rows (the prototype)
- **Issue:** invented key points; rows marked transcribed with no transcript.
- **Root cause:** trusting/forcing a status without the underlying artifact; analyzing
  from titles not transcripts (P6 + the #0 no-fabrication rule).
- **Fix:** analysis reads only on-disk transcripts; report citations validated
  against the real source list; `reconcile` resets unbacked `obtained`.
- **Rule:** P6 → see checklist 6, and CLAUDE.md Working Rule 0.
