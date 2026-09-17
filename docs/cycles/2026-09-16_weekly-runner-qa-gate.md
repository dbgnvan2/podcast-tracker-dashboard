# QA gate — weekly_run.py + weekly.sh (one-writer weekly runner)

**Gate:** learning-qa failure-pattern sweep · skill `learning-qa-sweep` · lens `~/.claude/agents/learning-qa.md`
**Run:** 2026-09-16 13:58 PDT, on mini1 (davemini), repo `/Users/davemini/ProjectsMini1/ptd`
**Reviewer:** one-shot Hermes process, no memory of writing the code. Read-only: no file in the repo was modified by this sweep.

---

## Paste-ready verdict for Claude Code

```
VERDICT:     REJECTED
RANGE:       origin/main...HEAD (merge-base diff; caller-supplied range)
COMMITS:     1 — 7de637b "Add weekly_run.py + weekly.sh: one-writer weekly runner (W1-W11)"
TESTS:       python3 test_app.py  ->  Ran 142 tests in 1.435s / OK / exit 0  (real run, this sweep)
FINDINGS:    9 — 0 high, 4 medium-high/medium (demonstrated), 5 low-medium
BLOCKING:    F1 (reconcile is the one stage that cannot be given the pinned DB),
             F2 (analyze/digest failure inside drain() -> exit 0, no failed-stage name),
             F3 (counts()/print_summary outside the stage guard -> a locked DB kills the
                 run before any output stage; traceback instead of the summary),
             F5+F6 (the one-writer lock is global-substring AND one-directional),
             F8 (producer/consumer count keys pinned only by hand-written literals in tests)
NOT ASSESSED: logic/algorithmic correctness, concurrency beyond the lock analysis, auth/security,
             injection, performance, dependency supply chain, API compatibility, architecture,
             test quality in general; live yt-dlp/LLM paths (all stages faked here); the external
             Hermes cron entry (not in-repo).
SECOND LENS: behaviour changed (new entry point + extracted drain loop) -> run
             `requesting-code-review` over the same range; this sweep is not a general review.
FLIP TO APPROVED: F1-F3 fixed with a test each; F5/F6 unified into one DB-keyed lock;
             F8's fake derived from the real producer's return value.
```

---

## Evidence table (every row was a command run in this sweep)

| # | Command | Real output tail |
|---|---|---|
| 1 | `git rev-parse --show-toplevel` · `git cat-file -e origin/main` · `git status --porcelain` | `/Users/davemini/ProjectsMini1/ptd` · OK · (empty) — right repo, base hash present, tree clean |
| 2 | `git diff origin/main...HEAD > /tmp/sweep-weeklyrunner.diff && wc -l` | `583` lines; `--stat`: 5 files, 513 insertions(+), 7 deletions(-) |
| 3 | `python3 test_app.py` | `Ran 142 tests in 1.435s` / `OK` / `EXIT=0` |
| 4 | `python3 /tmp/ptd_sweep_probe_a.py` (temp HERMES, 2 profiles) | `pinned cfg db_path: …/podcast_bb.db` · `dashboard_server.DB_PATH: …/podcast_aa.db` · `DIVERGENT: True` · `reconcile signature: ['include_search']` vs `migrate signature: ['db']` |
| 5 | `python3 /tmp/ptd_sweep_probe_c.py` (real `drain()`, analyze stubbed to raise) | `analyze error: analyze backend down` · `drain returned normally: 1 -> no exception reached the caller` |
| 6 | `python3 /tmp/ptd_sweep_probe_g.py` (real `weekly_run.main` with a locked DB) | `[stage FAILED] migrate: OperationalError: database is locked` · `weekly_run.main RAISED: OperationalError -> database is locked` · `stages that actually ran: []` · `digest ran: False \| report ran: False` |
| 7 | `python3 /tmp/ptd_sweep_probe_b.py` (decoy process whose argv merely mentions `weekly_run.py`) | `decoy pid: 5911` · `_pids sees: [5911]` · `_already_running(): True -> the runner would skip the week, exit 0` · `after decoy exit: False` |
| 8 | `python3 /tmp/ptd_sweep_probe_e.py` (decoy alive, both guards queried) | `weekly_run._already_running(): True` · `overnight_pipeline._already_running(): False` |
| 9 | `python3 /tmp/ptd_sweep_probe_d.py` (mutation: pin stripped immediately before reconcile, copy of the tree in /tmp) | `suite exit code: 0` · `Ran 142 tests in 1.450s \| OK` · `MUTANT SURVIVES (tests do not pin reconcile's DB): True` |
| 10 | `grep -n "_pids\|_already_running\|pgrep" test_app.py` | only `2009: patch(weekly_run, "_already_running", lambda: False)` and `2086: self.wr._already_running = lambda: True` — the real lock is never executed by the suite |
| 11 | `grep -n "youtube_enabled\|podcast_scraper.main" test_app.py` | only `1990` (inside the fake) — the real producer's key set is never asserted |
| 12 | `git show origin/main:overnight_pipeline.py` + `git diff origin/main...HEAD -- overnight_pipeline.py \| grep -c '^@@'` | `2` hunks; the loop moved verbatim (old 108-135 -> new `drain()` 110-138) — P12 checked, no dropped side effect |
| 13 | `ls -la ~/.hermes/podcast_tracker.db` (before and after the sweep) | `Sep 12 11:00` both times — the suite wrote no production DB (P28 clear for this range) |

---

## Findings (ranked)

### F1 · P5 / P13 · `weekly_run.py:199` + `dashboard_server.py:102,257` · the only stage that cannot be given the pinned DB · **medium-high**
`stage("reconcile", lambda: dashboard_server.reconcile(include_search=True))` — `migrate` is called with `db=db` (line 196) and `drain` with `db=db` (line 201), but `reconcile(include_search=False)` takes **no** `db` parameter and reads the import-frozen module constant `dashboard_server.DB_PATH = profiles.load()["db_path"]` (line 102). Every stage module in this repo freezes its path at import (`fetch_transcripts.DB_PATH:24`, `analyze_transcripts.DB_PATH:30`, `generate_digest.DIGEST_DIR:22`, `generate_report.REPORTS_DIR:30`), so the pin only works if the module is imported after `PTD_PROFILE` is set — true in the fresh-process cron path, false for **any in-process caller**, which is exactly how the new tests (and any future dashboard-side call) invoke it.
Measured (row 4): with `_active=aa` imported first and `PTD_PROFILE=bb`, the run's `cfg["db_path"]` is `podcast_bb.db` while `dashboard_server.DB_PATH` is `podcast_aa.db`. `reconcile` **mutates** rows (resets fake `obtained` -> `requested`, re-queues `not_available`), so the mismatch writes another profile's DB — the "one writer / ONE pinned profile" invariant (W3/W4/W11) fails in the write direction.
Mutation proof (row 9): stripping the pin immediately before the reconcile call leaves **142 tests OK** — nothing pins which DB reconcile targets. This is also the fix-log pattern "2026-07-08 — Spawned jobs followed a mid-run profile switch (found by learning-qa review)" re-entering through a new door.
**Fix:** `def reconcile(include_search=False, db=None)` using `target = db or DB_PATH`, and call `dashboard_server.reconcile(include_search=True, db=db)` — mirroring `migrate`. Add a test that pins a non-active profile and asserts reconcile's target path. · **confidence: high** (mechanism measured; impact conditional on an in-process caller).

### F2 · P15 / P24 · `overnight_pipeline.py:120-128` consumed by `weekly_run.py:201` · an analyze/digest failure inside `drain()` is swallowed, and the runner exits 0 · **medium-high**
`drain()` wraps `analyze_transcripts.analyze_all()` and `write_digest()` in `try/except Exception: print(...)`. `weekly_run`'s failure contract is "a stage that raises is recorded in `failed`, named, and exits non-zero"; a failure *inside* `drain` never raises, so `failed` stays empty, the `FAILED stages:` line is omitted, and `main()` returns 0 — while the analysis half of the week did not run. Measured (row 5): with `analyze_all` raising, real `drain()` printed `analyze error: …` and returned `1` normally — no exception reached the orchestrator. The digest stage then rebuilds from the un-analyzed set and the summary prints that artifact path as this week's deliverable. Module docstring: *"a failure must never look like a clean run."*
**Fix:** have `drain()` return the sub-stage failures (or raise an aggregate) and feed them into `failed`; at minimum count `analyze`/`digest` errors into the summary and the exit code. · **confidence: high** (measured).

### F3 · P2 · `weekly_run.py:197,207` + `print_summary` (`:111-150`) · the guard covers stages, not the read-backs between them · **medium**
`before`/`after = overnight_pipeline.counts(db)` (which has no error handling, `overnight_pipeline.py:45-52`) and everything in `print_summary` sit **outside** `stage()`. Measured (row 6): with a concurrent writer holding a transaction on the DB — the exact scenario in this repo's own *Open risks* ("database is locked" if a write transaction exceeds the 5s busy-wait) — `migrate` failed (caught, recorded) and then the unguarded `counts(db)` raised `sqlite3.OperationalError: database is locked` straight out of `weekly_run.main()`. Result: **no digest, no report, no WEEKLY SUMMARY** — the cron delivers a stack trace instead of the artifact the whole task exists to produce, in the one scenario where the runner most needs to say "this week failed, here is why".
**Fix:** put the counts calls and the summary build behind the same guard (`safe = stage("summary", …)`), and on failure print a degraded one-line summary that names the error, so a failed week is still reported as failed. · **confidence: high on mechanism, medium on likelihood.**

### F4 · P13 · `weekly_run.py:45-51` (+ `:36-42`) · the one-writer lock is a global substring match, and a skipped week exits 0 · **medium**
`_already_running()` = `pgrep -f weekly_run.py` + `pgrep -f overnight_pipeline.py`, unmatched to any profile or DB. Measured (row 7): a process whose argv merely *mentions* `weekly_run.py` (nothing to do with a run) makes the guard return True — the runner then prints "already running — skipped" and exits **0**, i.e. a week that did nothing reports success to the scheduler. On this machine `~/.hermes/profiles/` holds 5 profiles (seo-geo active plus zone2-training, bowen-theory, …), so a weekly run pinned to profile B is also skipped while a run for profile A is in flight — a per-DB concern gated by a machine-global condition.
**Fix:** a lock file beside the profile's DB (`<db>.weekly.lock` with a pid + start time, path-derived from the profile — also satisfies W11), replacing pgrep entirely; if pgrep stays, match the interpreter+script token and the profile argument, and print a distinguishable skip line. · **confidence: high** (measured).

### F5 · P5 · `overnight_pipeline.py:88-100` vs `weekly_run.py:45-51` · the guard is one-directional · **medium**
`weekly_run` refuses to start when an `overnight_pipeline.py` is live, but `overnight_pipeline._already_running()` only greps for its own name. Measured (row 8): with a decoy `weekly_run.py` process alive, `overnight_pipeline._already_running()` returned **False** → it starts and becomes the second writer on the same DB. Same class as the repo's own P5 rule ("are ALL calls of this kind hardened, or just the one that broke?") and the asymmetry is in the commit's headline claim ("Single writer via pgrep over weekly_run.py + overnight_pipeline.py").
**Fix:** one shared lock helper used by both entry points. · **confidence: high** (measured).

### F6 · P10 / P19 · `test_app.py:1981-1993`, `:2009`, `:2086` · the producer/consumer count contract is pinned by hand-written literals · **medium**
`weekly_run.print_summary` reads the new `podcast_scraper.main()` return dict with `.get(k, 0)`, and the fake producer (`fake_discovery`, line 1990) plus tests W7/W8 pass their own literal dicts. Row 11: `youtube_enabled` appears in the test file only inside that fake — nothing asserts the real producer's key set. So renaming a key in `podcast_scraper.main` (e.g. `reupload_dropped` -> `reupload_skipped`) keeps **142 tests green** while the summary silently prints `re-upload dups: 0` — the very exclusion W8 exists to surface, and the "never silent" (P2) property the return dict was added for. A silent 0 for an exclusion count is the failure this project has been burned by before.
**Fix:** derive the fake/expected key set from the producer (call `podcast_scraper.main`'s documented keys in one assertion, or have the fake build from the producer's own dict constant). · **confidence: medium-high.**

### F7 · P10 / P27 · `test_app.py:2086` · W4's test replaces the predicate it is meant to verify · **medium**
`test_w4_single_instance_lock_skips_cleanly` sets `self.wr._already_running = lambda: True`; row 10 shows the real `_already_running`/`_pids` are executed by **no** test. So the lock's real behaviour — scope, false positives, direction — is uncovered, which is precisely why F4 and F5 survived to this gate. A test that stubs the thing under test cannot report a lock defect.
**Fix:** extract the decision into a pure predicate (`def lock_conflict(pids, mypid) -> bool`) and test that, plus one test driving `_pids` against a controlled decoy process. · **confidence: high.**

### F8 · P2 · `weekly_run.py:68-85,88-103,116-121` · failed summary queries render as reassuring zeros · **medium-low**
`_scalar` returns `0`, `_has_column` returns `False`, `_top_picks` returns `[]` on `sqlite3.OperationalError`. In the chat-delivered summary a query that could not run is indistinguishable from a true zero: `analyzed (total): 0`, `captions unproven: 0` (a *good-news* reading of an unknown), and a silently missing "top digest picks" block. The intentional part (`insight_score` not existing yet, documented at `:89-90`) is fine; the unintentional part is that any schema/permission problem reads as a clean week. The summary is the artifact a human reads instead of the DB.
**Fix:** return `None` on failure and print `n/a (query failed)` — or record a flag so `print_summary` can print one "summary data unavailable" line. · **confidence: medium.**

### F9 · P19 (skill pitfall 3) · `weekly_run.py:208-210` vs `overnight_pipeline.py:67-70` · hand-copied cap predicate · **low**
The `capped-out` number is a second, hand-written copy of `promote_errors`' predicate (`transcript_status='error' AND COALESCE(fetch_attempts,0) >= MAX_FETCH_ATTEMPTS`) because `drain()` returns only the round count and discards the `capped` it already computed. Change the cap semantics in one place and the summary drifts silently in the other.
**Fix:** return `capped` (or the counts tuple) from `promote_errors`/`drain` and print the producer's number. · **confidence: high on the drift, low on impact.**

---

## Non-blocking observations

- **`REPORT_N = 8`** (`weekly_run.py:30`) is a literal window inside the runner; `generate_report.py --n` already parameterises it, so a profile with many sources silently gets an 8-source report (P9-lite).
- **W9's "touches nothing" is not what the test pins.** `test_w9_dry_run_touches_nothing` asserts only that no stage function was called; `resolve_profile` -> `profiles.load()` -> `_seed_default()`/`_ensure_dirs()` still `mkdir`s `profiles/ db/ digests/` and seeds `seo-geo.json` + `_active` when absent (`profiles.py:118-130`). Harmless, but the assertion cannot detect a filesystem write.
- **`insight_score`** (`_top_picks`, `:88-103`) is a speculative branch for a column that does not exist; documented, harmless, dead until the schema adds it.
- **Redundant digest build:** the drain rebuilds the digest every round and `stage("digest", …)` rebuilds it again at the end — extra LLM/filesystem work per run, not a correctness issue.
- **`_pids` does `int(p)`** without guarding non-numeric pgrep output; a parse error would escape `_already_running()` (only `FileNotFoundError` is caught).
- **`tearDown` does not clean the temp trees** (`tempfile.mkdtemp` per test, never removed) — housekeeping only.
- **P12 checked, clean:** the `drain()` extraction is faithful — 2 hunks, loop body moved verbatim, `_ensure_attempts_column` and the final "Pipeline finished" print both retained.

---

## Coverage statement (binding)

Clean against **none of the patterns considered**: 9 findings across a change to which **15 of the 35 generic patterns** were plausibly applicable (P2, P5, P6, P8, P9, P10, P12, P13, P15, P19, P22, P24, P27, P28, P31) — all of which were examined, plus all 12 items of this repo's `LEARNINGS.md` review checklist. Patterns checked and *not* fired include P1 (transient-as-terminal: no new status writes), P4 (hardcoded topic/date constants), P7 (scoring weights — untouched), P21 (built-but-not-wired: `weekly.sh` -> `weekly_run.py` is wired in-repo; its external cron caller is out of scope), P22 (the `podcast_scraper.main()` return-type change was AST-checked: exactly two `return` statements, both dicts — no stale `return None` branch left behind).

**Not assessed (scope limits of this lens):** logic/algorithmic correctness, concurrency beyond the lock analysis above, authentication/authorization, injection and other security classes, performance, dependency and supply-chain risk, API contract compatibility with external consumers, general test quality, architecture. Live yt-dlp/OpenAI paths were not exercised (every stage was faked in the suite and in this sweep's probes). The Hermes cron job that calls `weekly.sh` lives outside the repo and was not inspected — no `--profile` is baked into the shim, so pinning depends entirely on the cron command line (an unpinned run follows `_active` at each stage's call time; that is the "2026-07-08 mid-run profile switch" path and it is worth pinning in `weekly.sh` or requiring `--profile`).
