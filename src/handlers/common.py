"""/start, /help, /status, /cancel."""

from __future__ import annotations

import time

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from src.context import AppContext
from src.handlers.runner import MODE_NAMES
from src.handlers.usage import usage_snapshot
from src.keyboards.inline import STATUS_TEXTS, MenuCb, main_reply_kb, status_kb
from src.services.quota import QuotaResult
from src.services.reasoning import ReasoningEffort, effort_label, model_arg
from src.services.state import RunMode, limit_profile_title
from src.services.usage_report import quota_rows, spend_row
from src.services.usage_tracker import UsageSummary
from src.ui import theme
from src.ui.formatter import fit_html
from src.utils.text import esc, esc_fit, human_duration

router = Router(name="common")

COMMANDS_HELP = "\n".join([
    "<b>Команды</b>",
    "/sessions — проекты и чаты Claude, отчеты",
    "/files — проводник по разрешенным папкам",
    "/get <i>имя или *.маска</i> — найти и скачать файл",
    "/usage — лимиты подписки, кэш, расходы (/cost, /stats)",
    "/mode — настройки запуска: режим, доступ, лимиты, авто-отчеты",
    "/model [имя] — модель (Sonnet / Opus / Haiku) и ризонинг",
    "/effort <i>off|low|medium|high|xhigh|max|auto</i> — глубина размышлений",
    "/new [задача] — новый чат в рабочей папке",
    "/continue [задача] — продолжить последний чат папки",
    "/status — состояние и активные процессы",
    "/watch — вкл/выкл авто-отчеты о работе на ПК",
    "/pin — закреп с лимитами, обновляется сам",
    "/restart — безопасно перезапустить бота",
    "/cancel — отменить текущий ввод",
])

HOW_TO = theme.lines(
    theme.item("Выберите проект: 💬 Сессии → проект → 🎯 Рабочая папка или чат → ▶️ Продолжить"),
    theme.item("Напишите задачу текстом или голосом: бот запустит Claude и покажет прогресс"),
    theme.item("📎 Документ или фото сохраняются в рабочую папку, подпись станет задачей"),
)
BRAND = theme.lines(theme.title("claude-remote-ops"), theme.meta("Пульт Claude Code с телефона"))


def _model_line(ctx: AppContext) -> str:
    st = ctx.state
    parts = [theme.model_name(model_arg(st.claude_model) or "default")]
    if st.reasoning_effort != ReasoningEffort.DEFAULT:
        parts.append(f"ризонинг {effort_label(st.reasoning_effort)}")
    return theme.SEP.join(p for p in parts if p)


def context_rows(ctx: AppContext) -> list[str]:
    """Project, chat and mode of the next run: the facts the user needs before typing a task."""
    st = ctx.state
    cwd = ctx.active_cwd()
    rows = [theme.field("Проект", esc(theme.folder_label(cwd)) if cwd else "не выбран")]
    if st.run_mode == RunMode.RESUME and st.active_session_id:
        rows.append(theme.field("Чат", esc_fit(st.active_session_title or "выбранный чат", 80)))
    rows.append(theme.field("Режим", esc(f"{MODE_NAMES[st.run_mode]}{theme.SEP}{_model_line(ctx)}")))
    return rows


def render_help() -> str:
    return theme.join_blocks(BRAND, HOW_TO, theme.quote(COMMANDS_HELP))


def render_start(ctx: AppContext) -> str:
    return theme.join_blocks(BRAND, theme.lines(*context_rows(ctx)), HOW_TO, theme.quote(COMMANDS_HELP))


def _diagnostics(ctx: AppContext) -> str:
    st = ctx.state
    s = ctx.settings
    watcher_on = ctx.watcher is not None and ctx.watcher.running
    access = "полный (--dangerously-skip-permissions)" if st.skip_permissions else "только CLAUDE_ALLOWED_TOOLS"
    if s.VOICE_ENABLED:
        voice = f"Whisper {ctx.transcriber.describe()} · {'сразу запуск' if s.VOICE_AUTO_RUN else 'с подтверждением'}"
    else:
        voice = "выключены"
    rows = [
        "<b>Диагностика</b>",
        f"{theme.BULLET} Claude CLI: <code>{esc_fit(ctx.runner.describe_binary(), 300)}</code>",
        f"{theme.BULLET} Доступ: {esc(access)}",
        f"{theme.BULLET} Лимиты запуска: {esc(limit_profile_title(st.limit_profile, ctx.env_limits()))}",
        f"{theme.BULLET} Таймаут запуска: {human_duration(s.CLAUDE_TIMEOUT_SEC)}",
        f"{theme.BULLET} Авто-отчеты: {'вкл' if watcher_on else 'выкл'}",
        f"{theme.BULLET} Голосовые: {esc_fit(voice, 200)}",
        f"{theme.BULLET} Разрешенные папки:",
        *(f"   <code>{esc_fit(str(root), 200)}</code>" for root in ctx.safe.roots),
    ]
    return theme.quote("\n".join(rows))


def render_status(
    ctx: AppContext, quota: QuotaResult | None = None, summary: UsageSummary | None = None,
    now: float | None = None,
) -> str:
    now = time.time() if now is None else now
    runs = ctx.runner.active_runs
    if runs:
        activity = [f"{theme.DOT_BUSY} <b>Claude работает</b>{theme.SEP}{theme.count(len(runs), 'запуск', 'запуска', 'запусков')}"]
        activity += [
            theme.item(
                f"<code>{run.run_id}</code>{theme.SEP}{theme.duration(run.elapsed)}{theme.SEP}"
                f"{esc(theme.folder_label(run.request.cwd))}{theme.SEP}{esc_fit(run.activity, 100)}"
            )
            for run in runs
        ]
    else:
        activity = [f"{theme.DOT_OK} <b>Claude свободен</b>"]
    usage = [*quota_rows(quota, now)] if ctx.settings.USAGE_QUOTA_ENABLED else []
    usage.append(spend_row(summary))
    return fit_html(theme.join_blocks(
        theme.lines(theme.title("Состояние"), theme.meta(theme.clock(now))),
        theme.lines(*activity),
        theme.lines(*context_rows(ctx)),
        theme.lines(*usage),
        _diagnostics(ctx),
    ))


async def build_status(ctx: AppContext) -> str:
    quota, summary = await usage_snapshot(ctx)
    return render_status(ctx, quota, summary)


def status_markup(ctx: AppContext) -> InlineKeyboardMarkup:
    return status_kb([r.run_id for r in ctx.runner.active_runs])


@router.message(CommandStart())
async def cmd_start(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await message.answer(render_start(ctx), reply_markup=main_reply_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(render_help(), reply_markup=main_reply_kb())


@router.message(Command("status"))
@router.message(F.text.in_(STATUS_TEXTS))
async def cmd_status(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await message.answer(await build_status(ctx), reply_markup=status_markup(ctx))


@router.callback_query(MenuCb.filter(F.action.in_({"status", "status_refresh"})))
async def cb_status(call: CallbackQuery, ctx: AppContext) -> None:
    text = await build_status(ctx)
    if isinstance(call.message, Message):
        try:
            await call.message.edit_text(text, reply_markup=status_markup(ctx))
        except TelegramBadRequest as exc:
            if "not modified" not in exc.message:
                await call.message.answer(text, reply_markup=status_markup(ctx))
    await call.answer("🔄 Обновлено")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    had_state = await state.get_state() is not None
    await state.clear()
    await message.answer("✖️ Отменено" if had_state else "Нечего отменять", reply_markup=main_reply_kb())
