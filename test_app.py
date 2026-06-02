#!/usr/bin/env python3
"""Test suite for the podcast tracker pipeline.

Uses temporary DBs/dirs — never touches ~/.hermes. The LLM call is mocked so
tests are offline and free; real LLM connectivity is verified separately.

Run: python3 test_app.py
"""
import os
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import fetch_transcripts
import analyze_transcripts
import generate_digest
import dashboard_server
import podcast_scraper
import profiles


SCHEMA = """
CREATE TABLE videos (id TEXT PRIMARY KEY, channel_name TEXT, video_title TEXT,
    url TEXT, views INTEGER, quality_score REAL, transcript_status TEXT,
    transcribed_date TEXT);
CREATE TABLE transcripts (video_id TEXT PRIMARY KEY, file_path TEXT,
    full_text TEXT, word_count INTEGER);
CREATE TABLE key_points (id INTEGER PRIMARY KEY AUTOINCREMENT, video_id TEXT,
    timestamp_sec INTEGER, point_text TEXT, category TEXT);
CREATE TABLE ai_analysis (video_id TEXT PRIMARY KEY, seo_entities TEXT,
    geo_signals TEXT, best_quote TEXT, analyzed_at TEXT);
"""


def fresh_db(path):
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()


class TestParseVtt(unittest.TestCase):
    def test_dedup_and_timestamps(self):
        vtt = ("WEBVTT\nKind: captions\n\n"
               "00:00:01.000 --> 00:00:03.000\nhello world\n\n"
               "00:00:03.000 --> 00:00:05.000\n<c>hello world</c>\n\n"
               "00:00:05.000 --> 00:00:07.000\nsecond line\n")
        p = tempfile.mktemp(suffix=".vtt")
        Path(p).write_text(vtt)
        text, segs = fetch_transcripts.parse_vtt(p)
        os.remove(p)
        self.assertEqual(text, "hello world second line")  # dup collapsed
        self.assertEqual([s["start"] for s in segs], [1, 5])

    def test_rolling_caption_dedup(self):
        # Mimic YouTube rolling captions: each cue repeats the tail + adds words.
        segs = [
            {"start": 0, "text": "brand entity SEO in"},
            {"start": 2, "text": "brand entity SEO in 2026 for high"},
            {"start": 4, "text": "for high net worth individuals now"},
        ]
        out = fetch_transcripts.dedup_rolling(segs)
        text = " ".join(s["text"] for s in out)
        self.assertEqual(text, "brand entity SEO in 2026 for high net worth individuals now")
        # the late phrase keeps a later timestamp
        self.assertTrue(any(s["start"] == 4 for s in out))

    def test_downsample(self):
        segs = [{"start": s, "text": f"t{s}"} for s in (0, 10, 20, 30, 40)]
        out = analyze_transcripts.downsample_segments(segs, window=25)
        self.assertTrue(out.startswith("[0] "))
        self.assertIn("[30]", out)  # new bucket after 25s


class TestAnalyze(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        fresh_db(self.db)
        conn = sqlite3.connect(self.db)
        long_text = "entity SEO matters for AI overviews and knowledge graphs. " * 20
        conn.execute("INSERT INTO videos VALUES ('vid1','Chan','Title','u',1000,0.9,'obtained','2026-06-01')")
        conn.execute("INSERT INTO transcripts VALUES ('vid1','f',?,?)", (long_text, len(long_text.split())))
        conn.commit(); conn.close()
        analyze_transcripts.DB_PATH = Path(self.db)
        analyze_transcripts.TRANSCRIPTS_DIR = Path(self.tmp)

    def test_analyze_inserts(self):
        # Mock the LLM so the test is offline.
        canned = {
            "key_points": [{"timestamp_sec": 12, "point_text": "Build entity authority", "category": "strategy"}],
            "seo_entities": ["Google", "Perplexity"],
            "geo_signals": ["AI Overviews"],
            "best_quote": "Entities beat keywords.",
        }
        analyze_transcripts.call_llm = lambda *a, **k: canned
        analyze_transcripts.llm_config = lambda: ("fakekey", "http://x", "m")
        done = analyze_transcripts.analyze_all()
        self.assertEqual(done, 1)
        conn = sqlite3.connect(self.db)
        kp = conn.execute("SELECT timestamp_sec, point_text FROM key_points WHERE video_id='vid1'").fetchone()
        ai = conn.execute("SELECT seo_entities, best_quote FROM ai_analysis WHERE video_id='vid1'").fetchone()
        conn.close()
        self.assertEqual(kp, (12, "Build entity authority"))
        self.assertIn("Google", ai[0])
        self.assertEqual(ai[1], "Entities beat keywords.")


class TestDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        fresh_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO videos VALUES ('vid1','Chan','Great Talk','u',5000,0.95,'obtained','2026-06-01')")
        conn.execute("INSERT INTO ai_analysis VALUES ('vid1','[\"Ahrefs\"]','[\"AI Overviews\"]','Quote here.','2026-06-01')")
        conn.execute("INSERT INTO key_points VALUES (1,'vid1',30,'Do entity SEO','strategy')")
        conn.commit(); conn.close()
        generate_digest.DB_PATH = Path(self.db)

    def test_build_digest(self):
        md, day = generate_digest.build_digest(days=None, limit=10)
        self.assertIn("Great Talk", md)
        self.assertIn("Quote here.", md)
        self.assertIn("Do entity SEO", md)
        self.assertIn("Ahrefs", md)


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        fresh_db(self.db)
        conn = sqlite3.connect(self.db)
        # fake obtained (no transcript row) + a real one
        conn.execute("INSERT INTO videos VALUES ('fake','C','T','u',1,0.5,'obtained','2026-06-01')")
        conn.execute("INSERT INTO videos VALUES ('real','C','T','u',1,0.5,'obtained','2026-06-01')")
        conn.execute("INSERT INTO transcripts VALUES ('real','f','text',1)")
        conn.commit(); conn.close()
        Path(self.tmp, "stub.txt").write_text("junk")        # unbacked -> removed
        Path(self.tmp, "real.txt").write_text("real text")   # backed -> kept
        dashboard_server.DB_PATH = self.db
        dashboard_server.TRANSCRIPTS_DIR = self.tmp

    def test_reconcile(self):
        dashboard_server.reconcile()
        conn = sqlite3.connect(self.db)
        fake = conn.execute("SELECT transcript_status FROM videos WHERE id='fake'").fetchone()[0]
        real = conn.execute("SELECT transcript_status FROM videos WHERE id='real'").fetchone()[0]
        conn.close()
        self.assertEqual(fake, "requested")   # downgraded
        self.assertEqual(real, "obtained")    # kept
        self.assertFalse(Path(self.tmp, "stub.txt").exists())
        self.assertTrue(Path(self.tmp, "real.txt").exists())


class TestEmergingDiscovery(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        podcast_scraper.DB_PATH = self.db
        self.conn = podcast_scraper.init_db()

    def _add(self, vid, cid, name, score, via):
        self.conn.execute("""INSERT INTO videos
            (id,channel_name,channel_url,video_title,url,publish_date,duration_seconds,
             views,likes,comments,first_seen_date,last_updated_date,prev_views,view_change,
             view_change_pct,transcript_keywords_score,quality_score,transcript_summary,
             channel_id,is_new_channel,discovered_via,views_per_day)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (vid,name,f"http://yt/{cid}",f"t{vid}","u","2026-05-01",600,5000,100,10,
             "2026-05-02","2026-05-02",0,0,0,0.0,score,"",cid,
             1 if via=="search" else 0, via, 1000))
        self.conn.commit()

    def test_velocity_monotonic(self):
        hi = podcast_scraper.calculate_quality_score(5000,100,10,1800,0.5,"x","Unknown",views_per_day=5000)
        lo = podcast_scraper.calculate_quality_score(5000,100,10,1800,0.5,"x","Unknown",views_per_day=10)
        self.assertGreater(hi, lo)

    def test_suggested_and_autocurate(self):
        self._add("a1","UCnew","Newbie","0.65","search")
        self._add("a2","UCnew","Newbie","0.62","search")   # 2 strong -> suggested
        self._add("b1","UCmon","Monitored","0.70","channel")  # monitored -> auto-curated
        self._add("c1","UClow","Tiny","0.30","search")     # below threshold
        podcast_scraper.sync_channels(self.conn)
        rows = {r[0]: (r[1], r[2]) for r in self.conn.execute(
            "SELECT channel_name, curated, suggested FROM channels")}
        self.assertEqual(rows["Newbie"], (0, 1))     # suggested, not curated
        self.assertEqual(rows["Monitored"][0], 1)    # auto-curated (monitored)
        self.assertEqual(rows["Monitored"][1], 0)    # not suggested
        self.assertEqual(rows["Tiny"], (0, 0))       # neither

    def test_days_since(self):
        self.assertGreaterEqual(podcast_scraper.days_since("2026-05-01"), 1)
        self.assertEqual(podcast_scraper.days_since(None), 1)  # safe default

    def tearDown(self):
        self.conn.close()


class TestProfiles(unittest.TestCase):
    def setUp(self):
        # Redirect all profile storage to a temp area (never touch ~/.hermes).
        self.tmp = Path(tempfile.mkdtemp())
        profiles.PROFILES_DIR = self.tmp / "profiles"
        profiles.DB_DIR = self.tmp / "db"
        profiles.DIGESTS_DIR = self.tmp / "digests"
        profiles.ACTIVE_FILE = profiles.PROFILES_DIR / "_active"
        profiles.LEGACY_DB = self.tmp / "podcast_tracker.db"

    def test_seed_and_active(self):
        names = [p["name"] for p in profiles.list_profiles()]
        self.assertIn("seo-geo", names)
        self.assertEqual(profiles.active_name(), "seo-geo")

    def test_create_switch_isolation(self):
        profiles.create("zone2-training", label="Zone 2",
                        search_queries=["zone 2 training"], analysis_focus="aerobic base")
        # isolation: different DB file than the default
        self.assertNotEqual(profiles.db_path_for("zone2-training"),
                            profiles.db_path_for("seo-geo"))
        # default keeps the legacy DB path
        self.assertEqual(profiles.db_path_for("seo-geo"), str(profiles.LEGACY_DB))
        # switch + load reflects the new profile
        profiles.set_active("zone2-training")
        self.assertEqual(profiles.active_name(), "zone2-training")
        p = profiles.load()
        self.assertEqual(p["analysis_focus"], "aerobic base")
        self.assertIn("zone 2 training", p["search_queries"])

    def test_load_fills_defaults(self):
        profiles.create("bare", label="Bare")
        p = profiles.load("bare")
        self.assertEqual(p["min_views"], profiles.DEFAULTS["min_views"])
        self.assertTrue(p["db_path"].endswith("podcast_bare.db"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
