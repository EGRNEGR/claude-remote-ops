"""Remote Claude CLI runs from the phone: prompts, live status streaming, kill switch, launch modes."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import replace

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramNetworkError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from src.context import AppContext
from src.handlers.delivery import send_card, send_html, send_paths
from src.keyboards.inline import (
    REPLY_BUTTONS,
    SETTINGS_TEXTS,
    MenuCb,
    ModeCb,
    RunCb,
    background_conflict_kb,
    mode_kb,
    run_done_kb,
    run_status_kb,
)
from src.services.claude_runner import RunnerBusyError, RunRequest, RunState, RunStatus
from src.services.file_manager import PathAccessError
from src.services.reasoning import ReasoningEffort, describe_cli, effort_label, model_arg
from src.services.state import (
    BG_POLICY_TITLES,
    RUN_MODE_TITLES,
    UNLIMITED,
    BgSessionPolicy,
    LimitProfile,
    RunLimits,
    RunMode,
    limit_profile_title,
)
from src.ui import theme
from src.ui.formatter import fit_html
from src.utils.text import esc, esc_fit, human_duration, one_line, truncate
from src.utils.winpath import normcase

log = logging.getLogger(__name__)
router = Router(name="runner")

HEARTBEAT_SEC = 10.0
PROMPT_PREVIEW = 300
HISTORY_SHOWN = 5

_LIVE_TITLES = {
    RunStatus.STARTING: (theme.DOT_IDLE, "Запуск Claude"),
    RunStatus.RUNNING: (theme.DOT_BUSY, "Claude работает"),
}
# Final card: marker and the status word for the meta line (none for a normal finish).
_FINAL_STATUS = {
    RunStatus.FINISHED: (theme.MARK_TITLE, None),
    RunStatus.FAILED: (theme.DOT_ERROR, "Ошибка"),
    RunStatus.KILLED: (theme.DOT_IDLE, "Остановлено"),
    RunStatus.TIMEOUT: (theme.DOT_BUSY, "Превышен лимит времени"),
    RunStatus.STARTING: (theme.DOT_IDLE, "Не запустился"),
    RunStatus.RUNNING: (theme.DOT_BUSY, "Прервано"),
}
_SUBTYPE_HINTS = {
    "error_max_turns": "Достигнут лимит ходов (--max-turns). Нажмите 🔓 «Повторить без ограничений».",
    "error_max_budget_usd": "Достигнут лимит бюджета (--max-budget-usd). Нажмите 🔓 «Повторить без ограничений».",
    "error_during_execution": "Ошибка во время выполнения.",
}
MODE_NAMES = {
    RunMode.NEW: "новый чат",
    RunMode.CONTINUE: "последний чат папки",
    RunMode.RESUME: "выбранный чат",
}
RETRY_PROMPT = (
    "Предыдущий запуск был остановлен ограничениями бота ({reason}). Теперь разрешены все инструменты "
    "(Bash, Skills, MCP) и лимиты сняты. Продолжи с того места, где остановился, и доведи исходную "
    "задачу до конца.\n\nИсходная задача:\n{prompt}"
)


# ---------- rendering ----------

def _model_part(model: str | None, effort: ReasoningEffort) -> list[str]:
    parts = [theme.model_name(model)]
    if effort != ReasoningEffort.DEFAULT:
        parts.append(f"ризонинг {effort_label(effort)}")
    return [p for p in parts if p]


def _request_line(req: RunRequest) -> str:
    prompt = truncate(one_line(req.label or req.prompt), PROMPT_PREVIEW)
    return f"<b>Запрос:</b> <i>{esc(prompt)}</i>"


def _run_flags(req: RunRequest) -> list[str]:
    """Only what differs from the defaults: copy of a chat, restricted tools, limits."""
    flags = []
    if req.fork_session:
        flags.append("копия чата")
    if not req.skip_permissions:
        flags.append("🔒 ограниченный доступ")
    limits = RunLimits(req.max_turns, req.max_budget_usd)
    if not limits.unlimited:
        flags.append(f"лимит {limits.describe()}")
    return flags


def render_status(state: RunState) -> str:
    """Live progress card, edited in place while the CLI works."""
    req = state.request
    marker, label = _LIVE_TITLES.get(state.status, (theme.DOT_BUSY, "Claude работает"))
    head = theme.lines(
        f"{marker} <b>{label}</b>{theme.SEP}{theme.duration(state.elapsed)}",
        theme.meta(
            f"Рабочая папка: {theme.folder_label(req.cwd)}",
            *_model_part(state.model or req.model, req.reasoning),
            *_run_flags(req),
        ),
    )
    now = theme.item(f"<b>Сейчас:</b> {esc_fit(state.activity, 300)}")
    history = list(state.history)[:-1] if state.history and state.history[-1] == state.activity else list(state.history)
    earlier = ""
    if history:
        rows = "\n".join(f"{theme.BULLET} {esc_fit(entry, 160)}" for entry in history[-HISTORY_SHOWN:])
        earlier = theme.quote(rows, expandable=len(history) > 2)
    stats = [f"шагов {state.assistant_messages}", f"инструментов {state.tool_calls}"]
    if state.tool_errors:
        stats.append(f"ошибок {state.tool_errors}")
    return fit_html(theme.join_blocks(head, _request_line(req), theme.lines(now, earlier), theme.meta(*stats)))


def render_final_head(state: RunState, title: str) -> str:
    req = state.request
    marker, status_word = _FINAL_STATUS[state.status]
    return theme.join_blocks(
        theme.lines(
            theme.title(title, marker=marker),
            theme.meta(status_word, f"Рабочая папка: {theme.folder_label(req.cwd)}", theme.clock(time.time())),
        ),
        _request_line(req),
    )


def render_final_tail(state: RunState, branch_note: bool = False) -> str:
    """Hints after the answer (limits, denied tools, compaction, errors) and the footer."""
    result = state.result
    req = state.request
    hints: list[str] = []
    if branch_note:
        hints.append(theme.item("🌿 Это новая ветка чата: исходный чат на ПК не тронут"))
    if state.compacted is not None:
        pre, post = state.compacted
        hints.append(theme.item(f"🧹 Контекст сжат: {pre:,} → {post:,} токенов".replace(",", " ")))
    if result is not None and result.subtype in _SUBTYPE_HINTS:
        hints.append(theme.item(esc(_SUBTYPE_HINTS[result.subtype])))
    if result is not None and result.permission_denials:
        hints.append(theme.item(
            f"⚠️ Отклонено разрешений: {len(result.permission_denials)} "
            f"({esc_fit(', '.join(result.denied_tools), 300)}). "
            "Нажмите 🔓 «Повторить без ограничений» или включите 🔓 Полный доступ в ⚙️ Настройки."
        ))
    if state.error and state.status != RunStatus.FINISHED:
        hints.append(theme.quote(f"<pre>{esc_fit(state.error, 1500)}</pre>", expandable=True))
    if not (result is not None and result.text.strip()) and state.last_text and state.status != RunStatus.FINISHED:
        hints.insert(0, theme.item("<i>Последнее сообщение Claude до остановки — выше</i>"))
    footer = theme.meta(
        *_model_part(state.model or req.model, req.reasoning),
        theme.duration(state.elapsed),
        theme.money(result.cost_usd) if result is not None else None,
        *_run_flags(req),
    )
    return theme.join_blocks(theme.lines(*hints), footer)


def render_compact_final(state: RunState) -> str:
    """Used only when the progress message cannot be deleted: one quiet line pointing at the card."""
    marker, status_word = _FINAL_STATUS[state.status]
    return f"{marker} <b>{esc(status_word or 'Готово')}</b>{theme.SEP}{theme.duration(state.elapsed)} · ответ ниже"


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

    async def retire(self, fallback_text: str) -> None:
        """The final card is sent as a new message (with a notification): remove the progress card."""
        try:
            await self._bot.delete_message(self._chat_id, self._message_id)
        except TelegramAPIError as exc:
            log.info("Progress message not deleted (%s), collapsing it", exc)
            await self.edit(fallback_text, None)


# ---------- launching ----------

def _hint(title: str, *rows: str) -> str:
    return theme.lines(f"{theme.DOT_ERROR} <b>{esc(title)}</b>", *(f"<i>{row}</i>" for row in rows))


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
            return _hint(
                "Не выбран чат для продолжения",
                "Откройте 💬 Сессии → чат → ▶️ Продолжить или смените режим в ⚙️ Настройки",
            )
        raw_cwd = st.active_session_cwd or st.active_cwd or ""
    else:
        raw_cwd = st.active_cwd or ""
    if not raw_cwd:
        return _hint(
            "Не выбрана рабочая папка",
            "Выберите проект в 💬 Сессии или папку в 📁 Файлы и нажмите 🎯 Рабочая папка",
        )
    try:
        cwd = ctx.safe.resolve_dir(raw_cwd)
    except PathAccessError as exc:
        return _hint("Папка недоступна", esc(exc), "Добавьте ее в ALLOWED_DIRS (.env) и перезапустите бота")
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
        model=model_arg(st.claude_model),
        reasoning=st.reasoning_effort,
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
        model=req.model,
        reasoning=req.reasoning,
    )


def build_fork_retry(state: RunState) -> RunRequest:
    """Same prompt in a new branch of the busy chat: --resume <id> --fork-session (full context)."""
    req = state.request
    return replace(req, fork_session=True, label="🌿 копия чата: " + (req.label or req.prompt))


def build_resume_after_stop(state: RunState) -> RunRequest:
    """Same prompt in the original chat once `claude stop <bg_id>` has released it."""
    req = state.request
    return replace(req, fork_session=False, label="🛑 после claude stop: " + (req.label or req.prompt))


def _busy_text(state: RunState) -> str:
    conflict = state.background_conflict
    assert conflict is not None
    bg = esc(conflict.bg_id)
    return theme.join_blocks(
        theme.lines(
            theme.title("Чат занят на ПК", marker="🌿"),
            theme.meta(f"Рабочая папка: {theme.folder_label(state.request.cwd)}", f"фоновая сессия {conflict.bg_id}"),
        ),
        "Этот чат сейчас открыт в фоновой сессии Claude на ПК, поэтому <code>--resume</code> отказался его продолжать.",
        theme.lines(
            theme.item("<b>Ответвить копию</b> — тот же запрос в новой ветке с полным контекстом чата, "
                       "сессия на ПК продолжит работать."),
            theme.item(f"<b>Остановить на ПК</b> — <code>claude stop {bg}</code> (переписка сохранится), "
                       "затем запрос в исходном чате."),
        ),
    )


async def _handle_background_conflict(bot: Bot, chat_id: int, ctx: AppContext, state: RunState) -> None:
    conflict = state.background_conflict
    assert conflict is not None
    # A forked run has a new session id and cannot collide again: never auto-fork twice.
    if ctx.state.bg_session_policy == BgSessionPolicy.FORK and not state.request.fork_session:
        conflict.handled = True
        await bot.send_message(
            chat_id,
            f"🌿 Чат занят фоновой сессией на ПК (<code>{esc(conflict.bg_id)}</code>): "
            "запускаю тот же запрос в копии чата…",
        )
        await launch_run(bot, chat_id, ctx, build_fork_retry(state))
        return
    await send_html(bot, chat_id, _busy_text(state), reply_markup=background_conflict_kb(state.run_id))


async def launch_run(bot: Bot, chat_id: int, ctx: AppContext, request: RunRequest) -> None:
    try:
        ctx.runner.ensure_capacity()
    except RunnerBusyError as exc:
        active = ctx.runner.active_runs
        markup = run_status_kb(active[0].run_id) if active else None
        await bot.send_message(chat_id, f"{theme.DOT_BUSY} <b>Claude занят</b>\n<i>{esc(exc)}</i>", reply_markup=markup)
        return
    status = await bot.send_message(chat_id, f"{theme.DOT_IDLE} <b>Запуск Claude</b>\n{theme.meta(theme.folder_label(request.cwd))}")
    ctx.spawn(_run_and_report(bot, chat_id, ctx, request, status.message_id), name="claude-run")


def _card_title(ctx: AppContext, request: RunRequest, session_id: str | None) -> str:
    st = ctx.state
    if session_id and st.active_session_id == session_id and st.active_session_title and request.mode == RunMode.RESUME:
        return st.active_session_title
    return theme.folder_label(request.cwd)


async def _remember_session(ctx: AppContext, request: RunRequest, session_id: str) -> None:
    st = ctx.state
    if st.active_session_id == session_id:
        title = st.active_session_title
    elif request.fork_session and st.active_session_id == request.session_id and st.active_session_title:
        title = "🌿 " + st.active_session_title.removeprefix("🌿 ")
    else:
        title = truncate(one_line(request.prompt), 60)
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


async def _run_and_report(bot: Bot, chat_id: int, ctx: AppContext, request: RunRequest, message_id: int) -> None:
    updater = StatusUpdater(bot, chat_id, message_id, ctx.settings.STATUS_UPDATE_INTERVAL)
    try:
        state = await ctx.runner.run(
            request,
            on_update=lambda _s: updater.notify(),
            on_started=updater.start,
        )
    except RunnerBusyError as exc:
        await updater.edit(f"{theme.DOT_BUSY} <b>Claude занят</b>\n<i>{esc(exc)}</i>", None)
        return
    finally:
        await updater.stop()
    if ctx.quota_pin is not None:
        ctx.quota_pin.poke()
    if state.background_conflict is not None:
        await updater.retire(render_compact_final(state))
        await _handle_background_conflict(bot, chat_id, ctx, state)
        return

    result = state.result
    session_id = (result.session_id if result else None) or state.session_id
    if session_id and state.status in (RunStatus.FINISHED, RunStatus.FAILED, RunStatus.KILLED):
        await _remember_session(ctx, request, session_id)
    branch = bool(request.fork_session and request.session_id and session_id and request.session_id != session_id)
    markup = run_done_kb(
        state.run_id, len(state.touched_files), session_id,
        offer_unrestricted_retry=state.blocked_by_restrictions,
    )
    if result is not None and result.text.strip():
        answer = result.text
    elif state.last_text and state.status != RunStatus.FINISHED:
        answer = state.last_text
    else:
        answer = ""
    await send_card(
        bot, chat_id,
        render_final_head(state, _card_title(ctx, request, session_id)),
        answer,
        render_final_tail(state, branch_note=branch),
        markup,
        ctx.settings.MAX_INLINE_CHUNKS,
        filename=f"claude_{state.run_id}.md",
    )
    await updater.retire(render_compact_final(state))


# ---------- settings panel ----------

def render_mode(ctx: AppContext) -> str:
    st = ctx.state
    s = ctx.settings
    cwd = ctx.active_cwd()
    watcher_on = ctx.watcher is not None and ctx.watcher.running
    facts = [theme.field("Проект", esc(theme.folder_label(cwd)) if cwd else "не выбран")]
    if st.run_mode == RunMode.RESUME and st.active_session_title:
        facts.append(theme.field("Чат", esc_fit(st.active_session_title, 80)))
    facts += [
        theme.field("Режим", esc(MODE_NAMES[st.run_mode])),
        theme.field("Доступ", "полный" if st.skip_permissions else "только разрешенные инструменты"),
        theme.field("Лимиты", esc(limit_profile_title(st.limit_profile, ctx.env_limits()))),
        theme.field("Модель", esc(theme.SEP.join(_model_part(model_arg(st.claude_model) or "default", st.reasoning_effort)))),
        theme.field("Если чат занят на ПК", esc(BG_POLICY_TITLES[st.bg_session_policy].split(": ", 1)[-1])),
        theme.field("Авто-отчеты", f"{'вкл' if watcher_on else 'выкл'}, пауза {s.WATCH_DEBOUNCE_SEC:g} с"),
    ]
    limits = ctx.run_limits()
    turns = f"--max-turns {limits.max_turns}" if limits.max_turns else "без --max-turns"
    budget = f"--max-budget-usd {limits.max_budget_usd:g}" if limits.max_budget_usd else "без --max-budget-usd"
    access = (
        "--dangerously-skip-permissions (без --allowedTools)" if st.skip_permissions
        else f"--allowedTools {s.CLAUDE_ALLOWED_TOOLS}"
    )
    details = theme.lines(
        "<b>Флаги следующего запуска</b>",
        f"{theme.BULLET} {esc(turns)} · {esc(budget)} · таймаут {human_duration(s.CLAUDE_TIMEOUT_SEC)}",
        f"{theme.BULLET} <code>{esc_fit(access, 400)}</code>",
        f"{theme.BULLET} <code>{esc(describe_cli(st.claude_model, st.reasoning_effort))}</code>",
    )
    return theme.join_blocks(
        theme.lines(theme.title("Настройки"), theme.meta("Применяются к следующему запуску Claude")),
        theme.lines(*facts),
        theme.quote(details),
    )


async def set_watcher(ctx: AppContext, enabled: bool) -> str:
    if ctx.watcher is None:
        return "Авто-отчеты недоступны"
    try:
        if enabled:
            await ctx.watcher.start()
        else:
            await ctx.watcher.stop()
    except OSError as exc:
        return f"Не удалось переключить авто-отчеты: {exc}"
    await ctx.store.update(watcher_enabled=enabled)
    return "👁 Авто-отчеты включены" if enabled else "🙈 Авто-отчеты выключены"


def _mode_markup(ctx: AppContext) -> InlineKeyboardMarkup:
    return mode_kb(ctx.state, ctx.watcher is not None and ctx.watcher.running)


@router.message(Command("mode", "settings"))
@router.message(F.text.in_(SETTINGS_TEXTS))
async def cmd_mode(message: Message, ctx: AppContext, state: FSMContext) -> None:
    await state.clear()
    await message.answer(render_mode(ctx), reply_markup=_mode_markup(ctx))


@router.callback_query(MenuCb.filter(F.action == "mode"))
async def cb_menu_mode(call: CallbackQuery, ctx: AppContext) -> None:
    if isinstance(call.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_text(render_mode(ctx), reply_markup=_mode_markup(ctx))
    await call.answer()


@router.callback_query(ModeCb.filter())
async def cb_mode(call: CallbackQuery, callback_data: ModeCb, ctx: AppContext) -> None:
    action = callback_data.action
    note = ""
    if action in (RunMode.NEW.value, RunMode.CONTINUE.value, RunMode.RESUME.value):
        mode = RunMode(action)
        if mode == RunMode.RESUME and not ctx.state.active_session_id:
            await call.answer("Сначала выберите чат в 💬 Сессии (▶️ Продолжить этот чат)", show_alert=True)
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
    elif action.startswith("bg_"):
        try:
            policy = BgSessionPolicy(action.removeprefix("bg_"))
        except ValueError:
            await call.answer("Неизвестный вариант", show_alert=True)
            return
        await ctx.store.update(bg_session_policy=policy)
        note = BG_POLICY_TITLES[policy]
    elif action == "watch":
        running = ctx.watcher is not None and ctx.watcher.running
        note = await set_watcher(ctx, not running)
    if isinstance(call.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_text(render_mode(ctx), reply_markup=_mode_markup(ctx))
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
    where = f"Рабочая папка: {theme.folder_label(cwd)}" if cwd else "сначала выберите рабочую папку"
    title = "Новый чат" if mode == RunMode.NEW else "Последний чат папки"
    await message.answer(theme.lines(
        theme.title(title, marker="🆕" if mode == RunMode.NEW else "⏩"),
        theme.meta("Следующее сообщение запустит Claude", where),
    ))


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
    await call.answer("🔓 Перезапуск без ограничений…")
    if isinstance(call.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_reply_markup(
                reply_markup=run_done_kb(state.run_id, len(state.touched_files), request.session_id)
            )
    await launch_run(bot, call.from_user.id, ctx, request)


async def _drop_keyboard(call: CallbackQuery) -> None:
    if isinstance(call.message, Message):
        with contextlib.suppress(TelegramBadRequest):
            await call.message.edit_reply_markup(reply_markup=None)


@router.callback_query(RunCb.filter(F.action.in_({"fork", "bgstop"})))
async def cb_background_conflict(call: CallbackQuery, callback_data: RunCb, bot: Bot, ctx: AppContext) -> None:
    state = ctx.runner.get(callback_data.run_id)
    conflict = state.background_conflict if state is not None else None
    if state is None or conflict is None:
        await call.answer("Данные о запуске устарели (бот перезапускался)", show_alert=True)
        return
    if conflict.handled:
        await call.answer("Уже выполняется")
        return
    try:
        ctx.runner.ensure_capacity()  # before `claude stop`: never stop the PC session and then fail to run
        cwd = ctx.safe.resolve_dir(state.request.cwd)
    except RunnerBusyError as exc:
        await call.answer(f"⏳ {truncate(str(exc), 180)}", show_alert=True)
        return
    except PathAccessError as exc:
        await call.answer(f"🔒 {truncate(str(exc), 180)}", show_alert=True)
        return
    conflict.handled = True
    bg = esc(conflict.bg_id)
    if callback_data.action == "fork":
        await call.answer("🌿 Ответвляю копию чата...")
        await _drop_keyboard(call)
        await launch_run(bot, call.from_user.id, ctx, replace(build_fork_retry(state), cwd=cwd))
        return
    await call.answer(f"🛑 claude stop {conflict.bg_id}...")
    stopped, output = await ctx.runner.stop_background(conflict.bg_id)
    if not stopped:
        conflict.handled = False
        await bot.send_message(
            call.from_user.id,
            theme.join_blocks(
                theme.lines(
                    f"{theme.DOT_ERROR} <b>Не удалось остановить фоновую сессию <code>{bg}</code></b>",
                    theme.meta("Можно ответвить копию: она не трогает сессию на ПК"),
                ),
                theme.quote(f"<pre>{esc_fit(output or 'claude stop ничего не вывел', 1500)}</pre>"),
            ),
            reply_markup=background_conflict_kb(state.run_id, allow_stop=False),
        )
        return
    await _drop_keyboard(call)
    await bot.send_message(
        call.from_user.id,
        theme.lines(
            f"🛑 Фоновая сессия <code>{bg}</code> остановлена, переписка сохранена",
            theme.meta(one_line(output), "продолжаю в исходном чате"),
        ),
    )
    await launch_run(bot, call.from_user.id, ctx, replace(build_resume_after_stop(state), cwd=cwd))


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
