"""/restart and 🔄 «Перезапустить бота» (/status, /mode): safe self-restart with a hand-over to a new process."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, Update

from src.context import AppContext
from src.handlers.common import build_status, status_markup
from src.keyboards.inline import MenuCb, restart_confirm_kb
from src.services.restarter import RestartRequest
from src.ui import theme
from src.ui.formatter import fit_html
from src.utils.text import esc_fit

log = logging.getLogger(__name__)
router = Router(name="restart")

RESTARTING_TEXT = "♻️ <b>Перезапускаю бота…</b>"
RESTARTED_TEXT = "🟢 <b>Бот перезапущен и готов к работе</b>"


@router.message(Command("restart"))
async def cmd_restart(message: Message, bot: Bot, ctx: AppContext, state: FSMContext, event_update: Update) -> None:
    await state.clear()
    await request_restart(bot, ctx, message.chat.id, event_update.update_id)


@router.callback_query(MenuCb.filter(F.action.in_({"restart", "restart_force"})))
async def cb_restart(
    call: CallbackQuery, callback_data: MenuCb, bot: Bot, ctx: AppContext, event_update: Update
) -> None:
    await call.answer()
    force = callback_data.action == "restart_force"
    if force and isinstance(call.message, Message):
        try:
            await call.message.edit_reply_markup(reply_markup=None)
        except TelegramBadRequest as exc:
            log.debug("Confirmation keyboard not removed: %s", exc)
    chat_id = call.message.chat.id if call.message is not None else call.from_user.id
    await request_restart(bot, ctx, chat_id, event_update.update_id, force=force)


async def request_restart(bot: Bot, ctx: AppContext, chat_id: int, update_id: int, force: bool = False) -> None:
    if ctx.restarter.is_replayed(update_id):
        log.info("Ignoring update %s: it is the /restart that started this process", update_id)
        return
    if ctx.restarter.in_progress:
        await bot.send_message(chat_id, "⏳ Перезапуск уже выполняется…")
        return
    runs = ctx.runner.active_runs
    if runs and not force:
        await bot.send_message(
            chat_id,
            theme.lines(
                f"{theme.DOT_BUSY} <b>Сейчас работает Claude: процессов {len(runs)}</b>",
                theme.meta("Перезапуск остановит их (taskkill /F /T), незавершенная работа прервется"),
                "",
                "Перезапустить бота?",
            ),
            reply_markup=restart_confirm_kb(),
        )
        return
    notice = await bot.send_message(chat_id, RESTARTING_TEXT)
    ctx.spawn(_hand_over(bot, ctx, chat_id, notice.message_id, update_id), name="restart")


async def _hand_over(bot: Bot, ctx: AppContext, chat_id: int, message_id: int, update_id: int) -> None:
    result = await ctx.restarter.hand_over(chat_id, message_id, update_id)
    if result.ok:
        return
    text = theme.join_blocks(
        theme.lines(
            f"{theme.DOT_ERROR} <b>Перезапуск отменен</b>",
            theme.meta("Бот продолжает работать в текущем процессе", "логи: logs/bot.log, logs/restart_stderr.log"),
        ),
        theme.quote(f"<pre>{esc_fit(result.detail, 1500)}</pre>"),
    )
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id)
    except TelegramBadRequest:
        await bot.send_message(chat_id, text)


async def announce_restart(bot: Bot, ctx: AppContext, request: RestartRequest) -> None:
    """New process: turn "♻️ Перезапускаю бота…" into the ready notice with the current status."""
    text = fit_html(f"{RESTARTED_TEXT}\n\n{await build_status(ctx)}")
    markup = status_markup(ctx)
    if request.message_id is not None:
        try:
            await bot.edit_message_text(text, chat_id=request.chat_id, message_id=request.message_id, reply_markup=markup)
            return
        except TelegramBadRequest as exc:  # deleted, too old to edit, ...
            log.info("Restart notice not edited (%s), sending a new one", exc)
    try:
        await bot.send_message(request.chat_id, text, reply_markup=markup)
    except TelegramAPIError as exc:
        log.warning("Restart notice not delivered: %s", exc)
