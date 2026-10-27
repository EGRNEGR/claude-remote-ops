"""Background monitoring of ~/.claude/projects with watchdog: push a short report after a pause."""

from __future__ import annotations

import asyncio
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from src.services.session_parser import SESSION_ID_RE, SessionInspector, SessionReport
from src.utils.winpath import long_path, normcase

log = logging.getLogger(__name__)

NotifyCallback = Callable[[SessionReport], Awaitable[None]]
BusyProvider = Callable[[], set[str]]


class _JsonlEventHandler(FileSystemEventHandler):
    def __init__(self, callback: Callable[[str], None]) -> None:
        self._callback = callback

    def _emit(self, path: str | bytes) -> None:
        text = os.fsdecode(path)
        if text.endswith(".jsonl"):
            self._callback(text)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit(event.src_path)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._emit(event.dest_path)


class SessionWatcher:
    """Tracks byte offsets per transcript; after `debounce` seconds of quiet sends a delta report."""

    def __init__(
        self,
        inspector: SessionInspector,
        debounce_sec: float,
        notify: NotifyCallback,
        busy_sessions: BusyProvider,
        ignore_headless: bool = True,
    ) -> None:
        self._inspector = inspector
        self._root = inspector.projects_dir
        self._debounce = debounce_sec
        self._notify = notify
        self._busy = busy_sessions
        self._ignore_headless = ignore_headless
        self._observer: Observer | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._timers: dict[str, asyncio.TimerHandle] = {}
        self._offsets: dict[str, int] = {}
        self._tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self._observer is not None and self._observer.is_alive()

    async def start(self) -> None:
        async with self._lock:
            if self.running:
                return
            if not os.path.isdir(long_path(self._root)):
                raise FileNotFoundError(f"Папка истории Claude не найдена: {self._root}")
            self._loop = asyncio.get_running_loop()
            self._offsets = await asyncio.to_thread(self._snapshot_sizes)
            observer = Observer()
            observer.schedule(_JsonlEventHandler(self._on_fs_event), str(self._root), recursive=True)
            observer.daemon = True
            observer.start()
            self._observer = observer
            log.info("Session watcher started on %s (%s transcripts)", self._root, len(self._offsets))

    async def stop(self) -> None:
        async with self._lock:
            observer, self._observer = self._observer, None
            for handle in self._timers.values():
                handle.cancel()
            self._timers.clear()
            if observer is not None:
                observer.stop()
                await asyncio.to_thread(observer.join, 5)
                log.info("Session watcher stopped")

    def mark_consumed(self, path: Path) -> None:
        """Skip everything currently in the file (used after bot-driven runs)."""
        try:
            self._offsets[normcase(path)] = os.path.getsize(long_path(path))
        except OSError:
            return

    def _snapshot_sizes(self) -> dict[str, int]:
        sizes: dict[str, int] = {}
        try:
            with os.scandir(long_path(self._root)) as projects:
                for project in projects:
                    if not project.is_dir(follow_symlinks=False):
                        continue
                    with os.scandir(project.path) as files:
                        for entry in files:
                            if entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                                sizes[normcase(entry.path)] = entry.stat().st_size
        except OSError as exc:
            log.warning("Watcher snapshot failed: %s", exc)
        return sizes

    def _is_transcript(self, path: Path) -> bool:
        # Only <root>/<project>/<uuid>.jsonl (subagent sidechains live deeper and are skipped).
        return (
            normcase(path.parent.parent) == normcase(self._root)
            and SESSION_ID_RE.match(path.stem) is not None
        )

    def _on_fs_event(self, raw_path: str) -> None:
        """Called from the watchdog thread."""
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        loop.call_soon_threadsafe(self._schedule, raw_path)

    def _schedule(self, raw_path: str) -> None:
        path = Path(raw_path)
        if not self._is_transcript(path) or not self.running:
            return
        key = normcase(path)
        previous = self._timers.pop(key, None)
        if previous is not None:
            previous.cancel()
        assert self._loop is not None
        self._timers[key] = self._loop.call_later(self._debounce, self._fire, path)

    def _fire(self, path: Path) -> None:
        self._timers.pop(normcase(path), None)
        task = asyncio.ensure_future(self._process(path))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _process(self, path: Path) -> None:
        key = normcase(path)
        try:
            size = os.path.getsize(long_path(path))
        except OSError:
            self._offsets.pop(key, None)
            return
        start = self._offsets.get(key, 0)
        if size < start:
            start = 0
        if size == start:
            return
        if path.stem in self._busy():
            self._offsets[key] = size
            return
        try:
            report = await self._inspector.build_report_from_path(path, path.parent.name, start)
        except OSError as exc:
            log.warning("Watcher cannot parse %s: %s", path, exc)
            return
        self._offsets[key] = report.end_offset
        if not report.has_activity:
            return
        if self._ignore_headless and report.is_headless:
            log.debug("Skip headless session %s", report.session_id)
            return
        try:
            await self._notify(report)
        except Exception:  # noqa: BLE001 - a failed push must not kill the watcher
            log.exception("Watcher notification failed for %s", report.session_id)
