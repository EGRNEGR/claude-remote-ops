"""Shared delivery helpers: send files / zips / long texts to Telegram with size and error handling."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from aiogram import Bot
from aiogram.enums import ChatAction
from aiogram.exceptions import TelegramBadRequest, TelegramEntityTooLarge, TelegramNetworkError
from aiogram.types import BufferedInputFile, FSInputFile, InlineKeyboardMarkup

from src.context import AppContext
from src.services.file_manager import FileTooLargeError, PathAccessError
from src.utils.text import TG_SAFE_LIMIT, chunk_text, esc, esc_fit, human_size
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
            caption=caption or f"📄 <code>{esc_fit(str(real), 900)}</code> · {human_size(size)}",
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


async def send_long_text(
    bot: Bot,
    chat_id: int,
    text: str,
    max_chunks: int,
    reply_markup: InlineKeyboardMarkup | None = None,
    filename: str = "response.md",
) -> None:
    """Plain text (no parse mode, so Markdown from Claude cannot break HTML parsing).

    Up to `max_chunks` messages of 4000 chars, otherwise the text goes as a .md document.
    """
    body = text.strip() or "(пустой ответ)"
    chunks = chunk_text(body, TG_SAFE_LIMIT)
    if len(chunks) <= max_chunks:
        for index, chunk in enumerate(chunks):
            last = index == len(chunks) - 1
            await bot.send_message(chat_id, chunk, parse_mode=None, reply_markup=reply_markup if last else None)
        return
    caption = f"📄 Ответ длинный ({len(body)} символов), полностью в файле.\n\n{esc_fit(body, 850)}"
    await bot.send_document(
        chat_id,
        BufferedInputFile(body.encode("utf-8"), filename=filename),
        caption=caption,
        reply_markup=reply_markup,
    )
