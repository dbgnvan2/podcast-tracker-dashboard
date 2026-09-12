#!/usr/bin/env python3
"""Manual transcript import — phase 4 of DESIGN-transcript-availability.md.

The one case the automated path cannot win: a caption track exists (the side
panel shows a transcript), every yt-dlp client is blocked, and a human can copy
the panel in a few seconds.

This is a LEGITIMATE source — the panel renders the same caption track — but the
panel is *virtualized*: YouTube only materialises the lines near the viewport, so
a careless copy yields a PARTIAL transcript that looks complete. That is exactly
the failure class Working Rule 0 exists to prevent (the prototype presented
truncated browser text as a verified transcript). So a paste is accepted only
when it can be shown to be complete, and it is always labelled with its
provenance. See the Working Rule 0 amendment in CLAUDE.md.

Writes exactly what the fetcher writes, so nothing downstream is special-cased:
  ~/.hermes/transcripts/{id}.txt            plain text, no timestamps
  ~/.hermes/transcripts/{id}.segments.json  [{start, text}] for timestamped analysis
  transcripts row + videos.transcript_status='obtained',
  videos.transcript_source='manual_panel' (or 'manual_panel_partial')

Usage:
  python3 ingest_manual.py --id=VIDEO_ID --file=pasted.txt
  python3 ingest_manual.py --id=VIDEO_ID --file=pasted.txt --allow-partial
  python3 ingest_manual.py --id=VIDEO_ID --file=pasted.txt --replace

Panel copy shape: each line is `0:04` on its own line followed by the text, or
`0:04 text` on one line — both are accepted, because the copy depends on how the
selection lands.

NOTE on normalisation (a deliberate departure from the spec's wording): the spec
suggested reusing fetch_transcripts.dedup_rolling for consistency. That function
collapses YouTube's *rolling* caption cues, where each cue repeats the tail of
the previous one. The panel is ALREADY de-duplicated by YouTube, so applying it
there would delete legitimately repeated words at cue boundaries — a
data-corruption risk, not a consistency win. We re-use the same light
normalisation instead (the _clean_text sibling in fetch_transcripts) and leave
rolling-dedup to the path that actually receives rolling cues.
"""
import os
import re
import sys
import json
import sqlite3
from pathlib import Path
from datetime import datetime

import profiles

DB_PATH = Path(profiles.load()["db_path"])      # resolved per call site, never cached
TRANSCRIPTS_DIR = profiles.HERMES / "transcripts"

# "0:04" or "1:02:33", optionally followed by the text on the same line.
TS_RE = re.compile(r"^\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*(.*)$")

# Zero-width chars YouTube sprinkles into panel copies.
ZERO_WIDTH = dict.fromkeys(map(ord, "\u200b\u200c\u200d\ufeff\u2060"), None)

COMPLETE_RATIO = 0.95   # last timestamp must reach 95% of the video's duration


def clean(s):
    s = (s or "").translate(ZERO_WIDTH)
    return re.sub(r"\s+", " ", s).strip()


def ts_to_seconds(a, b, c):
    """Panel cue timestamps are MM:SS, or H:MM:SS once a video passes an hour.

    The regex yields (a, b, None) for MM:SS and (a, b, c) for H:MM:SS. Reading
    `a` as hours unconditionally turns 0:04 into 240s — caught by
    TestManualImport, which is why both shapes are asserted."""
    if c is None:
        return int(a) * 60 + int(b)
    return int(a) * 3600 + int(b) * 60 + int(c)


def parse_panel(text):
    """Pasted panel text -> [{start, text}], sorted by start.

    Accepts both copy shapes. Lines before the first timestamp are ignored (the
    panel header) rather than guessed at."""
    segments = []
    pending = None
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        m = TS_RE.match(line)
        if m:
            pending = ts_to_seconds(m.group(1), m.group(2), m.group(3))
            rest = clean(m.group(4))
            if rest:
                segments.append({"start": pending, "text": rest})
                pending = None
            continue
        if pending is None:
            continue        # no timestamp seen yet -> header/chrome, skip it
        segments.append({"start": pending, "text": clean(line)})
        pending = None
    return [s for s in segments if s["text"]]


def full_text_of(segments):
    return " ".join(s["text"] for s in segments).strip()


def load_target(conn, vid):
    """The row plus its availability, degrading on a not-yet-migrated DB.

    Availability is fetched in its own query so a missing column can't poison
    the main read (a mixed-version DB must never crash an import)."""
    conn.row_factory = sqlite3.Row
    row = conn.execute(
        "SELECT id, video_title, channel_name, duration_seconds, transcript_status "
        "FROM videos WHERE id = ?", (vid,)).fetchone()
    if row is None:
        return None
    avail = "unknown"
    try:
        r = conn.execute(
            "SELECT caption_availability FROM videos WHERE id = ?", (vid,)).fetchone()
        if r and r[0]:
            avail = r[0]
    except sqlite3.OperationalError:
        pass
    out = dict(row)
    out["caption_availability"] = avail
    return out


def has_transcript(conn, vid):
    return bool(conn.execute(
        "SELECT 1 FROM transcripts WHERE video_id = ?", (vid,)).fetchone())


def main():
    vid = None
    src = None
    replace = False
    allow_partial = False
    for a in sys.argv[1:]:
        if a.startswith("--id="):
            vid = a.split("=", 1)[1]
        elif a.startswith("--file="):
            src = a.split("=", 1)[1]
        elif a == "--replace":
            replace = True
        elif a == "--allow-partial":
            allow_partial = True

    if not vid or not src:
        print("Usage: python3 ingest_manual.py --id=VIDEO_ID --file=pasted.txt "
              "[--replace] [--allow-partial]")
        return 2

    src_path = Path(os.path.expanduser(src))
    if not src_path.is_file():
        print("Error: no such file: %s" % src_path)
        return 2
    paste = src_path.read_text(errors="replace")

    conn = sqlite3.connect(str(DB_PATH))
    row = load_target(conn, vid)
    if row is None:
        print("Error: %s is not in the DB. Discovery must see a video before it can be imported." % vid)
        conn.close()
        return 2

    # Gate 1: provenance honesty. Importing by hand is only meaningful when the
    # automated path is *supposed* to work but is blocked. If a probe has proven
    # there is no caption track, this paste cannot be a real transcript of it.
    if row["caption_availability"] != "exists":
        print("Refusing: caption_availability=%s for %s." % (row["caption_availability"], vid))
        print("  A paste is only accepted when a caption track is known to exist —")
        print("  otherwise we would be inventing a transcript for a video that has none.")
        print("  'exists' is set by a discovery run that enriches this video")
        print("  (python3 podcast_scraper.py); there is no standalone probe command.")
        conn.close()
        return 3

    # Gate 2: never destroy an existing transcript (Hard Constraint 4).
    if has_transcript(conn, vid) and not replace:
        print("Refusing: a transcript already exists for %s. Use --replace to overwrite." % vid)
        conn.close()
        return 3

    segments = parse_panel(paste)
    if not segments:
        print("Refusing: no `MM:SS text` lines found. Did the panel actually get copied?")
        conn.close()
        return 3
    segments.sort(key=lambda s: s["start"])
    text = full_text_of(segments)

    # Gate 3: the completeness check. The panel is virtualized, so a partial copy
    # looks exactly like a complete one. Compare the last timestamp against the
    # video's duration — a mechanical check, not a judgement call.
    duration = row["duration_seconds"] or 0
    last = segments[-1]["start"] if segments else 0
    partial = False
    if duration <= 0:
        partial = True
        reason = "no duration_seconds on the row, so completeness cannot be verified"
    else:
        ratio = last / float(duration)
        if ratio < COMPLETE_RATIO:
            partial = True
            reason = ("last timestamp %s is only %.0f%% of the %s duration"
                      % (fmt(last), ratio * 100, fmt(duration)))
        else:
            reason = "last timestamp %s reaches %.0f%% of the %s duration" % (
                fmt(last), ratio * 100, fmt(duration))

    print("Parsed %s: %d segments, %d words" % (vid, len(segments), len(text.split())))
    print("  first %s -> last %s   (%s)" % (fmt(segments[0]["start"]), fmt(last), reason))

    if partial and not allow_partial:
        print("Refusing: the paste looks PARTIAL. Scroll the panel to the very bottom and copy")
        print("  again, or re-run with --allow-partial to store it as 'manual_panel_partial'.")
        conn.close()
        return 4

    source = "manual_panel_partial" if partial else "manual_panel"

    TRANSCRIPTS_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = TRANSCRIPTS_DIR / ("%s.txt" % vid)
    txt_path.write_text(text, encoding="utf-8")
    (TRANSCRIPTS_DIR / ("%s.segments.json" % vid)).write_text(
        json.dumps(segments), encoding="utf-8")

    conn.execute(
        "INSERT OR REPLACE INTO transcripts (video_id, file_path, full_text, word_count) "
        "VALUES (?,?,?,?)", (vid, str(txt_path), text, len(text.split())))
    conn.execute(
        "UPDATE videos SET transcript_status='obtained', transcribed_date=? WHERE id=?",
        (datetime.now().strftime("%Y-%m-%d"), vid))
    try:
        conn.execute("UPDATE videos SET transcript_source=? WHERE id=?", (source, vid))
    except sqlite3.OperationalError:
        pass  # un-migrated DB; the transcript itself is stored regardless
    conn.commit()
    conn.close()

    print("Wrote %s and %s.segments.json" % (txt_path.name, vid))
    print("  transcript_source=%s, status=obtained. Run analyze_transcripts.py next." % source)
    return 0


def fmt(sec):
    sec = int(sec or 0)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return ("%d:%02d:%02d" % (h, m, s)) if h else ("%d:%02d" % (m, s))


if __name__ == "__main__":
    sys.exit(main())
