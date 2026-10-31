"""Telegram link health: a self-healing HTTP session, a startup wait and a stalled-polling watchdog.

aiogram's AiohttpSession keeps one aiohttp ClientSession per process with keep-alive connections and
a 1-hour DNS cache (`ttl_dns_cache=3600`). When the network or a VPN reconnects (TUN clients in
fake-IP mode hand out a new address for api.telegram.org), the cached address and the pooled sockets
stay dead and aiogram's polling loop retries them forever: only a process restart used to help.
ReconnectingSession drops the whole ClientSession (pool + DNS cache) after consecutive network
errors, so the next request resolves and connects from scratch.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.exceptions import TelegramAPIError, TelegramNetworkError
from aiogram.methods import GetMe, TelegramMethod
from aiogram.methods.base import TelegramType

log = logging.getLogger(__name__)

Clock = Callable[[], float]

STARTUP_RETRY_MIN_SEC = 1.0
STARTUP_RETRY_MAX_SEC = 30.0
PROBE_TIMEOUT_SEC = 15


@dataclass
class LinkHealth:
    last_ok: float  # monotonic time of the last call Telegram answered
    down_since: float | None = None  # first network error of the current outage
    failures: int = 0  # network errors in a row
    resets: int = 0  # session resets during the current outage
    total_resets: int = 0


class ReconnectingSession(AiohttpSession):
    """AiohttpSession that recreates its connection pool and DNS cache when Telegram stops answering."""

    def __init__(
        self,
        *,
        reset_after: int = 2,
        min_reset_interval: float = 10.0,
        on_restored: Callable[[float], None] | None = None,
        clock: Clock = time.monotonic,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self.reset_after = max(1, reset_after)
        self.min_reset_interval = min_reset_interval
        # Called with the outage length (seconds) on the first successful call after network errors.
        self.on_restored = on_restored
        self._clock = clock
        self.health = LinkHealth(last_ok=clock())
        self._last_reset = float("-inf")

    async def make_request(
        self, bot: Bot, method: TelegramMethod[TelegramType], timeout: int | None = None
    ) -> TelegramType:
        try:
            result = await super().make_request(bot, method, timeout)
        except TelegramNetworkError:
            await self._on_failure()
            raise
        except TelegramAPIError:
            self._on_success()  # Telegram answered with an error: the link itself works
            raise
        self._on_success()
        return result

    async def reset(self) -> None:
        """Drop pooled connections and the DNS cache: the next request resolves and connects anew."""
        session, self._session = self._session, None
        self._last_reset = self._clock()
        self.health.resets += 1
        self.health.total_resets += 1
        log.warning(
            "Telegram unreachable (%d network errors in a row): recreating the HTTP session (reset #%d)",
            self.health.failures, self.health.resets,
        )
        if session is not None and not session.closed:
            await session.close()

    async def _on_failure(self) -> None:
        health = self.health
        now = self._clock()
        if health.down_since is None:
            health.down_since = now
        health.failures += 1
        if health.failures >= self.reset_after and now - self._last_reset >= self.min_reset_interval:
            await self.reset()

    def _on_success(self) -> None:
        health = self.health
        now = self._clock()
        if health.down_since is not None:
            downtime = now - health.down_since
            log.info(
                "Telegram reachable again after %.0f s (%d network errors, %d session resets)",
                downtime, health.failures, health.resets,
            )
            if self.on_restored is not None:
                self.on_restored(downtime)
        health.last_ok = now
        health.down_since = None
        health.failures = 0
        health.resets = 0


async def wait_for_telegram(
    bot: Bot,
    min_delay: float = STARTUP_RETRY_MIN_SEC,
    max_delay: float = STARTUP_RETRY_MAX_SEC,
) -> None:
    """Retry getMe until Telegram answers: a start without network waits instead of crashing."""
    delay = min_delay
    while True:
        try:
            await bot.me()
            return
        except TelegramNetworkError as exc:
            log.warning("Telegram unreachable at startup (%s), next try in %.0f s", exc, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, max_delay)


async def probe_telegram(bot: Bot, timeout: int = PROBE_TIMEOUT_SEC) -> bool:
    """getMe over a brand-new session (own connections and DNS lookup). True = Telegram answers."""
    session = AiohttpSession(api=bot.session.api)
    try:
        await session.make_request(bot, GetMe(), timeout=timeout)
    except TelegramNetworkError as exc:
        log.info("Telegram probe failed: %s", exc)
        return False
    except TelegramAPIError:
        return True
    finally:
        await session.close()
    return True


class PollingWatchdog:
    """Last resort when the bot stays silent although Telegram answers a fresh connection.

    Every `check_interval` seconds: if no API call succeeded for `stall_sec` (idle long polling
    succeeds every few seconds), probe Telegram over a new connection. A real outage (probe fails)
    is left to ReconnectingSession; a stuck process is restarted through `restart` (start_bot.ps1
    relaunches it) or, without a launcher, gets a fresh HTTP session. Running Claude tasks are never
    killed for this: the restart waits until they finish.
    """

    def __init__(
        self,
        session: ReconnectingSession,
        *,
        stall_sec: float,
        probe: Callable[[], Awaitable[bool]],
        busy: Callable[[], int],
        restart: Callable[[], Awaitable[None]] | None,
        check_interval: float = 30.0,
        clock: Clock = time.monotonic,
    ) -> None:
        self._session = session
        self.stall_sec = stall_sec
        self._probe = probe
        self._busy = busy
        self._restart = restart
        self.check_interval = check_interval
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._deferred = False
        self.triggered = False

    @property
    def enabled(self) -> bool:
        return self.stall_sec > 0

    def start(self) -> None:
        if self.enabled and self._task is None:
            self._task = asyncio.create_task(self._loop(), name="net-watchdog")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        while not self.triggered:
            await asyncio.sleep(self.check_interval)
            try:
                await self.check()
            except Exception:  # noqa: BLE001 - the watchdog must outlive any single failure
                log.exception("Network watchdog check failed")

    async def check(self) -> bool:
        """One watchdog pass. True = the process restart was requested."""
        silent = self._clock() - self._session.health.last_ok
        if silent < self.stall_sec:
            self._deferred = False
            return False
        if not await self._probe():
            return False  # Telegram is really unreachable: a restart would not help
        if self._restart is None:
            log.error("Polling silent for %.0f s while Telegram answers: recreating the HTTP session", silent)
            await self._session.reset()
            return False
        busy = self._busy()
        if busy:
            if not self._deferred:
                log.warning("Polling silent for %.0f s: restart postponed until %d Claude run(s) finish", silent, busy)
                self._deferred = True
            return False
        log.error("Polling silent for %.0f s while Telegram answers: restarting the bot process", silent)
        self.triggered = True
        await self._restart()
        return True
