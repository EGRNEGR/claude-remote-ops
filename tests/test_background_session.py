"""A chat that is busy in a background session on the PC: detection, 🌿 --fork-session, 🛑 claude stop."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, EditMessageText, SendMessage
from conftest import SESSION_ID
from fake_claude import FORK_SID

from src.handlers.runner import BUSY_TITLE
from src.keyboards.inline import ModeCb, RunCb, SessCb
from src.services.claude_runner import ClaudeRunner, RunRequest, RunStatus, detect_background_conflict
from src.services.state import BgSessionPolicy, RunMode
from src.utils.winpath import IS_WINDOWS

BG_ID = "59080800"
# Captured verbatim from `claude -p --resume <id>` (Claude Code 2.1.304) while the chat ran in `claude --bg`.
REAL_ERROR = (
    "Error: That session is running in the background (59080800). Run `claude attach 59080800` to open it, "
    "or `claude stop 59080800` first to resume it here. Add --fork-session to branch off a copy instead."
)
windows_only = pytest.mark.skipif(not IS_WINDOWS, reason="uses the Windows cmd shim fake CLI")


@pytest.fixture
def busy_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Marks SESSION_ID as open in background session BG_ID for the fake CLI."""
    path = tmp_path / "busy.txt"
    path.write_text(f"{SESSION_ID} {BG_ID}", encoding="utf-8")
    monkeypatch.setenv("FAKE_CLAUDE_BUSY_FILE", str(path))
    return path


def test_detect_background_conflict() -> None:
    conflict = detect_background_conflict(["some noise", REAL_ERROR])
    assert conflict is not None and conflict.bg_id == BG_ID and not conflict.handled
    assert conflict.message.startswith("That session is running in the background (59080800).")
    continued = detect_background_conflict([
        "Your most recent conversation is running in the background (session 48940cac). Use `claude agents` "
        "to find and attach to it, or `claude --resume` to pick another session."
    ])
    assert continued is not None and continued.bg_id == "48940cac"
    assert detect_background_conflict(["Nothing running in the background."]) is None
    # Only [A-Za-z0-9_-] ids are accepted: the id ends up in `claude stop <id>`.
    assert detect_background_conflict(["That session is running in the background (a&b|c)."]) is None


def test_build_args_fork_session(make_settings, tmp_path: Path) -> None:
    runner = ClaudeRunner(make_settings())
    resume = runner.build_args(RunRequest(prompt="x", cwd=tmp_path, mode=RunMode.RESUME, session_id=SESSION_ID,
                                          fork_session=True))
    i = resume.index("--resume")
    assert resume[i:i + 3] == ["--resume", SESSION_ID, "--fork-session"]
    cont = runner.build_args(RunRequest(prompt="x", cwd=tmp_path, mode=RunMode.CONTINUE, fork_session=True))
    assert cont[cont.index("--continue") + 1] == "--fork-session"
    new = runner.build_args(RunRequest(prompt="x", cwd=tmp_path, mode=RunMode.NEW, fork_session=True))
    assert "--fork-session" not in new
    plain = runner.build_args(RunRequest(prompt="x", cwd=tmp_path, mode=RunMode.RESUME, session_id=SESSION_ID))
    assert "--fork-session" not in plain


@windows_only
def test_runner_detects_conflict_and_stops_background(make_settings, fake_claude_cmd: Path, tmp_path: Path,
                                                      busy_file: Path) -> None:
    runner = ClaudeRunner(make_settings(CLAUDE_BINARY=str(fake_claude_cmd)))
    request = RunRequest(prompt="Проверь проект", cwd=tmp_path, mode=RunMode.RESUME, session_id=SESSION_ID)

    async def scenario() -> None:
        busy = await runner.run(request)
        assert busy.status == RunStatus.FAILED and busy.return_code == 1 and busy.result is None
        assert busy.background_conflict is not None and busy.background_conflict.bg_id == BG_ID
        assert busy.error == busy.background_conflict.message

        fork = await runner.run(RunRequest(prompt="Проверь проект", cwd=tmp_path, mode=RunMode.RESUME,
                                           session_id=SESSION_ID, fork_session=True))
        assert fork.status == RunStatus.FINISHED and fork.session_id == FORK_SID
        assert fork.result is not None and f"--resume {SESSION_ID} --fork-session" in fork.result.text
        assert busy_file.exists()  # the fork leaves the PC session alone

        assert await runner.stop_background(BG_ID) == (True, f"stopped {BG_ID}")
        assert not busy_file.exists()
        stopped_again, output = await runner.stop_background(BG_ID)
        assert not stopped_again and f"No job matching '{BG_ID}'" in output
        refused, reason = await runner.stop_background("59080800 & calc")
        assert not refused and "Некорректный id" in reason

        resumed = await runner.run(request)
        assert resumed.status == RunStatus.FINISHED and resumed.session_id == SESSION_ID
        assert resumed.background_conflict is None

    asyncio.run(scenario())


# ---------- the bot: buttons and the automatic policy ----------

def _sent(calls: list[Any]) -> list[SendMessage]:
    return [c for c in calls if isinstance(c, SendMessage)]


def _answer(calls: list[Any]) -> str:
    return next(c.text for c in calls if isinstance(c, AnswerCallbackQuery))


async def _busy_run(h: Any) -> tuple[str, list[Any]]:
    """Select the chat for --resume and send a prompt that hits the background session."""
    await h.ctx.store.load()
    await h.callback(SessCb(action="resume", sid=SESSION_ID).pack())
    h.session.take()
    await h.text("Проверь проект")
    await h.drain()
    calls = h.session.take()
    return next(iter(h.ctx.runner._finished)), calls


@windows_only
def test_bot_offers_fork_and_stop_buttons(harness_factory, busy_file: Path) -> None:
    h = harness_factory()

    async def scenario() -> None:
        run_id, calls = await _busy_run(h)
        assert any(isinstance(c, EditMessageText) and c.text.startswith(BUSY_TITLE) for c in calls)
        offer = _sent(calls)[-1]
        assert f"(<code>{BG_ID}</code>)" in offer.text and "--fork-session" in offer.text
        buttons = [(b.text, b.callback_data) for row in offer.reply_markup.inline_keyboard for b in row]
        assert buttons == [
            ("🌿 Ответвить копию (--fork-session)", RunCb(action="fork", run_id=run_id).pack()),
            (f"🛑 Остановить фоновую (claude stop {BG_ID}) и продолжить здесь",
             RunCb(action="bgstop", run_id=run_id).pack()),
        ]
        assert h.ctx.state.active_session_id == SESSION_ID  # nothing switched on a refused run

        # 🌿 fork: same prompt with --resume <id> --fork-session, the bot moves to the new branch.
        await h.callback(RunCb(action="fork", run_id=run_id).pack())
        await h.drain()
        calls = h.session.take()
        assert _answer(calls) == "🌿 Ответвляю копию чата..." and any(isinstance(c, EditMessageReplyMarkup) for c in calls)
        answer = next(c for c in _sent(calls) if c.parse_mode is None)
        assert "echo: Проверь проект" in answer.text and f"--resume {SESSION_ID} --fork-session" in answer.text
        assert any("🌿 Это новая ветка <code>99999999</code>" in c.text for c in _sent(calls))
        st = h.ctx.state
        assert st.active_session_id == FORK_SID and st.run_mode == RunMode.RESUME
        assert (st.active_session_title or "").startswith("🌿 ")
        assert busy_file.exists()

        # A second tap on the same button does not start a second copy.
        await h.callback(RunCb(action="fork", run_id=run_id).pack())
        calls = h.session.take()
        assert _answer(calls) == "Уже выполняется" and not _sent(calls)

    asyncio.run(scenario())


@windows_only
def test_bot_stops_background_and_continues_in_original_chat(harness_factory, busy_file: Path) -> None:
    h = harness_factory()

    async def scenario() -> None:
        run_id, _ = await _busy_run(h)
        await h.callback(RunCb(action="bgstop", run_id=run_id).pack())
        await h.drain()
        calls = h.session.take()
        assert _answer(calls) == f"🛑 claude stop {BG_ID}..."
        texts = [c.text for c in _sent(calls)]
        assert any(f"Фоновая сессия <code>{BG_ID}</code> остановлена" in t and f"stopped {BG_ID}" in t for t in texts)
        answer = next(c for c in _sent(calls) if c.parse_mode is None)
        assert f"--resume {SESSION_ID}" in answer.text and "--fork-session" not in answer.text
        assert not busy_file.exists() and h.ctx.state.active_session_id == SESSION_ID

    asyncio.run(scenario())


@windows_only
def test_failed_stop_keeps_the_fork_option(harness_factory, busy_file: Path) -> None:
    h = harness_factory()

    async def scenario() -> None:
        run_id, _ = await _busy_run(h)
        busy_file.write_text(f"{SESSION_ID} otherbg1", encoding="utf-8")  # the PC session id changed meanwhile
        await h.callback(RunCb(action="bgstop", run_id=run_id).pack())
        await h.drain()
        failure = _sent(h.session.take())[-1]
        assert "Не удалось остановить фоновую сессию" in failure.text and f"No job matching '{BG_ID}'" in failure.text
        buttons = [b.callback_data for row in failure.reply_markup.inline_keyboard for b in row]
        assert buttons == [RunCb(action="fork", run_id=run_id).pack()]
        assert not h.ctx.runner.active_runs  # nothing was rerun
        await h.callback(RunCb(action="fork", run_id=run_id).pack())  # still allowed after the failure
        await h.drain()
        assert h.ctx.state.active_session_id == FORK_SID

    asyncio.run(scenario())


@windows_only
def test_fork_policy_forks_automatically(harness_factory, busy_file: Path) -> None:
    h = harness_factory(BG_SESSION_POLICY="fork")

    async def scenario() -> None:
        assert h.ctx.state.bg_session_policy is BgSessionPolicy.FORK  # default from .env
        _, calls = await _busy_run(h)
        texts = [c.text for c in _sent(calls)]
        assert any("запускаю тот же запрос в копии чата" in t for t in texts)
        assert not any(c.reply_markup for c in _sent(calls) if "🛑 Остановить" in str(c.reply_markup))
        answer = next(c for c in _sent(calls) if c.parse_mode is None)
        assert "--fork-session" in answer.text and h.ctx.state.active_session_id == FORK_SID
        assert len(h.ctx.runner._finished) == 2  # the refused run + exactly one fork

    asyncio.run(scenario())


def test_mode_panel_switches_policy(harness_factory) -> None:
    h = harness_factory()

    async def scenario() -> None:
        await h.ctx.store.load()
        await h.text("/mode")
        panel = _sent(h.session.take())[0]
        assert "Если чат занят фоновой сессией на ПК" in panel.text
        buttons = {b.callback_data: b.text for row in panel.reply_markup.inline_keyboard for b in row}
        assert buttons[ModeCb(action="bg_ask").pack()].startswith("✅ ❓")
        assert not buttons[ModeCb(action="bg_fork").pack()].startswith("✅")
        await h.callback(ModeCb(action="bg_fork").pack())
        assert h.ctx.state.bg_session_policy is BgSessionPolicy.FORK
        assert "сразу копия" in _answer(h.session.take())

    asyncio.run(scenario())
