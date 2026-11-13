"""Claude Code projects and sessions: lists with pagination, reports, switching the active chat."""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from src.context import AppContext
from src.handlers.delivery import send_paths
from src.keyboards.inline import (
    SESSIONS_TEXTS,
    ProjCb,
    SessCb,
    paginate,
    projects_kb,
    push_report_kb,
    session_report_kb,
    sessions_kb,
)
from src.services.file_manager import PathAccessError
from src.services.session_parser import (
    REPORT_PUSH,
    SessionMeta,
    SessionReport,
    render_report_html,
    render_report_markdown,
)
from src.services.state import RunMode
from src.utils.registry import TokenRegistry
from src.handlers.delivery import send_html
from src.ui import theme
from src.ui.formatter import fit_html
from src.utils.text import esc_fit, one_line, truncate

log = logging.getLogger(__name__)
router = Router(name="sessions")


async def _show(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Edit the callback message in place, or answer a command with a new message (both HTML-safe)."""
    body = fit_html(text)
    if isinstance(target, CallbackQuery):
        if isinstance(target.message, Message):
            try:
                await target.message.edit_text(body, reply_markup=markup)
                return
            except TelegramBadRequest as exc:
                if "not modified" in exc.message:
                    return
                log.debug("edit failed, sending new message: %s", exc.message)
            if target.bot is not None:
                await send_html(target.bot, target.message.chat.id, body, markup)
        return
    if target.bot is not None:
        await send_html(target.bot, target.chat.id, body, markup)


async def _project_key(ctx: AppContext, token: str) -> str | None:
    key = ctx.tokens.get(token)
    if key:
        return key
    # Token registry is in-memory: after a restart rebuild it from the folder names.
    for project in await ctx.inspector.list_projects():
        if TokenRegistry.make_token(project.key) == token:
            ctx.tokens.put(project.key)
            return project.key
    return None


async def show_projects(target: Message | CallbackQuery, ctx: AppContext, page: int = 0) -> None:
    projects = await ctx.inspector.list_projects()
    if not projects:
        await _show(target, theme.lines(theme.title("Проекты Claude Code"), theme.meta("Сессий Claude Code пока нет")), None)
        return
    size = ctx.settings.PROJECTS_PAGE_SIZE
    _, page, pages = paginate(projects, page, size)
    locked = any(not ctx.safe.is_allowed(p.display_path) for p in projects)
    pager = f"стр. {page + 1} из {pages}" if pages > 1 else None
    text = theme.join_blocks(
        theme.lines(
            theme.title("Проекты Claude Code"),
            theme.meta(theme.count(len(projects), "проект", "проекта", "проектов"), pager, "новые сверху"),
        ),
        theme.meta("🔒 — папка вне ALLOWED_DIRS: отчеты доступны, запуск Claude нет") if locked else "",
    )
    markup = projects_kb(projects, page, size, ctx.tokens, ctx.safe.is_allowed)
    await _show(target, text, markup)


async def show_sessions(target: Message | CallbackQuery, ctx: AppContext, key: str, page: int = 0) -> None:
    sessions = await ctx.inspector.list_sessions(key)
    cwd = await ctx.inspector.project_path(key) or key
    can_run = ctx.safe.is_allowed(cwd)
    size = ctx.settings.SESSIONS_PAGE_SIZE
    _, page, pages = paginate(sessions, page, size)
    active = next((m for m in sessions if m.session_id == ctx.state.active_session_id), None)
    pager = f"стр. {page + 1} из {pages}" if pages > 1 else None
    text = theme.join_blocks(
        theme.lines(
            theme.title(theme.folder_label(cwd)),
            theme.meta(
                theme.count(len(sessions), "чат", "чата", "чатов"), pager,
                None if can_run else "🔒 вне ALLOWED_DIRS",
            ),
        ),
        theme.item(f"⭐ Текущий чат: <b>{esc_fit(one_line(active.title), 80)}</b>") if active else "",
    )
    token = ctx.tokens.put(key)
    markup = sessions_kb(sessions, token, page, size, ctx.state.active_session_id, can_run)
    await _show(target, text, markup)


async def _load_report(ctx: AppContext, sid: str) -> tuple[SessionMeta, SessionReport] | None:
    meta = await ctx.inspector.find_session(sid)
    if meta is None:
        return None
    report = await ctx.inspector.build_report(meta)
    if report.title is None:
        report.title = meta.title
    return meta, report


@router.message(Command("sessions"))
@router.message(F.text.in_(SESSIONS_TEXTS))
async def cmd_sessions(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await show_projects(message, ctx)


@router.callback_query(ProjCb.filter(F.action == "list"))
async def cb_projects(call: CallbackQuery, callback_data: ProjCb, ctx: AppContext) -> None:
    await call.answer()
    await show_projects(call, ctx, callback_data.page)


@router.callback_query(ProjCb.filter(F.action == "open"))
async def cb_project_open(call: CallbackQuery, callback_data: ProjCb, ctx: AppContext) -> None:
    key = await _project_key(ctx, callback_data.token)
    if key is None or ctx.inspector.project_dir(key) is None:
        await call.answer("Проект не найден, обновите список", show_alert=True)
        return
    await call.answer()
    await show_sessions(call, ctx, key, callback_data.page)


@router.callback_query(ProjCb.filter(F.action == "cwd"))
async def cb_project_cwd(call: CallbackQuery, callback_data: ProjCb, ctx: AppContext) -> None:
    key = await _project_key(ctx, callback_data.token)
    raw = await ctx.inspector.project_path(key) if key else None
    if not raw:
        await call.answer("Проект не найден", show_alert=True)
        return
    try:
        cwd = ctx.safe.resolve_dir(raw)
    except PathAccessError as exc:
        await call.answer(f"🔒 {truncate(str(exc), 180)}", show_alert=True)
        return
    mode = ctx.state.run_mode if ctx.state.run_mode == RunMode.CONTINUE else RunMode.NEW
    await ctx.store.update(active_cwd=str(cwd), run_mode=mode)
    await call.answer("🎯 Рабочая папка выбрана")
    if isinstance(call.message, Message):
        await call.message.answer(working_folder_text(cwd, mode))


def working_folder_text(cwd: Path, mode: RunMode) -> str:
    """Confirmation after 🎯: the folder name, what the next message does, the full path for reference."""
    action = "продолжит последний чат папки" if mode == RunMode.CONTINUE else "начнет новый чат"
    return theme.lines(
        theme.title(theme.folder_label(cwd), marker="🎯"),
        theme.meta(f"Рабочая папка выбрана · следующее сообщение {action}"),
        f"<code>{esc_fit(str(cwd), 300)}</code>",
    )


@router.callback_query(SessCb.filter(F.action.in_({"open", "view"})))
async def cb_session_open(call: CallbackQuery, callback_data: SessCb, bot: Bot, ctx: AppContext) -> None:
    loaded = await _load_report(ctx, callback_data.sid)
    if loaded is None:
        await call.answer("Сессия не найдена", show_alert=True)
        return
    await call.answer()
    meta, report = loaded
    can_run = ctx.safe.is_allowed(report.cwd or meta.cwd or "")
    markup = session_report_kb(meta.session_id, ctx.tokens.put(meta.project_key), len(report.files), can_run)
    text = render_report_html(report)
    if callback_data.action == "view":
        await send_html(bot, call.from_user.id, text, markup)
        return
    await _show(call, text, markup)


@router.callback_query(SessCb.filter(F.action == "resume"))
async def cb_session_resume(call: CallbackQuery, callback_data: SessCb, ctx: AppContext) -> None:
    meta = await ctx.inspector.find_session(callback_data.sid)
    if meta is None:
        await call.answer("Сессия не найдена", show_alert=True)
        return
    raw = meta.cwd or await ctx.inspector.project_path(meta.project_key)
    try:
        cwd = ctx.safe.resolve_dir(raw or "")
    except PathAccessError as exc:
        await call.answer(f"🔒 {truncate(str(exc), 180)}", show_alert=True)
        return
    title = truncate(one_line(meta.title), 60)
    await ctx.store.update(
        active_session_id=meta.session_id,
        active_session_cwd=str(cwd),
        active_cwd=str(cwd),
        active_session_title=title,
        run_mode=RunMode.RESUME,
    )
    await call.answer("▶️ Чат выбран")
    if isinstance(call.message, Message):
        await call.message.answer(theme.lines(
            theme.title(title, marker="▶️"),
            theme.meta(f"Рабочая папка: {theme.folder_label(cwd)}", "чат выбран"),
            "",
            theme.item("Следующее сообщение продолжит этот чат"),
        ))


@router.callback_query(SessCb.filter(F.action == "md"))
async def cb_session_md(call: CallbackQuery, callback_data: SessCb, ctx: AppContext) -> None:
    await call.answer("⏳ Формирую отчет…")
    loaded = await _load_report(ctx, callback_data.sid)
    if loaded is None or not isinstance(call.message, Message):
        return
    _, report = loaded
    data = render_report_markdown(report).encode("utf-8")
    name = report.title or theme.folder_label(report.cwd)
    await call.message.answer_document(
        BufferedInputFile(data, filename=f"session_{report.session_id[:8]}.md"),
        caption=f"📄 <b>Полный отчет</b>\n{theme.meta(truncate(name, 200))}",
    )


@router.callback_query(SessCb.filter(F.action == "files"))
async def cb_session_files(call: CallbackQuery, callback_data: SessCb, bot: Bot, ctx: AppContext) -> None:
    await call.answer("⏳ Собираю файлы…")
    loaded = await _load_report(ctx, callback_data.sid)
    if loaded is None:
        return
    _, report = loaded
    base = None
    if report.cwd:
        with contextlib.suppress(PathAccessError):
            base = ctx.safe.resolve_dir(report.cwd)
    await send_paths(bot, call.from_user.id, ctx, report.file_paths(), base, f"session_{report.session_id[:8]}")


async def push_session_report(bot: Bot, chat_id: int, report: SessionReport) -> None:
    """Used by the watcher: a compact card after the user worked with Claude on the PC."""
    await send_html(bot, chat_id, render_report_html(report, kind=REPORT_PUSH), push_report_kb(report.session_id))
