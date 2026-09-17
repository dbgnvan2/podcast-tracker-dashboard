# QA gate #3 — weekly-runner fix commits (9c13303 + e076c2f) — learning-qa failure-pattern sweep

Independent review (no memory of writing this code). Supersedes nothing: gate #1
(`docs/cycles/2026-09-16_weekly-runner-qa-gate.md`) and gate #2
(`docs/cycles/2026-09-16_weekly-runner-qa-gate-2.md`) are left as written.

---

## Paste-ready verdict for Claude Code

```
VERDICT:     REJECTED — 1 blocking finding (G3-F1), 1 medium (G3-F2), 3 low/record

RANGE:       origin/main..HEAD = b1e1716...e076c2f (4 commits: 7de637b, d01881c,
             9c13303, e076c2f), base hash verified in this repo (git cat-file -e 7de637b).
             Unreviewed code = d01881c...HEAD (9c13303 + e076c2f); reviewed whole.
             Caller-named sub-range 9c13303..HEAD (= e076c2f alone) read for new defects.
             Working tree: no tracked modifications (`git status --porcelain` = `?? docs/`
             only — the two untracked prior gate files). Committed state only.

CLAIMS:      12/12 verified against the code AND by mutation (table 1). Gate #2 F1-HIGH
             (suite deleted the user's real transcripts) is genuinely closed: 166 tests
             pass and the real ~/.hermes fingerprint is byte-identical before/after.
             Gate #2 F2-HIGH (reconcile deleted other profiles' transcripts) is genuinely
             closed and test-pinned. F3–F7 (medium) all verified fixed and pinned.

BLOCKING:    G3-F1 — e076c2f deleted 13 test definitions, 9 of them the W5–W11 spec
             behaviours, with no replacement and no mention in the commit message that
             reports a RISING test count (164 -> 166). All 9 still pass against HEAD's
             code unchanged (175 tests OK in the restored copy), so this is pure
             verification loss, not a behaviour change. Three of the lost behaviours are
             now demonstrably unpinned (D1/D2/D3 stay green), and the dry-run one hides a
             real regression: the neutered `--dry-run` writes the DB (12288 -> 57344 B)
             and emits digest+report artifacts while the suite stays green.

FLIP TO APPROVED: restore the nine W5–W11 tests (proven green as-is), and either fix
             G3-F2's population source (enumerate the DB files, treat an unparseable
             profile as unread) or record the residual explicitly.

SECOND LENS: behaviour changed (locking, reconcile scope, CLI dispatch) -> recommend
             `requesting-code-review` over d01881c...HEAD as the orthogonal pass; this
             sweep is one family (failures that hide) only.
```

---

## Table 1 — claim-by-claim verification (each claim read in code and mutation-checked)

| # | Claim (commit) | How verified | Mutation | Result |
|---|---|---|---|---|
| 1 | 9c13303: reconcile deletes only files **no** profile DB backs | `dashboard_server.py:293-325`; loop over `_profile_db_paths()`, `backed |=` per other DB | M1 | **verified**, `FAILED (failures=2)` |
| 2 | 9c13303: any unreadable profile DB -> delete nothing, print the skip | `dashboard_server.py:299-316` (`unread` short-circuits the delete branch) | M2 | **verified**, `FAILED (failures=1)` |
| 3 | 9c13303: suite refuses deletes under the real HERMES root for the whole run | `test_app.py:45-69` wraps `os.remove/os.unlink/Path.unlink/shutil.rmtree` in `setUpModule` | M4 | **verified**, `FAILED (errors=1)` |
| 4 | 9c13303: 28 deleted `.txt` restored from `transcripts.full_text` | independent read-only compare, all 5 profile DBs | — | **verified**: 28/28 **byte-identical**, 0 mismatched, 0 files unbacked, all 28 have `.segments.json` |
| 5 | 9c13303: TestWeeklyRun redirects TRANSCRIPTS_DIR; TestReconcile restores patched module state; both assert they are off the real dir | `test_app.py:2265-2271`, `1913-1921`, `344`, `2371-2372` | M1/M2 | **verified** |
| 6 | e076c2f F5: dblock rebuilt on `flock` (atomic, no create->write steal, owner line diagnostics only) | `dblock.py:11-18, 48-107` | M6 | **verified**, `FAILED (failures=9)` |
| 7 | e076c2f F4: every DB-writing stage script's CLI takes the lock, exits 3 on timeout | `dblock.py:110-125` + `fetch_transcripts.py:327-329`, `analyze_transcripts.py:273-275`, `generate_digest.py:150-152`, `ingest_literature.py:134-136`, `podcast_scraper.py:1297-1301` (`--test` excluded, `dry_run()` writes nothing — checked, `podcast_scraper.py:770-800`) | M7/M8/M9 | **verified**, `failures=5 / 1 / 1` |
| 8 | e076c2f F4: dashboard refuses process_queue/analyze/digest/run_discovery/suggest_terms while a writer holds the DB, naming the holder | `dashboard_server.py:2539, 2568, 2593, 2762-2773, 2775-2779`; pinned by `test_app.py:1049-1067` | M16/M17 | **verified**, `failures=6 / 3` |
| 9 | e076c2f F4: generate_report writes no DB rows, so takes no lock | grep for INSERT/UPDATE/DELETE/commit in `generate_report.py` -> none (SELECT only) | — | **verified** (read-only, as claimed) |
| 10 | e076c2f F3: pin set DISCOVERED from the modules; exact set + every-entry mismatch pinned | `weekly_run.py:44-78`; `test_app.py:2517-2540` | M10/M11 | **verified**, `failures=13 / 1` |
| 11 | e076c2f F6: weekly_run runs the literature arm when the profile enables it, always prints a line | `weekly_run.py:276-282`; `test_app.py:2543-2572` | M15 | **verified**, `failures=2` |
| 12 | e076c2f F7: overnight_pipeline exits 1 on any drain failure | `overnight_pipeline.py:134-157`; `drain(..., failures=...)` | M13/M14 | **verified**, `failures=1 / 1` |

Gate #2 F8/F9 and gate #1 F8/F9 stay deferred by the author's triage — recorded, not
re-litigated (G3-F5 below).

---

## Table 2 — mutation battery (20 mutants; each = one edit in a copied tree, suite run with `python3 -B`)

| id | edit | target claim | real output | verdict |
|---|---|---|---|---|
| M1 | reconcile decides `unbacked` from the target DB alone | 9c13303 F2 | rc 1 `FAILED (failures=2)` | pinned |
| M2 | `unread` no longer stops the deletes | 9c13303 F2 | rc 1 `FAILED (failures=1)` | pinned |
| M3 | `_profile_db_paths()` returns `[]` | population producer | rc 0 `OK` | **NOT pinned** (G3-F2) |
| M4 | suite guard no longer wraps `os.remove` | 9c13303 F1 | rc 1 `FAILED (errors=1)` | pinned |
| M5 | `tearDownModule` fingerprint compare disabled | 9c13303 F1 (layer 2) | rc 0 `OK` | not pinned (G3-F4, benign) |
| M6 | `acquire` treats `BlockingIOError` as success | e076c2f F5 | rc 1 `FAILED (failures=9)` | pinned |
| M7 | `run_locked` returns instead of exiting 3 | e076c2f F4 | rc 1 `FAILED (failures=5)` | pinned |
| M8 | `fetch_transcripts` CLI unlocked | e076c2f F4 | rc 1 `FAILED (failures=1)` | pinned |
| M9 | `podcast_scraper` CLI unlocked | e076c2f F4 | rc 1 `FAILED (failures=1)` | pinned |
| M10 | `pin_mismatches` -> `[]` | d01881c F1 | rc 1 `FAILED (failures=13)` | pinned |
| M11 | `TRANSCRIPTS_DIR` dropped from the pin set | e076c2f F3 | rc 1 `FAILED (failures=1)` | pinned |
| M12 | skipped week prints no SKIPPED block | e076c2f F5 | rc 1 `FAILED (failures=1)` | pinned |
| M13 | `failures=failed` dropped from the drain call | d01881c F2 | rc 1 `FAILED (failures=1)` | pinned |
| M14 | `overnight_pipeline.main` always returns 0 | e076c2f F7 | rc 1 `FAILED (failures=1)` | pinned |
| M15 | literature stage never runs | e076c2f F6 | rc 1 `FAILED (failures=2)` | pinned |
| M16 | `_writer_busy` always None | e076c2f F4 | rc 1 `FAILED (failures=6)` | pinned |
| M17 | `spawn_job` writer check removed | e076c2f F4 | rc 1 `FAILED (failures=3)` | pinned |
| D1 | drain called with hardcoded `max_hours=8` | deleted W6 (`--max-hours` forwarding) | rc 0 `OK` | **NOT pinned** (G3-F1) |
| D2 | `--dry-run` early return neutered | deleted W9 (dry-run touches nothing) | rc 0 `OK` | **NOT pinned** (G3-F1) |
| D3 | `weekly.sh` execs `scripts/weekly_run.py` | deleted W10 (shim has no logic / no `scripts` dir) | rc 0 `OK` | **NOT pinned** (G3-F1) |

---

## Table 3 — evidence table (every row is a command run in this sweep, with its real tail)

| command | real output tail |
|---|---|
| `git rev-parse origin/main HEAD` + `git log --oneline origin/main..HEAD` | `b1e1716…` / `e076c2f…`; 4 commits `e076c2f, 9c13303, d01881c, 7de637b` |
| `python3 -B test_app.py` (3.9.6, `/usr/bin/python3`) | `EXIT 0 in 2.3s` … `Ran 166 tests in 2.228s` / `OK` |
| `/opt/homebrew/bin/python3.14 -B test_app.py` | `rc=0  Ran 166 tests in 2.243s \| OK` |
| REAL `~/.hermes` fingerprint (size+mtime_ns of transcripts/, digests/, reports/, db/, profiles/ + `podcast_tracker.db` + `.env`), before vs after the suite | `REAL-HERMES: UNCHANGED` — and unchanged again after the whole sweep |
| read-only compare of every `~/.hermes/transcripts/*.txt` against `transcripts.full_text` in all 5 profile DBs | `byte-identical: 28 \| equal after normalisation: 0 \| mismatched: 0 \| no DB row: 0`; `txt files with no segments.json: 0` |
| `mutants.py` (20 mutants, one edit per copied tree) | 17/17 expected-RED **RED**; `D1 D2 D3 GREEN` |
| `test_names.py` (test defs at 7de637b / d01881c / HEAD) | `7de637b=173 d01881c=192 HEAD=200`; 13 names in d01881c **not** in HEAD (9 of them W5–W11) |
| keyword grep of HEAD `test_app.py` for the deleted tests' subjects | `two_runs 0`, `zero_cap 0`, `dry_run 0`, `shim 0`, `weekly.sh 0`, `No change this week 0`, `re-upload dups: 0`, `capped-out: 0` |
| `tworun_check.py` | `tests calling wr.main >1x: NONE`; only surviving W-mention is the class docstring `test_app.py:2192` — `W1-W11: …` |
| `restore_deleted_tests.py` (HEAD code + the 9 deleted methods re-inserted) | `Ran 175 tests in 2.368s` / `OK` |
| `dryrun_probe.py` (pristine vs D2-neutered tree, temp HERMES, DB pre-created) | pristine: `db-sha 0a892eab3691->0a892eab3691  DB untouched`, `max_hours=8; nothing written.` — neutered: `db-size 12288->57344  db-sha 0a892eab3691->856f1b1c1748  DB WRITTEN`, artifacts `digests/lk/latest.md, reports/lk/latest.md` |

No probe attempted a real delete under `~/.hermes` (caller instruction, and correct: the
guard was verified by its own test + M4, not by trying to destroy data). Nothing in this
sweep wrote or deleted anything under `~/.hermes` — verified by the fingerprints above.

---

## Findings

### G3-F1 · P31 / P27 / P10 · `test_app.py` (commit e076c2f) · a fix commit removed 13 test definitions, 9 of them the W5–W11 spec behaviours, and reported only the rising count · **HIGH — blocking**

`e076c2f` was the only commit in `9c13303..HEAD`, the sub-range the caller asked me to read
for new defects. Its message closes with *"All 16 mutations red … Suite: 166 tests OK"*. The
count did rise (164 -> 166) — because 13 definitions were deleted and 19 added. Deleted, with
no replacement and no mention:

```
test_w5_two_runs_no_state_regression      test_w7_nothing_changed_one_line
test_w6_max_hours_forwarded_to_drain      test_w8_exclusions_reported_as_numbers
test_w6_drain_respects_zero_cap           test_w9_dry_run_touches_nothing
test_w7_summary_contract_fields_present   test_w10_shim_has_no_logic
                                          test_w11_writes_only_within_owned_roots
```
(plus `test_w4_lock_held_on_same_db_skips`, renamed to `…_with_marker`, and the three
`lock_holder`/stale-PID tests, whose mechanism was legitimately replaced by `flock`.)

Three of the lost behaviours are provably unpinned now, not merely untested:

- **D1** — hardcoding `max_hours=8` at the drain call (`weekly_run.py:283`) keeps the suite
  green: `fake_drain` records the value, nothing asserts it.
- **D2** — neutering `--dry-run` (`weekly_run.py:217`) keeps the suite green, **and the
  regression it hides is real**: in a temp HERMES the neutered runner wrote a pre-existing DB
  (12288 -> 57344 bytes) and emitted `digests/<p>/latest.md` + `reports/<p>/latest.md`. The
  pristine runner touched nothing (`db-sha 0a892eab3691->0a892eab3691`). `--dry-run` is the
  documented "print the plan and touch nothing" path — and e076c2f refactored exactly that
  CLI dispatch (`podcast_scraper.py` `__main__` -> `_cli_dispatch`, `weekly_run` straight into
  the lock).
- **D3** — pointing `weekly.sh` at `scripts/weekly_run.py` keeps the suite green (nothing in
  the repo references `weekly.sh` any more), while the deleted test was the guard against the
  superseded `~/.hermes/scripts` runner this work exists to replace.

The nine tests are not obsolete: re-inserted verbatim into a copy of HEAD
(`restore_deleted_tests.py`) they run `Ran 175 tests in 2.368s / OK`. The suite still
advertises them (`test_app.py:2192` — `"""W1-W11: the weekly runner orchestrates …"""`), and
the deletion also removes the only test satisfying LEARNINGS.md checklist item 8
(dirty-state / second-run idempotence, `LEARNINGS.md:45-47`) and the fix→test map of item 10
(`LEARNINGS.md:51-53`) for the runner.

**Fix:** restore the nine methods (green as-is), and if any is deliberately retired, say so in
the commit message and replace the behaviour with an equivalent assertion.

### G3-F2 · P31 / P2 · `dashboard_server.py:258-260` + `profiles.py:179-195, 220-231` · the absence proof's population is computed from profile JSONs, and its producer is unpinned · **medium**

`_profile_db_paths()` = `[profiles.db_path_for(p["name"]) for p in profiles.list_profiles()]`.
`list_profiles()` **silently `continue`s** on a profile file it cannot parse
(`profiles.py:184-187`), and `profiles.save()` writes that file non-atomically
(`profiles.py:230`), so a truncated/corrupt profile JSON removes its DB from the population.
A DB file left behind by a renamed or deleted profile is likewise not in the population at
all. In both cases reconcile's "no profile's DB backs it" is computed over a population
narrower than the set of DBs the shared transcripts dir actually has owners in — the same
class of bug 9c13303 fixed (`P31`), just moved one level out. The tests stub the producer
(`test_app.py:337, 1917` — `_profile_db_paths = lambda: []`), so **M3** (`return []`) is a
false survivor: nothing pins the function whose output decides what gets deleted.

**Fix:** enumerate the owners from the filesystem — `db/*.db` plus `LEGACY_DB` (skipping
zero-row DBs is unnecessary: a DB without the table already counts as unread) — or, at
minimum, treat an unparseable/unreadable profile as `unread` so nothing is deleted, and add a
test that exercises the real `_profile_db_paths()`.

### G3-F3 · P5 · `dashboard_server.py:2857-2863, 2649-2674` vs `fetch_transcripts.py:327-329` · "every writer takes the lock" is still overbroad: the dashboard's own CLI and its inline row writers are unlocked · **low-medium**

e076c2f routed every stage script's CLI entry through `dblock.run_locked` and named
`generate_report` as out of contract. Two writer paths are neither locked nor named:

1. `python3 dashboard_server.py --migrate` / `--reconcile` (the paths CLAUDE.md documents) run
   **outside** any lock, while the same two functions are the ones `weekly_run` runs *under*
   its lock.
2. The dashboard's inline row writers — `/api/request_transcribe`, `/api/unrequest`,
   `/api/dismiss_video`, `/api/undismiss_video`, `/api/promote_channel`, `/api/accept_term`
   (`dashboard_server.py:2651-2674, 2604+`) — write the DB during a weekly run; the fix only
   stops the dashboard from *spawning* stage scripts (`_writer_busy` at `2762-2773`).

Impact is low (SQLite serialises these short writes, `timeout=5.0`, and the discovery
re-visit never touches `transcript_status`), but the contract in the commit message and the
`dblock` docstring is stronger than what the code enforces.

**Fix:** wrap the `--migrate`/`--reconcile` CLI branches in `dblock.run_locked` (weekly_run
calls the functions in-process, so it is unaffected), and state the dashboard's short-write
path as a named exemption.

### G3-F4 · P28 / P34 · `test_app.py:45-60, 57-81` · the suite guard refuses deletes but only *detects* writes, and only under the transcripts dir · **low**

The wrapper covers `os.remove`, `os.unlink`, `Path.unlink`, `shutil.rmtree`; `os.rename`,
`os.replace`, `os.rmdir`, `shutil.move` and any plain write pass through, to be caught after
the fact by `tearDownModule`'s fingerprint — which covers `REAL_TRANSCRIPTS` only
(`test_app.py:35, 57-61`). A test that wrote into the real `~/.hermes/digests`,
`reports`, `profiles`, `db` or `logs` would finish green. **M5** shows the second layer is
itself unpinned (disabling the comparison stays green). No such write happened in this sweep:
my independent fingerprint of all five real dirs plus `podcast_tracker.db` and `.env` was
identical before/after the suite.

**Fix:** fingerprint the remaining owned roots too (cheap: the same helper, a few dirs), and
note in the header comment that writes are detected, not refused.

### G3-F5 · record · deferred lows + two residuals — **low, not re-litigated**

- Gate #1 F8 / gate #2 F8: `_scalar` / `_has_column` / `_top_picks` (`weekly_run.py:95-130`)
  still render an unreadable DB as `0` / `False` / `[]`. Gate #1 F9 / gate #2 F9: the
  capped-out predicate (`weekly_run.py:291-293`) is still a hand-copy of the producer's.
  Deferred by the author's severity triage; recorded so the deferral lives in the gate record.
- e076c2f F3 residual: the *names* in `PATH_ATTRS` (`weekly_run.py:47-52`) are still a
  hand-written list. A new import-frozen path attribute in a stage module under a name not in
  that dict is silently unchecked, and `test_g2f3_discovered_pin_set_is_exact`
  (`test_app.py:2517-2525`) asserts equality with a literal copy of the same list — it pins
  today's set, not the discovery rule.
- **LEARNINGS.md was not updated for this incident** (untouched across all four commits). The
  09-16 bug — a test that ran the real `reconcile()` against the real `~/.hermes/transcripts`
  and deleted all 28 `.txt` files, plus reconcile deleting every other profile's transcripts —
  has no **Fix log** entry and no checklist item, though CLAUDE.md Working Rule 6 and
  LEARNINGS.md's own header require one ("a delete/absence claim must be scoped to a proven
  population"; "a test must never reach production data"). P28/P31/P34 exist in the global
  catalogue but not in this repo's 12-item checklist, which is exactly the drift CAPTURE step 3
  warns about. Cheap to fix; recommended before the next sweep.

---

## Non-blocking observations

- **A skipped week still exits 0** (`weekly_run.py:225-233`). With the lock per-DB and the new
  SKIPPED block this is now an informed choice, not a silent one — recorded, not contested.
- `dashboard_server.py --reconcile` run *during* a weekly run still races the fetcher's
  `write file -> INSERT row` order (a file can be seen unbacked). Same root as G3-F3; the row's
  `full_text` keeps the content recoverable, and reconcile step 1 resets any `obtained` row
  whose transcript row is missing.
- `print(f"  Removed {removed} …")` now sits inside the `elif os.path.isdir(...)` branch
  (`dashboard_server.py:317-325`), so the line is absent both when a profile DB is unreadable
  and when the dir does not exist. Cosmetic; the skip path prints its own message.
- The lock file is never deleted (`dblock.py:14-15`, deliberate: unlinking a locked file lets a
  second process lock a new inode). No stray `*.writer.lock` files exist under the real
  `~/.hermes` (checked; only unrelated Hermes locks).

---

## Coverage honesty (binding)

Reviewed against the caller's lens: **35 generic patterns (P1–P35) + this repo's 12-item
checklist = 47 items**, of which **~20 were plausibly applicable** to this change (P1, P2, P3,
P5, P6, P8, P9, P10, P13, P14, P15, P18, P19, P21, P22, P24, P27, P28, P31, P34) — all were
examined, and every claim in table 1 was checked by mutation as well as by reading. Positively
checked and not fired: **P4** (no new hardcoded topic/date constants), **P7** (scoring
untouched), **P12** (the `drain()` extraction moves the loop verbatim), **P21** (the new
mechanisms are wired into their callers), **P22** (`podcast_scraper.main()`'s dict return has
one in-repo consumer), **P24**. **P28/P34 fired the other way** (the suite-reaches-production
bug the fix closes) and **P31 fired twice** (the fixed population, G3-F2's producer, G3-F1's
narrowed suite).

Not assessed (outside this lens): logic/algorithmic correctness, concurrency beyond the writer
lock, auth/authorization, injection, performance, dependency/supply-chain risk, API contract
compatibility, architecture, and — the one that matters most here — **test quality as a
discipline**, which is why G3-F1 rests on mutation evidence (D1/D2/D3) and a real
restore-and-run rather than on inspection. The 28-file restore is verified as byte-identical to
`transcripts.full_text` (the only surviving copy); it cannot be verified against the
pre-incident bytes. Behaviour changed in this range, so the orthogonal pass
(`requesting-code-review`) is recommended and was **not** silently substituted for this lens.
