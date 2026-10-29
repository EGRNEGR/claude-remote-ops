"""Runs Claude Code CLI headless on Windows, streams `stream-json` events, kills process trees.

Windows specifics handled here:
  * `claude` may be a native claude.exe (~/.local/bin), an npm shim (claude.cmd / claude.ps1) or a
    bare node script. The executable is located with shutil.which() and npm shims are unwrapped to
    `node cli.js` so no cmd.exe quoting is involved.
  * The prompt is written to stdin (UTF-8) instead of argv: no 8191/32767 char command line limits and
    no cmd.exe metacharacter injection through user text.
  * Child output is decoded as UTF-8 explicitly (errors="replace"), PYTHONIOENCODING/PYTHONUTF8/UTF8 are
    forced so Python tools launched by Claude do not fall back to cp1251/cp866.
  * proc.terminate() does not kill node/bash/pwsh grandchildren, so `taskkill /F /T /PID` is used.

stream-json events (verified on Claude Code 2.1.304):
  {"type":"system","subtype":"init","session_id":...,"cwd":...,"model":...,"permissionMode":...}
  {"type":"assistant","message":{"id":...,"content":[{"type":"tool_use","name":"Glob","input":{...}}]},
   "parent_tool_use_id":null|"toolu_...","session_id":...}
  {"type":"user","message":{"content":[{"type":"tool_result","tool_use_id":...,"is_error":bool}]}}
  {"type":"rate_limit_event","rate_limit_info":{"status":"allowed",...}}
  {"type":"result","subtype":"success"|"error_*","is_error":bool,"result":str,"total_cost_usd":float,
   "session_id":...,"num_turns":int,"duration_ms":int,"permission_denials":[...],"terminal_reason":...}

A chat that is running as a background session on the PC (`claude --bg`, agents view) cannot be resumed:
`claude -p --resume <id>` exits with code 1 before any event and prints to stderr (2.1.304):
  Error: That session is running in the background (59080800). Run `claude attach 59080800` to open it,
  or `claude stop 59080800` first to resume it here. Add --fork-session to branch off a copy instead.
Both ways out are supported: RunRequest.fork_session (new session id, full context, the PC session keeps
running) and ClaudeRunner.stop_background() (`claude stop <id>` keeps the conversation, then resume).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import subprocess
import time
from collections import OrderedDict, deque
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from src.config import Settings
from src.services.mcp_registry import McpRegistry
from src.services.reasoning import ReasoningEffort, effort_args, effort_env, prompt_for_effort
from src.services.session_parser import SESSION_ID_RE
from src.services.state import RunMode
from src.utils.text import one_line, strip_ansi, truncate, truncate_middle
from src.utils.winpath import IS_WINDOWS, is_within, normcase, oem_encoding

log = logging.getLogger(__name__)

STREAM_LIMIT = 64 * 1024 * 1024
FINISHED_KEEP = 30

# Variables injected by a parent Claude Code session. If the bot is started from a Claude terminal
# they must not leak into the child, otherwise it behaves as a nested/child session.
_PARENT_SESSION_ENV = frozenset({
    "CLAUDECODE", "CLAUDE_CODE_ENTRYPOINT", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_CHILD_SESSION",
    "CLAUDE_CODE_MESSAGING_SOCKET", "CLAUDE_CODE_MESSAGING_TOKEN", "CLAUDE_CODE_SESSION_ATTENDED",
    "CLAUDE_CODE_AGENT", "CLAUDE_CODE_EXECPATH", "CLAUDE_CODE_ALT_SCREEN_FULL_REPAINT",
    "CLAUDE_PID", "CLAUDE_JOB_DIR", "CLAUDE_EFFORT", "AI_AGENT",
})
_CMD_UNSAFE_CHARS = frozenset('"%^&|<>!\r\n')

_CREATION_FLAGS = (
    (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if IS_WINDOWS else 0
)

# `--resume` refused: the chat runs as a background session. The --continue wording is matched as well.
BACKGROUND_SESSION_RE = re.compile(
    r"(?:That session|Your most recent conversation) is running in the background "
    r"\((?:session )?([A-Za-z0-9_-]{4,64})\)"
)
# Short ids printed by `claude --bg` / `claude agents` (e.g. 59080800); also passed to `claude stop`.
BACKGROUND_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
STOP_TIMEOUT_SEC = 60.0


class RunStatus(StrEnum):
    STARTING = "starting"
    RUNNING = "running"
    FINISHED = "finished"
    FAILED = "failed"
    KILLED = "killed"
    TIMEOUT = "timeout"


class RunnerError(Exception):
    """Base error of the Claude runner."""


class RunnerBusyError(RunnerError):
    """MAX_CONCURRENT_RUNS reached."""


class ClaudeNotFoundError(RunnerError):
    """Claude CLI executable could not be located."""


@dataclass(slots=True)
class RunRequest:
    prompt: str
    cwd: Path
    mode: RunMode
    session_id: str | None = None
    skip_permissions: bool = False
    label: str = ""
    max_turns: int | None = None
    max_budget_usd: float | None = None
    # --fork-session with --resume / --continue: a new session id with the full context of the chat.
    fork_session: bool = False
    # --model value (alias or full name); None = the CLI default model.
    model: str | None = None
    reasoning: ReasoningEffort = ReasoningEffort.DEFAULT

    @property
    def stdin_prompt(self) -> str:
        """Text written to the CLI stdin (`max` reasoning adds the `ultrathink` keyword)."""
        return prompt_for_effort(self.prompt, self.reasoning)


@dataclass(slots=True)
class BackgroundConflict:
    """The chat is open as a background session on the PC (`claude --bg` / agents view)."""

    bg_id: str
    message: str
    handled: bool = False  # a 🌿 / 🛑 button was pressed: ignore repeated taps


def detect_background_conflict(lines: Iterable[str]) -> BackgroundConflict | None:
    for line in lines:
        match = BACKGROUND_SESSION_RE.search(line)
        if match is not None:
            message = line.strip().removeprefix("Error:").strip()
            return BackgroundConflict(bg_id=match.group(1), message=message)
    return None


LIMIT_SUBTYPES = frozenset({"error_max_turns", "error_max_budget_usd"})


@dataclass(slots=True)
class RunResult:
    subtype: str
    is_error: bool
    text: str
    cost_usd: float | None
    session_id: str | None
    num_turns: int | None
    duration_ms: int | None
    permission_denials: list[dict]
    terminal_reason: str | None

    @property
    def hit_limit(self) -> bool:
        return self.subtype in LIMIT_SUBTYPES

    @property
    def denied_tools(self) -> list[str]:
        return sorted({str(d.get("tool_name") or "?") for d in self.permission_denials})


@dataclass
class RunState:
    run_id: str
    request: RunRequest
    started_at: float = field(default_factory=time.monotonic)
    started_wall: float = field(default_factory=time.time)
    finished_at: float | None = None
    pid: int | None = None
    status: RunStatus = RunStatus.STARTING
    session_id: str | None = None
    model: str | None = None
    activity: str = "⏳ Запуск Claude CLI..."
    history: deque[str] = field(default_factory=lambda: deque(maxlen=6))
    tool_calls: int = 0
    tool_errors: int = 0
    assistant_messages: int = 0
    touched_files: dict[str, Path] = field(default_factory=dict)
    last_text: str = ""
    result: RunResult | None = None
    stderr_tail: deque[str] = field(default_factory=lambda: deque(maxlen=40))
    return_code: int | None = None
    error: str | None = None
    version: int = 0
    mcp_servers: list[str] = field(default_factory=list)
    background_conflict: BackgroundConflict | None = None
    # (tokens before, tokens after) of a /compact in this run, from the compact_boundary event
    compacted: tuple[int, int] | None = None
    _message_ids: set[str] = field(default_factory=set)

    @property
    def elapsed(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at

    @property
    def is_active(self) -> bool:
        return self.status in (RunStatus.STARTING, RunStatus.RUNNING)

    @property
    def blocked_by_restrictions(self) -> bool:
        """Permission denials or a --max-turns / --max-budget-usd stop: worth an unrestricted retry."""
        result = self.result
        return result is not None and (bool(result.permission_denials) or result.hit_limit)

    def touch(self) -> None:
        self.version += 1


def _rel(path_text: str, cwd: Path) -> str:
    path = Path(path_text)
    if path.is_absolute() and is_within(path, cwd):
        with contextlib.suppress(ValueError):
            return os.path.relpath(path, cwd)
    return path_text


def describe_tool(name: str, tool_input: object, cwd: Path) -> str:
    """Human readable (unescaped) description of a tool call for the live status."""
    inp = tool_input if isinstance(tool_input, dict) else {}

    def arg(key: str, limit: int = 90) -> str:
        value = inp.get(key)
        return truncate(one_line(str(value)), limit) if value else ""

    def file_arg(key: str = "file_path") -> str:
        value = inp.get(key)
        return truncate_middle(_rel(str(value), cwd), 70) if value else "?"

    if name == "Bash":
        return f"⚙️ Выполнение команды: {arg('command')}"
    if name == "PowerShell":
        return f"⚙️ PowerShell: {arg('command')}"
    if name in ("Edit", "MultiEdit"):
        return f"📝 Редактирование файла {file_arg()}"
    if name == "Write":
        return f"📄 Запись файла {file_arg()}"
    if name == "NotebookEdit":
        return f"📓 Правка ноутбука {file_arg('notebook_path')}"
    if name == "Read":
        return f"📖 Чтение файла {file_arg()}"
    if name == "Glob":
        return f"🔎 Поиск файлов: {arg('pattern', 60)}"
    if name == "Grep":
        return f"🔎 Поиск в коде: {arg('pattern', 60)}"
    if name == "WebFetch":
        return f"🌐 Загрузка страницы: {arg('url', 70)}"
    if name == "WebSearch":
        return f"🌐 Поиск в интернете: {arg('query', 70)}"
    if name in ("Task", "Agent"):
        return f"🤖 Субагент: {arg('description', 70) or arg('subagent_type', 40)}"
    if name == "TodoWrite":
        return "🗒 Обновление плана задач"
    if name == "Skill":
        return f"🧩 Навык: {arg('skill', 50) or arg('command', 50)}"
    if name == "ToolSearch":
        return "🧰 Подключение инструментов"
    if name.startswith("mcp__"):
        parts = name.split("__")
        return f"🔌 MCP {parts[1] if len(parts) > 1 else ''}: {parts[-1]}"
    return f"🔧 {name}"


def apply_event(state: RunState, event: dict) -> None:
    """Update RunState from one stream-json event."""
    etype = event.get("type")
    cwd = state.request.cwd
    if etype == "system":
        subtype = event.get("subtype")
        if subtype == "init":
            state.session_id = event.get("session_id") or state.session_id
            state.model = event.get("model") or state.model
            state.mcp_servers = [
                str(srv.get("name")) for srv in event.get("mcp_servers") or []
                if isinstance(srv, dict) and srv.get("name")
            ]
            state.activity = f"🚀 Сессия запущена ({state.model or 'model ?'})"
        elif subtype == "compact_boundary":
            state.activity = "🗜 Сжатие контекста..."
            meta = event.get("compact_metadata") if isinstance(event.get("compact_metadata"), dict) else {}
            try:
                state.compacted = (int(meta.get("pre_tokens") or 0), int(meta.get("post_tokens") or 0))
            except (TypeError, ValueError):
                state.compacted = (0, 0)
        elif subtype == "api_retry":
            state.activity = "🔁 Повтор запроса к API..."
    elif etype == "assistant":
        state.session_id = event.get("session_id") or state.session_id
        message = event.get("message") or {}
        mid = str(message.get("id") or event.get("uuid") or "")
        if mid and mid not in state._message_ids:
            state._message_ids.add(mid)
            state.assistant_messages += 1
        prefix = "[субагент] " if event.get("parent_tool_use_id") else ""
        for block in message.get("content") or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                name = str(block.get("name") or "?")
                tool_input = block.get("input") or {}
                state.tool_calls += 1
                desc = prefix + describe_tool(name, tool_input, cwd)
                state.activity = desc
                state.history.append(desc)
                if name in ("Edit", "MultiEdit", "Write", "NotebookEdit") and isinstance(tool_input, dict):
                    raw = tool_input.get("file_path") or tool_input.get("notebook_path")
                    if isinstance(raw, str) and raw:
                        path = Path(raw)
                        if not path.is_absolute():
                            path = cwd / path
                        state.touched_files.setdefault(normcase(path), path)
            elif btype == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    state.last_text = text.strip()
                    if not prefix:
                        state.activity = "💬 Claude формирует ответ..."
            elif btype == "thinking" and not prefix:
                state.activity = "🧠 Claude размышляет..."
    elif etype == "user":
        content = (event.get("message") or {}).get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result" and block.get("is_error"):
                    state.tool_errors += 1
    elif etype == "rate_limit_event":
        info = event.get("rate_limit_info") or {}
        if info.get("status") not in (None, "allowed"):
            state.activity = f"⏳ Лимит запросов: {info.get('status')}"
    elif etype == "result":
        cost = event.get("total_cost_usd")
        state.result = RunResult(
            subtype=str(event.get("subtype") or ""),
            is_error=bool(event.get("is_error")),
            text=str(event.get("result") or ""),
            cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
            session_id=event.get("session_id") or state.session_id,
            num_turns=event.get("num_turns") if isinstance(event.get("num_turns"), int) else None,
            duration_ms=event.get("duration_ms") if isinstance(event.get("duration_ms"), int) else None,
            permission_denials=[d for d in event.get("permission_denials") or [] if isinstance(d, dict)],
            terminal_reason=event.get("terminal_reason"),
        )
        state.session_id = state.result.session_id or state.session_id
        state.activity = "🏁 Завершение..."
    state.touch()


async def kill_process_tree(pid: int) -> tuple[int, str]:
    """Kill a process and all its children. Returns (exit_code, tool output)."""
    if not IS_WINDOWS:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        return 0, ""
    taskkill = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")
    proc = await asyncio.create_subprocess_exec(
        taskkill, "/F", "/T", "/PID", str(pid),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except TimeoutError:
        proc.kill()
        return -1, "taskkill timeout"
    text = out.decode(oem_encoding(), errors="replace").strip()
    log.info("taskkill /T /PID %s -> %s: %s", pid, proc.returncode, one_line(text))
    return proc.returncode or 0, text


class ClaudeRunner:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._active: dict[str, RunState] = {}
        self._procs: dict[str, asyncio.subprocess.Process] = {}
        self._finished: OrderedDict[str, RunState] = OrderedDict()
        self._command: list[str] | None = None
        self._use_shell = False
        self.mcp = McpRegistry()

    # ---------- executable discovery ----------

    def resolve_command(self) -> list[str]:
        if self._command is not None:
            return self._command
        configured = self.settings.CLAUDE_BINARY.strip().strip('"') or "claude"
        exe = shutil.which(configured)
        if exe is None and os.path.isfile(configured):
            exe = configured
        if exe is None and IS_WINDOWS:
            appdata = os.environ.get("APPDATA", "")
            for candidate in (
                Path.home() / ".local" / "bin" / "claude.exe",
                Path(appdata) / "npm" / "claude.cmd",
                Path(appdata) / "npm" / "claude.ps1",
            ):
                if candidate.is_file():
                    exe = str(candidate)
                    break
        if exe is None:
            raise ClaudeNotFoundError(
                "Claude CLI не найден. Установите его или укажите полный путь в CLAUDE_BINARY (.env)"
            )
        exe_path = Path(exe)
        suffix = exe_path.suffix.lower()
        command: list[str]
        if suffix in (".cmd", ".bat", ".ps1"):
            cli_js = exe_path.parent / "node_modules" / "@anthropic-ai" / "claude-code" / "cli.js"
            local_node = exe_path.parent / "node.exe"
            node = str(local_node) if local_node.is_file() else shutil.which("node")
            if cli_js.is_file() and node:
                command = [node, str(cli_js)]
            elif suffix == ".ps1":
                shell = shutil.which("pwsh") or shutil.which("powershell") or "powershell.exe"
                command = [shell, "-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", exe]
            else:
                command = [exe]
                self._use_shell = True
        elif suffix == ".js":
            node = shutil.which("node")
            if not node:
                raise ClaudeNotFoundError("Для cli.js нужен node.exe в PATH")
            command = [node, exe]
        else:
            command = [exe]
        log.info("Claude CLI command: %s", command)
        self._command = command
        return command

    def describe_binary(self) -> str:
        try:
            return " ".join(self.resolve_command())
        except ClaudeNotFoundError as exc:
            return f"не найден ({exc})"

    # ---------- process plumbing ----------

    def build_args(self, req: RunRequest, allowed_tools: str | None = None) -> list[str]:
        """CLI arguments (the prompt itself goes through stdin). `allowed_tools`: pre-expanded list."""
        s = self.settings
        args = ["-p", "--output-format", "stream-json", "--verbose"]
        # Limits are per run (profile from /mode); None or 0 = flag omitted = no artificial stop.
        if req.max_turns and req.max_turns > 0:
            args += ["--max-turns", str(req.max_turns)]
        if req.max_budget_usd and req.max_budget_usd > 0:
            args += ["--max-budget-usd", f"{req.max_budget_usd:g}"]
        if req.mode == RunMode.RESUME:
            if not req.session_id or not SESSION_ID_RE.match(req.session_id):
                raise RunnerError("Некорректный session_id для --resume")
            args += ["--resume", req.session_id]
        elif req.mode == RunMode.CONTINUE:
            args.append("--continue")
        if req.fork_session and req.mode in (RunMode.RESUME, RunMode.CONTINUE):
            args.append("--fork-session")
        if req.model:
            args += ["--model", req.model]
        args += effort_args(req.reasoning)
        if req.skip_permissions:
            # Full access (any Bash, Skill, MCP server). --allowedTools is NOT passed so that a
            # restrictive list can never narrow the bypass mode.
            args.append("--dangerously-skip-permissions")
        else:
            tools = (allowed_tools if allowed_tools is not None else s.CLAUDE_ALLOWED_TOOLS).strip()
            if tools:
                # Variadic option: keep it last so nothing is swallowed by it.
                args += ["--allowedTools", tools]
        return args

    @staticmethod
    def build_env(reasoning: ReasoningEffort = ReasoningEffort.DEFAULT) -> dict[str, str]:
        """Child env: parent-session vars stripped, UTF-8 forced, thinking vars set for the chosen level."""
        overrides = effort_env(reasoning)
        owned = {key.upper() for key in overrides}
        env = {
            k: v for k, v in os.environ.items()
            if k.upper() not in _PARENT_SESSION_ENV and k.upper() not in owned
        }
        env.update({key: value for key, value in overrides.items() if value is not None})
        env.update({
            "PYTHONIOENCODING": "utf-8",
            "PYTHONUTF8": "1",
            "UTF8": "1",
            "NO_COLOR": "1",
            "FORCE_COLOR": "0",
        })
        return env

    async def _spawn(self, req: RunRequest) -> asyncio.subprocess.Process:
        command = self.resolve_command()
        tools = None
        if not req.skip_permissions:
            # `mcp__*` is not honoured by the CLI in --allowedTools: expand to mcp__<server> rules.
            tools = await self.mcp.expand(self.settings.CLAUDE_ALLOWED_TOOLS, req.cwd)
        args = self.build_args(req, tools)
        log.info("Claude args: %s (reasoning=%s)", args, req.reasoning)
        common = dict(
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(req.cwd),
            env=self.build_env(req.reasoning),
            limit=STREAM_LIMIT,
        )
        if self._use_shell:
            # Fallback for an npm .cmd shim without cli.js: arguments are bot-controlled only
            # (the prompt goes through stdin), still refuse cmd.exe metacharacters.
            for arg in [*command, *args]:
                if any(ch in _CMD_UNSAFE_CHARS for ch in arg):
                    raise RunnerError(f"Небезопасный символ в аргументе для cmd.exe: {arg!r}")
            cmdline = subprocess.list2cmdline([*command, *args])
            return await asyncio.create_subprocess_shell(cmdline, creationflags=_CREATION_FLAGS, **common)
        return await asyncio.create_subprocess_exec(
            *command, *args, creationflags=_CREATION_FLAGS, start_new_session=not IS_WINDOWS, **common
        )

    @staticmethod
    async def _feed_stdin(proc: asyncio.subprocess.Process, prompt: str) -> None:
        if proc.stdin is None:
            return
        try:
            proc.stdin.write(prompt.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            log.warning("stdin closed early: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                proc.stdin.close()

    @staticmethod
    async def _drain_stderr(proc: asyncio.subprocess.Process, state: RunState) -> None:
        if proc.stderr is None:
            return
        while True:
            try:
                line = await proc.stderr.readline()
            except ValueError:
                continue
            if not line:
                break
            text = strip_ansi(line.decode("utf-8", errors="replace")).rstrip()
            if text:
                state.stderr_tail.append(truncate(text, 500))

    @staticmethod
    async def _read_stdout(
        proc: asyncio.subprocess.Process,
        state: RunState,
        on_update: Callable[[RunState], None] | None,
    ) -> None:
        assert proc.stdout is not None
        while True:
            try:
                line = await proc.stdout.readline()
            except ValueError:
                log.warning("stream-json line exceeded %s bytes, skipped", STREAM_LIMIT)
                continue
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except ValueError:
                state.stderr_tail.append(truncate(strip_ansi(text), 500))
                continue
            if not isinstance(event, dict):
                continue
            apply_event(state, event)
            if on_update is not None:
                on_update(state)

    # ---------- public API ----------

    @property
    def active_runs(self) -> list[RunState]:
        return list(self._active.values())

    def get(self, run_id: str) -> RunState | None:
        return self._active.get(run_id) or self._finished.get(run_id)

    def busy_session_ids(self, grace_sec: float = 120.0) -> set[str]:
        """Sessions written by bot runs (active or finished recently) - the watcher skips them."""
        ids: set[str] = set()
        for state in self._active.values():
            ids.update(sid for sid in (state.session_id, state.request.session_id) if sid)
        now = time.monotonic()
        for state in self._finished.values():
            if state.session_id and state.finished_at and now - state.finished_at < grace_sec:
                ids.add(state.session_id)
        return ids

    def ensure_capacity(self) -> None:
        if len(self._active) >= self.settings.MAX_CONCURRENT_RUNS:
            raise RunnerBusyError(
                f"Уже выполняется {len(self._active)} процесс(а). Дождитесь завершения или остановите его."
            )

    async def run(
        self,
        req: RunRequest,
        on_update: Callable[[RunState], None] | None = None,
        on_started: Callable[[RunState], None] | None = None,
    ) -> RunState:
        self.ensure_capacity()
        run_id = secrets.token_hex(4)
        state = RunState(run_id=run_id, request=req)
        self._active[run_id] = state
        if on_started is not None:
            on_started(state)
        try:
            await self._execute(state, on_update)
        finally:
            state.finished_at = time.monotonic()
            self._active.pop(run_id, None)
            self._procs.pop(run_id, None)
            self._finished[run_id] = state
            while len(self._finished) > FINISHED_KEEP:
                self._finished.popitem(last=False)
            self.mcp.learn(state.mcp_servers)
            result = state.result
            log.info(
                "Run %s finished: status=%s subtype=%s cost=%s turns=%s denied=%s session=%s",
                run_id, state.status, result.subtype if result else None,
                result.cost_usd if result else None, result.num_turns if result else None,
                result.denied_tools if result else [], state.session_id,
            )
            state.touch()
            if on_update is not None:
                on_update(state)
        return state

    async def _execute(self, state: RunState, on_update: Callable[[RunState], None] | None) -> None:
        req = state.request
        try:
            proc = await self._spawn(req)
        except (OSError, RunnerError) as exc:
            state.status = RunStatus.FAILED
            state.error = f"Не удалось запустить Claude CLI: {exc}"
            log.error(state.error)
            return
        state.pid = proc.pid
        state.status = RunStatus.RUNNING
        state.activity = "⏳ Claude CLI запущен, ожидание первого события..."
        state.touch()
        self._procs[state.run_id] = proc
        log.info("Run %s started: pid=%s cwd=%s mode=%s", state.run_id, proc.pid, req.cwd, req.mode)
        stderr_task = asyncio.create_task(self._drain_stderr(proc, state))
        await self._feed_stdin(proc, req.stdin_prompt)
        reader = asyncio.create_task(self._read_stdout(proc, state, on_update))
        waiter = asyncio.create_task(proc.wait())
        try:
            async with asyncio.timeout(self.settings.CLAUDE_TIMEOUT_SEC):
                done, _ = await asyncio.wait({reader, waiter}, return_when=asyncio.FIRST_COMPLETED)
                if waiter in done:
                    # Process exited; a background grandchild may still hold stdout open.
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(reader), timeout=5)
                else:
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(asyncio.shield(waiter), timeout=15)
                    if not waiter.done():
                        await kill_process_tree(proc.pid)
        except TimeoutError:
            if state.status != RunStatus.KILLED:
                state.status = RunStatus.TIMEOUT
                state.error = f"Превышен лимит времени {self.settings.CLAUDE_TIMEOUT_SEC} с"
            await kill_process_tree(proc.pid)
        finally:
            for task in (reader, waiter):
                if not task.done():
                    task.cancel()
            with contextlib.suppress(TimeoutError, ProcessLookupError):
                await asyncio.wait_for(proc.wait(), timeout=10)
            with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                await asyncio.wait_for(stderr_task, timeout=3)
            if not stderr_task.done():
                stderr_task.cancel()
        state.return_code = proc.returncode
        if state.status in (RunStatus.KILLED, RunStatus.TIMEOUT):
            return
        if state.result is not None and not state.result.is_error:
            state.status = RunStatus.FINISHED
            return
        state.status = RunStatus.FAILED
        if state.result is None and proc.returncode not in (0, None):
            conflict = detect_background_conflict(state.stderr_tail)
            if conflict is not None:
                state.background_conflict = conflict
                state.error = conflict.message
                log.info("Run %s: chat is busy in background session %s", state.run_id, conflict.bg_id)
                return
        if state.result is not None:
            state.error = f"Claude завершился с ошибкой: {state.result.subtype} ({state.result.terminal_reason})"
        else:
            tail = "\n".join(list(state.stderr_tail)[-8:])
            state.error = f"Процесс завершился без результата (код {proc.returncode}).\n{tail}".strip()

    async def kill(self, run_id: str) -> bool:
        state = self._active.get(run_id)
        proc = self._procs.get(run_id)
        if state is None or proc is None:
            return False
        state.status = RunStatus.KILLED
        state.error = "Остановлено пользователем"
        state.activity = "🛑 Остановка процесса..."
        state.touch()
        await kill_process_tree(proc.pid)
        return True

    async def kill_all(self) -> None:
        for run_id in list(self._active):
            await self.kill(run_id)

    async def stop_background(self, bg_id: str, timeout: float = STOP_TIMEOUT_SEC) -> tuple[bool, str]:
        """`claude stop <bg_id>`: stops a background session on the PC, its conversation is kept.

        Returns (stopped, CLI output). 2.1.304 prints "stopped <id>" (exit 0, also when it was already
        stopped) or "No job matching '<id>'..." (exit 1).
        """
        if not BACKGROUND_ID_RE.match(bg_id):
            return False, f"Некорректный id фоновой сессии: {bg_id!r}"
        try:
            command = [*self.resolve_command(), "stop", bg_id]
        except ClaudeNotFoundError as exc:
            return False, str(exc)
        common = dict(
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self.build_env(),
        )
        try:
            if self._use_shell:  # bg_id is [A-Za-z0-9_-] only: nothing for cmd.exe to interpret
                proc = await asyncio.create_subprocess_shell(
                    subprocess.list2cmdline(command), creationflags=_CREATION_FLAGS, **common
                )
            else:
                proc = await asyncio.create_subprocess_exec(*command, creationflags=_CREATION_FLAGS, **common)
        except OSError as exc:
            return False, f"Не удалось запустить claude stop: {exc}"
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except TimeoutError:
            await kill_process_tree(proc.pid)
            return False, f"claude stop {bg_id} не ответил за {timeout:g} с"
        text = strip_ansi(out.decode("utf-8", errors="replace")).strip()
        log.info("claude stop %s -> %s: %s", bg_id, proc.returncode, one_line(text))
        return proc.returncode == 0, text
