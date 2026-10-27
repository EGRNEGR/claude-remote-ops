"""Expands `mcp__*` in --allowedTools into per-server rules understood by Claude Code.

Verified on Claude Code 2.1.304 (headless `-p`): `--allowedTools mcp__*` does NOT allow MCP tools
(they end up in permission_denials), while a server-level rule `mcp__<server>` allows every tool of
that server. Server names come from:
  * ~/.claude.json: `mcpServers` (user scope), `projects[<cwd>].mcpServers` (local scope),
    `claudeAiMcpEverConnected` (claude.ai connectors, e.g. "claude.ai Claude Docs");
  * <cwd>/.mcp.json: `mcpServers` (project scope);
  * `mcp_servers` of the `system/init` event of previous runs (plugins and anything else).
Tool prefixes normalize names: every char outside [A-Za-z0-9_-] becomes "_"
("claude.ai Claude Docs" -> mcp__claude_ai_Claude_Docs).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Iterable
from pathlib import Path

from src.utils.winpath import long_path, normcase

log = logging.getLogger(__name__)

MCP_WILDCARD = "mcp__*"
_NAME_RE = re.compile(r"[^A-Za-z0-9_-]")


def mcp_rule(server_name: str) -> str:
    return "mcp__" + _NAME_RE.sub("_", server_name.strip())


def _read_json(path: Path) -> dict:
    try:
        with open(long_path(path), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _names(value: object) -> set[str]:
    if isinstance(value, dict):
        return {str(k) for k in value}
    if isinstance(value, list):
        return {str(v) for v in value if isinstance(v, str)}
    return set()


class McpRegistry:
    def __init__(self, claude_config: Path | None = None) -> None:
        self._config = claude_config or Path.home() / ".claude.json"
        self._learned: set[str] = set()

    def learn(self, server_names: Iterable[str]) -> None:
        new = {n for n in server_names if n} - self._learned
        if new:
            self._learned |= new
            log.info("Learned MCP servers from CLI init: %s", sorted(new))

    def configured_servers(self, cwd: Path) -> set[str]:
        data = _read_json(self._config)
        names = _names(data.get("mcpServers")) | _names(data.get("claudeAiMcpEverConnected"))
        cwd_key = normcase(cwd)
        for project_path, project in (data.get("projects") or {}).items():
            if isinstance(project, dict) and normcase(project_path) == cwd_key:
                names |= _names(project.get("mcpServers"))
        names |= _names(_read_json(cwd / ".mcp.json").get("mcpServers"))
        return names

    async def server_names(self, cwd: Path) -> set[str]:
        return await asyncio.to_thread(self.configured_servers, cwd) | self._learned

    async def expand(self, allowed_tools: str, cwd: Path) -> str:
        """Replace `mcp__*` with `mcp__<server>` rules for all known servers (wildcard is kept last)."""
        items = [t.strip() for t in allowed_tools.split(",") if t.strip()]
        if MCP_WILDCARD not in items:
            return ",".join(items)
        rules = sorted({mcp_rule(name) for name in await self.server_names(cwd)})
        result = [t for t in items if t != MCP_WILDCARD]
        result += [r for r in rules if r not in result]
        result.append(MCP_WILDCARD)  # harmless today, effective if a future CLI supports it
        return ",".join(result)
