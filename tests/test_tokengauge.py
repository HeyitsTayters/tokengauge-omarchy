"""Run with: python3 -m unittest discover tests"""
import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tokengauge as tg


def good(now):
    return {"limits": [tg.limit("5-hour session", 40, now + 3600), tg.limit("Weekly", 10, now + 86400)],
            "note": "Plan limits"}


def racer(cache, counter):
    os.environ["XDG_CACHE_HOME"] = cache

    def request(self, now):
        with open(counter, "a") as handle:
            handle.write("x")
        time.sleep(0.3)
        return good(now)

    with mock.patch.object(tg.ClaudeAccount, "request", request):
        tg.ClaudeAccount().fetch()


class Account(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        patcher = mock.patch.dict(os.environ, {"XDG_CACHE_HOME": self.dir.name})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_backends_share_one_request(self):
        counter = os.path.join(self.dir.name, "count")
        jobs = [multiprocessing.Process(target=racer, args=(self.dir.name, counter)) for _ in range(4)]
        for job in jobs:
            job.start()
        for job in jobs:
            job.join()
        self.assertEqual(os.path.getsize(counter), 1)

    def test_rate_limit_keeps_last_result_and_honors_retry_after(self):
        account = tg.ClaudeAccount()
        with mock.patch.object(tg.ClaudeAccount, "request", lambda s, now: good(now)):
            account.fetch()
        account.state["nextAllowed"] = 0
        (account.path).write_text(json.dumps(account.state))

        calls = []

        def limited(_, now):
            calls.append(now)
            raise tg.Failure("Claude usage is rate-limited right now", 429, 1200)

        with mock.patch.object(tg.ClaudeAccount, "request", limited):
            limits, note = account.fetch()
            self.assertEqual(limits[0]["percent"], 40)
            self.assertIn("rate-limited", note)
            self.assertIn("showing limits from", note)
            self.assertGreater(account.state["nextAllowed"], time.time() + 1100)
            limits, note = account.fetch()
        self.assertEqual(len(calls), 1)
        self.assertIn("rate-limited", note)

    def test_rate_limit_with_no_data_does_not_retry_early(self):
        calls = []

        def limited(_, now):
            calls.append(now)
            raise tg.Failure("Claude usage is rate-limited right now", 429, None)

        account = tg.ClaudeAccount()
        with mock.patch.object(tg.ClaudeAccount, "request", limited):
            for _ in range(3):
                with self.assertRaises(tg.Failure):
                    account.fetch()
        self.assertEqual(len(calls), 1)

    def test_backoff_doubles_to_an_hour(self):
        def limited(_, now):
            raise tg.Failure("limited", 429, None)

        account = tg.ClaudeAccount()
        seen = []
        with mock.patch.object(tg.ClaudeAccount, "request", limited):
            for _ in range(6):
                with self.assertRaises(tg.Failure):
                    account.fetch()
                seen.append(account.state["backoff"])
                account.state["nextAllowed"] = 0
                account.path.write_text(json.dumps(account.state))
        self.assertEqual(seen, [600, 1200, 2400, 3600, 3600, 3600])

    def test_window_past_its_reset_reads_zero(self):
        account = tg.ClaudeAccount()
        now = time.time()
        with mock.patch.object(tg.ClaudeAccount, "request", lambda s, n: {
                "limits": [tg.limit("5-hour session", 90, now - 10), tg.limit("Weekly", 30, now + 900)],
                "note": "n"}):
            limits, _ = account.fetch()
        self.assertEqual((limits[0]["percent"], limits[0]["detail"]), (0, "reset"))
        self.assertEqual(limits[1]["percent"], 30)

    def test_expired_login_makes_no_request(self):
        home = Path(self.dir.name)
        (home / ".credentials.json").write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "t", "expiresAt": 1000}}))
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}), \
             mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")):
            with self.assertRaisesRegex(tg.Failure, "expired"):
                tg.ClaudeAccount().fetch()

    def test_missing_login(self):
        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.dir.name}):
            with self.assertRaisesRegex(tg.Failure, "No Claude Code login"):
                tg.ClaudeAccount().fetch()

    def test_token_never_reaches_the_cache(self):
        home = Path(self.dir.name)
        (home / ".credentials.json").write_text(json.dumps(
            {"claudeAiOauth": {"accessToken": "SECRET-TOKEN", "subscriptionType": "pro"}}))

        class Response:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def read(self, _): return json.dumps({"five_hour": {"utilization": 5, "resets_at": None}}).encode()

        with mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(home)}), \
             mock.patch("urllib.request.urlopen", return_value=Response()) as opened:
            account = tg.ClaudeAccount()
            limits, note = account.fetch()
        self.assertEqual(opened.call_args[0][0].get_header("Authorization"), "Bearer SECRET-TOKEN")
        self.assertEqual(limits[0]["label"], "5-hour session")
        self.assertIn("(Pro)", note)
        self.assertNotIn("SECRET-TOKEN", account.path.read_text())
        self.assertEqual(account.path.stat().st_mode & 0o777, 0o600)


class Logs(unittest.TestCase):
    def write(self, lines):
        handle = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
        self.addCleanup(os.unlink, handle.name)
        handle.write("\n".join(lines) + "\n")
        handle.close()
        return handle.name

    def test_claude_skips_synthetic_and_bad_lines(self):
        def line(model):
            return json.dumps({"type": "assistant", "timestamp": "2026-11-06T08:27:56.012Z", "requestId": "r",
                               "message": {"id": "m", "model": model, "usage": {
                                   "input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3,
                                   "cache_creation_input_tokens": 4}}})
        events = tg.parse_claude(self.write([line("a"), line("<synthetic>"), '{"usage" broken']))
        self.assertEqual(len(events), 1)
        self.assertEqual((events[0].tokens.billable, events[0].tokens.total, events[0].key), (7, 10, "m:r"))

    def test_codex_counts_only_the_change(self):
        def line(total):
            return json.dumps({"timestamp": "2026-11-06T08:00:00.123456Z", "type": "event_msg", "payload": {
                "type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": total, "cached_input_tokens": 0, "output_tokens": 0}}}})
        events, _ = tg.parse_codex(self.write([line(100), line(100), line(150)]))
        self.assertEqual([event.tokens.input for event in events], [100, 50])

    def test_snapshot_keeps_seven_days_ending_today(self):
        now = time.time()
        old = tg.Event(now - 30 * tg.DAY, "m", tg.Tokens(input=5), None)
        new = tg.Event(now - 1, "m", tg.Tokens(input=7), None)
        snap = tg.snapshot("x", "X", [old, new], now)
        self.assertEqual(len(snap["daily"]), 7)
        self.assertEqual(snap["week"]["input"], 7)

    def test_token_format(self):
        self.assertEqual([tg.fmt_tokens(n) for n in (999, 1500, 2_500_000, 3_000_000_000)],
                         ["999", "2K", "2.5M", "3.0B"])


if __name__ == "__main__":
    unittest.main()
