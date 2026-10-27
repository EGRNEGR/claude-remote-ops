"""Text helpers: Telegram HTML escaping, truncation, chunking and human-friendly formatting."""

from __future__ import annotations

import html
import re
from datetime import datetime, timezone

TG_MESSAGE_LIMIT = 4096
TG_SAFE_LIMIT = 4000
TG_CAPTION_LIMIT = 1024

_WS_RE = re.compile(r"\s+")
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def esc(value: object) -> str:
    """Escape arbitrary text for Telegram HTML parse mode (<, >, &)."""
    return html.escape(str(value), quote=False)


def esc_fit(value: object, max_len: int) -> str:
    """Escape text so that the *escaped* result is at most `max_len` chars (never cuts an entity)."""
    text = str(value)
    escaped = esc(text)
    if len(escaped) <= max_len:
        return escaped
    limit = max_len
    while limit > 0:
        escaped = esc(truncate(text, limit))
        if len(escaped) <= max_len:
            return escaped
        limit -= max(1, len(escaped) - max_len)
    return ""


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def one_line(text: str) -> str:
    return _WS_RE.sub(" ", text).strip()


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    """Truncate raw (unescaped) text to `limit` characters."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(suffix):
        return text[:limit]
    return text[: limit - len(suffix)].rstrip() + suffix


def truncate_middle(text: str, limit: int) -> str:
    """Keep the start and the end of a long string (useful for Windows paths)."""
    if len(text) <= limit:
        return text
    if limit < 5:
        return text[:limit]
    head = (limit - 1) // 2
    tail = limit - 1 - head
    return f"{text[:head]}…{text[-tail:]}"


def chunk_text(text: str, size: int = TG_SAFE_LIMIT) -> list[str]:
    """Split text into chunks <= size, preferring paragraph and line boundaries."""
    if size <= 0:
        raise ValueError("size must be positive")
    chunks: list[str] = []
    rest = text
    while len(rest) > size:
        window = rest[:size]
        cut = window.rfind("\n\n")
        if cut < size // 2:
            cut = window.rfind("\n")
        if cut < size // 2:
            cut = window.rfind(" ")
        if cut < size // 2:
            cut = size
        chunks.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip("\n")
    if rest.strip():
        chunks.append(rest)
    return chunks or [""]


def human_size(num: float) -> str:
    units = ("Б", "КБ", "МБ", "ГБ", "ТБ")
    value = float(num)
    for unit in units:
        if abs(value) < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "Б" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} ТБ"


def human_duration(seconds: float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} ч {minutes} мин"
    if minutes:
        return f"{minutes} мин {secs} с"
    return f"{secs} с"


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def fmt_dt(value: datetime | float | None, with_date: bool = True) -> str:
    """Format a UTC datetime or POSIX timestamp in the local time zone."""
    if value is None:
        return "?"
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=timezone.utc)
    local = value.astimezone()
    return local.strftime("%d.%m.%Y %H:%M" if with_date else "%H:%M")


def fmt_short_dt(value: datetime | float | None) -> str:
    if value is None:
        return "?"
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=timezone.utc)
    local = value.astimezone()
    now = datetime.now().astimezone()
    if local.date() == now.date():
        return local.strftime("%H:%M")
    if local.year == now.year:
        return local.strftime("%d.%m %H:%M")
    return local.strftime("%d.%m.%y")


def fmt_cost(value: float | None) -> str:
    if value is None:
        return "н/д"
    return f"${value:.4f}" if value < 0.1 else f"${value:.2f}"


def plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural form: 1 файл, 2 файла, 5 файлов."""
    n_abs = abs(n) % 100
    if 11 <= n_abs <= 19:
        return many
    last = n_abs % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many
