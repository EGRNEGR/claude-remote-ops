"""Application container injected into every handler as `ctx` (via Dispatcher workflow data)."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.config import BASE_DIR, Settings
from src.services.claude_runner import ClaudeRunner
from src.services.file_manager import FileManager, PathAccessError, SafePathManager
from src.services.net import PollingWatchdog
from src.services.quota import QuotaClient
from src.services.restarter import RestartManager
from src.services.session_parser import SessionInspector
from src.services.state import BgSessionPolicy, BotState, RunLimits, RunMode, StateStore, limits_for
from src.services.transcriber import WhisperTranscriber
from src.services.usage_tracker import UsageTracker
from src.services.watcher import SessionWatcher
from src.utils.registry import TokenRegistry

log = logging.getLogger(__name__)


@dataclass
class AppContext:
    settings: Settings
    safe: SafePathManager
    files: FileManager
    inspector: SessionInspector
    runner: ClaudeRunner
    store: StateStore
    tokens: TokenRegistry
    transcriber: WhisperTranscriber
    restarter: RestartManager
    usage: UsageTracker
    quota: QuotaClient
    watcher: SessionWatcher | None = None
    netwatch: PollingWatchdog | None = None
    _background: set[asyncio.Task[Any]] = field(default_factory=set)
    _pending_voice: OrderedDict[int, str] = field(default_factory=OrderedDict)

    @property
    def state(self) -> BotState:
        return self.store.state

    def spawn(self, coro: Coroutine[Any, Any, Any], name: str) -> asyncio.Task[Any]:
        """Run a coroutine in the background, keep a reference and log failures."""
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)

        def _done(t: asyncio.Task[Any]) -> None:
            self._background.discard(t)
            if not t.cancelled() and t.exception() is not None:
                log.error("Background task %s failed", name, exc_info=t.exception())

        task.add_done_callback(_done)
        return task

    def remember_voice(self, message_id: int, text: str) -> None:
        """Transcript waiting for ▶️ / ❌ (VOICE_AUTO_RUN=false), keyed by the bot's message id."""
        self._pending_voice[message_id] = text
        while len(self._pending_voice) > 50:
            self._pending_voice.popitem(last=False)

    def pop_voice(self, message_id: int) -> str | None:
        return self._pending_voice.pop(message_id, None)

    def token(self, path: Path | str) -> str:
        return self.tokens.put(str(path))

    def path_from_token(self, token: str) -> Path | None:
        value = self.tokens.get(token)
        return Path(value) if value else None

    def env_limits(self) -> RunLimits:
        return RunLimits(self.settings.CLAUDE_MAX_TURNS, self.settings.CLAUDE_MAX_BUDGET_USD)

    def run_limits(self) -> RunLimits:
        """Effective --max-turns / --max-budget-usd for the next run (profile chosen in /mode)."""
        return limits_for(self.state.limit_profile, self.env_limits())

    def active_cwd(self) -> Path | None:
        """Working directory for the next run according to the current mode (validated)."""
        st = self.state
        raw = st.active_session_cwd if st.run_mode == RunMode.RESUME and st.active_session_cwd else st.active_cwd
        if not raw:
            return None
        try:
            return self.safe.resolve_dir(raw)
        except PathAccessError:
            return None


def build_context(settings: Settings) -> AppContext:
    safe = SafePathManager(settings.ALLOWED_DIRS)
    defaults = BotState(
        skip_permissions=settings.SKIP_PERMISSIONS_DEFAULT,
        watcher_enabled=settings.WATCH_ON_START,
        bg_session_policy=BgSessionPolicy(settings.BG_SESSION_POLICY),
        claude_model=settings.CLAUDE_MODEL,
        reasoning_effort=settings.CLAUDE_REASONING_EFFORT,
    )
    return AppContext(
        settings=settings,
        safe=safe,
        files=FileManager(safe, settings.ignored_dirs_lower, settings.SHOW_HIDDEN_FILES),
        inspector=SessionInspector(settings.CLAUDE_PROJECTS_DIR),
        runner=ClaudeRunner(settings),
        store=StateStore(settings.state_file, defaults),
        tokens=TokenRegistry(),
        transcriber=WhisperTranscriber.from_settings(settings),
        restarter=RestartManager(
            state_file=settings.restart_state_file,
            base_dir=BASE_DIR,
            stderr_log=settings.LOG_DIR / "restart_stderr.log",
            ready_timeout=settings.RESTART_READY_TIMEOUT_SEC,
        ),
        usage=UsageTracker(settings.CLAUDE_PROJECTS_DIR, settings.USAGE_HEAVY_CONTEXT_TOKENS),
        quota=QuotaClient(
            credentials_file=settings.CLAUDE_CREDENTIALS_FILE,
            cache_file=settings.quota_cache_file,
            base_url=settings.USAGE_API_BASE_URL,
            min_interval_sec=settings.USAGE_QUOTA_MIN_INTERVAL_SEC,
        ),
    )
