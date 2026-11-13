"""Design system: Markdown -> Telegram HTML, message limits, clean report cards, keyboards, safe delivery."""

from __future__ import annotations

import asyncio
import re
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText, SendDocument, SendMessage
from conftest import SESSION_ID

from src.handlers.delivery import edit_html, send_card, send_html
from src.handlers.runner import render_final_head, render_final_tail, render_status
from src.keyboards.inline import (
    NAV_BUTTONS,
    main_reply_kb,
    mode_kb,
    push_report_kb,
    run_done_kb,
    session_report_kb,
    status_kb,
    usage_kb,
)
from src.services.claude_runner import RunRequest, RunResult, RunState, RunStatus
from src.services.session_parser import REPORT_PUSH, SessionReport, render_report_html
from src.services.state import BotState, RunMode
from src.ui import theme
from src.ui.formatter import answer_parts, fit_html, md_to_html, plain_fallback, tg_size, visible_text

TG_LIMIT = 4096
ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a", "blockquote",
                "tg-spoiler", "span"}
BANNED = ("Затронутые файлы", "Команды терминала", "Новых запросов пользователя нет", "cost-state",
          ".claude\\projects", "C--Users")
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

CLAUDE_ANSWER = """## Итог

Исправил **два бага** в `scraper.py` и *обновил* тесты: my_var_name, 2 * 3 * 4, <tag> & "кавычки".

- [x] поиск через API
- [ ] ~~DOM-карточки~~
  - вложенный пункт
1. первый

> цитата с **жирным**

| Метрика | До | После |
|---|---:|---|
| Поиск | 54 с | **1,8 с** |

```python
def f(x):
    return x < 1 and "&"
```
Ссылки: [доки](https://example.com/a?b=1&c=2) и [плохая](javascript:alert(1))
"""


class TelegramHtmlChecker(HTMLParser):
    """What Telegram's HTML parse mode accepts: known tags, balanced, no nested quotes, nothing inside <pre>
    except one <code>."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.errors: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in ALLOWED_TAGS:
            self.errors.append(f"unsupported <{tag}>")
        if tag == "blockquote" and "blockquote" in self.stack:
            self.errors.append("nested blockquote")
        if self.stack and self.stack[-1] == "code" and "pre" in self.stack:
            self.errors.append(f"<{tag}> inside pre/code")
        if tag == "a" and not any(k == "href" and v and v.startswith(("http", "tg:", "mailto:")) for k, v in attrs):
            self.errors.append("unsafe link")
        self.stack.append(tag)

    def handle_endtag(self, tag: str) -> None:
        if not self.stack or self.stack[-1] != tag:
            self.errors.append(f"unbalanced </{tag}> (open: {self.stack})")
            return
        self.stack.pop()


def assert_telegram_html(text: str) -> None:
    checker = TelegramHtmlChecker()
    checker.feed(text)
    checker.close()
    assert not checker.errors and not checker.stack, (checker.errors, checker.stack, text[:300])
    assert re.search(r"&(?!(amp|lt|gt|quot|#x27);)", text) is None, "raw & in HTML"
    assert len(visible_text(text).encode("utf-16-le")) // 2 <= TG_LIMIT


# ---------- formatter ----------

def test_markdown_to_telegram_html() -> None:
    html = md_to_html(CLAUDE_ANSWER)
    assert_telegram_html(html)
    assert "◈ <b>Итог</b>" in html
    assert "<b>два бага</b>" in html and "<code>scraper.py</code>" in html and "<i>обновил</i>" in html
    assert "my_var_name" in html and "2 * 3 * 4" in html and "&lt;tag&gt; &amp;" in html
    assert "☑ поиск через API" in html and "☐ <s>DOM-карточки</s>" in html and "  ◦ вложенный пункт" in html
    assert "▎ <i>цитата с <b>жирным</b></i>" in html and html.count("<blockquote") == 0
    assert '<pre><code class="language-python">def f(x):\n    return x &lt; 1 and "&amp;"</code></pre>' in html
    assert "<pre>Метрика  До    После\n" in html and "Поиск    54 с  1,8 с</pre>" in html
    assert '<a href="https://example.com/a?b=1&amp;c=2">доки</a>' in html
    assert "javascript" not in html.split("плохая")[0] and "<a href=\"javascript" not in html


def test_inline_patterns_never_cross() -> None:
    for text in ("**a *b** c*", "__a **b__ c**", "*a `b* c`", "**[x](https://e.com)**", "~~a **b~~ c**",
                 "`unclosed", "**", "* * *", "_a_b_", "***x***", "[a](b)", "<b>raw</b>"):
        assert_telegram_html(md_to_html(text))
    assert md_to_html("<b>raw</b>") == "&lt;b&gt;raw&lt;/b&gt;"
    assert md_to_html("***x***") == "<b><i>x</i></b>"


def test_long_answer_is_split_into_balanced_collapsed_parts() -> None:
    parts = answer_parts(CLAUDE_ANSWER * 60, 1500, 3800)
    assert len(parts) > 3
    assert tg_size(parts[0]) <= 1500 and all(tg_size(p) <= 3800 for p in parts[1:])
    for part in parts:
        assert part.startswith("<blockquote expandable>") and part.endswith("</blockquote>")
        assert_telegram_html(part)
    short = answer_parts("Готово ✅", 1000)
    assert short == ["<blockquote>Готово ✅</blockquote>"]


def test_huge_code_block_and_emoji_respect_the_utf16_limit() -> None:
    code = "```\n" + "\n".join("x = '" + "y" * 300 + "'" for _ in range(60)) + "\n```"
    for part in answer_parts(code, 4000):
        assert_telegram_html(part)
    for text in ("😀" * 5000, "&" * 9000, "я" * 9000):
        parts = answer_parts(text, 4000)
        assert len(parts) >= 2 and all(parts) and all(tg_size(p) <= 4000 for p in parts)
        assert "".join(visible_text(p) for p in parts) == text
        for part in parts:
            assert_telegram_html(part)
    squeezed = answer_parts(CLAUDE_ANSWER, 40, 4000)  # a big card header leaves no room
    assert squeezed[0] == "" and squeezed[1].startswith("<blockquote")


def test_fit_html_cuts_only_between_top_level_lines() -> None:
    body = theme.quote("\n".join(f"строка {i}" for i in range(2000)))
    text = theme.join_blocks("✦ <b>Шапка</b>", body, "<i>подвал</i>")
    fitted = fit_html(text, 4000)
    assert_telegram_html(fitted)
    assert fitted == "✦ <b>Шапка</b>\n…"  # the quote cannot be cut inside, so it is dropped whole
    assert plain_fallback("<b>a &amp; b</b>") == "a &amp; b"


# ---------- theme ----------

def test_theme_helpers() -> None:
    home = Path.home()
    assert theme.folder_label(home) == "~"
    assert theme.folder_label(home / ".claude" / "projects" / "C--Users-x" / "memory") == "memory"
    assert theme.folder_label(r"C:\Hakatons\telegram_BOT\claude_remote_bot") == "telegram_BOT\\claude_remote_bot"
    assert theme.folder_label(home / "Documents" / "lab") == "Documents\\lab"
    assert theme.folder_label(None) == "папка не выбрана"
    assert [theme.model_name(m) for m in ("claude-opus-5-5-20261001", "claude-3-5-sonnet", "opus", "claude-fable-5-1")] \
        == ["Opus 5.5", "Sonnet 3.5", "Opus", "Fable 5.1"]
    assert [theme.duration(s) for s in (45, 185, 725, 4320)] == ["45 сек", "3 мин 5 сек", "12 мин", "1 ч 12 мин"]
    assert theme.money(0.183) == "~$0.18" and theme.money(0.004) == "<$0.01" and theme.money(None) == ""
    assert theme.bar(35) == "▰▰▰▰▱▱▱▱▱▱▱▱" and theme.level_dot(95) == theme.DOT_ERROR


# ---------- report cards ----------

def _noisy_report(tmp_path: Path, **kw: Any) -> SessionReport:
    report = SessionReport(session_id=SESSION_ID, path=tmp_path / "x.jsonl", project_key="C--Users-alice-proj",
                           cwd=str(Path.home() / "proj"), title="Починка тестов", **kw)
    report.prompts = ["Почини тесты"]
    report.files = {r"C:\p\src\app.py": Counter({"Edit": 2})}
    report.commands = [("Bash", "pytest -q"), ("PowerShell", "Get-ChildItem")]
    report.final_answer = CLAUDE_ANSWER
    report.assistant_messages = 4
    report.last_model = "claude-opus-5-5"
    report.estimated_cost = 0.18
    return report


def test_report_card_has_no_technical_noise(tmp_path: Path) -> None:
    report = _noisy_report(tmp_path)  # no cost-state yet: the estimate is shown
    for kind in ("view", REPORT_PUSH):
        html = render_report_html(report, kind=kind)
        print(f"\n--- {kind} card ---\n{html}")
        assert_telegram_html(html)
        assert not any(word in html for word in BANNED) and not UUID_RE.search(html)
        assert "app.py" not in html and "pytest -q" not in html and "Get-ChildItem" not in html
        assert html.startswith("✦ <b>Починка тестов</b>\n<i>")
        assert "Рабочая папка: proj" in html and "<b>Запрос:</b> <i>Почини тесты</i>" in html
        assert "<blockquote expandable>" in html and html.endswith("<i>Opus 5.5 · ~$0.18</i>")
    assert "Обновление на ПК" in render_report_html(report, kind=REPORT_PUSH)


def test_delta_report_prices_only_the_new_part(tmp_path: Path) -> None:
    report = _noisy_report(tmp_path, is_delta=True)
    report.cost_usd, report.cost_is_final = 25.0, True  # cumulative for the whole session
    assert report.display_cost == 0.18
    full = _noisy_report(tmp_path)
    full.cost_usd, full.cost_is_final = 1.25, True
    assert full.display_cost == 1.25
    full.cost_is_final = False
    assert full.display_cost == 1.25  # max of the stale total and the estimate


def test_report_without_answer_or_prompts(tmp_path: Path) -> None:
    report = SessionReport(session_id=SESSION_ID, path=tmp_path / "x.jsonl", project_key="k", cwd=None)
    report.assistant_messages = 2
    html = render_report_html(report, kind=REPORT_PUSH)
    assert_telegram_html(html)
    assert "Новых запросов" not in html and "Claude еще работает" in html


def _state(tmp_path: Path, status: RunStatus, text: str = CLAUDE_ANSWER, **kw: Any) -> RunState:
    request = RunRequest(prompt="Почини тесты", cwd=tmp_path, mode=RunMode.RESUME, session_id=SESSION_ID,
                         skip_permissions=True, model="opus")
    state = RunState(run_id="ab12cd34", request=request, status=status, model="claude-opus-5-5",
                     session_id=SESSION_ID, **kw)
    state.result = RunResult(subtype="success", is_error=False, text=text, cost_usd=0.183, session_id=SESSION_ID,
                             num_turns=3, duration_ms=45_000, permission_denials=[], terminal_reason=None)
    state.finished_at = state.started_at + 45
    return state


def test_run_cards(tmp_path: Path) -> None:
    state = _state(tmp_path, RunStatus.FINISHED)
    state.touched_files = {"a": tmp_path / "a.py"}
    head = render_final_head(state, "Починка тестов")
    tail = render_final_tail(state)
    print(f"\n--- final card ---\n{head}\n…\n{tail}")
    assert head.startswith("✦ <b>Починка тестов</b>\n<i>Рабочая папка: ")
    assert "<b>Запрос:</b> <i>Почини тесты</i>" in head
    assert tail == "<i>Opus 5.5 · 45 сек · ~$0.18</i>"
    for text in (head, tail):
        assert not UUID_RE.search(text) and not any(word in text for word in BANNED)
    failed = _state(tmp_path, RunStatus.FAILED, error="Traceback <boom> & co")
    assert render_final_head(failed, "x").startswith("🔴 <b>x</b>\n<i>Ошибка · ")
    assert "<pre>Traceback &lt;boom&gt; &amp; co</pre>" in render_final_tail(failed)

    live = _state(tmp_path, RunStatus.RUNNING, activity="📝 Редактирование файла src\\main.py")
    live.history.extend(["📖 Чтение файла a.py", "⚙️ Выполнение команды: git status", "📝 Редактирование файла src\\main.py"])
    html = render_status(live)
    print(f"\n--- live card ---\n{html}")
    assert_telegram_html(html)
    assert html.startswith("🟡 <b>Claude работает</b> · 45 сек") and "Opus 5.5" in html
    assert "▸ <b>Сейчас:</b> 📝 Редактирование файла" in html and not UUID_RE.search(html)


class FakeBot:
    """Records sends; optionally rejects the first HTML message like Telegram does on broken entities."""

    def __init__(self, reject_first: bool = False) -> None:
        self.sent: list[dict[str, Any]] = []
        self.documents: list[dict[str, Any]] = []
        self.reject = reject_first

    async def send_message(self, chat_id: int, text: str, **kw: Any) -> Any:
        if self.reject:
            self.reject = False
            raise TelegramBadRequest(method=SendMessage(chat_id=chat_id, text=text),
                                     message="Bad Request: can't parse entities: unexpected end tag")
        self.sent.append({"text": text, **kw})
        return None

    async def edit_message_text(self, text: str, **kw: Any) -> Any:
        if self.reject:
            self.reject = False
            raise TelegramBadRequest(method=EditMessageText(text=text), message="Bad Request: can't parse entities")
        self.sent.append({"text": text, **kw})
        return None

    async def send_document(self, chat_id: int, document: Any, **kw: Any) -> Any:
        self.documents.append({"document": document, **kw})
        return None


def test_send_card_splits_and_keeps_the_keyboard_last() -> None:
    bot = FakeBot()
    markup = run_done_kb("ab12cd34", 2, SESSION_ID)
    asyncio.run(send_card(bot, 1, "✦ <b>T</b>", CLAUDE_ANSWER * 25, "<i>Opus 5.5</i>", markup, max_messages=6))
    assert 2 <= len(bot.sent) <= 6 and not bot.documents
    for message in bot.sent:
        assert_telegram_html(message["text"])
    assert bot.sent[0]["text"].startswith("✦ <b>T</b>") and bot.sent[-1]["text"].endswith("<i>Opus 5.5</i>")
    assert [m.get("reply_markup") for m in bot.sent] == [None] * (len(bot.sent) - 1) + [markup]

    bot = FakeBot()
    asyncio.run(send_card(bot, 1, "✦ <b>T</b>", CLAUDE_ANSWER * 200, "<i>f</i>", markup, max_messages=3))
    assert len(bot.sent) == 1 and len(bot.documents) == 1
    assert "полностью в файле ниже" in bot.sent[0]["text"] and bot.sent[0]["reply_markup"] is markup
    assert_telegram_html(bot.sent[0]["text"])


def test_rejected_html_falls_back_to_plain_text() -> None:
    bot = FakeBot(reject_first=True)
    asyncio.run(send_html(bot, 1, "<b>жирный</b> &amp; <i>текст</i>"))
    assert bot.sent[0]["text"] == "жирный &amp; текст"
    bot = FakeBot(reject_first=True)
    assert asyncio.run(edit_html(bot, 1, 2, "<b>x</b>")) and bot.sent[0]["text"] == "x"


# ---------- keyboards ----------

def _labels(markup: Any) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


def test_inline_keyboards_never_repeat_navigation() -> None:
    reply = [b.text for row in main_reply_kb().keyboard for b in row]
    assert reply == list(NAV_BUTTONS) == ["💬 Сессии", "📁 Файлы", "📊 Лимиты", "⚙️ Настройки"]
    markups = [
        status_kb([]), status_kb(["ab12cd34"]), mode_kb(BotState(), True), usage_kb("main"), usage_kb("days7"),
        session_report_kb(SESSION_ID, "tok", 3, True), push_report_kb(SESSION_ID),
        run_done_kb("ab12cd34", 1, SESSION_ID, True),
    ]
    legacy = {"📂 Сессии", "🗂 Файлы", "⚙️ Режим", "📊 Статус"}
    for markup in markups:
        labels = _labels(markup)
        assert not set(labels) & (set(NAV_BUTTONS) | legacy), labels
        for row in markup.inline_keyboard:
            for button in row:
                assert len(button.callback_data.encode()) <= 64
    assert _labels(status_kb(["ab12cd34"])) == ["🛑 Остановить ab12cd34", "🔄 Обновить", "♻️ Перезапустить бота"]


# ---------- every screen through the real Dispatcher ----------

@pytest.mark.parametrize("command", ["/start", "/help", "/status", "/mode", "/usage", "/sessions", "/files",
                                     "/model", "💬 Сессии", "📁 Файлы", "📊 Лимиты", "⚙️ Настройки",
                                     "📂 Сессии", "📊 Статус"])
def test_screens_render_valid_clean_html(harness_factory, command: str) -> None:
    h = harness_factory()

    async def scenario() -> None:
        await h.ctx.store.load()
        await h.ctx.store.update(active_session_id=SESSION_ID, active_session_title="Починка тестов",
                                 active_session_cwd=str(h.ctx.safe.roots[0]), active_cwd=str(h.ctx.safe.roots[0]),
                                 run_mode=RunMode.RESUME)
        await h.text(command)

    asyncio.run(scenario())
    sent = [c for c in h.session.calls if isinstance(c, (SendMessage, SendDocument))]
    assert sent, command
    for call in sent:
        text = getattr(call, "text", None) or getattr(call, "caption", None) or ""
        print(f"\n--- {command} ---\n{text}")
        assert_telegram_html(text)
        assert not UUID_RE.search(text), text
        assert not any(word in text for word in BANNED), text
        markup = getattr(call, "reply_markup", None)
        if markup is not None and hasattr(markup, "inline_keyboard"):
            assert not set(_labels(markup)) & set(NAV_BUTTONS), _labels(markup)
