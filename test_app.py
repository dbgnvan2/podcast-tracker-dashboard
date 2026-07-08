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
    transcribed_date TEXT, discovered_via TEXT DEFAULT 'search', source_type TEXT DEFAULT 'youtube',
    dismissed INTEGER DEFAULT 0, digested_at TEXT);
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
        conn.execute("INSERT INTO videos VALUES ('vid1','Chan','Title','u',1000,0.9,'obtained','2026-06-01','search','youtube',0,NULL)")
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
        orig_call_llm = analyze_transcripts.call_llm
        orig_llm_config = analyze_transcripts.llm_config
        analyze_transcripts.call_llm = lambda *a, **k: canned
        analyze_transcripts.llm_config = lambda: ("fakekey", "http://x", "m")
        try:
            done = analyze_transcripts.analyze_all()
        finally:
            analyze_transcripts.call_llm = orig_call_llm
            analyze_transcripts.llm_config = orig_llm_config
        self.assertEqual(done, 1)
        conn = sqlite3.connect(self.db)
        kp = conn.execute("SELECT timestamp_sec, point_text FROM key_points WHERE video_id='vid1'").fetchone()
        ai = conn.execute("SELECT seo_entities, best_quote FROM ai_analysis WHERE video_id='vid1'").fetchone()
        conn.close()
        self.assertEqual(kp, (12, "Build entity authority"))
        self.assertIn("Google", ai[0])
        self.assertEqual(ai[1], "Entities beat keywords.")

    def test_load_transcript_truncation_is_visible(self):
        """P9 regression: a transcript longer than the budget is truncated, and
        load_transcript reports the true full length so the caller can warn.
        (Real-scale fixture: the cap MUST bite — toy data hid the original bug.)"""
        orig_max = analyze_transcripts.MAX_TRANSCRIPT_CHARS
        analyze_transcripts.MAX_TRANSCRIPT_CHARS = 500
        try:
            big = "x" * 50_000  # ~ a long-form transcript
            text, full_len = analyze_transcripts.load_transcript("no_such_vid", big)
            self.assertEqual(len(text), 500)      # capped
            self.assertEqual(full_len, 50_000)    # true length reported, not the cap
            # And with the real default the same transcript goes through whole.
            analyze_transcripts.MAX_TRANSCRIPT_CHARS = 150_000
            text2, full2 = analyze_transcripts.load_transcript("no_such_vid", big)
            self.assertEqual(len(text2), 50_000)  # full transcript sent
        finally:
            analyze_transcripts.MAX_TRANSCRIPT_CHARS = orig_max

    def test_empty_analysis_writes_no_row(self):
        """P6 regression: an empty LLM result must NOT create an ai_analysis row,
        or the video is marked 'analyzed' and never retried."""
        orig_call = analyze_transcripts.call_llm
        orig_cfg = analyze_transcripts.llm_config
        analyze_transcripts.call_llm = lambda *a, **k: {
            "key_points": [], "seo_entities": [], "geo_signals": [], "best_quote": ""}
        analyze_transcripts.llm_config = lambda: ("fakekey", "http://x", "m")
        try:
            done = analyze_transcripts.analyze_all()
        finally:
            analyze_transcripts.call_llm = orig_call
            analyze_transcripts.llm_config = orig_cfg
        self.assertEqual(done, 0)  # nothing counted as analyzed
        conn = sqlite3.connect(self.db)
        n = conn.execute("SELECT COUNT(*) FROM ai_analysis WHERE video_id='vid1'").fetchone()[0]
        conn.close()
        self.assertEqual(n, 0)  # no row written → next run retries it


class TestDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        fresh_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO videos VALUES ('vid1','Chan','Great Talk','u',5000,0.95,'obtained','2026-06-01','search','youtube',0,NULL)")
        conn.execute("INSERT INTO ai_analysis VALUES ('vid1','[\"Ahrefs\"]','[\"AI Overviews\"]','Quote here.','2026-06-01')")
        conn.execute("INSERT INTO key_points VALUES (1,'vid1',30,'Do entity SEO','strategy')")
        conn.commit(); conn.close()
        generate_digest.DB_PATH = Path(self.db)

    def test_build_digest(self):
        md, day, ids = generate_digest.build_digest(limit=10)
        self.assertIn("Great Talk", md)
        self.assertIn("Quote here.", md)
        self.assertIn("Do entity SEO", md)
        self.assertIn("Ahrefs", md)


class TestDigestBehavior(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.db = str(self.tmp / "t.db")
        fresh_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO videos VALUES ('v1','Chan','Title One','u',1000,0.9,'obtained','2026-06-01','search','youtube',0,NULL)")
        conn.execute("INSERT INTO videos VALUES ('v2','Chan','Title Two','u',800,0.8,'obtained','2026-06-02','search','youtube',0,NULL)")
        conn.execute("INSERT INTO ai_analysis (video_id,seo_entities,geo_signals,best_quote,analyzed_at) VALUES ('v1','[]','[]','quote one','2026-06-01')")
        conn.execute("INSERT INTO ai_analysis (video_id,seo_entities,geo_signals,best_quote,analyzed_at) VALUES ('v2','[]','[]','quote two','2026-06-02')")
        conn.commit(); conn.close()
        import generate_digest
        self._orig_db = generate_digest.DB_PATH
        self._orig_dir = generate_digest.DIGEST_DIR
        generate_digest.DB_PATH = Path(self.db)
        generate_digest.DIGEST_DIR = self.tmp / "digests"
        generate_digest.DIGEST_DIR.mkdir()

    def tearDown(self):
        import generate_digest
        generate_digest.DB_PATH = self._orig_db
        generate_digest.DIGEST_DIR = self._orig_dir

    def test_digest_includes_undigested(self):
        import generate_digest
        md, today, ids = generate_digest.build_digest()
        self.assertIn('v1', ids)
        self.assertIn('v2', ids)

    def test_build_digest_does_NOT_mark(self):
        """Safety property: build_digest must not mark videos digested — marking
        before the file is durably written risks losing them forever (P6/P8)."""
        import generate_digest
        generate_digest.build_digest()  # build only, no mark_digested
        conn = sqlite3.connect(self.db)
        rows = {r[0]: r[1] for r in conn.execute("SELECT id, digested_at FROM videos").fetchall()}
        conn.close()
        self.assertIsNone(rows['v1'])
        self.assertIsNone(rows['v2'])

    def test_mark_digested_after_build(self):
        """The intended flow: build, (write file), then mark_digested marks them."""
        import generate_digest
        md, today, ids = generate_digest.build_digest()
        generate_digest.mark_digested(ids, today)
        conn = sqlite3.connect(self.db)
        rows = {r[0]: r[1] for r in conn.execute("SELECT id, digested_at FROM videos").fetchall()}
        conn.close()
        self.assertIsNotNone(rows['v1'])
        self.assertIsNotNone(rows['v2'])

    def test_second_digest_skips_already_digested(self):
        import generate_digest
        _, day, ids = generate_digest.build_digest()
        generate_digest.mark_digested(ids, day)
        md, today, ids2 = generate_digest.build_digest()
        self.assertEqual(ids2, [])
        self.assertIn("No new analyzed videos", md)

    def test_digest_force_includes_already_digested(self):
        import generate_digest
        _, day, ids = generate_digest.build_digest()
        generate_digest.mark_digested(ids, day)
        md, today, ids2 = generate_digest.build_digest(force=True)
        self.assertIn('v1', ids2)
        self.assertIn('v2', ids2)


class TestReconcile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        fresh_db(self.db)
        conn = sqlite3.connect(self.db)
        # fake obtained (no transcript row) + a real one
        conn.execute("INSERT INTO videos VALUES ('fake','C','T','u',1,0.5,'obtained','2026-06-01','search','youtube',0,NULL)")
        conn.execute("INSERT INTO videos VALUES ('real','C','T','u',1,0.5,'obtained','2026-06-01','search','youtube',0,NULL)")
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

    def test_authority_exact_match_not_substring(self):
        """An impostor channel named to CONTAIN a known expert's handle must not
        inherit that expert's authority weight (LEARNINGS P7 — was substring)."""
        orig = podcast_scraper.CHANNEL_BONUS
        podcast_scraper.CHANNEL_BONUS = {"Neil Patel": 2.0, "UC_REAL_ID": 1.8}
        try:
            # Exact normalized name still matches.
            self.assertGreater(
                podcast_scraper.channel_authority("UCx", "Neil Patel", False), 0.0)
            # Impostor that merely contains the name does NOT.
            self.assertEqual(
                podcast_scraper.channel_authority("UCy", "Neil Patel Daily SEO", False), 0.0)
            self.assertEqual(
                podcast_scraper.channel_authority("UCz", "Fake Neil Patel", False), 0.0)
            # A bonus key written as a channel id matches the id exactly.
            self.assertGreater(
                podcast_scraper.channel_authority("UC_REAL_ID", "Whatever Name", False), 0.0)
        finally:
            podcast_scraper.CHANNEL_BONUS = orig

    def test_timeout_is_retryable(self):
        """A yt-dlp timeout must be classified transient (retryable 'error'),
        not terminal 'not_available' (LEARNINGS P1)."""
        import fetch_transcripts
        self.assertIn("timeout", fetch_transcripts.BLOCKED_MARKERS)

    def test_authority_beats_keyword_stuffing(self):
        # A monitored expert with a plain (low-keyword) title must outrank a
        # non-curated channel with a keyword-stuffed title and even more views.
        expert = podcast_scraper.calculate_quality_score(
            14000, 300, 30, 900, 0.0, "UCx", "Neil Patel", views_per_day=200, is_curated=True)
        stuffer = podcast_scraper.calculate_quality_score(
            20000, 300, 30, 900, 1.0, "UCy", "Random SEO Guy", views_per_day=50, is_curated=False)
        self.assertGreater(expert, stuffer)
        # curated authority floor > non-curated for the same channel
        self.assertGreater(
            podcast_scraper.channel_authority("UCx", "Neil Patel", True),
            podcast_scraper.channel_authority("UCx", "Neil Patel", False))

    def test_suggested_and_autocurate(self):
        # Rule: a non-curated channel needs >=5 videos scoring over 0.5 to be suggested.
        for i in range(5):
            self._add(f"a{i}", "UCnew", "Newbie", "0.55", "search")  # 5 strong -> suggested
        self._add("b1","UCmon","Monitored","0.70","channel")        # monitored -> auto-curated
        self._add("c1","UCfew","FewGood","0.65","search")           # only 1 good -> not enough
        self._add("c2","UCfew","FewGood","0.62","search")
        for i in range(6):
            self._add(f"d{i}","UClow","LowScores","0.40","search")  # many but all <=0.5 -> no
        podcast_scraper.sync_channels(self.conn)
        rows = {r[0]: (r[1], r[2]) for r in self.conn.execute(
            "SELECT channel_name, curated, suggested FROM channels")}
        self.assertEqual(rows["Newbie"], (0, 1))     # 5 videos >0.5 -> suggested
        self.assertEqual(rows["Monitored"][0], 1)    # auto-curated (monitored)
        self.assertEqual(rows["Monitored"][1], 0)    # not suggested
        self.assertEqual(rows["FewGood"][1], 0)      # only 2 videos -> not suggested
        self.assertEqual(rows["LowScores"][1], 0)    # many videos but none >0.5

    def test_days_since(self):
        self.assertGreaterEqual(podcast_scraper.days_since("2026-05-01"), 1)
        self.assertEqual(podcast_scraper.days_since(None), 1)  # safe default

    def tearDown(self):
        self.conn.close()


class TestReport(unittest.TestCase):
    def setUp(self):
        import generate_report
        self.gr = generate_report
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        fresh_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO videos VALUES ('vidA','ChanA','Talk A','https://youtube.com/watch?v=vidA',9,0.9,'obtained','2026-06-02','search','youtube',0,NULL)")
        conn.execute("INSERT INTO videos VALUES ('vidB','ChanB','Talk B','https://youtube.com/watch?v=vidB',9,0.8,'obtained','2026-06-01','search','youtube',0,NULL)")
        conn.execute("INSERT INTO transcripts VALUES ('vidA','f','full text about entity SEO and AI overviews here.',8)")
        conn.execute("INSERT INTO transcripts VALUES ('vidB','f','full text about knowledge graphs and citations here.',8)")
        conn.execute("INSERT INTO ai_analysis VALUES ('vidA','[]','[]','Quote A.','2026-06-02')")
        conn.execute("INSERT INTO ai_analysis VALUES ('vidB','[]','[]','Quote B.','2026-06-01')")
        conn.execute("INSERT INTO key_points VALUES (1,'vidA',30,'Point A','insight')")
        conn.execute("INSERT INTO key_points VALUES (2,'vidB',60,'Point B','strategy')")
        conn.commit(); conn.close()
        self.gr.DB_PATH = Path(self.db)

    def test_report_date_from_filter(self):
        """date_from='2026-06-02' should include only vidA (transcribed 2026-06-02), not vidB."""
        sources = self.gr.select_sources(8, date_from='2026-06-02')
        ids = [s["id"] for s in sources]
        self.assertIn('vidA', ids)
        self.assertNotIn('vidB', ids)

    def test_report_date_to_filter(self):
        """date_to='2026-06-01' should include only vidB (transcribed 2026-06-01), not vidA."""
        sources = self.gr.select_sources(8, date_to='2026-06-01')
        ids = [s["id"] for s in sources]
        self.assertIn('vidB', ids)
        self.assertNotIn('vidA', ids)

    def test_report_channel_filter(self):
        """channel='ChanA' filter should include only vidA."""
        sources = self.gr.select_sources(8, channel='ChanA')
        ids = [s["id"] for s in sources]
        self.assertIn('vidA', ids)
        self.assertNotIn('vidB', ids)

    def test_allocate_excerpts_budget(self):
        """Fair allocation: under budget = full text; over budget = capped to budget,
        with short transcripts kept whole and the leftover flowing to long ones."""
        # Under budget — everything goes in whole.
        texts = ["a" * 100, "b" * 200, "c" * 300]
        out = self.gr.allocate_excerpts(texts, 10_000)
        self.assertEqual([len(x) for x in out], [100, 200, 300])
        # Over budget — total sent never exceeds the budget.
        out2 = self.gr.allocate_excerpts(texts, 360)
        self.assertLessEqual(sum(len(x) for x in out2), 360)
        # The shortest transcript is still sent in full (water-filling).
        self.assertEqual(len(out2[0]), 100)
        # Empty input is safe.
        self.assertEqual(self.gr.allocate_excerpts([], 100), [])

    def test_build_prompt_sends_full_transcript(self):
        """build_prompt must include the actual transcript text, not a 2800-char stub."""
        big = "Entity SEO insight number %d. " % 0 + ("detail " * 2000)  # ~14k chars
        src = [{"n": 1, "id": "v1", "title": "T", "channel": "C", "url": "",
                "is_youtube": True, "best_quote": "Q", "key_points": [],
                "full_text": big, "first_ts": 0}]
        prompt = self.gr.build_prompt(src)
        # The whole transcript is present (well beyond the old 2800-char cap).
        self.assertIn(big, prompt)
        self.assertIn("full transcript", prompt)

    def test_report_multi_channel_filter(self):
        """A list of channels matches any of them exactly (multi-select picklist)."""
        both = [s["id"] for s in self.gr.select_sources(8, channel=['ChanA', 'ChanB'])]
        self.assertIn('vidA', both)
        self.assertIn('vidB', both)
        # Single-element list still exact-matches just that channel.
        just_b = [s["id"] for s in self.gr.select_sources(8, channel=['ChanB'])]
        self.assertEqual(just_b, ['vidB'])
        # Exact match: a partial name in the list matches nothing.
        none = self.gr.select_sources(8, channel=['Chan'])
        self.assertEqual(none, [])

    def test_report_cites_real_sources(self):
        # Mock the LLM with the rich two-layer shape; idea 2 cites invalid src 9.
        orig_call_llm = self.gr.A.call_llm
        orig_llm_config = self.gr.A.llm_config
        self.gr.A.call_llm = lambda *a, **k: {
            "overview": "Overview text.",
            "key_ideas": [
                {"title": "Idea One", "summary": "Short summary one.",
                 "why_it_matters": "It matters because reasons.",
                 "how_to_implement": ["Do step A", "Do step B"],
                 "details": "Deeper explanation one.", "sources": [1]},
                {"title": "Idea Two", "summary": "Short summary two.",
                 "why_it_matters": "Second significance.",
                 "how_to_implement": ["Do step C"],
                 "details": "Deeper explanation two.", "sources": [2, 9]},
            ],
        }
        self.gr.A.llm_config = lambda: ("k", "http://x", "m")
        try:
            md, day = self.gr.build_report(n=8)
        finally:
            self.gr.A.call_llm = orig_call_llm
            self.gr.A.llm_config = orig_llm_config
        # two-layer structure present
        self.assertIn("## Executive Key Ideas", md)
        self.assertIn("## Detailed Analysis", md)
        self.assertIn("**Idea One**", md)            # summary list
        self.assertIn("### 1. Idea One", md)         # detailed section
        self.assertIn("**Why it matters.**", md)
        self.assertIn("**How to implement:**", md)
        self.assertIn("- Do step A", md)
        # citations deep-link to the real source video + its key-point timestamp
        self.assertIn("watch?v=vidA&t=30s", md)
        self.assertIn("watch?v=vidB&t=60s", md)
        # invalid source 9 was dropped, not rendered
        self.assertNotIn("[S9]", md)
        self.assertIn("**S1**", md)
        self.assertIn("**S2**", md)


class TestMultiSource(unittest.TestCase):
    def test_scholarly_dedup_and_authority(self):
        from sources import scholarly
        recs = [
            {"title": "Paper A", "abstractText": "<i>abstract</i> A about lactate",
             "doi": "10.1/AAA", "citedByCount": 5, "firstPublicationDate": "2025-03-01",
             "authorString": "X et al", "source": "MED", "id": "1",
             "journalInfo": {"journal": {"title": "J Sport"}}},
            {"title": "Paper A (preprint)", "abstractText": "abstract A", "doi": "10.1/aaa",
             "citedByCount": 0, "firstPublicationDate": "2025-01-01", "source": "PPR", "id": "2"},
            {"title": "Paper B", "abstractText": "abstract B", "doi": "10.2/BBB",
             "citedByCount": 0, "firstPublicationDate": "2025-05-01", "source": "MED", "id": "3"},
        ]
        scholarly._europepmc_search = lambda q, per=25, retries=3: recs
        docs = scholarly.ScholarlyAdapter().discover({"queries": ["lactate"], "since_date": "2024-01-01"})
        # DOI dedup (case-insensitive) collapses the two A records → 2 unique docs
        self.assertEqual(len({d["id"] for d in docs}), 2)
        a = [d for d in docs if d["id"] == "10.1/aaa"][0]
        self.assertGreater(a["authority"], 0)          # 5 citations → some authority
        self.assertEqual(a["raw"]["citations"], 5)     # kept the higher-cited record
        self.assertNotIn("<i>", a["text"])             # html stripped

    def test_ingest_stores_papers_as_obtained(self):
        import ingest_literature as il
        import profiles as P
        from sources import scholarly
        from sources.base import make_document
        tmp = tempfile.mkdtemp(); db = os.path.join(tmp, "t.db")
        dashboard_server.migrate(db)  # full schema for a fresh profile DB
        orig_load, orig_disc = P.load, scholarly.ScholarlyAdapter.discover
        try:
            P.load = lambda name=None: {
                "name": "t", "label": "T", "db_path": db, "keywords": ["lactate"],
                "literature": {"enabled": True, "queries": ["x"], "max_results_per_source": 5}}
            scholarly.ScholarlyAdapter.discover = lambda self, arm: [make_document(
                "10.1/x", "literature", "europepmc", "Paper X about lactate", "Auth et al",
                "https://doi.org/10.1/x", "2025-06-01", "abstract discussing lactate threshold",
                authority=0.3, raw={"doi": "10.1/x", "citations": 4, "venue": "J Sport"})]
            n = il.ingest("t")
            self.assertEqual(n, 1)
            conn = sqlite3.connect(db)
            row = conn.execute("SELECT source_type, transcript_status, quality_score, url, citations "
                               "FROM videos WHERE id='10.1/x'").fetchone()
            tr = conn.execute("SELECT full_text FROM transcripts WHERE video_id='10.1/x'").fetchone()
            conn.close()
            self.assertEqual(row[0], "literature")
            self.assertEqual(row[1], "obtained")        # papers need no transcription
            self.assertGreater(row[2], 0)               # scored
            self.assertEqual(row[3], "https://doi.org/10.1/x")
            self.assertEqual(row[4], 4)
            self.assertIn("lactate", tr[0])             # abstract stored as content
        finally:
            P.load, scholarly.ScholarlyAdapter.discover = orig_load, orig_disc

    def test_reingest_refreshes_metadata(self):
        """P3 regression: re-ingesting a paper whose title/venue changed upstream
        must refresh the descriptive fields, not just the metrics."""
        import ingest_literature as il
        import profiles as P
        from sources import scholarly
        from sources.base import make_document
        tmp = tempfile.mkdtemp(); db = os.path.join(tmp, "t.db")
        dashboard_server.migrate(db)
        orig_load, orig_disc = P.load, scholarly.ScholarlyAdapter.discover
        try:
            P.load = lambda name=None: {
                "name": "t", "label": "T", "db_path": db, "keywords": ["lactate"],
                "literature": {"enabled": True, "queries": ["x"]}}
            scholarly.ScholarlyAdapter.discover = lambda self, arm: [make_document(
                "10.1/x", "literature", "europepmc", "Old Title", "Auth et al",
                "https://doi.org/10.1/x", "2025-06-01", "abstract about lactate",
                authority=0.3, raw={"doi": "10.1/x", "citations": 1, "venue": "Old Venue"})]
            il.ingest("t")
            # Upstream correction: title + venue change.
            scholarly.ScholarlyAdapter.discover = lambda self, arm: [make_document(
                "10.1/x", "literature", "europepmc", "Corrected Title", "Auth et al",
                "https://doi.org/10.1/x", "2025-06-01", "abstract about lactate",
                authority=0.5, raw={"doi": "10.1/x", "citations": 9, "venue": "New Venue"})]
            il.ingest("t")
            conn = sqlite3.connect(db)
            row = conn.execute("SELECT video_title, venue, citations FROM videos WHERE id='10.1/x'").fetchone()
            conn.close()
            self.assertEqual(row[0], "Corrected Title")  # title refreshed (was the bug)
            self.assertEqual(row[1], "New Venue")        # venue refreshed
            self.assertEqual(row[2], 9)                  # metric refreshed
        finally:
            P.load, scholarly.ScholarlyAdapter.discover = orig_load, orig_disc

    def test_briefing_citation_validation(self):
        import spike_multisource as sp
        sources = [{"n": 1, "id": "10.1/A", "title": "A", "byline": "X", "url": "https://doi.org/10.1/A",
                    "date": "2025-01-01", "kind": "literature", "text": "t", "meta": "5 citations"},
                   {"n": 2, "id": "vidX", "title": "Vid", "byline": "Chan", "url": "https://youtube.com/watch?v=vidX",
                    "date": "2026-01-01", "kind": "youtube", "text": "t", "meta": "Chan"}]
        report = {"overview": "ov", "key_ideas": [
            {"title": "Idea", "summary": "s", "why_it_matters": "w", "how_to_apply": ["do x"],
             "details": "d", "sources": [1, 2, 9]}]}  # 9 is invalid
        md = sp.render(report, sources, "topic", "2026-06-03")
        self.assertIn("[S1](https://doi.org/10.1/A)", md)              # paper citation
        self.assertIn("[S2](https://youtube.com/watch?v=vidX)", md)   # video citation (cross-source)
        self.assertNotIn("[S9]", md)                                  # invalid dropped


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

    def test_update_settings(self):
        profiles.create("vid", label="Vid")
        profiles.update("vid", {"min_views": 2000, "min_publish_date": "2024-06-01"})
        p = profiles.load("vid")
        self.assertEqual(p["min_views"], 2000)
        self.assertEqual(p["min_publish_date"], "2024-06-01")
        # blanks/None are ignored, existing values preserved
        profiles.update("vid", {"min_views": None, "max_videos_per_channel": ""})
        self.assertEqual(profiles.load("vid")["min_views"], 2000)


class TestHTMLJavaScript(unittest.TestCase):
    """Catch Python-string-escape bugs that break the embedded JS at runtime."""

    def _extract_js(self):
        html = dashboard_server.HTML
        start = html.find("<script>")
        end = html.find("</script>", start)
        self.assertGreater(start, 0, "No <script> block found in HTML")
        return html[start + 8:end]

    def test_js_parses_with_node(self):
        import subprocess
        js = self._extract_js()
        result = subprocess.run(
            ["node", "--check", "--input-type=commonjs"],
            input=js.encode(), capture_output=True, timeout=10
        )
        self.assertEqual(result.returncode, 0,
            f"JS syntax error:\n{result.stderr.decode()[:800]}")

    def test_no_python_escape_in_js_joins(self):
        """Detect the Python-string-escape-in-JS bug: join('\\n') where the \\n
        is a real newline (from Python's string escaping) rather than the two
        characters backslash-n. node --check catches this too, but this test
        gives a more precise error message pointing at the exact pattern."""
        import re
        js = self._extract_js()
        # A real newline inside a join() call string argument — the bug we hit
        bad = re.findall(r"join\('[^']*\n[^']*'\)", js)
        self.assertEqual(bad, [],
            f"Raw newline in .join() string arg (Python escape leak): {bad}")

    def test_all_api_routes_referenced_in_html(self):
        """Key API calls in the JS should have matching server routes."""
        js = self._extract_js()
        for route in ("/api/candidates", "/api/profiles", "/api/llm_providers",
                      "/api/run_discovery", "/api/job_log", "/api/llm_selection"):
            self.assertIn(route, js, f"Route {route} missing from JS")


class TestLLMProviders(unittest.TestCase):
    """Provider CRUD: add, edit, delete, key storage, selection persistence."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self._orig_providers = dashboard_server.PROVIDERS_FILE
        self._orig_env = dashboard_server.ENV_FILE
        dashboard_server.PROVIDERS_FILE = self.tmp / "llm_providers.json"
        dashboard_server.ENV_FILE = self.tmp / ".env"

    def tearDown(self):
        dashboard_server.PROVIDERS_FILE = self._orig_providers
        dashboard_server.ENV_FILE = self._orig_env

    def test_empty_returns_defaults(self):
        d = dashboard_server._load_providers()
        self.assertEqual(d["providers"], [])
        self.assertEqual(d["bulk"], {})

    def test_add_provider_and_key(self):
        pid = "p_test"
        data = dashboard_server._load_providers()
        data["providers"].append({"id": pid, "label": "Test", "base": "https://api.test.com/v1"})
        dashboard_server._save_providers(data)
        dashboard_server._write_env_file({f"LLM_KEY_{pid}": "sk-test123"})

        loaded = dashboard_server._load_providers()
        self.assertEqual(len(loaded["providers"]), 1)
        self.assertEqual(loaded["providers"][0]["label"], "Test")
        self.assertEqual(dashboard_server._provider_key(pid), "sk-test123")

    def test_edit_provider(self):
        data = {"providers": [{"id": "p1", "label": "Old", "base": "https://a.com/v1"}],
                "bulk": {}, "synth": {}}
        dashboard_server._save_providers(data)
        data["providers"][0]["label"] = "New"
        data["providers"][0]["base"] = "https://b.com/v1"
        dashboard_server._save_providers(data)
        loaded = dashboard_server._load_providers()
        self.assertEqual(loaded["providers"][0]["label"], "New")
        self.assertEqual(loaded["providers"][0]["base"], "https://b.com/v1")

    def test_delete_clears_selection(self):
        data = {
            "providers": [{"id": "p1", "label": "A", "base": "https://a.com/v1"}],
            "bulk": {"provider_id": "p1", "model": "gpt-4o", "thinking": "low"},
            "synth": {"provider_id": "p1", "model": "gpt-4o", "thinking": "medium"},
        }
        dashboard_server._save_providers(data)
        data["providers"] = []
        for role in ("bulk", "synth"):
            if data.get(role, {}).get("provider_id") == "p1":
                data[role] = {}
        dashboard_server._save_providers(data)
        loaded = dashboard_server._load_providers()
        self.assertEqual(loaded["providers"], [])
        self.assertEqual(loaded["bulk"], {})
        self.assertEqual(loaded["synth"], {})

    def test_key_hint_masked(self):
        dashboard_server._write_env_file({"LLM_KEY_px": "sk-abcdefghij"})
        key = dashboard_server._provider_key("px")
        self.assertEqual(key, "sk-abcdefghij")
        hint = ("..." + key[-4:]) if len(key) > 4 else "*" * len(key)
        self.assertEqual(hint, "...ghij")

    def test_selection_written_to_env(self):
        data = {
            "providers": [
                {"id": "p1", "label": "OpenAI", "base": "https://api.openai.com/v1"},
                {"id": "p2", "label": "Anthropic", "base": "https://api.anthropic.com/v1"},
            ],
            "bulk": {}, "synth": {},
        }
        dashboard_server._save_providers(data)
        dashboard_server._write_env_file({"LLM_KEY_p1": "sk-oai", "LLM_KEY_p2": "sk-ant"})
        # Simulate what /api/llm_selection does
        env_updates = {
            "PODCAST_LLM_BASE": "https://api.openai.com/v1",
            "PODCAST_LLM_MODEL": "gpt-4o-mini",
            "PODCAST_LLM_KEY": "sk-oai",
            "PODCAST_SYNTH_BASE": "https://api.anthropic.com/v1",
            "PODCAST_SYNTH_MODEL": "claude-sonnet-4-6",
            "PODCAST_SYNTH_KEY": "sk-ant",
        }
        dashboard_server._write_env_file(env_updates)
        env = dashboard_server._load_env_file()
        self.assertEqual(env["PODCAST_LLM_MODEL"], "gpt-4o-mini")
        self.assertEqual(env["PODCAST_SYNTH_MODEL"], "claude-sonnet-4-6")
        self.assertEqual(env["PODCAST_SYNTH_KEY"], "sk-ant")
        self.assertNotEqual(env["PODCAST_LLM_KEY"], env["PODCAST_SYNTH_KEY"])


class TestSynthConfig(unittest.TestCase):
    """synth_config must use PODCAST_SYNTH_KEY/BASE when set (different provider)."""

    def setUp(self):
        self._saved = {}
        for k in ("PODCAST_LLM_KEY", "PODCAST_LLM_BASE", "PODCAST_LLM_MODEL",
                  "PODCAST_SYNTH_KEY", "PODCAST_SYNTH_BASE", "PODCAST_SYNTH_MODEL",
                  "OPENAI_API_KEY"):
            self._saved[k] = os.environ.pop(k, None)
        # Disable load_env so it doesn't pull from disk
        self._orig_load_env = analyze_transcripts.load_env
        analyze_transcripts.load_env = lambda: None

    def tearDown(self):
        for k, v in self._saved.items():
            if v is not None:
                os.environ[k] = v
            else:
                os.environ.pop(k, None)
        analyze_transcripts.load_env = self._orig_load_env

    def test_synth_falls_back_to_bulk(self):
        os.environ["PODCAST_LLM_KEY"] = "bulk-key"
        os.environ["PODCAST_LLM_BASE"] = "https://api.openai.com/v1"
        os.environ["PODCAST_LLM_MODEL"] = "gpt-4o-mini"
        key, base, model = analyze_transcripts.synth_config()
        self.assertEqual(key, "bulk-key")
        self.assertEqual(base, "https://api.openai.com/v1")
        self.assertEqual(model, "gpt-4o-mini")

    def test_synth_uses_own_key_and_base(self):
        os.environ["PODCAST_LLM_KEY"] = "bulk-key"
        os.environ["PODCAST_LLM_BASE"] = "https://api.openai.com/v1"
        os.environ["PODCAST_LLM_MODEL"] = "gpt-4o-mini"
        os.environ["PODCAST_SYNTH_KEY"] = "synth-key"
        os.environ["PODCAST_SYNTH_BASE"] = "https://api.anthropic.com/v1"
        os.environ["PODCAST_SYNTH_MODEL"] = "claude-sonnet-4-6"
        key, base, model = analyze_transcripts.synth_config()
        self.assertEqual(key, "synth-key")
        self.assertEqual(base, "https://api.anthropic.com/v1")
        self.assertEqual(model, "claude-sonnet-4-6")

    def test_synth_model_only_override(self):
        os.environ["PODCAST_LLM_KEY"] = "shared-key"
        os.environ["PODCAST_LLM_BASE"] = "https://api.openai.com/v1"
        os.environ["PODCAST_LLM_MODEL"] = "gpt-4o-mini"
        os.environ["PODCAST_SYNTH_MODEL"] = "gpt-4o"
        key, base, model = analyze_transcripts.synth_config()
        self.assertEqual(key, "shared-key")   # key unchanged
        self.assertEqual(base, "https://api.openai.com/v1")  # base unchanged
        self.assertEqual(model, "gpt-4o")    # model overridden


class TestHermesDirEnvVar(unittest.TestCase):
    """profiles.HERMES must respect the HERMES_DIR env var."""

    def test_default_is_home_hermes(self):
        import importlib
        saved = os.environ.pop("HERMES_DIR", None)
        try:
            import profiles as p
            importlib.reload(p)
            self.assertEqual(p.HERMES, Path.home() / ".hermes")
        finally:
            if saved:
                os.environ["HERMES_DIR"] = saved
            importlib.reload(profiles)

    def test_override_via_env_var(self):
        import importlib
        tmp = tempfile.mkdtemp()
        os.environ["HERMES_DIR"] = tmp
        try:
            import profiles as p
            importlib.reload(p)
            self.assertEqual(str(p.HERMES), tmp)
        finally:
            del os.environ["HERMES_DIR"]
            importlib.reload(profiles)


class TestAPISmoke(unittest.TestCase):
    """Start a real test server and verify all key endpoints return valid JSON."""

    @classmethod
    def setUpClass(cls):
        import threading, http.client, time
        cls.tmp = Path(tempfile.mkdtemp())
        # Point everything at the temp dir — DB, providers, env, and HERMES (for logs)
        fresh_db(str(cls.tmp / "t.db"))
        dashboard_server.DB_PATH = str(cls.tmp / "t.db")
        dashboard_server.PROVIDERS_FILE = cls.tmp / "llm_providers.json"
        dashboard_server.ENV_FILE = cls.tmp / ".env"
        cls._orig_hermes = profiles.HERMES
        cls._orig_legacy_db = profiles.LEGACY_DB
        cls._orig_digests_dir = profiles.DIGESTS_DIR
        profiles.HERMES = cls.tmp
        profiles.LEGACY_DB = cls.tmp / "t.db"  # refresh_active_db() calls profiles.load() on every request
        profiles.DIGESTS_DIR = cls.tmp / "digests"  # digest_dir_for() uses module-level DIGESTS_DIR
        (cls.tmp / "logs").mkdir(exist_ok=True)
        (cls.tmp / "digests").mkdir(exist_ok=True)
        cls.server = __import__("http").server.HTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.2)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        profiles.HERMES = cls._orig_hermes
        profiles.LEGACY_DB = cls._orig_legacy_db
        profiles.DIGESTS_DIR = cls._orig_digests_dir

    def _get(self, path):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", path)
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, json.loads(body)

    def _post(self, path, payload):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = json.dumps(payload).encode()
        conn.request("POST", path, body=data, headers={"Content-Type": "application/json"})
        r = conn.getresponse()
        body = r.read()
        conn.close()
        return r.status, json.loads(body)

    def test_candidates_returns_list(self):
        status, body = self._get("/api/candidates")
        self.assertEqual(status, 200)
        self.assertIsInstance(body, list)

    def test_stats_returns_dict(self):
        status, body = self._get("/api/stats")
        self.assertEqual(status, 200)
        self.assertIn("total", body)

    def test_profiles_returns_active(self):
        status, body = self._get("/api/profiles")
        self.assertEqual(status, 200)
        self.assertIn("active", body)
        self.assertIn("settings", body)

    def test_llm_providers_returns_list(self):
        status, body = self._get("/api/llm_providers")
        self.assertEqual(status, 200)
        self.assertIn("providers", body)
        self.assertIsInstance(body["providers"], list)

    def test_job_log_missing_returns_exists_false(self):
        status, body = self._get("/api/job_log?name=nonexistent_job_xyz")
        self.assertEqual(status, 200)
        self.assertFalse(body["exists"])

    def test_job_status_returns_running_bool(self):
        status, body = self._get("/api/job_status?name=podcast_scraper")
        self.assertEqual(status, 200)
        self.assertIn("running", body)
        self.assertIsInstance(body["running"], bool)

    def test_add_then_delete_provider(self):
        status, body = self._post("/api/llm_providers/add",
            {"label": "TestCo", "base": "https://api.test.com/v1", "key": "sk-test99"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        pid = body["id"]
        # verify it appears in list
        _, providers = self._get("/api/llm_providers")
        ids = [p["id"] for p in providers["providers"]]
        self.assertIn(pid, ids)
        # delete it
        status2, body2 = self._post("/api/llm_providers/delete", {"id": pid})
        self.assertTrue(body2["ok"])
        _, providers2 = self._get("/api/llm_providers")
        ids2 = [p["id"] for p in providers2["providers"]]
        self.assertNotIn(pid, ids2)

    def test_job_log_offset_excludes_previous_run(self):
        """Second run: reader must not see lines from the first run."""
        log_dir = self.tmp / "logs"
        log_dir.mkdir(exist_ok=True)
        log_file = log_dir / "podcast_scraper.log"
        # Simulate a completed first run already in the log
        first_run = "\n".join(f"line {i}" for i in range(50)) + "\n--- Run complete ---\n"
        log_file.write_text(first_run)
        # Snapshot offset (what the client records before starting run 2)
        status, snap = self._get("/api/job_log?name=podcast_scraper&lines=1")
        offset = snap["total_lines"]
        self.assertEqual(offset, 51)  # 50 lines + "--- Run complete ---"
        # Append second run output
        log_file.write_text(first_run + "run2 line A\nrun2 line B\n")
        # Fetch with offset — should only see run 2 lines
        status, body = self._get(f"/api/job_log?name=podcast_scraper&lines=80&offset={offset}")
        self.assertEqual(status, 200)
        self.assertEqual(body["lines"], ["run2 line A", "run2 line B"])
        self.assertNotIn("--- Run complete ---", body["lines"])

    def test_unknown_route_returns_404(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/does_not_exist")
        r = conn.getresponse()
        conn.close()
        self.assertEqual(r.status, 404)

    def test_digest_pending_returns_count(self):
        status, body = self._get("/api/digest_pending")
        self.assertEqual(status, 200)
        self.assertIn("count", body)

    def test_report_channels_returns_list(self):
        status, body = self._get("/api/report_channels")
        self.assertEqual(status, 200)
        self.assertIn("channels", body)
        self.assertIsInstance(body["channels"], list)

    def _digest_dir(self):
        """Returns the digest dir the test server resolves to (DIGESTS_DIR / profile-slug)."""
        import profiles as p
        ddir = p.DIGESTS_DIR / "seo-geo"
        ddir.mkdir(parents=True, exist_ok=True)
        return ddir

    def test_digest_list_returns_list(self):
        ddir = self._digest_dir()
        (ddir / "digest_2026-06-01.md").write_text("# Test Digest\n")
        status, body = self._get("/api/digest_list")
        self.assertEqual(status, 200)
        self.assertIn("digests", body)
        self.assertIsInstance(body["digests"], list)
        names = [d["filename"] for d in body["digests"]]
        self.assertIn("digest_2026-06-01.md", names)

    def test_digest_file_returns_content(self):
        ddir = self._digest_dir()
        (ddir / "digest_2026-06-02.md").write_text("# Hello Digest\n")
        status, body = self._get("/api/digest_file?name=digest_2026-06-02.md")
        self.assertEqual(status, 200)
        self.assertIn("Hello Digest", body["markdown"])

    def test_digest_file_rejects_traversal(self):
        import http.client
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request("GET", "/api/digest_file?name=../../../etc/passwd")
        r = conn.getresponse()
        conn.close()
        self.assertEqual(r.status, 400)

    def test_delete_digest_removes_file(self):
        ddir = self._digest_dir()
        (ddir / "digest_2026-06-03.md").write_text("# To Delete\n")
        status, body = self._post("/api/delete_digest", {"filename": "digest_2026-06-03.md"})
        self.assertEqual(status, 200)
        self.assertTrue(body["ok"])
        self.assertFalse((ddir / "digest_2026-06-03.md").exists())

    def test_delete_digest_rejects_traversal(self):
        status, body = self._post("/api/delete_digest", {"filename": "../../../tmp/evil.md"})
        self.assertEqual(status, 200)
        self.assertFalse(body["ok"])


class TestOvernightPipeline(unittest.TestCase):
    """The unattended runner must not crash, and must stop re-promoting a
    stubbornly-failing 'error' row forever (LEARNINGS P8)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.db = os.path.join(self.tmp, "t.db")
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE videos (id TEXT PRIMARY KEY, transcript_status TEXT, "
                     "fetch_attempts INTEGER DEFAULT 0)")
        conn.execute("INSERT INTO videos VALUES ('dead', 'error', 0)")
        conn.execute("INSERT INTO videos VALUES ('ok', 'requested', 0)")
        conn.commit(); conn.close()
        import overnight_pipeline
        self.op = overnight_pipeline

    def test_promote_errors_caps_retries(self):
        # Promote up to MAX_FETCH_ATTEMPTS times, then stop (row becomes capped).
        promoted_rounds = 0
        for _ in range(self.op.MAX_FETCH_ATTEMPTS + 3):
            # Re-mark the dead row as error each round (a real fetch would).
            c = sqlite3.connect(self.db)
            c.execute("UPDATE videos SET transcript_status='error' WHERE id='dead' "
                      "AND transcript_status='requested'")
            c.commit(); c.close()
            promoted, capped = self.op.promote_errors(self.db)
            if promoted:
                promoted_rounds += 1
        # It promoted at most MAX_FETCH_ATTEMPTS times, then gave up.
        self.assertLessEqual(promoted_rounds, self.op.MAX_FETCH_ATTEMPTS)
        c = sqlite3.connect(self.db)
        attempts = c.execute("SELECT fetch_attempts FROM videos WHERE id='dead'").fetchone()[0]
        c.close()
        self.assertEqual(attempts, self.op.MAX_FETCH_ATTEMPTS)

    def test_write_digest_signature_matches(self):
        # Regression: overnight called build_digest(days=...) which doesn't exist,
        # crashing every round. Ensure the call signature is now valid.
        import inspect, generate_digest
        sig = inspect.signature(generate_digest.build_digest)
        self.assertNotIn("days", sig.parameters)
        self.assertIn("force", sig.parameters)


if __name__ == "__main__":
    unittest.main(verbosity=2)
