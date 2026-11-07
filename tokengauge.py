#!/usr/bin/env python3
"""TokenGauge backend: AI token usage and plan limits as JSON.

A Linux port of the provider logic in the TokenGauge macOS app. Reads Claude
Code and Codex CLI session logs, takes plan limits and every other agent from
the usage records Omarchy's own collectors keep, and optionally org usage from
the Anthropic and OpenAI Admin APIs. Without those records it falls back to the
endpoint behind Claude Code's /usage screen and the limits in Codex's logs.

  tokengauge.py                  print one snapshot and exit
  tokengauge.py --watch          print a JSON line every --interval seconds;
                                 a line on stdin refreshes now, "hide <id>" and
                                 "show <id>" also set an agent, EOF exits

Standard library only. Nothing but the numbers shown in the widget is cached.
"""

import argparse
import fcntl
import json
import os
import re
import select
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

USER_AGENT = "TokenGauge/1.0"
KEYRING_SERVICE = "dev.tokengauge.apikeys"
BLOCK_SECONDS = 5 * 3600
DAY = 86400
MAX_RESPONSE = 4 * 1024 * 1024
MAX_PAGES = 20
API_TTL = 300
AGENT_TTL = 900
# Omarchy's record names for the two agents read from their own logs too.
RECORD_IDS = {"claudeCode": "claude", "codex": "codex"}


@dataclass
class Tokens:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def billable(self):
        """Tokens that count against budgets. Cache reads are cheap and excluded."""
        return self.input + self.output + self.cache_write

    @property
    def total(self):
        return self.billable + self.cache_read

    def add(self, other):
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write += other.cache_write

    def json(self):
        return {"input": self.input, "output": self.output, "cacheRead": self.cache_read,
                "cacheWrite": self.cache_write, "billable": self.billable, "total": self.total}


Event = namedtuple("Event", "ts model tokens key")


def fmt_tokens(n):
    if abs(n) >= 1_000_000_000:
        return f"{n / 1_000_000_000:.1f}B"
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if abs(n) >= 1_000:
        return f"{n / 1_000:.0f}K"
    return str(n)


def as_int(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


def parse_time(value):
    """ISO 8601 string to epoch seconds, or None."""
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def local_midnight(day):
    return datetime(day.year, day.month, day.day).timestamp()


def clock(ts):
    return datetime.fromtimestamp(ts).strftime("%H:%M")


def state_home():
    return Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")


def cache_home():
    return Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")


def valid_id(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,40}", value) is not None


def clean_name(value, fallback):
    """Omarchy's tooltip isn't plain text, so a name that reaches it can't carry markup."""
    name = re.sub(r"[^\w .+-]", "", value)[:40].strip() if isinstance(value, str) else ""
    return name or fallback


def jsonl(path, needle):
    """Parsed objects from the lines of a JSONL file that contain needle."""
    try:
        with open(path, "rb") as handle:
            for line in handle:
                if needle in line:
                    try:
                        obj = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(obj, dict):
                        yield obj
    except OSError:
        return


class FileCache:
    """Re-parses a JSONL file only when its size or modification time changes."""

    def __init__(self, parse):
        self.parse = parse
        self.entries = {}

    def scan(self, root, cutoff):
        seen = {}
        for folder, _, names in os.walk(root):
            for name in names:
                if not name.endswith(".jsonl"):
                    continue
                path = os.path.join(folder, name)
                try:
                    info = os.stat(path)
                except OSError:
                    continue
                if info.st_mtime < cutoff:
                    continue
                stamp = (info.st_mtime_ns, info.st_size)
                cached = self.entries.get(path)
                seen[path] = cached if cached and cached[0] == stamp else (stamp, self.parse(path))
        self.entries = seen
        return [value for _, value in seen.values()]


def snapshot(provider, name, events, now):
    """Today and week totals, a 7-day series, and a per-model ranking."""
    today = date.fromtimestamp(now)
    days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    week_start = local_midnight(days[0])
    today_start = local_midnight(today)

    today_tokens, week_tokens = Tokens(), Tokens()
    per_day, per_model = {}, {}
    for event in events:
        if not week_start <= event.ts <= now:
            continue
        week_tokens.add(event.tokens)
        if event.ts >= today_start:
            today_tokens.add(event.tokens)
        day = date.fromtimestamp(event.ts)
        per_day[day] = per_day.get(day, 0) + event.tokens.billable
        per_model[event.model] = per_model.get(event.model, 0) + event.tokens.billable

    ranked = sorted(per_model.items(), key=lambda item: -item[1])[:4]
    return {
        "id": provider,
        "name": name,
        "today": today_tokens.json(),
        "week": week_tokens.json(),
        "daily": [{"day": d.isoformat(), "tokens": per_day.get(d, 0)} for d in days],
        "limits": [],
        "topModels": [{"name": m, "tokens": t} for m, t in ranked],
        "note": None,
        "error": None,
    }


def failed(provider, name, error):
    snap = snapshot(provider, name, [], time.time())
    snap["error"] = str(error)
    return snap


def limit(label, percent, resets_at=None, detail=None):
    return {"label": label, "percent": percent, "resetsAt": resets_at, "detail": detail}


# ---- Claude Code


def parse_claude(path):
    events = []
    for obj in jsonl(path, b'"usage"'):
        message = obj.get("message")
        if obj.get("type") != "assistant" or not isinstance(message, dict):
            continue
        usage, ts = message.get("usage"), parse_time(obj.get("timestamp"))
        model = message.get("model") or "unknown"
        if not isinstance(usage, dict) or ts is None or model == "<synthetic>":
            continue
        tokens = Tokens(as_int(usage.get("input_tokens")), as_int(usage.get("output_tokens")),
                        as_int(usage.get("cache_read_input_tokens")),
                        as_int(usage.get("cache_creation_input_tokens")))
        message_id = message.get("id")
        key = f"{message_id}:{obj.get('requestId') or ''}" if message_id else None
        events.append(Event(ts, str(model), tokens, key))
    return events


class Failure(Exception):
    def __init__(self, message, status=None, retry_after=None):
        super().__init__(message)
        self.status = status
        self.retry_after = retry_after


class ClaudeAccount:
    """Exact plan limits from the endpoint behind Claude Code's /usage screen
    (undocumented; may change).

    Uses Claude Code's own login. The token is only read, never refreshed, so
    this can't sign Claude Code out; if it has expired, using Claude Code
    renews it. The endpoint rate-limits aggressively and other tools may poll
    it too, so results last 5 minutes and 429s back off up to an hour. That
    state is shared on disk under a lock: every bar runs its own backend, and
    together they should send one request, as should a shell reload. The token
    itself is never written anywhere.
    """

    def __init__(self):
        self.path = cache_home() / "tokengauge" / "claude-limits.json"
        self.lock = self.path.with_suffix(".lock")
        self.state = {}

    def load(self):
        self.state = {"last": None, "lastSuccess": None, "nextAllowed": 0, "backoff": 300, "error": None}
        try:
            self.state.update(json.loads(self.path.read_text()))
        except (OSError, ValueError):
            pass

    def save(self):
        try:
            temp = self.path.with_suffix(".tmp")
            descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                json.dump(self.state, handle)
            os.replace(temp, self.path)
        except OSError:
            pass

    def fetch(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(self.lock, "w") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            self.load()
            now, state = time.time(), self.state
            if now >= state["nextAllowed"]:
                try:
                    state["last"] = self.request(now)
                    state.update(lastSuccess=now, nextAllowed=now + 300, backoff=300, error=None)
                except Failure as error:
                    state["error"] = str(error)
                    if error.status == 429:
                        state["nextAllowed"] = now + (error.retry_after or state["backoff"])
                        state["backoff"] = min(state["backoff"] * 2, 3600)
                    else:
                        state["nextAllowed"] = now + 60
                self.save()

        last = state["last"]
        if not last:
            raise Failure(state["error"] or "Claude limits aren't available yet")
        # A window whose reset time has passed is back to zero, even without fresh data.
        limits = [limit(l["label"], 0, None, "reset") if l["resetsAt"] and l["resetsAt"] <= now else l
                  for l in last["limits"]]
        note = last["note"]
        if state["error"] and state["lastSuccess"]:
            note = f"{state['error']} · showing limits from {clock(state['lastSuccess'])}"
        return limits, note

    def request(self, now):
        creds = self.credentials()
        token = creds.get("accessToken")
        if not isinstance(token, str) or not token:
            raise Failure("No Claude Code login found")
        expires = creds.get("expiresAt")
        if isinstance(expires, (int, float)) and expires / 1000 < now:
            raise Failure("Claude Code login expired; it renews next time you use Claude Code")

        request = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": USER_AGENT,
        })
        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                body = json.loads(response.read(MAX_RESPONSE))
        except urllib.error.HTTPError as error:
            message = ("Claude usage is rate-limited right now" if error.code == 429
                       else f"Usage endpoint returned HTTP {error.code}")
            raise Failure(message, error.code, retry_after(error)) from None
        except (OSError, ValueError):
            raise Failure("Couldn't reach the Claude usage endpoint") from None
        if not isinstance(body, dict):
            raise Failure("Usage endpoint returned an unexpected response")

        limits = []
        entries = body.get("limits")
        if isinstance(entries, list) and entries:
            for entry in entries:
                if isinstance(entry, dict) and isinstance(entry.get("percent"), (int, float)):
                    limits.append(limit(limit_label(entry.get("kind")), entry["percent"],
                                        parse_time(entry.get("resets_at"))))
        else:
            for key, label in (("five_hour", "5-hour session"), ("seven_day", "Weekly (all models)"),
                               ("seven_day_opus", "Weekly (Opus)"), ("seven_day_sonnet", "Weekly (Sonnet)")):
                window = body.get(key)
                if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)):
                    limits.append(limit(label, window["utilization"], parse_time(window.get("resets_at"))))

        note = "Plan limits from your Claude account"
        plan = creds.get("subscriptionType")
        if isinstance(plan, str) and plan:
            note += f" ({plan.capitalize()})"
        spend = body.get("spend")
        used = spend.get("used") if isinstance(spend, dict) else None
        if isinstance(used, dict) and as_int(used.get("amount_minor")) > 0:
            exponent = as_int(used.get("exponent")) or 2
            note += f" · credits used ${used['amount_minor'] / 10 ** exponent:.2f}"
        return {"limits": limits, "note": note}

    @staticmethod
    def credentials():
        home = os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
        try:
            oauth = json.loads((Path(home) / ".credentials.json").read_text()).get("claudeAiOauth")
        except (OSError, ValueError, AttributeError):
            oauth = None
        if not isinstance(oauth, dict):
            raise Failure("No Claude Code login found")
        return oauth


def retry_after(error):
    try:
        return min(max(float(error.headers.get("Retry-After")), 0), 6 * 3600) or None
    except (TypeError, ValueError):
        return None


def limit_label(kind):
    kind = kind if isinstance(kind, str) else ""
    return {"session": "5-hour session", "weekly_all": "Weekly (all models)"}.get(
        kind, kind.replace("_", " ").title())


def budget_window(label, used, budget, resets_at=None):
    percent = min(100, used / budget * 100) if budget > 0 else -1
    detail = f"{fmt_tokens(used)} / {fmt_tokens(budget)}" if budget > 0 else f"{fmt_tokens(used)} tokens"
    return limit(label, percent, resets_at, detail)


# ---- Omarchy usage records


class OmarchyUsage:
    """The records `omarchy-agent-usage-update` keeps, one per agent.

    Omarchy's Agents widget runs that command itself, so it only runs here when
    nothing has refreshed the records for AGENT_TTL. That check is shared on
    disk because every bar runs its own backend. It runs in the background, and
    the next snapshot picks up what it wrote.
    """

    def __init__(self):
        self.folder = state_home() / "omarchy" / "agents" / "usage"
        self.stamp = cache_home() / "tokengauge" / "agents-updated"
        self.command = shutil.which("omarchy-agent-usage-update")
        self.process = None

    def read(self):
        records = {}
        try:
            paths = list(self.folder.glob("*.json"))
        except OSError:
            return records
        for path in paths:
            try:
                record = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(record, dict) and record.get("schemaVersion") == 1 and record.get("id") == path.stem \
                    and valid_id(path.stem):
                records[path.stem] = record
        return records

    def newest(self):
        try:
            return max((path.stat().st_mtime for path in self.folder.glob("*.json")), default=0)
        except OSError:
            return 0

    def update(self, skip, now):
        if not self.command or (self.process and self.process.poll() is None):
            return
        try:
            self.stamp.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(self.stamp.with_suffix(".lock"), "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                try:
                    started = float(self.stamp.read_text())
                except (OSError, ValueError):
                    started = 0
                if now - max(started, self.newest()) < AGENT_TTL:
                    return
                self.stamp.write_text(str(now))
            command = [self.command]
            for agent in sorted(skip):
                command += ["--except", agent]
            self.process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                            stderr=subprocess.DEVNULL)
        except OSError:
            pass


def record_limits(record, now):
    """Limit windows from a record. Records hold them as fractions, not percents."""
    entries = record.get("limits")
    limits = []
    for entry in entries if isinstance(entries, list) else []:
        percent = entry.get("percent") if isinstance(entry, dict) else None
        if not isinstance(percent, (int, float)) or isinstance(percent, bool) or percent < 0:
            continue
        label = entry.get("title") or entry.get("label")
        label = label[:40] if isinstance(label, str) and label else "Limit"
        resets = parse_time(entry.get("resetsAt"))
        expired = resets is not None and resets <= now
        limits.append(limit(label, 0 if expired else percent * 100, None if expired else resets,
                            "reset" if expired else None))
    return limits


def record_note(record, now):
    parts = []
    plan = record.get("tierLabel")
    if isinstance(plan, str) and plan:
        parts.append(f"Plan: {plan[:40]}")
    fetched = as_int(record.get("limitsFetchedAt")) / 1000
    if fetched and (record.get("limitsStale") or now - fetched > 3600):
        parts.append(f"limits as of {clock(fetched)}")
    for key in ("usageStatusText", "authHelpText"):
        text = record.get(key)
        if isinstance(text, str) and text:
            parts.append(text[:160])
            break
    return " · ".join(parts) or None


def uses_record(record):
    """Whether Omarchy tracks this agent here. An agent the machine doesn't use
    still gets a record, an empty one."""
    return bool(record) and record.get("ready") is True


def record_agent(record, now):
    """A provider built from a record alone. Its token counts come as one total
    that includes cache reads, so there's no input/output breakdown to show."""
    today = date.fromtimestamp(now)
    days = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    entries = record.get("recentDays")
    per_day = {entry.get("date"): as_int(entry.get("messageCount"))
               for entry in (entries if isinstance(entries, list) else []) if isinstance(entry, dict)}
    daily = [{"day": day, "tokens": per_day.get(day, 0)} for day in days]

    def totals(tokens):
        return Tokens(input=tokens).json()

    models = record.get("todayTokensByModel")
    models = models if isinstance(models, dict) and per_day.get(days[-1]) else {}
    ranked = sorted(((str(name), as_int(tokens)) for name, tokens in models.items()), key=lambda item: -item[1])
    return {
        "id": record["id"],
        "name": clean_name(record.get("name"), record["id"]),
        "today": totals(per_day.get(days[-1], 0)),
        "week": totals(sum(entry["tokens"] for entry in daily)),
        "daily": daily,
        "limits": record_limits(record, now),
        "topModels": [{"name": name, "tokens": tokens} for name, tokens in ranked[:4]],
        "note": record_note(record, now),
        "error": None,
        "breakdown": False,
    }


class HiddenAgents:
    """Agents switched off in the panel. Kept in state, not the cache, so a
    cache cleanup doesn't bring them back."""

    def __init__(self):
        self.path = state_home() / "tokengauge" / "hidden.json"

    def load(self):
        try:
            value = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return set()
        return {agent for agent in value if valid_id(agent)} if isinstance(value, list) else set()

    def set(self, agent, hidden):
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with open(self.path.with_suffix(".lock"), "w") as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                agents = self.load()
                (agents.add if hidden else agents.discard)(agent)
                temp = self.path.with_suffix(".tmp")
                temp.write_text(json.dumps(sorted(agents)))
                os.replace(temp, self.path)
        except OSError:
            pass


def claude_code(options, cache, account, record=None):
    home = os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude"
    root = Path(home) / "projects"
    if not root.is_dir():
        return failed("claudeCode", "Claude Code", "No ~/.claude/projects directory")

    now = time.time()
    # The same message can be logged in several session files.
    events, seen = [], set()
    for parsed in cache.scan(root, now - 8 * DAY):
        for event in parsed:
            if event.key is None or event.key not in seen:
                seen.add(event.key)
                events.append(event)
    events.sort(key=lambda event: event.ts)

    snap = snapshot("claudeCode", "Claude Code", events, now)

    # A 5-hour block starts at the hour of the first message after the previous block ended.
    block_start, block_tokens = None, 0
    for event in events:
        if block_start is not None and event.ts < block_start + BLOCK_SECONDS:
            block_tokens += event.tokens.billable
        else:
            hour = datetime.fromtimestamp(event.ts).replace(minute=0, second=0, microsecond=0)
            block_start, block_tokens = hour.timestamp(), event.tokens.billable
    if block_start is not None and now < block_start + BLOCK_SECONDS:
        block = budget_window("5-hour session", block_tokens, options.block_budget, block_start + BLOCK_SECONDS)
    else:
        block = budget_window("5-hour session", 0, options.block_budget)
    rolling = sum(event.tokens.billable for event in events if event.ts >= now - 7 * DAY)
    snap["limits"] = [block, budget_window("Rolling 7 days", rolling, options.weekly_budget)]

    # Omarchy already asks for these, so asking too would only spend the
    # endpoint's rate limit. Its record says what's wrong if it has none.
    if uses_record(record):
        limits = record_limits(record, now)
        snap["note"] = record_note(record, now)
        if limits:
            snap["limits"] = limits
            return snap
    elif options.account_limits:
        try:
            limits, note = account.fetch()
            if limits:
                snap["limits"], snap["note"] = limits, note
                return snap
        except Failure as error:
            snap["note"] = f"{error}. Showing local token counts."
            return snap
    if not options.block_budget and not options.weekly_budget and not snap["note"]:
        snap["note"] = "Set blockBudget and weeklyBudget to see % gauges."
    return snap


# ---- Codex


def parse_codex(path):
    """Events plus the newest rate-limit snapshot in a rollout file."""
    events, model, rate = [], "codex", None
    last = Tokens()
    for obj in jsonl(path, b'"type"'):
        payload = obj.get("payload")
        if not isinstance(payload, dict):
            continue
        if obj.get("type") == "turn_context" and isinstance(payload.get("model"), str):
            model = payload["model"]
            continue
        ts = parse_time(obj.get("timestamp"))
        if payload.get("type") != "token_count" or ts is None:
            continue

        limits = payload.get("rate_limits")
        if isinstance(limits, dict) and isinstance(limits.get("primary"), dict):
            rate = (ts, limits)

        # total_token_usage is cumulative per session, so count only the change.
        info = payload.get("info")
        total = info.get("total_token_usage") if isinstance(info, dict) else None
        if not isinstance(total, dict):
            continue
        cached = as_int(total.get("cached_input_tokens"))
        current = Tokens(as_int(total.get("input_tokens")) - cached, as_int(total.get("output_tokens")),
                         cached, as_int(total.get("cache_write_input_tokens")))
        delta = Tokens(current.input - last.input, current.output - last.output,
                       current.cache_read - last.cache_read, current.cache_write - last.cache_write)
        last = current
        if delta.total > 0:
            events.append(Event(ts, model, delta, None))
    return events, rate


def codex(cache, record=None):
    home = os.environ.get("CODEX_HOME") or Path.home() / ".codex"
    root = Path(home) / "sessions"
    if not root.is_dir():
        return failed("codex", "Codex", "No ~/.codex/sessions directory")

    now = time.time()
    # Look back further than a week so a weekly-limit snapshot from an older session is still found.
    parsed = cache.scan(root, now - 10 * DAY)
    snap = snapshot("codex", "Codex", [event for events, _ in parsed for event in events], now)

    # Omarchy asks Codex directly, so its limits are newer than the logs' last session.
    limits = record_limits(record, now) if uses_record(record) else []
    if limits:
        snap["limits"], snap["note"] = limits, record_note(record, now)
        return snap

    rates = [rate for _, rate in parsed if rate]
    if not rates:
        snap["note"] = "No rate-limit snapshot in recent sessions."
        return snap

    taken, limits = max(rates, key=lambda rate: rate[0])
    for key, label in (("primary", "5-hour"), ("secondary", "Weekly")):
        window = limits.get(key)
        if not isinstance(window, dict):
            continue
        resets = window.get("resets_at")
        resets = resets if isinstance(resets, (int, float)) else None
        expired = resets is not None and resets <= now
        minutes = as_int(window.get("window_minutes"))
        name = f"{minutes // 60}h window" if minutes > 0 and minutes not in (300, 10080) else label
        percent = window.get("used_percent")
        percent = percent if isinstance(percent, (int, float)) else 0
        snap["limits"].append(limit(name, 0 if expired else percent, None if expired else resets,
                                    "reset since last session" if expired else None))
    plan = limits.get("plan_type")
    if isinstance(plan, str) and plan:
        snap["note"] = f"Plan: {plan.capitalize()} · limits as of {clock(taken)}"
    return snap


# ---- Admin APIs


def keyring(account):
    try:
        result = subprocess.run(["secret-tool", "lookup", "service", KEYRING_SERVICE, "account", account],
                                capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return ""
    return result.stdout.strip() if result.returncode == 0 else ""


def get_json(url, headers):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, **headers})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = json.loads(response.read(MAX_RESPONSE))
    except urllib.error.HTTPError as error:
        raise Failure(f"HTTP {error.code}: {error.read(140).decode('utf-8', 'replace')}") from None
    except (OSError, ValueError):
        raise Failure("Couldn't reach the usage API") from None
    if not isinstance(body, dict):
        raise Failure("The usage API returned an unexpected response")
    return body


def paged(base, params, headers):
    """Every bucket across the pages of a usage report."""
    page = None
    for _ in range(MAX_PAGES):
        query = params + ([("page", page)] if page else [])
        body = get_json(f"{base}?{urllib.parse.urlencode(query)}", headers)
        data = body.get("data")
        yield from (bucket for bucket in data if isinstance(bucket, dict)) if isinstance(data, list) else ()
        page = body.get("next_page") if body.get("has_more") is True else None
        if not page:
            return


def results(bucket):
    rows = bucket.get("results")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def week_start():
    return local_midnight(date.today() - timedelta(days=6))


def anthropic_api():
    key = keyring("anthropic-admin")
    if not key:
        raise Failure("Add an Admin API key to the keyring (see the README).")
    start = datetime.fromtimestamp(week_start(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    params = [("starting_at", start), ("bucket_width", "1d"), ("group_by[]", "model"), ("limit", "8")]
    events = []
    for bucket in paged("https://api.anthropic.com/v1/organizations/usage_report/messages", params,
                        {"x-api-key": key, "anthropic-version": "2023-06-01"}):
        ts = parse_time(bucket.get("starting_at"))
        if ts is None:
            continue
        for row in results(bucket):
            creation = row.get("cache_creation")
            creation = creation if isinstance(creation, dict) else {}
            tokens = Tokens(as_int(row.get("uncached_input_tokens")), as_int(row.get("output_tokens")),
                            as_int(row.get("cache_read_input_tokens")),
                            as_int(creation.get("ephemeral_5m_input_tokens"))
                            + as_int(creation.get("ephemeral_1h_input_tokens")))
            events.append(Event(ts, str(row.get("model") or "other"), tokens, None))
    snap = snapshot("anthropicAPI", "Anthropic API", events, time.time())
    snap["note"] = "Org-wide usage · may lag a few minutes"
    return snap


def openai_api():
    key = keyring("openai-admin")
    if not key:
        raise Failure("Add an Admin API key to the keyring (see the README).")
    params = [("start_time", str(int(week_start()))), ("bucket_width", "1d"),
              ("group_by", "model"), ("limit", "8")]
    events = []
    for bucket in paged("https://api.openai.com/v1/organization/usage/completions", params,
                        {"Authorization": f"Bearer {key}"}):
        ts = bucket.get("start_time")
        if not isinstance(ts, (int, float)):
            continue
        for row in results(bucket):
            cached = as_int(row.get("input_cached_tokens"))
            tokens = Tokens(as_int(row.get("input_tokens")) - cached, as_int(row.get("output_tokens")), cached)
            events.append(Event(ts, str(row.get("model") or "other"), tokens, None))
    snap = snapshot("openAIAPI", "OpenAI API", events, time.time())
    snap["note"] = "Org-wide completions usage"
    return snap


class ApiCache:
    """Holds an Admin API result for a few minutes between refreshes."""

    def __init__(self, provider, name, fetch):
        self.provider, self.name, self.fetch = provider, name, fetch
        self.value, self.expires = None, 0

    def get(self):
        now = time.time()
        if now >= self.expires:
            try:
                self.value = self.fetch()
            except Failure as error:
                self.value = failed(self.provider, self.name, error)
            self.expires = now + API_TTL
        return self.value


# ---- Output


class Collector:
    def __init__(self, options):
        self.options = options
        self.claude_cache = FileCache(parse_claude)
        self.codex_cache = FileCache(parse_codex)
        self.account = ClaudeAccount()
        self.anthropic = ApiCache("anthropicAPI", "Anthropic API", anthropic_api)
        self.openai = ApiCache("openAIAPI", "OpenAI API", openai_api)
        self.usage = OmarchyUsage()
        self.hidden = HiddenAgents()

    def collect(self):
        options, providers, agents = self.options, [], []
        now, hidden = time.time(), self.hidden.load()
        records = {}
        if options.omarchy:
            off = {RECORD_IDS.get(agent, agent) for agent in hidden}
            off |= set() if options.claude and options.account_limits else {"claude"}
            off |= set() if options.codex else {"codex"}
            self.usage.update(off, now)
            records = self.usage.read()

        def offer(agent, name, build):
            agents.append({"id": agent, "name": name, "hidden": agent in hidden})
            if agent not in hidden:
                providers.append(build())

        if options.claude:
            offer("claudeCode", "Claude Code",
                  lambda: claude_code(options, self.claude_cache, self.account,
                                      records.get("claude") if options.account_limits else None))
        if options.codex:
            offer("codex", "Codex", lambda: codex(self.codex_cache, records.get("codex")))
        for agent, record in sorted(records.items()):
            if agent not in RECORD_IDS.values() and uses_record(record):
                offer(agent, clean_name(record.get("name"), agent), lambda r=record: record_agent(r, now))
        if options.anthropic_api:
            providers.append(self.anthropic.get())
        if options.openai_api:
            providers.append(self.openai.get())
        return {"schemaVersion": 1, "updated": now, "providers": providers, "agents": agents}

    def command(self, data):
        """Act on lines from stdin. Anything but hide and show is just a refresh."""
        for line in data.decode("utf-8", "replace").splitlines():
            parts = line.split()
            if len(parts) == 2 and parts[0] in ("hide", "show") and valid_id(parts[1]):
                self.hidden.set(parts[1], parts[0] == "hide")


def watch(collector, interval):
    while True:
        print(json.dumps(collector.collect(), separators=(",", ":")), flush=True)
        ready, _, _ = select.select([0], [], [], interval)
        # The shell holds stdin open, so EOF means it's gone and we should be too.
        if ready:
            data = os.read(0, 4096)
            if not data:
                return
            collector.command(data)


def main():
    parser = argparse.ArgumentParser(description="AI token usage and plan limits as JSON.")
    parser.add_argument("--watch", action="store_true", help="keep running and print a JSON line per refresh")
    parser.add_argument("--interval", type=float, default=60, help="seconds between refreshes (minimum 15)")
    parser.add_argument("--no-claude", dest="claude", action="store_false")
    parser.add_argument("--no-codex", dest="codex", action="store_false")
    parser.add_argument("--no-account-limits", dest="account_limits", action="store_false",
                        help="without Omarchy's records, don't use the Claude login")
    parser.add_argument("--no-omarchy", dest="omarchy", action="store_false",
                        help="don't use Omarchy's usage records or run its collectors")
    parser.add_argument("--anthropic-api", action="store_true")
    parser.add_argument("--openai-api", action="store_true")
    parser.add_argument("--block-budget", type=int, default=0, help="5-hour token budget for local gauges")
    parser.add_argument("--weekly-budget", type=int, default=0, help="rolling 7-day token budget for local gauges")
    options = parser.parse_args()

    collector = Collector(options)
    try:
        if options.watch:
            os.nice(10)
            watch(collector, max(15, options.interval))
        else:
            print(json.dumps(collector.collect(), indent=2))
    except (BrokenPipeError, KeyboardInterrupt):
        pass


if __name__ == "__main__":
    main()
