"""Live subscription quotas (the `Current session` / `Current week` bars of `claude /usage`).

Claude Code 2.1.304 reads them with `GET https://api.anthropic.com/api/oauth/usage` and the claude.ai OAuth
access token from `~/.claude/.credentials.json` (`claudeAiOauth.accessToken`). The call does not touch the
model, so it costs no tokens, and it answers in 0.3-0.7 s:
  {"five_hour": {"utilization": 5.0, "resets_at": "2026-10-29T12:00:00+00:00"},
   "seven_day": {"utilization": 97.0, "resets_at": "..."}, "seven_day_opus": null, ...}
The bot never refreshes the token itself (a refresh rotates the refresh token and could log the CLI out):
an expired token falls back to the last saved snapshot until the CLI renews it on its next run.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import aiofiles
import aiohttp

from src.utils.winpath import long_path, retry_on_lock

log = logging.getLogger(__name__)

USAGE_PATH = "/api/oauth/usage"
REQUEST_TIMEOUT_SEC = 5.0
# Optional per-model weekly windows, shown when the server reports them.
EXTRA_WINDOWS = {"seven_day_opus": "Opus (неделя)", "seven_day_sonnet": "Sonnet (неделя)"}


class QuotaError(Exception):
    """The live quota request failed (the message is shown to the user)."""


@dataclass(frozen=True, slots=True)
class QuotaWindow:
    utilization: float
    resets_at: float | None  # POSIX timestamp


@dataclass(frozen=True, slots=True)
class QuotaSnapshot:
    fetched_at: float
    session: QuotaWindow | None
    week: QuotaWindow | None
    extra: dict[str, QuotaWindow] = field(default_factory=dict)
    subscription: str | None = None

    def to_dict(self) -> dict[str, Any]:
        def win(w: QuotaWindow | None) -> dict[str, Any] | None:
            return None if w is None else {"utilization": w.utilization, "resets_at": w.resets_at}

        return {
            "fetched_at": self.fetched_at, "session": win(self.session), "week": win(self.week),
            "extra": {k: win(v) for k, v in self.extra.items()}, "subscription": self.subscription,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "QuotaSnapshot":
        def win(raw: object) -> QuotaWindow | None:
            if not isinstance(raw, dict) or raw.get("utilization") is None:
                return None
            resets = raw.get("resets_at")
            return QuotaWindow(float(raw["utilization"]), float(resets) if resets is not None else None)

        extra = {k: w for k, v in (data.get("extra") or {}).items() if (w := win(v)) is not None}
        return cls(float(data["fetched_at"]), win(data.get("session")), win(data.get("week")), extra,
                   data.get("subscription"))


@dataclass(frozen=True, slots=True)
class QuotaResult:
    snapshot: QuotaSnapshot | None
    live: bool                 # True: fetched just now (or within the throttle interval)
    error: str | None = None   # why the live request failed (the snapshot is then the cached one)


def _parse_iso(value: object) -> float | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def parse_usage_response(data: object, fetched_at: float, subscription: str | None = None) -> QuotaSnapshot:
    if not isinstance(data, dict):
        raise QuotaError("неожиданный ответ API")

    def win(raw: object) -> QuotaWindow | None:
        if not isinstance(raw, dict) or raw.get("utilization") is None:
            return None
        try:
            return QuotaWindow(float(raw["utilization"]), _parse_iso(raw.get("resets_at")))
        except (TypeError, ValueError):
            return None

    extra = {key: w for key in EXTRA_WINDOWS if (w := win(data.get(key))) is not None}
    session, week = win(data.get("five_hour")), win(data.get("seven_day"))
    if session is None and week is None:
        raise QuotaError("в ответе нет окон five_hour / seven_day")
    return QuotaSnapshot(fetched_at, session, week, extra, subscription)


Fetcher = Callable[[str, dict[str, str]], Awaitable[tuple[int, Any]]]


async def http_get_json(url: str, headers: dict[str, str]) -> tuple[int, Any]:
    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SEC)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers=headers, allow_redirects=False) as resp:
            if resp.status != 200:
                return resp.status, None
            return resp.status, await resp.json(content_type=None)


class QuotaClient:
    def __init__(
        self,
        credentials_file: Path,
        cache_file: Path,
        base_url: str = "https://api.anthropic.com",
        min_interval_sec: float = 30.0,
        fetcher: Fetcher = http_get_json,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.credentials_file = credentials_file
        self.cache_file = cache_file
        self.url = base_url.rstrip("/") + USAGE_PATH
        self.min_interval_sec = min_interval_sec
        self._fetch = fetcher
        self._clock = clock
        self._cached: QuotaSnapshot | None = None
        self._cache_loaded = False
        self._last_attempt = 0.0
        self._last_error: str | None = None
        self._lock = asyncio.Lock()

    async def _read_credentials(self) -> tuple[str, float | None, str | None]:
        try:
            async with aiofiles.open(long_path(self.credentials_file), "r", encoding="utf-8") as fh:
                data = json.loads(await fh.read())
        except FileNotFoundError as exc:
            raise QuotaError("нет ~/.claude/.credentials.json (войдите в Claude Code: claude /login)") from exc
        except (OSError, ValueError) as exc:
            raise QuotaError(f"не удалось прочитать .credentials.json: {exc}") from exc
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        token = oauth.get("accessToken") if isinstance(oauth, dict) else None
        if not isinstance(token, str) or not token:
            raise QuotaError("в .credentials.json нет OAuth-токена claude.ai (вход по API-ключу?)")
        expires = oauth.get("expiresAt")
        expires_at = float(expires) / 1000 if isinstance(expires, (int, float)) else None
        subscription = oauth.get("subscriptionType") if isinstance(oauth.get("subscriptionType"), str) else None
        return token, expires_at, subscription

    async def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        self._cache_loaded = True
        try:
            async with aiofiles.open(long_path(self.cache_file), "r", encoding="utf-8") as fh:
                self._cached = QuotaSnapshot.from_dict(json.loads(await fh.read()))
        except FileNotFoundError:
            return
        except (OSError, ValueError, KeyError, TypeError) as exc:
            log.warning("Quota cache %s is unreadable: %s", self.cache_file, exc)

    async def _save_cache(self, snapshot: QuotaSnapshot) -> None:
        payload = json.dumps(snapshot.to_dict(), ensure_ascii=False, indent=2)
        os.makedirs(long_path(self.cache_file.parent), exist_ok=True)
        tmp = self.cache_file.with_suffix(".tmp")
        async with aiofiles.open(long_path(tmp), "w", encoding="utf-8") as fh:
            await fh.write(payload)

        async def _replace() -> None:
            await asyncio.to_thread(os.replace, long_path(tmp), long_path(self.cache_file))

        await retry_on_lock(_replace)

    async def _fetch_live(self) -> QuotaSnapshot:
        token, expires_at, subscription = await self._read_credentials()
        now = self._clock()
        if expires_at is not None and expires_at <= now:
            raise QuotaError("OAuth-токен истек: он обновится при следующем запуске Claude Code")
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-remote-ops",
        }
        try:
            status, data = await self._fetch(self.url, headers)
        except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as exc:
            raise QuotaError(f"сеть: {exc.__class__.__name__}") from exc
        if status == 401:
            raise QuotaError("API отклонил токен (401): он обновится при следующем запуске Claude Code")
        if status == 429:
            raise QuotaError("API временно ограничил запросы (429)")
        if status != 200:
            raise QuotaError(f"API ответил HTTP {status}")
        return parse_usage_response(data, now, subscription)

    async def get(self, force: bool = False) -> QuotaResult:
        """Live quotas, at most one request per `min_interval_sec`; on failure the last saved snapshot."""
        async with self._lock:
            await self._load_cache()
            now = self._clock()
            if not force and now - self._last_attempt < self.min_interval_sec:
                return QuotaResult(self._cached, self._last_error is None, self._last_error)
            self._last_attempt = now
            try:
                snapshot = await self._fetch_live()
            except QuotaError as exc:
                self._last_error = str(exc)
                log.info("Quota request failed: %s", exc)
                return QuotaResult(self._cached, False, self._last_error)
            self._last_error = None
            self._cached = snapshot
            try:
                await self._save_cache(snapshot)
            except OSError as exc:
                log.warning("Cannot save quota cache: %s", exc)
            return QuotaResult(snapshot, True, None)
