# QA gate #2 — weekly-runner fix commit (F1–F7 claims) — learning-qa failure-pattern sweep

**Gate:** learning-qa failure-pattern sweep · skill `learning-qa-sweep` · lens `~/.claude/agents/learning-qa.md`
**Run:** 2026-09-16 14:0x–14:15 PDT, on mini1 (davemini), repo `/Users/davemini/ProjectsMini1/ptd`
**Reviews:** the full range `origin/main..HEAD` (2 commits) **and** the fix commit `d01881c` as its own range (`7de637b...HEAD`).
**Predecessor (do not confuse):** `docs/cycles/2026-09-16_weekly-runner-qa-gate.md` (gate #1, REJECTED on `7de637b`). This file is gate #2 and supersedes its verdict for the current tree only.
**Reviewer:** one-shot Hermes process with no memory of writing the code, plus one independent delegated reviewer
(`~/.hermes/cache/delegation/live/deleg_c82be9ed/task-0.log`). Read-only: **no repo file was modified or created by this sweep** (only this gate file was written). Every reproduction ran against throwaway `HERMES_DIR` homes; see the incident note in finding 1.

---

## Paste-ready verdict for Claude Code

```
VERDICT:     REJECTED
RANGE:       origin/main...HEAD (merge-base diff; caller-supplied range) = 2 commits
             also reviewed as its own range: 7de637b...HEAD (the fix commit)
COMMITS:     2 — 7de637b "Add weekly_run.py + weekly.sh: one-writer weekly runner (W1-W11)"
                 d01881c "Fix weekly-runner QA gate findings F1-F7 (per-DB lock, pin check, honest exit)"
TESTS:       python3 test_app.py -> Ran 160 tests in 1.485s / OK / EXIT=0  (real run, this sweep)
             the suite is green AND destructive: running it deletes real transcript files (finding 1)
F1-F7 CLAIMS: 6 of 7 verified as genuinely fixed and mutation-pinned (table 2).
             F4 is only half-fixed: the lock is per-DB, but the writers of the same DB
             that are NOT weekly_run/overnight_pipeline still hold no lock (finding 4).
NEW FINDINGS: 9 — 2 high (one is a live data-loss incident), 5 medium, 2 low
BLOCKING:    F1 test_app.py:2166 deletes the user's real transcripts on every suite run
                (demonstrated; ~/.hermes/transcripts lost all 16 .txt files at 14:02:24,
                 i.e. during the suite run that d01881c's message reports as "160 tests OK")
             F2 dashboard_server.py:287-297 deletes transcript files from the SHARED
                transcripts dir using ONE profile's DB -> `weekly_run --profile B`
                wipes profile A's transcripts (demonstrated)
NOT ASSESSED: logic/algorithmic correctness, auth/security, injection, performance, dependency
             supply chain, API compatibility, architecture, general test quality; live
             yt-dlp/LLM paths (all stages faked or stubbed); the out-of-repo Hermes cron entry.
SECOND LENS: behaviour changed (new entry point, new lock module, stage extraction) -> run
             `requesting-code-review` over origin/main...HEAD; this lens is not a general review.
DATA NOTE:   the 16 lost .txt files are RECOVERABLE — every `transcripts` row still holds
             `full_text` (6,129–75,902 chars) and all 16 `{id}.segments.json` files are on disk.
FLIP TO APPROVED: F1 + F2 fixed with a test each (a redirect for the suite, a file-ownership
             rule for reconcile); F4's remaining writers either acquire the same lock or are
             named as out of contract; F5's lock made atomic (flock or link-into-place).
```

---

## Evidence table (every row is a command run in this sweep)

| # | Command | Real output tail |
|---|---|---|
| 1 | `pwd` · `git rev-parse --show-toplevel` · `git cat-file -e origin/main` · `git status --porcelain` | `/Users/davemini/ProjectsMini1/ptd` · same · `base-ok` · `?? docs/` (untracked gate files only) |
| 2 | `git log --oneline origin/main..HEAD` · `git diff origin/main...HEAD > /tmp/sweep-wr2-full.diff; wc -l` · `git diff 7de637b...HEAD > …fix.diff; wc -l` | 2 commits (`d01881c`, `7de637b`) · `1072` lines · `777` lines |
| 3 | `python3 test_app.py` | `Ran 160 tests in 1.485s` / `OK` / `EXIT=0` |
| 4 | `stat -f '%Sm' ~/.hermes/podcast_tracker.db` (before + after the suite) | `2026-09-12 11:00:37` both times; no `*.writer.lock` left in `~/.hermes` |
| 5 | `python3 /tmp/sweep-mutants.py` (16 runs: baseline + 14 mutations, each in its own copied tree, `HERMES_DIR`=throwaway) | baseline `OK`; 13 mutants `killed`; **M4 `SURVIVED (bad)`** (see table 2) |
| 6 | `python3 /tmp/sweep-e1.py` (fake home, one planted `REALVID001.txt`, runs the single new test) | `BEFORE: ['REALVID001.segments.json', 'REALVID001.txt']` · `AFTER: ['REALVID001.segments.json']` · `REAL TRANSCRIPT DELETED BY SUITE: True` · test `rc: 0 OK` |
| 7 | `python3 /tmp/sweep-e2b.py` (fake home, profiles aa+bb, `aa` owns a transcript, `reconcile(db=bb)`) | `Removed 1 unbacked transcript file(s).` · `after B-reconcile: file exists = False` · `A still says 'obtained': obtained` |
| 8 | `python3 /tmp/sweep-recover.py` | `rows=16  .txt present=0  .segments.json present=16` · `file_path values exist: 0` · full_text 6,129–75,902 chars |
| 9 | `ls -la ~/.hermes/transcripts` + `git log --date=format:'%F %T'` | dir mtime `2026-09-16 14:02:24`; `d01881c` committed `2026-09-16 14:03:21` |
| 10 | `python3 /tmp/sweep-confirm.py` | `second acquire while first holds (mid-write): (True, None) -> stolen: True` · `disc={} -> 'WARNING: discovery result missing keys' printed: False` / `'re-upload dups: 0' printed: True` · `'literature' in weekly_run.py: False`, 2 live profiles enable it |
| 11 | `grep -n "pgrep\|_job_running" dashboard_server.py` · `grep -rn "dblock\." --include=*.py` | pgrep still at `2237`, `2539`, `2716`; `dblock` imported by exactly `weekly_run.py` and `overnight_pipeline.py` |
| 12 | `grep -c '^### P' ~/.claude/standards/learnings.md` · `grep -c '^[0-9]*\.' LEARNINGS.md` | `35` generic patterns · `12` repo checklist items |
| 13 | delegated reviewer (persona `~/.claude/agents/learning-qa.md`, same range, read-only) | full report read; live transcript `~/.hermes/cache/delegation/live/deleg_c82be9ed/task-0.log` |

### Table 2 — mutation battery for the F1–F7 claims (each mutant = one edit in a copied tree)

| Mutant | Claim it targets | Suite | Meaning |
|---|---|---|---|
| baseline (no edit) | harness sanity | `OK` | mutations are comparable |
| M1 drop `db=db` from the reconcile call | F1 | rc 1 `FAILED (errors=1)` | pinned |
| M2 gut `pin_mismatches` to `return []` | F1 | rc 1 `failures=1` | pinned |
| M3 drop `fetch_transcripts.DB_PATH` from `PIN_ATTRS` | F1 | rc 1 `failures=1` | pinned |
| M4 drop `generate_digest.DIGEST_DIR` from `PIN_ATTRS` | F1 | **rc 0 `OK`** | **NOT pinned** (finding 3) |
| M5 drop `generate_report.REPORTS_DIR` from `PIN_ATTRS` | F1 | rc 1 `failures=2` | killed (incidentally, via the artifact-path assert) |
| M6 drop `failures=failed` from the `drain` call | F2 | rc 1 `failures=1` | pinned |
| M7 drop `failures.append("drain:analyze")` | F2 | rc 1 `failures=1` | pinned |
| M8 drop the summary `try/except` | F3 | rc 1 `errors=1` | pinned |
| M9 un-guard `counts-after` | F3 | rc 1 `errors=1` | pinned |
| M10 make the lock file DB-independent | F4 | rc 1 `failures=1` | pinned |
| M11 make `lock_holder` never block | F7 | rc 1 `failures=4` | pinned |
| M12 `overnight_pipeline` stops locking | F5 | rc 1 `failures=1` | pinned |
| M13 producer renames `reupload_dropped` | F6 | rc 1 `failures=2, errors=2` | pinned |
| M14 `discovery_result` stops checking keys | F6 | rc 1 `failures=1` | pinned |

---

## Findings (ranked)

### F1 · P28 / P6 / CLAUDE.md constraint 4 · `test_app.py:2155-2170` (call at `:2166`) + `dashboard_server.py:287-297` · the fix commit's new test deletes the user's real transcripts on every suite run · **HIGH — blocking**
`test_w3_f1_real_reconcile_writes_only_the_given_db` calls the **real** `dashboard_server.reconcile(db=<temp db>)` while `dashboard_server.TRANSCRIPTS_DIR` is still the *production* `$HERMES_DIR/transcripts` — the test's `setUp` redirects `profiles.*` and (via `PIN_ATTRS`) the DB/digest/report paths, but **`TRANSCRIPTS_DIR` is not in `PIN_ATTRS` and is never patched**. `reconcile` step 2 then deletes *every* `.txt` in that shared directory that the target DB does not back: `os.remove(...)` at `dashboard_server.py:296`.
Measured (row 6): in a throwaway home with one planted transcript, running **that single test** leaves `AFTER: ['REALVID001.segments.json']` — the `.txt` is gone — and the suite still reports `OK`.
This is not theoretical. On this machine `~/.hermes/transcripts` now holds **0 `.txt` files** while the `transcripts` table holds **16 rows whose `file_path` points into that directory** (all 16 videos still `obtained`). Directory mtime `14:02:24`, i.e. *before* `d01881c` was committed at `14:03:21` and before any run in this sweep (my first suite run was after `14:03:33`) — consistent with: test written → suite run for the message's "160 tests OK" → commit. Gate #1's 142-test suite ran the same three reconcile callers *with* `TRANSCRIPTS_DIR` redirected (`test_app.py:259`, `:1789`) and therefore deleted nothing; the destructive call is new in `d01881c`. This is exactly global pattern **P28, "a test that reaches production data through a default argument"**, and exactly why no test may touch a production path unless it redirects it.
**Fix:** patch `dashboard_server.TRANSCRIPTS_DIR` (and `analyze_transcripts.TRANSCRIPTS_DIR`) to a temp dir in `TestWeeklyRun.setUp`, and add `TRANSCRIPTS_DIR` to `PIN_ATTRS` so the pin check covers the directory the destructive step writes. Add one assertion that the suite's fake home is unchanged after reconciling. · **confidence: high** (mechanism reproduced; live loss observed)
**Recovery (no action taken by this sweep):** every one of the 16 `transcripts` rows still carries `full_text` (6,129–75,902 chars) and all 16 `{id}.segments.json` files are intact, so the `.txt` bodies can be restored from the DB without re-fetching.

### F2 · P6 / P2 / P3 · `weekly_run.py:254` + `dashboard_server.py:287-297` · reconciling one profile deletes another profile's transcripts · **HIGH — blocking**
`reconcile(db=db)` now takes the pinned DB (the F1 fix), but its step 2 deletes from a directory that is **shared across profiles** (`~/.hermes/transcripts`, keyed by video id — CLAUDE.md). So the officially supported path `weekly_run.py --profile B` removes every transcript file that B's DB does not happen to back — i.e. all of profile A's.
Measured (row 7): with `aa` holding an `obtained` video plus its `transcripts` row on disk and `bb` knowing nothing about it, `reconcile(include_search=True, db=<bb>)` printed `Removed 1 unbacked transcript file(s)` and the file was gone while `A still says 'obtained'`. The row's status now asserts an artifact that no longer exists — the P6 failure `reconcile` exists to prevent, inverted. `weekly_run`'s summary never mentions the deletion count (P2): a week that destroys 16 transcripts reports a clean run with a digest and a report.
**Fix:** delete a file only when the *target DB* has a row for that video id and that row is not backed by a `transcripts` row (an actual stub), instead of "every id the target DB has never heard of"; optionally check all profile DBs before deleting a shared file, and surface the count in the weekly summary. · **confidence: high** (demonstrated)

### F3 · P19 / P10 (skill pitfall 3) · `weekly_run.py:41-50` + `test_app.py:2155-2170` · the pin check's coverage list is hand-maintained, incomplete, and only partially pinned · **medium**
`PIN_ATTRS` is a hand-written list of eight import-frozen paths. Two problems:
1. It omits `dashboard_server.TRANSCRIPTS_DIR` — the one path the F1/F2 deletion actually mutates (the runner's own reconcile call is the writer). A pin check that cannot see the directory its stage deletes from is not a guard for that stage.
2. Seven of its eight entries are unproven: mutation **M4** (drop `("generate_digest", "DIGEST_DIR", "digest_dir")`) left **160 tests green** — no test fails when that entry disappears, so the list can silently shrink and the runner will then happily write the digest into another profile's directory (the exact "2026-07-08 mid-run profile switch" class the check was added for).
**Fix:** derive the checked attrs from the stage modules' own profile-derived constants (or assert `set(PIN_ATTRS)` equals an expected set, and pin each entry with one parametrised mismatch test). · **confidence: high on M4 surviving; high on the omission.**

### F4 · P5 · `dashboard_server.py:2512-2520, 2539, 2709-2725` vs `weekly_run.py:211` / `overnight_pipeline.py:138` · "one writer per DB" covers two of the writers · **medium**
`dblock` is imported by exactly two files (row 11). The dashboard still spawns `podcast_scraper.py`, `fetch_transcripts.py`, `analyze_transcripts.py`, `generate_digest.py`, `generate_report.py` and `ingest_literature.py` **with no lock**, and still guards with the mechanism this commit removed from the runners — `pgrep -f <basename>`. A dashboard click therefore neither sees the weekly run's lock (the weekly stages run *in-process*, so no process argv contains `fetch_transcripts.py`) nor is seen by it. The F4 finding named two entry points; the fix hardened those two, not the class (P5). The commit message's "the guard is per-DB and works in both directions" is true of the two runners and false of the DB's other writers.
**Fix:** have the spawned stage scripts acquire the same per-DB lock (one line in each `main()`), or take it in `_spawn`'s child env, and drop `_job_running`'s pgrep once the lock is the single mechanism. · **confidence: medium** (both reviewers agree; not executed as two live writers)

### F5 · P1 / P6 · `dblock.py:36-47, 50-75` · the lock can be stolen in its own create→write window, and a foreign live PID wedges the week · **medium**
`acquire()` creates the file with `O_CREAT|O_EXCL` (`:58`) and only then writes the owner line (`:73`), while `lock_holder()` treats unreadable/empty content as stale and `acquire()` unlinks it (`:68`). A second runner landing in that window steals a **live** lock: measured (row 10) `second acquire while first holds (mid-write): (True, None) -> stolen: True`. Two writers on one DB, silently. The recorded start time is never validated either, so a lock file whose PID belongs to an unrelated process (recycled PID, leftover file, another tool) blocks the week for as long as that process lives; the skip path then prints one line and exits 0, so a week that did nothing still looks clean to the scheduler.
**Fix:** make the lock atomic — `fcntl.flock(fd, LOCK_EX|LOCK_NB)` on the file (stdlib, released by the kernel on death) or write to `<db>.writer.lock.<pid>` and `os.link`/`os.replace` it into place; treat the PID line as diagnostics only. Give the skip a distinguishable outcome (non-zero exit or an explicit `SKIPPED (lock held by pid N since T)` marker). · **confidence: high** (race demonstrated by delegate, re-verified here; the wedge is code-read)

### F6 · P3 · `weekly_run.py:31-32, 255` · the weekly runner never runs the profile's literature arm · **medium**
`STAGE_PLAN` and the discovery stage call only `podcast_scraper.main()`. `ingest_literature.py` is launched by the dashboard's Run Discovery when `literature.enabled` is true (`dashboard_server.py:2551-2553`) — and two profiles on this machine have it enabled (`zone2-training`, `interstitium-system-in-humers`), as does the shipped `profile_examples/zone2-training.json`. So for those profiles the automated week silently ingests no papers while reporting a clean, complete week (P3: not all sources enumerated).
**Fix:** add a guarded literature stage (`ingest_literature.main()` when `cfg.get("literature",{}).get("enabled")`), or print an explicit "literature arm: not run" line so the omission is visible. · **confidence: medium** (intent could have been deliberate scoping; the silence is the defect, not the omission)

### F7 · P5 / P15 · `overnight_pipeline.py:143, 153-154` · the sibling entry point discards the new failure channel · **medium**
`drain()` gained `failures` so a failed analyze/digest cannot look clean, but its only other caller passes `drain(MAX_HOURS, db)` with no channel, and `__main__` is a bare `main()` with no exit status. An overnight run whose analyze stage failed in every round still prints `Pipeline finished after N round(s)` and exits 0.
**Fix:** `failed = []; drain(MAX_HOURS, db, failures=failed)` then `sys.exit(1 if failed else 0)`. · **confidence: high on the code, medium on impact** (the overnight runner has no exit contract today)

### F8 · P10 / P19 · `weekly_run.py:136` · the F6 warning cannot fire in the case it exists for · **low**
`missing = [k for k in SUMMARY_DISC_KEYS if disc and k not in disc]` — when discovery returns nothing (`disc == {}`) the guard short-circuits, `missing` stays empty, and no warning is printed while the exclusion counts render as zeros. Measured (row 10): `disc={}` → warning `False`, `re-upload dups: 0` printed. The test that covers this (`test_f6_missing_key_is_flagged_in_summary`) only passes a **non-empty** dict, whose missing keys are summed on lines that are clearly absent. A guard whose only test has a value that the real failure case does not have is a floor, not a pin.
**Fix:** `missing = [k for k in SUMMARY_DISC_KEYS if k not in disc]` and print the warning whenever `disc` is empty or incomplete. · **confidence: high** (measured)

### F9 · (gate #1 F8/F9, deferred by the author) · `weekly_run.py:84-91, 139-144, 264-266` · still open, and still silent · **low — deferred, not re-litigated**
Gate #1's F8 (`_scalar`/`_has_column`/`_top_picks` collapse query failures into `0`/`False`/`[]`) and F9 (the hand-copied cap predicate) remain unchanged. The fix's `counts_ok` handling covers the `status`/`newly transcribed` pair, but `analyzed (total)`, `errors (retryable)`, `capped-out` and `captions unproven` still print `0` for an unreadable DB, verified independently by the delegated reviewer's probe against a DB path it could not open. Recorded here so the deferral is visible in the gate record, not only in the commit message. · **confidence: high; severity deliberately low**

---

## Non-blocking observations

- **A skipped week still exits 0** (`weekly_run.py:212-214`, pinned by `test_w4_lock_held_on_same_db_skips` as "a skipped week is not a failure"). With the lock now per-DB this is defensible for a genuine overlap, but combined with F5's phantom-PID case it means "did nothing" and "did the week" are still indistinguishable to the scheduler. Gate #1 raised this inside F4; the fix kept it deliberately — flagging so the choice is recorded rather than accidental.
- **`weekly.sh` still passes no `--profile`** (`exec python3 weekly_run.py "$@"`), so pinning depends entirely on the out-of-repo cron command line. `.hermes/profiles/_active` is the fallback. Worth baking in, or requiring the flag.
- **`REPORT_N = 8`** (`weekly_run.py:30`) is a literal window inside the runner whereas `generate_report.py --n` already parameterises it (carried over from gate #1).
- **Redundant digest build**: `drain()` rebuilds the digest every round and `stage("digest", …)` rebuilds it again — extra LLM/filesystem work per run, not a correctness issue.
- **Dashboard job status reads "not running" during a weekly in-flight stage** (`dashboard_server.py:2237` pgrep by basename): the Transcribed/Discovery UI can show idle while the DB is being written. Same root as F4.
- **`_run()` has no top-level guard**: anything raised outside `stage()` and the summary `try` (the `capped = _scalar(...)` call at `:264`, `dblock.acquire` on an unwritable profile dir) still escapes `main()` as a traceback with no summary. Trigger is narrow (needs a non-`OperationalError`, e.g. a corrupt DB file); F3's fix did not cover this sibling line.
- **`tearDown` never removes its temp trees** (`tempfile.mkdtemp` per test) — housekeeping only.
- **Delegated reviewer hygiene note:** that subagent created one stray directory inside the repo via a mistyped `write_file` path and removed it; `git status --porcelain` is `?? docs/` before and after this sweep.

---

## Coverage statement (binding)

Reviewed **162 patterns (35 generic + 12 repo checklist items)**, of which **~22 were plausibly applicable** to this change (P1, P2, P3, P5, P6, P8, P9, P10, P12, P13, P14, P15, P18, P19, P21, P22, P24, P27, P28, P31, P32, P34) — **all were examined**, and each of the six F1–F7 claims was checked by mutation as well as by reading. Not fired, positively checked: **P4** (no new hardcoded topic/date constants), **P7** (scoring untouched), **P12** (the `drain()` extraction and `main()` reorder move the loop verbatim; no dropped side effect), **P21** (both new mechanisms are wired into their callers), **P22** (`podcast_scraper.main()`'s dict return has one in-repo consumer, grep-clean), **P24**, **P31**, **P34**. **P28 fired the other way**: the suite reaches production data through a default argument (F1).
**Verdict is not "clean"**: 9 findings, 2 high, one of them a live data-loss incident with a reproducibility recipe.

**Not assessed (scope limits of this lens):** logic/algorithmic correctness; concurrency beyond the lock analysis above; authentication/authorization; injection and other security classes; performance; dependency and supply-chain risk; API contract compatibility with external consumers; general test quality; architecture. Live `yt-dlp`/OpenAI/EuropePMC paths were not exercised (every stage was faked or stubbed). The Hermes cron entry that calls `weekly.sh` lives outside the repo and was not inspected.
**Second lens recommended:** the range changes behaviour (new entry point, new lock module, extracted loop, changed function signature) — run `requesting-code-review` over `origin/main...HEAD`. This sweep's two reviewers share one lens; agreement between them is not a substitute for the orthogonal pass.
