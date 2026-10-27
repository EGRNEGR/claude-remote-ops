"""Voice-to-Prompt: voice / audio messages are transcribed by faster-whisper and sent to Claude
through exactly the same pipeline as a typed prompt (current session, mode, access and limits)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import tempfile
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.fsm.context import FSMContext
from aiogram.types import Audio, CallbackQuery, InlineKeyboardMarkup, Message, Voice

from src.context import AppContext
from src.handlers.files import FileAsk, answer_file_question
from src.handlers.runner import build_request, launch_run
from src.keyboards.inline import VoiceCb, voice_confirm_kb
from src.services.transcriber import Transcript, TranscriberError, TranscriberUnavailable
from src.utils.text import esc, esc_fit, human_duration, human_size

log = logging.getLogger(__name__)
router = Router(name="voice")

_MIME_SUFFIX = {
    "audio/ogg": ".ogg", "audio/opus": ".ogg", "audio/mpeg": ".mp3", "audio/mp3": ".mp3",
    "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/aac": ".aac", "audio/wav": ".wav",
    "audio/x-wav": ".wav", "audio/webm": ".webm", "audio/flac": ".flac",
}
_SAFE_EXT_RE = re.compile(r"^\.[A-Za-z0-9]{1,5}$")


def _suffix(media: Voice | Audio) -> str:
    name = getattr(media, "file_name", None) or ""
    ext = os.path.splitext(name)[1].lower()
    if _SAFE_EXT_RE.match(ext):
        return ext
    return _MIME_SUFFIX.get((media.mime_type or "").lower(), ".ogg")


def render_transcript(text: str, transcript: Transcript | None = None) -> str:
    body = f"🎙 <b>Распознанный запрос:</b>\n<blockquote>{esc_fit(text, 3300)}</blockquote>"
    if transcript is None:
        return body
    meta = f"аудио {human_duration(transcript.duration)} · распознано за {transcript.elapsed:.1f} с · {transcript.device}"
    if transcript.language:
        meta += f" · {transcript.language}"
    return f"{body}\n<i>{esc(meta)}</i>"


async def submit_prompt(bot: Bot, chat_id: int, ctx: AppContext, state: FSMContext, text: str) -> str | None:
    """Same route as a typed message. Returns an HTML error to show, or None when the run started."""
    if await state.get_state() == FileAsk.waiting_question.state:
        await answer_file_question(bot, chat_id, ctx, state, text)
        return None
    request = build_request(ctx, text, label=f"🎙 {text}")
    if isinstance(request, str):
        return request
    await launch_run(bot, chat_id, ctx, request)
    return None


async def _edit(message: Message, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    try:
        await message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as exc:
        if "not modified" not in exc.message:
            log.warning("Voice status edit failed: %s", exc.message)
            await message.answer(text, reply_markup=markup)


async def _remove_temp(path: Path) -> None:
    for attempt in range(5):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            # Windows: the decoder may release the handle a moment later.
            await asyncio.sleep(0.2 * (attempt + 1))
    log.warning("Temporary voice file was not removed: %s", path)


@router.message(F.voice | F.audio)
async def on_voice(message: Message, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    settings = ctx.settings
    if not settings.VOICE_ENABLED:
        await message.answer("🎙 Голосовые сообщения выключены (VOICE_ENABLED=false в .env).")
        return
    media: Voice | Audio = message.voice or message.audio  # type: ignore[assignment]
    if media.file_size and media.file_size > settings.max_download_bytes:
        await message.answer(
            f"⚠️ Аудио {human_size(media.file_size)}: Bot API позволяет скачать максимум "
            f"{settings.MAX_DOWNLOAD_FILE_MB} МБ."
        )
        return
    if media.duration and media.duration > settings.VOICE_MAX_DURATION_SEC:
        await message.answer(
            f"⚠️ Слишком длинное аудио ({human_duration(media.duration)}), "
            f"лимит {human_duration(settings.VOICE_MAX_DURATION_SEC)} (VOICE_MAX_DURATION_SEC)."
        )
        return

    first_load = not ctx.transcriber.loaded
    status_text = "🎙 Распознаю голосовое..."
    if first_load:
        status_text += f"\n<i>Первый запуск: загружаю модель Whisper «{esc(ctx.transcriber.model_name)}»...</i>"
    status = await message.reply(status_text)

    fd, tmp_name = tempfile.mkstemp(prefix="tg_voice_", suffix=_suffix(media))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        await bot.download(media.file_id, destination=tmp)
        transcript = await ctx.transcriber.transcribe(tmp)
    except TranscriberUnavailable as exc:
        await _edit(status, f"⚠️ Распознавание речи недоступно: {esc_fit(exc, 800)}")
        return
    except (TranscriberError, TelegramBadRequest, TelegramNetworkError, OSError) as exc:
        log.warning("Voice transcription failed: %s", exc)
        await _edit(status, f"❌ Не удалось распознать голосовое: {esc_fit(exc, 800)}")
        return
    finally:
        await _remove_temp(tmp)

    log.info(
        "Voice transcribed: %.1fs audio in %.1fs on %s (%s chars)",
        transcript.duration, transcript.elapsed, transcript.device, len(transcript.text),
    )
    if not transcript.text:
        await _edit(status, "🤷 Речь не распознана: в сообщении тишина или шум. Попробуйте еще раз.")
        return

    if settings.VOICE_AUTO_RUN:
        await _edit(status, render_transcript(transcript.text, transcript))
        error = await submit_prompt(bot, message.chat.id, ctx, state, transcript.text)
        if error:
            await message.answer(error)
        return
    ctx.remember_voice(status.message_id, transcript.text)
    await _edit(
        status,
        render_transcript(transcript.text, transcript) + "\n\nЗапустить Claude с этим текстом?",
        voice_confirm_kb(),
    )


@router.callback_query(VoiceCb.filter())
async def cb_voice(call: CallbackQuery, callback_data: VoiceCb, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    message = call.message if isinstance(call.message, Message) else None
    text = ctx.pop_voice(message.message_id) if message is not None else None
    if text is None:
        await call.answer("Запрос устарел (бот перезапускался). Отправьте голосовое заново.", show_alert=True)
        return
    if callback_data.action == "cancel":
        await call.answer("❌ Отменено")
        if message is not None:
            await _edit(message, render_transcript(text) + "\n❌ Отменено")
        return
    await call.answer("▶️ Запускаю Claude...")
    if message is not None:
        with contextlib.suppress(TelegramBadRequest):
            await message.edit_reply_markup(reply_markup=None)
    error = await submit_prompt(bot, call.from_user.id, ctx, state, text)
    if error:
        await bot.send_message(call.from_user.id, error)
