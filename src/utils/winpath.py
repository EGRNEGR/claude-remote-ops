"""Windows specific path helpers: case-insensitive containment, long paths, file names, OEM code page."""

from __future__ import annotations

import asyncio
import logging
import os
import re
import stat
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

IS_WINDOWS = sys.platform == "win32"
LONG_PATH_THRESHOLD = 240
_LONG_PREFIX = "\\\\?\\"
_UNC_LONG_PREFIX = "\\\\?\\UNC\\"

_FORBIDDEN_CHARS_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)

# Win32 sharing violation / lock violation: file is opened by another process.
_LOCK_WINERRORS = frozenset({32, 33})


def normcase(path: Path | str) -> str:
    """Case-folded, normalized string form of a path (C:\\Foo == c:\\foo on Windows)."""
    return os.path.normcase(os.path.normpath(str(path)))


def strip_long_prefix(path: str) -> str:
    if path.startswith(_UNC_LONG_PREFIX):
        return "\\\\" + path[len(_UNC_LONG_PREFIX):]
    if path.startswith(_LONG_PREFIX):
        return path[len(_LONG_PREFIX):]
    return path


def is_within(child: Path, root: Path) -> bool:
    """True if `child` equals or is inside `root`, comparing case-insensitively on Windows."""
    child_norm = Path(normcase(strip_long_prefix(str(child))))
    root_norm = Path(normcase(strip_long_prefix(str(root))))
    return child_norm == root_norm or child_norm.is_relative_to(root_norm)


def long_path(path: Path | str) -> str:
    """Return a string usable by Win32 APIs even when the path is longer than MAX_PATH (260)."""
    text = str(path)
    if not IS_WINDOWS or len(text) < LONG_PATH_THRESHOLD or text.startswith(_LONG_PREFIX):
        return text
    absolute = os.path.abspath(text)
    if absolute.startswith("\\\\"):
        return _UNC_LONG_PREFIX + absolute[2:]
    return _LONG_PREFIX + absolute


def sanitize_filename(name: str, fallback: str = "file") -> str:
    """Make a user supplied file name safe for NTFS."""
    base = os.path.basename(name.replace("\\", "/").split("/")[-1])
    base = _FORBIDDEN_CHARS_RE.sub("_", base).strip().rstrip(". ")
    if not base:
        base = fallback
    stem, dot, ext = base.partition(".")
    if stem.upper() in _RESERVED_NAMES:
        base = f"_{stem}{dot}{ext}"
    if len(base) > 180:
        root, ext = os.path.splitext(base)
        base = root[: 180 - len(ext)] + ext
    return base


def unique_path(directory: Path, name: str) -> Path:
    """Return directory/name, adding ` (n)` before the extension if the file already exists."""
    candidate = directory / name
    if not os.path.exists(long_path(candidate)):
        return candidate
    stem, ext = os.path.splitext(name)
    for index in range(1, 10_000):
        candidate = directory / f"{stem} ({index}){ext}"
        if not os.path.exists(long_path(candidate)):
            return candidate
    raise FileExistsError(f"Не удалось подобрать свободное имя для {name}")


def is_hidden_entry(entry: os.DirEntry[str]) -> bool:
    """Hidden or system files (Windows attributes) and dot files."""
    if entry.name.startswith("."):
        return True
    if not IS_WINDOWS:
        return False
    try:
        attrs = entry.stat(follow_symlinks=False).st_file_attributes
    except (OSError, AttributeError):
        return False
    return bool(attrs & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM))


def oem_encoding() -> str:
    """Encoding used by console tools like taskkill.exe (cp866 on Russian Windows)."""
    if not IS_WINDOWS:
        return "utf-8"
    try:
        import ctypes

        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except (OSError, AttributeError):
        return "cp866"


def is_lock_error(exc: BaseException) -> bool:
    return isinstance(exc, PermissionError) and getattr(exc, "winerror", None) in _LOCK_WINERRORS


async def retry_on_lock(
    func: Callable[[], Awaitable[T]],
    attempts: int = 5,
    delay: float = 0.3,
) -> T:
    """Retry an async file operation while Windows reports a sharing/lock violation."""
    for attempt in range(1, attempts + 1):
        try:
            return await func()
        except PermissionError as exc:
            if not is_lock_error(exc) or attempt == attempts:
                raise
            log.debug("File locked (attempt %s/%s): %s", attempt, attempts, exc)
            await asyncio.sleep(delay * attempt)
    raise RuntimeError("unreachable")
