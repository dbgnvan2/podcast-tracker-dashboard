#!/usr/bin/env python3
"""Weekly runner — one writer per DB, every stage, one honest summary.

Purpose: the repo-native entry point a Hermes cron job (no_agent mode) calls on a
         weekly schedule. It runs the full pipeline against ONE pinned profile's DB
         and prints a compact summary a chat delivery can read verbatim. It replaces
         the stale `~/.hermes/scripts` copy that ran a superseded formula and wrote
         this repo's DB behind its back — the rule now is one writer, many readers.
Spec:    session task W1-W11 (weekly runner).
Tests:   test_app.py::TestWeeklyRun

Stage order (W2): migrate -> reconcile -> discovery -> drain(fetch/analyze/digest)
-> advisor report. Every stage is guarded; a failure is recorded and named but the
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
import subprocess
from pathlib import Path

import profiles  # safe to import early: it resolves the profile lazily, honoring PTD_PROFILE

REPORT_N = 8  # advisor-report window (last N analyzed sources)
STAGE_PLAN = ["migrate", "reconcile", "discovery",
              "drain (fetch+analyze+digest)", "report"]


# ── single-instance lock (W4) ────────────────────────────────────────────────
def _pids(pattern):
    try:
        out = subprocess.run(["pgrep", "-f", pattern],
                             capture_output=True, text=True).stdout.split()
    except FileNotFoundError:
        return []  # no pgrep (non-mac/linux) — best effort, don't block
    return [int(p) for p in out if p]


def _already_running():
    """Refuse to start if another weekly_run.py OR overnight_pipeline.py is live —
    two writers interleave status writes and race the 429 cooldown. Best-effort,
    matching the repo's existing pgrep idiom (it does not parse the target DB)."""
    others = [p for p in (_pids("weekly_run.py") + _pids("overnight_pipeline.py"))
              if p != os.getpid()]
    return bool(others)


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


def print_summary(cfg, before, after, disc, failed, capped, out):
    """The summary contract (W7): stdout ENDS with this compact, chat-ready block."""
    db = cfg["db_path"]
    new_disc = int(disc.get("new", 0))
    newly_obtained = after.get("obtained", 0) - before.get("obtained", 0)
    analyzed = _scalar(db, "SELECT COUNT(*) FROM ai_analysis")
    errors = after.get("error", 0)
    reupload = int(disc.get("reupload_dropped", 0))
    skipdrop = int(disc.get("skiplist_dropped", 0))
    unproven = _scalar(db, "SELECT COUNT(*) FROM videos "
                           "WHERE COALESCE(caption_availability,'unknown') != 'exists'")

    changed = (new_disc or newly_obtained or reupload or skipdrop or failed)

    print("\n" + "═" * 64, file=out)
    print(f"WEEKLY SUMMARY — profile '{cfg['name']}'", file=out)
    print(f"  db: {db}", file=out)
    if not changed:
        print("  No change this week: nothing newly discovered, transcribed, or dropped.",
              file=out)
    print(f"  status: {_status_line(before, after)}", file=out)
    print(f"  discovered new: {new_disc}   newly transcribed: {newly_obtained}   "
          f"analyzed (total): {analyzed}", file=out)
    print(f"  errors (retryable): {errors}   capped-out: {capped}", file=out)
    print(f"  excluded — re-upload dups: {reupload}   skip-list: {skipdrop}   "
          f"captions unproven: {unproven}", file=out)
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
    if failed:
        print(f"  FAILED stages: {', '.join(failed)}", file=out)
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

    if _already_running():  # W4
        print("already running — skipped")
        return 0

    # Deferred imports AFTER pinning (W3) — each resolves its DB/dirs from PTD_PROFILE.
    import dashboard_server
    import podcast_scraper
    import generate_report
    import overnight_pipeline

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
    before = overnight_pipeline.counts(db)

    stage("reconcile", lambda: dashboard_server.reconcile(include_search=True))
    disc = stage("discovery", podcast_scraper.main) or {}
    stage("drain", lambda: overnight_pipeline.drain(max_hours=args.max_hours, db=db))
    # Output stages run regardless of earlier failures (W2): a bad discovery must
    # never cost the week's digest.
    stage("digest", overnight_pipeline.write_digest)
    stage("report", lambda: _write_report(generate_report))

    after = overnight_pipeline.counts(db)
    capped = _scalar(db, "SELECT COUNT(*) FROM videos WHERE transcript_status='error' "
                         "AND COALESCE(fetch_attempts,0) >= ?",
                     (overnight_pipeline.MAX_FETCH_ATTEMPTS,))
    print_summary(cfg, before, after, disc, failed, capped, sys.stdout)

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
