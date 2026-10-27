"""Short token registry: Telegram callback_data is limited to 64 bytes, Windows paths are not."""

from __future__ import annotations

import hashlib
from collections import OrderedDict


class TokenRegistry:
    """Maps long strings (paths, project names) to stable 12-char tokens. Bounded LRU."""

    def __init__(self, max_items: int = 20_000) -> None:
        self._items: OrderedDict[str, str] = OrderedDict()
        self._max_items = max_items

    @staticmethod
    def make_token(value: str) -> str:
        return hashlib.blake2b(value.encode("utf-8"), digest_size=6).hexdigest()

    def put(self, value: str) -> str:
        token = self.make_token(value)
        self._items[token] = value
        self._items.move_to_end(token)
        while len(self._items) > self._max_items:
            self._items.popitem(last=False)
        return token

    def get(self, token: str) -> str | None:
        value = self._items.get(token)
        if value is not None:
            self._items.move_to_end(token)
        return value

    def __len__(self) -> int:
        return len(self._items)
