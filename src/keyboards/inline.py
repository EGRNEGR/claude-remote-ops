"""Inline / reply keyboards and compact CallbackData (Telegram limit: 64 bytes per callback)."""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import TypeVar

from aiogram.filters.callback_data import CallbackData
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from src.services.file_manager import EntryInfo
from src.services.session_parser import ProjectInfo, SessionMeta
from src.services.state import (
    BG_POLICY_TITLES,
    RUN_MODE_TITLES,
    BgSessionPolicy,
    BotState,
    LimitProfile,
    RunLimits,
    RunMode,
    limit_profile_title,
)
from src.utils.registry import TokenRegistry
from src.utils.text import fmt_short_dt, human_size, one_line, truncate, truncate_middle

T = TypeVar("T")

BTN_SESSIONS = "📂 Сессии"
BTN_FILES = "🗂 Файлы"
BTN_MODE = "⚙️ Режим"
BTN_STATUS = "📊 Статус"
BTN_RESTART = "🔄 Перезапустить бота"
REPLY_BUTTONS = (BTN_SESSIONS, BTN_FILES, BTN_MODE, BTN_STATUS)


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


class UpCb(CallbackData, prefix="up"):
    action: str  # save | save_run | cancel


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
    buttons.append((f"{page + 1}/{pages}", make(page)))
    if page < pages - 1:
        buttons.append(("▶️", make(page + 1)))
    row = InlineKeyboardBuilder()
    for text, cb in buttons:
        row.button(text=text, callback_data=cb)
    row.adjust(len(buttons))
    builder.attach(row)


def main_reply_kb() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SESSIONS), KeyboardButton(text=BTN_FILES)],
            [KeyboardButton(text=BTN_MODE), KeyboardButton(text=BTN_STATUS)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="Задача для Claude...",
    )


def main_inline_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text=BTN_SESSIONS, callback_data=ProjCb(action="list"))
    b.button(text=BTN_FILES, callback_data=FileCb(action="roots"))
    b.button(text=BTN_MODE, callback_data=MenuCb(action="mode"))
    b.button(text=BTN_STATUS, callback_data=MenuCb(action="status"))
    b.adjust(2, 2)
    return b.as_markup()


def cancel_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="❌ Отмена", callback_data=MenuCb(action="cancel"))
    return b.as_markup()


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
        lock = "" if allowed(project.display_path) else "🔒 "
        label = f"{lock}📁 {truncate_middle(project.display_path, 34)} · {project.session_count}"
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
        label = f"{mark}🕒 {fmt_short_dt(meta.mtime)} · {truncate(one_line(meta.title), 36)}"
        b.button(text=label, callback_data=SessCb(action="open", sid=meta.session_id))
    b.adjust(1)
    _nav_row(b, page, pages, lambda p: ProjCb(action="open", token=project_token, page=p))
    tail = InlineKeyboardBuilder()
    if can_run:
        tail.button(text="🎯 Сделать рабочей папкой", callback_data=ProjCb(action="cwd", token=project_token))
    tail.button(text="⬅️ Проекты", callback_data=ProjCb(action="list"))
    tail.adjust(1)
    b.attach(tail)
    return b.as_markup()


def session_report_kb(sid: str, project_token: str, has_files: bool, can_run: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if can_run:
        b.button(text="▶️ Продолжить этот чат", callback_data=SessCb(action="resume", sid=sid))
    b.button(text="📄 Полный отчет (.md)", callback_data=SessCb(action="md", sid=sid))
    if has_files:
        b.button(text="📥 Скачать затронутые файлы", callback_data=SessCb(action="files", sid=sid))
    b.button(text="🔄 Обновить", callback_data=SessCb(action="open", sid=sid))
    b.button(text="⬅️ К сессиям", callback_data=ProjCb(action="open", token=project_token))
    b.adjust(1)
    return b.as_markup()


def push_report_kb(sid: str, has_files: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Продолжить с телефона", callback_data=SessCb(action="resume", sid=sid))
    b.button(text="📊 Полный отчет", callback_data=SessCb(action="view", sid=sid))
    if has_files:
        b.button(text="📥 Файлы", callback_data=SessCb(action="files", sid=sid))
    b.adjust(1, 2)
    return b.as_markup()


PERM_FULL_TITLE = "🔓 Skip Permissions (Full MCP & Bash)"
PERM_RESTRICTED_TITLE = "🔒 Restricted Tools"


def mode_kb(state: BotState, watcher_running: bool, env_limits: RunLimits) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for mode in (RunMode.NEW, RunMode.CONTINUE, RunMode.RESUME):
        mark = "✅ " if state.run_mode == mode else ""
        b.button(text=mark + RUN_MODE_TITLES[mode], callback_data=ModeCb(action=mode.value))
    full_mark, restricted_mark = ("✅ ", "") if state.skip_permissions else ("", "✅ ")
    b.button(text=full_mark + PERM_FULL_TITLE, callback_data=ModeCb(action="perm_full"))
    b.button(text=restricted_mark + PERM_RESTRICTED_TITLE, callback_data=ModeCb(action="perm_restricted"))
    for profile in (LimitProfile.AUTO, LimitProfile.ECONOMY, LimitProfile.PRO):
        mark = "✅ " if state.limit_profile == profile else ""
        b.button(
            text=mark + limit_profile_title(profile, env_limits),
            callback_data=ModeCb(action=f"lim_{profile.value}"),
        )
    for policy in (BgSessionPolicy.ASK, BgSessionPolicy.FORK):
        mark = "✅ " if state.bg_session_policy == policy else ""
        b.button(text=mark + BG_POLICY_TITLES[policy], callback_data=ModeCb(action=f"bg_{policy.value}"))
    watch = "ВКЛ" if watcher_running else "ВЫКЛ"
    b.button(text=f"👁 Авто-отчеты (watchdog): {watch}", callback_data=ModeCb(action="watch"))
    b.button(text="📂 Выбрать проект / сессию", callback_data=ProjCb(action="list"))
    b.button(text="🗂 Выбрать папку в проводнике", callback_data=FileCb(action="roots"))
    b.button(text=BTN_RESTART, callback_data=MenuCb(action="restart"))
    b.adjust(1)
    return b.as_markup()


def run_status_kb(run_id: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🛑 Остановить процесс", callback_data=RunCb(action="kill", run_id=run_id))
    return b.as_markup()


def run_done_kb(
    run_id: str,
    files_count: int,
    session_id: str | None,
    offer_unrestricted_retry: bool = False,
) -> InlineKeyboardMarkup | None:
    b = InlineKeyboardBuilder()
    if offer_unrestricted_retry:
        label = "🔓 Повторить без ограничений (--resume)" if session_id else "🔓 Повторить без ограничений"
        b.button(text=label, callback_data=RunCb(action="retry", run_id=run_id))
    if files_count:
        b.button(
            text=f"📥 Скачать измененные файлы ({files_count})",
            callback_data=RunCb(action="files", run_id=run_id),
        )
    if session_id:
        b.button(text="📊 Отчет по сессии", callback_data=SessCb(action="view", sid=session_id))
    b.adjust(1)
    markup = b.as_markup()
    return markup if markup.inline_keyboard else None


def background_conflict_kb(run_id: str, bg_id: str, allow_stop: bool = True) -> InlineKeyboardMarkup:
    """`--resume` refused because the chat runs in a background session on the PC."""
    b = InlineKeyboardBuilder()
    b.button(text="🌿 Ответвить копию (--fork-session)", callback_data=RunCb(action="fork", run_id=run_id))
    if allow_stop:
        b.button(
            text=f"🛑 Остановить фоновую (claude stop {bg_id}) и продолжить здесь",
            callback_data=RunCb(action="bgstop", run_id=run_id),
        )
    b.adjust(1)
    return b.as_markup()


def active_runs_kb(run_ids: Sequence[str]) -> InlineKeyboardMarkup | None:
    if not run_ids:
        return None
    b = InlineKeyboardBuilder()
    for run_id in run_ids:
        b.button(text=f"🛑 Остановить {run_id}", callback_data=RunCb(action="kill", run_id=run_id))
    b.adjust(1)
    return b.as_markup()


def status_kb(run_ids: Sequence[str]) -> InlineKeyboardMarkup:
    """/status: kill switches for active runs (or the main menu when idle) + self-restart."""
    rows = [list(row) for row in (active_runs_kb(run_ids) or main_inline_kb()).inline_keyboard]
    rows.append([InlineKeyboardButton(text=BTN_RESTART, callback_data=MenuCb(action="restart").pack())])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def restart_confirm_kb() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🔄 Да, перезапустить", callback_data=MenuCb(action="restart_force"))
    b.button(text="❌ Отмена", callback_data=MenuCb(action="cancel"))
    b.adjust(1)
    return b.as_markup()


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
    tail.button(text="⬅️ Назад", callback_data=back)
    tail.button(text="🔄", callback_data=FileCb(action="dir", token=dir_token, page=page))
    tail.button(text="📌 Сделать рабочей папкой", callback_data=FileCb(action="cwd", token=dir_token))
    tail.adjust(2, 1)
    b.attach(tail)
    return b.as_markup()


def file_actions_kb(file_token: str, parent_token: str | None, preview_lines: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📥 Скачать файл на телефон", callback_data=FileCb(action="dl", token=file_token))
    b.button(text=f"👁 Предпросмотр (первые {preview_lines} строк)", callback_data=FileCb(action="pv", token=file_token))
    b.button(text="🤖 Задать вопрос Claude по этому файлу", callback_data=FileCb(action="ask", token=file_token))
    back = FileCb(action="dir", token=parent_token) if parent_token else FileCb(action="roots")
    b.button(text="⬅️ Назад", callback_data=back)
    b.adjust(1)
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
    b.button(text="💾 Сохранить в проект", callback_data=UpCb(action="save"))
    label = "💾 Сохранить и выполнить подпись в Claude" if has_caption else "💾 Сохранить и дать задачу Claude"
    b.button(text=label, callback_data=UpCb(action="save_run"))
    b.button(text="❌ Отмена", callback_data=UpCb(action="cancel"))
    b.adjust(1)
    return b.as_markup()


def after_upload_kb(file_token: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🤖 Дать задачу Claude по этому файлу", callback_data=FileCb(action="ask", token=file_token))
    b.button(text="👁 Предпросмотр", callback_data=FileCb(action="pv", token=file_token))
    b.adjust(1)
    return b.as_markup()


class VoiceCb(CallbackData, prefix="vc"):
    action: str  # run | cancel


def voice_confirm_kb() -> InlineKeyboardMarkup:
    """Shown under a transcript when VOICE_AUTO_RUN=false (the text is kept per message id)."""
    b = InlineKeyboardBuilder()
    b.button(text="▶️ Запустить", callback_data=VoiceCb(action="run"))
    b.button(text="❌ Отмена", callback_data=VoiceCb(action="cancel"))
    b.adjust(2)
    return b.as_markup()
