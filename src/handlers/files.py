"""Inline file explorer, /get search, previews, uploads from the phone and per-file Claude tasks."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from src.context import AppContext
from src.handlers.delivery import send_file
from src.handlers.runner import build_request, launch_run
from src.keyboards.inline import (
    BTN_FILES,
    REPLY_BUTTONS,
    FileCb,
    MenuCb,
    UpCb,
    after_upload_kb,
    browser_kb,
    cancel_kb,
    file_actions_kb,
    paginate,
    roots_kb,
    search_results_kb,
    upload_kb,
)
from src.services.file_manager import PathAccessError
from src.services.state import RunMode
from src.utils.text import esc, esc_fit, fmt_dt, human_size, truncate
from src.utils.winpath import is_within, long_path, normcase, sanitize_filename, unique_path

log = logging.getLogger(__name__)
router = Router(name="files")

_LANGS = {
    ".py": "python", ".ps1": "powershell", ".psm1": "powershell", ".js": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".jsx": "javascript", ".json": "json", ".md": "markdown", ".yml": "yaml",
    ".yaml": "yaml", ".toml": "toml", ".ini": "ini", ".cfg": "ini", ".sql": "sql", ".sh": "bash",
    ".bat": "batch", ".cmd": "batch", ".cs": "csharp", ".cpp": "cpp", ".c": "c", ".h": "c", ".go": "go",
    ".rs": "rust", ".java": "java", ".html": "html", ".css": "css", ".xml": "xml",
}


class FileAsk(StatesGroup):
    waiting_question = State()


class UploadFlow(StatesGroup):
    choosing = State()


async def _render(target: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None) -> None:
    if isinstance(target, CallbackQuery):
        if isinstance(target.message, Message):
            try:
                await target.message.edit_text(text, reply_markup=markup)
                return
            except TelegramBadRequest as exc:
                if "not modified" in exc.message:
                    return
            await target.message.answer(text, reply_markup=markup)
        return
    await target.answer(text, reply_markup=markup)


def _token_path(ctx: AppContext, token: str) -> Path | None:
    return ctx.path_from_token(token)


async def show_roots(target: Message | CallbackQuery, ctx: AppContext) -> None:
    roots = ctx.safe.roots
    if len(roots) == 1:
        await show_dir(target, ctx, roots[0], 0)
        return
    text = "🗂 <b>Разрешенные папки</b>\n\n" + "\n".join(f"💽 <code>{esc(r)}</code>" for r in roots)
    await _render(target, text, roots_kb(roots, ctx.tokens))


async def show_dir(target: Message | CallbackQuery, ctx: AppContext, path: Path, page: int) -> None:
    listing = await ctx.files.list_dir(path)
    dirs = sum(1 for e in listing.entries if e.is_dir)
    files = len(listing.entries) - dirs
    size = ctx.settings.FILES_PAGE_SIZE
    _, page, pages = paginate(listing.entries, page, size)
    lines = [f"🗂 <code>{esc_fit(str(listing.path), 600)}</code>"]
    summary = f"📁 {dirs} · 📄 {files}"
    if pages > 1:
        summary += f" · стр. {page + 1}/{pages}"
    if listing.skipped:
        summary += f" · скрыто: {listing.skipped}"
    lines.append(summary)
    active = ctx.active_cwd()
    if active is not None and normcase(active) == normcase(listing.path):
        lines.append("🎯 <i>Это текущая рабочая папка Claude</i>")
    if listing.error:
        lines.append(f"⚠️ {esc(listing.error)}")
    if not listing.entries and not listing.error:
        lines.append("<i>Папка пуста</i>")
    dir_token = ctx.token(listing.path)
    parent_token = None if ctx.safe.is_root(listing.path) else ctx.token(listing.path.parent)
    markup = browser_kb(listing.entries, dir_token, parent_token, page, size, ctx.tokens)
    await _render(target, "\n".join(lines), markup)


async def show_file(target: Message | CallbackQuery, ctx: AppContext, path: Path) -> None:
    real = ctx.safe.resolve_file(path)
    st = await asyncio.to_thread(os.stat, long_path(real))
    text = (
        f"📄 <b>{esc_fit(real.name, 300)}</b>\n"
        f"📁 <code>{esc_fit(str(real.parent), 600)}</code>\n"
        f"💾 {human_size(st.st_size)} · 🕒 {fmt_dt(st.st_mtime)}"
    )
    if st.st_size > ctx.settings.max_send_bytes:
        text += f"\n⚠️ Больше лимита отправки ({ctx.settings.MAX_SEND_FILE_MB} МБ)"
    parent_token = ctx.token(real.parent)
    await _render(target, text, file_actions_kb(ctx.token(real), parent_token, ctx.settings.PREVIEW_LINES))


@router.message(Command("files"))
@router.message(F.text == BTN_FILES)
async def cmd_files(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await show_roots(message, ctx)


@router.callback_query(FileCb.filter(F.action == "roots"))
async def cb_roots(call: CallbackQuery, ctx: AppContext) -> None:
    await call.answer()
    await show_roots(call, ctx)


@router.callback_query(FileCb.filter(F.action.in_({"dir", "file", "dl", "pv", "ask", "cwd"})))
async def cb_file_action(
    call: CallbackQuery, callback_data: FileCb, bot: Bot, ctx: AppContext, state: FSMContext
) -> None:
    path = _token_path(ctx, callback_data.token)
    if path is None:
        await call.answer("Кнопка устарела (бот перезапускался). Откройте /files заново.", show_alert=True)
        return
    action = callback_data.action
    answered = False

    async def answer(text: str | None = None) -> None:
        nonlocal answered
        if not answered:
            answered = True
            await call.answer(text)

    try:
        if action == "dir":
            await answer()
            await show_dir(call, ctx, path, callback_data.page)
        elif action == "file":
            await answer()
            await show_file(call, ctx, path)
        elif action == "dl":
            await answer("📥 Отправляю...")
            await send_file(bot, call.from_user.id, ctx, path)
        elif action == "pv":
            await answer()
            await _send_preview(bot, call.from_user.id, ctx, path)
        elif action == "ask":
            real = ctx.safe.resolve_file(path)
            await state.set_state(FileAsk.waiting_question)
            await state.update_data(path=str(real))
            await answer()
            await bot.send_message(
                call.from_user.id,
                f"🤖 Напишите вопрос или задачу для Claude по файлу <code>{esc_fit(real.name, 300)}</code>",
                reply_markup=cancel_kb(),
            )
        elif action == "cwd":
            real = ctx.safe.resolve_dir(path)
            mode = ctx.state.run_mode if ctx.state.run_mode == RunMode.CONTINUE else RunMode.NEW
            await ctx.store.update(active_cwd=str(real), run_mode=mode)
            await answer("📌 Рабочая папка выбрана")
            await bot.send_message(
                call.from_user.id,
                f"📌 Рабочая папка Claude: <code>{esc_fit(str(real), 600)}</code>\n"
                f"Режим: {'продолжить последний чат' if mode == RunMode.CONTINUE else 'новый чат'}. "
                "Напишите задачу.",
            )
    except PathAccessError as exc:
        if answered:
            await bot.send_message(call.from_user.id, f"🔒 {esc(exc)}")
        else:
            await call.answer(f"🔒 {truncate(str(exc), 180)}", show_alert=True)


async def _send_preview(bot: Bot, chat_id: int, ctx: AppContext, path: Path) -> None:
    result = await ctx.files.preview(path, ctx.settings.PREVIEW_LINES)
    name = esc_fit(path.name, 200)
    if result.is_binary:
        await bot.send_message(chat_id, f"👁 <b>{name}</b>: бинарный файл ({human_size(result.size)}), предпросмотр недоступен.")
        return
    if not result.text.strip():
        await bot.send_message(chat_id, f"👁 <b>{name}</b>: файл пуст.")
        return
    lang = _LANGS.get(path.suffix.lower())
    body = esc(result.text)
    code = f'<pre><code class="language-{lang}">{body}</code></pre>' if lang else f"<pre>{body}</pre>"
    tail = " · показано не все" if result.truncated else ""
    header = f"👁 <b>{name}</b> · {result.lines_shown} строк · {esc(result.encoding)}{tail}\n"
    await bot.send_message(chat_id, header + code)


async def answer_file_question(bot: Bot, chat_id: int, ctx: AppContext, state: FSMContext, text: str) -> None:
    """Run Claude on the file remembered in FileAsk.waiting_question (used by text and voice)."""
    data = await state.get_data()
    await state.clear()
    try:
        real = ctx.safe.resolve_file(str(data.get("path", "")))
    except PathAccessError as exc:
        await bot.send_message(chat_id, f"🔒 {esc(exc)}")
        return
    active = ctx.active_cwd()
    cwd = active if active is not None and is_within(real, active) else real.parent
    rel = os.path.relpath(real, cwd)
    prompt = f"Работаем с файлом `{rel}` (полный путь: {real}).\n\n{text}"
    request = build_request(ctx, prompt, cwd_override=str(cwd), label=f"[{real.name}] {text}")
    if isinstance(request, str):
        await bot.send_message(chat_id, request)
        return
    await launch_run(bot, chat_id, ctx, request)


@router.message(FileAsk.waiting_question, F.text, ~F.text.startswith("/"), ~F.text.in_(REPLY_BUTTONS))
async def on_file_question(message: Message, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    await answer_file_question(bot, message.chat.id, ctx, state, message.text or "")


@router.callback_query(MenuCb.filter(F.action == "cancel"))
async def cb_cancel(call: CallbackQuery, state: FSMContext) -> None:
    await state.clear()
    await call.answer("Отменено")
    if isinstance(call.message, Message):
        await call.message.edit_reply_markup(reply_markup=None)


@router.message(Command("get"))
async def cmd_get(message: Message, command: CommandObject, bot: Bot, ctx: AppContext) -> None:
    pattern = (command.args or "").strip().strip('"')
    if not pattern:
        await message.answer(
            "🔎 Использование: <code>/get имя_файла</code> или маска <code>/get *.log</code>\n"
            "Поиск идет в текущей рабочей папке (или во всех разрешенных папках)."
        )
        return
    active = ctx.active_cwd()
    roots = [active] if active is not None else ctx.safe.roots
    limit = ctx.settings.SEARCH_RESULTS_LIMIT
    await bot.send_chat_action(message.chat.id, "typing")
    try:
        result = await ctx.files.search(roots, pattern, limit)
    except PathAccessError as exc:
        await message.answer(f"🔒 {esc(exc)}")
        return
    where = esc_fit(str(active), 300) if active else "разрешенных папках"
    if not result.matches:
        note = " (поиск прерван по лимиту)" if result.limited else ""
        await message.answer(f"🔎 «{esc(pattern)}» не найдено в <code>{where}</code>{note}.")
        return
    if len(result.matches) == 1:
        await send_file(bot, message.chat.id, ctx, result.matches[0])
        return
    more = " (показаны первые)" if result.limited or len(result.matches) >= limit else ""
    await message.answer(
        f"🔎 Найдено {len(result.matches)}{more} по «{esc(pattern)}» в <code>{where}</code>:",
        reply_markup=search_results_kb(result.matches, ctx.tokens, active),
    )


# ---------- uploads from the phone ----------

def _upload_dir(ctx: AppContext) -> Path:
    active = ctx.active_cwd()
    if active is not None:
        return active
    target = ctx.safe.roots[0] / ctx.settings.UPLOAD_SUBDIR
    os.makedirs(long_path(target), exist_ok=True)
    return ctx.safe.resolve_dir(target)


@router.message(F.document | F.photo)
async def on_upload(message: Message, ctx: AppContext, state: FSMContext) -> None:
    if message.document is not None:
        file_id = message.document.file_id
        name = message.document.file_name or f"file_{message.message_id}"
        size = message.document.file_size or 0
    else:
        photo = message.photo[-1]  # type: ignore[index]
        file_id = photo.file_id
        name = f"photo_{datetime.now():%Y%m%d_%H%M%S}.jpg"
        size = photo.file_size or 0
    if size > ctx.settings.max_download_bytes:
        await message.answer(
            f"⚠️ Файл {human_size(size)}: Telegram Bot API позволяет боту скачать максимум "
            f"{ctx.settings.MAX_DOWNLOAD_FILE_MB} МБ."
        )
        return
    try:
        target = _upload_dir(ctx)
    except (PathAccessError, OSError) as exc:
        await message.answer(f"🔒 Некуда сохранить файл: {esc(exc)}")
        return
    caption = (message.caption or "").strip()
    await state.set_state(UploadFlow.choosing)
    await state.update_data(file_id=file_id, file_name=name, target=str(target), caption=caption)
    text = (
        f"📎 <b>{esc_fit(name, 200)}</b> ({human_size(size)})\n"
        f"Сохранить в <code>{esc_fit(str(target), 600)}</code>?"
    )
    if caption:
        text += f"\n📝 Подпись станет задачей для Claude: <i>{esc_fit(caption, 500)}</i>"
    await message.answer(text, reply_markup=upload_kb(bool(caption)))


async def _save_upload(bot: Bot, ctx: AppContext, data: dict[str, str]) -> Path:
    target = ctx.safe.resolve_dir(data["target"])
    dest = unique_path(target, sanitize_filename(data["file_name"]))
    ctx.safe.resolve(dest, must_exist=False)
    await bot.download(data["file_id"], destination=long_path(dest))
    log.info("Uploaded file saved to %s", dest)
    return dest


@router.callback_query(UpCb.filter())
async def cb_upload(call: CallbackQuery, callback_data: UpCb, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    data = await state.get_data()
    if await state.get_state() != UploadFlow.choosing.state or "file_id" not in data:
        await call.answer("Загрузка устарела, отправьте файл заново", show_alert=True)
        return
    message = call.message if isinstance(call.message, Message) else None
    if callback_data.action == "cancel":
        await state.clear()
        await call.answer("Отменено")
        if message is not None:
            await message.edit_text("❌ Загрузка отменена")
        return
    await call.answer("💾 Сохраняю...")
    try:
        dest = await _save_upload(bot, ctx, data)
    except (PathAccessError, OSError, TelegramBadRequest, TelegramNetworkError) as exc:
        await state.clear()
        await bot.send_message(call.from_user.id, f"❌ Не удалось сохранить файл: {esc(exc)}")
        return
    await state.clear()
    saved = f"✅ Сохранено: <code>{esc_fit(str(dest), 600)}</code>"
    if callback_data.action == "save":
        if message is not None:
            await message.edit_text(saved, reply_markup=after_upload_kb(ctx.token(dest)))
        return
    caption = data.get("caption", "")
    if message is not None:
        await message.edit_text(saved)
    if caption:
        cwd = ctx.active_cwd() or dest.parent
        rel = os.path.relpath(dest, cwd) if is_within(dest, cwd) else str(dest)
        prompt = f"Пользователь загрузил файл `{rel}` (полный путь: {dest}).\n\n{caption}"
        request = build_request(ctx, prompt, cwd_override=str(cwd), label=f"[{dest.name}] {caption}")
        if isinstance(request, str):
            await bot.send_message(call.from_user.id, request)
            return
        await launch_run(bot, call.from_user.id, ctx, request)
        return
    await state.set_state(FileAsk.waiting_question)
    await state.update_data(path=str(dest))
    await bot.send_message(
        call.from_user.id,
        f"🤖 Напишите задачу для Claude по файлу <code>{esc_fit(dest.name, 300)}</code>",
        reply_markup=cancel_kb(),
    )
