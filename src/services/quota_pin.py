"""Pinned quota line in the admin chat: `📊 35% 5ч. сброс 13:00, 83% нед.`, refreshed in the background.

The bot sends one silent message, pins it without a notification and later only edits it (and only when
the text changes), so the chat header always shows the current subscription limits. The message id is kept
in the bot state; if the user deletes or unpins the message, the next refresh sends and pins a new one.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError

from src.services.quota import QuotaResult, QuotaWindow
from src.services.state import StateStore
from src.utils.text import esc_fit

log = logging.getLogger(__name__)

# A window that has just been reset is fetched again shortly after `resets_at`.
RESET_GRACE_SEC = 15.0


def _hhmm(ts: float) -> str:
    return datetime.fromtimestamp(ts).astimezone().strftime("%H:%M")


def _percent(window: QuotaWindow | None, now: float) -> str:
    if window is None:
        return "?"
    if window.resets_at is not None and window.resets_at <= now:
        return "0%"  # the window has rolled over since the snapshot
    return f"{window.utilization:.0f}%"


def pin_text(result: QuotaResult | None, now: float | None = None) -> str:
    """`📊 35% 5ч. сброс 13:00, 83% нед.` (+ `⚠️ на 12:05` when the live request failed)."""
    now = time.time() if now is None else now
    snap = result.snapshot if result else None
    if snap is None:
        reason = result.error if result and result.error else "нет данных"
        return f"📊 Лимиты: недоступны ({esc_fit(reason, 120)})"
    session = snap.session
    five = f"{_percent(session, now)} 5ч."
    if session is not None and session.resets_at is not None and session.resets_at > now:
        five += f" сброс {_hhmm(session.resets_at)}"
    text = f"📊 {five}, {_percent(snap.week, now)} нед."
    if not result or not result.live:
        text += f" ⚠️ на {_hhmm(snap.fetched_at)}"
    return text


def next_delay(result: QuotaResult | None, interval: float, now: float) -> float:
    """The regular interval, shortened so a 5-hour or weekly reset shows up right after it happens."""
    delay = interval
    snap = result.snapshot if result else None
    if snap is not None:
        for window in (snap.session, snap.week):
            if window is not None and window.resets_at is not None and window.resets_at > now:
                delay = min(delay, window.resets_at - now + RESET_GRACE_SEC)
    return delay


QuotaGetter = Callable[[], Awaitable[QuotaResult | None]]


class QuotaPin:
    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        store: StateStore,
        get_quota: QuotaGetter,
        interval_sec: float,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._store = store
        self._get_quota = get_quota
        self._interval = interval_sec
        self._clock = clock
        self._last_text: str | None = None
        self._lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    @property
    def enabled(self) -> bool:
        return self._store.state.quota_pin_enabled

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> None:
        if self.enabled and not self.running:
            self._task = asyncio.create_task(self._loop(), name="quota-pin")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self._task
        self._task = None

    def poke(self) -> None:
        """Refresh soon (after a Claude run or /usage) instead of waiting for the next tick."""
        self._wake.set()

    async def _loop(self) -> None:
        while True:
            result = await self.refresh()
            delay = next_delay(result, self._interval, self._clock())
            self._wake.clear()
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=delay)

    async def refresh(self) -> QuotaResult | None:
        """Fetch the quotas and update the pinned message; never raises (the loop must keep running)."""
        try:
            result = await self._get_quota()
        except Exception as exc:  # noqa: BLE001 - a broken quota source must not kill the loop
            log.warning("Quota pin: quota request failed: %s", exc)
            return None
        if result is None or not self.enabled:
            return result
        if result.snapshot is None and self._store.state.quota_pin_message_id is None:
            return result  # never pin a bare error (e.g. Claude Code is not logged in yet)
        try:
            await self.publish(pin_text(result, self._clock()))
        except TelegramNetworkError as exc:
            log.info("Quota pin not updated (network): %s", exc)
        except TelegramAPIError as exc:
            log.warning("Quota pin not updated: %s", exc)
        return result

    async def publish(self, text: str) -> None:
        async with self._lock:
            message_id = self._store.state.quota_pin_message_id
            if message_id is not None:
                if text == self._last_text:
                    return
                if await self._edit(message_id, text):
                    self._last_text = text
                    return
            await self._send_and_pin(text)

    async def _edit(self, message_id: int, text: str) -> bool:
        """True if the pinned message now shows `text`; False if it is gone and must be sent again."""
        try:
            await self._bot.edit_message_text(text=text, chat_id=self._chat_id, message_id=message_id)
        except TelegramBadRequest as exc:
            if "message is not modified" in str(exc):
                return True
            log.info("Pinned quota message %s cannot be edited (%s): sending a new one", message_id, exc)
            return False
        return True

    async def _send_and_pin(self, text: str) -> None:
        message = await self._bot.send_message(self._chat_id, text, disable_notification=True)
        await self._store.update(quota_pin_message_id=message.message_id)
        self._last_text = text
        await self._bot.pin_chat_message(self._chat_id, message.message_id, disable_notification=True)
        log.info("Quota line pinned as message %s", message.message_id)

    async def enable(self) -> None:
        """Turn the pin on; the loop refreshes right away (and re-sends the message if it was deleted)."""
        await self._store.update(quota_pin_enabled=True)
        self._last_text = None
        if self.running:
            self.poke()
        else:
            self.start()

    async def disable(self) -> None:
        """Stop refreshing, unpin and delete the message."""
        await self.stop()
        message_id = self._store.state.quota_pin_message_id
        await self._store.update(quota_pin_enabled=False, quota_pin_message_id=None)
        self._last_text = None
        if message_id is None:
            return
        for action in (
            self._bot.unpin_chat_message(self._chat_id, message_id=message_id),
            self._bot.delete_message(self._chat_id, message_id),
        ):
            try:
                await action
            except TelegramAPIError as exc:
                log.info("Quota pin cleanup: %s", exc)

