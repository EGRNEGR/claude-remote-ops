"""Self-restart: launch flags, detached spawn, the hand-over handshake, /restart handlers, single instance."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageReplyMarkup, EditMessageText, GetMe, GetUpdates, SendMessage, TelegramMethod
from aiogram.types import Chat, Message, Update, User
from tg_harness import MockSession

import main
from src.handlers.restart import RESTARTED_TEXT, RESTARTING_TEXT
from src.keyboards.inline import MenuCb
from src.services import restarter as rs
from src.services.claude_runner import ClaudeRunner
from src.services.restarter import Launch, RestartManager, RestartRequest, build_launch, pid_alive, spawn_detached
from src.utils.single_instance import InstanceLock
from src.utils.winpath import IS_WINDOWS

ROOT = Path(__file__).resolve().parent.parent
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="Windows process creation flags")


class FakeProc:
    def __init__(self, pid: int = 4242, code: int | None = None) -> None:
        self.pid, self._code = pid, code

    def poll(self) -> int | None:
        return self._code


def ready_spawner(state_file: Path, launches: list[Launch]):
    """Simulates a new process that starts instantly and reports ready (runs in a worker thread)."""
    def spawn(launch: Launch, cwd: Path, stderr_log: Path) -> FakeProc:
        launches.append(launch)
        data = json.loads(state_file.read_text(encoding="utf-8"))
        data["ready_pid"] = 4242
        state_file.write_text(json.dumps(data), encoding="utf-8")
        return FakeProc()

    return spawn


def _manager(tmp_path: Path, **kwargs: Any) -> RestartManager:
    params: dict[str, Any] = {
        "state_file": tmp_path / "data" / "restart_state.json",
        "base_dir": tmp_path,
        "stderr_log": tmp_path / "logs" / "restart_stderr.log",
        "ready_timeout": 20.0,
    }
    params.update(kwargs)
    return RestartManager(**params)


def _sleeper(seconds: float) -> subprocess.Popen[bytes]:
    return subprocess.Popen([sys.executable, "-c", f"import time; time.sleep({seconds})"])


# ---------- launch command and spawning ----------

@windows_only
def test_build_launch(tmp_path: Path) -> None:
    flags = subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP
    script = tmp_path / "start_bot.ps1"
    script.write_text("", encoding="utf-8")
    ps1 = build_launch(tmp_path, launcher="start_bot.ps1")
    assert ps1.via == "start_bot.ps1" and ps1.creationflags == flags
    assert ps1.argv[0].lower().endswith("powershell.exe") and ps1.argv[-2:] == ["-File", str(script)]

    direct = build_launch(tmp_path, launcher="")
    assert direct.via == "python main.py" and direct.creationflags == flags
    assert direct.argv == [sys.executable, str(tmp_path / "main.py")]

    script.unlink()
    assert build_launch(tmp_path, launcher="start_bot.ps1").via == "python main.py"  # script missing


@windows_only
def test_spawn_detached_hidden_console_and_clean_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe, out = tmp_path / "probe.py", tmp_path / "probe.txt"
    probe.write_text(
        "import ctypes, os, sys\n"
        "sys.stderr.write('probe stderr')\n"
        "hwnd = ctypes.windll.kernel32.GetConsoleWindow()\n"
        "open(sys.argv[1], 'w').write(f\"{hwnd} {os.environ.get('CLAUDECODE')}\")\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CLAUDECODE", "1")
    launch = Launch([sys.executable, str(probe), str(out)], build_launch(tmp_path, launcher="").creationflags, "probe")
    proc = spawn_detached(launch, tmp_path, tmp_path / "logs" / "stderr.log")
    assert proc.wait(60) == 0
    # No console window even through the venv python.exe stub; the parent Claude session env is dropped.
    assert out.read_text(encoding="utf-8") == "0 None"
    assert "probe stderr" in (tmp_path / "logs" / "stderr.log").read_text(encoding="utf-8")


def test_pid_alive() -> None:
    assert pid_alive(os.getpid())
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait(30)
    assert not pid_alive(proc.pid) and not pid_alive(0)


# ---------- old process: hand_over ----------

def _fake_main(tmp_path: Path, body: str) -> None:
    (tmp_path / "main.py").write_text(f"import sys\nsys.path.insert(0, {str(ROOT)!r})\n{body}", encoding="utf-8")


def test_hand_over_with_a_real_new_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(rs.LAUNCHER_ENV, raising=False)
    # The new side, minus the wait for the old PID (that would be the pytest process itself).
    _fake_main(tmp_path, (
        "import asyncio, dataclasses, os\n"
        "from pathlib import Path\n"
        "from src.services.restarter import RestartManager\n"
        "m = RestartManager(Path('data/restart_state.json').resolve(), Path('.'), Path('err.log'), 5)\n"
        "req = asyncio.run(m.read())\n"
        "asyncio.run(m.write(dataclasses.replace(req, ready_pid=os.getpid())))\n"
    ))
    manager = _manager(tmp_path)
    stops: list[bool] = []

    async def stop() -> None:
        stops.append(True)

    manager.bind(stop)
    result = asyncio.run(manager.hand_over(chat_id=42, message_id=7, update_id=99))
    assert result.ok and "python main.py" in result.detail, result.detail
    assert stops == [True] and manager.handed_over and manager.in_progress
    request = asyncio.run(manager.read())
    assert request is not None and request.ready_pid and request.ready_pid != os.getpid()
    assert (request.chat_id, request.message_id, request.update_id, request.old_pid) == (42, 7, 99, os.getpid())


def test_hand_over_aborts_when_new_process_crashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(rs.LAUNCHER_ENV, raising=False)
    _fake_main(tmp_path, "sys.stderr.write('SyntaxError: boom in handlers')\nsys.exit(1)\n")
    manager = _manager(tmp_path)
    stops: list[bool] = []

    async def stop() -> None:
        stops.append(True)

    manager.bind(stop)
    result = asyncio.run(manager.hand_over(chat_id=42, message_id=7, update_id=99))
    assert not result.ok and "кодом 1" in result.detail and "boom in handlers" in result.detail
    assert stops == [] and not manager.handed_over and not manager.in_progress
    assert not (tmp_path / "data" / "restart_state.json").exists()


def test_hand_over_kills_a_new_process_that_never_gets_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(rs.LAUNCHER_ENV, raising=False)
    _fake_main(tmp_path, "import time\ntime.sleep(120)\n")
    spawned: list[Any] = []

    def spawner(launch: Launch, cwd: Path, stderr_log: Path) -> Any:
        proc = spawn_detached(launch, cwd, stderr_log)
        spawned.append(proc)
        return proc

    manager = _manager(tmp_path, ready_timeout=1.5, spawner=spawner)
    result = asyncio.run(manager.hand_over(chat_id=42, message_id=7, update_id=None))
    assert not result.ok and "не ответил за 1.5 с" in result.detail
    assert spawned[0].wait(30) is not None and not manager.in_progress


def test_hand_over_reports_spawn_errors(tmp_path: Path) -> None:
    def broken(launch: Launch, cwd: Path, stderr_log: Path) -> Any:
        raise FileNotFoundError("powershell.exe")

    manager = _manager(tmp_path, spawner=broken)
    result = asyncio.run(manager.hand_over(chat_id=42, message_id=None, update_id=None))
    assert not result.ok and "не удалось запустить" in result.detail and not manager.in_progress


# ---------- new process: take_over ----------

def _write_request(manager: RestartManager, **overrides: Any) -> RestartRequest:
    values: dict[str, Any] = {"chat_id": 42, "message_id": 7, "update_id": 99, "old_pid": 0,
                              "requested_at": time.time()}
    values.update(overrides)
    request = RestartRequest(**values)
    asyncio.run(manager.write(request))
    return request


def test_take_over_waits_for_the_old_process(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    assert asyncio.run(manager.take_over()) is None  # normal start: no request file
    old = _sleeper(1.0)
    _write_request(manager, old_pid=old.pid)
    request = asyncio.run(manager.take_over())
    assert request is not None and request.ready_pid == os.getpid() and manager.pending == request
    assert old.poll() is not None  # returned only after the old process had exited
    on_disk = asyncio.run(manager.read())
    assert on_disk is not None and on_disk.ready_pid == os.getpid()
    assert manager.is_replayed(99) and manager.is_replayed(98) and not manager.is_replayed(100)


def test_take_over_finishes_off_a_stuck_old_process(tmp_path: Path) -> None:
    manager = _manager(tmp_path, handover_timeout=0.5)
    old = _sleeper(120)
    _write_request(manager, old_pid=old.pid)
    assert asyncio.run(manager.take_over()) is not None
    assert old.wait(30) is not None


def test_take_over_ignores_stale_or_broken_requests(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    _write_request(manager, requested_at=time.time() - 3600)
    assert asyncio.run(manager.take_over()) is None and manager.pending is None
    assert not (tmp_path / "data" / "restart_state.json").exists()
    (tmp_path / "data" / "restart_state.json").write_text("{not json", encoding="utf-8")
    assert asyncio.run(manager.take_over()) is None


# ---------- Telegram handlers ----------

def _calls(h: Any, kind: type[TelegramMethod[Any]]) -> list[Any]:
    return [c for c in h.session.calls if isinstance(c, kind)]


def test_restart_command_hands_over(harness_factory, tmp_path: Path) -> None:
    h = harness_factory()
    state_file = h.ctx.settings.restart_state_file
    launches: list[Launch] = []
    stops: list[bool] = []
    h.ctx.restarter = _manager(tmp_path, state_file=state_file, spawner=ready_spawner(state_file, launches))

    async def stop() -> None:
        stops.append(True)

    h.ctx.restarter.bind(stop)

    async def scenario() -> None:
        await h.ctx.store.load()
        await h.text("/restart")
        await h.drain()
        await h.text("/restart")

    asyncio.run(scenario())
    texts = [c.text for c in _calls(h, SendMessage)]
    assert texts[0] == RESTARTING_TEXT and texts[1] == "⏳ Перезапуск уже выполняется..."
    assert stops == [True] and len(launches) == 1 and h.ctx.restarter.handed_over
    request = RestartRequest.from_dict(json.loads(state_file.read_text(encoding="utf-8")))
    assert (request.chat_id, request.message_id, request.update_id) == (42, 1000, 1)
    assert (request.old_pid, request.ready_pid) == (os.getpid(), 4242)


def test_restart_asks_before_killing_active_runs(harness_factory, tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    h = harness_factory()
    state_file = h.ctx.settings.restart_state_file
    launches: list[Launch] = []
    h.ctx.restarter = _manager(tmp_path, state_file=state_file, spawner=ready_spawner(state_file, launches))
    monkeypatch.setattr(ClaudeRunner, "active_runs", property(lambda self: [SimpleNamespace(run_id="abcd1234")]))

    async def scenario() -> None:
        await h.ctx.store.load()
        await h.text("/restart")
        assert launches == []
        await h.callback(MenuCb(action="restart_force").pack())
        await h.drain()

    asyncio.run(scenario())
    warning, notice = _calls(h, SendMessage)
    assert "Сейчас работает Claude: процессов 1" in warning.text
    buttons = [b.callback_data for row in warning.reply_markup.inline_keyboard for b in row]
    assert buttons == [MenuCb(action="restart_force").pack(), MenuCb(action="cancel").pack()]
    assert notice.text == RESTARTING_TEXT and len(launches) == 1
    assert _calls(h, EditMessageReplyMarkup)  # the confirmation buttons are removed


def test_restart_failure_is_reported_and_bot_keeps_running(harness_factory, tmp_path: Path) -> None:
    h = harness_factory()

    def crashed(launch: Launch, cwd: Path, stderr_log: Path) -> FakeProc:
        return FakeProc(code=1)

    h.ctx.restarter = _manager(tmp_path, state_file=h.ctx.settings.restart_state_file, spawner=crashed)

    async def scenario() -> None:
        await h.ctx.store.load()
        await h.callback(MenuCb(action="restart").pack())
        await h.drain()

    asyncio.run(scenario())
    edits = _calls(h, EditMessageText)
    assert edits and "Перезапуск отменен" in edits[-1].text and "кодом 1" in edits[-1].text
    assert not h.ctx.restarter.handed_over and not h.ctx.restarter.in_progress


def test_replayed_restart_update_is_ignored(harness_factory, tmp_path: Path) -> None:
    h = harness_factory()
    state_file = h.ctx.settings.restart_state_file
    launches: list[Launch] = []
    h.ctx.restarter = _manager(tmp_path, state_file=state_file, spawner=ready_spawner(state_file, launches))
    _write_request(h.ctx.restarter, update_id=1, old_pid=0)

    async def scenario() -> None:
        assert await h.ctx.restarter.take_over() is not None
        await h.ctx.restarter.clear()
        await h.ctx.store.load()
        await h.text("/restart")  # update_id 1: the very /restart that started this process
        assert h.session.take() == [] and launches == []
        await h.text("/restart")  # update_id 2: a new request
        await h.drain()

    asyncio.run(scenario())
    assert [c.text for c in _calls(h, SendMessage)] == [RESTARTING_TEXT] and len(launches) == 1


def test_status_and_mode_menus_offer_restart(harness_factory) -> None:
    h = harness_factory()

    async def scenario() -> None:
        await h.ctx.store.load()
        await h.text("/status")
        await h.text("/mode")

    asyncio.run(scenario())
    restart_cb = MenuCb(action="restart").pack()
    for call in _calls(h, SendMessage):
        buttons = [(b.text, b.callback_data) for row in call.reply_markup.inline_keyboard for b in row]
        assert ("🔄 Перезапустить бота", restart_cb) in buttons


# ---------- startup / shutdown in main.py ----------

class EditFailsSession:
    """Wraps the harness MockSession: editing the old notice fails (deleted / too old)."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def __call__(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        if isinstance(method, EditMessageText):
            self._inner.calls.append(method)
            raise TelegramBadRequest(method=method, message="Bad Request: message to edit not found")
        return await self._inner(bot, method, timeout)


@pytest.mark.parametrize("edit_fails", [False, True])
def test_new_process_announces_restart(harness_factory, tmp_path: Path, edit_fails: bool) -> None:
    h = harness_factory(WATCH_ON_START=False)
    manager = _manager(tmp_path, state_file=h.ctx.settings.restart_state_file)
    h.ctx.restarter = manager
    _write_request(manager, message_id=77, update_id=5, old_pid=0)
    if edit_fails:
        h.bot.session = EditFailsSession(h.session)  # type: ignore[assignment]

    async def scenario() -> None:
        assert await manager.take_over() is not None
        await main.confirm_updates(h.bot, 5)
        await main.on_startup(h.bot, h.ctx)

    asyncio.run(scenario())
    get_updates = _calls(h, GetUpdates)[0]
    assert (get_updates.offset, get_updates.limit, get_updates.timeout) == (6, 1, 0)
    edit = _calls(h, EditMessageText)[0]
    assert (edit.chat_id, edit.message_id) == (42, 77)
    assert edit.text.startswith(RESTARTED_TEXT) and "📊 <b>Состояние</b>" in edit.text
    sent = [c.text for c in _calls(h, SendMessage)]
    assert not any("claude-remote-ops запущен" in t for t in sent)  # no second "started" message
    assert sent == ([edit.text] if edit_fails else [])
    assert not h.ctx.settings.restart_state_file.exists()


def test_old_process_shuts_down_silently_after_hand_over(harness_factory) -> None:
    h = harness_factory()
    h.ctx.restarter.handed_over = True
    asyncio.run(main.on_shutdown(h.bot, h.ctx))
    assert _calls(h, SendMessage) == []
    h.ctx.restarter.handed_over = False
    asyncio.run(main.on_shutdown(h.bot, h.ctx))
    assert [c.text for c in _calls(h, SendMessage)] == ["🔴 claude-remote-ops остановлен"]


# ---------- main.run_bot end to end: real Dispatcher polling loop, scripted Bot API ----------

class PollingSession(MockSession):
    def __init__(self, batches: list[list[Update]]) -> None:
        super().__init__()
        self.batches = batches

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        if isinstance(method, GetMe):
            self.calls.append(method)
            return User(id=123456, is_bot=True, first_name="Bot", username="test_bot")
        if isinstance(method, GetUpdates):
            self.calls.append(method)
            if method.timeout == 0:  # confirm_updates()
                return []
            if self.batches:
                return self.batches.pop(0)
            await asyncio.sleep(0.05)  # an empty long poll
            return []
        return await super().make_request(bot, method, timeout)


def _restart_update(update_id: int) -> Update:
    message = Message(message_id=update_id, date=datetime.now(), chat=Chat(id=42, type="private"),
                      from_user=User(id=42, is_bot=False, first_name="Admin"), text="/restart")
    return Update(update_id=update_id, message=message)


def _run_bot(settings: Any, session: PollingSession, monkeypatch: pytest.MonkeyPatch, launches: list[Launch]) -> int:
    monkeypatch.setattr(main, "Bot", lambda token, default: Bot(token, session=session, default=default))
    monkeypatch.setattr(rs, "spawn_detached", ready_spawner(settings.restart_state_file, launches))
    # Routers are module singletons: detach them from the Dispatcher of a previous test.
    for module in (main.common, main.restart, main.model, main.usage, main.sessions, main.files, main.voice,
                   main.runner):
        module.router._parent_router = None
    return asyncio.run(asyncio.wait_for(main.run_bot(settings), timeout=60))


def test_run_bot_old_side_hands_over_and_exits(make_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(WATCH_ON_START=False)
    session = PollingSession([[_restart_update(10)]])
    launches: list[Launch] = []
    assert _run_bot(settings, session, monkeypatch, launches) == 0  # polling stopped by the hand-over
    texts = [c.text for c in session.calls if isinstance(c, SendMessage)]
    assert texts[0].startswith("🟢") and texts[1:] == [RESTARTING_TEXT]  # no "🔴 остановлен"
    request = RestartRequest.from_dict(json.loads(settings.restart_state_file.read_text(encoding="utf-8")))
    assert (request.update_id, request.message_id, request.ready_pid) == (10, 1001, 4242) and len(launches) == 1
    assert InstanceLock(settings.DATA_DIR).acquire()  # released on exit


def test_run_bot_new_side_takes_over(make_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(WATCH_ON_START=False)
    manager = _manager(settings.DATA_DIR.parent, state_file=settings.restart_state_file)
    _write_request(manager, message_id=555, update_id=10, old_pid=0)
    # Telegram redelivers /restart #10 (as if the confirmation had failed), then a new /restart #11 arrives.
    session = PollingSession([[_restart_update(10)], [_restart_update(11)]])
    launches: list[Launch] = []
    assert _run_bot(settings, session, monkeypatch, launches) == 0
    updates = [c for c in session.calls if isinstance(c, GetUpdates)]
    assert (updates[0].offset, updates[0].timeout) == (11, 0)  # the /restart is confirmed before polling
    edit = next(c for c in session.calls if isinstance(c, EditMessageText))
    assert edit.message_id == 555 and edit.text.startswith(RESTARTED_TEXT)
    texts = [c.text for c in session.calls if isinstance(c, SendMessage)]
    assert texts == [RESTARTING_TEXT] and len(launches) == 1  # #10 ignored, #11 handled, no "🟢 запущен"


def test_run_bot_refuses_a_second_instance(make_settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = make_settings(WATCH_ON_START=False)
    holder = InstanceLock(settings.DATA_DIR)
    assert holder.acquire()
    session = PollingSession([])
    try:
        assert _run_bot(settings, session, monkeypatch, []) == main.EXIT_ALREADY_RUNNING
    finally:
        holder.release()
    assert not any(isinstance(c, GetUpdates) for c in session.calls)


# ---------- single instance ----------

def test_instance_lock(tmp_path: Path) -> None:
    first, second = InstanceLock(tmp_path / "a"), InstanceLock(tmp_path / "a")
    other = InstanceLock(tmp_path / "b")
    try:
        assert first.acquire() and not second.acquire() and other.acquire()
        first.release()
        assert second.acquire()
    finally:
        for lock in (first, second, other):
            lock.release()


def test_request_roundtrip() -> None:
    request = RestartRequest(42, None, 5, 123, 1.5, None)
    assert RestartRequest.from_dict(json.loads(json.dumps(asdict(request)))) == request
    with pytest.raises(KeyError):
        RestartRequest.from_dict({"chat_id": 1})
