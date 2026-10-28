"""Application settings loaded from `.env` via pydantic-settings."""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent

# Used only when skip-permissions is switched off in /mode. `mcp__*` covers every MCP server.
DEFAULT_ALLOWED_TOOLS = (
    "Read,Edit,MultiEdit,Write,Glob,Grep,LS,Bash,Skill,Task,"
    "WebFetch,WebSearch,NotebookEdit,mcp__*"
)
DEFAULT_WHISPER_PROMPT = (
    "Claude, Python, Figma, Git, Bash, MCP, API, Docker, pytest, рефакторинг, презентация, слайды, скилл"
)
_UNLIMITED_WORDS = frozenset({"", "0", "0.0", "none", "null", "off", "unlimited", "inf"})

DEFAULT_IGNORED_DIRS = (
    ".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build",
    ".idea", ".vs", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
    "$RECYCLE.BIN", "System Volume Information",
)


def _split_list(value: object) -> list[str]:
    """Split a `;`-separated env value (Windows paths contain `:` so `;` is used, like PATH)."""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(v).strip() for v in value if str(v).strip()]
    text = str(value).strip()
    if not text:
        return []
    return [part.strip().strip('"') for part in text.split(";") if part.strip()]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- Telegram ---
    BOT_TOKEN: SecretStr
    ADMIN_USER_ID: int

    # --- File system sandbox ---
    ALLOWED_DIRS: Annotated[list[Path], NoDecode] = Field(default_factory=list)
    IGNORED_DIRS: Annotated[list[str], NoDecode] = Field(default_factory=lambda: list(DEFAULT_IGNORED_DIRS))
    SHOW_HIDDEN_FILES: bool = False
    UPLOAD_SUBDIR: str = "_uploads"

    # --- Claude CLI ---
    CLAUDE_BINARY: str = "claude"
    CLAUDE_PROJECTS_DIR: Path = Field(default_factory=lambda: Path.home() / ".claude" / "projects")
    CLAUDE_ALLOWED_TOOLS: str = DEFAULT_ALLOWED_TOOLS
    # None (0 or empty in .env) = the flag is not passed to the CLI at all, i.e. no limit.
    CLAUDE_MAX_TURNS: int | None = Field(default=None, gt=0, le=10_000)
    CLAUDE_MAX_BUDGET_USD: float | None = Field(default=None, gt=0)
    CLAUDE_MODEL: str = ""
    CLAUDE_TIMEOUT_SEC: int = Field(default=3600, ge=30)
    # Full trust by default: the bot is already locked to ADMIN_USER_ID by AuthMiddleware.
    SKIP_PERMISSIONS_DEFAULT: bool = True
    MAX_CONCURRENT_RUNS: int = Field(default=2, ge=1, le=10)
    # Chat busy in a background session on the PC: ask = buttons 🌿 / 🛑, fork = rerun with --fork-session
    BG_SESSION_POLICY: Literal["ask", "fork"] = "ask"

    # --- UX / limits ---
    STATUS_UPDATE_INTERVAL: float = Field(default=3.0, ge=1.0)
    SESSIONS_PAGE_SIZE: int = Field(default=8, ge=3, le=20)
    PROJECTS_PAGE_SIZE: int = Field(default=8, ge=3, le=20)
    FILES_PAGE_SIZE: int = Field(default=14, ge=5, le=40)
    PREVIEW_LINES: int = Field(default=50, ge=5, le=200)
    MAX_SEND_FILE_MB: int = Field(default=50, ge=1, le=2000)
    MAX_DOWNLOAD_FILE_MB: int = Field(default=20, ge=1, le=2000)
    MAX_INLINE_CHUNKS: int = Field(default=3, ge=1, le=10)
    SEARCH_RESULTS_LIMIT: int = Field(default=20, ge=1, le=50)

    # --- Voice-to-Prompt (faster-whisper) ---
    VOICE_ENABLED: bool = True
    # tiny | base | small | medium | large-v3 | turbo ... (downloaded on first voice message)
    WHISPER_MODEL: str = "medium"
    WHISPER_DEVICE: Literal["auto", "cpu", "cuda"] = "auto"
    # Empty = float16 on CUDA, int8 on CPU
    WHISPER_COMPUTE_TYPE: str = ""
    # Empty = auto-detect the language
    WHISPER_LANGUAGE: str = "ru"
    # Empty = standard Hugging Face cache (%USERPROFILE%\.cache\huggingface)
    WHISPER_DOWNLOAD_DIR: Path | None = None
    WHISPER_INITIAL_PROMPT: str = DEFAULT_WHISPER_PROMPT
    WHISPER_BEAM_SIZE: int = Field(default=5, ge=1, le=10)
    VOICE_AUTO_RUN: bool = True
    VOICE_MAX_DURATION_SEC: int = Field(default=600, ge=5)

    # --- Watchdog ---
    WATCH_ON_START: bool = True
    WATCH_DEBOUNCE_SEC: float = Field(default=30.0, ge=5.0)
    WATCH_IGNORE_HEADLESS: bool = True

    # --- Self-restart (/restart) ---
    # How long the old process waits for the new one to start (start_bot.ps1 may run pip install first)
    RESTART_READY_TIMEOUT_SEC: int = Field(default=180, ge=10, le=1800)

    # --- Runtime ---
    DATA_DIR: Path = BASE_DIR / "data"
    LOG_DIR: Path = BASE_DIR / "logs"
    LOG_LEVEL: str = "INFO"

    @field_validator("ALLOWED_DIRS", mode="before")
    @classmethod
    def _parse_allowed_dirs(cls, value: object) -> list[Path]:
        return [Path(os.path.expandvars(os.path.expanduser(p))) for p in _split_list(value)]

    @field_validator("IGNORED_DIRS", mode="before")
    @classmethod
    def _parse_ignored(cls, value: object) -> list[str]:
        items = _split_list(value)
        return items or list(DEFAULT_IGNORED_DIRS)

    @field_validator("CLAUDE_MAX_TURNS", "CLAUDE_MAX_BUDGET_USD", mode="before")
    @classmethod
    def _optional_limit(cls, value: object) -> object:
        """0, empty string, `none`, `off`, `unlimited` -> None (no limit flag)."""
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip().strip('"')
            return None if text.lower() in _UNLIMITED_WORDS else text
        if isinstance(value, (int, float)) and value == 0:
            return None
        return value

    @field_validator("WHISPER_DOWNLOAD_DIR", mode="before")
    @classmethod
    def _optional_dir(cls, value: object) -> object:
        if value is None or (isinstance(value, str) and not value.strip().strip('"')):
            return None
        if isinstance(value, str):
            return Path(os.path.expandvars(os.path.expanduser(value.strip().strip('"'))))
        return value

    @field_validator("WHISPER_DEVICE", "BG_SESSION_POLICY", mode="before")
    @classmethod
    def _lower_device(cls, value: object) -> object:
        return value.strip().lower() if isinstance(value, str) else value

    @field_validator("CLAUDE_PROJECTS_DIR", "DATA_DIR", "LOG_DIR", mode="before")
    @classmethod
    def _expand_path(cls, value: object) -> object:
        if isinstance(value, str) and value.strip():
            return Path(os.path.expandvars(os.path.expanduser(value.strip().strip('"'))))
        return value

    @model_validator(mode="after")
    def _validate_dirs(self) -> "Settings":
        resolved: list[Path] = []
        for path in self.ALLOWED_DIRS:
            try:
                real = path.resolve(strict=True)
            except OSError as exc:
                raise ValueError(f"ALLOWED_DIRS: папка не найдена: {path} ({exc})") from exc
            if not real.is_dir():
                raise ValueError(f"ALLOWED_DIRS: это не папка: {path}")
            resolved.append(real)
        if not resolved:
            raise ValueError("ALLOWED_DIRS пуст: укажите хотя бы одну разрешенную папку (через ;)")
        self.ALLOWED_DIRS = resolved
        return self

    @property
    def ignored_dirs_lower(self) -> frozenset[str]:
        return frozenset(name.lower() for name in self.IGNORED_DIRS)

    @property
    def max_send_bytes(self) -> int:
        return self.MAX_SEND_FILE_MB * 1024 * 1024

    @property
    def max_download_bytes(self) -> int:
        return self.MAX_DOWNLOAD_FILE_MB * 1024 * 1024

    @property
    def state_file(self) -> Path:
        return self.DATA_DIR / "state.json"

    @property
    def restart_state_file(self) -> Path:
        return self.DATA_DIR / "restart_state.json"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
