"""/usage (/cost, /stats): subscription quotas, session + prompt-cache state, spend by day / project."""

from __future__ import annotations

import asyncio
import contextlib
import logging

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.context import AppContext
from src.handlers.runner import launch_run
from src.keyboards.inline import BTN_USAGE, UsageCb, usage_kb
from src.services.claude_runner import RunRequest
from src.services.file_manager import PathAccessError
from src.services.quota import QuotaResult
from src.services.reasoning import model_arg
from src.services.state import RunMode
from src.services.usage_report import render_dashboard, render_days, render_top, status_line
from src.services.usage_tracker import SessionView
from src.utils.text import esc, esc_fit

log = logging.getLogger(__name__)
router = Router(name="usage")

COMPACT_PROMPT = "/compact"


async def _quota(ctx: AppContext, force: bool = False) -> QuotaResult | None:
    if not ctx.settings.USAGE_QUOTA_ENABLED:
        return None
    return await ctx.quota.get(force=force)


def current_session(ctx: AppContext) -> tuple[SessionView | None, bool]:
    """The chat selected in the bot (if it has transcripts), else the most recently active one."""
    selected = ctx.state.active_session_id
    if selected and ctx.usage.has_session(selected):
        return ctx.usage.session_view(selected), True
    latest = ctx.usage.latest_session_id()
    return (ctx.usage.session_view(latest) if latest else None), False


async def build_dashboard(ctx: AppContext, force_quota: bool = False) -> tuple[str, bool]:
    quota, _ = await asyncio.gather(_quota(ctx, force_quota), ctx.usage.refresh())
    view, selected = current_session(ctx)
    text = render_dashboard(quota, ctx.usage.summary(), view, selected, ctx.settings.USAGE_QUOTA_ENABLED)
    log.debug("Usage dashboard built (transcripts refresh %.3fs)", ctx.usage.last_refresh_sec)
    return text, view is not None


async def usage_status_line(ctx: AppContext) -> str:
    """`📊 Квоты: 5ч 5% · Нед 97% | 💵 Сегодня: $4.13 (7д: $276.12)` for /status (never raises)."""
    try:
        quota, _ = await asyncio.gather(_quota(ctx), ctx.usage.refresh())
        return status_line(quota, ctx.usage.summary())
    except Exception as exc:  # noqa: BLE001 - /status must render even if the analytics fail
        log.warning("Usage line failed: %s", exc)
        return "📊 Квоты и расходы: недоступны"


@router.message(Command("usage", "cost", "stats"))
@router.message(F.text == BTN_USAGE)
async def cmd_usage(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    text, has_session = await build_dashboard(ctx, force_quota=True)
    await message.answer(text, reply_markup=usage_kb("main", has_session))


async def _edit(call: CallbackQuery, text: str, markup) -> None:  # type: ignore[no-untyped-def]
    if not isinstance(call.message, Message):
        return
    with contextlib.suppress(TelegramBadRequest):  # "message is not modified" on a quick double tap
        await call.message.edit_text(text, reply_markup=markup)


@router.callback_query(UsageCb.filter(F.action.in_({"refresh", "days7", "days30", "top"})))
async def cb_usage_view(call: CallbackQuery, callback_data: UsageCb, ctx: AppContext) -> None:
    action = callback_data.action
    if action == "refresh":
        text, has_session = await build_dashboard(ctx, force_quota=True)
        await _edit(call, text, usage_kb("main", has_session))
        await call.answer("🔄 Обновлено")
        return
    await ctx.usage.refresh()
    if action in ("days7", "days30"):
        days = 7 if action == "days7" else 30
        await _edit(call, render_days(ctx.usage.summary(), days), usage_kb(action))
    else:
        text = render_top(
            ctx.usage.top(7, "project"), ctx.usage.top(7, "session"),
            ctx.usage.top(30, "project"), ctx.usage.top(30, "session"),
        )
        await _edit(call, text, usage_kb("top"))
    await call.answer()


@router.callback_query(UsageCb.filter(F.action == "compact"))
async def cb_compact(call: CallbackQuery, ctx: AppContext, bot: Bot) -> None:
    await ctx.usage.refresh()
    view, _ = current_session(ctx)
    if view is None or not view.cwd:
        await call.answer("Нет чата для сжатия: выберите его в 📂 Сессии", show_alert=True)
        return
    busy = {sid for run in ctx.runner.active_runs for sid in (run.session_id, run.request.session_id) if sid}
    if view.session_id in busy:
        await call.answer("Этот чат сейчас работает в боте: дождитесь окончания запуска", show_alert=True)
        return
    try:
        cwd = ctx.safe.resolve_dir(view.cwd)
    except PathAccessError as exc:
        await call.answer(f"Папка чата вне ALLOWED_DIRS: {exc}"[:190], show_alert=True)
        return
    st = ctx.state
    request = RunRequest(
        prompt=COMPACT_PROMPT,
        cwd=cwd,
        mode=RunMode.RESUME,
        session_id=view.session_id,
        skip_permissions=st.skip_permissions,
        label=f"🧹 /compact: сжатие контекста ({view.cache.context_tokens:,} токенов)".replace(",", " "),
        model=model_arg(st.claude_model),
        # No reasoning override: `max` would append "ultrathink" and turn it into /compact instructions.
    )
    await call.answer("🧹 Запускаю /compact")
    if isinstance(call.message, Message):
        await launch_run(bot, call.message.chat.id, ctx, request)


@router.callback_query(UsageCb.filter(F.action == "newchat"))
async def cb_new_chat(call: CallbackQuery, ctx: AppContext) -> None:
    await ctx.usage.refresh()
    view, _ = current_session(ctx)
    raw_cwd = (view.cwd if view else None) or ctx.state.active_cwd
    if not raw_cwd:
        await call.answer("Сначала выберите рабочую папку в 📂 Сессии или 🗂 Файлы", show_alert=True)
        return
    try:
        cwd = ctx.safe.resolve_dir(raw_cwd)
    except PathAccessError as exc:
        await call.answer(f"Папка вне ALLOWED_DIRS: {exc}"[:190], show_alert=True)
        return
    await ctx.store.update(run_mode=RunMode.NEW, active_cwd=str(cwd))
    await call.answer("🆕 Следующее сообщение начнет новый чат")
    if isinstance(call.message, Message):
        old = f" Старый чат <code>{esc(view.session_id[:8])}</code> не тронут." if view else ""
        await call.message.answer(
            f"🆕 <b>Чистая сессия</b>: следующее сообщение начнет новый чат в "
            f"<code>{esc_fit(str(cwd), 300)}</code> без старого контекста и его кэша.{old}\n"
            "Вернуться к старому чату: 📂 Сессии → ▶️ Продолжить этот чат."
        )
