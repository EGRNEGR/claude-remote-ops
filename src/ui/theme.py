"""Clean Editorial Dark-HUD: the semantic markers, typography and small widgets shared by every screen.

Card anatomy (every message the bot builds follows it):
    ✦ <b>Title</b>                      title()     - subject of the card
    <i>meta · meta · 09:07</i>          meta()      - muted context line
                                                    - air between blocks
    ◈ Field: <b>value</b>              field()     - key facts
    ▸ action or hint                   item()
    <blockquote expandable>…</blockquote>           - secondary details, one tap to open
    <i>Opus 5.5 · 45 сек · ~$0.18</i>  meta()      - footer

All helpers take plain text and escape it; arguments named `*_html` are trusted, already escaped HTML.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path, PureWindowsPath

from src.services.pricing import normalize_model_id
from src.utils.text import esc, plural, truncate

MARK_TITLE = "✦"
MARK_SECTION = "◈"
MARK_ITEM = "▸"
BULLET = "•"
SEP = " · "
DIVIDER = "┈┈┈┈┈┈┈┈┈┈"

DOT_OK = "🟢"
DOT_BUSY = "🟡"
DOT_ERROR = "🔴"
DOT_IDLE = "⚪"

BAR_ON = "▰"
BAR_OFF = "▱"
BAR_WIDTH = 12

_ENCODED_DIR_RE = re.compile(r"^[A-Za-z]--")
_UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_MODEL_RE = re.compile(r"^claude-(?:(\d+)-(\d+)-)?([a-z]+)(?:-(\d+))?(?:-(\d+))?$")


# ---------- typography ----------

def title(text: str, marker: str = MARK_TITLE, limit: int = 120) -> str:
    return f"{marker} <b>{esc(truncate(text, limit))}</b>"


def meta(*parts: str | None) -> str:
    """Muted metadata line from plain-text parts; empty parts are skipped."""
    shown = [esc(p) for p in parts if p]
    return f"<i>{SEP.join(shown)}</i>" if shown else ""


def meta_html(*parts_html: str | None) -> str:
    shown = [p for p in parts_html if p]
    return f"<i>{SEP.join(shown)}</i>" if shown else ""


def field(label: str, value_html: str) -> str:
    return f"{MARK_SECTION} {esc(label)}: <b>{value_html}</b>"


def section(label: str) -> str:
    return f"{MARK_SECTION} <b>{esc(label)}</b>"


def item(text_html: str) -> str:
    return f"{MARK_ITEM} {text_html}"


def quote(body_html: str, expandable: bool = True) -> str:
    tag = "blockquote expandable" if expandable else "blockquote"
    return f"<{tag}>{body_html}</blockquote>"


def join_blocks(*blocks: str | None) -> str:
    """Blocks separated by one empty line (the "air" of the layout); empty blocks are dropped."""
    return "\n\n".join(b for b in blocks if b)


def lines(*rows: str | None) -> str:
    return "\n".join(r for r in rows if r)


# ---------- widgets ----------

def bar(percent: float, width: int = BAR_WIDTH) -> str:
    """`▰▰▰▱▱▱` clamped to 0-100%; any non-zero value shows at least one cell."""
    value = max(0.0, min(100.0, float(percent)))
    filled = round(value / 100 * width)
    if value > 0 and filled == 0:
        filled = 1
    return BAR_ON * filled + BAR_OFF * (width - filled)


def level_dot(percent: float) -> str:
    return DOT_ERROR if percent >= 90 else DOT_BUSY if percent >= 70 else DOT_OK


# ---------- human formats ----------

def duration(seconds: float | None) -> str:
    """45 сек · 3 мин 5 сек · 12 мин · 1 ч 12 мин."""
    if seconds is None:
        return ""
    total = max(0, int(round(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours} ч {minutes} мин" if minutes else f"{hours} ч"
    if minutes:
        return f"{minutes} мин {secs} сек" if minutes < 10 and secs else f"{minutes} мин"
    return f"{secs} сек"


def money(usd: float | None, approx: bool = True) -> str:
    """`~$0.18`; costs on a subscription are API-equivalent estimates, hence the tilde."""
    if usd is None:
        return ""
    sign = "~" if approx else ""
    if usd < 0.01:
        return f"{sign}$0.00" if usd <= 0 else "<$0.01"
    return f"{sign}${usd:,.2f}".replace(",", " ")


def clock(value: datetime | float | None, now: datetime | None = None) -> str:
    """09:07 today, 12.11 09:07 this year, 12.11.25 otherwise (local time zone)."""
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        value = datetime.fromtimestamp(value, tz=timezone.utc)
    local = value.astimezone()
    current = (now or datetime.now()).astimezone()
    if local.date() == current.date():
        return local.strftime("%H:%M")
    if local.year == current.year:
        return local.strftime("%d.%m %H:%M")
    return local.strftime("%d.%m.%y")


def folder_label(path: str | Path | None) -> str:
    """Readable project name instead of a full path: `telegram_BOT\\claude_remote_bot`, `memory`, `~`.

    The last two folders are shown (relative to the home folder when inside it). Encoded Claude cache
    folders (`C--Users-...`) and session UUIDs are never shown.
    """
    if not path:
        return "папка не выбрана"
    raw = str(path).rstrip("\\/")
    win = PureWindowsPath(raw)
    windows = bool(win.drive)
    parts = list(win.parts[1:]) if windows else [p for p in Path(raw).parts if p != "/"]
    home = PureWindowsPath(str(Path.home())) if windows else Path.home()
    home_parts = [p.lower() for p in (home.parts[1:] if windows else home.parts[1:])]
    lowered = [p.lower() for p in parts]
    if lowered == home_parts:
        return "~"
    if home_parts and lowered[: len(home_parts)] == home_parts:
        parts = parts[len(home_parts):]
    clean = [p for p in parts if not _ENCODED_DIR_RE.match(p) and not _UUID_RE.match(p)]
    if not clean:
        return win.drive or raw
    if ".claude" in (p.lower() for p in parts):
        return clean[-1]
    return ("\\" if windows else "/").join(clean[-2:])


def model_name(model: str | None) -> str:
    """`claude-opus-5-5-20261001` -> `Opus 5.5`, `claude-3-5-sonnet` -> `Sonnet 3.5`, `opus` -> `Opus`."""
    canonical = normalize_model_id(model)
    if not canonical or canonical.startswith("<"):
        return ""
    if canonical in ("default", "auto"):
        return "Авто"
    match = _MODEL_RE.match(canonical)
    if match is None:
        return canonical.capitalize() if canonical.isalpha() else canonical
    old_major, old_minor, family, major, minor = match.groups()
    if old_major is not None:
        version = f"{old_major}.{old_minor}"
    elif major is not None:
        version = f"{major}.{minor}" if minor is not None else major
    else:
        version = ""
    return f"{family.capitalize()} {version}".strip()


def count(n: int, one: str, few: str, many: str) -> str:
    """`3 чата`, `1 файл`, `12 проектов`."""
    return f"{n} {plural(n, one, few, many)}"
