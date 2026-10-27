"""Real aiogram Dispatcher + routers + middleware with a mocked Telegram Bot API session."""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import AsyncGenerator
from datetime import datetime
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.base import BaseSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.methods import GetFile, SendDocument, SendMessage, TelegramMethod
from aiogram.types import Audio, CallbackQuery, Chat, Document, File, Message, Update, User, Voice

from src.context import AppContext
from src.handlers import common, files, runner, sessions, voice
from src.middlewares.auth import AuthMiddleware

ADMIN = 42
STRANGER = 666
AUDIO_BYTES = "содержимое с телефона".encode("utf-8")


class MockSession(BaseSession):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[TelegramMethod[Any]] = []
        self._ids = itertools.count(1000)

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        self.calls.append(method)
        if isinstance(method, (SendMessage, SendDocument)):
            # Real aiogram binds API results to the bot, so message.edit_text() works on them.
            return Message(
                message_id=next(self._ids), date=datetime.now(),
                chat=Chat(id=int(method.chat_id), type="private"),
                text=getattr(method, "text", None),
            ).as_(bot)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u1", file_path="voice/file_1.oga")
        return True

    async def stream_content(
        self, url: str, headers: dict[str, Any] | None = None, timeout: int = 30,
        chunk_size: int = 65536, raise_for_status: bool = True,
    ) -> AsyncGenerator[bytes, None]:
        yield AUDIO_BYTES

    async def close(self) -> None:
        return None

    def texts(self, kind: type = SendMessage) -> list[str]:
        return [c.text for c in self.calls if isinstance(c, kind) and getattr(c, "text", None)]

    def take(self) -> list[TelegramMethod[Any]]:
        calls, self.calls = self.calls, []
        return calls


class Harness:
    def __init__(self, ctx: AppContext) -> None:
        self.ctx = ctx
        self.session = MockSession()
        self.bot = Bot("123456:TEST_TOKEN_abcdefghijklmnopqrstuvwxyz", session=self.session,
                       default=DefaultBotProperties(parse_mode=ParseMode.HTML))
        self.dp = Dispatcher(storage=MemoryStorage())
        self.dp["ctx"] = ctx
        self.dp.update.outer_middleware(AuthMiddleware(ADMIN))
        # Same order as main.py. Routers are module singletons: detach them from a previous test Dispatcher.
        routers = (common.router, sessions.router, files.router, voice.router, runner.router)
        for router in routers:
            router._parent_router = None
        self.dp.include_routers(*routers)
        self._ids = itertools.count(1)

    @staticmethod
    def _user(uid: int) -> User:
        return User(id=uid, is_bot=False, first_name="Тест")

    async def _feed_message(self, uid: int, **fields: Any) -> None:
        msg_id = next(self._ids)
        msg = Message(message_id=msg_id, date=datetime.now(), chat=Chat(id=uid, type="private"),
                      from_user=self._user(uid), **fields)
        await self.dp.feed_update(self.bot, Update(update_id=msg_id, message=msg))

    async def text(self, text: str, uid: int = ADMIN) -> None:
        await self._feed_message(uid, text=text)

    async def document(self, name: str, caption: str | None = None) -> None:
        await self._feed_message(
            ADMIN, caption=caption,
            document=Document(file_id="FILE1", file_unique_id="U1", file_name=name, file_size=21),
        )

    async def voice(self, duration: int = 4, file_size: int = 21, uid: int = ADMIN) -> None:
        await self._feed_message(
            uid, voice=Voice(file_id="VOICE1", file_unique_id="V1", duration=duration,
                             mime_type="audio/ogg", file_size=file_size),
        )

    async def audio(self, name: str = "memo.m4a", duration: int = 7) -> None:
        await self._feed_message(
            ADMIN, audio=Audio(file_id="AUDIO1", file_unique_id="A1", duration=duration,
                               file_name=name, mime_type="audio/mp4", file_size=42),
        )

    async def callback(self, data: str, uid: int = ADMIN, message_id: int = 500) -> None:
        upd_id = next(self._ids)
        origin = Message(message_id=message_id, date=datetime.now(), chat=Chat(id=uid, type="private"), text="old")
        query = CallbackQuery(id=str(upd_id), from_user=self._user(uid), chat_instance="ci", data=data, message=origin)
        await self.dp.feed_update(self.bot, Update(update_id=upd_id, callback_query=query))

    async def drain(self) -> None:
        for _ in range(200):
            if not self.ctx._background:
                return
            await asyncio.sleep(0.1)
        raise AssertionError("background runs did not finish")
