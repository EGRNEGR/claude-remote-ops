"""Safe self-restart from the phone: an independent new bot process takes over polling.

Handshake through data/restart_state.json (old process -> new process):
  1. old: sends "🔄 Перезапускаю бота...", saves chat / message / update ids and its PID, then spawns
     the new process outside its console, process group and job, so it survives the old one exiting.
  2. new: imports and builds everything, writes `ready_pid` and waits for the old PID to exit.
  3. old: sees `ready_pid` and stops polling. If the new process dies or never gets ready (syntax error,
     broken .env, failed pip install), the old one kills it and keeps working: a bad update cannot take
     the bot down.
  4. new: commits the /restart update (aiogram does not confirm the last offset when polling stops, so
     Telegram would deliver /restart again: an endless restart loop), starts polling and reports
     "✅ Бот перезапущен и готов к работе!".

Launch: under start_bot.ps1 (CLAUDE_REMOTE_LAUNCHER=start_bot.ps1) the new instance goes through the script
again to keep its venv / requirements checks and the crash auto-restart loop; otherwise `python main.py`.
Flags: CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP, plus CREATE_BREAKAWAY_FROM_JOB when the job allows it.
The process gets its own hidden console (closing the old window does not touch it) and its own group
(Ctrl+C there does not reach it). DETACHED_PROCESS is not used on purpose: powershell.exe silently exits
without a console, and the venv python.exe stub starts the real interpreter in a new *visible* console.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import logging
import os
import subprocess
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, replace
from functools import lru_cache
from pathlib import Path
from typing import Any

import aiofiles

from src.services.claude_runner import kill_process_tree
from src.utils.text import one_line
from src.utils.winpath import IS_WINDOWS, long_path, retry_on_lock

log = logging.getLogger(__name__)

LAUNCHER_ENV = "CLAUDE_REMOTE_LAUNCHER"
PS1_LAUNCHER = "start_bot.ps1"
# A request older than this is a leftover of a crashed hand-over, not a restart in progress.
STALE_AFTER_SEC = 15 * 60
STDERR_LOG_MAX_BYTES = 1024 * 1024
_PARENT_SESSION_ENV = frozenset({
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
})


@dataclass(frozen=True, slots=True)
class RestartRequest:
    chat_id: int
    message_id: int | None
    update_id: int | None
    old_pid: int
    requested_at: float
    ready_pid: int | None = None

    @classmethod
    def from_dict(cls, data: object) -> "RestartRequest":
        if not isinstance(data, dict):
            raise ValueError("restart state is not an object")

        def optional_int(key: str) -> int | None:
            value = data.get(key)
            return None if value is None else int(value)

        return cls(
            chat_id=int(data["chat_id"]),
            message_id=optional_int("message_id"),
            update_id=optional_int("update_id"),
            old_pid=int(data["old_pid"]),
            requested_at=float(data["requested_at"]),
            ready_pid=optional_int("ready_pid"),
        )


@dataclass(frozen=True, slots=True)
class Launch:
    argv: list[str]
    creationflags: int
    via: str


@dataclass(frozen=True, slots=True)
class HandoverResult:
    ok: bool
    detail: str


Spawner = Callable[[Launch, Path, Path], Any]


def build_launch(base_dir: Path, launcher: str | None = None) -> Launch:
    """Command for the new instance: through start_bot.ps1 if this one was started by it, else main.py."""
    launcher = os.environ.get(LAUNCHER_ENV, "") if launcher is None else launcher
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP if IS_WINDOWS else 0
    script = base_dir / PS1_LAUNCHER
    if IS_WINDOWS and launcher == PS1_LAUNCHER and script.is_file():
        powershell = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "WindowsPowerShell" / "v1.0"
        exe = powershell / "powershell.exe"
        return Launch(
            argv=[str(exe) if exe.is_file() else "powershell.exe",
                  "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
            creationflags=flags,
            via=PS1_LAUNCHER,
        )
    return Launch(argv=[sys.executable, str(base_dir / "main.py")], creationflags=flags, via="python main.py")


def spawn_detached(launch: Launch, cwd: Path, stderr_log: Path) -> subprocess.Popen[bytes]:
    """Start a process that outlives this one. Its stderr (crash before logging is set up) goes to a file."""
    env = {k: v for k, v in os.environ.items() if k.upper() not in _PARENT_SESSION_ENV}
    os.makedirs(long_path(stderr_log.parent), exist_ok=True)
    try:
        oversized = os.path.getsize(long_path(stderr_log)) > STDERR_LOG_MAX_BYTES
    except OSError:
        oversized = False
    with open(long_path(stderr_log), "wb" if oversized else "ab") as err:
        common: dict[str, Any] = {
            "cwd": str(cwd), "env": env, "close_fds": True,
            "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": err,
        }
        if not IS_WINDOWS:
            return subprocess.Popen(launch.argv, start_new_session=True, **common)
        try:
            return subprocess.Popen(
                launch.argv, creationflags=launch.creationflags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **common
            )
        except PermissionError:  # the job of this process does not allow breakaway
            return subprocess.Popen(launch.argv, creationflags=launch.creationflags, **common)


@lru_cache(maxsize=1)
def _kernel32() -> Any:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return kernel32


def pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if not IS_WINDOWS:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    kernel32 = _kernel32()
    synchronize, query_limited = 0x00100000, 0x1000
    handle = kernel32.OpenProcess(synchronize | query_limited, False, pid)
    if not handle:
        return ctypes.get_last_error() == 5  # ERROR_ACCESS_DENIED: exists, but belongs to someone else
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x102  # WAIT_TIMEOUT: still running
    finally:
        kernel32.CloseHandle(handle)


async def wait_pid_exit(pid: int, timeout: float, poll: float = 0.3) -> bool:
    deadline = time.monotonic() + timeout
    while pid_alive(pid):
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(poll)
    return True


class RestartManager:
    def __init__(
        self,
        state_file: Path,
        base_dir: Path,
        stderr_log: Path,
        ready_timeout: float,
        handover_timeout: float = 60.0,
        spawner: Spawner | None = None,
    ) -> None:
        self._state_file = state_file
        self._base_dir = base_dir
        self._stderr_log = stderr_log
        self._ready_timeout = ready_timeout
        self._handover_timeout = handover_timeout
        self._spawner = spawner if spawner is not None else spawn_detached
        self._stop_polling: Callable[[], Awaitable[None]] | None = None
        self._busy = False
        self._replayed_up_to: int | None = None
        # New side: the request this process was started for (announced in on_startup, then cleared).
        self.pending: RestartRequest | None = None
        # Old side: polling was stopped for a new process (on_shutdown stays silent).
        self.handed_over = False

    def bind(self, stop_polling: Callable[[], Awaitable[None]]) -> None:
        self._stop_polling = stop_polling

    @property
    def in_progress(self) -> bool:
        return self._busy

    def is_replayed(self, update_id: int) -> bool:
        """True for the /restart update that started this process, if Telegram delivers it again."""
        return self._replayed_up_to is not None and update_id <= self._replayed_up_to

    # ---------- state file ----------

    async def read(self) -> RestartRequest | None:
        try:
            async with aiofiles.open(long_path(self._state_file), "r", encoding="utf-8") as fh:
                raw = await fh.read()
        except FileNotFoundError:
            return None
        except OSError as exc:  # replaced by the other process right now: just "not yet"
            log.debug("Restart state not readable yet: %s", exc)
            return None
        try:
            return RestartRequest.from_dict(json.loads(raw))
        except (ValueError, TypeError, KeyError) as exc:
            log.warning("Restart state %s is broken (%s), ignoring it", self._state_file, exc)
            return None

    async def write(self, request: RestartRequest) -> None:
        os.makedirs(long_path(self._state_file.parent), exist_ok=True)
        tmp = self._state_file.with_suffix(f".{os.getpid()}.tmp")
        async with aiofiles.open(long_path(tmp), "w", encoding="utf-8") as fh:
            await fh.write(json.dumps(asdict(request), indent=2))

        async def _replace() -> None:
            await asyncio.to_thread(os.replace, long_path(tmp), long_path(self._state_file))

        await retry_on_lock(_replace)

    async def clear(self) -> None:
        async def _remove() -> None:
            await asyncio.to_thread(os.remove, long_path(self._state_file))

        try:
            await retry_on_lock(_remove)
        except FileNotFoundError:
            log.debug("Restart state %s is already gone", self._state_file)

    # ---------- old process ----------

    async def hand_over(self, chat_id: int, message_id: int | None, update_id: int | None) -> HandoverResult:
        """Spawn the new instance, wait until it is ready, then stop polling here."""
        if self._busy:
            return HandoverResult(False, "перезапуск уже выполняется")
        self._busy = True
        try:
            await self.write(RestartRequest(chat_id, message_id, update_id, os.getpid(), time.time()))
            launch = build_launch(self._base_dir)
            stderr_offset = self._stderr_size()
            try:
                proc = await asyncio.to_thread(self._spawner, launch, self._base_dir, self._stderr_log)
            except OSError as exc:
                await self.clear()
                return HandoverResult(False, f"не удалось запустить новый процесс ({launch.via}): {exc}")
            log.info("Restart: started %s (PID %s), waiting until it is ready", launch.via, proc.pid)
            ready = await self._wait_ready(proc)
            if ready is None or ready.ready_pid is None:
                code = proc.poll()
                if code is None:
                    await kill_process_tree(proc.pid)
                    reason = f"новый процесс ({launch.via}) не ответил за {self._ready_timeout:g} с"
                else:
                    reason = f"новый процесс ({launch.via}) завершился с кодом {code}"
                tail = self._stderr_tail(stderr_offset)
                await self.clear()
                log.warning("Restart aborted: %s %s", reason, one_line(tail))
                return HandoverResult(False, f"{reason}\n{tail}".strip())
            log.info("Restart: new process PID %s is ready, stopping polling here", ready.ready_pid)
            self.handed_over = True
            if self._stop_polling is not None:
                await self._stop_polling()
            return HandoverResult(True, f"новый процесс PID {ready.ready_pid} ({launch.via})")
        finally:
            if not self.handed_over:
                self._busy = False

    async def _wait_ready(self, proc: Any) -> RestartRequest | None:
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            request = await self.read()
            if request is not None and request.ready_pid:
                return request
            if proc.poll() is not None:
                return None
            await asyncio.sleep(0.5)
        return None

    def _stderr_size(self) -> int:
        try:
            return os.path.getsize(long_path(self._stderr_log))
        except OSError:
            return 0

    def _stderr_tail(self, offset: int, limit: int = 800) -> str:
        try:
            with open(long_path(self._stderr_log), "rb") as fh:
                if offset <= os.path.getsize(long_path(self._stderr_log)):
                    fh.seek(offset)
                data = fh.read()
        except OSError:
            return ""
        return data.decode("utf-8", errors="replace").strip()[-limit:]

    # ---------- new process ----------

    async def take_over(self) -> RestartRequest | None:
        """Called by a fresh process before it touches state or polling. None = a normal start."""
        request = await self.read()
        if request is None:
            return None
        if time.time() - request.requested_at > STALE_AFTER_SEC or request.old_pid == os.getpid():
            log.warning("Ignoring a stale restart request from PID %s", request.old_pid)
            await self.clear()
            return None
        request = replace(request, ready_pid=os.getpid())
        await self.write(request)
        log.info("Restart: ready, waiting for the previous process PID %s to exit", request.old_pid)
        if not await wait_pid_exit(request.old_pid, self._handover_timeout):
            # It agreed to hand over and stopped polling: finishing it off is safe.
            log.warning("Previous process PID %s is still alive, killing it", request.old_pid)
            await kill_process_tree(request.old_pid)
            await wait_pid_exit(request.old_pid, 10.0)
        self.pending = request
        if request.update_id is not None:
            self._replayed_up_to = request.update_id
        return request
