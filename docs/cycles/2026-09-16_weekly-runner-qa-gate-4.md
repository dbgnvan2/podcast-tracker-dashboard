# QA gate #4 — weekly-runner fix commits (origin/main..HEAD, incl. e076c2f..HEAD) — learning-qa failure-pattern sweep

Independent review (no memory of writing this code). Supersedes nothing: gate #1
(`docs/cycles/2026-09-16_weekly-runner-qa-gate.md`), gate #2 (`…-gate-2.md`) and gate #3
(`…-gate-3.md`) are left as written (sizes/mtimes unchanged, verified).

---

## Paste-ready verdict for Claude Code

```
VERDICT:     REJECTED — 1 blocking finding (G4-F1, medium). 0 high.
             Gate-3 F1, F2 and F3 are all genuinely CLOSED (verified in code AND by
             mutation). No test definition lost anywhere in the range except the four
             lock tests the flock rewrite replaced (accounting in table 3).

RANGE:       origin/main..HEAD = b1e1716...d3a5cfb (5 commits: 7de637b, d01881c,
             9c13303, e076c2f, d3a5cfb). Base hash verified present in this repo
             (git cat-file -e b1e1716). Caller-named sub-range e076c2f..HEAD (= d3a5cfb
             alone, 251 diff lines) reviewed as its own range, read line-by-line.
             Working tree: `git status --porcelain` = `?? docs/` only (the three
             untracked prior gate files) — committed state only, no tracked edits.

RAN:         python3 -B test_app.py            -> Ran 177 tests in 2.388s | OK   EXIT 0
             /opt/homebrew/bin/python3.14 -B test_app.py -> Ran 177 tests in 2.355s | OK  rc 0
             16 mutants in 16 copied trees (python3 -B), 2 controls: both controls GREEN,
             14/14 expected RED -> RED.
             No probe wrote or deleted anything under ~/.hermes; all probes used a temp
             HERMES_DIR / tempfile.mkdtemp tree. Real-HERMES fingerprint: transcripts,
             digests, reports, db, profiles, podcast_tracker.db, .env all byte-identical
             before/after (only this reviewer's own logs/agent.log + logs/errors.log moved).

BLOCKING:    G4-F1 · P2 (silent failure) · run.sh:29 + dashboard_server.py:2865-2876
             `--migrate` now *skips* when another writer holds the DB (exit 3, correct and
             loud on the CLI) — but run.sh, the only caller that runs it automatically
             (and the documented GUI entry point), sends it to /dev/null and forces
             success: `python3 dashboard_server.py --migrate >/dev/null 2>&1 || true`.
             Demonstrated end-to-end: the skip is real (a deliberately dropped
             `key_points` table was NOT recreated) and the dashboard then starts against
             an un-migrated DB with zero signal to the user. The commit's own rationale
             ("run.sh runs it on every launch") is defeated by that redirect.

FLIP TO APPROVED: make the skip reachable in run.sh (stop swallowing stderr / tee the
             skip line to ~/.hermes/logs/, or record it in the dashboard), and pin it with
             a test that holds the lock and asserts the skip is visible in run.sh's output.
             One-line change; nothing else is outstanding on the three named findings.

SECOND LENS: behaviour changed (locking, reconcile scope, CLI dispatch, run.sh migration
             path) -> recommend `requesting-code-review` over e076c2f...HEAD as the
             orthogonal pass; this sweep is one family (failures that hide) only.
```

---

## Table 1 — claim-by-claim verification (each claim read in code *and* mutation-checked)

| # | Claim (commit) | How verified | Mutation | Result |
|---|---|---|---|---|
| 1 | d3a5cfb F1: the nine W5–W11 tests are restored **verbatim** | method bodies extracted from `git show <rev>:test_app.py` and compared textually | — | **verified**: 9/9 **byte-identical** to 9c13303 *and* to d01881c (the commit that lost them) |
| 2 | d3a5cfb F1: the suite is whole again | def-count per commit `127/142/160/164/166/177`; runner's own count `Ran 177` | — | **verified**: e076c2f 166 → HEAD 177 = +9 restored +2 new (arithmetic closes) |
| 3 | d3a5cfb F1: "all pass unchanged" | suite green before and after; restored tests exercised by mutation | M1–M4, M13–M15 | **verified**: 8 mutants RED across the restored W5–W11 tests |
| 4 | d3a5cfb F1: new `test_spec_every_w_criterion_has_a_test` fails if any W1..W11 loses its last test | `test_app.py:2323-2331` (per-W-id `any()` assertion, not a floor) | M5 | **verified**: renaming the last W8 test away → `FAIL: … (spec='W8')` |
| 5 | d3a5cfb F1: "the only tests removed since 9c13303 are the four written for the old PID-file lock, replaced by the flock tests" | per-commit name sets (table 3) | — | **verified with one wording caveat**: the four *lost names* are 3 PID tests + the renamed W4 test; the 4th *deletion* is the pgrep test removed a commit earlier (d01881c). Every removed behaviour has a named, stronger flock replacement |
| 6 | d3a5cfb F2: `_profile_db_paths()` enumerates DB **files on disk** (`db/*.db` + legacy) | `dashboard_server.py:258-268` | M6 (old `[]` survivor), M7 (revert to profile JSONs), M12 (drop legacy) | **verified**: all three RED; M6 was gate-3's *false survivor* and is now pinned |
| 7 | d3a5cfb F2: an unparseable profile JSON / a DB with no JSON still counts as an owner | new test runs the **real** function (`test_app.py:369-397`) | M7 | **verified**: reverting to `list_profiles()`-derived paths → RED |
| 8 | d3a5cfb F2 is exhaustive (no third DB location) | `profiles.db_path_for()` (`profiles.py:133-138`) returns only `DB_DIR/podcast_<slug>.db` or `LEGACY_DB`; `load()` overwrites any JSON-supplied `db_path` (`profiles.py:211`); live set = 4 `db/*.db` + legacy = the 5 DBs on disk | — | **verified** as of today's conventions (see observation 2) |
| 9 | d3a5cfb F3: `--migrate` takes the lock, does **not** wait, skips loudly with exit 3 | `dashboard_server.py:2865-2876`; live probe under a real holder process | M8 (unlocked), M9 (exit 0), M11 (message changed) | **verified**: rc 3 in 0.06 s under a live holder *with the default 2 h wait setting*, holder named, and the skip is real (dropped `key_points` stayed dropped) |
| 10 | d3a5cfb F3: `--reconcile` waits for the lock | `dashboard_server.py:2881-2882`; added to the `SCRIPTS` matrix (`test_app.py:2191`) | M10 | **verified**: RED |
| 11 | d3a5cfb F3: dblock's docstring names who takes the lock and the exemptions "accurately" | cross-checked every DB-row writer in the dashboard's POST surface against `dblock.py:20-32` — promote_channel, accept/reject_term, request/unrequest, dismiss/undismiss, set/create_profile migrate: all named; `generate_report.py` really has no INSERT/UPDATE/DELETE/commit | — | **verified** for DB rows; file writers are unnamed (observation 3) |
| 12 | Deferred lows unchanged by this commit | `weekly_run.py:95-112` (`_scalar`→0), `weekly_run.py:291-293` (hand-copied predicate), `test_app.py:45-81` (guard wraps 4 delete functions, fingerprints transcripts only), `git log b1e1716..HEAD -- LEARNINGS.md` empty | — | **verified still deferred**, exactly as triaged (G4-F3 records them) |

---

## Table 2 — mutation battery (16 mutants + 2 controls; one edit per copied tree, `python3 -B`)

Harness note, recorded because it nearly invalidated the pass: the first harness copied only
top-level files, and **every** mutant — including a trailing-space no-op control — came back
RED. The control is what caught it (missing `sources/`, `profile_examples/` dirs). The battery
below uses full-tree copies and two no-op controls; both controls are GREEN, so a RED row is
attributable to its edit.

| id | edit | target claim | real output | verdict |
|---|---|---|---|---|
| C1 | trailing space in `weekly_run.py` (no-op) | harness validity | rc 0 `Ran 177 tests … OK` | control GREEN |
| C2 | trailing space in `dblock.py` (no-op) | harness validity | rc 0 `Ran 177 tests … OK` | control GREEN |
| M1 | `drain(max_hours=args.max_hours)` → `max_hours=8` | gate-3 D1 (was a false survivor) | rc 1 `FAIL: test_w6_max_hours_forwarded_to_drain` | **pinned** |
| M2 | `--dry-run` `return 0` removed | gate-3 D2 (hid a real regression) | rc 1 `FAIL: test_w9_dry_run_touches_nothing` | **pinned** |
| M3 | `weekly.sh` → `$HOME/.hermes/scripts/weekly_run.py` | gate-3 D3 | rc 1 `FAIL: test_w10_shim_has_no_logic` | **pinned** |
| M4 | `drain` loop enters even with a 0-hour cap | W6 zero cap | rc 1 `FAIL: test_w6_drain_respects_zero_cap` | **pinned** |
| M5 | last W8 test renamed away | F1 spec-coverage guard | rc 1 `FAIL: test_spec_every_w_criterion_has_a_test (spec='W8')` | **pinned** |
| M6 | `_profile_db_paths()` → `[]` | F2 producer (gate-3 M3 survivor) | rc 1 `FAIL: test_g3f2_real_owner_enumeration…` | **pinned** |
| M7 | back to `[db_path_for(p) for p in list_profiles()]` | F2 population | rc 1 `FAIL: test_g3f2_real_owner_enumeration…` | **pinned** |
| M8 | `--migrate` unlocked | F3 | rc 1 `FAIL: test_each_writer_script_waits_for_the_lock` | **pinned** |
| M9 | `--migrate` skips but exits 0 | F3 | rc 1 same test | **pinned** |
| M10 | `--reconcile` unlocked | F3 | rc 1 same test (`script='dashboard --reconcile'`) | **pinned** |
| M11 | skip message reworded | F3 "loud" claim | rc 1 same test | **pinned** |
| M12 | legacy DB dropped from the population | F2 exact-set assertion | rc 1 `FAIL: test_g3f2_real_owner_enumeration…` | **pinned** |
| M13 | hardcoded `/Users/…` literal in `weekly_run.py` | restored W11 | rc 1 `FAIL: test_w11_writes_only_within_owned_roots` | **pinned** |
| M14 | "No change this week" line reworded | restored W7 | rc 1 `FAIL: test_w7_nothing_changed_one_line` | **pinned** |
| M15 | exclusion labels removed from the summary | restored W8 | rc 1 `FAIL: test_w7_summary_contract_fields_present` + `…test_w8_exclusions_reported_as_numbers` (failures=2) | **pinned** |

The eight mutants landing on restored/added tests (M1–M5, M13–M15) are the evidence that the
restore is not a vacuous "tests exist" pass: each restored test fails when its behaviour is
broken.

---

## Table 3 — test-definition accounting across the whole range (the caller's explicit ask)

Method bodies extracted per commit from `git show <rev>:test_app.py`; name sets differenced
commit-by-commit and base-to-HEAD.

| commit | `def test_*` at that commit | names lost vs previous |
|---|---|---|
| b1e1716 (origin/main base) | 127 | — |
| 7de637b | 142 | — |
| d01881c | 160 | `test_w4_single_instance_lock_skips_cleanly` |
| 9c13303 | 164 | — |
| e076c2f | 166 | 13 (below) |
| d3a5cfb (HEAD) | 177 | none |

`9c13303 → e076c2f` removed 13: the nine W5–W11 (restored by d3a5cfb, byte-identical) plus
the four lock-mechanism tests below. `b1e1716 → HEAD`: **zero** names lost besides those four;
`e076c2f → HEAD`: zero.

| removed definition | mechanism it tested | flock replacement at HEAD |
|---|---|---|
| `test_holder_pure_decision` | decision from the lock file's *content* (alive/stale PID) | `test_empty_lock_file_does_not_let_a_second_writer_in` (content no longer decides) + `test_live_holder_blocks_and_is_named` |
| `test_release_leaves_another_owners_lock` | `release()` not stealing another PID's lock | `test_reentrant_in_process` (release frees only at depth 0) + `test_other_db_not_blocked` |
| `test_stale_lock_from_dead_pid_is_taken_over` | staleness by PID liveness | `test_killed_holder_releases_lock` (SIGKILL → kernel release; strictly stronger) |
| `test_w4_single_instance_lock_skips_cleanly` (lost at d01881c) | pgrep-based `overnight_pipeline._already_running()` guard (function no longer exists — grep-verified) | `test_w4_lock_held_on_same_db_skips_with_marker` (a *real* holder process, superset of the monkeypatched version) |
| `test_w4_lock_held_on_same_db_skips` | same, renamed | `test_w4_lock_held_on_same_db_skips_with_marker` — present at HEAD, assertions are a superset (adds the SKIPPED marker) |

So: **4 definitions removed across the range, all lock-mechanism tests superseded by the flock
rewrite, 1 renamed (not lost), 0 unexplained.** The commit's phrasing ("the four written for
the old PID-file lock") folds the renamed W4 test into that four; the fourth *deletion* is the
pgrep test, removed one commit earlier. Cosmetic, not a coverage gap.

---

## Table 4 — evidence (every row is a command run in this sweep, with its real output tail)

| command | real output tail |
|---|---|
| `pwd`; `git rev-parse --show-toplevel`; `git status --porcelain` | `/Users/davemini/ProjectsMini1/ptd`; same; `?? docs/` |
| `git log --oneline origin/main..HEAD`; `git rev-parse origin/main HEAD` | 5 commits `d3a5cfb, e076c2f, 9c13303, d01881c, 7de637b`; base `b1e1716…`, HEAD `d3a5cfb…` |
| `git diff b1e1716...HEAD > /tmp/sweep-g4-full.diff`; `…e076c2f...HEAD > …-fix.diff` | `1781` and `251` lines; `11 files changed, 1450 insertions(+), 59 deletions(-)` |
| def/name inventory script (`/tmp/g4/verbatim.py`) | `127/142/160/164/166/177`; `nine restored tests … VERBATIM_vs_d01881c=True` ×9; `names in b1e1716 not at HEAD: none` |
| `python3 -B test_app.py` (3.9.6, `/usr/bin/python3`) | `Ran 177 tests in 2.388s` / `OK`, `PY39_EXIT=0` |
| `/opt/homebrew/bin/python3.14 -B test_app.py` | `Ran 177 tests in 2.355s` / `OK`, `PY314_EXIT=0` |
| `mutants2.py` (13 trees incl. control) | `SUMMARY: C1_noop:GREEN M1_D1:RED … M12_F2c:RED` |
| `mutants3.py` (4 trees incl. control) | `M13_W11:RED M14_W7:RED M15_W8:RED C2_ctrl:GREEN` |
| `probe_migrate.py` (temp HERMES + real holder process) | `[A] rc=0` creates DB; `parent sees: holder()= 26353 g4-holder …`; `[B] rc=3 in 0.06s` → `'Migration skipped: another pipeline writer holds … (26353 g4-holder …)'`, tables after skip `… 'channels', 'runs', …` — **`key_points` still missing**; `[C] bash stdout='LINERC=0'`, visible output `''` — swallowed; `[D] rc=0`, tables now include `key_points` |
| REAL `~/.hermes` fingerprint (size+mtime_ns of transcripts/, digests/, reports/, db/, profiles/, logs/ + `podcast_tracker.db` + `.env`), before vs after all of the above | `UNCHANGED transcripts / digests / reports / db / profiles / podcast_tracker.db / .env`; only `logs/agent.log` + `logs/errors.log` moved — content inspected: this reviewer's own Hermes session warnings (`Tool terminal returned error … heredoc BLOCKED`, `search_files returned error`), not the suite |
| `find ~/.hermes -maxdepth 3 -name '*.writer.lock'`; `ls ~/.hermes/transcripts/*.txt \| wc -l` | none; `28` |
| `grep -n 'os.remove\|os.unlink\|shutil.rmtree\|…' *.py` (prod code, excl. test_app) | 4 delete sites total; the only population-scoped one is `dashboard_server.py:331` (fixed); siblings are single-artifact deletes (`fetch_transcripts.py:248,295`, `skiplist.py:78-81`, `dashboard_server.py:2643,2649`) |
| dashboard POST-endpoint + `conn.execute` enumeration | 20 endpoints; DB-row writers: promote_channel, accept/reject term, request/unrequest, dismiss/undismiss, set/create_profile migrate — all named in `dblock.py:20-32` |
| `grep -nE 'INSERT\|UPDATE\|DELETE\|commit\(' generate_report.py` | none — read-only, as the docstring claims |
| `git log --oneline b1e1716..HEAD -- LEARNINGS.md CLAUDE.md` | empty (untouched) |
| `ls -l docs/cycles/` | gate #1 17335 B, gate #2 22077 B, gate #3 20933 B — original sizes/mtimes, not overwritten |

---

## Findings

### G4-F1 · P2 (silent failure) · `run.sh:29` + `dashboard_server.py:2865-2876` · the new skip path means the GUI launch can silently run the dashboard against an un-migrated DB · **medium — blocking**

d3a5cfb's F3 fix is correct on the CLI: with another writer holding the DB lock, `--migrate`
prints `Migration skipped: another pipeline writer holds <db> (<holder>).` and exits 3 — and it
does so *without waiting*, which is what run.sh needs (probe: rc 3 in 0.06 s while the default
`PTD_LOCK_WAIT_SEC` is 2 h). The defect is in the only caller that runs it automatically:

```
run.sh:29   python3 dashboard_server.py --migrate >/dev/null 2>&1 || true
```

Both stdout and stderr go to `/dev/null`, and `|| true` forces success. Measured on the exact
line, with a real holder process: `LINERC=0`, visible output `''`. Meanwhile the skip is real —
the probe's deliberately dropped `key_points` table was still missing after the launch-time
migrate, so the dashboard would have started against a stale schema; after the holder exited,
the same invocation recreated the table (rc 0).

Before this range, `--migrate` always migrated, so the redirect was harmless noise suppression.
After it, "the DB schema is up to date at launch" is no longer guaranteed, and the signal that
would explain a subsequent query failure is discarded at the one place a GUI-first user
actually triggers it (CLAUDE.md "Running Locally" → `./run.sh`). The commit calls the skip
"honest and loud"; here it is neither. Trigger: any concurrent writer (`overnight_pipeline`
holds the lock up to 8 h and does **not** migrate) plus a pending additive column — the routine
change this project makes (`dashboard_server.py:migrate()`).

**Fix:** surface the skip where run.sh can't discard it — e.g.
`python3 dashboard_server.py --migrate 2>&1 | tee -a "$HOME/.hermes/logs/dashboard_migrate.log" || true`
(or have the skip record itself in the dashboard's log / a stats field), and pin it with a test
that holds the lock and asserts the skip is visible through `run.sh` rather than only through
the CLI. Nothing else about F3 needs to change.

### G4-F2 · record · gate-3 F1/F2/F3 closed — **no further action**

F1: nine tests restored byte-identically, the coverage guard fires, and eight mutants prove the
restored tests bite. F2: the population is enumerated from disk and pinned by three mutants
(including the one that survived gate #3). F3: both CLI entries are locked and pinned by four
mutants plus a live probe. Full accounting in tables 1–3.

### G4-F3 · record · deferred lows, unchanged and still deferred — **low, not re-litigated**

Author's triage stands; recorded so the deferrals stay in the gate record:

- Gate-3 F4 — the suite guard refuses deletes but only *detects* writes, and fingerprints
  `transcripts/` only (`test_app.py:45-81`); `os.rename/os.replace/os.rmdir/shutil.move` pass
  through. No such write happened in this sweep (fingerprint above).
- Gate-3 F5 — `weekly_run.paths` (`PATH_ATTRS`, `weekly_run.py:47-52`) is still a hand-written
  name list, and `test_g2f3_discovered_pin_set_is_exact` still asserts a literal copy of it.
- Gate-1/2 F8 — `_scalar`/`_has_column`/`_top_picks` (`weekly_run.py:95-130`) still render an
  unreadable DB as `0`/`False`/`[]`, so `captions unproven: 0` can mean "column missing".
- Gate-1/2 F9 — the capped-out predicate (`weekly_run.py:291-293`) is still a hand-copy of
  `overnight_pipeline.py:65-69`.
- Gate-3 F5 residual — `LEARNINGS.md` is untouched across all five commits: the 09-16 incident
  (a test deleting the user's 28 real transcripts; reconcile deleting other profiles') still has
  no **Fix log** entry and no checklist item, though CLAUDE.md Working Rule 6 and the
  LEARNINGS.md header require one. Gate #3 recommended it "before the next sweep"; it is now
  twice deferred.

---

## Non-blocking observations

1. **The spec-coverage guard is coarser than the incident.** `test_spec_every_w_criterion_has_a_test`
   (`test_app.py:2323-2331`) checks that each W-id keeps *at least one* test (`any()` over
   `test_w<i>_*`). It catches the gate-3 incident (all nine gone) and M5 proves it fires, but it
   would not catch losing *one of several* — e.g. `test_w7_nothing_changed_one_line` while
   `test_w7_summary_contract_fields_present` remains. A per-behaviour manifest (one row per
   spec ID → test names) would close that; the test's own comment states its scope accurately,
   so this is a strengthening idea, not a false claim.
2. **F2's population still mirrors a convention rather than the producer.** `_profile_db_paths()`
   globs `db/*.db` + `LEGACY_DB`, which today is exactly where `profiles.db_path_for()` puts
   things (verified: 4 `db/*.db` + legacy = the 5 DBs on disk). A future third location would
   narrow the absence proof again. Cheap hardening: also glob `profiles.HERMES/"*.db"`, or
   assert in a test that the enumerated set equals `{db_path_for(p) for p in every profile JSON}
   ∪ disk` — the two sources cross-checking each other.
3. **File writers stay unlocked and unnamed.** `dblock`'s exemption list is DB-row scoped and
   accurate; the dashboard's non-DB writes (`skiplist.add_skip_title` — the very file a
   concurrent discovery reads, `runstate.clear_transcribe_result`, digest file delete/write at
   `dashboard_server.py:2634-2651`, `.env` edits) are unmentioned. `skiplist` already writes via
   `os.replace`, so readers see old-or-new; low risk, worth a word in the docstring.
4. **Counting method differs from gate #3's table.** My per-commit `def test_*` counts
   (142/160/166/177) match the runner's own `Ran N tests` line and close arithmetically
   (166 + 9 restored + 2 new = 177; 166 + 9 = 175, which is the count gate #3's restore
   experiment observed). Gate #3 recorded 173/192/200 for the same commits, so its regex was
   broader. Both gates agree on behaviour and on the nine-test loss; only that row differs.
5. **A skipped week still exits 0** (`weekly_run.py:225-233`) with an explicit SKIPPED block —
   an informed choice, recorded (not contested) since gate #2.
6. **The lock file is never deleted** (deliberate, `dblock.py:14-15`); no stray `*.writer.lock`
   exists under the real `~/.hermes` (checked).

---

## Coverage honesty (binding)

Reviewed against the caller's lens: **35 generic patterns (P1–P35) + this repo's 12-item
checklist = 47 items**, of which **18 were plausibly applicable** to this change: P1, P2, P3,
P5, P6, P10, P12, P19, P21, P27, P28, P31, P34 plus checklist items 2 (failure visibility),
4 (scope completeness), 6 (ground-truth check), 10 (fix→test map), 11–12 (concurrent writer /
background worker guard). All 18 were examined. Positively checked and not fired: **P1** (no
new transient-as-permanent write; the skip is an explicit retryable instruction), **P3** (the
DB population is now enumerated broadly — observation 2 is the residual), **P4** (no new
hardcoded topic/date constants; M13 proves W11 guards path literals), **P6/P12** (reconcile's
status resets still require a real `transcripts` row; the `drain()` extraction moved the loop
verbatim), **P19** (`DISCOVERY_RESULT_KEYS`/`discovery_result()` make the producer's contract
fail loudly; `SUMMARY_DISC_KEYS` is test-pinned as a subset), **P21** (every new mechanism is
wired to a caller: both CLI entries, the dashboard's `_writer_busy`, the new tests).
**P2 fired once** (G4-F1); **P27/P28/P31/P34 fired the other way** — the range *closes* a test
that reached production data (P28/P34) and a narrowed absence proof (P31), and gate #3's
unfailable-producer survivor (P27) is now pinned by M6.

Not assessed (outside this lens): logic/algorithmic correctness, concurrency beyond the writer
lock, auth/authorization, injection, performance, dependency/supply-chain risk, API contract
compatibility, architecture, and test quality as a discipline — the last is why the F1 verdict
rests on mutation evidence and byte-comparison rather than on inspection. Behaviour changed in
this range, so the orthogonal second lens (`requesting-code-review`) is recommended and was
**not** silently substituted for this one.

No probe wrote or deleted anything under `~/.hermes` (binding caller instruction): all probes
ran against `tempfile.mkdtemp` roots or a temp `HERMES_DIR`, and the pre/post fingerprint of
every real data root is identical.
