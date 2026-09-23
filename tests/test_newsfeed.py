from __future__ import annotations

import json
import stat
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from newsfeed.cli import main
from newsfeed.core import atomic_json, exclusive_lock, parse_feed, publish, refresh
from newsfeed.server import handler_factory

RSS = b'''<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>Newest</title><link>https://example.test/new</link><pubDate>Wed, 23 Sep 2026 16:00:00 +0000</pubDate></item>
<item><title>Second item</title><link>https://example.test/second</link><pubDate>Wed, 23 Sep 2026 15:00:00 +0000</pubDate></item>
<item><title>Old</title><link>https://example.test/old</link><pubDate>Sun, 20 Sep 2026 15:00:00 +0000</pubDate></item>
<item><title>No date</title><link>https://example.test/no-date</link></item>
</channel></rss>'''
ATOM = b'''<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
<entry><title>Atom title</title><link rel="alternate" href="https://example.test/atom"/><published>2026-09-23T14:00:00+02:00</published></entry>
</feed>'''


class CollectorTests(unittest.TestCase):
    def test_production_config_is_exact_contract(self):
        expected = {
            "Hacker News": "https://hnrss.org/frontpage?count=10",
            "Ars Technica": "https://feeds.arstechnica.com/arstechnica/index",
            "SVT Nyheter": "https://www.svt.se/nyheter/rss.xml",
            "Rock Paper Shotgun": "https://www.rockpapershotgun.com/feed",
            "Zephyr Project": "https://zephyrproject.org/feed/",
            "Quanta Magazine": "https://api.quantamagazine.org/feed/",
            "embedded.fm": "http://makingembeddedsystems.libsyn.com/rss",
            "arXiv cs.AI": "http://export.arxiv.org/rss/cs.AI",
            "arXiv cs.LG": "http://export.arxiv.org/rss/cs.LG",
            "Tom's Hardware": "https://www.tomshardware.com/feeds/all",
            "TechPowerUp": "https://www.techpowerup.com/rss/news",
            "Phoronix": "https://www.phoronix.com/rss.php",
            "Chips and Cheese": "https://chipsandcheese.com/feed/",
            "ServeTheHome": "https://www.servethehome.com/feed/",
            "Hackaday": "https://hackaday.com/feed/",
            "PC Gamer": "https://www.pcgamer.com/rss/",
            "Eurogamer": "https://www.eurogamer.net/feed",
            "Steam New Releases": "https://store.steampowered.com/feeds/newreleases.xml",
        }
        config = Path(__file__).parents[1] / "config" / "feeds.json"
        self.assertEqual(json.loads(config.read_text()), expected)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        self.now = datetime(2026, 9, 23, 17, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temp.cleanup()

    def test_rss_atom_and_exact_item_contract(self):
        rss = parse_feed(RSS, "RSS", self.now)
        atom = parse_feed(ATOM, "Atom", self.now)
        self.assertEqual(atom[0]["publishedAt"], "2026-09-23T12:00:00+00:00")
        self.assertEqual(list(rss[0]), ["title", "url", "source", "publishedAt", "fetchedAt"])
        self.assertIsNone(rss[-1]["publishedAt"])

    def test_filter_limit_and_global_sort_nulls_last(self):
        feeds = {"RSS": "rss", "Atom": "atom"}
        bodies = {"rss": RSS, "atom": ATOM}
        output, status = refresh(feeds=feeds, now=self.now, output_dir=self.out, retries=0,
                                 fetcher=lambda url, timeout: bodies[url])
        self.assertNotIn("https://example.test/old", [x["url"] for x in output])
        self.assertEqual([x["title"] for x in output], ["Newest", "Second item", "Atom title", "No date"])
        self.assertTrue(status["healthy"])
        self.assertTrue(all(set(x) == {"title", "url", "source", "publishedAt", "fetchedAt"} for x in output))

    def test_deliberate_broken_feed_retains_unchanged_last_good_and_records_error(self):
        feeds = {"Broken Later": "working"}
        first, first_status = refresh(feeds=feeds, now=self.now, output_dir=self.out, retries=0,
                                      fetcher=lambda url, timeout: RSS)
        self.assertIsNone(first_status["sources"]["Broken Later"]["error"])
        first_source = [x for x in first if x["source"] == "Broken Later"]

        def broken(url, timeout):
            raise OSError("deliberate broken-feed proof")

        second, status = refresh(feeds={"Broken Later": "broken://deliberate"},
                                 now=self.now + timedelta(hours=1), output_dir=self.out,
                                 retries=1, fetcher=broken, sleeper=lambda _: None)
        second_source = [x for x in second if x["source"] == "Broken Later"]
        self.assertEqual(second_source, first_source)
        state = status["sources"]["Broken Later"]
        self.assertEqual(state["lastSuccess"], "2026-09-23T17:00:00+00:00")
        self.assertEqual(state["lastAttempt"], "2026-09-23T18:00:00+00:00")
        self.assertEqual(state["itemCount"], len(first_source))
        self.assertIn("deliberate broken-feed proof", state["error"])
        self.assertFalse(status["healthy"])
        self.assertEqual(json.loads((self.out / "feed.json").read_text()), first_source)
        public = self.out / "published"
        publish(second, status, [public])
        self.assertEqual(json.loads((public / "feed.json").read_text()), first_source)
        self.assertEqual(json.loads((public / "health.json").read_text()), {
            "healthy": False,
            "generatedAt": "2026-09-23T18:00:00+00:00",
            "failedSources": ["Broken Later"],
        })

    def test_successful_empty_after_age_filter_is_valid(self):
        old = RSS.replace(b"Wed, 23 Sep 2026 16:00:00 +0000", b"Sun, 20 Sep 2026 16:00:00 +0000").replace(
            b"Wed, 23 Sep 2026 15:00:00 +0000", b"Sun, 20 Sep 2026 15:00:00 +0000")
        output, status = refresh(feeds={"x": "x"}, now=self.now, output_dir=self.out, retries=0,
                                 fetcher=lambda u, t: old)
        self.assertEqual([x["title"] for x in output], ["No date"])
        self.assertIsNone(status["sources"]["x"]["error"])

    def test_nonblocking_lock_rejects_overlap(self):
        lock = self.out / "refresh.lock"
        with exclusive_lock(lock):
            with self.assertRaisesRegex(RuntimeError, "already running"):
                with exclusive_lock(lock):
                    pass

    def test_publication_contains_exact_public_files_and_scrubs_private_state(self):
        public = self.out / "public"
        public.mkdir()
        (public / "cache.json").write_text('{"secret": true}\n')
        (public / "refresh.lock").write_text("stale")
        status = {
            "generatedAt": "2026-09-23T17:00:00+00:00",
            "healthy": True,
            "sources": {"RSS": {"error": None}},
        }
        publish([{"title": "x"}], status, [public])
        self.assertEqual({path.name for path in public.iterdir()}, {"feed.json", "status.json", "health.json"})
        self.assertEqual(json.loads((public / "health.json").read_text()), {
            "healthy": True,
            "generatedAt": "2026-09-23T17:00:00+00:00",
            "failedSources": [],
        })

    def test_atomic_json_keeps_prior_complete_document_on_serialization_failure(self):
        target = self.out / "feed.json"
        target.write_text('[{"old": true}]\n')
        before = target.read_bytes()
        with mock.patch("newsfeed.core.json.dump", side_effect=RuntimeError("deliberate write failure")):
            with self.assertRaisesRegex(RuntimeError, "deliberate write failure"):
                atomic_json(target, [{"new": True}])
        self.assertEqual(target.read_bytes(), before)
        self.assertEqual(list(self.out.glob(".feed.json.*")), [])

    def test_atomic_json_publishes_world_readable_files(self):
        target = self.out / "feed.json"
        atomic_json(target, [{"public": True}])
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

    def test_cli_publishes_degraded_fallback_to_every_directory_before_nonzero(self):
        config = self.out / "feeds.json"
        config.write_text('{"Broken": "broken://deliberate"}\n')
        status = {
            "generatedAt": "2026-09-23T18:00:00+00:00",
            "healthy": False,
            "sources": {"Broken": {"error": "deliberate source failure"}},
        }
        first = self.out / "static"
        second = self.out / "built"
        with mock.patch("newsfeed.cli.refresh", return_value=([{"title": "retained"}], status)):
            result = main([
                "refresh", "--config", str(config), "--output-dir", str(self.out / "data"),
                "--publish-dir", str(first), "--publish-dir", str(second),
            ])
        self.assertEqual(result, 1)
        for directory in (first, second):
            self.assertEqual({path.name for path in directory.iterdir()}, {"feed.json", "status.json", "health.json"})
            self.assertEqual(json.loads((directory / "feed.json").read_text()), [{"title": "retained"}])
            self.assertEqual(json.loads((directory / "health.json").read_text()), {
                "healthy": False,
                "generatedAt": "2026-09-23T18:00:00+00:00",
                "failedSources": ["Broken"],
            })


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.out = Path(self.temp.name)
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        (self.out / "feed.json").write_text("[]\n")
        (self.out / "status.json").write_text(json.dumps({"generatedAt": now, "healthy": True, "sources": {}}))
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory(self.out, 3600))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def request(self, path, method="GET"):
        return urllib.request.urlopen(urllib.request.Request(self.base + path, method=method))

    def test_exact_routes_and_health(self):
        self.assertEqual(self.request("/command-news/feed.json").status, 200)
        self.assertEqual(self.request("/command-news/status.json").status, 200)
        with self.request("/command-news/health") as response:
            self.assertEqual(json.load(response)["healthy"], True)
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.request("/command-news/nope")
        self.assertEqual(caught.exception.code, 404)

    def test_mutations_rejected(self):
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            with self.assertRaises(urllib.error.HTTPError) as caught:
                self.request("/command-news/feed.json", method)
            self.assertEqual(caught.exception.code, 405)
            self.assertEqual(caught.exception.headers["Allow"], "GET, HEAD")


if __name__ == "__main__":
    unittest.main()
