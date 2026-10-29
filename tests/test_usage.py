"""UsageTracker, quota client, dashboard rendering and the /usage flow (Telegram API and network mocked)."""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from conftest import write_jsonl

from src.keyboards.inline import BTN_USAGE, UsageCb
from src.services.pricing import normalize_model_id, request_cost
from src.services.reasoning import ReasoningEffort
from src.services.quota import QuotaClient, QuotaResult, parse_usage_response
from src.services.state import RunMode
from src.services.usage_report import (
    fit_message,
    fmt_tokens,
    progress_bar,
    render_dashboard,
    render_days,
    status_line,
)
from src.services.usage_tracker import TTL_1H, TTL_5M, Tokens, UsageTracker
from src.utils.winpath import IS_WINDOWS

TZ = timezone(timedelta(hours=3))
NOW_DT = datetime(2026, 10, 29, 12, 0, tzinfo=TZ)
NOW = NOW_DT.timestamp()
S1 = "aaaaaaaa-1111-4111-8111-111111111111"
S2 = "bbbbbbbb-2222-4222-8222-222222222222"
OPUS = "claude-opus-5-5"


def at(day: int, hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, tzinfo=TZ)


def iso(when: datetime) -> str:
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def assistant(mid: str, when: datetime, sid: str = S1, *, inp: int = 0, out: int = 0, read: int = 0,
              w1h: int = 0, w5m: int = 0, model: str = OPUS, side: bool = False, cwd: str = r"C:\proj",
              block: str = "text") -> dict:
    return {
        "type": "assistant", "sessionId": sid, "isSidechain": side, "cwd": cwd, "timestamp": iso(when),
        "uuid": f"u-{mid}-{block}",
        "message": {"id": mid, "model": model, "role": "assistant", "content": [{"type": block}],
                    "usage": {"input_tokens": inp, "output_tokens": out, "cache_read_input_tokens": read,
                              "cache_creation_input_tokens": w1h + w5m, "service_tier": "standard",
                              "cache_creation": {"ephemeral_1h_input_tokens": w1h, "ephemeral_5m_input_tokens": w5m}}},
    }


def cost_state(sid: str, total: float, added: int = 0, removed: int = 0) -> dict:
    return {"type": "cost-state", "sessionId": sid, "totalCostUSD": total, "totalAPIDuration": 3_320_000,
            "totalDuration": 92_100_000, "totalLinesAdded": added, "totalLinesRemoved": removed,
            "startTime": 0, "modelUsage": {}, "hasUnknownModelCost": False}


def patch_result(when: datetime, sid: str = S1, added: int = 2, removed: int = 1, create: str | None = None) -> dict:
    hunk = {"oldStart": 1, "oldLines": removed + 1, "newStart": 1, "newLines": added + 1,
            "lines": [" ctx", *(f"-old{i}" for i in range(removed)), *(f"+new{i}" for i in range(added))]}
    result = {"type": "create", "filePath": "a.py", "content": create, "structuredPatch": []} if create is not None \
        else {"filePath": "a.py", "oldString": "x", "newString": "y", "structuredPatch": [hunk], "originalFile": "x"}
    return {"type": "user", "sessionId": sid, "timestamp": iso(when), "cwd": r"C:\proj",
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t"}]},
            "toolUseResult": result}


def opus_cost(inp: int = 0, out: int = 0, read: int = 0, w1h: int = 0, w5m: int = 0) -> float:
    return (inp * 4 + out * 20 + read * 0.2 + w1h * 8 + w5m * 5) / 1_000_000


@pytest.fixture
def projects(tmp_path: Path) -> Path:
    path = tmp_path / "projects"
    path.mkdir()
    return path


def make_tracker(projects: Path, now: float = NOW) -> UsageTracker:
    return UsageTracker(projects, clock=lambda: now, tz=TZ)


def refreshed(tracker: UsageTracker) -> UsageTracker:
    asyncio.run(tracker.refresh())
    return tracker


# ---------- pricing ----------

def test_pricing_matches_cli_catalog() -> None:
    assert normalize_model_id("claude-fable-5.1") == "claude-fable-5-1"
    assert normalize_model_id("us.anthropic.claude-opus-5-5-20261001-v1:0") == "claude-opus-5-5"
    assert normalize_model_id("claude-opus-5-5[1m]") == "claude-opus-5-5"
    # Opus 5.5: $4 in / $20 out / $5 5m write / $8 1h write / $0.20 read per MTok.
    assert request_cost(OPUS, 1_000_000, 0, 0, 0, 0) == pytest.approx(4.0)
    assert request_cost(OPUS, 0, 1_000_000, 1_000_000, 1_000_000, 1_000_000) == pytest.approx(20 + 0.2 + 5 + 8)
    assert request_cost("claude-opus-5-5-20261001", 0, 1_000_000, 0, 0, 0) == pytest.approx(20)  # not opus-5
    assert request_cost("claude-fable-5.1", 0, 0, 1_000_000, 0, 0) == pytest.approx(0.25)
    # Haiku 5.5 long-prompt tier above 100k prompt tokens.
    assert request_cost("claude-haiku-5-5", 50_000, 0, 0, 0, 0) == pytest.approx(0.005)
    assert request_cost("claude-haiku-5-5", 200_000, 0, 0, 0, 0) == pytest.approx(0.1)
    assert request_cost("<synthetic>", 10, 10, 0, 0, 0) == 0.0
    assert request_cost("gpt-9", 10, 10, 0, 0, 0) is None


# ---------- parsing ----------

def test_tokens_dedup_blobs_and_synthetic(projects: Path) -> None:
    write_jsonl(projects / "C--proj" / f"{S1}.jsonl", [
        assistant("m1", at(29, 10), inp=1000, out=10, w1h=100_000, block="thinking"),
        '{"type":"api-request-blob","sessionId":"x","message":{"usage":{"input_tokens":999999}}}',
        assistant("m1", at(29, 10), inp=1000, out=1000, w1h=100_000, block="text"),  # same response, final usage
        assistant("m2", at(29, 10, 5), model="<synthetic>"),
        {"type": "ai-title", "sessionId": S1, "aiTitle": "Почини <тесты> & CI"},
        "not json at all",
    ])
    t = refreshed(make_tracker(projects))
    view = t.session_view(S1)
    assert view is not None and view.title == "Почини <тесты> & CI"
    assert view.requests == 2
    [model] = view.models  # synthetic messages are not a model
    assert model.model == OPUS and model.requests == 1
    assert (model.tokens.input, model.tokens.output, model.tokens.cache_write_1h) == (1000, 1000, 100_000)
    assert view.cost == pytest.approx(opus_cost(inp=1000, out=1000, w1h=100_000))


def test_cost_state_calibration_and_live_tail(projects: Path) -> None:
    base = opus_cost(out=100_000)
    write_jsonl(projects / "C--proj" / f"{S1}.jsonl", [
        assistant("m1", at(29, 9), out=50_000),
        assistant("m2", at(29, 9, 30), out=50_000),
        cost_state(S1, base * 1.5),  # the CLI billed compaction / classifier calls on top
        assistant("m3", at(29, 11), out=10_000),  # after the last cost-state: priced from usage only
    ])
    t = refreshed(make_tracker(projects))
    view = t.session_view(S1)
    assert view.cli_cost == pytest.approx(base * 1.5)
    assert view.cost == pytest.approx(base * 1.5 + opus_cost(out=10_000))
    assert t.summary().today.cost == pytest.approx(view.cost)
    assert (view.api_ms, view.wall_ms) == (3_320_000, 92_100_000)


def test_fork_copies_and_subagents(projects: Path) -> None:
    project = projects / "C--proj"
    write_jsonl(project / f"{S1}.jsonl", [assistant("m1", at(29, 9), out=1000), assistant("m2", at(29, 9, 1), out=1000)])
    # A --fork-session copy repeats m1/m2 and adds its own m3.
    write_jsonl(project / f"{S2}.jsonl", [
        assistant("m1", at(29, 9), sid=S2, out=1000), assistant("m2", at(29, 9, 1), sid=S2, out=1000),
        assistant("m3", at(29, 10), sid=S2, out=1000),
    ])
    write_jsonl(project / S1 / "subagents" / "agent-1.jsonl", [assistant("s1", at(29, 9, 2), out=1000, side=True)])
    t = refreshed(make_tracker(projects))
    s = t.summary()
    assert s.today.requests == 4 and s.today.sessions == 2
    assert s.today.cost == pytest.approx(opus_cost(out=4000))
    assert t.session_view(S1).requests == 3 and t.session_view(S2).requests == 1
    assert t.session_view(S1).cache.requests == 2  # the subagent (sidechain) is not the main prompt cache


def test_incremental_refresh(projects: Path) -> None:
    path = projects / "C--proj" / f"{S1}.jsonl"
    write_jsonl(path, [assistant("m1", at(29, 9), out=1000)])
    t = refreshed(make_tracker(projects))
    assert t.summary().today.requests == 1
    asyncio.run(t.refresh())  # nothing changed: only a stat
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(assistant("m2", at(29, 10), out=1000)) + "\n")
        fh.write(json.dumps(assistant("m3", at(29, 10), out=1000))[:40])  # the CLI is still writing this line
    asyncio.run(t.refresh())
    assert t.summary().today.requests == 2
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(assistant("m3", at(29, 10), out=1000))[40:] + "\n")
    asyncio.run(t.refresh())
    assert t.summary().today.requests == 3
    write_jsonl(path, [assistant("x1", at(29, 11), out=1000)])  # rewritten (shorter): parsed again from 0
    asyncio.run(t.refresh())
    assert t.summary().today.requests == 1
    path.unlink()
    asyncio.run(t.refresh())
    assert t.summary().today.requests == 0 and t.latest_session_id() is None


def test_cache_ttl_warm_cold_and_misses(projects: Path) -> None:
    ctx = 300_000
    write_jsonl(projects / "C--proj" / f"{S1}.jsonl", [
        assistant("m1", at(28, 9), inp=10, out=500, w1h=ctx),
        assistant("m2", at(28, 9, 5), inp=10, out=500, read=ctx, w1h=600),
        # 3 h of idle: the 1h cache expired, the whole context is written again.
        assistant("m3", at(28, 12, 5), inp=10, out=700, read=0, w1h=ctx + 1_000),
        # /compact: the context shrinks, a small write is not a miss.
        {"type": "system", "subtype": "compact_boundary", "sessionId": S1, "timestamp": iso(at(28, 12, 6)),
         "compactMetadata": {"trigger": "manual", "preTokens": ctx, "postTokens": 13_000}},
        assistant("m4", at(29, 11, 30), inp=10, out=800, read=0, w1h=13_000),
    ])
    t = refreshed(make_tracker(projects))
    cache = t.session_view(S1).cache
    assert cache.ttl_sec == TTL_1H and cache.requests == 4
    assert len(cache.misses) == 1
    miss = cache.misses[0]
    assert miss.recached_tokens == ctx + 1_000 and "1h TTL" in miss.cause and miss.gap_sec == 3 * 3600
    assert cache.warm and cache.ttl_left_sec == pytest.approx(30 * 60)
    assert cache.context_tokens == 13_010 and cache.rewrite_tokens == 13_810
    total_prompt = (10 + ctx) + (10 + ctx + 600) + (10 + ctx + 1_000) + (10 + 13_000)
    assert cache.hit_share == pytest.approx(ctx / total_prompt)
    assert [(c.pre_tokens, c.post_tokens) for c in t.session_view(S1).compacts] == [(ctx, 13_000)]

    cold = refreshed(make_tracker(projects, now=at(29, 14).timestamp())).session_view(S1).cache
    assert not cold.warm and cold.idle_sec == pytest.approx(2.5 * 3600) and cold.ttl_left_sec == 0


def test_five_minute_ttl_and_model_switch(projects: Path) -> None:
    write_jsonl(projects / "C--proj" / f"{S1}.jsonl", [
        assistant("m1", at(29, 11, 0), out=10, w5m=50_000),
        assistant("m2", at(29, 11, 1), out=10, w5m=50_100, model="claude-sonnet-5-5"),
    ])
    cache = refreshed(make_tracker(projects)).session_view(S1).cache
    assert cache.ttl_sec == TTL_5M and [m.cause for m in cache.misses] == ["смена модели"]
    assert not cache.warm  # 59 min idle > 5 min


def test_heavy_context_diagnostics(projects: Path) -> None:
    write_jsonl(projects / "C--proj" / f"{S1}.jsonl", [
        assistant("old", at(27, 9), out=1000, read=500_000),  # older than 24h: not in diagnostics
        assistant("h1", at(29, 9), out=1000, read=160_000),
        assistant("h2", at(29, 10), out=1000, read=149_000, w1h=2_000),  # 151k > 150k
        assistant("l1", at(29, 11), out=1000, read=10_000),
        assistant("l2", at(29, 11, 30), out=1000, read=1_000, model="claude-sonnet-5-5"),
    ])
    d = refreshed(make_tracker(projects)).diagnostics()
    assert (d.requests, d.heavy_requests) == (4, 2) and d.heavy_share == 0.5
    heavy = opus_cost(out=1000, read=160_000) + opus_cost(out=1000, read=149_000, w1h=2_000)
    total = heavy + opus_cost(out=1000, read=10_000) + request_cost("claude-sonnet-5-5", 0, 1000, 1000, 0, 0)
    assert d.heavy_cost_share == pytest.approx(heavy / total)
    assert [name for name, _ in d.model_shares] == ["Opus", "Sonnet"]
    assert sum(share for _, share in d.model_shares) == pytest.approx(1.0)


def test_daily_aggregation_uses_local_midnight(projects: Path) -> None:
    write_jsonl(projects / "C--proj" / f"{S1}.jsonl", [
        assistant("a", at(28, 23, 30), out=100_000),   # 20:30Z: yesterday in UTC+3
        assistant("b", at(29, 0, 30), out=200_000),    # 21:30Z on the 28th, but today locally
        assistant("c", at(23, 12), out=50_000),        # 6 days ago: still in the 7-day window
        assistant("d", at(22, 12), out=50_000),        # 7 days ago: only in 30 days
    ])
    write_jsonl(projects / "C--other" / f"{S2}.jsonl", [assistant("e", datetime(2026, 9, 20, 12, tzinfo=TZ), sid=S2, out=10_000, cwd=r"C:\other")])
    s = refreshed(make_tracker(projects)).summary()
    assert s.today.cost == pytest.approx(4.0) and s.today.requests == 1
    assert s.yesterday.cost == pytest.approx(2.0)
    assert s.week.cost == pytest.approx(7.0) and s.week.requests == 3
    assert s.month.cost == pytest.approx(8.0) and s.all_time.cost == pytest.approx(8.2)
    assert len(s.days) == 30 and s.days[-1].day == date(2026, 10, 29) and s.days[0].day == date(2026, 9, 30)
    assert [d.cost for d in s.last_days(2)] == pytest.approx([2.0, 4.0])
    text = render_days(s, 7)
    assert "Чт 29.10 ▰▰▰▰▰▰▰▰▰▰" in text and "Ср 28.10 ▰▰▰▰▰▱▱▱▱▱" in text and "$4.00" in text and "Итого: <b>$7.00</b>" in text
    assert len(render_days(s, 30)) < 4000


def test_top_projects_and_chats(projects: Path) -> None:
    write_jsonl(projects / "C--proj" / f"{S1}.jsonl", [
        assistant("a", at(29, 9), out=100_000), {"type": "ai-title", "sessionId": S1, "aiTitle": "Дорогой чат"},
    ])
    write_jsonl(projects / "C--other" / f"{S2}.jsonl", [assistant("b", at(29, 9), sid=S2, out=10_000, cwd=r"C:\other")])
    t = refreshed(make_tracker(projects))
    assert [(i.label, round(i.cost, 2)) for i in t.top(7, "project")] == [(r"C:\proj", 2.0), (r"C:\other", 0.2)]
    assert [i.label for i in t.top(7, "session")] == ["Дорогой чат", S2[:8]]
    assert t.latest_session_id() in (S1, S2)


def test_lines_from_patches_and_cost_state(projects: Path) -> None:
    write_jsonl(projects / "C--proj" / f"{S1}.jsonl", [
        assistant("a", at(29, 9), out=10),
        patch_result(at(29, 9, 1), added=3, removed=2),
        patch_result(at(29, 9, 2), create="line1\nline2\nline3"),
    ])
    write_jsonl(projects / "C--proj" / f"{S2}.jsonl", [
        assistant("b", at(29, 9), sid=S2, out=10), patch_result(at(29, 9, 1), sid=S2, added=1, removed=0),
        cost_state(S2, 0.0001, added=40, removed=5),
    ])
    t = refreshed(make_tracker(projects))
    v1, v2 = t.session_view(S1), t.session_view(S2)
    assert (v1.lines_added, v1.lines_removed) == (6, 2)
    assert (v2.lines_added, v2.lines_removed) == (40, 5)  # the CLI counter wins when it is larger


def test_tracker_is_fast_on_repeat(projects: Path) -> None:
    records = [assistant(f"m{i}", at(29, 9) + timedelta(seconds=i), out=100, read=1000) for i in range(3000)]
    write_jsonl(projects / "C--proj" / f"{S1}.jsonl", records)
    t = refreshed(make_tracker(projects))
    asyncio.run(t.refresh())
    assert t.last_refresh_sec < 0.1
    assert t.summary().today.requests == 3000


# ---------- quota ----------

USAGE_JSON = {
    "five_hour": {"utilization": 99.0, "resets_at": "2026-10-29T17:00:00.1+00:00"},
    "seven_day": {"utilization": 85.0, "resets_at": "2026-10-29T21:00:00+00:00"},
    "seven_day_opus": {"utilization": 40.0, "resets_at": None},
    "seven_day_sonnet": None,
}


def _credentials(path: Path, expires_ms: float) -> None:
    path.write_text(json.dumps({"claudeAiOauth": {"accessToken": "sk-ant-oat-SECRET", "expiresAt": expires_ms,
                                                  "subscriptionType": "pro"}}), encoding="utf-8")


def test_parse_usage_response() -> None:
    snap = parse_usage_response(USAGE_JSON, NOW, "pro")
    assert snap.session.utilization == 99.0 and snap.week.utilization == 85.0
    assert snap.week.resets_at == datetime(2026, 10, 29, 21, tzinfo=timezone.utc).timestamp()
    assert set(snap.extra) == {"seven_day_opus"} and snap.extra["seven_day_opus"].resets_at is None


def test_quota_client_live_cache_and_fallbacks(tmp_path: Path) -> None:
    creds, cache = tmp_path / "credentials.json", tmp_path / "data" / "quota_cache.json"
    _credentials(creds, (NOW + 3600) * 1000)
    calls: list[tuple[str, dict]] = []
    responses: list[tuple[int, object]] = [(200, USAGE_JSON), (401, None), (200, USAGE_JSON)]
    clock = [NOW]

    async def fetcher(url: str, headers: dict[str, str]) -> tuple[int, object]:
        calls.append((url, headers))
        return responses.pop(0)

    def client() -> QuotaClient:
        return QuotaClient(creds, cache, "https://api.example", 30, fetcher, clock=lambda: clock[0])

    async def scenario() -> None:
        c = client()
        first = await c.get()
        assert first.live and first.snapshot.week.utilization == 85.0 and first.snapshot.subscription == "pro"
        url, headers = calls[0]
        assert url == "https://api.example/api/oauth/usage" and headers["Authorization"] == "Bearer sk-ant-oat-SECRET"
        assert json.loads(cache.read_text(encoding="utf-8"))["week"]["utilization"] == 85.0
        clock[0] += 10
        assert (await c.get()).live and len(calls) == 1  # throttled: no second request within 30 s
        failed = await c.get(force=True)  # 401
        assert not failed.live and "401" in failed.error and failed.snapshot.week.utilization == 85.0
        assert "SECRET" not in failed.error
        # A fresh process falls back to the snapshot saved on disk.
        _credentials(creds, (clock[0] - 1) * 1000)  # expired token: no request at all
        restarted = client()
        stale = await restarted.get()
        assert not stale.live and "истек" in stale.error and stale.snapshot.session.utilization == 99.0
        assert len(calls) == 2
        creds.unlink()
        missing = await client().get(force=True)
        assert "credentials" in missing.error and missing.snapshot is not None

    asyncio.run(scenario())


# ---------- rendering ----------

def test_formatting_helpers() -> None:
    assert [fmt_tokens(n) for n in (580, 315_300, 40_200_000)] == ["580", "315.3k", "40.2m"]
    assert progress_bar(0) == "░" * 20 and progress_bar(100) == "█" * 20 and progress_bar(150) == "█" * 20
    assert progress_bar(99) == "█" * 19 + "▌" and len(progress_bar(37)) == 20
    long_text = "\n".join(f"<b>строка {i}</b>" for i in range(1000))
    fitted = fit_message(long_text)
    assert len(fitted) <= 4000 and fitted.endswith("…") and fitted.count("<b>") == fitted.count("</b>")


def test_dashboard_text(projects: Path) -> None:
    write_jsonl(projects / "C--proj" / f"{S1}.jsonl", [
        assistant("m1", at(29, 11, 30), out=1000, read=200_000, w1h=1000),
        {"type": "ai-title", "sessionId": S1, "aiTitle": "<script>&"},
    ])
    t = refreshed(make_tracker(projects))
    quota = QuotaResult(parse_usage_response(USAGE_JSON, NOW, "pro"), live=True)
    text = render_dashboard(quota, t.summary(), t.session_view(S1), True, now=NOW)
    assert "<b>99%</b> 🔴" in text and "<b>85%</b> 🟠" in text
    assert "Сброс в 20:00 (через 8ч 0м)" in text or "Сброс в" in text
    assert "&lt;script&gt;&amp;" in text and "<script>" not in text
    assert "🔥 Кэш тёплый" in text and "📏 Текущий контекст чата: 201.0k" in text
    assert "&gt;150.0k" in text and len(text) < 4000
    stale = QuotaResult(quota.snapshot, live=False, error="сеть: ClientConnectorError")
    assert "Снимок от" in render_dashboard(stale, t.summary(), None, False, now=NOW + 600)
    line = status_line(quota, t.summary())
    assert line.startswith("📊 Квоты: 5ч 99% · Нед 85% | 💵 Сегодня: $")
    assert status_line(None, t.summary()).startswith("📊 Квоты: н/д")


# ---------- Telegram flow ----------

def _make_usage_harness(harness_factory, tmp_path: Path):  # type: ignore[no-untyped-def]
    h = harness_factory()
    allowed = str((tmp_path / "allowed").resolve())
    write_jsonl(tmp_path / "projects" / "C--allowed-usage" / f"{S1}.jsonl", [
        assistant("m1", datetime.now(TZ) - timedelta(minutes=10), out=2000, read=180_000, w1h=5000, cwd=allowed),
        {"type": "ai-title", "sessionId": S1, "aiTitle": "Отчет по лимитам"},
    ])
    creds = tmp_path / "credentials.json"
    _credentials(creds, (datetime.now().timestamp() + 3600) * 1000)

    async def fetcher(url: str, headers: dict[str, str]) -> tuple[int, object]:
        return 200, USAGE_JSON

    h.ctx.quota = QuotaClient(creds, tmp_path / "data" / "quota_cache.json", "https://api.example", 30, fetcher)
    return h, allowed


def test_usage_flow(harness_factory, tmp_path: Path) -> None:
    h, allowed = _make_usage_harness(harness_factory, tmp_path)

    async def scenario() -> None:
        await h.ctx.store.load()
        await h.ctx.store.update(active_session_id=S1, active_session_cwd=allowed, run_mode=RunMode.RESUME)

        for command in ("/usage", "/cost", "/stats"):
            await h.text(command)
            msg = next(c for c in h.session.take() if isinstance(c, SendMessage))
            assert "Расход и лимиты Claude" in msg.text and "<b>99%</b>" in msg.text
            assert "«Отчет по лимитам»" in msg.text and "выбранный чат" in msg.text
            buttons = [b.text for row in msg.reply_markup.inline_keyboard for b in row]
            assert buttons == ["🔄 Обновить", "📅 По дням (7д / 30д)", "📁 По проектам и чатам",
                               "🧹 Сжать контекст (/compact)", "🆕 Начать чистую сессию"]

        for action, marker in (("days7", "Расходы по дням: 7 дн."), ("days30", "Расходы по дням: 30 дн."),
                               ("top", "Самые дорогие проекты"), ("refresh", "Расход и лимиты Claude")):
            await h.callback(UsageCb(action=action).pack())
            calls = h.session.take()
            assert not any(isinstance(c, SendMessage) for c in calls)
            assert marker in next(c for c in calls if isinstance(c, EditMessageText)).text

        # /status: compact line + button.
        await h.text("/status")
        msg = next(c for c in h.session.take() if isinstance(c, SendMessage))
        assert "📊 Квоты: 5ч 99% · Нед 85% | 💵 Сегодня: $" in msg.text
        assert BTN_USAGE in [b.text for row in msg.reply_markup.inline_keyboard for b in row]

        # 🆕 clean session: next prompt starts a new chat in the same folder.
        await h.callback(UsageCb(action="newchat").pack())
        calls = h.session.take()
        assert h.ctx.state.run_mode is RunMode.NEW and h.ctx.state.active_cwd == allowed
        assert any(isinstance(c, SendMessage) and "Чистая сессия" in c.text for c in calls)

    asyncio.run(scenario())


@pytest.mark.skipif(not IS_WINDOWS, reason="uses the Windows cmd shim fake CLI")
def test_compact_button_runs_compact_in_session(harness_factory, tmp_path: Path) -> None:
    h, allowed = _make_usage_harness(harness_factory, tmp_path)

    async def scenario() -> None:
        await h.ctx.store.load()
        await h.ctx.store.update(active_session_id=S1, active_session_cwd=allowed, run_mode=RunMode.RESUME,
                                 reasoning_effort=ReasoningEffort.MAX)
        await h.callback(UsageCb(action="compact").pack())
        await h.drain()
        calls = h.session.take()
        answers = [c.text for c in calls if isinstance(c, AnswerCallbackQuery) and c.text]
        assert "🧹 Запускаю /compact" in answers
        edits = [c.text for c in calls if isinstance(c, EditMessageText)]
        assert any("🧹 Контекст сжат: 349 129 → 13 187 токенов" in e for e in edits), edits
        answer = next(c.text for c in calls if isinstance(c, SendMessage) and c.parse_mode is None)
        assert f"--resume {S1}" in answer and "--effort" not in answer  # no ultrathink appended to /compact
        assert h.ctx.state.active_session_id == S1

    asyncio.run(scenario())


def test_compact_refuses_without_session(harness_factory, tmp_path: Path) -> None:
    h = harness_factory()
    for path in (tmp_path / "projects").rglob("*.jsonl"):
        path.unlink()

    async def scenario() -> None:
        await h.ctx.store.load()
        await h.callback(UsageCb(action="compact").pack())
        answers = [c for c in h.session.take() if isinstance(c, AnswerCallbackQuery)]
        assert answers and answers[0].show_alert and "Нет чата" in answers[0].text
        await h.text("/usage")
        msg = next(c for c in h.session.take() if isinstance(c, SendMessage))
        assert "Нет данных" in msg.text and "Не удалось получить" in msg.text  # no credentials in tests
        assert "🧹 Сжать контекст (/compact)" not in [b.text for row in msg.reply_markup.inline_keyboard for b in row]

    asyncio.run(scenario())


def test_tokens_helpers() -> None:
    t = Tokens.from_usage({"input_tokens": 5, "output_tokens": 7, "cache_read_input_tokens": 11,
                           "cache_creation_input_tokens": 20})  # no TTL split: priced as 5m writes
    assert (t.cache_write_5m, t.cache_write_1h, t.prompt, t.total) == (20, 0, 36, 43)


def test_compact_after_last_request_shrinks_context(projects: Path) -> None:
    write_jsonl(projects / "C--proj" / f"{S1}.jsonl", [
        assistant("m1", at(28, 9), out=1_000, read=430_000, w1h=3_000),
        {"type": "system", "subtype": "compact_boundary", "sessionId": S1, "timestamp": iso(at(29, 0)),
         "compactMetadata": {"trigger": "manual", "preTokens": 434_000, "postTokens": 19_000}},
    ])
    cache = refreshed(make_tracker(projects)).session_view(S1).cache
    assert cache.context_tokens == cache.rewrite_tokens == 19_000 and not cache.warm
