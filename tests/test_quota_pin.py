"""Pinned quota line: text, refresh schedule, send/pin/edit/re-send, /pin toggle and the state migration."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import DeleteMessage, EditMessageText, PinChatMessage, SendMessage, UnpinChatMessage

from src.services.quota import QuotaResult, QuotaSnapshot, QuotaWindow
from src.services.quota_pin import RESET_GRACE_SEC, QuotaPin, next_delay, pin_text
from src.services.state import STATE_SCHEMA_VERSION, BotState, StateStore
from tg_harness import ADMIN, MockSession

# Naive datetimes are local time, the same clock the pinned line is rendered in.
NOW = datetime(2026, 11, 5, 10, 25).timestamp()
RESET_5H = datetime(2026, 11, 5, 13, 0).timestamp()
RESET_WEEK = datetime(2026, 11, 8, 9, 0).timestamp()


def quota(five: float = 35, week: float = 83, live: bool = True, fetched_at: float = NOW,
          session_reset: float | None = RESET_5H) -> QuotaResult:
    snap = QuotaSnapshot(fetched_at, QuotaWindow(five, session_reset), QuotaWindow(week, RESET_WEEK))
    return QuotaResult(snap, live, None if live else "сеть: ClientConnectorError")


def test_pin_text() -> None:
    assert pin_text(quota(), NOW) == "📊 35% 5ч. сброс 13:00, 83% нед."
    # The live request failed: the cached snapshot is shown with its time.
    stale = quota(live=False, fetched_at=datetime(2026, 11, 5, 10, 5).timestamp())
    assert pin_text(stale, NOW) == "📊 35% 5ч. сброс 13:00, 83% нед. ⚠️ на 10:05"
    # The 5-hour window rolled over after the snapshot; no window opened yet (no reset time).
    assert pin_text(quota(), RESET_5H + 60).startswith("📊 0% 5ч., 83% нед.")
    assert pin_text(quota(five=0, session_reset=None), NOW) == "📊 0% 5ч., 83% нед."
    assert pin_text(QuotaResult(None, False, "нет <токена>"), NOW) == "📊 Лимиты: недоступны (нет &lt;токена&gt;)"
    assert pin_text(None, NOW) == "📊 Лимиты: недоступны (нет данных)"


def test_next_delay_wakes_up_after_a_reset() -> None:
    assert next_delay(quota(), 300, NOW) == 300
    assert next_delay(quota(), 300, RESET_5H - 100) == 100 + RESET_GRACE_SEC
    # Already past the reset (the server has not rolled the window yet): back to the regular interval.
    assert next_delay(quota(), 300, RESET_5H + 1) == 300
    assert next_delay(None, 300, NOW) == 300


class GoneSession(MockSession):
    """Telegram answers "message to edit not found" once the user has deleted the pinned message."""

    def __init__(self) -> None:
        super().__init__()
        self.gone: set[int] = set()

    async def make_request(self, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        if isinstance(method, EditMessageText) and method.message_id in self.gone:
            self.calls.append(method)
            raise TelegramBadRequest(method, "Bad Request: message to edit not found")
        return await super().make_request(bot, method, timeout)


def make_pin(tmp_path: Path, results: list[QuotaResult]) -> tuple[QuotaPin, GoneSession, StateStore]:
    session = GoneSession()
    bot = Bot("123456:TEST_TOKEN_abcdefghijklmnopqrstuvwxyz", session=session)
    store = StateStore(tmp_path / "state.json", BotState())
    feed = iter(results)

    async def get_quota() -> QuotaResult | None:
        return next(feed)

    return QuotaPin(bot, ADMIN, store, get_quota, 300, clock=lambda: NOW), session, store


def test_send_pin_edit_and_resend(tmp_path: Path) -> None:
    pin, session, store = make_pin(tmp_path, [quota(), quota(), quota(five=36), quota(five=37)])

    async def scenario() -> None:
        await store.load()
        # First refresh: one silent message, pinned without a notification, id saved in the state.
        await pin.refresh()
        sent, pinned = session.take()
        assert isinstance(sent, SendMessage) and sent.text == "📊 35% 5ч. сброс 13:00, 83% нед."
        assert sent.disable_notification and isinstance(pinned, PinChatMessage) and pinned.disable_notification
        message_id = store.state.quota_pin_message_id
        assert pinned.message_id == message_id and pinned.chat_id == ADMIN
        assert json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))["quota_pin_message_id"] == message_id

        # Same numbers: no Telegram call at all. New numbers: the pinned message is edited in place.
        await pin.refresh()
        assert session.take() == []
        await pin.refresh()
        (edit,) = session.take()
        assert isinstance(edit, EditMessageText) and edit.message_id == message_id
        assert edit.text == "📊 36% 5ч. сброс 13:00, 83% нед."

        # The user deleted the message: a new one is sent and pinned.
        session.gone.add(message_id)
        await pin.refresh()
        edit, sent, pinned = session.take()
        assert isinstance(edit, EditMessageText) and isinstance(sent, SendMessage) and isinstance(pinned, PinChatMessage)
        assert store.state.quota_pin_message_id == pinned.message_id != message_id

    asyncio.run(scenario())


def test_no_new_pin_without_any_quota_data(tmp_path: Path) -> None:
    missing = QuotaResult(None, False, "нет ~/.claude/.credentials.json")
    pin, session, store = make_pin(tmp_path, [missing, quota(), missing])

    async def scenario() -> None:
        await store.load()
        await pin.refresh()
        assert session.take() == [] and store.state.quota_pin_message_id is None
        await pin.refresh()
        assert [type(c) for c in session.take()] == [SendMessage, PinChatMessage]
        # An existing pin does show the failure instead of keeping stale numbers silently.
        await pin.refresh()
        (edit,) = session.take()
        assert isinstance(edit, EditMessageText) and "недоступны" in edit.text

    asyncio.run(scenario())


def test_disable_unpins_and_deletes(tmp_path: Path) -> None:
    pin, session, store = make_pin(tmp_path, [quota(), quota()])

    async def scenario() -> None:
        await store.load()
        await pin.refresh()
        message_id = store.state.quota_pin_message_id
        session.take()
        await pin.disable()
        unpin, delete = session.take()
        assert isinstance(unpin, UnpinChatMessage) and unpin.message_id == message_id
        assert isinstance(delete, DeleteMessage) and delete.message_id == message_id
        assert not store.state.quota_pin_enabled and store.state.quota_pin_message_id is None
        # Disabled: refreshes no longer touch the chat.
        await pin.refresh()
        assert session.take() == []

    asyncio.run(scenario())


def test_loop_refreshes_on_start_and_on_poke(tmp_path: Path) -> None:
    pin, session, store = make_pin(tmp_path, [quota(), quota(five=50), quota(five=50)])

    async def wait_for(kind: type, count: int) -> None:
        for _ in range(100):
            if sum(isinstance(c, kind) for c in session.calls) >= count:
                return
            await asyncio.sleep(0.01)
        raise AssertionError(f"no {kind.__name__} after poke")

    async def scenario() -> None:
        await store.load()
        pin.start()
        await wait_for(PinChatMessage, 1)
        pin.poke()  # e.g. a Claude run has finished
        await wait_for(EditMessageText, 1)
        assert session.calls[-1].text.startswith("📊 50% 5ч.")
        await pin.stop()
        assert not pin.running

    asyncio.run(scenario())


def test_pin_command_toggles(harness_factory) -> None:  # type: ignore[no-untyped-def]
    h = harness_factory()
    results = iter([quota(), quota()])

    async def get_quota() -> QuotaResult | None:
        return next(results)

    async def scenario() -> None:
        await h.ctx.store.load()
        h.ctx.quota_pin = pin = QuotaPin(h.bot, ADMIN, h.ctx.store, get_quota, 300)
        await h.text("/pin")  # on by default -> off
        assert not h.ctx.state.quota_pin_enabled
        assert "выключен" in h.session.texts()[-1]

        await h.text("/pin on")
        for _ in range(100):
            if h.ctx.state.quota_pin_message_id is not None:
                break
            await asyncio.sleep(0.01)
        assert h.ctx.state.quota_pin_enabled and pin.running
        assert any(isinstance(c, PinChatMessage) for c in h.session.calls)
        assert "Закреп с лимитами включен" in "".join(h.session.texts())

        await h.text("/pin maybe")
        assert "/pin on" in h.session.texts()[-1]
        await pin.stop()

    asyncio.run(scenario())


def test_pin_command_without_quotas(harness_factory) -> None:  # type: ignore[no-untyped-def]
    h = harness_factory(USAGE_QUOTA_ENABLED=False)

    async def scenario() -> None:
        await h.ctx.store.load()
        await h.text("/pin")
        assert "недоступен" in h.session.texts()[-1]

    asyncio.run(scenario())


def test_state_v4_migrates_pin_default(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": 4, "claude_model": "opus"}), encoding="utf-8")

    async def scenario() -> None:
        st = await StateStore(path, BotState(quota_pin_enabled=False)).load()
        assert st.schema_version == STATE_SCHEMA_VERSION == 5
        assert not st.quota_pin_enabled and st.quota_pin_message_id is None and st.claude_model == "opus"

    asyncio.run(scenario())
