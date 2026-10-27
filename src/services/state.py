"""Persistent bot state (active project, session, launch mode, toggles) stored as JSON."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, fields, replace
from enum import StrEnum
from pathlib import Path

import aiofiles

from src.utils.winpath import long_path, retry_on_lock

log = logging.getLogger(__name__)

# v1: initial format. v2: limit profiles + full-trust default (skip-permissions) applied on upgrade.
STATE_SCHEMA_VERSION = 2


class RunMode(StrEnum):
    NEW = "new"
    CONTINUE = "continue"
    RESUME = "resume"


RUN_MODE_TITLES: dict[RunMode, str] = {
    RunMode.NEW: "🆕 Новый чат в проекте",
    RunMode.CONTINUE: "⏩ Последний чат в папке (--continue)",
    RunMode.RESUME: "🔁 Выбранный чат (--resume)",
}


class LimitProfile(StrEnum):
    AUTO = "auto"
    ECONOMY = "economy"
    PRO = "pro"


@dataclass(frozen=True, slots=True)
class RunLimits:
    """Per-run CLI limits. None = the flag is omitted (no limit)."""

    max_turns: int | None = None
    max_budget_usd: float | None = None

    @property
    def unlimited(self) -> bool:
        return self.max_turns is None and self.max_budget_usd is None

    def describe(self) -> str:
        if self.unlimited:
            return "без лимитов"
        budget = f"${self.max_budget_usd:g}" if self.max_budget_usd else "$∞"
        turns = f"{self.max_turns} ходов" if self.max_turns else "∞ ходов"
        return f"{budget} / {turns}"


UNLIMITED = RunLimits()
PROFILE_LIMITS: dict[LimitProfile, RunLimits] = {
    LimitProfile.ECONOMY: RunLimits(max_turns=15, max_budget_usd=2.0),
    LimitProfile.PRO: RunLimits(max_turns=80, max_budget_usd=15.0),
}


def limits_for(profile: LimitProfile, env_limits: RunLimits) -> RunLimits:
    """AUTO uses CLAUDE_MAX_TURNS / CLAUDE_MAX_BUDGET_USD from .env (0 or empty there = unlimited)."""
    return PROFILE_LIMITS.get(profile, env_limits)


def limit_profile_title(profile: LimitProfile, env_limits: RunLimits) -> str:
    if profile == LimitProfile.AUTO:
        if env_limits.unlimited:
            return "♾ Без лимитов (Auto)"
        return f"♾ Auto (.env: {env_limits.describe()})"
    limits = PROFILE_LIMITS[profile]
    icon, name = ("💵", "Эконом") if profile == LimitProfile.ECONOMY else ("🚀", "Pro")
    return f"{icon} {name} (${limits.max_budget_usd:g} / {limits.max_turns} ходов)"


@dataclass(slots=True)
class BotState:
    active_cwd: str | None = None
    active_session_id: str | None = None
    active_session_cwd: str | None = None
    active_session_title: str | None = None
    run_mode: RunMode = RunMode.NEW
    skip_permissions: bool = True
    watcher_enabled: bool = True
    limit_profile: LimitProfile = LimitProfile.AUTO
    schema_version: int = STATE_SCHEMA_VERSION

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "BotState":
        known = {f.name for f in fields(cls)}
        clean = {k: v for k, v in data.items() if k in known}
        state = cls(**clean)  # type: ignore[arg-type]
        try:
            state.run_mode = RunMode(str(state.run_mode))
        except ValueError:
            state.run_mode = RunMode.NEW
        try:
            state.limit_profile = LimitProfile(str(state.limit_profile))
        except ValueError:
            state.limit_profile = LimitProfile.AUTO
        return state

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["run_mode"] = str(self.run_mode)
        data["limit_profile"] = str(self.limit_profile)
        return data


class StateStore:
    """Async JSON persistence with atomic replace (safe against crashes and AV file locks)."""

    def __init__(self, path: Path, defaults: BotState) -> None:
        self._path = path
        self._defaults = replace(defaults)
        self._state = defaults
        self._lock = asyncio.Lock()

    @property
    def state(self) -> BotState:
        return self._state

    async def load(self) -> BotState:
        if not os.path.exists(long_path(self._path)):
            await self.save()
            return self._state
        try:
            async with aiofiles.open(long_path(self._path), "r", encoding="utf-8") as fh:
                raw = await fh.read()
            data = json.loads(raw)
            stored_version = int(data.get("schema_version", 1))
            state = BotState.from_dict(data)
        except (OSError, ValueError, TypeError) as exc:
            log.warning("State file %s is unreadable (%s), using defaults", self._path, exc)
            return self._state
        if stored_version < 2:
            # Upgrade: apply the new full-trust default and the limit profile from settings.
            state.skip_permissions = self._defaults.skip_permissions
            state.limit_profile = self._defaults.limit_profile
            log.info(
                "State migrated v%s -> v%s: skip_permissions=%s, limit_profile=%s",
                stored_version, STATE_SCHEMA_VERSION, state.skip_permissions, state.limit_profile,
            )
        state.schema_version = STATE_SCHEMA_VERSION
        self._state = state
        if stored_version < STATE_SCHEMA_VERSION:
            await self.save()
        return self._state

    async def save(self) -> None:
        async with self._lock:
            payload = json.dumps(self._state.to_dict(), ensure_ascii=False, indent=2)
            os.makedirs(long_path(self._path.parent), exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            async with aiofiles.open(long_path(tmp), "w", encoding="utf-8") as fh:
                await fh.write(payload)

            async def _replace() -> None:
                await asyncio.to_thread(os.replace, long_path(tmp), long_path(self._path))

            await retry_on_lock(_replace)

    async def update(self, **changes: object) -> BotState:
        for key, value in changes.items():
            if not hasattr(self._state, key):
                raise AttributeError(f"Unknown state field: {key}")
            setattr(self._state, key, value)
        await self.save()
        return self._state
