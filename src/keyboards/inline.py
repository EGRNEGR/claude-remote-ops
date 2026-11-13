"""Inline / reply keyboards and compact CallbackData (Telegram limit: 64 bytes per callback).

Navigation rule: the persistent reply keyboard owns the four main sections (💬 Сессии, 📁 Файлы, 📊 Лимиты,
⚙️ Настройки); inline keyboards under a message hold only the actions of that screen and never repeat them.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.services.file_manager import EntryInfo
from src.services.reasoning import EFFORT_BUTTONS, EFFORT_TITLES, MODEL_DEFAULT, MODEL_PRESETS, MODEL_TITLES
from src.services.session_parser import ProjectInfo, SessionMeta
from src.services.state import (
    PROFILE_LIMITS,
    BgSessionPolicy,
    BotState,
    LimitProfile,
    RunMode,
)
from src.ui.theme import folder_label
from src.utils.registry import TokenRegistry
from src.utils.text import fmt_short_dt, human_size, one_line, truncate, truncate_middle

T = TypeVar("T")

BTN_SESSIONS = "💬 Сессии"
BTN_FILES = "📁 Файлы"
BTN_USAGE = "📊 Лимиты"
BTN_SETTINGS = "⚙️ Настройки"
BTN_MODE = BTN_SETTINGS
NAV_BUTTONS = (BTN_SESSIONS, BTN_FILES, BTN_USAGE, BTN_SETTINGS)

# Labels of the previous keyboard: a phone keeps showing it until /start, so they still work.
LEGACY_SESSIONS = "📂 Сессии"
LEGACY_FILES = "🗂 Файлы"
LEGACY_MODE = "⚙️ Режим"
LEGACY_STATUS = "📊 Статус"
LEGACY_USAGE = "📊 Расход и лимиты"
BTN_STATUS = LEGACY_STATUS

SESSIONS_TEXTS = frozenset({BTN_SESSIONS, LEGACY_SESSIONS})
FILES_TEXTS = frozenset({BTN_FILES, LEGACY_FILES})
USAGE_TEXTS = frozenset({BTN_USAGE, LEGACY_USAGE})
SETTINGS_TEXTS = frozenset({BTN_SETTINGS, LEGACY_MODE})
STATUS_TEXTS = frozenset({LEGACY_STATUS})
REPLY_BUTTONS = tuple(SESSIONS_TEXTS | FILES_TEXTS | USAGE_TEXTS | SETTINGS_TEXTS | STATUS_TEXTS)

BTN_RESTART = "♻️ Перезапустить бота"
BTN_MODEL = "🧠 Модель и ризонинг"
BTN_REFRESH = "🔄 Обновить"
BTN_BACK = "⬅️ Назад"

RUN_MODE_BUTTONS: dict[RunMode, str] = {
    RunMode.NEW: "🆕 Новый",
    RunMode.CONTINUE: "⏩ Последний",
    RunMode.RESUME: "🔁 Выбранный",
}
PERM_FULL_TITLE = "🔓 Полный доступ"
PERM_RESTRICTED_TITLE = "🔒 Ограниченный"
BG_POLICY_BUTTONS: dict[BgSessionPolicy, str] = {
    BgSessionPolicy.ASK: "❓ Спросить",
    BgSessionPolicy.FORK: "🌿 Сразу копия",
}
CHECK = "✅ "


class MenuCb(CallbackData, prefix="mn"):
    action: str


class ProjCb(CallbackData, prefix="pj"):
    action: str  # list | open | cwd
    token: str = ""
    page: int = 0


class SessCb(CallbackData, prefix="ss"):
    action: str  # open | view | resume | md | files
    sid: str


class FileCb(CallbackData, prefix="fb"):
    action: str  # roots | dir | file | dl | pv | ask | cwd
    token: str = ""
    page: int = 0


class RunCb(CallbackData, prefix="rn"):
    action: str  # kill | files | retry | fork | bgstop
    run_id: str


class ModeCb(CallbackData, prefix="md"):
    action: str  # new | continue | resume | perm_full | perm_restricted | lim_<profile> | bg_<policy> | watch


class ModelCb(CallbackData, prefix="ml"):
    kind: str   # m = model | e = reasoning effort
    value: str  # sonnet | opus | haiku | default / off | low | medium | high | max | default


class UsageCb(CallbackData, prefix="us"):
    action: str  # refresh | days7 | days30 | top | compact | newchat | back


class UpCb(CallbackData, prefix="up"):
    action: str  # save | save_run | cancel


class VoiceCb(CallbackData, prefix="vc"):
    action: str  # run | cancel


def paginate(items: Sequence[T], page: int, size: int) -> tuple[list[T], int, int]:
    pages = max(1, math.ceil(len(items) / size))
    page = min(max(0, page), pages - 1)
    return list(items[page * size:(page + 1) * size]), page, pages


def _nav_row(
    builder: InlineKeyboardBuilder,
    page: int,
    pages: int,
    make: Callable[[int], CallbackData],
) -> None:
    if pages <= 1:
        return
    buttons = []
    if page > 0:
        buttons.append(("◀️", make(page - 1)))
    buttons.append((f"{page + 1} / {pages}", make(page)))
    if page < pages - 1:
        buttons.append(("▶️", make(page + 1)))
    row = InlineKeyboardBuilder()
    for text, cb in buttons:
        row.button(text=text, callback_data=cb)
    row.adjust(len(buttons))
    builder.attach(row)


def _mark(selected: bool, label: str) -> str:
    return CHECK + label if selected else label


# ---------- navigation ----------

def main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SESSIONS), KeyboardButton(text=BTN_FILES)],
            [KeyboardButton(text=BTN_USAGE), KeyboardButton(text=BTN_SETTINGS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Задача для Claude…",
    )


def cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="✖️ Отмена", callback_data=MenuCb(action="cancel"))
    return b.as_markup()


def status_kb(run_ids: Sequence[str]) -> InlineKeyboardMarkup:
    """/status: stop switches for active runs, refresh in place and the self-restart."""
    b = InlineKeyboardBuilder()
    for run_id in run_ids:
        b.button(text=f"🛑 Остановить {run_id}", callback_data=RunCb(action="kill", run_id=run_id))
    b.button(text=BTN_REFRESH, callback_data=MenuCb(action="status_refresh"))
    b.button(text=BTN_RESTART, callback_data=MenuCb(action="restart"))
    b.adjust(*([1] * len(run_ids)), 2)
    return b.as_markup()


def restart_confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="♻️ Да, перезапустить", callback_data=MenuCb(action="restart_force"))
    b.button(text="✖️ Отмена", callback_data=MenuCb(action="cancel"))
    b.adjust(2)
    return b.as_markup()


# ---------- sessions ----------

def projects_kb(
    projects: Sequence[ProjectInfo],
    page: int,
    page_size: int,
    registry: TokenRegistry,
    allowed: Callable[[str], bool],
) -> InlineKeyboardMarkup:
    items, page, pages = paginate(projects, page, page_size)
    b = InlineKeyboardBuilder()
    for project in items:
        lock = "" if allowed(project.display_path) else " 🔒"
        name = truncate_middle(folder_label(project.display_path), 32)
        label = f"📁 {name} · {project.session_count}{lock}"
        b.button(text=label, callback_data=ProjCb(action="open", token=registry.put(project.key)))
    b.adjust(1)
    _nav_row(b, page, pages, lambda p: ProjCb(action="list", page=p))
    return b.as_markup()


def sessions_kb(
    sessions: Sequence[SessionMeta],
    project_token: str,
    page: int,
    page_size: int,
    active_sid: str | None,
    can_run: bool,
) -> InlineKeyboardMarkup:
    items, page, pages = paginate(sessions, page, page_size)
    b = InlineKeyboardBuilder()
    for meta in items:
        mark = "⭐ " if meta.session_id == active_sid else ""
        label = f"{mark}{fmt_short_dt(meta.mtime)} · {truncate(one_line(meta.title), 38)}"
        b.button(text=label, callback_data=SessCb(action="open", sid=meta.session_id))
    b.adjust(1)
    _nav_row(b, page, pages, lambda p: ProjCb(action="open", token=project_token, page=p))
    tail = InlineKeyboardBuilder()
    tail.button(text="⬅️ Проекты", callback_data=ProjCb(action="list"))
    if can_run:
        tail.button(text="🎯 Рабочая папка", callback_data=ProjCb(action="cwd", token=project_token))
    tail.adjust(2)
    b.attach(tail)
    return b.as_markup()


def session_report_kb(sid: str, project_token: str, files_count: int, can_run: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    sizes = []
    if can_run:
        b.button(text="▶️ Продолжить этот чат", callback_data=SessCb(action="resume", sid=sid))
        sizes.append(1)
    b.button(text="📄 Отчет .md", callback_data=SessCb(action="md", sid=sid))
    if files_count:
        b.button(text=f"📥 Файлы · {files_count}", callback_data=SessCb(action="files", sid=sid))
    sizes.append(2 if files_count else 1)
    b.button(text="⬅️ К чатам", callback_data=ProjCb(action="open", token=project_token))
    b.button(text="🔄", callback_data=SessCb(action="open", sid=sid))
    sizes.append(2)
    b.adjust(*sizes)
    return b.as_markup()


def push_report_kb(sid: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Продолжить", callback_data=SessCb(action="resume", sid=sid))
    b.button(text="📄 Подробнее", callback_data=SessCb(action="view", sid=sid))
    b.adjust(2)
    return b.as_markup()


# ---------- settings ----------

def limit_button(profile: LimitProfile) -> str:
    if profile == LimitProfile.AUTO:
        return "♾ Auto"
    limits = PROFILE_LIMITS[profile]
    icon, name = ("💵", "Эконом") if profile == LimitProfile.ECONOMY else ("🚀", "Pro")
    return f"{icon} {name} ${limits.max_budget_usd:g}"


def mode_kb(state: BotState, watcher_running: bool) -> InlineKeyboardMarkup:
    """⚙️ Настройки: one row per choice, the current value marked with ✅ (limit values are in the text)."""
    b = InlineKeyboardBuilder()
    for mode, label in RUN_MODE_BUTTONS.items():
        b.button(text=_mark(state.run_mode == mode, label), callback_data=ModeCb(action=mode.value))
    b.button(text=_mark(state.skip_permissions, PERM_FULL_TITLE), callback_data=ModeCb(action="perm_full"))
    b.button(text=_mark(not state.skip_permissions, PERM_RESTRICTED_TITLE), callback_data=ModeCb(action="perm_restricted"))
    for profile in (LimitProfile.AUTO, LimitProfile.ECONOMY, LimitProfile.PRO):
        b.button(
            text=_mark(state.limit_profile == profile, limit_button(profile)),
            callback_data=ModeCb(action=f"lim_{profile.value}"),
        )
    for policy, label in BG_POLICY_BUTTONS.items():
        b.button(text=_mark(state.bg_session_policy == policy, label), callback_data=ModeCb(action=f"bg_{policy.value}"))
    b.button(text=BTN_MODEL, callback_data=MenuCb(action="model"))
    watch = "вкл" if watcher_running else "выкл"
    b.button(text=f"👁 Авто-отчеты: {watch}", callback_data=ModeCb(action="watch"))
    b.button(text=BTN_RESTART, callback_data=MenuCb(action="restart"))
    b.adjust(3, 2, 3, 2, 1, 2)
    return b.as_markup()


def model_kb(state: BotState) -> InlineKeyboardMarkup:
    """/model: rows 1-2 model, rows 3-4 reasoning effort; the current choice is marked with ✅."""
    b = InlineKeyboardBuilder()
    for model in (*MODEL_PRESETS, MODEL_DEFAULT):
        b.button(text=_mark(state.claude_model == model, MODEL_TITLES[model]), callback_data=ModelCb(kind="m", value=model))
    for effort in EFFORT_BUTTONS:
        b.button(
            text=_mark(state.reasoning_effort == effort, EFFORT_TITLES[effort]),
            callback_data=ModelCb(kind="e", value=effort.value),
        )
    b.button(text="⬅️ Настройки", callback_data=MenuCb(action="mode"))
    b.adjust(2, 2, 3, 3, 1)
    return b.as_markup()


# ---------- runs ----------

def run_status_kb(run_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🛑 Остановить", callback_data=RunCb(action="kill", run_id=run_id))
    return b.as_markup()


def run_done_kb(
    run_id: str,
    files_count: int,
    session_id: str | None,
    offer_unrestricted_retry: bool = False,
) -> InlineKeyboardMarkup | None:
    b = InlineKeyboardBuilder()
    sizes = []
    if offer_unrestricted_retry:
        label = "🔓 Повторить без ограничений (--resume)" if session_id else "🔓 Повторить без ограничений"
        b.button(text=label, callback_data=RunCb(action="retry", run_id=run_id))
        sizes.append(1)
    row = 0
    if files_count:
        b.button(text=f"📥 Файлы · {files_count}", callback_data=RunCb(action="files", run_id=run_id))
        row += 1
    if session_id:
        b.button(text="📄 Отчет по чату", callback_data=SessCb(action="view", sid=session_id))
        row += 1
    if row:
        sizes.append(row)
    if sizes:
        b.adjust(*sizes)
    markup = b.as_markup()
    return markup if markup.inline_keyboard else None


def background_conflict_kb(run_id: str, allow_stop: bool = True) -> InlineKeyboardMarkup:
    """`--resume` refused because the chat runs in a background session on the PC (its id is in the text)."""
    b = InlineKeyboardBuilder()
    b.button(text="🌿 Ответвить копию", callback_data=RunCb(action="fork", run_id=run_id))
    if allow_stop:
        b.button(text="🛑 Остановить на ПК и продолжить", callback_data=RunCb(action="bgstop", run_id=run_id))
    b.adjust(1)
    return b.as_markup()


# ---------- usage ----------

def usage_kb(view: str = "main", has_session: bool = True) -> InlineKeyboardMarkup:
    """/usage dashboard and its detail views (days7 | days30 | top)."""
    b = InlineKeyboardBuilder()
    if view == "main":
        b.button(text=BTN_REFRESH, callback_data=UsageCb(action="refresh"))
        b.button(text="📅 По дням", callback_data=UsageCb(action="days7"))
        b.button(text="🏆 Топ", callback_data=UsageCb(action="top"))
        if has_session:
            b.button(text="🧹 Сжать контекст", callback_data=UsageCb(action="compact"))
        b.button(text="🆕 Чистый чат", callback_data=UsageCb(action="newchat"))
        b.adjust(3, 2) if has_session else b.adjust(3, 1)
        return b.as_markup()
    if view in ("days7", "days30"):
        other = "days30" if view == "days7" else "days7"
        b.button(text="📅 30 дней" if other == "days30" else "📅 7 дней", callback_data=UsageCb(action=other))
        b.button(text=BTN_REFRESH, callback_data=UsageCb(action=view))
    else:
        b.button(text=BTN_REFRESH, callback_data=UsageCb(action="top"))
    b.button(text="⬅️ К лимитам", callback_data=UsageCb(action="refresh"))
    b.adjust(3) if view in ("days7", "days30") else b.adjust(2)
    return b.as_markup()


# ---------- files ----------

def roots_kb(roots: Sequence[Path], registry: TokenRegistry) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for root in roots:
        b.button(text=f"💽 {truncate_middle(str(root), 40)}", callback_data=FileCb(action="dir", token=registry.put(str(root))))
    b.adjust(1)
    return b.as_markup()


def browser_kb(
    entries: Sequence[EntryInfo],
    dir_token: str,
    parent_token: str | None,
    page: int,
    page_size: int,
    registry: TokenRegistry,
) -> InlineKeyboardMarkup:
    items, page, pages = paginate(entries, page, page_size)
    b = InlineKeyboardBuilder()
    for entry in items:
        token = registry.put(str(entry.path))
        if entry.is_dir:
            b.button(text=f"📁 {truncate_middle(entry.name, 40)}", callback_data=FileCb(action="dir", token=token))
        else:
            label = f"📄 {truncate_middle(entry.name, 32)} · {human_size(entry.size)}"
            b.button(text=label, callback_data=FileCb(action="file", token=token))
    b.adjust(1)
    _nav_row(b, page, pages, lambda p: FileCb(action="dir", token=dir_token, page=p))
    tail = InlineKeyboardBuilder()
    back = FileCb(action="dir", token=parent_token) if parent_token else FileCb(action="roots")
    tail.button(text=BTN_BACK, callback_data=back)
    tail.button(text="🔄", callback_data=FileCb(action="dir", token=dir_token, page=page))
    tail.button(text="🎯 Рабочая папка", callback_data=FileCb(action="cwd", token=dir_token))
    tail.adjust(3)
    b.attach(tail)
    return b.as_markup()


def file_actions_kb(file_token: str, parent_token: str | None, preview_lines: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📥 Скачать", callback_data=FileCb(action="dl", token=file_token))
    b.button(text=f"👁 {preview_lines} строк", callback_data=FileCb(action="pv", token=file_token))
    b.button(text="🤖 Спросить Claude об этом файле", callback_data=FileCb(action="ask", token=file_token))
    back = FileCb(action="dir", token=parent_token) if parent_token else FileCb(action="roots")
    b.button(text=BTN_BACK, callback_data=back)
    b.adjust(2, 1, 1)
    return b.as_markup()


def search_results_kb(paths: Sequence[Path], registry: TokenRegistry, base: Path | None) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for path in paths:
        shown = str(path)
        if base is not None:
            try:
                shown = str(path.relative_to(base))
            except ValueError:
                shown = str(path)
        b.button(text=f"📄 {truncate_middle(shown, 44)}", callback_data=FileCb(action="file", token=registry.put(str(path))))
    b.adjust(1)
    return b.as_markup()


def upload_kb(has_caption: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💾 Сохранить", callback_data=UpCb(action="save"))
    label = "💾 Сохранить и выполнить подпись" if has_caption else "💾 Сохранить и дать задачу"
    b.button(text=label, callback_data=UpCb(action="save_run"))
    b.button(text="✖️ Отмена", callback_data=UpCb(action="cancel"))
    b.adjust(1, 1, 1)
    return b.as_markup()


def after_upload_kb(file_token: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🤖 Задача Claude по файлу", callback_data=FileCb(action="ask", token=file_token))
    b.button(text="👁 Просмотр", callback_data=FileCb(action="pv", token=file_token))
    b.adjust(2)
    return b.as_markup()


def voice_confirm_kb() -> InlineKeyboardMarkup:
    """Shown under a transcript when VOICE_AUTO_RUN=false (the text is kept per message id)."""
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Запустить", callback_data=VoiceCb(action="run"))
    b.button(text="✖️ Отмена", callback_data=VoiceCb(action="cancel"))
    b.adjust(2)
    return b.as_markup()
