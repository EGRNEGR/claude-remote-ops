"""Entry point: claude-remote-ops Telegram bot (Windows / PowerShell friendly)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, ErrorEvent, LinkPreviewOptions
from pydantic import ValidationError

from src.config import Settings, get_settings
from src.context import AppContext, build_context
from src.handlers import common, files, model, restart, runner, sessions, usage, voice
from src.middlewares.auth import AuthMiddleware
from src.services.session_parser import SessionReport
from src.services.watcher import SessionWatcher
from src.utils.single_instance import InstanceLock
from src.utils.text import esc_fit
from src.utils.winpath import long_path

log = logging.getLogger("claude_remote_bot")

EXIT_CONFIG_ERROR = 2
EXIT_ALREADY_RUNNING = 3  # start_bot.ps1 does not restart on this code

BOT_COMMANDS = [
    BotCommand(command="start", description="Главное меню"),
    BotCommand(command="sessions", description="Проекты и сессии Claude"),
    BotCommand(command="files", description="Файловый менеджер"),
    BotCommand(command="get", description="Найти и скачать файл"),
    BotCommand(command="mode", description="Режим запуска Claude"),
    BotCommand(command="model", description="Модель и уровень ризонинга"),
    BotCommand(command="effort", description="Ризонинг: off|low|medium|high|max|auto"),
    BotCommand(command="new", description="Новый чат в рабочей папке"),
    BotCommand(command="continue", description="Продолжить последний чат папки"),
    BotCommand(command="status", description="Состояние и процессы"),
    BotCommand(command="usage", description="Лимиты, кэш и расходы ($)"),
    BotCommand(command="cost", description="Расходы и лимиты (= /usage)"),
    BotCommand(command="watch", description="Вкл/выкл авто-отчеты"),
    BotCommand(command="restart", description="Перезапустить бота"),
    BotCommand(command="help", description="Справка"),
]


def setup_console_utf8() -> None:
    """Windows consoles default to cp866/cp1251: force UTF-8 so Cyrillic/emoji logs never crash."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="replace")


def setup_logging(settings: Settings) -> None:
    os.makedirs(long_path(settings.LOG_DIR), exist_ok=True)
    fmt = logging.Formatter("%(asctime)s | %(levelname)-7s | %(name)s | %(message)s", "%Y-%m-%d %H:%M:%S")
    file_handler = RotatingFileHandler(
        settings.LOG_DIR / "bot.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(settings.LOG_LEVEL.upper())
    root.addHandler(file_handler)
    root.addHandler(console)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)


async def on_startup(bot: Bot, ctx: AppContext) -> None:
    settings = ctx.settings
    await ctx.store.load()
    # Parse ~/.claude/projects once in the background: the first /usage is then incremental (ms).
    ctx.spawn(ctx.usage.refresh(), "usage-warmup")
    await bot.set_my_commands(BOT_COMMANDS)
    admin_id = settings.ADMIN_USER_ID

    async def notify(report: SessionReport) -> None:
        await sessions.push_session_report(bot, admin_id, report)

    ctx.watcher = SessionWatcher(
        inspector=ctx.inspector,
        debounce_sec=settings.WATCH_DEBOUNCE_SEC,
        notify=notify,
        busy_sessions=ctx.runner.busy_session_ids,
        ignore_headless=settings.WATCH_IGNORE_HEADLESS,
    )
    watcher_note = "выключены"
    if ctx.state.watcher_enabled:
        try:
            await ctx.watcher.start()
            watcher_note = "включены"
        except OSError as exc:
            watcher_note = f"ошибка: {exc}"
            log.warning("Watcher not started: %s", exc)
    claude = ctx.runner.describe_binary()
    log.info("Bot started (PID %s). Claude CLI: %s", os.getpid(), claude)
    if ctx.restarter.pending is not None:
        await restart.announce_restart(bot, ctx, ctx.restarter.pending)
        await ctx.restarter.clear()
        return
    try:
        await bot.send_message(
            admin_id,
            "🟢 <b>claude-remote-ops запущен</b>\n"
            f"🤖 Claude CLI: <code>{esc_fit(claude, 300)}</code>\n"
            f"👁 Авто-отчеты: {esc_fit(watcher_note, 200)}\n/start - меню",
        )
    except TelegramAPIError as exc:
        log.warning("Cannot notify admin (send /start to the bot first): %s", exc)


async def on_shutdown(bot: Bot, ctx: AppContext) -> None:
    log.info("Shutting down...")
    if ctx.watcher is not None:
        await ctx.watcher.stop()
    await ctx.runner.kill_all()
    await ctx.store.save()
    if ctx.restarter.handed_over:
        log.info("Polling handed over to the new process")
        return
    try:
        await bot.send_message(ctx.settings.ADMIN_USER_ID, "🔴 claude-remote-ops остановлен")
    except TelegramAPIError as exc:
        log.debug("Shutdown notice not delivered: %s", exc)


async def on_error(event: ErrorEvent, bot: Bot, ctx: AppContext) -> bool:
    log.error("Unhandled error in update %s", event.update.update_id, exc_info=event.exception)
    try:
        await bot.send_message(
            ctx.settings.ADMIN_USER_ID,
            f"⚠️ Ошибка: <code>{esc_fit(f'{type(event.exception).__name__}: {event.exception}', 800)}</code>",
        )
    except TelegramAPIError as exc:
        log.debug("Error notice not delivered: %s", exc)
    return True


async def confirm_updates(bot: Bot, update_id: int) -> None:
    """Mark updates up to `update_id` as handled on Telegram's side (aiogram commits the offset lazily)."""
    try:
        await bot.get_updates(offset=update_id + 1, limit=1, timeout=0)
    except TelegramAPIError as exc:
        log.warning("Cannot confirm update %s: %s", update_id, exc)


async def run_bot(settings: Settings) -> int:
    ctx = build_context(settings)
    bot = Bot(
        token=settings.BOT_TOKEN.get_secret_value(),
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview=LinkPreviewOptions(is_disabled=True),
        ),
    )
    dp = Dispatcher(storage=MemoryStorage())
    dp["ctx"] = ctx
    dp.update.outer_middleware(AuthMiddleware(settings.ADMIN_USER_ID))
    # Order matters: the runner router holds the catch-all "free text = prompt" handler.
    dp.include_routers(
        common.router, restart.router, model.router, usage.router, sessions.router, files.router, voice.router,
        runner.router,
    )
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)
    dp.errors.register(on_error)
    ctx.restarter.bind(dp.stop_polling)
    lock = InstanceLock(settings.DATA_DIR)
    try:
        # Started by /restart: report ready, then wait until the previous process has exited.
        handover = await ctx.restarter.take_over()
        if not lock.acquire():
            log.error("Another claude-remote-ops instance is already running for %s", settings.DATA_DIR)
            print("❌ Бот уже запущен (другой процесс). Остановите его или используйте /restart.", file=sys.stderr)
            return EXIT_ALREADY_RUNNING
        if handover is not None and handover.update_id is not None:
            await confirm_updates(bot, handover.update_id)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        lock.release()
        await bot.session.close()
    return 0


def main() -> int:
    setup_console_utf8()
    try:
        settings = get_settings()
    except ValidationError as exc:
        print("❌ Ошибка конфигурации .env:\n", file=sys.stderr)
        for err in exc.errors():
            field = ".".join(str(p) for p in err.get("loc", ())) or "settings"
            print(f"  - {field}: {err.get('msg')}", file=sys.stderr)
        print("\nСкопируйте .env.example в .env и заполните BOT_TOKEN, ADMIN_USER_ID, ALLOWED_DIRS.", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    setup_logging(settings)
    try:
        return asyncio.run(run_bot(settings))
    except KeyboardInterrupt:
        log.info("Stopped by Ctrl+C")
    return 0


if __name__ == "__main__":
    sys.exit(main())
