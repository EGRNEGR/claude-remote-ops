"""Markdown as Claude writes it -> Telegram HTML that always parses and always fits.

Supported: fenced code (```lang), `inline code`, **bold** / __bold__, *italic* / _italic_, ~~strike~~,
[links](https://...), # headings, - / * / 1. lists, [ ] / [x] checkboxes, > quotes, --- rules and pipe tables
(rendered as aligned monospace). Everything else is escaped text.

Guarantees used by the rest of the bot:
  * every unit (one line or one code block) is balanced HTML on its own, so cutting between units never
    breaks a tag; inline patterns never cross each other because the content of a later pattern may not
    contain `<` / `>` (all text is escaped first, so those characters only come from our own tags);
  * Markdown quotes become `▎` lines, never `<blockquote>`, because Telegram forbids nested quotes and the
    answer itself is shown inside `<blockquote expandable>`;
  * sizes are measured as Telegram does: UTF-16 code units (an emoji counts twice), with the raw HTML length
    as an upper bound of the visible text.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass

from src.ui.theme import BULLET, DIVIDER, MARK_SECTION, quote
from src.utils.text import TG_SAFE_LIMIT, esc, esc_fit

QUOTE_OVERHEAD = len("<blockquote expandable></blockquote>")
COLLAPSE_CHARS = 280
COLLAPSE_LINES = 4
TABLE_CELL_MAX = 24

_FENCE_OPEN_RE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([^\s`~]*)[^`]*$")
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_HR_RE = re.compile(r"^\s{0,3}([-*_])(?:\s*\1){2,}\s*$")
_BULLET_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_ORDERED_RE = re.compile(r"^(\s*)(\d{1,3})[.)]\s+(.*)$")
_QUOTE_RE = re.compile(r"^\s{0,3}>\s?(.*)$")
_CHECKBOX_RE = re.compile(r"^\[([ xX])\]\s+")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{2,}:?\s*(?:\|\s*:?-{2,}:?\s*)*\|?\s*$")

_CODE_SPAN_RE = re.compile(r"(`+)(?!`)(.+?)(?<!`)\1(?!`)")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\(([^)\s]+)(?:\s+\"[^\"\n]*\")?\)")
_BOLD_ITALIC_RE = re.compile(r"\*\*\*(?=[^\s*])([^*\n]+?)(?<=[^\s*])\*\*\*")
_BOLD_STAR_RE = re.compile(r"\*\*(?=\S)(.+?)(?<=\S)\*\*")
_BOLD_UNDER_RE = re.compile(r"(?<!\w)__(?=\S)([^<>\n]+?)(?<=\S)__(?!\w)")
_ITALIC_STAR_RE = re.compile(r"(?<![\w*\\])\*(?=[^\s*])([^*\n<>]+?)(?<=[^\s*])\*(?![\w*])")
_ITALIC_UNDER_RE = re.compile(r"(?<![\w\\])_(?=[^\s_])([^_\n<>]+?)(?<=[^\s_])_(?!\w)")
_STRIKE_RE = re.compile(r"~~(?=\S)([^~<>\n]+?)(?<=\S)~~")
_PLACEHOLDER_RE = re.compile("\x00(\\d+)\x00")
_TAG_RE = re.compile(r"<(/?)([a-zA-Z][\w-]*)[^>]*>")
_LANG_RE = re.compile(r"^[\w+#.-]{1,24}$")
_SAFE_SCHEMES = ("http://", "https://", "tg://", "mailto:")
_MD_CELL_RE = re.compile(r"\*\*|__|`")


@dataclass(frozen=True, slots=True)
class Unit:
    """One balanced piece of HTML: a text line, a blank line or a whole code block."""

    html: str
    kind: str = "text"  # text | blank | pre
    code: str = ""      # raw code of a pre unit (to split a huge block by lines)
    lang: str = ""


# ---------- measuring ----------

def tg_size(text: str) -> int:
    """Upper bound of what Telegram counts: raw length plus one extra unit per astral character."""
    return len(text) + sum(1 for ch in text if ord(ch) > 0xFFFF)


def visible_text(html_text: str) -> str:
    """Text the user sees (tags removed, entities decoded)."""
    return html.unescape(_TAG_RE.sub("", html_text))


def plain_fallback(html_text: str, limit: int = TG_SAFE_LIMIT) -> str:
    """Same content without any markup: the last resort when Telegram rejects the entities."""
    return esc_fit(visible_text(html_text), limit)


# ---------- inline markdown ----------

def _link_html(label: str, url: str) -> str:
    if not url.lower().startswith(_SAFE_SCHEMES):
        return esc(f"{label} ({url})")
    return f'<a href="{html.escape(url, quote=True)}">{esc(label)}</a>'


def inline_html(text: str) -> str:
    """Escape one line and convert inline Markdown into Telegram HTML."""
    stash: list[str] = []

    def keep(fragment: str) -> str:
        stash.append(fragment)
        return f"\x00{len(stash) - 1}\x00"

    text = text.replace("\x00", "")
    text = _CODE_SPAN_RE.sub(lambda m: keep(f"<code>{esc(m.group(2).strip() or m.group(2))}</code>"), text)
    text = _LINK_RE.sub(lambda m: keep(_link_html(m.group(1), m.group(2))), text)
    out = esc(text)
    out = _BOLD_ITALIC_RE.sub(r"<b><i>\1</i></b>", out)
    out = _BOLD_STAR_RE.sub(r"<b>\1</b>", out)
    out = _BOLD_UNDER_RE.sub(r"<b>\1</b>", out)
    out = _ITALIC_STAR_RE.sub(r"<i>\1</i>", out)
    out = _ITALIC_UNDER_RE.sub(r"<i>\1</i>", out)
    out = _STRIKE_RE.sub(r"<s>\1</s>", out)
    return _PLACEHOLDER_RE.sub(lambda m: stash[int(m.group(1))], out)


# ---------- blocks ----------

def _pre_unit(code: str, lang: str = "") -> Unit:
    lang = lang if _LANG_RE.match(lang or "") else ""
    body = esc(code)
    markup = f'<pre><code class="language-{lang}">{body}</code></pre>' if lang else f"<pre>{body}</pre>"
    return Unit(markup, "pre", code, lang)


def _split_row(line: str) -> list[str]:
    row = line.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [_MD_CELL_RE.sub("", cell).strip() for cell in row.split("|")]


def _clip(cell: str) -> str:
    return cell if len(cell) <= TABLE_CELL_MAX else cell[: TABLE_CELL_MAX - 1] + "…"


def _table_unit(rows: list[str]) -> Unit:
    header, body = _split_row(rows[0]), [_split_row(r) for r in rows[2:]]
    width = max(len(header), *(len(r) for r in body)) if body else len(header)
    grid = [[_clip(c) for c in (r + [""] * width)[:width]] for r in [header, *body]]
    widths = [max(len(row[i]) for row in grid) for i in range(width)]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip() for row in grid]
    lines.insert(1, "  ".join("─" * w for w in widths))
    return _pre_unit("\n".join(lines))


def _list_prefix(indent: str) -> str:
    level = len(indent.replace("\t", "  ")) // 2
    return "  " * level


def _line_unit(line: str) -> Unit:
    if not line.strip():
        return Unit("", "blank")
    if _HR_RE.match(line):
        return Unit(DIVIDER)
    heading = _HEADING_RE.match(line)
    if heading:
        level, text = len(heading.group(1)), heading.group(2)
        marker = f"{MARK_SECTION} " if level <= 2 else ""
        return Unit(f"{marker}<b>{inline_html(text)}</b>")
    bullet = _BULLET_RE.match(line)
    if bullet:
        indent, text = bullet.groups()
        box = _CHECKBOX_RE.match(text)
        if box:
            mark = "☑" if box.group(1).lower() == "x" else "☐"
            return Unit(f"{_list_prefix(indent)}{mark} {inline_html(text[box.end():])}")
        mark = BULLET if not _list_prefix(indent) else "◦"
        return Unit(f"{_list_prefix(indent)}{mark} {inline_html(text)}")
    ordered = _ORDERED_RE.match(line)
    if ordered:
        indent, number, text = ordered.groups()
        return Unit(f"{_list_prefix(indent)}{number}. {inline_html(text)}")
    quoted = _QUOTE_RE.match(line)
    if quoted:
        inner = quoted.group(1)
        return Unit(f"▎ <i>{inline_html(inner)}</i>" if inner.strip() else "▎")
    return Unit(inline_html(line.rstrip()))


def _dedent(lines: list[str], indent: int) -> list[str]:
    return [ln[indent:] if ln[:indent].strip() == "" else ln.lstrip() for ln in lines]


def md_units(text: str) -> list[Unit]:
    """Split Markdown into balanced units; blank runs collapse to one, edges are trimmed."""
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n").expandtabs(4)
    lines = raw.split("\n")
    units: list[Unit] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        fence = _FENCE_OPEN_RE.match(line)
        if fence:
            indent, marks, lang = fence.groups()
            body: list[str] = []
            i += 1
            while i < len(lines):
                closing = lines[i].strip()
                if closing.startswith(marks[0] * len(marks)) and not closing.strip(marks[0]):
                    break
                body.append(lines[i])
                i += 1
            i += 1  # skip the closing fence (or the end of text for an unclosed block)
            units.append(_pre_unit("\n".join(_dedent(body, len(indent))).rstrip("\n"), lang))
            continue
        if "|" in line and i + 1 < len(lines) and _TABLE_SEP_RE.match(lines[i + 1]) and "|" in lines[i + 1]:
            rows = [line, lines[i + 1]]
            i += 2
            while i < len(lines) and "|" in lines[i] and lines[i].strip():
                rows.append(lines[i])
                i += 1
            units.append(_table_unit(rows))
            continue
        units.append(_line_unit(line))
        i += 1
    cleaned: list[Unit] = []
    for unit in units:
        if unit.kind == "blank" and (not cleaned or cleaned[-1].kind == "blank"):
            continue
        cleaned.append(unit)
    while cleaned and cleaned[-1].kind == "blank":
        cleaned.pop()
    return cleaned


def md_to_html(text: str) -> str:
    return "\n".join(u.html for u in md_units(text))


# ---------- packing into messages ----------

def _slices(text: str, budget: int, overhead: int) -> list[str]:
    """Cut raw text into pieces whose escaped form plus `overhead` stays within `budget` Telegram units."""
    pieces: list[str] = []
    start, size = 0, overhead
    for index, ch in enumerate(text):
        cost = tg_size(esc(ch))
        if size + cost > budget and index > start:
            pieces.append(text[start:index])
            start, size = index, overhead
        size += cost
    pieces.append(text[start:])
    return pieces


def _split_oversized(unit: Unit, budget: int) -> list[Unit]:
    """Units bigger than one message: text loses inline markup, code is re-wrapped per piece."""
    if tg_size(unit.html) + 1 <= budget:
        return [unit]
    if unit.kind != "pre":
        return [Unit(esc(piece)) for piece in _slices(visible_text(unit.html), budget - 1, 0)]
    wrapper = tg_size(_pre_unit("", unit.lang).html) + 1
    pieces: list[Unit] = []
    current: list[str] = []
    for code_line in unit.code.split("\n"):
        candidate = "\n".join([*current, code_line])
        if current and tg_size(_pre_unit(candidate, unit.lang).html) + 1 > budget:
            pieces.append(_pre_unit("\n".join(current), unit.lang))
            current = []
        if tg_size(_pre_unit(code_line, unit.lang).html) + 1 > budget:
            pieces.extend(_pre_unit(piece, unit.lang) for piece in _slices(code_line, budget, wrapper))
            continue
        current.append(code_line)
    if current:
        pieces.append(_pre_unit("\n".join(current), unit.lang))
    return pieces


def pack(units: list[Unit], first_budget: int, next_budget: int = TG_SAFE_LIMIT) -> list[str]:
    """Group units into HTML chunks: the first <= first_budget, the others <= next_budget.

    The first chunk is "" when the first unit does not fit into a smaller first_budget (a card header left
    no room): the answer then starts in the next message.
    """
    expanded = [piece for unit in units for piece in _split_oversized(unit, next_budget)]
    chunks: list[str] = []
    current: list[Unit] = []
    size = 0
    budget = first_budget

    def flush() -> None:
        while current and current[-1].kind == "blank":
            current.pop()
        chunks.append("\n".join(u.html for u in current))
        current.clear()

    for unit in expanded:
        if unit.kind == "blank" and not current:
            continue
        cost = tg_size(unit.html) + 1
        if size + cost > budget:
            if current:
                flush()
            elif not chunks and first_budget < next_budget:
                chunks.append("")
            budget, size = next_budget, 0
            if unit.kind == "blank":
                continue
        current.append(unit)
        size += cost
    if current:
        flush()
    while chunks and not chunks[-1]:
        chunks.pop()
    return chunks


def should_collapse(body_html: str) -> bool:
    visible = visible_text(body_html)
    return len(visible) > COLLAPSE_CHARS or visible.count("\n") >= COLLAPSE_LINES


def answer_parts(text: str, first_budget: int, next_budget: int = TG_SAFE_LIMIT) -> list[str]:
    """Claude's answer as quote blocks: [first part for the card, continuation parts...].

    Long parts are `<blockquote expandable>` (the start stays visible, one tap opens the rest), short ones a
    plain quote. An empty first part means the card itself had no room for the answer.
    """
    chunks = pack(md_units(text), first_budget - QUOTE_OVERHEAD, next_budget - QUOTE_OVERHEAD)
    return [quote(chunk, should_collapse(chunk)) if chunk else "" for chunk in chunks]


def fit_html(text: str, limit: int = TG_SAFE_LIMIT) -> str:
    """Cut a rendered message at the last top-level line break that fits, so no tag is ever split."""
    if tg_size(text) <= limit:
        return text
    depth = 0
    cuts: list[int] = []
    pos = 0
    for match in _TAG_RE.finditer(text):
        if depth == 0:
            cuts.extend(k for k in _newlines(text, pos, match.start()))
        depth += -1 if match.group(1) else 1
        pos = match.end()
    if depth == 0:
        cuts.extend(_newlines(text, pos, len(text)))
    for cut in reversed(cuts):
        head = text[:cut].rstrip()
        if head and tg_size(head) + 2 <= limit:
            return head + "\n…"
    return plain_fallback(text, limit)


def _newlines(text: str, start: int, end: int) -> list[int]:
    return [k for k in range(start, end) if text[k] == "\n"]
