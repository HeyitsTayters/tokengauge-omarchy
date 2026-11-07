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


def record(agent="claude", **fields):
    return {"schemaVersion": 1, "id": agent, "name": "Claude Code", "ready": True, "tierLabel": "Pro",
            "limits": [{"label": "Session", "percent": 0.25, "resetsAt": "2099-01-01T00:00:00+00:00"}],
            "limitsFetchedAt": 0, **fields}


def options(**fields):
    values = {"claude": True, "codex": True, "omarchy": True, "account_limits": True, "anthropic_api": False,
              "openai_api": False, "block_budget": 0, "weekly_budget": 0, **fields}
    return mock.Mock(**values)


class Records(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        root = Path(self.dir.name)
        patcher = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                               "XDG_CACHE_HOME": str(root / "cache"),
                                               "CLAUDE_CONFIG_DIR": str(root / "claude"),
                                               "CODEX_HOME": str(root / "codex")})
        patcher.start()
        self.addCleanup(patcher.stop)
        (root / "claude" / "projects").mkdir(parents=True)
        (root / "codex" / "sessions").mkdir(parents=True)
        self.folder = root / "state" / "omarchy" / "agents" / "usage"
        self.folder.mkdir(parents=True)

    def write(self, value):
        (self.folder / f"{value['id']}.json").write_text(json.dumps(value))

    def collector(self, **fields):
        collector = tg.Collector(options(**fields))
        collector.usage.command = None
        return collector

    def test_fractions_become_percents(self):
        self.assertEqual(tg.record_limits(record(), time.time())[0]["percent"], 25)

    def test_window_past_its_reset_reads_zero(self):
        value = record(limits=[{"label": "Session", "percent": 0.9, "resetsAt": "2001-01-01T00:00:00+00:00"}])
        self.assertEqual(tg.record_limits(value, time.time())[0]["percent"], 0)

    def test_bad_limits_are_skipped(self):
        value = record(limits=["x", {"percent": "1"}, {"percent": True}, {"percent": -1}])
        self.assertEqual(tg.record_limits(value, time.time()), [])

    def test_record_replaces_the_login(self):
        self.write(record())
        collector = self.collector()
        with mock.patch.object(tg.ClaudeAccount, "fetch") as fetch:
            claude = collector.collect()["providers"][0]
        fetch.assert_not_called()
        self.assertEqual(claude["limits"][0]["percent"], 25)
        self.assertEqual(claude["note"], "Plan: Pro")

    def test_record_without_limits_still_keeps_us_off_the_endpoint(self):
        self.write(record(limits=[], usageStatusText="Rate-limited"))
        collector = self.collector()
        with mock.patch.object(tg.ClaudeAccount, "fetch") as fetch:
            claude = collector.collect()["providers"][0]
        fetch.assert_not_called()
        self.assertEqual(claude["note"], "Plan: Pro · Rate-limited")

    def test_no_record_falls_back_to_the_login(self):
        collector = self.collector()
        with mock.patch.object(tg.ClaudeAccount, "fetch", return_value=([tg.limit("S", 40)], "n")) as fetch:
            claude = collector.collect()["providers"][0]
        fetch.assert_called_once()
        self.assertEqual(claude["limits"][0]["percent"], 40)

    def test_account_limits_off_ignores_the_record(self):
        self.write(record())
        collector = self.collector(account_limits=False)
        with mock.patch.object(tg.ClaudeAccount, "fetch") as fetch:
            claude = collector.collect()["providers"][0]
        fetch.assert_not_called()
        self.assertEqual(claude["limits"][0]["percent"], -1)

    def test_omarchy_off_reads_nothing(self):
        self.write(record("grok", name="Grok"))
        ids = [p["id"] for p in self.collector(omarchy=False).collect()["providers"]]
        self.assertEqual(ids, ["claudeCode", "codex"])

    def test_other_agents_appear_and_unused_ones_do_not(self):
        today = tg.date.today().isoformat()
        self.write(record("opencode", name="Open<b>Code", limits=[], recentDays=[{"date": today, "messageCount": 900}],
                          todayTokensByModel={"m": 900}))
        self.write(record("kimi", name="Kimi", ready=False, limits=[]))
        snap = self.collector().collect()
        agent = snap["providers"][2]
        self.assertEqual((agent["id"], agent["name"]), ("opencode", "OpenbCode"))
        self.assertEqual(agent["today"]["billable"], 900)
        self.assertEqual(agent["topModels"], [{"name": "m", "tokens": 900}])
        self.assertEqual([a["id"] for a in snap["agents"]], ["claudeCode", "codex", "opencode"])

    def test_yesterdays_record_counts_nothing_today(self):
        yesterday = (tg.date.today() - tg.timedelta(days=1)).isoformat()
        value = record("grok", recentDays=[{"date": yesterday, "messageCount": 5}], todayTokensByModel={"m": 5})
        agent = tg.record_agent(value, time.time())
        self.assertEqual((agent["today"]["total"], agent["week"]["total"], agent["topModels"]), (0, 5, []))

    def test_mismatched_or_damaged_records_are_ignored(self):
        (self.folder / "a.json").write_text("{")
        (self.folder / "b.json").write_text(json.dumps(record("other")))
        (self.folder / "c.json").write_text(json.dumps({**record("c"), "schemaVersion": 2}))
        self.assertEqual(tg.OmarchyUsage().read(), {})

    def test_hiding_an_agent_survives_a_new_backend_and_a_cache_wipe(self):
        collector = self.collector()
        with mock.patch.object(tg.ClaudeAccount, "fetch", side_effect=tg.Failure("x")) as fetch:
            collector.command(b"hide claudeCode\n")
            fresh = self.collector().collect()
        fetch.assert_not_called()
        self.assertEqual([p["id"] for p in fresh["providers"]], ["codex"])
        self.assertEqual(fresh["agents"][0], {"id": "claudeCode", "name": "Claude Code", "hidden": True})
        collector.command(b"show claudeCode\n")
        self.assertEqual(tg.HiddenAgents().load(), set())

    def test_bad_commands_change_nothing(self):
        self.collector().command(b"refresh\nhide ../x\nhide a b\n\xff\n")
        self.assertEqual(tg.HiddenAgents().load(), set())

    def test_update_runs_once_and_skips_hidden_agents(self):
        usage = tg.OmarchyUsage()
        usage.command = "update"
        other = tg.OmarchyUsage()
        other.command = "update"
        with mock.patch.object(tg.subprocess, "Popen") as popen:
            popen.return_value.poll.return_value = 0
            usage.update({"claude"}, time.time())
            other.update(set(), time.time())
        popen.assert_called_once()
        self.assertEqual(popen.call_args[0][0], ["update", "--except", "claude"])

    def test_fresh_records_mean_no_update(self):
        self.write(record())
        usage = tg.OmarchyUsage()
        usage.command = "update"
        with mock.patch.object(tg.subprocess, "Popen") as popen:
            usage.update(set(), time.time())
        popen.assert_not_called()

    def test_stale_records_do_update(self):
        self.write(record())
        os.utime(self.folder / "claude.json", (1000, 1000))
        usage = tg.OmarchyUsage()
        usage.command = "update"
        with mock.patch.object(tg.subprocess, "Popen") as popen:
            usage.update(set(), time.time())
        popen.assert_called_once()


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
