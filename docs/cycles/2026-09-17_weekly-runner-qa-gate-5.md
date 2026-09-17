# QA gate #5 — weekly runner (learning-qa failure-pattern sweep)

**Repo:** `/Users/davemini/ProjectsMini1/ptd` (podcast-tracker-dashboard)
**Date:** 2026-09-17 · **Reviewer:** Hermes `learning-qa-sweep` (independent; no part in writing this code)
**Supersedes nothing:** gate-1…gate-4 (`docs/cycles/2026-09-16_weekly-runner-qa-gate*.md`) are left intact.

---

## Verdict block (paste-ready for Claude Code)

```
VERDICT:     APPROVED — 0 blocking findings. 1 low finding (non-blocking, G5-F1).
             Gate-4 F1 (medium) is genuinely CLOSED: verified in code, by live probe,
             and by mutation. The fix chose a stronger route than the gate's own
             flip instruction — it removed the silent-skip path entirely instead of
             making the skip visible.
             No test definition that existed at origin/main is missing at HEAD
             (accounting in table 3). Suite green by exit code.

RANGE:       origin/main..HEAD = b1e1716...efd9eb8 (6 commits: 7de637b, d01881c,
             9c13303, e076c2f, d3a5cfb, efd9eb8). Base hash verified present in this
             repo (git cat-file -e b1e1716 -> ok). Caller-named sub-range
             d3a5cfb..HEAD (= efd9eb8 alone, 4 files, +66/-19) reviewed as its own
             range, read line-by-line. Working tree: `git status --porcelain` = `?? docs/`
             only — no tracked edits, HEAD unchanged; the untracked dir holds the gate
             files. Committed state read; nothing in the repo was modified by this sweep.

RAN:         python3 -B test_app.py  ->  Ran 179 tests in 2.991s | OK   EXIT 0   (run twice)
             Mutation battery: 9 mutants + 2 no-op controls, full-tree copies, python3 -B.
             Both controls GREEN; 6/9 mutants RED, 3 GREEN (the 3 survivors are G5-F1,
             a test-coverage gap, not a live defect). Table 2.
             Live probes against the REAL run.sh, each with a temp HERMES_DIR, a free
             PORT, `open`/`curl` shimmed on PATH, PTD_RUNSH_MIGRATE_ONLY=1 (table 4).
             Real dashboard on port 9091 (PID 92728, up 5d) untouched throughout;
             no new probe process survived (ps check).
             Real ~/.hermes fingerprint (transcripts, *.db, digests/reports *.md,
             profiles) byte-identical before/after: only Hermes's own state.db mtime
             moved — the agent runtime's session DB, not written by any probe.

NOT ASSESSED: the lens covers one family (failures that hide). Not covered: logic and
             algorithmic correctness, concurrency and races, auth/authz, injection and
             other security classes, performance, dependency/supply-chain, API contract
             compatibility, test quality in general, architecture. Concurrency matters
             here (the now-unlocked --migrate) and is OUT of this lens — see observation 2
             and the second-lens recommendation.

SECOND LENS: behaviour changed (locking policy, CLI dispatch, launcher error path) ->
             recommend `requesting-code-review` over origin/main..HEAD as the orthogonal
             pass. This sweep is one family only.
```

---

## Table 1 — claim-by-claim verification of the fix commit (efd9eb8 / d3a5cfb..HEAD)

| # | Claim (commit message) | How verified | Result |
|---|---|---|---|
| 1 | "`--migrate` no longer takes the writer lock" | `dashboard_server.py:2866-2873` — the `dblock.acquire`/`release` + exit-3 block is gone; `migrate()` is called bare | **verified** (mutation M6 restores the lock → RED) |
| 2 | "`migrate()` is additive, idempotent DDL" | `migrate()` (`dashboard_server.py:121-256`) is `CREATE TABLE IF NOT EXISTS` + per-column `ALTER TABLE ADD COLUMN` gated on `PRAGMA table_info`; no `DROP`/`DELETE`/destructive DDL | **verified** |
| 3 | "the dashboard already runs it unlocked on profile switch/create" | `dashboard_server.py:2338`, `2366` — in-request `migrate(db_path_for(...))`, no lock, failures surfaced as `{"ok": false, "error": ...}` | **verified** (so the exemption is consistent, not novel) |
| 4 | "A real failure still raises and exits non-zero" | probe A (DB path is a directory) → `rc=1`, `sqlite3.OperationalError: unable to open database file`; probe B (corrupt DB file) → `rc=1`, `sqlite3.DatabaseError: file is not a database`. Mutation M5 (swallow it) → RED | **verified** |
| 5 | "run.sh prints a WARNING with the last lines of migrate's output instead of discarding it" | `run.sh:30-33`; probes A and B print the header **and** the exception line verbatim (table 4). M1 (the pre-fix `>/dev/null … \|\| true` line) → RED; M2 (WARNING to /dev/null) → RED; M10 (WARNING unconditional) → RED | **verified for the warning; the "with migrate's output" half is unpinned — that is G5-F1** |
| 6 | "run.sh keeps launching if migration fails" | `run.sh` has no `exit 1` in the branch; both tests assert `rc == 0` on the broken-DB path | **verified** |
| 7 | "`PTD_RUNSH_MIGRATE_ONLY` stops run.sh after the migrate step, for tests" | `run.sh:35`; probe D shows run.sh produces **no output at all** and exits 0 without launching anything; M8 (hook removed) → both tests fail after the 60 s process-group kill, i.e. the hook is load-bearing for the test | **verified** |
| 8 | "Tests: `--migrate` succeeds while another process holds the lock" | `test_app.py:2257-2262` (asserts rc 0 + "Migration complete"); independently re-run live: a real holder process owns `<db>.writer.lock` (`71744 PROBE-HOLDER …`) and `--migrate` returned **rc 0 in 0.06 s**, creating all 8 tables (table 4, C-lock) | **verified** (M6 → RED) |
| 9 | "the real run.sh shows a failed migration and prints nothing on a clean one" | `test_app.py:2111-2151` runs the **real** `run.sh` via `bash`, temp HERMES, `open`/`curl` shimmed, free port, `PTD_PROFILE` popped, process-group SIGKILL on timeout | **verified** — and see P34 note below |
| 10 | "open/curl shimmed, free port, process-group kill on timeout" | read at `test_app.py:2117-2141`; `ps` after the full battery shows **no** surviving `dashboard_server.py`/`run.sh` process — only the user's long-running dashboard on 9091 | **verified** (this is P34 handled correctly: a test that runs the real entry point does not inherit its side effects on this machine) |
| 11 | "Mutations red (2/2, -B)" | re-derived independently: my M1 and M2 are the two obvious ones and both are RED; M5/M6/M8/M10 add four more RED | **verified and strengthened** |
| 12 | "Suite: 179 tests OK; real transcripts intact" | `python3 -B test_app.py` → `Ran 179 tests in 2.991s | OK`, exit 0 (twice); 28 `~/.hermes/transcripts/*.txt` present; fingerprint identical | **verified** |
| 13 | dblock docstring now names `--migrate` as an exemption and drops it from "who takes it" | `dblock.py:20-22` vs `:33-35` — accurate against every lock site I could enumerate (`run_locked` in the 5 stage scripts + `--reconcile`; `weekly_run`/`overnight_pipeline` call `acquire` directly) | **verified by hand; not pinned by a test** (M7 survives — observation 5) |
| 14 | No stale consumer of the old "exit 3 = skipped" contract | grep for `--migrate` across the repo: the only caller is `run.sh`; docs (`profile_examples/README.md:19`, `SPEC.md`, `CLAUDE.md`) tell a human to run it directly, where a failure is a loud traceback | **verified** (P22 clean) |

---

## Table 2 — mutation battery (9 mutants + 2 controls, full-tree copies, `python3 -B`)

Harness: `shutil.copytree` per mutant into `tempfile.mkdtemp` (excluding `.git`/`__pycache__`),
one text edit per tree with the anchor asserted to occur exactly once, then `python3 -B test_app.py`.
No mutation touched the repo working tree.

| id | edit | target claim | real output | verdict |
|---|---|---|---|---|
| C1 | trailing space in a `run.sh` comment (no-op) | harness validity | rc 0 `Ran 179 tests … OK` | control GREEN |
| C2 | trailing space in a `dblock.py` comment (no-op) | harness validity | rc 0 `Ran 179 tests … OK` | control GREEN |
| M1 | `run.sh` back to the pre-fix line (`--migrate >/dev/null 2>&1 \|\| true`) | gate-4 F1 itself | rc 1 `FAIL: test_g4f1_failed_migration_is_shown` | **pinned** |
| M2 | the WARNING `echo` redirected to `/dev/null` | warning is visible | rc 1 `FAIL: test_g4f1_failed_migration_is_shown` | **pinned** |
| M5 | `dashboard_server.py` wraps `migrate()` in `try/except: pass` | "a real failure still exits non-zero" | rc 1 `FAIL: test_g4f1_failed_migration_is_shown` | **pinned** |
| M6 | `--migrate` put back under the lock (exit 3 when held) | the unlock itself | rc 1 `FAIL: test_each_writer_script_waits_for_the_lock` | **pinned** |
| M8 | `PTD_RUNSH_MIGRATE_ONLY` hook removed | the test can stop run.sh | rc 1 after 122 s: `FAIL: test_g4f1_failed_migration_is_shown` + `FAIL: test_g4f1_clean_migration_prints_no_warning` | **pinned** |
| M10 | WARNING printed unconditionally (`if … \|\| true; then`) | clean run stays quiet | rc 1 `FAIL: test_g4f1_clean_migration_prints_no_warning` | **pinned** |
| M3 | `run.sh`'s capture changed `2>&1` → `2>/dev/null` | *the reason* is shown | rc 0 `Ran 179 tests … OK` | **SURVIVOR** → G5-F1 |
| M4 | `run.sh:32` `tail -5` → `tail -0` (no reason printed) | *the reason* is shown | rc 0 `Ran 179 tests … OK` | **SURVIVOR** → G5-F1 |
| M7 | the dblock docstring exemption paragraph deleted | docstring claim accuracy | rc 0 `Ran 179 tests … OK` | **survivor** (doc-level, see observation 5) |

Controls both GREEN ⇒ a RED row is attributable to its edit. 6/9 mutants killed.

---

## Table 3 — test-definition accounting across `origin/main..HEAD`

Method: extract every `def test_\w+` name per commit from `git show <rev>:test_app.py` and
diff consecutive commits; then check membership against HEAD.

| commit | defs | added | removed |
|---|---|---|---|
| b1e1716 (base `origin/main`) | 127 | — | — |
| 7de637b | 142 | 15 (W1–W11 + 1 subprocess pin) | 0 |
| d01881c | 160 | 19 | 1 (`test_w4_single_instance_lock_skips_cleanly`) |
| 9c13303 | 164 | 4 | 0 |
| e076c2f | 166 | 15 | 14 (see below) |
| d3a5cfb | 177 | 11 (9 restored **verbatim** + 2 new) | 0 |
| efd9eb8 (HEAD) | 179 | 2 (`test_g4f1_failed_migration_is_shown`, `test_g4f1_clean_migration_prints_no_warning`) | 0 |

**Names removed and never re-added (5):**

| removed name | added by | removed by | flock-era replacement |
|---|---|---|---|
| `test_w4_single_instance_lock_skips_cleanly` | 7de637b | d01881c | `test_w4_lock_held_on_same_db_skips_with_marker` |
| `test_holder_pure_decision` | d01881c | e076c2f | `test_acquire_release_roundtrip`, `test_live_holder_blocks_and_is_named` |
| `test_release_leaves_another_owners_lock` | d01881c | e076c2f | `test_killed_holder_releases_lock`, `test_reentrant_in_process` |
| `test_stale_lock_from_dead_pid_is_taken_over` | d01881c | e076c2f | `test_killed_holder_releases_lock` (the kernel now releases a dead holder's lock) |
| `test_w4_lock_held_on_same_db_skips` | d01881c | e076c2f | same test **renamed** to `…_skips_with_marker` |

**`names that existed at origin/main and are missing at HEAD: NONE`** — no pre-existing test was
lost, at any commit in the range. All five lost names were *introduced inside this range* and every
one is a lock-mechanism test replaced by a stronger flock test (gate-4's "four replaced PID-lock
tests" and this five-name list are the same set under two readings: 3 PID-file-lock tests + 1
renamed W4 test + 1 pgrep W4 test). No test body was rewritten except the four assertions
`efd9eb8` changed in `TestStageScriptLocks` (`test_app.py:2257-2262`, from "skips with exit 3" to
"runs, exit 0, Migration complete") — that is the intended flip, not a loss.

Caveat, stated honestly: this accounting proves **name** survival. It does not prove body
identity across the whole range; gate-4 verified body identity for the nine W5–W11 restorations
(byte-identical, re-confirmed before this sweep), and `efd9eb8` restores nothing.

---

## Table 4 — live probes (real `run.sh`; temp HERMES, free port, `open`/`curl` shimmed, `PTD_RUNSH_MIGRATE_ONLY=1`)

| probe | setup | literal output (tail) | reading |
|---|---|---|---|
| A | `$HERMES/podcast_tracker.db` is a **directory** | `run.sh rc=0` / `WARNING: database migration failed — the dashboard may show errors or missing data:` / `  File "…/dashboard_server.py", line 124, in migrate` / `    conn = sqlite3.connect(target)` / `sqlite3.OperationalError: unable to open database file` | failure is visible **and** the reason is shown; dashboard still launches (by design) |
| B | DB file is **not** a SQLite database (20× a junk line) | `run.sh rc=0` / same header / `sqlite3.DatabaseError: file is not a database` | a *realistic* failure (not just the test's artificial break) takes the same path |
| C | clean temp HERMES | `run.sh rc=0` / `(no output)`; standalone `--migrate` → `Migration complete.` | clean launch is silent, as before the fix |
| C-lock | a real holder process owns `<db>.writer.lock` (`71744 PROBE-HOLDER 2026-09-17T10:28:39`) | `--migrate rc=0 in 0.06s` / `Adding 'dismissed' column…` … `Migration complete.` / `tables after: ['ai_analysis','channels','key_points','runs','sqlite_sequence','suggested_terms','transcripts','videos']` | the unlock is real and works under a live writer |

---

## FINDINGS

### G5-F1 · P10 · `test_app.py:2143-2150` (with `run.sh:30-33`) · the fix's *second* half is unpinned: the test asserts the WARNING header, not that migrate's output is shown · severity **low** · confidence **high (demonstrated)**

The commit message claims run.sh "prints a WARNING **with the last lines of migrate's output**
instead of discarding it". `test_g4f1_failed_migration_is_shown` asserts only
`assertIn("WARNING: database migration failed", out)`. Two mutants that strip the *reason* while
keeping the header both survive the whole suite:

- **M3** `run.sh:30` `2>&1` → `2>/dev/null` ⇒ `migrate_out` holds only stdout
  (`Running migration on <db>...`); the WARNING shows a path with **no reason at all** — rc 0,
  `Ran 179 tests … OK`.
- **M4** `run.sh:32` `tail -5` → `tail -0` ⇒ no output line is printed at all — rc 0, `OK`.

So the "make the failure visible" half of gate-4 F1 is pinned; the "…and diagnosable" half is not,
even though it is present in the shipped code (probes A and B) and is the part that tells the user
*why* their schema is stale.

**Fix (one line):** assert the reason, not just the header —
`self.assertIn("unable to open database file", out)` in `test_g4f1_failed_migration_is_shown`.
That is the exact string the broken-DB path produces today (probe A, and the test's own way of
breaking the DB), so the assertion is exact rather than a floor.
**Non-blocking:** the live behaviour is correct; nothing here hides a failure from the user.

---

## Non-blocking observations (recorded, not findings)

1. **The test hook exits 0 and prints nothing** (`run.sh:35`). If `PTD_RUNSH_MIGRATE_ONLY` is ever
   set in a user's shell profile, `./run.sh` migrates, prints nothing, exits 0 and never starts the
   dashboard — "did nothing" indistinguishable from "started fine". The hook is deliberate and is
   documented as test-only; M8 proves removing it breaks the test, so it must stay. A one-word
   marker (`echo "migration-only mode (PTD_RUNSH_MIGRATE_ONLY)"`) would make it self-explaining.
2. **`--migrate` now runs DDL concurrently with a live writer** (`dashboard_server.py:2872`).
   Both paths stay loud: run.sh prints the WARNING with the reason, and `weekly_run`'s
   `stage("migrate", …)` failure is named in `failed` and forces a non-zero exit
   (`weekly_run.py:271`). A partial migrate is possible (Python's sqlite3 autocommits each DDL) but
   is idempotent on re-run and was already the profile-switch exposure. **Races are outside this
   lens** — handed to the second lens, not cleared by this gate.
3. **run.sh keeps launching on a failed migration** (`run.sh:30-37`) — the deliberate trade-off the
   gate-4 flip invited (visibility, not refusal). The warning text ("may show errors or missing
   data") is honest about it.
4. **The success path discards migrate's output** (`migrate_out` is captured and never printed when
   the command succeeds — probe C shows run.sh is completely silent on a clean launch). Identical to
   the pre-fix `>/dev/null`; no regression, no confirmation either.
5. **dblock's exemption docstring is unpinned** (`dblock.py:33-35`; M7 survives). Doc-level claims
   have never been test-pinned here; gate-4 checked the previous wording by hand and so did this
   sweep (table 1 #13).
6. **Deferred lows are genuinely unchanged** — `efd9eb8` touches only
   `dashboard_server.py`, `dblock.py`, `run.sh`, `test_app.py`. Still open, still deferred:
   - gate-2 F8 / gate-1 F8: `_scalar` / `_has_column` / `_top_picks` still render an unreadable DB
     as `0`/`False`/`[]` (`weekly_run.py:95-112`, untouched).
   - gate-2 F9 / gate-1 F9: the capped-out predicate is still a hand-copy of the producer's
     (`weekly_run.py:291-293`).
   - gate-3 F4: the test guard still wraps only the four delete functions and fingerprints
     `REAL_TRANSCRIPTS` only (`test_app.py:40-81`) — a test writing into the real `digests`,
     `reports`, `profiles`, `db` or `logs` would finish green. Independently corroborated here: my
     own fingerprint covered transcripts + all `*.db` + digests/reports `*.md` + profiles and was
     **identical** before and after the suite, so no such write happened in this sweep either.
   - gate-3 F5 residuals: `PATH_ATTRS` (`weekly_run.py:47-52`) is still a hand-written list pinned
     by a test that holds a literal copy; `LEARNINGS.md` still has **no Fix log entry** for the
     2026-09-16 reconcile/transcript-deletion incident (`git log origin/main..HEAD -- LEARNINGS.md`
     is empty), which CLAUDE.md Working Rule 6 requires.
7. **Housekeeping:** `TestRunShMigrate` leaves its `mkdtemp` tree behind (2 per suite run), matching
   the existing pattern in `TestStageScriptLocks`; trivial.

---

## Coverage statement (binding)

Clean against the applicable lens patterns — **P1, P2, P5, P6, P10** of the global catalogue
(35 patterns), plus the project's own 12-item checklist in `LEARNINGS.md` (items 2, 3, 5, 6, 10 were
in play; 6 and 10 fired as G5-F1 and table 1 #13). Also checked and **not** triggered: P8/P19
(no producer/consumer contract changed; run.sh branches on the exit code, not on parsed output),
P13 (the exemption is scoped to `--migrate` alone, and pinned by a test), P21/P25 (the warning is
wired through the real launcher and exercised by a test that runs the real `run.sh`), P22 (no stale
consumer of the old exit-3 contract), P24 (no success-output parsing in the fix), P27/P29 (the new
assertions are exact and are killed by mutation), P28/P34 (the real-entry-point test redirects
HERMES, the port, `open`/`curl` and the process group, and leaves no stray process), P31/P33
(absence claims here were scoped to the on-disk DB set / a name-set accounting, both stated).

**Not assessed:** the lens's excluded families, named in the verdict block — most relevantly
**concurrency/races** around the now-unlocked `--migrate`, and general test quality.
Primary artifacts read: `run.sh`, `dashboard_server.py` (`migrate`, `__main__`, profile
create/switch handlers), `dblock.py`, `weekly_run.py` (`stage()`), `test_app.py`
(`TestRunShMigrate`, `TestStageScriptLocks`, the module guard), `LEARNINGS.md`, all four prior gates.

**Scope narrowing:** none imposed by the caller beyond the range and the deferral of the named lows.
`docs/cycles/` is untracked in this repo, so the gate record itself is the only file this sweep writes.
