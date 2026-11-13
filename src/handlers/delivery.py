"""Shared delivery helpers: send files / zips / long texts to Telegram with size and error handling."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramEntityTooLarge, TelegramNetworkError
from aiogram.types import BufferedInputFile, FSInputFile, InlineKeyboardMarkup, Message

from src.context import AppContext
from src.services.file_manager import FileTooLargeError, PathAccessError
from src.ui.formatter import answer_parts, fit_html, plain_fallback, tg_size
from src.ui import theme
from src.ui.theme import item, join_blocks
from src.utils.text import TG_SAFE_LIMIT, esc, esc_fit, human_size
from src.utils.winpath import long_path

log = logging.getLogger(__name__)


async def send_file(bot: Bot, chat_id: int, ctx: AppContext, path: Path, caption: str | None = None) -> bool:
    limit = ctx.settings.max_send_bytes
    try:
        real = ctx.safe.resolve_file(path)
        size = await ctx.files.ensure_sendable(real, limit)
    except FileTooLargeError as exc:
        await bot.send_message(
            chat_id,
            f"⚠️ Файл <code>{esc(exc.path.name)}</code> весит {human_size(exc.size)}, "
            f"лимит Telegram для ботов {human_size(exc.limit)}.",
        )
        return False
    except PathAccessError as exc:
        await bot.send_message(chat_id, f"🔒 {esc(exc)}")
        return False
    await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    try:
        await bot.send_document(
            chat_id,
            FSInputFile(long_path(real), filename=real.name),
            caption=caption or theme.lines(
                f"📄 <b>{esc_fit(real.name, 300)}</b>", theme.meta(theme.folder_label(real.parent), human_size(size))
            ),
        )
    except TelegramEntityTooLarge:
        await bot.send_message(chat_id, f"⚠️ Telegram отклонил файл {esc(real.name)}: слишком большой.")
        return False
    except (TelegramBadRequest, TelegramNetworkError, OSError) as exc:
        log.warning("send_document failed for %s: %s", real, exc)
        await bot.send_message(chat_id, f"❌ Не удалось отправить {esc(real.name)}: {esc(exc)}")
        return False
    return True


async def send_paths(
    bot: Bot,
    chat_id: int,
    ctx: AppContext,
    paths: Sequence[Path],
    base: Path | None,
    zip_prefix: str,
) -> None:
    """Send one file directly or several files as a zip archive. Skips missing / forbidden files."""
    allowed: list[Path] = []
    forbidden = missing = 0
    for path in paths:
        try:
            allowed.append(ctx.safe.resolve_file(path))
        except PathAccessError as exc:
            if "вне разрешенных" in str(exc):
                forbidden += 1
            else:
                missing += 1
    notes = []
    if missing:
        notes.append(f"не найдено: {missing}")
    if forbidden:
        notes.append(f"вне ALLOWED_DIRS: {forbidden}")
    note = f" ({', '.join(notes)})" if notes else ""
    if not allowed:
        await bot.send_message(chat_id, f"📭 Нет доступных файлов для отправки{esc(note)}.")
        return
    if len(allowed) == 1:
        await send_file(bot, chat_id, ctx, allowed[0])
        if note:
            await bot.send_message(chat_id, f"ℹ️ Остальные файлы пропущены{esc(note)}.")
        return
    await bot.send_chat_action(chat_id, ChatAction.UPLOAD_DOCUMENT)
    data, included = await ctx.files.zip_files(allowed, base, ctx.settings.max_send_bytes)
    if not included:
        await bot.send_message(chat_id, "⚠️ Файлы слишком большие для отправки одним архивом.")
        return
    skipped = len(allowed) - len(included)
    if skipped:
        notes.append(f"не влезло в лимит: {skipped}")
        note = f" ({', '.join(notes)})"
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    listing = "\n".join(f"• {esc_fit(p.name, 70)}" for p in included[:10])
    more = f"\n… и еще {len(included) - 10}" if len(included) > 10 else ""
    caption = f"📦 Файлов: {len(included)}{esc(note)}\n{listing}{more}"
    try:
        await bot.send_document(
            chat_id,
            BufferedInputFile(data, filename=f"{zip_prefix}_{stamp}.zip"),
            caption=caption,
        )
    except (TelegramBadRequest, TelegramNetworkError) as exc:
        log.warning("send zip failed: %s", exc)
        await bot.send_message(chat_id, f"❌ Не удалось отправить архив: {esc(exc)}")


def _entities_rejected(exc: TelegramBadRequest) -> bool:
    text = exc.message.lower()
    return "parse entities" in text or "can't find end" in text or "unsupported start tag" in text


async def send_html(
    bot: Bot,
    chat_id: int,
    text: str,
    reply_markup: InlineKeyboardMarkup | None = None,
    disable_notification: bool | None = None,
) -> Message:
    """send_message that never fails on markup: an oversized text is cut at a safe line, rejected
    entities fall back to the same text without formatting."""
    body = fit_html(text, TG_SAFE_LIMIT)
    try:
        return await bot.send_message(
            chat_id, body, reply_markup=reply_markup, disable_notification=disable_notification
        )
    except TelegramBadRequest as exc:
        if not _entities_rejected(exc):
            raise
        log.warning("Telegram rejected HTML (%s), sending plain text", exc.message)
        return await bot.send_message(
            chat_id, plain_fallback(body), reply_markup=reply_markup, disable_notification=disable_notification
        )


async def edit_html(
    bot: Bot, chat_id: int, message_id: int, text: str, reply_markup: InlineKeyboardMarkup | None = None
) -> bool:
    """edit_message_text with the same fallbacks; False when Telegram refused the edit."""
    body = fit_html(text, TG_SAFE_LIMIT)
    for attempt in (body, plain_fallback(body)):
        try:
            await bot.edit_message_text(attempt, chat_id=chat_id, message_id=message_id, reply_markup=reply_markup)
            return True
        except TelegramBadRequest as exc:
            if "not modified" in exc.message:
                return True
            if not _entities_rejected(exc):
                log.info("Edit refused: %s", exc.message)
                return False
            log.warning("Telegram rejected HTML on edit (%s), retrying as plain text", exc.message)
    return False


async def send_card(
    bot: Bot,
    chat_id: int,
    head: str,
    answer: str,
    tail: str,
    reply_markup: InlineKeyboardMarkup | None,
    max_messages: int,
    filename: str = "response.md",
) -> None:
    """A report card with Claude's answer: `head`, the answer in quote blocks, `tail` (hints + footer).

    The answer continues in follow-up messages when it does not fit; past `max_messages` the card keeps the
    beginning and the full answer goes as a .md document. The keyboard is attached to the last message.
    """
    margin = 8  # the blank-line separators added by join_blocks
    first_budget = TG_SAFE_LIMIT - tg_size(head) - tg_size(tail) - margin
    next_budget = TG_SAFE_LIMIT - tg_size(tail) - margin
    parts = answer_parts(answer, first_budget, next_budget) if answer.strip() else []
    if len(parts) <= max(1, max_messages):
        if len(parts) <= 1:
            await send_html(bot, chat_id, join_blocks(head, parts[0] if parts else "", tail), reply_markup)
            return
        await send_html(bot, chat_id, join_blocks(head, parts[0]))
        for part in parts[1:-1]:
            await send_html(bot, chat_id, part, disable_notification=True)
        await send_html(bot, chat_id, join_blocks(parts[-1], tail), reply_markup, disable_notification=True)
        return
    note = item(f"<i>Ответ длинный ({len(answer)} символов): полностью в файле ниже</i>")
    first_budget -= tg_size(note) + 2
    preview = answer_parts(answer, first_budget, next_budget)[0]
    await send_html(bot, chat_id, join_blocks(head, preview, note, tail), reply_markup)
    await bot.send_document(
        chat_id,
        BufferedInputFile(answer.strip().encode("utf-8"), filename=filename),
        caption=f"📄 Полный ответ · {len(answer)} символов",
        disable_notification=True,
    )
