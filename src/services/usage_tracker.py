"""Usage, cost and prompt-cache analytics over local Claude Code transcripts (`~/.claude/projects`).

Data sources, as written by Claude Code 2.1.304:
  * `assistant` records: one record per content block, all blocks of one API response share `message.id`
    and repeat its `usage` (input / output / cache_read / cache_creation split into ephemeral_1h and
    ephemeral_5m). Deduplicated by message id, also across files: `--fork-session` copies the history.
  * `cost-state`: cumulative per session (`totalCostUSD`, `totalAPIDuration`, `totalDuration`,
    `totalLinesAdded/Removed`, `modelUsage`), written when a CLI run ends. It also bills calls that never
    reach the transcript (compaction, the auto-mode classifier), so it is used as a floor for the session
    cost: requests up to the last cost-state are scaled to it, requests after it are priced from `usage`.
  * `toolUseResult.structuredPatch` of Edit / Write / MultiEdit results: lines added / removed.
  * `<session>/subagents/*.jsonl`: subagent transcripts, same `sessionId`, `isSidechain: true`.

Files are append-only, so every file is parsed incrementally from the last complete line, keyed by
mtime + size. A repeated refresh only stats the files and reads what was appended since.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, tzinfo
from pathlib import Path

from src.services.pricing import SYNTHETIC_MODEL, normalize_model_id, request_cost
from src.services.session_parser import SESSION_ID_RE
from src.utils.winpath import long_path, normcase

log = logging.getLogger(__name__)

HEAVY_CONTEXT_TOKENS = 150_000
TTL_1H = 3600
TTL_5M = 300
# A request is a cache miss when it re-writes at least half of the previous prompt instead of reading it.
_MISS_MIN_PROMPT = 10_000
_MISS_SHARE = 0.5
# cost-state calibration is capped: a larger gap means the transcript is incomplete, not CLI overhead.
_MAX_CALIBRATION = 3.0
_SKIP_PREFIX = b'{"type":"api-request'
_HEAD = 700  # record type and flags are near the start of a line; tool results are huge


@dataclass(slots=True)
class Tokens:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0

    @property
    def cache_write(self) -> int:
        return self.cache_write_5m + self.cache_write_1h

    @property
    def prompt(self) -> int:
        """Context sent with the request: fresh input + cache read + cache write."""
        return self.input + self.cache_read + self.cache_write

    @property
    def total(self) -> int:
        return self.prompt + self.output

    def add(self, other: "Tokens") -> None:
        self.input += other.input
        self.output += other.output
        self.cache_read += other.cache_read
        self.cache_write_5m += other.cache_write_5m
        self.cache_write_1h += other.cache_write_1h

    @classmethod
    def from_usage(cls, usage: dict) -> "Tokens":
        def num(value: object) -> int:
            try:
                return max(0, int(value or 0))  # type: ignore[call-overload]
            except (TypeError, ValueError):
                return 0

        split = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
        w1h = num(split.get("ephemeral_1h_input_tokens"))
        w5m = num(split.get("ephemeral_5m_input_tokens"))
        total_write = num(usage.get("cache_creation_input_tokens"))
        if w1h + w5m < total_write:  # older records without the TTL split: priced as 5m writes, like the CLI
            w5m += total_write - w1h - w5m
        return cls(num(usage.get("input_tokens")), num(usage.get("output_tokens")),
                   num(usage.get("cache_read_input_tokens")), w5m, w1h)

    def merge_max(self, other: "Tokens") -> None:
        """Blocks of one response repeat the usage (the last one is final): keep the maximum."""
        self.input = max(self.input, other.input)
        self.output = max(self.output, other.output)
        self.cache_read = max(self.cache_read, other.cache_read)
        self.cache_write_5m = max(self.cache_write_5m, other.cache_write_5m)
        self.cache_write_1h = max(self.cache_write_1h, other.cache_write_1h)


@dataclass(slots=True)
class ApiRequest:
    key: str
    ts: float
    model: str
    tokens: Tokens
    session_id: str
    sidechain: bool
    cwd: str | None = None
    cost: float = 0.0          # priced from usage
    adjusted: float = 0.0      # after cost-state calibration (used everywhere for $)
    priced: bool = True        # False: the model is missing from the price catalog


@dataclass(frozen=True, slots=True)
class CostSnapshot:
    ts: float
    total_usd: float
    api_ms: int
    wall_ms: int
    lines_added: int
    lines_removed: int


@dataclass(frozen=True, slots=True)
class CompactEvent:
    ts: float
    pre_tokens: int
    post_tokens: int


@dataclass(slots=True)
class _FileState:
    path: Path
    is_subagent: bool
    mtime: float = 0.0
    size: int = 0
    offset: int = 0
    session_id: str | None = None
    cwd: str | None = None
    title: str | None = None
    first_ts: float | None = None
    last_ts: float | None = None
    requests: dict[str, ApiRequest] = field(default_factory=dict)
    cost_states: list[CostSnapshot] = field(default_factory=list)
    compacts: list[CompactEvent] = field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0


@dataclass(slots=True)
class SessionAgg:
    session_id: str
    cwd: str | None = None
    title: str | None = None
    requests: list[ApiRequest] = field(default_factory=list)
    cost_state: CostSnapshot | None = None
    compacts: list[CompactEvent] = field(default_factory=list)
    lines_added: int = 0
    lines_removed: int = 0
    first_ts: float | None = None
    last_ts: float | None = None

    @property
    def cost(self) -> float:
        return sum(r.adjusted for r in self.requests)


@dataclass(slots=True)
class PeriodStats:
    cost: float = 0.0
    requests: int = 0
    sessions: int = 0
    tokens: Tokens = field(default_factory=Tokens)


@dataclass(frozen=True, slots=True)
class DayStats:
    day: date
    cost: float
    requests: int
    tokens: Tokens


@dataclass(frozen=True, slots=True)
class TopItem:
    key: str
    label: str
    cost: float
    requests: int


@dataclass(frozen=True, slots=True)
class Diagnostics:
    requests: int
    heavy_requests: int
    heavy_cost_share: float
    cost: float
    model_shares: list[tuple[str, float]]
    threshold: int

    @property
    def heavy_share(self) -> float:
        return self.heavy_requests / self.requests if self.requests else 0.0


@dataclass(frozen=True, slots=True)
class UsageSummary:
    generated_at: float
    today: PeriodStats
    yesterday: PeriodStats
    week: PeriodStats
    month: PeriodStats
    all_time: PeriodStats
    days: list[DayStats]            # oldest first, 30 entries ending today
    diagnostics: Diagnostics
    unpriced_models: list[str]

    def last_days(self, n: int) -> list[DayStats]:
        return self.days[-n:]


@dataclass(frozen=True, slots=True)
class ModelUsage:
    model: str
    tokens: Tokens
    cost: float
    requests: int


@dataclass(frozen=True, slots=True)
class CacheMiss:
    ts: float
    gap_sec: float
    recached_tokens: int
    cause: str


@dataclass(frozen=True, slots=True)
class CacheStats:
    requests: int
    hit_share: float
    misses: list[CacheMiss]
    ttl_sec: int
    last_activity: float | None
    idle_sec: float | None
    context_tokens: int        # prompt of the last request (what the next one re-sends)
    rewrite_tokens: int        # prompt + output of the last request: re-written if the cache is cold

    @property
    def warm(self) -> bool:
        return self.idle_sec is not None and self.idle_sec < self.ttl_sec

    @property
    def ttl_left_sec(self) -> float:
        return max(0.0, self.ttl_sec - (self.idle_sec or 0.0))


@dataclass(frozen=True, slots=True)
class SessionView:
    session_id: str
    title: str | None
    cwd: str | None
    cost: float
    cli_cost: float | None
    api_ms: int | None
    wall_ms: int | None
    lines_added: int
    lines_removed: int
    models: list[ModelUsage]
    cache: CacheStats
    compacts: list[CompactEvent]
    requests: int


# ---------- parsing (worker thread) ----------

def _ts(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _patch_lines(result: dict) -> tuple[int, int]:
    added = removed = 0
    if result.get("type") == "create" and isinstance(result.get("content"), str):
        content = result["content"]
        added += content.count("\n") + (0 if content.endswith("\n") or not content else 1)
    for hunk in result.get("structuredPatch") or []:
        if not isinstance(hunk, dict):
            continue
        for line in hunk.get("lines") or []:
            if isinstance(line, str):
                if line.startswith("+"):
                    added += 1
                elif line.startswith("-"):
                    removed += 1
    return added, removed


def _interesting(line: bytes) -> bool:
    head = line[:_HEAD]
    return (
        b'"assistant"' in head or b'"cost-state"' in head or b'"ai-title"' in head
        or b'compact_boundary' in head or b'"structuredPatch"' in line
    )


def _parse_lines(state: _FileState, data: bytes) -> None:
    for raw in data.split(b"\n"):
        if not raw or raw.startswith(_SKIP_PREFIX) or not _interesting(raw):
            continue
        try:
            rec = json.loads(raw)
        except ValueError:
            continue
        if isinstance(rec, dict):
            _apply_record(state, rec)


def _apply_record(state: _FileState, rec: dict) -> None:
    rtype = rec.get("type")
    ts = _ts(rec.get("timestamp"))
    if ts is not None:
        state.first_ts = ts if state.first_ts is None else min(state.first_ts, ts)
        state.last_ts = ts if state.last_ts is None else max(state.last_ts, ts)
    sid = rec.get("sessionId")
    if isinstance(sid, str) and SESSION_ID_RE.match(sid) and state.session_id is None:
        state.session_id = sid
    if isinstance(rec.get("cwd"), str) and rec["cwd"]:
        state.cwd = rec["cwd"]
    if rtype == "assistant":
        message = rec.get("message") if isinstance(rec.get("message"), dict) else {}
        usage = message.get("usage")
        if not isinstance(usage, dict) or ts is None:
            return
        key = str(message.get("id") or rec.get("uuid") or "")
        if not key:
            return
        model = str(message.get("model") or "")
        tokens = Tokens.from_usage(usage)
        known = state.requests.get(key)
        if known is not None:
            known.tokens.merge_max(tokens)
            return
        state.requests[key] = ApiRequest(
            key=key, ts=ts, model=model, tokens=tokens,
            session_id=str(sid or state.session_id or ""),
            sidechain=bool(rec.get("isSidechain")) or state.is_subagent,
            cwd=rec.get("cwd") if isinstance(rec.get("cwd"), str) else None,
        )
    elif rtype == "cost-state":
        try:
            snap = CostSnapshot(
                ts=state.last_ts or 0.0,
                total_usd=float(rec.get("totalCostUSD") or 0.0),
                api_ms=int(rec.get("totalAPIDuration") or 0),
                wall_ms=int(rec.get("totalDuration") or 0),
                lines_added=int(rec.get("totalLinesAdded") or 0),
                lines_removed=int(rec.get("totalLinesRemoved") or 0),
            )
        except (TypeError, ValueError):
            return
        state.cost_states.append(snap)
    elif rtype == "ai-title" and isinstance(rec.get("aiTitle"), str):
        state.title = rec["aiTitle"]
    elif rtype == "system" and rec.get("subtype") == "compact_boundary":
        meta = rec.get("compactMetadata") if isinstance(rec.get("compactMetadata"), dict) else {}
        state.compacts.append(CompactEvent(
            ts=ts or 0.0, pre_tokens=int(meta.get("preTokens") or 0), post_tokens=int(meta.get("postTokens") or 0),
        ))
    elif rtype == "user" and isinstance(rec.get("toolUseResult"), dict):
        added, removed = _patch_lines(rec["toolUseResult"])
        state.lines_added += added
        state.lines_removed += removed


def _read_from(path: Path, offset: int, size: int) -> bytes:
    with open(long_path(path), "rb") as fh:
        fh.seek(offset)
        return fh.read(max(0, size - offset))


def _price(req: ApiRequest) -> None:
    cost = request_cost(req.model, req.tokens.input, req.tokens.output, req.tokens.cache_read,
                        req.tokens.cache_write_5m, req.tokens.cache_write_1h)
    req.priced = cost is not None
    req.cost = req.adjusted = cost or 0.0


def model_family(model: str) -> str:
    canonical = normalize_model_id(model)
    for family in ("opus", "sonnet", "haiku", "fable", "mythos"):
        if family in canonical:
            return family.capitalize()
    return canonical or "?"


class UsageTracker:
    def __init__(
        self,
        projects_dir: Path,
        heavy_context_tokens: int = HEAVY_CONTEXT_TOKENS,
        clock: Callable[[], float] = time.time,
        tz: tzinfo | None = None,
    ) -> None:
        self.projects_dir = projects_dir
        self.heavy_context_tokens = heavy_context_tokens
        self._clock = clock
        self._tz = tz
        self._files: dict[str, _FileState] = {}
        self._sessions: dict[str, SessionAgg] = {}
        self._requests: list[ApiRequest] = []
        self._lock = asyncio.Lock()
        self.last_refresh_sec = 0.0

    # ---------- refresh ----------

    def _discover(self) -> list[tuple[Path, bool, float, int]]:
        found: list[tuple[Path, bool, float, int]] = []

        def add(path: Path, sub: bool) -> None:
            try:
                st = os.stat(long_path(path))
            except OSError:
                return
            found.append((path, sub, st.st_mtime, st.st_size))

        try:
            projects = [e for e in os.scandir(long_path(self.projects_dir)) if e.is_dir(follow_symlinks=False)]
        except OSError as exc:
            log.warning("Cannot read %s: %s", self.projects_dir, exc)
            return found
        for project in projects:
            pdir = self.projects_dir / project.name
            try:
                entries = list(os.scandir(long_path(pdir)))
            except OSError:
                continue
            for entry in entries:
                if entry.name.endswith(".jsonl") and SESSION_ID_RE.match(entry.name[:-6]):
                    add(pdir / entry.name, False)
                elif SESSION_ID_RE.match(entry.name) and entry.is_dir(follow_symlinks=False):
                    sub_dir = pdir / entry.name / "subagents"
                    try:
                        subs = [s.name for s in os.scandir(long_path(sub_dir)) if s.name.endswith(".jsonl")]
                    except OSError:
                        continue
                    for name in subs:
                        add(sub_dir / name, True)
        return found

    def _refresh_sync(self) -> bool:
        changed = False
        seen: set[str] = set()
        for path, sub, mtime, size in self._discover():
            key = normcase(path)
            seen.add(key)
            state = self._files.get(key)
            if state is not None and state.mtime == mtime and state.size == size:
                continue
            if state is None or size < state.offset:
                state = _FileState(path=path, is_subagent=sub)
                if not sub:
                    state.session_id = path.stem
                self._files[key] = state
            try:
                data = _read_from(path, state.offset, size)
            except OSError as exc:
                log.debug("Cannot read %s: %s", path, exc)
                continue
            cut = data.rfind(b"\n")  # the CLI may be writing the last line right now
            if cut >= 0:
                _parse_lines(state, data[: cut + 1])
                state.offset += cut + 1
            state.mtime, state.size = mtime, size
            changed = True
        for key in set(self._files) - seen:
            del self._files[key]
            changed = True
        if changed or not self._sessions:
            self._rebuild()
        return changed

    def _rebuild(self) -> None:
        sessions: dict[str, SessionAgg] = {}
        seen_keys: set[str] = set()
        # Main transcripts first, oldest first: a forked copy of a message stays with the original chat.
        ordered = sorted(self._files.values(), key=lambda f: (f.is_subagent, f.first_ts or 0.0))
        for f in ordered:
            sid = f.session_id or f.path.stem
            agg = sessions.setdefault(sid, SessionAgg(session_id=sid))
            if not f.is_subagent:
                agg.cwd = f.cwd or agg.cwd
                agg.title = f.title or agg.title
                agg.compacts.extend(f.compacts)
                if f.cost_states:
                    last = f.cost_states[-1]
                    if agg.cost_state is None or last.total_usd >= agg.cost_state.total_usd:
                        agg.cost_state = last
            agg.lines_added += f.lines_added
            agg.lines_removed += f.lines_removed
            for ts in (f.first_ts, f.last_ts):
                if ts is not None:
                    agg.first_ts = ts if agg.first_ts is None else min(agg.first_ts, ts)
                    agg.last_ts = ts if agg.last_ts is None else max(agg.last_ts, ts)
            for req in f.requests.values():
                if req.key in seen_keys:
                    continue
                seen_keys.add(req.key)
                req.session_id = sid
                req.cwd = req.cwd or agg.cwd
                _price(req)
                agg.requests.append(req)
        requests: list[ApiRequest] = []
        for agg in sessions.values():
            agg.requests.sort(key=lambda r: r.ts)
            self._calibrate(agg)
            if agg.cost_state is not None:
                agg.lines_added = max(agg.lines_added, agg.cost_state.lines_added)
                agg.lines_removed = max(agg.lines_removed, agg.cost_state.lines_removed)
            requests.extend(agg.requests)
        requests.sort(key=lambda r: r.ts)
        self._sessions = {sid: agg for sid, agg in sessions.items() if agg.requests or agg.cost_state}
        self._requests = requests

    @staticmethod
    def _calibrate(agg: SessionAgg) -> None:
        """Scale requests up to the last cost-state so their sum equals the CLI's own total."""
        snap = agg.cost_state
        if snap is None or snap.total_usd <= 0:
            return
        before = [r for r in agg.requests if r.ts <= snap.ts + 1.0]
        priced = sum(r.cost for r in before)
        if priced <= 0:
            return
        factor = min(_MAX_CALIBRATION, snap.total_usd / priced)
        if factor > 1.0:
            for req in before:
                req.adjusted = req.cost * factor

    async def refresh(self) -> None:
        async with self._lock:
            started = time.perf_counter()
            await asyncio.to_thread(self._refresh_sync)
            self.last_refresh_sec = time.perf_counter() - started

    # ---------- queries ----------

    def _local(self, ts: float) -> datetime:
        return datetime.fromtimestamp(ts, tz=self._tz) if self._tz else datetime.fromtimestamp(ts).astimezone()

    def _period(self, requests: list[ApiRequest]) -> PeriodStats:
        stats = PeriodStats()
        sessions: set[str] = set()
        for req in requests:
            stats.cost += req.adjusted
            stats.requests += 1
            stats.tokens.add(req.tokens)
            sessions.add(req.session_id)
        stats.sessions = len(sessions)
        return stats

    def _since(self, start: datetime, end: datetime | None = None) -> list[ApiRequest]:
        lo = start.timestamp()
        hi = end.timestamp() if end is not None else float("inf")
        return [r for r in self._requests if lo <= r.ts < hi]

    def _midnight(self, now: datetime, days_back: int = 0) -> datetime:
        day = now.date() - timedelta(days=days_back)
        naive = datetime(day.year, day.month, day.day)
        if self._tz is not None:
            return naive.replace(tzinfo=self._tz)
        return naive.astimezone()

    def summary(self) -> UsageSummary:
        now_ts = self._clock()
        now = self._local(now_ts)
        today0 = self._midnight(now)
        days: list[DayStats] = []
        for back in range(29, -1, -1):
            start = self._midnight(now, back)
            end = self._midnight(now, back - 1) if back else None
            period = self._period(self._since(start, end))
            days.append(DayStats(start.date(), period.cost, period.requests, period.tokens))
        unpriced = sorted({normalize_model_id(r.model) for r in self._requests if not r.priced})
        return UsageSummary(
            generated_at=now_ts,
            today=self._period(self._since(today0)),
            yesterday=self._period(self._since(self._midnight(now, 1), today0)),
            week=self._period(self._since(self._midnight(now, 6))),
            month=self._period(self._since(self._midnight(now, 29))),
            all_time=self._period(self._requests),
            days=days,
            diagnostics=self.diagnostics(now_ts),
            unpriced_models=unpriced,
        )

    def diagnostics(self, now_ts: float | None = None, hours: float = 24.0) -> Diagnostics:
        now_ts = self._clock() if now_ts is None else now_ts
        recent = [r for r in self._requests if r.ts >= now_ts - hours * 3600 and r.model != SYNTHETIC_MODEL]
        heavy = [r for r in recent if r.tokens.prompt > self.heavy_context_tokens]
        cost = sum(r.adjusted for r in recent)
        by_family: dict[str, float] = defaultdict(float)
        for req in recent:
            by_family[model_family(req.model)] += req.adjusted
        shares = sorted(((name, value / cost) for name, value in by_family.items() if cost > 0),
                        key=lambda item: item[1], reverse=True)
        return Diagnostics(
            requests=len(recent),
            heavy_requests=len(heavy),
            heavy_cost_share=(sum(r.adjusted for r in heavy) / cost) if cost > 0 else 0.0,
            cost=cost,
            model_shares=shares,
            threshold=self.heavy_context_tokens,
        )

    def top(self, days: int, by: str, limit: int = 5) -> list[TopItem]:
        """Most expensive projects (`by="project"`, grouped by cwd) or chats (`by="session"`)."""
        now = self._local(self._clock())
        recent = self._since(self._midnight(now, days - 1))
        groups: dict[str, list[ApiRequest]] = defaultdict(list)
        labels: dict[str, str] = {}
        for req in recent:
            if by == "project":
                cwd = req.cwd or (self._sessions.get(req.session_id).cwd if req.session_id in self._sessions else None)
                key = normcase(cwd) if cwd else "?"
                labels.setdefault(key, cwd or "?")
            else:
                key = req.session_id
                agg = self._sessions.get(key)
                labels.setdefault(key, (agg.title if agg and agg.title else key[:8]))
            groups[key].append(req)
        items = [
            TopItem(key, labels[key], sum(r.adjusted for r in reqs), len(reqs)) for key, reqs in groups.items()
        ]
        items.sort(key=lambda item: item.cost, reverse=True)
        return items[:limit]

    def latest_session_id(self) -> str | None:
        main = [agg for agg in self._sessions.values() if agg.requests and any(not r.sidechain for r in agg.requests)]
        if not main:
            return None
        return max(main, key=lambda agg: max(r.ts for r in agg.requests)).session_id

    def has_session(self, session_id: str | None) -> bool:
        return bool(session_id) and session_id in self._sessions

    def session_view(self, session_id: str) -> SessionView | None:
        agg = self._sessions.get(session_id)
        if agg is None:
            return None
        per_model: dict[str, list[ApiRequest]] = defaultdict(list)
        for req in agg.requests:
            if req.model != SYNTHETIC_MODEL:
                per_model[normalize_model_id(req.model) or "?"].append(req)
        models = []
        for model, reqs in per_model.items():
            tokens = Tokens()
            for req in reqs:
                tokens.add(req.tokens)
            models.append(ModelUsage(model, tokens, sum(r.adjusted for r in reqs), len(reqs)))
        models.sort(key=lambda m: m.cost, reverse=True)
        snap = agg.cost_state
        wall = snap.wall_ms if snap else None
        if agg.first_ts is not None and agg.last_ts is not None:
            wall = max(wall or 0, int((agg.last_ts - agg.first_ts) * 1000))
        return SessionView(
            session_id=session_id,
            title=agg.title,
            cwd=agg.cwd,
            cost=agg.cost,
            cli_cost=snap.total_usd if snap else None,
            api_ms=snap.api_ms if snap else None,
            wall_ms=wall,
            lines_added=agg.lines_added,
            lines_removed=agg.lines_removed,
            models=models,
            cache=self.cache_stats(agg),
            compacts=list(agg.compacts),
            requests=len(agg.requests),
        )

    def cache_stats(self, agg: SessionAgg) -> CacheStats:
        main = [r for r in agg.requests if not r.sidechain and r.model != SYNTHETIC_MODEL]
        prompt = sum(r.tokens.prompt for r in main)
        read = sum(r.tokens.cache_read for r in main)
        if any(r.tokens.cache_write_1h for r in main):
            ttl = TTL_1H
        elif any(r.tokens.cache_write_5m for r in main):
            ttl = TTL_5M
        else:
            ttl = TTL_1H
        misses: list[CacheMiss] = []
        for prev, cur in zip(main, main[1:]):
            prev_prompt = prev.tokens.prompt
            if prev_prompt < _MISS_MIN_PROMPT:
                continue
            if cur.tokens.cache_read >= _MISS_SHARE * prev_prompt:
                continue
            if cur.tokens.cache_write < _MISS_SHARE * prev_prompt:
                continue  # the context shrank (/compact, new topic): not a re-cache
            gap = cur.ts - prev.ts
            if normalize_model_id(cur.model) != normalize_model_id(prev.model):
                cause = "смена модели"
            elif gap >= ttl:
                cause = f"простой дольше {'1h' if ttl == TTL_1H else '5m'} TTL"
            else:
                cause = "кэш сброшен (изменились инструменты или системный промпт)"
            misses.append(CacheMiss(cur.ts, gap, cur.tokens.cache_write, cause))
        last = main[-1] if main else None
        now = self._clock()
        context = last.tokens.prompt if last else 0
        rewrite = (last.tokens.prompt + last.tokens.output) if last else 0
        compacted = [c for c in agg.compacts if last is None or c.ts > last.ts]
        if compacted and compacted[-1].post_tokens:
            # /compact after the last request: the next one sends only the summary.
            context = rewrite = compacted[-1].post_tokens
        return CacheStats(
            requests=len(main),
            hit_share=read / prompt if prompt else 0.0,
            misses=misses,
            ttl_sec=ttl,
            last_activity=last.ts if last else None,
            idle_sec=(now - last.ts) if last else None,
            context_tokens=context,
            rewrite_tokens=rewrite,
        )
