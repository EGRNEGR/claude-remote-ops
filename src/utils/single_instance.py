"""One running bot per data folder: two pollers fight over getUpdates and run every task twice.

Windows: a named mutex (the OS drops it the moment the owner dies, no stale PID files).
Elsewhere: an exclusive flock on data/bot.lock.
"""

from __future__ import annotations

import ctypes
import hashlib
import os
from functools import lru_cache
from pathlib import Path
from typing import Any

from src.utils.winpath import IS_WINDOWS, long_path

_ERROR_ALREADY_EXISTS = 183


@lru_cache(maxsize=1)
def _kernel32() -> Any:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.argtypes = (wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR)
    kernel32.CreateMutexW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    return kernel32


class InstanceLock:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        digest = hashlib.sha1(os.path.normcase(str(data_dir.resolve())).encode("utf-8")).hexdigest()[:16]
        self.name = f"Local\\claude-remote-ops-{digest}"
        self._handle: Any = None

    def acquire(self) -> bool:
        if self._handle is not None:
            return True
        if IS_WINDOWS:
            kernel32 = _kernel32()
            ctypes.set_last_error(0)
            handle = kernel32.CreateMutexW(None, False, self.name)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            if ctypes.get_last_error() == _ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False
            self._handle = handle
            return True
        import fcntl

        os.makedirs(long_path(self._data_dir), exist_ok=True)
        fh = open(self._data_dir / "bot.lock", "a+")  # noqa: SIM115 - kept open while the bot runs
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._handle = fh
        return True

    def release(self) -> None:
        if self._handle is None:
            return
        if IS_WINDOWS:
            _kernel32().CloseHandle(self._handle)
        else:
            self._handle.close()
        self._handle = None
