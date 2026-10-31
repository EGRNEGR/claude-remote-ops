"""Telegram link recovery: stale DNS / dead pooled connections after a network or VPN reconnect."""

from __future__ import annotations

import asyncio
import json
import socket
from typing import Any

import pytest
from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import GetMe
from aiogram.types import Message
from aiohttp import web
from aiohttp.abc import AbstractResolver, ResolveResult

from src.services.net import PollingWatchdog, ReconnectingSession, probe_telegram, wait_for_telegram

TOKEN = "123456:TEST_TOKEN_abcdefghijklmnopqrstuvwxyz"
HOST = "api.telegram.test"
BOT_USER = {"id": 123456, "is_bot": True, "first_name": "Test", "username": "test_bot"}


class SwitchableResolver(AbstractResolver):
    """DNS that can be repointed, like a TUN VPN in fake-IP mode handing out a new address."""

    def __init__(self, port: int) -> None:
        self.port = port
        self.calls = 0

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> list[ResolveResult]:
        self.calls += 1
        return [ResolveResult(hostname=host, host="127.0.0.1", port=self.port, family=socket.AF_INET,
                              proto=0, flags=socket.AI_NUMERICHOST)]

    async def close(self) -> None:
        return None


class FakeBotApi:
    """Minimal Bot API: getMe, long-polling getUpdates, sendMessage. Several sites share one state."""

    def __init__(self) -> None:
        self.updates: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.sent: list[str] = []
        self._update_id = 100
        self._runners: list[web.AppRunner] = []

    def push_text(self, text: str) -> None:
        self._update_id += 1
        self.updates.put_nowait({
            "update_id": self._update_id,
            "message": {
                "message_id": self._update_id, "date": 0, "text": text,
                "chat": {"id": 42, "type": "private"},
                "from": {"id": 42, "is_bot": False, "first_name": "Admin"},
            },
        })

    async def _handle(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        data = await request.post()
        if method == "getMe":
            result: Any = BOT_USER
        elif method == "getUpdates":
            result = []
            try:
                result.append(await asyncio.wait_for(self.updates.get(), 0.5))
            except TimeoutError:
                pass
        elif method == "sendMessage":
            self.sent.append(str(data["text"]))
            result = {"message_id": len(self.sent), "date": 0, "text": data["text"],
                      "chat": {"id": int(str(data["chat_id"])), "type": "private"}}
        else:
            result = True
        return web.Response(text=json.dumps({"ok": True, "result": result}), content_type="application/json")

    async def serve(self) -> tuple[web.AppRunner, int]:
        app = web.Application()
        app.router.add_post("/bot{token}/{method}", self._handle)
        runner = web.AppRunner(app, shutdown_timeout=0.5)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self._runners.append(runner)
        return runner, runner.addresses[0][1]

    async def close(self) -> None:
        for runner in self._runners:
            await runner.cleanup()


def free_port() -> int:
    """A port nobody listens on: connecting there fails like the old, dead VPN address."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def make_session(cls: type[AiohttpSession], resolver: SwitchableResolver, **kwargs: Any) -> AiohttpSession:
    session = cls(api=TelegramAPIServer.from_base(f"http://{HOST}"), **kwargs)
    session._connector_init["resolver"] = resolver  # same TCPConnector aiogram builds, fake DNS
    return session


def test_plain_aiogram_session_keeps_the_stale_address() -> None:
    """The bug: aiogram caches DNS for an hour, so a changed address is never picked up."""

    async def scenario() -> None:
        api = FakeBotApi()
        _, live_port = await api.serve()
        resolver = SwitchableResolver(free_port())
        bot = Bot(TOKEN, session=make_session(AiohttpSession, resolver))
        try:
            with pytest.raises(TelegramNetworkError):
                await bot(GetMe())
            resolver.port = live_port  # the network is back under a new address
            with pytest.raises(TelegramNetworkError):
                await bot(GetMe())
            assert resolver.calls == 1
        finally:
            await bot.session.close()
            await api.close()

    asyncio.run(scenario())


def test_reconnecting_session_picks_up_the_new_address() -> None:
    async def scenario() -> None:
        api = FakeBotApi()
        _, live_port = await api.serve()
        resolver = SwitchableResolver(free_port())
        restored: list[float] = []
        session = make_session(ReconnectingSession, resolver, reset_after=1, on_restored=restored.append)
        bot = Bot(TOKEN, session=session)
        try:
            with pytest.raises(TelegramNetworkError):
                await bot(GetMe())
            assert session.health.total_resets == 1 and session.health.failures == 1
            resolver.port = live_port
            me = await bot(GetMe())
            assert me.username == "test_bot"
            assert resolver.calls == 2
            assert len(restored) == 1
            assert session.health.failures == 0 and session.health.down_since is None
        finally:
            await bot.session.close()
            await api.close()

    asyncio.run(scenario())


def test_polling_resumes_after_the_old_address_dies() -> None:
    """End to end: real aiogram polling survives the server address changing under it."""

    async def scenario() -> None:
        api = FakeBotApi()
        old_runner, old_port = await api.serve()
        _, new_port = await api.serve()
        resolver = SwitchableResolver(old_port)
        session = make_session(ReconnectingSession, resolver, reset_after=1)
        bot = Bot(TOKEN, session=session)
        dp = Dispatcher()

        @dp.message(F.text)
        async def echo(message: Message) -> None:
            await message.answer(f"pong {message.text}")

        async def wait_sent(text: str, timeout: float) -> None:
            async with asyncio.timeout(timeout):
                while text not in api.sent:
                    await asyncio.sleep(0.05)

        polling = asyncio.create_task(dp.start_polling(bot, polling_timeout=1, handle_signals=False))
        try:
            api.push_text("1")
            await wait_sent("pong 1", 10)
            # VPN reconnect: the old address goes dead (open connections too), DNS now says new_port.
            await old_runner.cleanup()
            resolver.port = new_port
            api.push_text("2")
            await wait_sent("pong 2", 30)
            assert session.health.total_resets >= 1
        finally:
            await dp.stop_polling()
            await polling
            await api.close()

    asyncio.run(scenario())


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def scripted_session(
    monkeypatch: pytest.MonkeyPatch, outcomes: list[Exception | None], **kwargs: Any
) -> tuple[ReconnectingSession, FakeClock, list[int]]:
    """ReconnectingSession whose transport replays `outcomes` (None = success); resets are counted."""
    clock = FakeClock()
    session = ReconnectingSession(clock=clock, **kwargs)
    resets: list[int] = []

    async def fake_request(self: AiohttpSession, bot: Bot, method: Any, timeout: int | None = None) -> Any:
        outcome = outcomes.pop(0)
        if outcome is not None:
            raise outcome
        return True

    monkeypatch.setattr(AiohttpSession, "make_request", fake_request)
    monkeypatch.setattr(ReconnectingSession, "reset", lambda self: _counting_reset(self, resets))
    return session, clock, resets


async def _counting_reset(session: ReconnectingSession, resets: list[int]) -> None:
    session._last_reset = session._clock()
    session.health.resets += 1
    session.health.total_resets += 1
    resets.append(1)


def net_error() -> TelegramNetworkError:
    return TelegramNetworkError(method=GetMe(), message="ClientConnectorError: Cannot connect")


def test_reset_threshold_and_min_interval(monkeypatch: pytest.MonkeyPatch) -> None:
    outcomes: list[Exception | None] = [net_error(), net_error(), net_error(), net_error(), None]
    session, clock, resets = scripted_session(monkeypatch, outcomes, reset_after=2, min_reset_interval=10)
    bot = Bot(TOKEN, session=session)

    async def call() -> None:
        with pytest.raises(TelegramNetworkError):
            await session.make_request(bot, GetMe())

    async def scenario() -> None:
        await call()
        assert resets == []  # a single hiccup (Telegram drops keep-alive hourly) does not reset
        await call()
        assert len(resets) == 1
        clock.now += 5
        await call()
        assert len(resets) == 1  # not more often than min_reset_interval
        clock.now += 6
        await call()
        assert len(resets) == 2
        assert await session.make_request(bot, GetMe()) is True
        assert session.health.failures == 0 and session.health.resets == 0
        assert session.health.total_resets == 2

    asyncio.run(scenario())


def test_telegram_error_answer_means_the_link_works(monkeypatch: pytest.MonkeyPatch) -> None:
    restored: list[float] = []
    bad = TelegramBadRequest(method=GetMe(), message="Bad Request: query is too old")
    session, clock, _ = scripted_session(monkeypatch, [net_error(), bad], on_restored=restored.append)
    bot = Bot(TOKEN, session=session)

    async def scenario() -> None:
        with pytest.raises(TelegramNetworkError):
            await session.make_request(bot, GetMe())
        clock.now += 90
        with pytest.raises(TelegramBadRequest):
            await session.make_request(bot, GetMe())

    asyncio.run(scenario())
    assert restored == [90]
    assert session.health.down_since is None and session.health.last_ok == clock.now


def test_wait_for_telegram_retries_until_it_answers(monkeypatch: pytest.MonkeyPatch) -> None:
    session, _, _ = scripted_session(monkeypatch, [net_error(), net_error(), None], reset_after=5)
    bot = Bot(TOKEN, session=session)
    sleeps: list[float] = []

    async def fake_sleep(delay: float) -> None:
        sleeps.append(delay)

    monkeypatch.setattr("src.services.net.asyncio.sleep", fake_sleep)
    monkeypatch.setattr(Bot, "me", lambda self: session.make_request(self, GetMe()))
    asyncio.run(wait_for_telegram(bot, min_delay=1, max_delay=3))
    assert sleeps == [1, 2]


def test_probe_uses_a_fresh_session() -> None:
    async def probe(port: int) -> bool:
        bot = Bot(TOKEN, session=AiohttpSession(api=TelegramAPIServer.from_base(f"http://127.0.0.1:{port}")))
        try:
            return await probe_telegram(bot, timeout=5)
        finally:
            await bot.session.close()

    async def scenario() -> tuple[bool, bool]:
        api = FakeBotApi()
        _, port = await api.serve()
        try:
            return await probe(port), await probe(free_port())
        finally:
            await api.close()

    alive, dead = asyncio.run(scenario())
    assert alive is True and dead is False


class WatchdogRig:
    def __init__(self, *, probe_ok: bool = True, busy: int = 0, launcher: bool = True) -> None:
        self.clock = FakeClock()
        self.session = ReconnectingSession(clock=self.clock)
        self.probe_ok = probe_ok
        self.busy = busy
        self.probes = 0
        self.restarts = 0
        self.resets = 0

        async def probe() -> bool:
            self.probes += 1
            return self.probe_ok

        async def restart() -> None:
            self.restarts += 1

        async def reset() -> None:
            self.resets += 1

        self.session.reset = reset  # type: ignore[method-assign]
        self.dog = PollingWatchdog(
            self.session, stall_sec=300, probe=probe, busy=lambda: self.busy,
            restart=restart if launcher else None, clock=self.clock,
        )

    def check(self) -> bool:
        return asyncio.run(self.dog.check())


def test_watchdog_quiet_while_polling_succeeds() -> None:
    rig = WatchdogRig()
    rig.clock.now += 299
    assert rig.check() is False and rig.probes == 0


def test_watchdog_waits_out_a_real_outage() -> None:
    rig = WatchdogRig(probe_ok=False)
    rig.clock.now += 3600
    assert rig.check() is False
    assert rig.probes == 1 and rig.restarts == 0 and not rig.dog.triggered


def test_watchdog_restarts_a_stuck_process_but_not_during_claude_runs() -> None:
    rig = WatchdogRig(busy=1)
    rig.clock.now += 301
    assert rig.check() is False and rig.restarts == 0
    rig.busy = 0
    assert rig.check() is True
    assert rig.restarts == 1 and rig.dog.triggered


def test_watchdog_without_launcher_only_resets_the_session() -> None:
    rig = WatchdogRig(launcher=False)
    rig.clock.now += 301
    assert rig.check() is False
    assert rig.resets == 1 and rig.restarts == 0 and not rig.dog.triggered


@pytest.mark.parametrize("stall_sec, enabled", [(0, False), (300, True)])
def test_watchdog_can_be_switched_off(stall_sec: int, enabled: bool) -> None:
    dog = PollingWatchdog(
        ReconnectingSession(), stall_sec=stall_sec, probe=_never, busy=lambda: 0, restart=None,
    )
    assert dog.enabled is enabled


async def _never() -> bool:
    raise AssertionError("probe must not run")

