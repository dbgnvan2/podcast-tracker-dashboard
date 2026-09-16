#!/usr/bin/env python3
"""Weekly runner — one writer per DB, every stage, one honest summary.

Purpose: the repo-native entry point a Hermes cron job (no_agent mode) calls on a
         weekly schedule. It runs the full pipeline against ONE pinned profile's DB
         and prints a compact summary a chat delivery can read verbatim. It replaces
         the stale `~/.hermes/scripts` copy that ran a superseded formula and wrote
         this repo's DB behind its back — the rule now is one writer, many readers.
Spec:    session task W1-W11 (weekly runner).
Tests:   test_app.py::TestWeeklyRun

Stage order (W2): migrate -> reconcile -> discovery -> literature (if the profile
enables it) -> drain(fetch/analyze/digest) -> advisor report. Every stage is guarded; a failure is recorded and named but the
output stages still run, and the process exits non-zero if anything failed — a
failure must never look like a clean run.

Profile pinning (W3): the profile is set via PTD_PROFILE *before* the stage modules
are imported, because they resolve their DB/dirs at import time (the P5 mechanism
from "Harden spawned-job profile scoping"). Deferred imports below are deliberate.
"""
import os
import sys
import argparse
import sqlite3
from pathlib import Path

import dblock
import profiles  # safe to import early: it resolves the profile lazily, honoring PTD_PROFILE

REPORT_N = 8  # advisor-report window (last N analyzed sources)
STAGE_PLAN = ["migrate", "reconcile", "discovery", "literature (if enabled)",
              "drain (fetch+analyze+digest)", "report"]


# The discovery-result keys the summary reads. Must be a subset of
# podcast_scraper.DISCOVERY_RESULT_KEYS (test-pinned, QA gate F6).
SUMMARY_DISC_KEYS = ("new", "reupload_dropped", "skiplist_dropped")

# Every stage module this runner calls, and the import-frozen path attributes they
# may define, each mapped to what it must equal for the pinned profile. The
# (module, attribute) pairs actually checked are DISCOVERED from the modules, so a
# new frozen path in a stage module is checked without editing a list; the exact
# discovered set is test-pinned (QA gate #2 F3).
STAGE_MODULES = ("dashboard_server", "podcast_scraper", "fetch_transcripts",
                 "analyze_transcripts", "generate_digest", "generate_report",
                 "ingest_literature", "overnight_pipeline")
PATH_ATTRS = {
    "DB_PATH": lambda cfg: cfg["db_path"],
    "DIGEST_DIR": lambda cfg: cfg["digest_dir"],
    "REPORTS_DIR": lambda cfg: cfg["reports_dir"],
    "TRANSCRIPTS_DIR": lambda cfg: profiles.HERMES / "transcripts",
}


def pinned_attrs():
    """[(module name, attribute)] for every PATH_ATTRS name a stage module defines."""
    out = []
    for mod_name in STAGE_MODULES:
        mod = sys.modules.get(mod_name) or __import__(mod_name)
        out += [(mod_name, a) for a in PATH_ATTRS if hasattr(mod, a)]
    return out


# ── profile pin check (W3, QA gate F1) ───────────────────────────────────────
def pin_mismatches(cfg):
    """Every stage module path that does NOT match the pinned profile.

    Stage modules resolve their DB/dirs at import. In a fresh process (the cron
    path) PTD_PROFILE is set first, so they match. An in-process caller that
    imported them earlier would silently write another profile's DB — so check,
    and refuse to run on any mismatch."""
    bad = []
    for mod_name, attr in pinned_attrs():
        want = PATH_ATTRS[attr](cfg)
        have = getattr(sys.modules[mod_name], attr)
        if have is None or Path(have) != Path(want):
            bad.append(f"{mod_name}.{attr}={have} (pinned {want})")
    return bad


# ── profile pinning (W3) ─────────────────────────────────────────────────────
def resolve_profile(name):
    """Pin `name` for EVERY stage by exporting PTD_PROFILE before any stage import.
    Returns the resolved profile config (name, db_path, digest_dir, reports_dir)."""
    if name:
        slug = profiles.slug(name)
        if not (profiles.PROFILES_DIR / f"{slug}.json").exists():
            print(f"weekly_run: no profile named '{name}'", file=sys.stderr)
            sys.exit(2)
        os.environ["PTD_PROFILE"] = slug
    return profiles.load()  # honors PTD_PROFILE, else _active


# ── summary helpers (W7/W8) ──────────────────────────────────────────────────
def _scalar(db, sql, params=()):
    try:
        c = sqlite3.connect(str(db))
        v = c.execute(sql, params).fetchone()
        c.close()
        return v[0] if v else 0
    except sqlite3.OperationalError:
        return 0


def _has_column(db, table, col):
    try:
        c = sqlite3.connect(str(db))
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        c.close()
        return col in cols
    except sqlite3.OperationalError:
        return False


def _top_picks(db, n=3):
    """The digest's top picks: obtained + analyzed, by quality (discovery) score.
    Includes an insight score only if that column exists (it does not yet)."""
    has_insight = _has_column(db, "videos", "insight_score")
    sel = "video_title, channel_name, quality_score" + (", insight_score" if has_insight else "")
    try:
        c = sqlite3.connect(str(db))
        rows = c.execute(
            f"SELECT {sel} FROM videos "
            "WHERE transcript_status='obtained' "
            "AND id IN (SELECT video_id FROM ai_analysis) "
            "ORDER BY quality_score DESC LIMIT ?", (n,)).fetchall()
        c.close()
    except sqlite3.OperationalError:
        return []
    return rows


def _status_line(before, after):
    keys = sorted(set(before) | set(after))
    return ", ".join(f"{k} {before.get(k,0)}→{after.get(k,0)}" for k in keys) or "(empty)"


def print_summary(cfg, before, after, disc, failed, capped, out, literature=None):
    """The summary contract (W7): stdout ENDS with this compact, chat-ready block.

    `before`/`after` are None when the count query failed; they print as n/a,
    never as a zero that reads like a quiet week."""
    db = cfg["db_path"]
    counts_ok = before is not None and after is not None
    before = before or {}
    after = after or {}
    missing = [k for k in SUMMARY_DISC_KEYS if disc and k not in disc]
    new_disc = int(disc.get("new", 0))
    newly_obtained = (after.get("obtained", 0) - before.get("obtained", 0)) if counts_ok else "n/a"
    analyzed = _scalar(db, "SELECT COUNT(*) FROM ai_analysis")
    errors = after.get("error", 0)
    reupload = int(disc.get("reupload_dropped", 0))
    skipdrop = int(disc.get("skiplist_dropped", 0))
    unproven = _scalar(db, "SELECT COUNT(*) FROM videos "
                           "WHERE COALESCE(caption_availability,'unknown') != 'exists'")

    changed = (new_disc or (counts_ok and newly_obtained) or reupload or skipdrop
               or failed or not counts_ok)

    print("\n" + "═" * 64, file=out)
    print(f"WEEKLY SUMMARY — profile '{cfg['name']}'", file=out)
    print(f"  db: {db}", file=out)
    if not changed:
        print("  No change this week: nothing newly discovered, transcribed, or dropped.",
              file=out)
    status = _status_line(before, after) if counts_ok else "n/a (count query failed)"
    print(f"  status: {status}", file=out)
    print(f"  discovered new: {new_disc}   newly transcribed: {newly_obtained}   "
          f"analyzed (total): {analyzed}", file=out)
    print(f"  errors (retryable): {errors}   capped-out: {capped}", file=out)
    print(f"  excluded — re-upload dups: {reupload}   skip-list: {skipdrop}   "
          f"captions unproven: {unproven}", file=out)
    if literature is not None:
        print(f"  literature: {literature}", file=out)
    picks = _top_picks(db, 3)
    if picks:
        print("  top digest picks:", file=out)
        for row in picks:
            title, chan, score = row[0], row[1], row[2]
            extra = f"   insight {row[3]}" if len(row) > 3 and row[3] is not None else ""
            print(f"    • {(title or '')[:60]} — {chan or ''} — score {score:.2f}{extra}",
                  file=out)
    dd = Path(cfg["digest_dir"]); rd = Path(cfg["reports_dir"])
    print(f"  artifacts: digest {dd/'latest.md'}", file=out)
    print(f"             report {rd/'latest.md'}", file=out)
    if missing:
        print(f"  WARNING: discovery result missing keys {missing} — counts above are unreliable",
              file=out)
    if failed:
        print(f"  FAILED stages: {', '.join(failed)}", file=out)
    print("═" * 64, file=out)


def print_degraded_summary(cfg, failed, err, out):
    """Last-resort summary when the full one cannot be built (QA gate F3): a failed
    week must still be reported as failed, not as a traceback."""
    print("\n" + "═" * 64, file=out)
    print(f"WEEKLY SUMMARY — profile '{cfg['name']}' (DEGRADED)", file=out)
    print(f"  db: {cfg['db_path']}", file=out)
    print(f"  summary unavailable: {type(err).__name__}: {err}", file=out)
    print(f"  FAILED stages: {', '.join(failed) or '(none before summary)'}", file=out)
    print("═" * 64, file=out)


# ── orchestration (W2) ───────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description="Weekly pipeline runner (one writer per DB).")
    ap.add_argument("--profile", help="pin this investigation profile for every stage")
    ap.add_argument("--max-hours", type=float, default=8,
                    help="cap the drain stage's runtime (default 8)")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the stage plan + profile/DB and touch nothing")
    args = ap.parse_args(sys.argv[1:] if argv is None else argv)

    cfg = resolve_profile(args.profile)

    if args.dry_run:  # W9
        print(f"weekly_run DRY RUN — profile '{cfg['name']}'  db={cfg['db_path']}")
        print("stages: " + " → ".join(STAGE_PLAN))
        print(f"max_hours={args.max_hours}; nothing written.")
        return 0

    db = cfg["db_path"]
    ok, holder = dblock.acquire(db, "weekly_run")  # W4: one writer per DB
    if not ok:
        # Exit 0 per W4 (a skipped week is not a failure), but never a silent one:
        # the chat delivery shows an explicit SKIPPED summary (QA gate #2 F5).
        print("\n" + "═" * 64)
        print(f"WEEKLY SUMMARY — profile '{cfg['name']}' — SKIPPED")
        print(f"  already running — another writer holds {db}: {holder}")
        print("  Nothing was run this week.")
        print("═" * 64)
        return 0
    try:
        return _run(cfg, args)
    finally:
        dblock.release(db)


def _run(cfg, args):
    # Deferred imports AFTER pinning (W3) — each resolves its DB/dirs from PTD_PROFILE.
    import dashboard_server
    import podcast_scraper
    import generate_report
    import overnight_pipeline
    import ingest_literature

    bad = pin_mismatches(cfg)
    if bad:
        print("weekly_run: stage modules are not pinned to this profile — refusing to write:",
              file=sys.stderr)
        for b in bad:
            print(f"  {b}", file=sys.stderr)
        return 2

    db = cfg["db_path"]
    failed = []
    disc = {}

    def stage(name, fn):
        try:
            return fn()
        except Exception as e:
            import traceback
            traceback.print_exc()
            failed.append(name)
            print(f"[stage FAILED] {name}: {type(e).__name__}: {e}", flush=True)
            return None

    # migrate must run first so `before` counts see the full schema.
    stage("migrate", lambda: dashboard_server.migrate(db=db))
    before = stage("counts-before", lambda: overnight_pipeline.counts(db))

    stage("reconcile", lambda: dashboard_server.reconcile(include_search=True, db=db))
    disc = stage("discovery", podcast_scraper.main) or {}
    # The literature arm, when the profile enables it (QA gate #2 F6) — the same
    # arm the dashboard's Run Discovery launches. Its outcome is always printed.
    if cfg.get("literature", {}).get("enabled", False):
        n_lit = stage("literature", lambda: ingest_literature.ingest(cfg["name"]))
        literature = f"{n_lit} paper(s) ingested" if n_lit is not None else "FAILED"
    else:
        literature = "not enabled for this profile"
    stage("drain", lambda: overnight_pipeline.drain(max_hours=args.max_hours, db=db,
                                                    failures=failed))
    # Output stages run regardless of earlier failures (W2): a bad discovery must
    # never cost the week's digest.
    stage("digest", overnight_pipeline.write_digest)
    stage("report", lambda: _write_report(generate_report))

    after = stage("counts-after", lambda: overnight_pipeline.counts(db))
    capped = _scalar(db, "SELECT COUNT(*) FROM videos WHERE transcript_status='error' "
                         "AND COALESCE(fetch_attempts,0) >= ?",
                     (overnight_pipeline.MAX_FETCH_ATTEMPTS,))
    try:
        print_summary(cfg, before, after, disc, failed, capped, sys.stdout,
                      literature=literature)
    except Exception as e:
        failed.append("summary")
        print_degraded_summary(cfg, failed, e, sys.stdout)

    if failed:
        return 1  # a failure must never look like a clean run (W2)
    return 0


def _write_report(generate_report):
    md, today = generate_report.build_report(n=REPORT_N)
    d = Path(generate_report.REPORTS_DIR)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"report_{today}.md").write_text(md, encoding="utf-8")
    (d / "latest.md").write_text(md, encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
