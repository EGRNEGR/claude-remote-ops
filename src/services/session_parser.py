"""Reads Claude Code history in ~/.claude/projects and builds session reports.

Layout verified on Claude Code 2.1.304 (Windows):
  ~/.claude/projects/<encoded-cwd>/<session_id>.jsonl      - main transcript (one JSON object per line)
  ~/.claude/projects/<encoded-cwd>/<session_id>/subagents/ - sidechain transcripts (ignored here)
  ~/.claude/projects/<encoded-cwd>/memory/                 - auto-memory (ignored)
There is no sessions-index.json in this version, so sessions are discovered by scanning *.jsonl.

<encoded-cwd> replaces every char outside [A-Za-z0-9] with "-":
  C:\\Users\\alice\\.claude\\jobs\\x  ->  C--Users-alice--claude-jobs-x
The encoding is lossy (Cyrillic, dots, spaces all become "-"), so the real path is taken from the
"cwd" field of the transcript records and only falls back to a file-system guided decode.

Record types used (top level "type"):
  user          message.content: str (typed prompt) | list[text | tool_result]; isMeta marks injected text
  assistant     message.id + message.content: one content block per record (text | thinking | tool_use)
  cost-state    totalCostUSD (cumulative for the session, written when a CLI run ends)
  ai-title      aiTitle (auto-generated session title)
  last-prompt   lastPrompt
Lines starting with {"type":"api-request...} or {"type":"file-history...} are large blobs and skipped.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import aiofiles

from src.utils.text import (
    TG_SAFE_LIMIT,
    esc,
    fmt_cost,
    fmt_dt,
    human_duration,
    one_line,
    parse_iso,
    truncate,
    truncate_middle,
)
from src.utils.winpath import is_within, long_path, normcase, retry_on_lock

log = logging.getLogger(__name__)

SESSION_ID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ENCODE_RE = re.compile(r"[^A-Za-z0-9]")
_DRIVE_RE = re.compile(r"^([A-Za-z])--(.*)$")
_COMMAND_NAME_RE = re.compile(r"<command-name>\s*(.*?)\s*</command-name>", re.S)
_COMMAND_ARGS_RE = re.compile(r"<command-args>\s*(.*?)\s*</command-args>", re.S)
_BASH_INPUT_RE = re.compile(r"<bash-input>\s*(.*?)\s*</bash-input>", re.S)
_LEADING_TAG_RE = re.compile(r"^\s*<([A-Za-z][\w-]*)[\s>]")

_SKIP_LINE_PREFIXES = (
    b'{"type":"api-request',
    b'{"type":"file-history',
    b'{"type":"atis-latch',
)
_READ_CHUNK = 2 * 1024 * 1024
_HEAD_BYTES = 256 * 1024
_TAIL_BYTES = 512 * 1024
_SYSTEM_ORIGINS = frozenset({"task-notification", "peer"})
_HEADLESS_ENTRYPOINTS = frozenset({"sdk-cli", "sdk-ts", "sdk-py"})

FILE_TOOLS: dict[str, str] = {
    "Edit": "правка",
    "MultiEdit": "правки",
    "Write": "запись",
    "NotebookEdit": "ноутбук",
}
SHELL_TOOLS = frozenset({"Bash", "PowerShell"})


def encode_project_path(path: str) -> str:
    return _ENCODE_RE.sub("-", path)


def _dfs_decode(base: Path, rest: str, depth: int = 0) -> Path | None:
    if not rest:
        return base
    if depth > 40:
        return None
    try:
        with os.scandir(long_path(base)) as it:
            children = [e.name for e in it if e.is_dir(follow_symlinks=False)]
    except OSError:
        return None
    for name in sorted(children, key=len, reverse=True):
        encoded = encode_project_path(name)
        if rest == encoded:
            return base / name
        if rest.startswith(encoded + "-"):
            found = _dfs_decode(base / name, rest[len(encoded) + 1:], depth + 1)
            if found is not None:
                return found
    return None


def decode_project_dir(name: str) -> tuple[str, bool]:
    """Decode a project folder name. Returns (path, exact) where exact=True if verified on disk."""
    match = _DRIVE_RE.match(name)
    if not match:
        return name.replace("-", "\\"), False
    drive = Path(f"{match.group(1).upper()}:\\")
    rest = match.group(2)
    found = _dfs_decode(drive, rest)
    if found is not None:
        return str(found), True
    return str(drive) + rest.replace("-", "\\"), False


def clean_prompt(text: str) -> tuple[str | None, str | None]:
    """Classify user text. Returns (prompt, shell_command) - both None for injected/system text."""
    stripped = text.strip()
    if not stripped:
        return None, None
    if stripped.startswith("[Request interrupted"):
        return None, None
    if stripped.startswith(("Base directory for this skill", "Caveat:")):
        return None, None
    name = _COMMAND_NAME_RE.search(stripped)
    if name:
        args = _COMMAND_ARGS_RE.search(stripped)
        command = f"{name.group(1)} {args.group(1) if args else ''}".strip()
        return command, None
    bash = _BASH_INPUT_RE.search(stripped)
    if bash:
        return f"! {bash.group(1)}", bash.group(1)
    if _LEADING_TAG_RE.match(stripped):
        return None, None
    return stripped, None


@dataclass(slots=True)
class TokenUsage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    @property
    def total(self) -> int:
        return self.input + self.output + self.cache_read + self.cache_creation


@dataclass(slots=True)
class ProjectInfo:
    key: str
    dir: Path
    display_path: str
    exact: bool
    session_count: int
    last_mtime: float


@dataclass(slots=True)
class SessionMeta:
    session_id: str
    path: Path
    project_key: str
    mtime: float
    size: int
    title: str
    first_prompt: str
    last_prompt: str
    cwd: str | None
    entrypoint: str | None


@dataclass
class SessionReport:
    session_id: str
    path: Path
    project_key: str
    is_delta: bool = False
    cwd: str | None = None
    title: str | None = None
    git_branch: str | None = None
    version: str | None = None
    entrypoint: str | None = None
    prompts: list[str] = field(default_factory=list)
    files: dict[str, Counter[str]] = field(default_factory=dict)
    commands: list[tuple[str, str]] = field(default_factory=list)
    tool_counter: Counter[str] = field(default_factory=Counter)
    final_answer: str = ""
    assistant_messages: int = 0
    tool_calls: int = 0
    tool_errors: int = 0
    api_errors: int = 0
    interrupted: int = 0
    cost_usd: float | None = None
    cost_is_final: bool = True
    tokens: TokenUsage = field(default_factory=TokenUsage)
    models: set[str] = field(default_factory=set)
    started_at: datetime | None = None
    ended_at: datetime | None = None
    end_offset: int = 0

    @property
    def has_activity(self) -> bool:
        return bool(self.prompts or self.assistant_messages)

    @property
    def is_headless(self) -> bool:
        return (self.entrypoint or "") in _HEADLESS_ENTRYPOINTS

    @property
    def duration_sec(self) -> float | None:
        if self.started_at and self.ended_at:
            return (self.ended_at - self.started_at).total_seconds()
        return None

    def file_paths(self) -> list[Path]:
        base = Path(self.cwd) if self.cwd else None
        result: list[Path] = []
        for raw in self.files:
            path = Path(raw)
            if not path.is_absolute() and base is not None:
                path = base / path
            result.append(path)
        return result


class ReportBuilder:
    """Incrementally consumes transcript records (dicts) and accumulates a SessionReport."""

    def __init__(self, report: SessionReport) -> None:
        self.report = report
        self._seen_messages: set[str] = set()
        self._seen_blocks: set[tuple[str, int]] = set()
        self._usage: dict[str, TokenUsage] = {}
        self._last_text_mid: str | None = None
        self._final_parts: list[str] = []
        self._assistant_seq = 0
        self._cost_seq = -1

    def feed(self, rec: dict) -> None:
        if rec.get("isSidechain"):
            return
        rtype = rec.get("type")
        r = self.report
        ts = parse_iso(rec.get("timestamp")) if isinstance(rec.get("timestamp"), str) else None
        if ts is not None:
            if r.started_at is None or ts < r.started_at:
                r.started_at = ts
            if r.ended_at is None or ts > r.ended_at:
                r.ended_at = ts
        if rtype in ("user", "assistant", "system"):
            r.cwd = rec.get("cwd") or r.cwd
            r.git_branch = rec.get("gitBranch") or r.git_branch
            r.version = rec.get("version") or r.version
            r.entrypoint = rec.get("entrypoint") or r.entrypoint
        if rtype == "user":
            self._on_user(rec)
        elif rtype == "assistant":
            self._on_assistant(rec)
        elif rtype == "cost-state":
            cost = rec.get("totalCostUSD")
            if isinstance(cost, (int, float)):
                r.cost_usd = float(cost) if r.cost_usd is None else max(r.cost_usd, float(cost))
                self._cost_seq = self._assistant_seq
        elif rtype == "ai-title":
            title = rec.get("aiTitle")
            if isinstance(title, str) and title.strip():
                r.title = title.strip()

    def _on_user(self, rec: dict) -> None:
        r = self.report
        message = rec.get("message") or {}
        content = message.get("content")
        texts: list[str] = []
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_result":
                    if block.get("is_error"):
                        r.tool_errors += 1
                elif block.get("type") == "text" and isinstance(block.get("text"), str):
                    texts.append(block["text"])
        for text in texts:
            if text.strip().startswith("[Request interrupted"):
                r.interrupted += 1
        if rec.get("isMeta") or (rec.get("origin") or {}).get("kind") in _SYSTEM_ORIGINS:
            return
        for text in texts:
            prompt, shell = clean_prompt(text)
            if prompt:
                r.prompts.append(prompt)
            if shell:
                r.commands.append(("!", shell))

    def _on_assistant(self, rec: dict) -> None:
        r = self.report
        message = rec.get("message") or {}
        mid = str(message.get("id") or rec.get("uuid") or "")
        block_index = rec.get("apiBlockIndex")
        if isinstance(block_index, int):
            key = (mid, block_index)
            if key in self._seen_blocks:
                return
            self._seen_blocks.add(key)
        if rec.get("isApiErrorMessage"):
            r.api_errors += 1
        if mid not in self._seen_messages:
            self._seen_messages.add(mid)
            r.assistant_messages += 1
            self._assistant_seq += 1
        model = message.get("model")
        if isinstance(model, str) and model and not model.startswith("<"):
            r.models.add(model)
        usage = message.get("usage")
        if isinstance(usage, dict):
            current = self._usage.setdefault(mid, TokenUsage())
            current.input = max(current.input, int(usage.get("input_tokens") or 0))
            current.output = max(current.output, int(usage.get("output_tokens") or 0))
            current.cache_read = max(current.cache_read, int(usage.get("cache_read_input_tokens") or 0))
            current.cache_creation = max(
                current.cache_creation, int(usage.get("cache_creation_input_tokens") or 0)
            )
        content = message.get("content")
        if not isinstance(content, list):
            return
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    if mid != self._last_text_mid:
                        self._last_text_mid = mid
                        self._final_parts = []
                    self._final_parts.append(text.strip())
            elif btype == "tool_use":
                self._on_tool(str(block.get("name") or "?"), block.get("input") or {})

    def _on_tool(self, name: str, tool_input: dict) -> None:
        r = self.report
        r.tool_calls += 1
        r.tool_counter[name] += 1
        if not isinstance(tool_input, dict):
            return
        if name in FILE_TOOLS:
            path = tool_input.get("file_path") or tool_input.get("notebook_path")
            if isinstance(path, str) and path:
                r.files.setdefault(path, Counter())[name] += 1
        elif name in SHELL_TOOLS:
            command = tool_input.get("command")
            if isinstance(command, str) and command.strip():
                r.commands.append((name, command.strip()))

    def finish(self) -> SessionReport:
        r = self.report
        r.final_answer = "\n\n".join(self._final_parts)
        tokens = TokenUsage()
        for usage in self._usage.values():
            tokens.input += usage.input
            tokens.output += usage.output
            tokens.cache_read += usage.cache_read
            tokens.cache_creation += usage.cache_creation
        r.tokens = tokens
        r.cost_is_final = r.cost_usd is not None and self._cost_seq >= self._assistant_seq
        return r


def _decode_lines(data: bytes) -> list[dict]:
    records: list[dict] = []
    for raw in data.split(b"\n"):
        raw = raw.strip()
        if not raw or raw.startswith(_SKIP_LINE_PREFIXES):
            continue
        try:
            obj = json.loads(raw)
        except UnicodeDecodeError:
            try:
                obj = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
        except ValueError:
            continue
        if isinstance(obj, dict):
            records.append(obj)
    return records


async def iter_record_batches(path: Path, start: int = 0) -> AsyncIterator[tuple[list[dict], int]]:
    """Read a JSONL file from `start` with aiofiles, decoding complete lines in a worker thread.

    Yields (records, offset_after_last_complete_line). A trailing line without newline is only
    consumed if it is valid JSON (the CLI may still be writing it).
    """

    async def _open():  # type: ignore[no-untyped-def]
        return await aiofiles.open(long_path(path), "rb")

    fh = await retry_on_lock(_open)
    try:
        await fh.seek(start)
        offset = start
        buffer = b""
        while True:
            chunk = await fh.read(_READ_CHUNK)
            if not chunk:
                break
            buffer += chunk
            cut = buffer.rfind(b"\n")
            if cut < 0:
                continue
            complete, buffer = buffer[: cut + 1], buffer[cut + 1:]
            offset += len(complete)
            records = await asyncio.to_thread(_decode_lines, complete)
            yield records, offset
        if buffer.strip():
            tail = await asyncio.to_thread(_decode_lines, buffer)
            if tail:
                offset += len(buffer)
                yield tail, offset
    finally:
        await fh.close()


async def _read_window(path: Path, start: int, size: int) -> bytes:
    async def _read() -> bytes:
        async with aiofiles.open(long_path(path), "rb") as fh:
            await fh.seek(start)
            return await fh.read(size)

    return await retry_on_lock(_read)


class SessionInspector:
    """Discovers projects/sessions and builds reports. All disk I/O is async or off-loaded."""

    def __init__(self, projects_dir: Path) -> None:
        self.projects_dir = projects_dir
        self._meta_cache: dict[str, tuple[tuple[float, int], SessionMeta]] = {}
        self._decode_cache: dict[str, tuple[str, bool]] = {}

    # ---------- discovery ----------

    def _project_dirs(self) -> list[Path]:
        try:
            with os.scandir(long_path(self.projects_dir)) as it:
                return [self.projects_dir / e.name for e in it if e.is_dir(follow_symlinks=False)]
        except OSError as exc:
            log.warning("Cannot read %s: %s", self.projects_dir, exc)
            return []

    @staticmethod
    def _session_files(project_dir: Path) -> list[tuple[Path, float, int]]:
        result: list[tuple[Path, float, int]] = []
        try:
            with os.scandir(long_path(project_dir)) as it:
                for entry in it:
                    if not entry.name.endswith(".jsonl") or not entry.is_file(follow_symlinks=False):
                        continue
                    if not SESSION_ID_RE.match(entry.name[:-6]):
                        continue
                    try:
                        st = entry.stat()
                    except OSError:
                        continue
                    result.append((project_dir / entry.name, st.st_mtime, st.st_size))
        except OSError:
            return []
        result.sort(key=lambda item: item[1], reverse=True)
        return result

    def project_dir(self, key: str) -> Path | None:
        if not key or any(ch in key for ch in '\\/:*?"<>|') or key in (".", ".."):
            return None
        path = self.projects_dir / key
        return path if os.path.isdir(long_path(path)) else None

    async def list_projects(self) -> list[ProjectInfo]:
        dirs = await asyncio.to_thread(self._project_dirs)
        projects: list[ProjectInfo] = []
        for project_dir in dirs:
            files = await asyncio.to_thread(self._session_files, project_dir)
            if not files:
                continue
            newest = files[0]
            meta = await self._meta_for(project_dir.name, *newest)
            if meta.cwd:
                display, exact = meta.cwd, True
            else:
                display, exact = await self._decode_cached(project_dir.name)
            projects.append(
                ProjectInfo(
                    key=project_dir.name,
                    dir=project_dir,
                    display_path=display,
                    exact=exact,
                    session_count=len(files),
                    last_mtime=newest[1],
                )
            )
        projects.sort(key=lambda p: p.last_mtime, reverse=True)
        return projects

    async def _decode_cached(self, key: str) -> tuple[str, bool]:
        if key not in self._decode_cache:
            self._decode_cache[key] = await asyncio.to_thread(decode_project_dir, key)
        return self._decode_cache[key]

    async def project_path(self, key: str) -> str | None:
        """Real working directory of a project (from transcripts, else decoded name)."""
        project_dir = self.project_dir(key)
        if project_dir is None:
            return None
        files = await asyncio.to_thread(self._session_files, project_dir)
        for path, mtime, size in files[:3]:
            meta = await self._meta_for(key, path, mtime, size)
            if meta.cwd:
                return meta.cwd
        decoded, _ = await self._decode_cached(key)
        return decoded

    async def list_sessions(self, project_key: str) -> list[SessionMeta]:
        project_dir = self.project_dir(project_key)
        if project_dir is None:
            return []
        files = await asyncio.to_thread(self._session_files, project_dir)
        return [await self._meta_for(project_key, path, mtime, size) for path, mtime, size in files]

    async def find_session(self, session_id: str) -> SessionMeta | None:
        if not SESSION_ID_RE.match(session_id or ""):
            return None
        for project_dir in await asyncio.to_thread(self._project_dirs):
            path = project_dir / f"{session_id}.jsonl"
            try:
                st = await asyncio.to_thread(os.stat, long_path(path))
            except OSError:
                continue
            return await self._meta_for(project_dir.name, path, st.st_mtime, st.st_size)
        return None

    async def _meta_for(self, project_key: str, path: Path, mtime: float, size: int) -> SessionMeta:
        cache_key = normcase(path)
        cached = self._meta_cache.get(cache_key)
        if cached and cached[0] == (mtime, size):
            return cached[1]
        meta = await self._read_meta(project_key, path, mtime, size)
        self._meta_cache[cache_key] = ((mtime, size), meta)
        return meta

    async def _read_meta(self, project_key: str, path: Path, mtime: float, size: int) -> SessionMeta:
        cwd: str | None = None
        entrypoint: str | None = None
        first_prompt = ""
        title = ""
        last_prompt = ""
        tail_start = max(0, size - _TAIL_BYTES)
        try:
            head = await _read_window(path, 0, _HEAD_BYTES)
            tail = head if tail_start == 0 else await _read_window(path, tail_start, _TAIL_BYTES)
        except OSError as exc:
            log.warning("Cannot read session %s: %s", path, exc)
            head = tail = b""
            tail_start = 0
        head_lines = head.split(b"\n")
        if len(head) == _HEAD_BYTES:
            head_lines = head_lines[:-1]
        head_records = await asyncio.to_thread(_decode_lines, b"\n".join(head_lines))
        for rec in head_records:
            if rec.get("isSidechain"):
                continue
            if cwd is None and isinstance(rec.get("cwd"), str):
                cwd = rec["cwd"]
            if entrypoint is None and isinstance(rec.get("entrypoint"), str):
                entrypoint = rec["entrypoint"]
            if not first_prompt and rec.get("type") == "user" and not rec.get("isMeta"):
                content = (rec.get("message") or {}).get("content")
                texts = [content] if isinstance(content, str) else [
                    b.get("text", "") for b in content or [] if isinstance(b, dict) and b.get("type") == "text"
                ]
                for text in texts:
                    prompt, _ = clean_prompt(text)
                    if prompt:
                        first_prompt = prompt
                        break
            if not title and rec.get("type") == "ai-title" and isinstance(rec.get("aiTitle"), str):
                title = rec["aiTitle"]
        tail_lines = tail.split(b"\n")
        if tail_start > 0:
            tail_lines = tail_lines[1:]
        tail_records = await asyncio.to_thread(_decode_lines, b"\n".join(tail_lines))
        for rec in tail_records:
            rtype = rec.get("type")
            if rtype == "ai-title" and isinstance(rec.get("aiTitle"), str):
                title = rec["aiTitle"]
            elif rtype == "last-prompt" and isinstance(rec.get("lastPrompt"), str):
                last_prompt = rec["lastPrompt"]
            if cwd is None and isinstance(rec.get("cwd"), str) and not rec.get("isSidechain"):
                cwd = rec["cwd"]
        session_id = path.stem
        if not title:
            title = one_line(first_prompt or last_prompt) or "(без названия)"
        return SessionMeta(
            session_id=session_id,
            path=path,
            project_key=project_key,
            mtime=mtime,
            size=size,
            title=title,
            first_prompt=first_prompt,
            last_prompt=last_prompt,
            cwd=cwd,
            entrypoint=entrypoint,
        )

    # ---------- reports ----------

    async def build_report(self, meta: SessionMeta, start_offset: int = 0) -> SessionReport:
        report = SessionReport(
            session_id=meta.session_id,
            path=meta.path,
            project_key=meta.project_key,
            is_delta=start_offset > 0,
            cwd=meta.cwd,
        )
        return await self.build_report_from_path(meta.path, meta.project_key, start_offset, report)

    async def build_report_from_path(
        self,
        path: Path,
        project_key: str,
        start_offset: int = 0,
        report: SessionReport | None = None,
    ) -> SessionReport:
        if report is None:
            report = SessionReport(
                session_id=path.stem, path=path, project_key=project_key, is_delta=start_offset > 0
            )
        builder = ReportBuilder(report)
        report.end_offset = start_offset
        async for records, offset in iter_record_batches(path, start_offset):
            for rec in records:
                builder.feed(rec)
            report.end_offset = offset
        if report.title is None and start_offset > 0:
            meta = await self.find_session(report.session_id)
            if meta is not None:
                report.title = meta.title
        return builder.finish()


# ---------- rendering ----------

def _rel_path(raw: str, cwd: str | None) -> str:
    path = Path(raw)
    if cwd and path.is_absolute() and is_within(path, Path(cwd)):
        try:
            return os.path.relpath(path, cwd)
        except ValueError:
            return raw
    return raw


def _ops_label(ops: Counter[str]) -> str:
    return ", ".join(f"{FILE_TOOLS.get(name, name)}×{count}" for name, count in ops.items())


def _compose_html(
    r: SessionReport,
    header: str,
    answer_budget: int,
    prompt_budget: int,
    max_files: int,
    max_cmds: int,
) -> str:
    lines: list[str] = [f"<b>{esc(header)}</b>"]
    if r.title:
        lines.append(f"🏷 <b>{esc(truncate(r.title, 120))}</b>")
    place = f"📁 <code>{esc(truncate_middle(r.cwd or r.project_key, 90))}</code>"
    if r.git_branch and r.git_branch != "HEAD":
        place += f" · 🌿 {esc(truncate(r.git_branch, 40))}"
    lines.append(place)
    lines.append(f"🆔 <code>{esc(r.session_id)}</code>")
    if r.started_at:
        period = f"🕒 {fmt_dt(r.started_at)}"
        if r.ended_at and r.ended_at != r.started_at:
            same_day = r.started_at.astimezone().date() == r.ended_at.astimezone().date()
            period += f" → {fmt_dt(r.ended_at, with_date=not same_day)}"
            if r.duration_sec is not None:
                period += f" ({human_duration(r.duration_sec)})"
        lines.append(period)

    lines.append("")
    if r.prompts:
        label = "🙋 <b>Запрос (продолжение сессии):</b>" if r.is_delta else "🙋 <b>Запрос пользователя:</b>"
        lines.append(label)
        lines.append(f"<blockquote expandable>{esc(truncate(r.prompts[0], prompt_budget))}</blockquote>")
        if len(r.prompts) > 1:
            lines.append(f"↪️ <b>Последний запрос</b> (всего запросов: {len(r.prompts)}):")
            lines.append(
                f"<blockquote expandable>{esc(truncate(r.prompts[-1], max(120, prompt_budget // 2)))}</blockquote>"
            )
    else:
        lines.append("🙋 Новых запросов пользователя нет")

    if r.files:
        lines.append("")
        lines.append(f"📝 <b>Затронутые файлы ({len(r.files)}):</b>")
        for raw, ops in list(r.files.items())[:max_files]:
            rel = truncate_middle(_rel_path(raw, r.cwd), 80)
            lines.append(f"• <code>{esc(rel)}</code> <i>({esc(_ops_label(ops))})</i>")
        if len(r.files) > max_files:
            lines.append(f"<i>… и еще {len(r.files) - max_files}</i>")

    if r.commands:
        lines.append("")
        lines.append(f"⚙️ <b>Команды терминала ({len(r.commands)}):</b>")
        shown = r.commands[-max_cmds:]
        if len(r.commands) > max_cmds:
            lines.append(f"<i>… ранее еще {len(r.commands) - max_cmds}</i>")
        for tool, command in shown:
            prefix = "PS> " if tool == "PowerShell" else ("! " if tool == "!" else "$ ")
            lines.append(f"• <code>{esc(prefix + truncate(one_line(command), 110))}</code>")

    lines.append("")
    if r.final_answer:
        lines.append("💬 <b>Итоговый ответ ассистента:</b>")
        lines.append(f"<blockquote expandable>{esc(truncate(r.final_answer, answer_budget))}</blockquote>")
    else:
        lines.append("💬 Текстового ответа ассистента нет")

    stats = [f"👣 Шагов: {r.assistant_messages}", f"🔧 инструментов: {r.tool_calls}"]
    if r.tool_errors:
        stats.append(f"❗ ошибок: {r.tool_errors}")
    if r.interrupted:
        stats.append(f"⛔ прерываний: {r.interrupted}")
    lines.append(" · ".join(stats))
    if r.cost_usd is not None:
        cost_line = f"💵 Стоимость сессии: <b>{fmt_cost(r.cost_usd)}</b>"
        if not r.cost_is_final:
            cost_line += " <i>(на момент последнего сохранения, сессия продолжается)</i>"
    else:
        cost_line = "💵 Стоимость: <i>CLI еще не записал cost-state (сессия активна)</i>"
    lines.append(cost_line)
    t = r.tokens
    if t.total:
        lines.append(
            f"🔤 Токены: вход {t.input:,} · выход {t.output:,} · кэш {t.cache_read + t.cache_creation:,}".replace(",", " ")
        )
    if r.models:
        lines.append(f"🧠 {esc(', '.join(sorted(r.models)))}")
    return "\n".join(lines)


def render_report_html(r: SessionReport, header: str = "📊 Отчет по сессии", limit: int = TG_SAFE_LIMIT) -> str:
    """Telegram HTML report that always fits into one message."""
    answer_budget, prompt_budget, max_files, max_cmds = 1400, 600, 15, 10
    text = ""
    for _ in range(8):
        text = _compose_html(r, header, answer_budget, prompt_budget, max_files, max_cmds)
        if len(text) <= limit:
            return text
        answer_budget = max(150, int(answer_budget * 0.6))
        prompt_budget = max(120, int(prompt_budget * 0.7))
        max_files = max(3, max_files - 4)
        max_cmds = max(3, max_cmds - 3)
    return _compose_html(r, header, 100, 100, 2, 2)


def render_report_markdown(r: SessionReport) -> str:
    """Full, untruncated report for download as .md."""
    out: list[str] = [f"# Отчет по сессии Claude Code: {r.title or r.session_id}", ""]
    out.append(f"- Session ID: `{r.session_id}`")
    out.append(f"- Папка: `{r.cwd or r.project_key}`")
    if r.git_branch:
        out.append(f"- Git-ветка: `{r.git_branch}`")
    if r.started_at:
        out.append(f"- Период: {fmt_dt(r.started_at)} - {fmt_dt(r.ended_at)}")
    out.append(f"- Шагов (ответов модели): {r.assistant_messages}; вызовов инструментов: {r.tool_calls}; ошибок: {r.tool_errors}")
    out.append(f"- Стоимость: {fmt_cost(r.cost_usd)}{'' if r.cost_is_final else ' (промежуточная)'}")
    t = r.tokens
    out.append(f"- Токены: вход {t.input}, выход {t.output}, кэш-чтение {t.cache_read}, кэш-запись {t.cache_creation}")
    if r.tool_counter:
        out.append("- Инструменты: " + ", ".join(f"{k}×{v}" for k, v in r.tool_counter.most_common()))
    out.append("")
    out.append(f"## Запросы пользователя ({len(r.prompts)})")
    for index, prompt in enumerate(r.prompts, 1):
        out.append(f"\n### {index}.\n\n{prompt}")
    out.append(f"\n## Затронутые файлы ({len(r.files)})\n")
    for raw, ops in r.files.items():
        out.append(f"- `{raw}` ({_ops_label(ops)})")
    out.append(f"\n## Команды терминала ({len(r.commands)})\n")
    for tool, command in r.commands:
        lang = "powershell" if tool == "PowerShell" else "bash"
        out.append(f"```{lang}\n{command}\n```")
    out.append("\n## Итоговый ответ ассистента\n")
    out.append(r.final_answer or "_нет_")
    return "\n".join(out) + "\n"
