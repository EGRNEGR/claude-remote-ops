from __future__ import annotations

import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import Settings  # noqa: E402
from src.context import build_context  # noqa: E402
from src.services.session_parser import encode_project_path  # noqa: E402
from tg_harness import Harness  # noqa: E402

FAKE_CLAUDE = Path(__file__).resolve().parent / "fake_claude.py"
SESSION_ID = "6a17f423-802d-4103-b021-8562aecdb7d7"


def write_jsonl(path: Path, records: list[dict | str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write((rec if isinstance(rec, str) else json.dumps(rec, ensure_ascii=False)) + "\n")


def sample_records(cwd: str) -> list[dict | str]:
    """Records shaped exactly like Claude Code 2.1.304 transcripts (trimmed)."""
    base = {"isSidechain": False, "cwd": cwd, "sessionId": SESSION_ID, "version": "2.1.304",
            "gitBranch": "main", "entrypoint": "cli", "userType": "external"}
    return [
        {"type": "queue-operation", "operation": "enqueue", "timestamp": "2026-10-25T10:00:00.000Z",
         "sessionId": SESSION_ID, "content": "Почини тесты"},
        {**base, "type": "user", "timestamp": "2026-10-25T10:00:01.000Z", "promptSource": "typed",
         "origin": {"kind": "human"}, "message": {"role": "user", "content": "Почини тесты в проекте"}},
        {**base, "type": "user", "isMeta": True, "timestamp": "2026-10-25T10:00:01.100Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "Base directory for this skill: X"}]}},
        '{"type":"api-request-blob","sessionId":"x","hash":"h","message":{"role":"user","content":[]}}',
        {**base, "type": "attachment", "timestamp": "2026-10-25T10:00:01.200Z", "attachment": {"type": "environment"}},
        {**base, "type": "assistant", "timestamp": "2026-10-25T10:00:02.000Z", "apiBlockIndex": 0,
         "message": {"id": "msg_A", "model": "claude-opus-5-5", "role": "assistant",
                     "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
                               "cache_creation_input_tokens": 50},
                     "content": [{"type": "thinking", "thinking": ""}]}},
        {**base, "type": "assistant", "timestamp": "2026-10-25T10:00:02.100Z", "apiBlockIndex": 1,
         "message": {"id": "msg_A", "model": "claude-opus-5-5", "role": "assistant",
                     "usage": {"input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 100,
                               "cache_creation_input_tokens": 50},
                     "content": [{"type": "text", "text": "Сначала посмотрю"}]}},
        {**base, "type": "assistant", "timestamp": "2026-10-25T10:00:02.200Z", "apiBlockIndex": 2,
         "message": {"id": "msg_A", "model": "claude-opus-5-5", "role": "assistant",
                     "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                  "input": {"command": "pytest -q", "description": "run tests"}}]}},
        {**base, "type": "assistant", "timestamp": "2026-10-25T10:00:02.200Z", "apiBlockIndex": 2,
         "message": {"id": "msg_A", "model": "claude-opus-5-5", "role": "assistant",
                     "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                                  "input": {"command": "pytest -q", "description": "duplicate block"}}]}},
        {**base, "type": "user", "timestamp": "2026-10-25T10:00:03.000Z", "sourceToolAssistantUUID": "u",
         "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "is_error": True,
                                                  "content": "1 failed"}]}},
        {**base, "type": "assistant", "timestamp": "2026-10-25T10:00:04.000Z", "apiBlockIndex": 0,
         "message": {"id": "msg_B", "model": "claude-opus-5-5", "role": "assistant",
                     "content": [{"type": "tool_use", "id": "t2", "name": "Edit",
                                  "input": {"file_path": cwd + "\\src\\app.py", "old_string": "a", "new_string": "b"}}]}},
        {**base, "type": "assistant", "timestamp": "2026-10-25T10:00:04.100Z", "apiBlockIndex": 1,
         "message": {"id": "msg_B", "model": "claude-opus-5-5", "role": "assistant",
                     "content": [{"type": "tool_use", "id": "t3", "name": "Write",
                                  "input": {"file_path": cwd + "\\tests\\test_new.py", "content": "x"}}]}},
        {**base, "type": "assistant", "timestamp": "2026-10-25T10:00:04.200Z", "apiBlockIndex": 2,
         "message": {"id": "msg_B", "model": "claude-opus-5-5", "role": "assistant",
                     "content": [{"type": "tool_use", "id": "t4", "name": "PowerShell",
                                  "input": {"command": "Get-ChildItem -Recurse"}}]}},
        {**base, "type": "assistant", "isSidechain": True, "timestamp": "2026-10-25T10:00:04.300Z",
         "message": {"id": "msg_SIDE", "content": [{"type": "tool_use", "name": "Write",
                                                    "input": {"file_path": "C:\\side.txt"}}]}},
        {**base, "type": "user", "timestamp": "2026-10-25T10:00:05.000Z",
         "message": {"role": "user", "content": "<command-name>/compact</command-name>\n<command-args></command-args>"}},
        {**base, "type": "user", "timestamp": "2026-10-25T10:00:05.500Z",
         "message": {"role": "user", "content": "<local-command-stdout>done</local-command-stdout>"}},
        {**base, "type": "user", "timestamp": "2026-10-25T10:00:05.700Z", "origin": {"kind": "task-notification"},
         "message": {"role": "user", "content": "<task-notification>x</task-notification>"}},
        {**base, "type": "user", "timestamp": "2026-10-25T10:00:06.000Z",
         "message": {"role": "user", "content": [{"type": "text", "text": "[Request interrupted by user]"}]}},
        {**base, "type": "user", "timestamp": "2026-10-25T10:00:07.000Z", "promptSource": "typed",
         "message": {"role": "user", "content": "Теперь добавь тест"}},
        {**base, "type": "assistant", "timestamp": "2026-10-25T10:00:08.000Z", "apiBlockIndex": 0,
         "message": {"id": "msg_C", "model": "claude-opus-5-5", "role": "assistant",
                     "content": [{"type": "text", "text": "Все тесты проходят."}]}},
        {**base, "type": "assistant", "timestamp": "2026-10-25T10:00:08.100Z", "apiBlockIndex": 1,
         "message": {"id": "msg_C", "model": "claude-opus-5-5", "role": "assistant",
                     "content": [{"type": "text", "text": "Изменено 2 файла."}]}},
        {"type": "ai-title", "aiTitle": "fix-tests", "sessionId": SESSION_ID},
        {"type": "last-prompt", "lastPrompt": "Теперь добавь тест", "sessionId": SESSION_ID},
        {"type": "cost-state", "sessionId": SESSION_ID, "totalCostUSD": 0.5},
        {"type": "cost-state", "sessionId": SESSION_ID, "totalCostUSD": 1.25},
    ]


@pytest.fixture
def make_settings(tmp_path: Path) -> Callable[..., Settings]:
    def factory(**overrides: object) -> Settings:
        allowed = tmp_path / "allowed"
        allowed.mkdir(exist_ok=True)
        projects = tmp_path / "projects"
        projects.mkdir(exist_ok=True)
        values: dict[str, object] = {
            "BOT_TOKEN": "123456:TEST_TOKEN_abcdefghijklmnopqrstuvwxyz",
            "ADMIN_USER_ID": 42,
            "ALLOWED_DIRS": str(allowed),
            "CLAUDE_PROJECTS_DIR": str(projects),
            "DATA_DIR": str(tmp_path / "data"),
            "LOG_DIR": str(tmp_path / "logs"),
        }
        values.update(overrides)
        return Settings(_env_file=None, **values)  # type: ignore[call-arg]

    return factory


@pytest.fixture
def fake_claude_cmd(tmp_path: Path) -> Path:
    """claude.cmd shim without node_modules -> exercises the cmd.exe fallback path."""
    shim_dir = tmp_path / "npm bin"
    shim_dir.mkdir()
    shim = shim_dir / "claude.cmd"
    shim.write_text(f'@"{sys.executable}" "{FAKE_CLAUDE}" %*\r\n', encoding="utf-8")
    return shim


@pytest.fixture
def harness_factory(make_settings, fake_claude_cmd: Path, tmp_path: Path):
    def factory(**overrides: object) -> Harness:
        settings = make_settings(CLAUDE_BINARY=str(fake_claude_cmd), STATUS_UPDATE_INTERVAL=1.0, **overrides)
        ctx = build_context(settings)
        return Harness(ctx)

    allowed = tmp_path / "allowed"
    (allowed / "src").mkdir(parents=True, exist_ok=True)
    (allowed / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (allowed / "node_modules").mkdir(exist_ok=True)
    (allowed / "node_modules" / "main.py").write_text("x", encoding="utf-8")
    cwd = str(allowed.resolve())
    write_jsonl(tmp_path / "projects" / encode_project_path(cwd) / f"{SESSION_ID}.jsonl", sample_records(cwd))
    return factory
