"""Hard access filter: only ADMIN_USER_ID may talk to the bot, everyone else is silently ignored."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, Update, User

log = logging.getLogger("security")

_LOG_INTERVAL_SEC = 60.0


class AuthMiddleware(BaseMiddleware):
    """Registered as an outer middleware on `dp.update`, i.e. before routing, filters and FSM."""

    def __init__(self, admin_user_id: int) -> None:
        self._admin_id = admin_user_id
        self._last_logged: dict[int, float] = {}
        self._dropped: dict[int, int] = {}

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is not None and user.id == self._admin_id:
            return await handler(event, data)
        self._log_attempt(user, event)
        return None

    def _log_attempt(self, user: User | None, event: TelegramObject) -> None:
        user_id = user.id if user is not None else 0
        self._dropped[user_id] = self._dropped.get(user_id, 0) + 1
        now = time.monotonic()
        if now - self._last_logged.get(user_id, 0.0) < _LOG_INTERVAL_SEC:
            return
        self._last_logged[user_id] = now
        update_type = event.event_type if isinstance(event, Update) else type(event).__name__
        snippet = ""
        if isinstance(event, Update):
            if event.message is not None:
                snippet = (event.message.text or event.message.caption or "")[:80]
            elif event.callback_query is not None:
                snippet = (event.callback_query.data or "")[:80]
        if user is None:
            log.warning("Dropped update without user (type=%s)", update_type)
            return
        log.warning(
            "Unauthorized access attempt: id=%s username=@%s name=%r type=%s text=%r (dropped total: %s)",
            user.id, user.username, user.full_name, update_type, snippet, self._dropped[user_id],
        )
