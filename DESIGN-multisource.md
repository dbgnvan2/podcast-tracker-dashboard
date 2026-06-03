# Design Spec — Multi-Source Topic Intelligence (YouTube + Literature)

> **Status:** design proposal. Goal: let one **Topic** (a "search parameter set")
> pull from **both** YouTube and scholarly-literature sources, normalize everything
> into a common **document**, and run it through the existing shared
> analysis → digest → cited-report layers. Builds on the investigation-profiles
> architecture already in this repo (`profiles.py`) and the `biorx` repo's
> scholarly API clients.

---

## 1. The idea in one picture

```
A TOPIC (search parameter set) has two discovery arms:

  youtube  arm → queries + CHANNELS (followed creators)        ─┐
  literature arm → queries + SOURCES (databases) + AUTHORS/labs ─┤
                                                                 │  each emits
                                                                 ▼
                                    a normalized DOCUMENT { text, authority, ... }
                                                                 │
        ┌────────────────────────────────────────────────────────┘
        ▼  (shared, source-agnostic)
   analyze → key points / entities / claims · score · state machine · dashboard
        ▼
   weekly digest · cited advisor report  ← cites across BOTH arms
```

The seam is the **source adapter**: everything below the dashed line already
exists and doesn't care which arm a document came from.

---

## 2. Terminology (answering "does this make sense?")

| Concept | YouTube arm | Literature arm |
|---|---|---|
| Find candidates by query | `queries` (search terms) | `queries` (search terms) |
| **Monitored authorities** | **`channels`** (creators we always pull) | **`authors` / `labs` / `venues`** we always pull |
| **Backends to search** | (implicitly: YouTube) | **`sources`** = which databases (PubMed, bioRxiv, OpenAlex…) |
| Content to analyze | transcript | abstract + (optional) full text |
| Reach/velocity signal | views, views/day | citations, citation velocity |
| Authority signal | channel tier + monitored | venue + citation count + author h-index |

So: **a Topic has `channels` (YouTube) and `sources` (literature)** — exactly as you
framed it. The only nuance: "sources" in literature are *databases*; the closest
analog to a curated *channel* is a followed **author/lab/venue**. Both are optional.

---

## 3. The unified Topic / profile schema

A profile (`~/.hermes/profiles/<name>.json`) gains optional `youtube` and
`literature` arms. Topic-level fields (relevance keywords, analysis framing,
filters that apply to both) stay at the top. Either arm may be omitted (a
YouTube-only or literature-only Topic).

```jsonc
{
  "name": "zone2-metabolic-health",
  "label": "Zone 2 / Metabolic Health",
  "analysis_focus": "Zone 2 training, lactate metabolism, mitochondrial function, GLP-1 / metabolic health",
  "digest_title": "Best of Zone 2 & Metabolic Health",

  // shared relevance keywords (used by scoring across both arms)
  "keywords": ["zone 2", "lactate threshold", "mitochondria", "VO2 max", "GLP-1", "insulin sensitivity"],

  // ── YouTube arm ──────────────────────────────────────────────
  "youtube": {
    "enabled": true,
    "queries": ["zone 2 training explained", "lactate threshold training"],
    "channels": {                          // followed creators (always pulled)
      "PeterAttiaMD": "Peter Attia",
      "https://www.youtube.com/channel/UC...": "Inigo San Millan talks"
    },
    "channel_bonus": { "Peter Attia": 1.5 },
    "min_views": 2000,
    "min_duration_sec": 300,
    "max_duration_sec": 5400,
    "min_days_old": 7,
    "min_publish_date": "2025-01-01",
    "max_videos_per_channel": 15
  },

  // ── Literature arm ───────────────────────────────────────────
  "literature": {
    "enabled": true,
    "queries": ["zone 2 exercise lactate", "mitochondrial biogenesis endurance", "GLP-1 metabolic"],
    "sources": ["europepmc", "pubmed", "biorxiv_medrxiv", "openalex"],  // which databases
    "authors":  ["Inigo San Millan", "Iñigo San Millán"],   // followed authors (always pulled)
    "venues":   [],                                          // optional followed journals
    "since_date": "2024-06-01",
    "min_citations": 0,            // preprints start at 0 — don't over-filter
    "include_preprints": true,
    "open_access_only": false,
    "max_results_per_source": 30
  }
}
```

**Back-compat:** existing pure-YouTube profiles (e.g. `seo-geo`) keep their current
top-level `search_queries`/`curated_channels`/etc. `profiles.load()` normalizes a
legacy profile into `{youtube: {...legacy fields...}}` so nothing breaks. New
profiles use explicit arms.

---

## 4. The normalized `document`

Both adapters emit the same record. This is the universal currency that lets the
upper layers be source-agnostic.

```python
document = {
  "id":            str,    # youtube video id  | DOI (or source:accession)
  "source_type":   str,    # "youtube" | "literature"
  "source":        str,    # "youtube" | "pubmed" | "biorxiv" | "openalex" | ...
  "title":         str,
  "byline":        str,    # channel name | author list
  "url":           str,    # watch url | DOI/landing url
  "published_date":str,    # YYYY-MM-DD
  "text":          str,    # transcript | abstract (+ full text if fetched)
  "content_status":str,    # generalized state machine (see §7)
  "authority":     float,  # 0..1 normalized (see §6)
  "velocity":      float,  # views/day | citations/year
  "quality_score": float,
  "raw": dict,             # source-specific: views/likes | citations/venue/doi/oa_status
}
```

---

## 5. Source-adapter interface (the one real new abstraction)

```python
class SourceAdapter:
    name: str                                  # "youtube" | "scholarly"

    def discover(self, arm: dict) -> list[dict]:
        """Run the arm's queries + monitored authorities (channels / authors).
        Return document stubs (id, title, byline, url, published_date, raw)."""

    def fetch_content(self, stub: dict) -> str | None:
        """Get the analyzable text. YouTube: transcript (yt-dlp). Literature:
        abstract immediately; full text on demand. None on genuine failure
        (caller records a retryable status — see LEARNINGS P1/P2)."""

    def authority(self, doc: dict) -> float:
        """0..1 source authority. YouTube: channel tier + monitored. Literature:
        venue + citation count + author metrics."""
```

- `youtube` adapter = today's `podcast_scraper` discovery + `fetch_transcripts` +
  `channel_authority`, refactored behind this interface.
- `scholarly` adapter = wraps `biorx`'s `src/biorxiv_api.py` + its EuropePMC/PubMed/
  OpenAlex/Crossref/Unpaywall clients; `fetch_content` returns the abstract (and,
  on demand, the Unpaywall/PMC full text); `authority` from OpenAlex citations +
  venue + author h-index.
- The scraper's `main()` becomes: `for adapter in active_adapters(profile): adapter.discover(arm)` → normalize → the existing enrich/score/store loop.

---

## 6. Scoring (source-aware authority)

`quality_score` stays a single 0–1 number so both arms rank in one list, but the
**authority** and **velocity** terms are computed per source type:

- **YouTube** (unchanged): authority = channel tier + monitored; velocity = views/day.
- **Literature:** authority = normalized( venue prestige + log(citations) + author
  h-index ); velocity = citations/year (or Altmetric attention if available).
  Preprints legitimately start near 0 citations — weight **recency + venue + author**
  so new preprints aren't buried (mirror of the "don't over-filter on views" lesson).

Shared terms (relevance from `keywords`, recency) apply to both. Keep the additive
authority weighting from the recent scoring fix (LEARNINGS P7) — experts/established
venues should rank above keyword-stuffed titles or low-signal preprints.

> **Cross-source bonus (phase 2):** a claim/finding present in *both* a paper and a
> video is stronger than either alone → a small additive "corroboration" term. This
> is a ranking signal no single-source tool can compute.

---

## 7. State machine (generalized)

`transcript_status` → **`content_status`** (same column, same values, meaning per
source). Values unchanged: `not_requested → requested → obtained | not_available | error`.

- **YouTube:** as today (obtained = transcript on disk + row).
- **Literature:** the **abstract** is usually available at discovery, so a paper can
  reach `obtained` immediately (abstract is enough to analyze + digest). Full-text
  fetch is the analog of "mark for transcription": user/scorer promotes the top
  picks; `requested` → fetch full text → `obtained (full)`. Keep abstract-first to
  control volume and cost (mirror of candidates → mark → transcribe).

---

## 8. Data model (additive migration — honor the rules)

Keep the physical `videos` table (it's the only copy of real data; additive only),
treat it logically as `documents`. Add via `migrate()`:

- `source_type TEXT DEFAULT 'youtube'`, `source TEXT DEFAULT 'youtube'`
- `doi TEXT`, `citations INTEGER`, `venue TEXT`, `oa_url TEXT`  (literature fields, NULL for video)
- `byline TEXT` (generic for channel|authors; existing `channel_name` stays populated for video)

`channels` registry gains a sibling concept for literature: a small `authorities`
table (or reuse `channels` with a `kind` column = `youtube_channel | author | venue`).
`sources` (databases) are config, not a registry — they live in the profile arm.

> A future cosmetic rename `videos → documents` is optional and **not** required to
> ship; the additive columns make the table source-aware today.

---

## 9. Dashboard implications

- The existing tabs work unchanged (they read `documents` regardless of source).
  Add a **source filter** (All / Video / Literature) and show a source badge per row.
- **Discovery** tab: "channels to monitor" (video) sits beside "authors/labs to
  follow" (literature); suggested-authority logic (≥N strong items) applies to both.
- **Settings / + New profile:** the form grows two collapsible sections — a YouTube
  arm (queries, channels, view/duration filters) and a Literature arm (queries,
  sources checkboxes, authors, since-date, min-citations, OA-only).
- **Report/Digest:** unchanged — the cited advisor report now cites a mix of
  `[video @ t]` and `[DOI]`. Citation validation still drops anything unverifiable.

---

## 10. Cross-source linking (phased — the real prize)

- **Phase 1 — co-surfacing (cheap):** both arms feed one analysis set + one report.
  Mixed citations. Ships value immediately.
- **Phase 2 — evidence alignment:** flag/boost a video whose claims match the
  literature set ("evidence-backed"); flag contradictions. New authority signal.
- **Phase 3 — claim→DOI linking:** the analyzer extracts the references a transcript
  makes; resolve them against the literature index ("Attia [video] discusses San
  Millán [DOI] on lactate"). Entity resolution; highest value, most work. **Every
  link must be verified, never LLM-guessed** (LEARNINGS P6 / Working Rule 0).

---

## 11. Phased build plan

1. **Adapter seam + co-surfacing.** Define `SourceAdapter`; refactor today's YouTube
   path behind it; add a `scholarly` adapter wrapping one biorx client (EuropePMC or
   OpenAlex). A dual-arm Topic flows discover → fetch abstract → analyze → one cited
   report. Profile schema (§3) + additive migration (§8). Back-compat for legacy profiles.
2. **Literature authority/velocity scorer** (§6) + abstract-first state machine (§7).
3. **Dashboard** source filter + arm-aware New/Settings forms (§9).
4. **Fold in remaining biorx sources**; keep its local Qwen as an LLM backend option
   (cheap bulk triage; stronger model for the synthesis report).
5. **Cross-source linking** phases 2→3 (§10).

---

## 12. Decisions

- **[DECIDED] One engine.** *This* repo is the engine; scholarly sources are
  adapters. Phase-1 ships a self-contained EuropePMC adapter in-repo (stdlib) as the
  seam; production wraps biorx's broader multi-source/dedup/OA clients behind the
  same `SourceAdapter` interface.
- **[DECIDED] LLM per stage.** Two roles, configurable: a **bulk** model (cheap/local,
  e.g. Ollama Qwen) for per-item abstract/transcript analysis, and a **synthesis**
  model (stronger/hosted) for the digest + advisor report. Env/profile:
  `PODCAST_LLM_MODEL` (bulk default) and `PODCAST_SYNTH_MODEL` (falls back to bulk).
  Defaults preserve current behavior.
- **[DECIDED] DOI dedup in the adapter.** The scholarly adapter owns dedup: normalize
  DOIs, collapse duplicates, prefer the published version over the preprint when both
  appear. Cross-modality (video vs paper) never dedups.
- **[DEFERRED] Rename `videos`→`documents`.** Cosmetic; additive `source_type` columns
  make the table source-aware without a rename.
