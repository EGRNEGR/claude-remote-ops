"""Model / reasoning effort switch: CLI args, child env, persisted state and the /model, /effort UI."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage

from src.keyboards.inline import BTN_MODEL, MenuCb, ModelCb, model_kb
from src.services.claude_runner import ClaudeRunner, RunRequest
from src.services.reasoning import (
    MODEL_DEFAULT,
    ReasoningEffort,
    describe_choice,
    describe_cli,
    effort_args,
    effort_env,
    normalize_model,
    parse_effort,
    prompt_for_effort,
)
from src.services.state import STATE_SCHEMA_VERSION, BotState, RunMode, StateStore
from src.utils.winpath import IS_WINDOWS

E = ReasoningEffort
THINKING_VARS = ("CLAUDE_CODE_EFFORT_LEVEL", "MAX_THINKING_TOKENS", "CLAUDE_CODE_DISABLE_THINKING")


def _flag(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


# (effort, --effort value, MAX_THINKING_TOKENS, CLAUDE_CODE_DISABLE_THINKING)
EFFORT_MATRIX = [
    (E.DEFAULT, None, None, None),
    (E.OFF, "low", "0", "1"),
    (E.LOW, "low", "4096", None),
    (E.MEDIUM, "medium", "10000", None),
    (E.HIGH, "high", "20000", None),
    (E.XHIGH, "xhigh", "24000", None),
    (E.MAX, "max", "32000", None),
]


@pytest.mark.parametrize("model", [None, "sonnet", "opus", "haiku", "claude-opus-5-5[1m]"])
@pytest.mark.parametrize(("effort", "cli_effort", "budget", "no_thinking"), EFFORT_MATRIX)
def test_args_and_env_matrix(make_settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, model: str | None,
                             effort: ReasoningEffort, cli_effort: str | None, budget: str | None,
                             no_thinking: str | None) -> None:
    # Values inherited from the PC must not leak into an explicit choice (and stay for "default").
    monkeypatch.setenv("CLAUDE_CODE_EFFORT_LEVEL", "xhigh")
    monkeypatch.setenv("MAX_THINKING_TOKENS", "777")
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_THINKING", "1")
    monkeypatch.setenv("CLAUDE_EFFORT", "high")  # set by a parent Claude session, always stripped
    runner = ClaudeRunner(make_settings())
    req = RunRequest(prompt="x", cwd=tmp_path, mode=RunMode.NEW, skip_permissions=True, model=model, reasoning=effort)
    args = runner.build_args(req)
    assert _flag(args, "--model") == model
    assert _flag(args, "--effort") == cli_effort
    assert args.count("--effort") == (0 if cli_effort is None else 1)
    assert args[-1] == "--dangerously-skip-permissions"  # variadic-safe order is kept

    env = runner.build_env(req.reasoning)
    assert "CLAUDE_EFFORT" not in env
    if effort == E.DEFAULT:
        assert (env["CLAUDE_CODE_EFFORT_LEVEL"], env["MAX_THINKING_TOKENS"]) == ("xhigh", "777")
        assert env["CLAUDE_CODE_DISABLE_THINKING"] == "1"
    else:
        assert env["CLAUDE_CODE_EFFORT_LEVEL"] == cli_effort  # env and flag always agree
        assert env["MAX_THINKING_TOKENS"] == budget
        assert env.get("CLAUDE_CODE_DISABLE_THINKING") == no_thinking
    assert env["PYTHONIOENCODING"] == "utf-8"


def test_restricted_mode_keeps_allowed_tools_last(make_settings, tmp_path: Path) -> None:
    runner = ClaudeRunner(make_settings())
    args = runner.build_args(
        RunRequest(prompt="x", cwd=tmp_path, mode=RunMode.NEW, model="haiku", reasoning=E.LOW), "Read,Bash",
    )
    assert args[-2:] == ["--allowedTools", "Read,Bash"]
    assert _flag(args, "--model") == "haiku" and _flag(args, "--effort") == "low"


def test_ultrathink_only_for_max(tmp_path: Path) -> None:
    assert prompt_for_effort("Почини тесты  \n", E.MAX) == "Почини тесты\n\nultrathink"
    assert prompt_for_effort("UltraThink: почини", E.MAX) == "UltraThink: почини"  # never doubled
    for effort in (E.DEFAULT, E.OFF, E.LOW, E.HIGH, E.XHIGH):
        assert prompt_for_effort("x", effort) == "x"
    req = RunRequest(prompt="задача", cwd=tmp_path, mode=RunMode.NEW, reasoning=E.MAX)
    assert req.stdin_prompt.endswith("\n\nultrathink") and req.prompt == "задача"


def test_parsing_and_descriptions() -> None:
    for raw in ("", "  ", "default", "Auto", "авто", None):
        assert normalize_model(raw) == MODEL_DEFAULT
    assert normalize_model(" Sonnet ") == "sonnet" and normalize_model("OPUS") == "opus"
    assert normalize_model("claude-sonnet-5-5") == "claude-sonnet-5-5"
    for bad in ("--dangerously-skip-permissions", "opus & calc", "a b", "x" * 200):
        with pytest.raises(ValueError):
            normalize_model(bad)
    assert parse_effort("") is E.DEFAULT and parse_effort("auto") is E.DEFAULT
    assert parse_effort("Выкл") is E.OFF and parse_effort("0") is E.OFF and parse_effort("MAX") is E.MAX
    assert parse_effort("xhigh") is E.XHIGH and parse_effort(E.LOW) is E.LOW
    with pytest.raises(ValueError, match="допустимо"):
        parse_effort("turbo")
    assert describe_choice("sonnet", E.LOW) == "🧠 Модель: sonnet · 💭 Ризонинг: low"
    assert describe_choice(None, E.DEFAULT, "claude-opus-5-5") == "🧠 Модель: авто (claude-opus-5-5) · 💭 Ризонинг: авто"
    assert describe_cli("opus", E.MAX) == "--model opus --effort max · MAX_THINKING_TOKENS=32000 · + ultrathink"
    assert describe_cli("default", E.OFF) == (
        "--effort low · MAX_THINKING_TOKENS=0 · CLAUDE_CODE_DISABLE_THINKING=1"
    )
    assert describe_cli("default", E.DEFAULT).startswith("без --model")
    assert effort_args(E.DEFAULT) == [] and effort_env(E.DEFAULT) == {}


def test_settings_from_env(make_settings) -> None:
    s = make_settings(CLAUDE_MODEL="", CLAUDE_REASONING_EFFORT="")
    assert s.CLAUDE_MODEL == MODEL_DEFAULT and s.CLAUDE_REASONING_EFFORT is E.DEFAULT
    s = make_settings(CLAUDE_MODEL="Haiku", CLAUDE_REASONING_EFFORT="Medium")
    assert s.CLAUDE_MODEL == "haiku" and s.CLAUDE_REASONING_EFFORT is E.MEDIUM
    with pytest.raises(ValueError):
        make_settings(CLAUDE_REASONING_EFFORT="ultra-mega")


def test_state_v3_migrates_and_choice_persists(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text(json.dumps({"schema_version": 3, "skip_permissions": False, "run_mode": "continue"}),
                    encoding="utf-8")
    defaults = BotState(claude_model="sonnet", reasoning_effort=E.LOW)

    async def scenario() -> None:
        store = StateStore(path, defaults)
        st = await store.load()
        # v3 -> v4: earlier choices kept, model/effort from CLAUDE_MODEL / CLAUDE_REASONING_EFFORT.
        assert st.schema_version == STATE_SCHEMA_VERSION == 4 and not st.skip_permissions
        assert (st.claude_model, st.reasoning_effort) == ("sonnet", E.LOW)
        await store.update(claude_model="opus", reasoning_effort=E.MAX)
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["claude_model"] == "opus" and saved["reasoning_effort"] == "max"
        # After a restart the saved choice wins over the .env defaults.
        again = await StateStore(path, defaults).load()
        assert (again.claude_model, again.reasoning_effort) == ("opus", E.MAX)
        # A corrupted value falls back to the CLI defaults instead of breaking the launch.
        path.write_text(json.dumps({"schema_version": 4, "claude_model": "--evil", "reasoning_effort": "?"}),
                        encoding="utf-8")
        broken = await StateStore(path, defaults).load()
        assert (broken.claude_model, broken.reasoning_effort) == (MODEL_DEFAULT, E.DEFAULT)

    asyncio.run(scenario())


def _buttons(markup) -> list[list[str]]:
    return [[button.text for button in row] for row in markup.inline_keyboard]


def test_model_keyboard_layout() -> None:
    rows = _buttons(model_kb(BotState(claude_model="sonnet", reasoning_effort=E.LOW)))
    assert rows[:4] == [
        ["✅ ⚡ Sonnet", "🧠 Opus"],
        ["🪶 Haiku", "⚙️ Авто"],
        ["🚫 Выкл", "✅ 🟢 Low", "🟡 Medium"],
        ["🟠 High", "🔥 Max", "⚙️ Авто"],
    ]
    rows = _buttons(model_kb(BotState()))
    assert rows[1][1] == "✅ ⚙️ Авто" and rows[3][2] == "✅ ⚙️ Авто"
    for row in model_kb(BotState()).inline_keyboard:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


def _answers(calls) -> list[str]:
    return [c.text for c in calls if isinstance(c, AnswerCallbackQuery) and c.text]


def test_model_menu_flow(harness_factory) -> None:
    h = harness_factory(CLAUDE_MODEL="opus", CLAUDE_REASONING_EFFORT="high")

    async def scenario() -> None:
        await h.ctx.store.load()
        assert (h.ctx.state.claude_model, h.ctx.state.reasoning_effort) == ("opus", E.HIGH)

        # /model without arguments: one message with both blocks, current choice marked.
        await h.text("/model")
        calls = h.session.take()
        menu = next(c for c in calls if isinstance(c, SendMessage))
        assert "🧠 Модель: opus · 💭 Ризонинг: high" in menu.text
        assert "--model opus --effort high" in menu.text
        assert _buttons(menu.reply_markup)[0] == ["⚡ Sonnet", "✅ 🧠 Opus"]

        # Tap Sonnet: state saved, the same message is edited in place, no new messages.
        await h.callback(ModelCb(kind="m", value="sonnet").pack())
        calls = h.session.take()
        assert not any(isinstance(c, SendMessage) for c in calls)
        edit = next(c for c in calls if isinstance(c, EditMessageText))
        assert "🧠 Модель: sonnet · 💭 Ризонинг: high" in edit.text
        assert _buttons(edit.reply_markup)[0] == ["✅ ⚡ Sonnet", "🧠 Opus"]
        assert h.ctx.state.claude_model == "sonnet"
        assert any("sonnet" in a for a in _answers(calls))

        # Tap Low, then Auto for the model.
        await h.callback(ModelCb(kind="e", value="low").pack())
        await h.callback(ModelCb(kind="m", value="default").pack())
        calls = h.session.take()
        assert h.ctx.state.reasoning_effort is E.LOW and h.ctx.state.claude_model == MODEL_DEFAULT
        last_edit = [c for c in calls if isinstance(c, EditMessageText)][-1]
        assert "🧠 Модель: авто · 💭 Ризонинг: low" in last_edit.text

        # Same button again: no edit (Telegram would answer "message is not modified").
        await h.callback(ModelCb(kind="e", value="low").pack())
        calls = h.session.take()
        assert not any(isinstance(c, EditMessageText) for c in calls)
        assert any(a.startswith("Уже выбрано") for a in _answers(calls))

        # Forged callback values are rejected.
        await h.callback(ModelCb(kind="e", value="turbo").pack())
        await h.callback(ModelCb(kind="m", value="gpt").pack())
        assert h.ctx.state.reasoning_effort is E.LOW and h.ctx.state.claude_model == MODEL_DEFAULT
        h.session.take()

        # Slash commands.
        await h.text("/model haiku")
        await h.text("/effort max")
        texts = h.session.texts()
        assert "✅ 🧠 Модель: haiku · 💭 Ризонинг: low" in texts[0]
        assert "✅ 🧠 Модель: haiku · 💭 Ризонинг: max" in texts[1]
        await h.text("/model claude-sonnet-5-5")
        assert h.ctx.state.claude_model == "claude-sonnet-5-5"
        await h.text("/model --dangerously-skip-permissions")
        await h.text("/effort turbo")
        texts = h.session.texts()
        assert "Недопустимое имя модели" in texts[-2] and "Неизвестный уровень" in texts[-1]
        assert h.ctx.state.claude_model == "claude-sonnet-5-5" and h.ctx.state.reasoning_effort is E.MAX
        h.session.take()

        # /status and /mode show the pair and a button that opens the menu.
        for command in ("/status", "/mode"):
            await h.text(command)
            msg = next(c for c in h.session.take() if isinstance(c, SendMessage))
            assert "🧠 Модель: claude-sonnet-5-5 · 💭 Ризонинг: max" in msg.text
            assert BTN_MODEL in [b for row in _buttons(msg.reply_markup) for b in row]
        await h.callback(MenuCb(action="model").pack())
        assert any("Модель и Ризонинг" in t for t in h.session.texts())

    asyncio.run(scenario())


@pytest.mark.skipif(not IS_WINDOWS, reason="uses the Windows cmd shim fake CLI")
def test_run_uses_selected_model_and_effort(harness_factory, tmp_path: Path) -> None:
    h = harness_factory()
    allowed = str((tmp_path / "allowed").resolve())

    async def run(prompt: str) -> tuple[str, list[str]]:
        await h.text(prompt)
        await h.drain()
        calls = h.session.take()
        answer = next(c.text for c in calls if isinstance(c, SendMessage) and c.parse_mode is None)
        edits = [c.text for c in calls if isinstance(c, EditMessageText)]
        return answer, edits

    async def scenario() -> None:
        await h.ctx.store.load()
        await h.ctx.store.update(active_cwd=allowed, run_mode=RunMode.NEW)

        await h.callback(ModelCb(kind="m", value="opus").pack())
        await h.callback(ModelCb(kind="e", value="max").pack())
        h.session.take()
        answer, edits = await run("Сделай ревью")
        # argv: native flags; env: the same level + thinking budget; stdin: ultrathink appended.
        assert "--model opus" in answer and "--effort max" in answer
        assert "CLAUDE_CODE_EFFORT_LEVEL=max MAX_THINKING_TOKENS=32000 CLAUDE_CODE_DISABLE_THINKING=-" in answer
        assert "ultrathink" in answer
        # The run header shows the pair (and the model the CLI reported in init).
        assert any("🧠 Модель: opus" in e and "💭 Ризонинг: max" in e for e in edits), edits

        await h.text("/effort off")
        await h.text("/model auto")
        h.session.take()
        answer, edits = await run("Быстрый вопрос")
        assert "--model" not in answer and "--effort low" in answer and "ultrathink" not in answer
        assert "CLAUDE_CODE_EFFORT_LEVEL=low MAX_THINKING_TOKENS=0 CLAUDE_CODE_DISABLE_THINKING=1" in answer
        assert any("💭 Ризонинг: off" in e for e in edits)

        await h.text("/effort auto")
        h.session.take()
        answer, _ = await run("Обычный запуск")
        assert "--effort" not in answer and "--model" not in answer

    asyncio.run(scenario())


def test_retries_keep_model_and_effort(tmp_path: Path) -> None:
    from src.handlers.runner import build_fork_retry, build_resume_after_stop, build_unrestricted_retry
    from src.services.claude_runner import RunResult, RunState

    sid = "6a17f423-802d-4103-b021-8562aecdb7d7"
    req = RunRequest(prompt="p", cwd=tmp_path, mode=RunMode.RESUME, session_id=sid, max_turns=15,
                     max_budget_usd=2.0, model="haiku", reasoning=E.LOW)
    state = RunState(run_id="r", request=req)
    state.result = RunResult(subtype="error_max_turns", is_error=True, text="", cost_usd=1.0, session_id=sid,
                             num_turns=15, duration_ms=1, permission_denials=[], terminal_reason="max_turns")
    for retry in (build_unrestricted_retry(state), build_fork_retry(state), build_resume_after_stop(state)):
        assert (retry.model, retry.reasoning) == ("haiku", E.LOW)
    unrestricted = build_unrestricted_retry(state)
    assert unrestricted.skip_permissions and unrestricted.max_turns is None and unrestricted.mode is RunMode.RESUME
