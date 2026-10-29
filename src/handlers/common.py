"""/start, /help, /status, /cancel and the main menu."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from src.context import AppContext
from src.handlers.usage import usage_status_line
from src.keyboards.inline import BTN_STATUS, MenuCb, main_inline_kb, main_reply_kb, status_kb
from src.services.reasoning import describe_choice
from src.services.state import RUN_MODE_TITLES, limit_profile_title
from src.utils.text import esc, esc_fit, human_duration

router = Router(name="common")

HELP_TEXT = (
    "🤖 <b>claude-remote-ops</b> - пульт Claude Code с телефона\n\n"
    "<b>Как пользоваться</b>\n"
    "1. Выберите проект: 📂 Сессии → проект → 🎯 или чат → ▶️ Продолжить\n"
    "2. Просто напишите задачу текстом - бот запустит <code>claude -p</code> и покажет прогресс\n"
    "3. Кнопка 🛑 остановит процесс (<code>taskkill /F /T</code>)\n\n"
    "<b>Команды</b>\n"
    "/sessions - проекты и сессии Claude, отчеты\n"
    "/files - проводник по разрешенным папкам\n"
    "/get <i>имя или *.маска</i> - найти и скачать файл\n"
    "/mode - режим чата, доступ (Full / Restricted), профиль лимитов, авто-отчеты\n"
    "/model [имя] - модель (Sonnet / Opus / Haiku) и уровень ризонинга\n"
    "/effort <i>off|low|medium|high|xhigh|max|auto</i> - глубина размышлений\n"
    "/new [задача] - новый чат в рабочей папке\n"
    "/continue [задача] - продолжить последний чат папки\n"
    "/watch - вкл/выкл авто-отчеты о работе на ПК\n"
    "/status - состояние и активные процессы\n"
    "/usage - лимиты подписки, кэш, расходы по дням и проектам (/cost, /stats)\n"
    "/restart - безопасно перезапустить бота (новый процесс, затем остановка старого)\n"
    "/cancel - отменить текущий ввод\n\n"
    "📎 Отправьте документ или фото - бот сохранит его в рабочую папку и предложит задачу для Claude.\n"
    "🎙 Голосовое или аудио - бот распознает речь (Whisper) и выполнит ее как текстовую задачу."
)


def render_status(ctx: AppContext, usage_line: str = "") -> str:
    st = ctx.state
    cwd = ctx.active_cwd()
    lines = ["📊 <b>Состояние</b>", ""]
    if usage_line:
        lines += [esc(usage_line), ""]
    lines.append(f"📁 Рабочая папка: <code>{esc_fit(str(cwd), 300)}</code>" if cwd else "📁 Рабочая папка: <i>не выбрана</i>")
    if st.active_session_id:
        title = f" «{esc_fit(st.active_session_title, 80)}»" if st.active_session_title else ""
        lines.append(f"💬 Чат: <code>{esc(st.active_session_id)}</code>{title}")
    lines.append(f"🧭 Режим: {esc(RUN_MODE_TITLES[st.run_mode])}")
    access = "🔓 Skip Permissions (Full MCP &amp; Bash)" if st.skip_permissions else "🔒 Restricted Tools"
    lines.append(f"Доступ: {access}")
    lines.append(f"📏 Лимиты: {esc(limit_profile_title(st.limit_profile, ctx.env_limits()))}")
    lines.append(esc(describe_choice(st.claude_model, st.reasoning_effort)))
    lines.append(f"⏰ Таймаут запуска: {human_duration(ctx.settings.CLAUDE_TIMEOUT_SEC)}")
    watcher_on = ctx.watcher is not None and ctx.watcher.running
    lines.append(f"👁 Авто-отчеты: {'ВКЛ' if watcher_on else 'ВЫКЛ'}")
    lines.append(f"🤖 Claude CLI: <code>{esc_fit(ctx.runner.describe_binary(), 300)}</code>")
    if ctx.settings.VOICE_ENABLED:
        run = "сразу запуск" if ctx.settings.VOICE_AUTO_RUN else "с подтверждением"
        lines.append(f"🎙 Голосовые: Whisper {esc_fit(ctx.transcriber.describe(), 200)} · {run}")
    else:
        lines.append("🎙 Голосовые: выключены")
    lines.append("🛡 Разрешенные папки:")
    lines.extend(f"   • <code>{esc_fit(str(root), 200)}</code>" for root in ctx.safe.roots)
    runs = ctx.runner.active_runs
    lines.append("")
    if runs:
        lines.append(f"⚙️ <b>Активные процессы: {len(runs)}</b>")
        for run in runs:
            lines.append(
                f"   • <code>{run.run_id}</code> · PID {run.pid or '?'} · {human_duration(run.elapsed)} · "
                f"{esc_fit(run.activity, 120)}"
            )
    else:
        lines.append("💤 Активных процессов нет")
    return "\n".join(lines)


@router.message(CommandStart())
async def cmd_start(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await message.answer(HELP_TEXT, reply_markup=main_reply_kb())
    await message.answer(render_status(ctx, await usage_status_line(ctx)), reply_markup=main_inline_kb())


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, reply_markup=main_reply_kb())


@router.message(Command("status"))
@router.message(F.text == BTN_STATUS)
async def cmd_status(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await message.answer(
        render_status(ctx, await usage_status_line(ctx)), reply_markup=status_kb([r.run_id for r in ctx.runner.active_runs])
    )


@router.callback_query(MenuCb.filter(F.action == "status"))
async def cb_status(call: CallbackQuery, ctx: AppContext) -> None:
    await call.answer()
    if isinstance(call.message, Message):
        await call.message.answer(
            render_status(ctx, await usage_status_line(ctx)),
            reply_markup=status_kb([r.run_id for r in ctx.runner.active_runs]),
        )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext) -> None:
    had_state = await state.get_state() is not None
    await state.clear()
    await message.answer("❌ Отменено" if had_state else "Нечего отменять", reply_markup=main_reply_kb())
