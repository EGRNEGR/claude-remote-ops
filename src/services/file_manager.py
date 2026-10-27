"""Sandboxed file system access: path validation, directory listing, search, preview, zipping."""

from __future__ import annotations

import asyncio
import fnmatch
import io
import logging
import os
import time
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import aiofiles

from src.utils.winpath import (
    is_hidden_entry,
    is_within,
    long_path,
    normcase,
    retry_on_lock,
    strip_long_prefix,
)

log = logging.getLogger(__name__)

_BINARY_SAMPLE = 8192
_PREVIEW_READ_LIMIT = 256 * 1024
_SEARCH_MAX_ENTRIES = 300_000
_SEARCH_MAX_SECONDS = 20.0


class PathAccessError(Exception):
    """Path is missing or outside of the allowed directories."""


class FileTooLargeError(Exception):
    def __init__(self, path: Path, size: int, limit: int) -> None:
        super().__init__(f"{path.name}: {size} > {limit}")
        self.path = path
        self.size = size
        self.limit = limit


class SafePathManager:
    """Validates every path against ALLOWED_DIRS (case-insensitive, symlink/junction aware)."""

    def __init__(self, allowed_dirs: list[Path]) -> None:
        self._roots = [Path(strip_long_prefix(str(p))) for p in allowed_dirs]

    @property
    def roots(self) -> list[Path]:
        return list(self._roots)

    def root_for(self, path: Path) -> Path | None:
        matches = [root for root in self._roots if is_within(path, root)]
        return max(matches, key=lambda r: len(str(r))) if matches else None

    def is_allowed(self, path: Path | str) -> bool:
        try:
            self.resolve(path, must_exist=False)
        except PathAccessError:
            return False
        return True

    def is_root(self, path: Path) -> bool:
        return any(normcase(path) == normcase(root) for root in self._roots)

    def resolve(self, path: Path | str, base: Path | None = None, must_exist: bool = True) -> Path:
        """Resolve symlinks/junctions/`..` and ensure the result stays inside an allowed root."""
        raw = Path(strip_long_prefix(str(path)).strip().strip('"'))
        if not raw.is_absolute():
            if base is None:
                raise PathAccessError(f"Относительный путь без базовой папки: {path}")
            raw = base / raw
        try:
            real = Path(strip_long_prefix(os.path.realpath(long_path(raw), strict=must_exist)))
        except (OSError, ValueError) as exc:
            raise PathAccessError(f"Путь не найден: {raw}") from exc
        if must_exist and not os.path.exists(long_path(real)):
            raise PathAccessError(f"Путь не найден: {raw}")
        if self.root_for(real) is None:
            log.warning("Path traversal blocked: %s -> %s", path, real)
            raise PathAccessError(f"Доступ запрещен: путь вне разрешенных папок ({real})")
        return real

    def resolve_dir(self, path: Path | str, base: Path | None = None) -> Path:
        real = self.resolve(path, base=base)
        if not os.path.isdir(long_path(real)):
            raise PathAccessError(f"Это не папка: {real}")
        return real

    def resolve_file(self, path: Path | str, base: Path | None = None) -> Path:
        real = self.resolve(path, base=base)
        if not os.path.isfile(long_path(real)):
            raise PathAccessError(f"Это не файл: {real}")
        return real

    def display(self, path: Path) -> str:
        """Path relative to its allowed root, prefixed with the root name."""
        root = self.root_for(path)
        if root is None:
            return str(path)
        if normcase(path) == normcase(root):
            return str(root)
        rel = os.path.relpath(path, root)
        return f"{root.name or str(root)}\\{rel}"


@dataclass(frozen=True, slots=True)
class EntryInfo:
    name: str
    path: Path
    is_dir: bool
    size: int
    mtime: float


@dataclass(slots=True)
class DirListing:
    path: Path
    entries: list[EntryInfo] = field(default_factory=list)
    skipped: int = 0
    error: str | None = None


@dataclass(slots=True)
class PreviewResult:
    text: str
    encoding: str
    lines_shown: int
    truncated: bool
    is_binary: bool
    size: int


@dataclass(slots=True)
class SearchResult:
    matches: list[Path]
    scanned: int
    limited: bool


class FileManager:
    def __init__(self, safe: SafePathManager, ignored_dirs: frozenset[str], show_hidden: bool) -> None:
        self.safe = safe
        self._ignored = ignored_dirs
        self._show_hidden = show_hidden

    def _skip_dir(self, entry: os.DirEntry[str]) -> bool:
        if entry.name.lower() in self._ignored:
            return True
        return not self._show_hidden and is_hidden_entry(entry)

    async def list_dir(self, path: Path) -> DirListing:
        real = self.safe.resolve_dir(path)
        return await asyncio.to_thread(self._list_dir_sync, real)

    def _list_dir_sync(self, path: Path) -> DirListing:
        listing = DirListing(path=path)
        try:
            with os.scandir(long_path(path)) as it:
                for entry in it:
                    try:
                        is_dir = entry.is_dir(follow_symlinks=True)
                        if is_dir and self._skip_dir(entry):
                            listing.skipped += 1
                            continue
                        if not is_dir and not self._show_hidden and is_hidden_entry(entry):
                            listing.skipped += 1
                            continue
                        st = entry.stat(follow_symlinks=True)
                        listing.entries.append(
                            EntryInfo(
                                name=entry.name,
                                path=path / entry.name,
                                is_dir=is_dir,
                                size=0 if is_dir else st.st_size,
                                mtime=st.st_mtime,
                            )
                        )
                    except OSError:
                        listing.skipped += 1
        except PermissionError:
            listing.error = "Нет прав на чтение папки"
        except OSError as exc:
            listing.error = f"Ошибка чтения папки: {exc.strerror or exc}"
        listing.entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return listing

    async def search(self, roots: list[Path], pattern: str, limit: int) -> SearchResult:
        valid_roots = [self.safe.resolve_dir(root) for root in roots]
        return await asyncio.to_thread(self._search_sync, valid_roots, pattern.strip(), limit)

    def _search_sync(self, roots: list[Path], pattern: str, limit: int) -> SearchResult:
        needle = pattern.lower()
        has_wildcards = any(ch in needle for ch in "*?[")
        exact: list[Path] = []
        partial: list[Path] = []
        scanned = 0
        limited = False
        deadline = time.monotonic() + _SEARCH_MAX_SECONDS
        stack = list(roots)
        while stack:
            current = stack.pop()
            try:
                with os.scandir(long_path(current)) as it:
                    entries = list(it)
            except OSError:
                continue
            for entry in entries:
                scanned += 1
                if scanned > _SEARCH_MAX_ENTRIES or time.monotonic() > deadline:
                    limited = True
                    stack.clear()
                    break
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if not self._skip_dir(entry):
                            stack.append(current / entry.name)
                        continue
                except OSError:
                    continue
                name = entry.name.lower()
                path = current / entry.name
                if has_wildcards:
                    if fnmatch.fnmatchcase(name, needle):
                        exact.append(path)
                elif name == needle:
                    exact.append(path)
                elif needle in name:
                    partial.append(path)
                if len(exact) + len(partial) >= limit * 5:
                    limited = True
                    stack.clear()
                    break
        exact.sort(key=lambda p: (len(p.parts), str(p).lower()))
        partial.sort(key=lambda p: (len(p.parts), str(p).lower()))
        return SearchResult(matches=(exact + partial)[:limit], scanned=scanned, limited=limited)

    async def file_size(self, path: Path) -> int:
        st = await asyncio.to_thread(os.stat, long_path(path))
        return st.st_size

    async def ensure_sendable(self, path: Path, limit: int) -> int:
        real = self.safe.resolve_file(path)
        size = await self.file_size(real)
        if size > limit:
            raise FileTooLargeError(real, size, limit)
        return size

    async def preview(self, path: Path, max_lines: int, max_chars: int = 3500) -> PreviewResult:
        real = self.safe.resolve_file(path)
        size = await self.file_size(real)

        async def _read() -> bytes:
            async with aiofiles.open(long_path(real), "rb") as fh:
                return await fh.read(_PREVIEW_READ_LIMIT)

        data = await retry_on_lock(_read)
        if b"\x00" in data[:_BINARY_SAMPLE] and not data.startswith((b"\xff\xfe", b"\xfe\xff")):
            return PreviewResult("", "binary", 0, False, True, size)
        text, encoding = decode_text(data)
        lines = text.splitlines()
        shown: list[str] = []
        total_chars = 0
        truncated = len(lines) > max_lines or len(data) < size
        for line in lines[:max_lines]:
            if total_chars + len(line) + 1 > max_chars:
                truncated = True
                break
            shown.append(line)
            total_chars += len(line) + 1
        return PreviewResult("\n".join(shown), encoding, len(shown), truncated, False, size)

    async def zip_files(self, paths: list[Path], base: Path | None, limit: int) -> tuple[bytes, list[Path]]:
        """Zip allowed, existing files (relative to `base` when possible). Returns (zip, included)."""
        valid: list[Path] = []
        total = 0
        for path in paths:
            try:
                real = self.safe.resolve_file(path)
            except PathAccessError:
                continue
            size = await self.file_size(real)
            if total + size > limit:
                continue
            total += size
            valid.append(real)
        if not valid:
            return b"", []
        data = await asyncio.to_thread(self._zip_sync, valid, base)
        return data, valid

    @staticmethod
    def _zip_sync(paths: list[Path], base: Path | None) -> bytes:
        buffer = io.BytesIO()
        used: set[str] = set()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            for path in paths:
                if base is not None and is_within(path, base):
                    arcname = os.path.relpath(path, base)
                else:
                    arcname = path.name
                arcname = arcname.replace("\\", "/")
                candidate = arcname
                index = 1
                while candidate.lower() in used:
                    stem, ext = os.path.splitext(arcname)
                    candidate = f"{stem}_{index}{ext}"
                    index += 1
                used.add(candidate.lower())
                try:
                    zf.write(long_path(path), arcname=candidate)
                except OSError as exc:
                    log.warning("Cannot add %s to zip: %s", path, exc)
        return buffer.getvalue()


def decode_text(data: bytes) -> tuple[str, str]:
    """Decode bytes from a Windows machine: BOMs, UTF-8, then legacy Cyrillic code pages."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace"), "utf-8-sig"
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace"), "utf-16"
    try:
        return data.decode("utf-8"), "utf-8"
    except UnicodeDecodeError as exc:
        # A multi-byte char may be cut at the end of the read window.
        if exc.start >= len(data) - 3:
            return data[: exc.start].decode("utf-8", errors="replace"), "utf-8"
    for encoding in ("cp1251", "cp866"):
        try:
            return data.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace"), "utf-8 (с заменами)"
