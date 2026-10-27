from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path

import pytest

from src.services.claude_runner import (
    ClaudeRunner,
    RunRequest,
    RunState,
    RunStatus,
    apply_event,
    describe_tool,
)
from src.services.state import RunMode
from src.utils.winpath import IS_WINDOWS, oem_encoding

pytestmark = pytest.mark.skipif(not IS_WINDOWS, reason="Windows runner behaviour")


def _pid_alive(pid: int) -> bool:
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True).stdout
    return str(pid) in out.decode(oem_encoding(), errors="replace")


def test_describe_tool() -> None:
    cwd = Path(r"C:\proj")
    assert describe_tool("Edit", {"file_path": r"C:\proj\src\main.py"}, cwd) == "📝 Редактирование файла src\\main.py"
    assert describe_tool("Bash", {"command": "git  status\n"}, cwd) == "⚙️ Выполнение команды: git status"
    assert describe_tool("mcp__github__create_issue", {}, cwd) == "🔌 MCP github: create_issue"
    assert describe_tool("Unknown", None, cwd) == "🔧 Unknown"


def test_apply_event_real_stream_shape(tmp_path: Path) -> None:
    """Event shapes captured from `claude -p --output-format stream-json --verbose` 2.1.304."""
    state = RunState(run_id="r1", request=RunRequest(prompt="p", cwd=tmp_path, mode=RunMode.NEW))
    events = [
        {"type": "system", "subtype": "init", "session_id": "2b846c6f-4081-4db2-a5ac-2e3f27c4444d",
         "model": "claude-opus-5-5", "cwd": str(tmp_path), "permissionMode": "default"},
        {"type": "assistant", "message": {"model": "claude-opus-5-5", "id": "msg_1", "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Glob", "input": {"pattern": "*.py"},
             "caller": {"type": "direct"}}]}, "parent_tool_use_id": None,
         "session_id": "2b846c6f-4081-4db2-a5ac-2e3f27c4444d"},
        {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"}},
        {"type": "user", "message": {"role": "user", "content": [
            {"tool_use_id": "toolu_1", "type": "tool_result", "content": "a.py"}]},
         "tool_use_result": {"filenames": ["a.py"]}},
        {"type": "assistant", "message": {"id": "msg_2", "content": [{"type": "thinking", "thinking": ""}]}},
        {"type": "assistant", "message": {"id": "msg_2", "content": [
            {"type": "tool_use", "name": "Write", "input": {"file_path": "out\\r.md", "content": "x"}}]},
         "parent_tool_use_id": "toolu_9"},
        {"type": "assistant", "message": {"id": "msg_2", "content": [{"type": "text", "text": "1"}]}},
        {"duration_api_ms": 10319, "stop_reason": "end_turn", "session_id": "2b846c6f-4081-4db2-a5ac-2e3f27c4444d",
         "total_cost_usd": 0.0843384, "permission_denials": [{"tool_name": "Bash"}], "terminal_reason": "completed",
         "is_error": False, "num_turns": 2, "subtype": "success", "result": "1", "type": "result", "duration_ms": 10751},
    ]
    for event in events:
        apply_event(state, event)
    assert state.session_id == "2b846c6f-4081-4db2-a5ac-2e3f27c4444d" and state.model == "claude-opus-5-5"
    assert state.tool_calls == 2 and state.assistant_messages == 2
    assert list(state.touched_files.values()) == [tmp_path / "out" / "r.md"]
    assert state.history[-1].startswith("[субагент] 📄 Запись файла")
    assert state.result is not None and state.result.text == "1" and state.result.cost_usd == pytest.approx(0.0843384)
    assert state.result.permission_denials == [{"tool_name": "Bash"}]
    assert state.version == len(events)


def test_build_args_and_env(make_settings, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    runner = ClaudeRunner(make_settings(CLAUDE_MODEL="opus"))
    sid = "6a17f423-802d-4103-b021-8562aecdb7d7"
    # Full access: bypass flag, NO --allowedTools, no limits when they are None.
    full = runner.build_args(RunRequest(prompt="x", cwd=tmp_path, mode=RunMode.RESUME, session_id=sid,
                                        skip_permissions=True))
    assert full[:4] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert full[full.index("--resume") + 1] == sid and full[full.index("--model") + 1] == "opus"
    assert full[-1] == "--dangerously-skip-permissions"
    assert "--allowedTools" not in full and "--max-turns" not in full and "--max-budget-usd" not in full
    # Restricted: wide default list incl. every MCP server, limits passed only when > 0.
    restricted = runner.build_args(RunRequest(prompt="x", cwd=tmp_path, mode=RunMode.NEW,
                                              max_turns=80, max_budget_usd=15.0))
    assert "--dangerously-skip-permissions" not in restricted
    assert restricted[-2] == "--allowedTools"
    assert restricted[-1] == "Read,Edit,MultiEdit,Write,Glob,Grep,LS,Bash,Skill,Task,WebFetch,WebSearch,NotebookEdit,mcp__*"
    assert restricted[restricted.index("--max-turns") + 1] == "80"
    assert restricted[restricted.index("--max-budget-usd") + 1] == "15"
    zero = runner.build_args(RunRequest(prompt="x", cwd=tmp_path, mode=RunMode.NEW, max_turns=0, max_budget_usd=0.0))
    assert "--max-turns" not in zero and "--max-budget-usd" not in zero
    assert "--continue" in runner.build_args(RunRequest(prompt="x", cwd=tmp_path, mode=RunMode.CONTINUE))
    with pytest.raises(Exception):
        runner.build_args(RunRequest(prompt="x", cwd=tmp_path, mode=RunMode.RESUME, session_id="../evil"))
    monkeypatch.setenv("CLAUDECODE", "1")
    monkeypatch.setenv("CLAUDE_CODE_GIT_BASH_PATH", "C:\\Git\\bin\\bash.exe")
    env = runner.build_env()
    assert "CLAUDECODE" not in env and env["CLAUDE_CODE_GIT_BASH_PATH"].endswith("bash.exe")
    assert env["PYTHONIOENCODING"] == "utf-8" and env["PYTHONUTF8"] == "1" and env["UTF8"] == "1"


def test_resolve_command_variants(make_settings, tmp_path: Path) -> None:
    npm = tmp_path / "npm"
    cli = npm / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("")
    (npm / "node.exe").write_bytes(b"")
    shim = npm / "claude.cmd"
    shim.write_text("@echo off")
    runner = ClaudeRunner(make_settings(CLAUDE_BINARY=str(shim)))
    assert runner.resolve_command() == [str(npm / "node.exe"), str(cli)]
    assert not runner._use_shell


def _run(runner: ClaudeRunner, req: RunRequest) -> RunState:
    return asyncio.run(runner.run(req))


def test_run_through_cmd_shim(make_settings, fake_claude_cmd: Path, tmp_path: Path) -> None:
    runner = ClaudeRunner(make_settings(CLAUDE_BINARY=str(fake_claude_cmd)))
    work = tmp_path / "allowed"
    prompt = 'Привет "мир" & echo pwned | 100% ^ <tag>'
    state = _run(runner, RunRequest(prompt=prompt, cwd=work, mode=RunMode.NEW))
    assert runner._use_shell
    assert state.status == RunStatus.FINISHED, (state.error, list(state.stderr_tail))
    assert state.result is not None
    assert state.result.text.startswith(f"echo: {prompt} | argv: -p --output-format stream-json")
    assert "--allowedTools Read,Edit,MultiEdit" in state.result.text and "mcp__*" in state.result.text
    assert "--max-turns" not in state.result.text and "--max-budget-usd" not in state.result.text
    full = _run(runner, RunRequest(prompt="x", cwd=work, mode=RunMode.NEW, skip_permissions=True,
                                   max_turns=5, max_budget_usd=0.5))
    assert full.result is not None and "--allowedTools" not in full.result.text
    assert "--dangerously-skip-permissions" in full.result.text and "--max-turns 5 --max-budget-usd 0.5" in full.result.text
    assert state.last_text == "Готово ✅"
    assert list(state.touched_files.values()) == [work / "src" / "main.py"]
    assert state.session_id == "11111111-2222-4333-8444-555555555555"
    assert runner.get(state.run_id) is state and not runner.active_runs
    assert state.session_id in runner.busy_session_ids()


def test_kill_switch_kills_process_tree(make_settings, fake_claude_cmd: Path, tmp_path: Path,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    pidfile = tmp_path / "child.pid"
    monkeypatch.setenv("FAKE_CLAUDE_PIDFILE", str(pidfile))
    runner = ClaudeRunner(make_settings(CLAUDE_BINARY=str(fake_claude_cmd)))

    async def scenario() -> tuple[RunState, int]:
        task = asyncio.create_task(runner.run(RunRequest(prompt="SLEEP", cwd=tmp_path, mode=RunMode.NEW)))
        deadline = time.monotonic() + 20
        while not pidfile.exists() and time.monotonic() < deadline:
            await asyncio.sleep(0.2)
        assert pidfile.exists(), "fake claude did not start"
        child_pid = int(pidfile.read_text())
        assert _pid_alive(child_pid)
        run_id = runner.active_runs[0].run_id
        assert await runner.kill(run_id)
        state = await asyncio.wait_for(task, timeout=30)
        return state, child_pid

    state, child_pid = asyncio.run(scenario())
    assert state.status == RunStatus.KILLED
    assert "Bash" in "".join(state.history) or state.tool_calls == 1
    time.sleep(0.5)
    assert not _pid_alive(child_pid), "grandchild survived taskkill /T"


def test_timeout_kills_run(make_settings, fake_claude_cmd: Path, tmp_path: Path) -> None:
    settings = make_settings(CLAUDE_BINARY=str(fake_claude_cmd))
    settings.CLAUDE_TIMEOUT_SEC = 3
    runner = ClaudeRunner(settings)
    started = time.monotonic()
    state = _run(runner, RunRequest(prompt="SLEEP", cwd=tmp_path, mode=RunMode.NEW))
    assert state.status == RunStatus.TIMEOUT
    assert time.monotonic() - started < 25


def test_missing_binary_reports_error(make_settings, tmp_path: Path) -> None:
    runner = ClaudeRunner(make_settings(CLAUDE_BINARY=str(tmp_path / "nope" / "claude.exe")))
    runner._command = None
    if os.path.exists(Path.home() / ".local" / "bin" / "claude.exe"):
        # resolve_command falls back to the real install location; force a broken path instead.
        runner._command = [str(tmp_path / "nope" / "claude.exe")]
    state = _run(runner, RunRequest(prompt="x", cwd=tmp_path, mode=RunMode.NEW))
    assert state.status == RunStatus.FAILED and state.error
