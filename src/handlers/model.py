"""/model and /effort: one-tap model (Sonnet / Opus / Haiku) and reasoning effort switch, persisted in state."""

from __future__ import annotations

import contextlib
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.context import AppContext
from src.keyboards.inline import MenuCb, ModelCb, model_kb
from src.services.reasoning import (
    EFFORT_TITLES,
    MODEL_TITLES,
    ReasoningEffort,
    describe_choice,
    describe_cli,
    effort_label,
    model_label,
    normalize_model,
    parse_effort,
)
from src.utils.text import esc

log = logging.getLogger(__name__)
router = Router(name="model")

MODEL_HELP = (
    "<i>⚡ Sonnet - баланс скорости и качества · 🧠 Opus - самые сложные задачи · "
    "🪶 Haiku - быстро и дешево.\n"
    "🚫 Выкл / 🟢 Low экономят лимиты, 🟠 High / 🔥 Max думают глубже (Max добавляет ultrathink).\n"
    "⚙️ Авто = настройки Claude Code по умолчанию.</i>\n"
    "Любая модель: <code>/model claude-sonnet-5-5</code> · уровень: "
    "<code>/effort off|low|medium|high|xhigh|max|auto</code>"
)


def render_model_menu(ctx: AppContext) -> str:
    st = ctx.state
    return "\n".join([
        "🧠 <b>Модель и Ризонинг</b>",
        "",
        f"<b>{esc(describe_choice(st.claude_model, st.reasoning_effort))}</b>",
        f"🔧 <code>{esc(describe_cli(st.claude_model, st.reasoning_effort))}</code>",
        "Применяется к следующему запуску, в том числе к продолжению чата (--resume). Выбор сохраняется.",
        "",
        MODEL_HELP,
    ])


async def _show_menu(message: Message, ctx: AppContext) -> None:
    await message.answer(render_model_menu(ctx), reply_markup=model_kb(ctx.state))


@router.message(Command("model"))
async def cmd_model(message: Message, command: CommandObject, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not command.args:
        await _show_menu(message, ctx)
        return
    try:
        model = normalize_model(command.args)
    except ValueError as exc:
        await message.answer(f"⚠️ {esc(exc)}\nПример: <code>/model sonnet</code> или <code>/model claude-opus-5-5</code>")
        return
    await ctx.store.update(claude_model=model)
    log.info("Model set to %s via /model", model)
    await message.answer(
        f"✅ {esc(describe_choice(model, ctx.state.reasoning_effort))}", reply_markup=model_kb(ctx.state)
    )


@router.message(Command("effort"))
async def cmd_effort(message: Message, command: CommandObject, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not command.args:
        await _show_menu(message, ctx)
        return
    try:
        effort = parse_effort(command.args)
    except ValueError as exc:
        await message.answer(f"⚠️ {esc(exc)}")
        return
    await ctx.store.update(reasoning_effort=effort)
    log.info("Reasoning effort set to %s via /effort", effort)
    await message.answer(
        f"✅ {esc(describe_choice(ctx.state.claude_model, effort))}", reply_markup=model_kb(ctx.state)
    )


@router.callback_query(MenuCb.filter(F.action == "model"))
async def cb_menu_model(call: CallbackQuery, ctx: AppContext) -> None:
    await call.answer()
    if isinstance(call.message, Message):
        await _show_menu(call.message, ctx)


@router.callback_query(ModelCb.filter())
async def cb_model(call: CallbackQuery, callback_data: ModelCb, ctx: AppContext) -> None:
    st = ctx.state
    if callback_data.kind == "m":
        if callback_data.value not in MODEL_TITLES:
            await call.answer("Неизвестная модель", show_alert=True)
            return
        changed = st.claude_model != callback_data.value
        if changed:
            await ctx.store.update(claude_model=callback_data.value)
        note = f"🧠 Модель: {model_label(callback_data.value)}"
    elif callback_data.kind == "e":
        try:
            effort = ReasoningEffort(callback_data.value)
        except ValueError:
            await call.answer("Неизвестный уровень", show_alert=True)
            return
        changed = st.reasoning_effort != effort
        if changed:
            await ctx.store.update(reasoning_effort=effort)
        note = f"💭 Ризонинг: {EFFORT_TITLES[effort]} ({effort_label(effort)})"
    else:
        await call.answer("Неизвестное действие", show_alert=True)
        return
    if changed and isinstance(call.message, Message):
        log.info("Model/effort switched: %s / %s", ctx.state.claude_model, ctx.state.reasoning_effort)
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_text(render_model_menu(ctx), reply_markup=model_kb(ctx.state))
    await call.answer(note if changed else f"Уже выбрано · {note}")
