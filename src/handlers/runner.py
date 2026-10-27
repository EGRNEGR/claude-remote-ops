"""Remote Claude CLI runs from the phone: prompts, live status streaming, kill switch, launch modes."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from src.context import AppContext
from src.handlers.delivery import send_long_text, send_paths
from src.keyboards.inline import (
    BTN_MODE,
    REPLY_BUTTONS,
    MenuCb,
    ModeCb,
    RunCb,
    mode_kb,
    run_done_kb,
    run_status_kb,
)
from src.services.claude_runner import RunnerBusyError, RunRequest, RunState, RunStatus
from src.services.file_manager import PathAccessError
from src.services.state import (
    RUN_MODE_TITLES,
    UNLIMITED,
    LimitProfile,
    RunLimits,
    RunMode,
    limit_profile_title,
)
from src.utils.text import esc, esc_fit, fmt_cost, human_duration, one_line, truncate
from src.utils.winpath import normcase

log = logging.getLogger(__name__)
router = Router(name="runner")

HEARTBEAT_SEC = 10.0

_STATUS_TITLES = {
    RunStatus.STARTING: "⏳ <b>Запуск Claude</b>",
    RunStatus.RUNNING: "⚙️ <b>Claude работает</b>",
    RunStatus.FINISHED: "✅ <b>Готово</b>",
    RunStatus.FAILED: "❌ <b>Ошибка</b>",
    RunStatus.KILLED: "🛑 <b>Остановлено</b>",
    RunStatus.TIMEOUT: "⏰ <b>Превышен лимит времени</b>",
}
_SUBTYPE_HINTS = {
    "error_max_turns": "Достигнут лимит ходов (--max-turns). Нажмите 🔓 «Повторить без ограничений».",
    "error_max_budget_usd": "Достигнут лимит бюджета (--max-budget-usd). Нажмите 🔓 «Повторить без ограничений».",
    "error_during_execution": "Ошибка во время выполнения.",
}
RETRY_PROMPT = (
    "Предыдущий запуск был остановлен ограничениями бота ({reason}). Теперь разрешены все инструменты "
    "(Bash, Skills, MCP) и лимиты сняты. Продолжи с того места, где остановился, и доведи исходную "
    "задачу до конца.\n\nИсходная задача:\n{prompt}"
)


# ---------- rendering ----------

def _mode_line(state: RunState) -> str:
    req = state.request
    line = f"🧭 {esc(RUN_MODE_TITLES[req.mode])}"
    sid = state.session_id or req.session_id
    if sid:
        line += f" · <code>{esc(sid[:8])}</code>"
    line += " · 🔓 full access" if req.skip_permissions else " · 🔒 restricted"
    limits = RunLimits(req.max_turns, req.max_budget_usd)
    line += " · ♾" if limits.unlimited else f" · 💵 {esc(limits.describe())}"
    return line


def render_status(state: RunState) -> str:
    lines = [
        f"{_STATUS_TITLES[state.status]} · ⏱ {human_duration(state.elapsed)}",
        f"📁 <code>{esc_fit(str(state.request.cwd), 300)}</code>",
        _mode_line(state),
        f"🙋 <i>{esc_fit(one_line(state.request.label or state.request.prompt), 200)}</i>",
        "",
        f"▶️ <b>Сейчас:</b> {esc_fit(state.activity, 300)}",
    ]
    history = list(state.history)[:-1] if state.history and state.history[-1] == state.activity else list(state.history)
    if history:
        lines.append("")
        lines.append("🕘 <b>Последние действия:</b>")
        lines.extend(f"• {esc_fit(item, 200)}" for item in history[-5:])
    lines.append("")
    stats = [f"👣 Шагов: {state.assistant_messages}", f"🔧 Инструментов: {state.tool_calls}"]
    if state.touched_files:
        stats.append(f"📝 Файлов: {len(state.touched_files)}")
    if state.tool_errors:
        stats.append(f"❗ Ошибок: {state.tool_errors}")
    lines.append(" · ".join(stats))
    return "\n".join(lines)


def render_final(state: RunState) -> str:
    result = state.result
    lines = [
        f"{_STATUS_TITLES[state.status]} · ⏱ {human_duration(state.elapsed)}",
        f"📁 <code>{esc_fit(str(state.request.cwd), 300)}</code>",
        _mode_line(state),
    ]
    stats = []
    if result is not None:
        stats.append(f"💵 {fmt_cost(result.cost_usd)}")
        if result.num_turns is not None:
            stats.append(f"🔁 Ходов: {result.num_turns}")
    stats.append(f"🔧 Инструментов: {state.tool_calls}")
    if state.touched_files:
        stats.append(f"📝 Файлов: {len(state.touched_files)}")
    if state.tool_errors:
        stats.append(f"❗ Ошибок инструментов: {state.tool_errors}")
    lines.append(" · ".join(stats))
    if state.session_id:
        lines.append(f"🆔 <code>{esc(state.session_id)}</code>")
    if result is not None and result.subtype in _SUBTYPE_HINTS:
        lines.append(f"ℹ️ {_SUBTYPE_HINTS[result.subtype]}")
    if result is not None and result.permission_denials:
        lines.append(
            f"⚠️ Отклонено разрешений: {len(result.permission_denials)} "
            f"({esc_fit(', '.join(result.denied_tools), 300)}). "
            "Нажмите 🔓 «Повторить без ограничений» или включите 🔓 Skip Permissions в /mode."
        )
    if state.error and state.status != RunStatus.FINISHED:
        lines.append("")
        lines.append(f"<pre>{esc_fit(state.error, 1500)}</pre>")
    return "\n".join(lines)


# ---------- live status message ----------

class StatusUpdater:
    """Edits one Telegram message with debounce (>= interval) so FloodWait is not triggered."""

    def __init__(self, bot: Bot, chat_id: int, message_id: int, interval: float) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id
        self._interval = interval
        self._event = asyncio.Event()
        self._stopped = False
        self._state: RunState | None = None
        self._task: asyncio.Task[None] | None = None
        self._last_text = ""

    def start(self, state: RunState) -> None:
        self._state = state
        self._task = asyncio.create_task(self._loop(), name=f"status-{state.run_id}")
        self._event.set()

    def notify(self) -> None:
        self._event.set()

    async def stop(self) -> None:
        self._stopped = True
        self._event.set()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        last_sent = 0.0
        while not self._stopped:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._event.wait(), timeout=HEARTBEAT_SEC)
            self._event.clear()
            delay = self._interval - (time.monotonic() - last_sent)
            if delay > 0:
                await asyncio.sleep(delay)
            if self._stopped or self._state is None:
                break
            await self.edit(render_status(self._state), run_status_kb(self._state.run_id))
            last_sent = time.monotonic()

    async def edit(self, text: str, markup: InlineKeyboardMarkup | None) -> None:
        if text == self._last_text:
            return
        for _ in range(3):
            try:
                await self._bot.edit_message_text(
                    text=text, chat_id=self._chat_id, message_id=self._message_id, reply_markup=markup
                )
                self._last_text = text
                return
            except TelegramRetryAfter as exc:
                log.info("FloodWait on status edit: %ss", exc.retry_after)
                await asyncio.sleep(exc.retry_after + 0.5)
            except TelegramBadRequest as exc:
                if "not modified" not in exc.message:
                    log.warning("Status edit rejected: %s", exc.message)
                return
            except TelegramNetworkError as exc:
                log.warning("Status edit network error: %s", exc)
                await asyncio.sleep(2)


# ---------- launching ----------

def build_request(
    ctx: AppContext,
    prompt: str,
    cwd_override: str | None = None,
    label: str = "",
) -> RunRequest | str:
    """Create a RunRequest from the persisted state. Returns an HTML error message on problems."""
    st = ctx.state
    mode = st.run_mode
    session_id = st.active_session_id if mode == RunMode.RESUME else None
    if cwd_override is not None:
        raw_cwd = cwd_override
        if mode == RunMode.RESUME and normcase(st.active_session_cwd or "") != normcase(cwd_override):
            mode, session_id = RunMode.NEW, None
    elif mode == RunMode.RESUME:
        if not st.active_session_id:
            return (
                "💬 Не выбран чат для <code>--resume</code>.\n"
                "Откройте 📂 Сессии и нажмите ▶️ «Продолжить этот чат» или смените режим в ⚙️ Режим."
            )
        raw_cwd = st.active_session_cwd or st.active_cwd or ""
    else:
        raw_cwd = st.active_cwd or ""
    if not raw_cwd:
        return (
            "📁 Не выбрана рабочая папка.\n"
            "Выберите проект в 📂 Сессии (🎯 Сделать рабочей папкой) или папку в 🗂 Файлы (📌)."
        )
    try:
        cwd = ctx.safe.resolve_dir(raw_cwd)
    except PathAccessError as exc:
        return f"🔒 {esc(exc)}\nДобавьте папку в ALLOWED_DIRS (.env) и перезапустите бота."
    limits = ctx.run_limits()
    return RunRequest(
        prompt=prompt,
        cwd=cwd,
        mode=mode,
        session_id=session_id,
        skip_permissions=st.skip_permissions,
        label=label,
        max_turns=limits.max_turns,
        max_budget_usd=limits.max_budget_usd,
    )


def build_unrestricted_retry(state: RunState) -> RunRequest:
    """Same task with --dangerously-skip-permissions and no turn/budget limits (resume if possible)."""
    req = state.request
    result = state.result
    session_id = (result.session_id if result else None) or state.session_id
    reasons: list[str] = []
    if result is not None and result.permission_denials:
        reasons.append("отклонены разрешения: " + ", ".join(result.denied_tools))
    if result is not None and result.hit_limit:
        reasons.append(result.subtype)
    if session_id:
        prompt = RETRY_PROMPT.format(reason="; ".join(reasons) or "ограничения", prompt=req.prompt)
        mode = RunMode.RESUME
    else:
        prompt, mode = req.prompt, req.mode
    return RunRequest(
        prompt=prompt,
        cwd=req.cwd,
        mode=mode,
        session_id=session_id if mode == RunMode.RESUME else req.session_id,
        skip_permissions=True,
        label="🔓 повтор без ограничений: " + (req.label or req.prompt),
        max_turns=UNLIMITED.max_turns,
        max_budget_usd=UNLIMITED.max_budget_usd,
    )


async def launch_run(bot: Bot, chat_id: int, ctx: AppContext, request: RunRequest) -> None:
    try:
        ctx.runner.ensure_capacity()
    except RunnerBusyError as exc:
        active = ctx.runner.active_runs
        markup = run_status_kb(active[0].run_id) if active else None
        await bot.send_message(chat_id, f"⏳ {esc(exc)}", reply_markup=markup)
        return
    status = await bot.send_message(chat_id, "⏳ <b>Запуск Claude CLI...</b>")
    ctx.spawn(_run_and_report(bot, chat_id, ctx, request, status.message_id), name="claude-run")


async def _run_and_report(bot: Bot, chat_id: int, ctx: AppContext, request: RunRequest, message_id: int) -> None:
    updater = StatusUpdater(bot, chat_id, message_id, ctx.settings.STATUS_UPDATE_INTERVAL)
    try:
        state = await ctx.runner.run(
            request,
            on_update=lambda _s: updater.notify(),
            on_started=updater.start,
        )
    except RunnerBusyError as exc:
        await updater.edit(f"⏳ {esc(exc)}", None)
        return
    finally:
        await updater.stop()
    result = state.result
    session_id = (result.session_id if result else None) or state.session_id
    await updater.edit(render_final(state), None)

    if session_id and state.status in (RunStatus.FINISHED, RunStatus.FAILED, RunStatus.KILLED):
        st = ctx.state
        title = st.active_session_title if st.active_session_id == session_id else truncate(one_line(request.prompt), 60)
        await ctx.store.update(
            active_session_id=session_id,
            active_session_cwd=str(request.cwd),
            active_cwd=str(request.cwd),
            active_session_title=title,
            run_mode=RunMode.RESUME,
        )
        meta = await ctx.inspector.find_session(session_id)
        if meta is not None and ctx.watcher is not None:
            ctx.watcher.mark_consumed(meta.path)

    markup = run_done_kb(
        state.run_id, len(state.touched_files), session_id,
        offer_unrestricted_retry=state.blocked_by_restrictions,
    )
    if result is not None and result.text.strip():
        await send_long_text(bot, chat_id, result.text, ctx.settings.MAX_INLINE_CHUNKS, markup)
    elif state.last_text and state.status != RunStatus.FINISHED:
        await send_long_text(
            bot, chat_id, "Последнее сообщение Claude до остановки:\n\n" + state.last_text,
            ctx.settings.MAX_INLINE_CHUNKS, markup,
        )
    elif markup is not None:
        await bot.send_message(chat_id, "📎 Действия по завершенному запуску:", reply_markup=markup)
    if session_id and state.status in (RunStatus.FINISHED, RunStatus.FAILED, RunStatus.KILLED):
        await bot.send_message(
            chat_id,
            "↪️ Следующее сообщение продолжит этот чат. /new - начать новый чат в этой папке.",
        )


# ---------- mode panel ----------

def render_mode(ctx: AppContext) -> str:
    st = ctx.state
    s = ctx.settings
    cwd = ctx.active_cwd()
    lines = ["⚙️ <b>Режим запуска Claude</b>", ""]
    lines.append(f"📁 Рабочая папка: <code>{esc_fit(str(cwd), 300)}</code>" if cwd else "📁 Рабочая папка: <i>не выбрана</i>")
    if st.active_session_id:
        title = f" «{esc_fit(st.active_session_title, 80)}»" if st.active_session_title else ""
        lines.append(f"💬 Выбранный чат: <code>{esc(st.active_session_id[:8])}</code>{title}")
    lines.append(f"🧭 Режим: <b>{esc(RUN_MODE_TITLES[st.run_mode])}</b>")
    if st.skip_permissions:
        lines.append("🔓 Доступ: <b>Skip Permissions (Full MCP &amp; Bash)</b>")
    else:
        lines.append("🔒 Доступ: <b>Restricted Tools</b> (только CLAUDE_ALLOWED_TOOLS)")
    lines.append(f"📏 Лимиты: <b>{esc(limit_profile_title(st.limit_profile, ctx.env_limits()))}</b>")
    watcher_on = ctx.watcher is not None and ctx.watcher.running
    lines.append(f"👁 Авто-отчеты: {'ВКЛ' if watcher_on else 'ВЫКЛ'} (пауза {s.WATCH_DEBOUNCE_SEC:g} с)")
    lines.append("")
    limits = ctx.run_limits()
    turns = f"--max-turns {limits.max_turns}" if limits.max_turns else "без --max-turns"
    budget = f"--max-budget-usd {limits.max_budget_usd:g}" if limits.max_budget_usd else "без --max-budget-usd"
    lines.append(f"🛠 {turns} · {budget} · таймаут {human_duration(s.CLAUDE_TIMEOUT_SEC)}")
    if st.skip_permissions:
        lines.append("🔧 Флаг: <code>--dangerously-skip-permissions</code> (--allowedTools не передается)")
    else:
        lines.append(f"🔧 allowedTools: <code>{esc_fit(s.CLAUDE_ALLOWED_TOOLS, 400)}</code>")
    return "\n".join(lines)


async def set_watcher(ctx: AppContext, enabled: bool) -> str:
    if ctx.watcher is None:
        return "Watcher не инициализирован"
    try:
        if enabled:
            await ctx.watcher.start()
        else:
            await ctx.watcher.stop()
    except OSError as exc:
        return f"Не удалось переключить авто-отчеты: {exc}"
    await ctx.store.update(watcher_enabled=enabled)
    return "👁 Авто-отчеты включены" if enabled else "🙈 Авто-отчеты выключены"


@router.message(Command("mode"))
@router.message(F.text == BTN_MODE)
async def cmd_mode(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    watcher_on = ctx.watcher is not None and ctx.watcher.running
    await message.answer(render_mode(ctx), reply_markup=mode_kb(ctx.state, watcher_on, ctx.env_limits()))


@router.callback_query(MenuCb.filter(F.action == "mode"))
async def cb_menu_mode(call: CallbackQuery, ctx: AppContext) -> None:
    watcher_on = ctx.watcher is not None and ctx.watcher.running
    if isinstance(call.message, Message):
        await call.message.answer(render_mode(ctx), reply_markup=mode_kb(ctx.state, watcher_on, ctx.env_limits()))
    await call.answer()


@router.callback_query(ModeCb.filter())
async def cb_mode(call: CallbackQuery, callback_data: ModeCb, ctx: AppContext) -> None:
    action = callback_data.action
    note = ""
    if action in (RunMode.NEW.value, RunMode.CONTINUE.value, RunMode.RESUME.value):
        mode = RunMode(action)
        if mode == RunMode.RESUME and not ctx.state.active_session_id:
            await call.answer("Сначала выберите чат в 📂 Сессии (▶️ Продолжить этот чат)", show_alert=True)
            return
        await ctx.store.update(run_mode=mode)
        note = RUN_MODE_TITLES[mode]
    elif action in ("perm_full", "perm_restricted", "skip"):
        enabled = (action == "perm_full") if action != "skip" else not ctx.state.skip_permissions
        await ctx.store.update(skip_permissions=enabled)
        note = (
            "🔓 Полный доступ: любые Bash, Skills и MCP без подтверждений"
            if enabled else "🔒 Только инструменты из CLAUDE_ALLOWED_TOOLS"
        )
    elif action.startswith("lim_"):
        try:
            profile = LimitProfile(action.removeprefix("lim_"))
        except ValueError:
            await call.answer("Неизвестный профиль", show_alert=True)
            return
        await ctx.store.update(limit_profile=profile)
        note = f"📏 {limit_profile_title(profile, ctx.env_limits())}"
    elif action == "watch":
        running = ctx.watcher is not None and ctx.watcher.running
        note = await set_watcher(ctx, not running)
    watcher_on = ctx.watcher is not None and ctx.watcher.running
    if isinstance(call.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_text(render_mode(ctx), reply_markup=mode_kb(ctx.state, watcher_on, ctx.env_limits()))
    await call.answer(note)


@router.message(Command("watch"))
async def cmd_watch(message: Message, ctx: AppContext) -> None:
    running = ctx.watcher is not None and ctx.watcher.running
    await message.answer(esc(await set_watcher(ctx, not running)))


@router.message(Command("new", "continue"))
async def cmd_new(message: Message, command: CommandObject, bot: Bot, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    mode = RunMode.NEW if command.command == "new" else RunMode.CONTINUE
    await ctx.store.update(run_mode=mode)
    if command.args and command.args.strip():
        request = build_request(ctx, command.args.strip())
        if isinstance(request, str):
            await message.answer(request)
            return
        await launch_run(bot, message.chat.id, ctx, request)
        return
    cwd = ctx.active_cwd()
    where = f" в <code>{esc_fit(str(cwd), 300)}</code>" if cwd else " (сначала выберите папку)"
    await message.answer(f"{esc(RUN_MODE_TITLES[mode])}: следующее сообщение запустит Claude{where}.")


# ---------- kill switch / results ----------

@router.callback_query(RunCb.filter(F.action == "kill"))
async def cb_kill(call: CallbackQuery, callback_data: RunCb, ctx: AppContext) -> None:
    killed = await ctx.runner.kill(callback_data.run_id)
    await call.answer("🛑 Дерево процессов остановлено (taskkill /F /T)" if killed else "Процесс уже завершен")


@router.callback_query(RunCb.filter(F.action == "retry"))
async def cb_retry_unrestricted(call: CallbackQuery, callback_data: RunCb, bot: Bot, ctx: AppContext) -> None:
    state = ctx.runner.get(callback_data.run_id)
    if state is None:
        await call.answer("Данные о запуске устарели (бот перезапускался)", show_alert=True)
        return
    request = build_unrestricted_retry(state)
    try:
        request.cwd = ctx.safe.resolve_dir(request.cwd)
    except PathAccessError as exc:
        await call.answer(f"🔒 {truncate(str(exc), 180)}", show_alert=True)
        return
    await call.answer("🔓 Перезапуск без ограничений...")
    if isinstance(call.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_reply_markup(
                reply_markup=run_done_kb(state.run_id, len(state.touched_files), request.session_id)
            )
    await launch_run(bot, call.from_user.id, ctx, request)


@router.callback_query(RunCb.filter(F.action == "files"))
async def cb_run_files(call: CallbackQuery, callback_data: RunCb, bot: Bot, ctx: AppContext) -> None:
    state = ctx.runner.get(callback_data.run_id)
    if state is None:
        await call.answer("Данные о запуске устарели (бот перезапускался)", show_alert=True)
        return
    await call.answer("Собираю файлы...")
    await send_paths(
        bot, call.from_user.id, ctx, list(state.touched_files.values()), state.request.cwd, f"changes_{state.run_id}"
    )


# ---------- free text = task for Claude (registered last) ----------

@router.message(StateFilter(None), F.text, ~F.text.startswith("/"), ~F.text.in_(REPLY_BUTTONS))
async def on_prompt(message: Message, bot: Bot, ctx: AppContext) -> None:
    request = build_request(ctx, message.text or "")
    if isinstance(request, str):
        await message.answer(request)
        return
    await launch_run(bot, message.chat.id, ctx, request)
