"""End-to-end handler tests: real Dispatcher, middleware, routers, FSM; Telegram API is mocked."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiogram.methods import EditMessageText, SendDocument, SendMessage
from conftest import SESSION_ID
from tg_harness import STRANGER

from src.keyboards.inline import FileCb, ProjCb, RunCb, SessCb, UpCb
from src.services.session_parser import encode_project_path
from src.utils.winpath import IS_WINDOWS


@pytest.mark.skipif(not IS_WINDOWS, reason="uses the Windows cmd shim fake CLI")
def test_full_bot_flow(harness_factory, tmp_path: Path) -> None:
    h = harness_factory()
    allowed = (tmp_path / "allowed").resolve()

    async def scenario() -> None:
        await h.ctx.store.load()

        # 1. Strangers are silently ignored, nothing is sent.
        await h.text("/start", uid=STRANGER)
        await h.callback(FileCb(action="roots").pack(), uid=STRANGER)
        assert h.session.take() == []

        # 2. /start for the admin.
        await h.text("/start")
        assert any("claude-remote-ops" in t for t in h.session.texts())
        h.session.take()

        # 3. Free text without a working folder -> hint.
        await h.text("сделай что-нибудь")
        assert any("Не выбрана рабочая папка" in t for t in h.session.texts())
        h.session.take()

        # 4. Projects -> sessions -> report.
        await h.text("/sessions")
        assert any("Проекты Claude Code" in t and "allowed" in t for t in h.session.texts())
        h.session.take()
        project_key = encode_project_path(str(allowed))
        token = h.ctx.tokens.put(project_key)
        await h.callback(ProjCb(action="open", token=token).pack())
        assert any("fix-tests" in t for t in h.session.texts(EditMessageText))
        h.session.take()
        await h.callback(SessCb(action="open", sid=SESSION_ID).pack())
        report = h.session.texts(EditMessageText)
        assert report and "Почини тесты" in report[0] and "$1.25" in report[0]
        h.session.take()

        # 5. Resume that session -> state switches to --resume.
        await h.callback(SessCb(action="resume", sid=SESSION_ID).pack())
        assert h.ctx.state.active_session_id == SESSION_ID and h.ctx.state.run_mode.value == "resume"
        h.session.take()

        # 6. Prompt -> fake claude run with live status, final edit and answer.
        await h.text("Проверь проект")
        await h.drain()
        calls = h.session.take()
        sent = [c.text for c in calls if isinstance(c, SendMessage)]
        edits = [c.text for c in calls if isinstance(c, EditMessageText)]
        assert sent[0].startswith("⏳")
        assert any("Готово" in e for e in edits), edits
        answer = next(c for c in calls if isinstance(c, SendMessage) and c.parse_mode is None)
        assert "echo: Проверь проект" in answer.text and f"--resume {SESSION_ID}" in answer.text
        assert not h.ctx.runner.active_runs
        run_id = next(iter(h.ctx.runner._finished))

        # 7. Download changed files from that run (src\main.py exists in the sandbox).
        await h.callback(RunCb(action="files", run_id=run_id).pack())
        assert any(isinstance(c, SendDocument) for c in h.session.take())

        # 8. File browser: ignored folders hidden, file menu, preview.
        await h.text("/files")
        listing = h.session.texts()
        assert listing and "node_modules" not in listing[0]
        h.session.take()
        await h.callback(FileCb(action="pv", token=h.ctx.token(allowed / "src" / "main.py")).pack())
        assert any("print(&#x27;hi&#x27;)" in t or "print('hi')" in t for t in h.session.texts())
        h.session.take()

        # 9. Path traversal through a forged token is refused.
        bad = h.ctx.token(tmp_path / "projects")
        await h.callback(FileCb(action="dl", token=bad).pack())
        assert not any(isinstance(c, SendDocument) for c in h.session.take())

        # 10. /get finds and sends a file.
        await h.text("/get main.py")
        assert any(isinstance(c, SendDocument) for c in h.session.take())

        # 11. Upload from phone -> save into the working folder.
        await h.document("notes.txt")
        assert any("notes.txt" in t for t in h.session.texts())
        h.session.take()
        await h.callback(UpCb(action="save").pack())
        saved = allowed / "notes.txt"
        assert saved.read_text(encoding="utf-8") == "содержимое с телефона"
        h.session.take()

        # 12. Full trust is the default; /mode switches access and limit profiles in one tap.
        from src.keyboards.inline import ModeCb

        assert h.ctx.state.skip_permissions
        await h.text("/mode")
        panel = h.session.take()
        mode_msg = next(c for c in panel if isinstance(c, SendMessage))
        labels = [b.text for row in mode_msg.reply_markup.inline_keyboard for b in row]
        assert "✅ 🔓 Skip Permissions (Full MCP & Bash)" in labels and "🔒 Restricted Tools" in labels
        assert "✅ ♾ Без лимитов (Auto)" in labels
        assert "💵 Эконом ($2 / 15 ходов)" in labels and "🚀 Pro ($15 / 80 ходов)" in labels
        await h.callback(ModeCb(action="perm_restricted").pack())
        await h.callback(ModeCb(action="lim_economy").pack())
        assert not h.ctx.state.skip_permissions and h.ctx.state.limit_profile.value == "economy"
        assert h.ctx.run_limits().max_budget_usd == 2.0 and h.ctx.run_limits().max_turns == 15
        h.session.take()

        # 13. Restricted run hits permission denials -> retry button -> unrestricted --resume.
        await h.text("DENY открой браузер и собери слайды")
        await h.drain()
        calls = h.session.take()
        final = [c.text for c in calls if isinstance(c, EditMessageText)][-1]
        assert "Отклонено разрешений: 2 (Bash, mcp__playwright__browser_run_code_unsafe)" in final
        answer = next(c for c in calls if isinstance(c, SendMessage) and c.parse_mode is None)
        buttons = [b for row in answer.reply_markup.inline_keyboard for b in row]
        retry = next(b for b in buttons if b.text == "🔓 Повторить без ограничений (--resume)")
        denied_run = h.ctx.runner.get(RunCb.unpack(retry.callback_data).run_id)
        assert denied_run is not None and denied_run.blocked_by_restrictions
        assert not denied_run.request.skip_permissions and denied_run.request.max_budget_usd == 2.0
        await h.callback(retry.callback_data)
        await h.drain()
        calls = h.session.take()
        answer = next(c for c in calls if isinstance(c, SendMessage) and c.parse_mode is None)
        assert "--dangerously-skip-permissions" in answer.text and f"--resume {SESSION_ID}" in answer.text
        assert "--allowedTools" not in answer.text and "--max-budget-usd" not in answer.text
        assert "--max-turns" not in answer.text and "Исходная задача" in answer.text
        assert not any(b.text.startswith("🔓 Повторить") for row in (answer.reply_markup.inline_keyboard
                       if answer.reply_markup else []) for b in row)

        # 14. Budget stop under the economy profile also offers the unrestricted retry.
        await h.text("BUDGET большой рефакторинг")
        await h.drain()
        calls = h.session.take()
        final = [c.text for c in calls if isinstance(c, EditMessageText)][-1]
        assert "Ошибка" in final and "лимит бюджета" in final
        offer = next(c for c in calls if isinstance(c, SendMessage) and c.reply_markup is not None
                     and any(b.text.startswith("🔓 Повторить") for row in c.reply_markup.inline_keyboard for b in row))
        assert offer is not None

    asyncio.run(scenario())
