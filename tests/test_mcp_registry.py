from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.services.claude_runner import ClaudeRunner, RunRequest, RunState, apply_event
from src.services.mcp_registry import McpRegistry, mcp_rule
from src.services.state import RunMode
from src.utils.winpath import IS_WINDOWS

TOOLS = "Read,Edit,MultiEdit,Write,Glob,Grep,LS,Bash,Skill,Task,WebFetch,WebSearch,NotebookEdit,mcp__*"


def _config(tmp_path: Path, project: Path) -> Path:
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"playwright": {"command": "npx"}, "memory": {"command": "npx"}},
        "claudeAiMcpEverConnected": ["claude.ai Claude Docs"],
        "projects": {
            str(project).replace("\\", "/").upper(): {"mcpServers": {"figma-local": {"command": "x"}}},
            "C:/Other": {"mcpServers": {"not-mine": {}}},
        },
    }), encoding="utf-8")
    (project / ".mcp.json").write_text(json.dumps({"mcpServers": {"proj.db": {"command": "y"}}}), encoding="utf-8")
    return cfg


def test_mcp_rule_normalization() -> None:
    assert mcp_rule("claude.ai Claude Docs") == "mcp__claude_ai_Claude_Docs"
    assert mcp_rule("sequential-thinking") == "mcp__sequential-thinking"
    assert mcp_rule("proj.db") == "mcp__proj_db"


def test_expand_wildcard_from_all_sources(tmp_path: Path) -> None:
    project = tmp_path / "proj"
    project.mkdir()
    registry = McpRegistry(_config(tmp_path, project))
    registry.learn(["plugin:figma"])

    expanded = asyncio.run(registry.expand(TOOLS, project)).split(",")
    assert expanded[:13] == TOOLS.split(",")[:13]
    rules = set(expanded[13:-1])
    expected = {"mcp__playwright", "mcp__memory", "mcp__claude_ai_Claude_Docs", "mcp__proj_db", "mcp__plugin_figma"}
    if IS_WINDOWS:
        expected.add("mcp__figma-local")  # local scope matched case-insensitively with / separators
    assert rules == expected
    assert "mcp__not-mine" not in expanded and expanded[-1] == "mcp__*"
    assert asyncio.run(registry.expand("Read,Bash", project)) == "Read,Bash"


def test_runner_expands_only_in_restricted_mode(make_settings, tmp_path: Path, fake_claude_cmd: Path) -> None:
    project = tmp_path / "allowed"
    runner = ClaudeRunner(make_settings(CLAUDE_BINARY=str(fake_claude_cmd)))
    runner.mcp = McpRegistry(_config(tmp_path, project))
    if not IS_WINDOWS:
        pytest.skip("cmd shim is Windows only")
    restricted = asyncio.run(runner.run(RunRequest(prompt="x", cwd=project, mode=RunMode.NEW)))
    assert restricted.result is not None
    assert "mcp__playwright" in restricted.result.text and "mcp__claude_ai_Claude_Docs" in restricted.result.text
    full = asyncio.run(runner.run(RunRequest(prompt="x", cwd=project, mode=RunMode.NEW, skip_permissions=True)))
    assert full.result is not None and "mcp__" not in full.result.text.split("| argv:")[1]


def test_init_event_teaches_registry(tmp_path: Path) -> None:
    state = RunState(run_id="r", request=RunRequest(prompt="p", cwd=tmp_path, mode=RunMode.NEW))
    apply_event(state, {"type": "system", "subtype": "init", "session_id": "s", "mcp_servers": [
        {"name": "playwright", "status": "connected", "source": "user"},
        {"name": "claude.ai Claude Docs", "status": "pending", "source": "claudeai"},
    ]})
    assert state.mcp_servers == ["playwright", "claude.ai Claude Docs"]
    registry = McpRegistry(tmp_path / "missing.json")
    registry.learn(state.mcp_servers)
    assert asyncio.run(registry.server_names(tmp_path)) == {"playwright", "claude.ai Claude Docs"}
