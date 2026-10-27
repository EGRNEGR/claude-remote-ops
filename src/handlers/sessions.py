"""Claude Code projects and sessions: lists with pagination, reports, switching the active chat."""

from __future__ import annotations

import contextlib
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message

from src.context import AppContext
from src.handlers.delivery import send_paths
from src.keyboards.inline import (
    BTN_SESSIONS,
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
    SessionMeta,
    SessionReport,
    render_report_html,
    render_report_markdown,
)
from src.services.state import RunMode
from src.utils.registry import TokenRegistry
from src.utils.text import esc, esc_fit, fmt_short_dt, human_size, one_line, truncate, truncate_middle

log = logging.getLogger(__name__)
router = Router(name="sessions")


async def _show(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    """Edit the callback message in place, or answer a command with a new message."""
    if isinstance(target, CallbackQuery):
        if isinstance(target.message, Message):
            try:
                await target.message.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest as exc:
                if "not modified" in exc.message:
                    return
                log.debug("edit failed, sending new message: %s", exc.message)
            await target.message.answer(text, reply_markup=markup)
        return
    await target.answer(text, reply_markup=markup)


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
        await _show(target, f"📭 В <code>{esc(ctx.settings.CLAUDE_PROJECTS_DIR)}</code> нет сессий Claude Code.", None)
        return
    size = ctx.settings.PROJECTS_PAGE_SIZE
    items, page, pages = paginate(projects, page, size)
    lines = [f"📂 <b>Проекты Claude Code</b> ({len(projects)}) · стр. {page + 1}/{pages}", ""]
    for index, project in enumerate(items, start=page * size + 1):
        lock = "" if ctx.safe.is_allowed(project.display_path) else " 🔒"
        approx = "" if project.exact else " <i>(путь восстановлен приблизительно)</i>"
        lines.append(f"{index}. <code>{esc_fit(project.display_path, 200)}</code>{lock}{approx}")
        lines.append(f"    💬 {project.session_count} · 🕒 {fmt_short_dt(project.last_mtime)}")
    lines.append("")
    lines.append("<i>🔒 - папка вне ALLOWED_DIRS: отчеты доступны, запуск Claude нет.</i>")
    markup = projects_kb(projects, page, size, ctx.tokens, ctx.safe.is_allowed)
    await _show(target, "\n".join(lines), markup)


def _session_line(index: int, meta: SessionMeta, active_sid: str | None) -> str:
    star = "⭐ " if meta.session_id == active_sid else ""
    headless = " 🤖" if (meta.entrypoint or "").startswith("sdk") else ""
    return (
        f"{index}. {star}<b>{esc_fit(truncate(one_line(meta.title), 80), 200)}</b>{headless}\n"
        f"    🕒 {fmt_short_dt(meta.mtime)} · {human_size(meta.size)} · <code>{esc(meta.session_id[:8])}</code>"
    )


async def show_sessions(target: Message | CallbackQuery, ctx: AppContext, key: str, page: int = 0) -> None:
    sessions = await ctx.inspector.list_sessions(key)
    cwd = await ctx.inspector.project_path(key) or key
    can_run = ctx.safe.is_allowed(cwd)
    size = ctx.settings.SESSIONS_PAGE_SIZE
    items, page, pages = paginate(sessions, page, size)
    lines = [
        f"📁 <b>{esc_fit(truncate_middle(cwd, 120), 300)}</b>",
        f"💬 Сессий: {len(sessions)} · стр. {page + 1}/{pages}" + ("" if can_run else " · 🔒 вне ALLOWED_DIRS"),
        "",
    ]
    for index, meta in enumerate(items, start=page * size + 1):
        lines.append(_session_line(index, meta, ctx.state.active_session_id))
    if any((m.entrypoint or "").startswith("sdk") for m in items):
        lines.append("\n<i>🤖 - headless-запуск (claude -p)</i>")
    token = ctx.tokens.put(key)
    markup = sessions_kb(sessions, token, page, size, ctx.state.active_session_id, can_run)
    await _show(target, "\n".join(lines), markup)


async def _load_report(ctx: AppContext, sid: str) -> tuple[SessionMeta, SessionReport] | None:
    meta = await ctx.inspector.find_session(sid)
    if meta is None:
        return None
    report = await ctx.inspector.build_report(meta)
    if report.title is None:
        report.title = meta.title
    return meta, report


@router.message(Command("sessions"))
@router.message(F.text == BTN_SESSIONS)
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
        await call.message.answer(
            f"🎯 Рабочая папка: <code>{esc_fit(str(cwd), 300)}</code>\n"
            f"Режим: {'продолжить последний чат' if mode == RunMode.CONTINUE else 'новый чат'}. "
            "Напишите задачу для Claude."
        )


@router.callback_query(SessCb.filter(F.action.in_({"open", "view"})))
async def cb_session_open(call: CallbackQuery, callback_data: SessCb, ctx: AppContext) -> None:
    loaded = await _load_report(ctx, callback_data.sid)
    if loaded is None:
        await call.answer("Сессия не найдена", show_alert=True)
        return
    await call.answer()
    meta, report = loaded
    can_run = ctx.safe.is_allowed(report.cwd or meta.cwd or "")
    markup = session_report_kb(meta.session_id, ctx.tokens.put(meta.project_key), bool(report.files), can_run)
    text = render_report_html(report)
    if callback_data.action == "view" and isinstance(call.message, Message):
        await call.message.answer(text, reply_markup=markup)
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
        await call.message.answer(
            f"▶️ Следующее сообщение продолжит чат «<b>{esc(title)}</b>»\n"
            f"🆔 <code>{esc(meta.session_id)}</code>\n📁 <code>{esc_fit(str(cwd), 300)}</code>\n\n"
            "Напишите задачу для Claude (будет запущено <code>claude -p --resume</code>)."
        )


@router.callback_query(SessCb.filter(F.action == "md"))
async def cb_session_md(call: CallbackQuery, callback_data: SessCb, ctx: AppContext) -> None:
    await call.answer("⏳ Формирую отчет...")
    loaded = await _load_report(ctx, callback_data.sid)
    if loaded is None or not isinstance(call.message, Message):
        return
    _, report = loaded
    data = render_report_markdown(report).encode("utf-8")
    await call.message.answer_document(
        BufferedInputFile(data, filename=f"session_{report.session_id[:8]}.md"),
        caption=f"📄 Полный отчет: {esc_fit(report.title or report.session_id, 200)}",
    )


@router.callback_query(SessCb.filter(F.action == "files"))
async def cb_session_files(call: CallbackQuery, callback_data: SessCb, bot: Bot, ctx: AppContext) -> None:
    await call.answer("⏳ Собираю файлы...")
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
    """Used by the watcher: short push after the user worked in PowerShell on the PC."""
    text = render_report_html(report, header="🔔 Сессия Claude обновилась на ПК")
    await bot.send_message(chat_id, text, reply_markup=push_report_kb(report.session_id, bool(report.files)))
