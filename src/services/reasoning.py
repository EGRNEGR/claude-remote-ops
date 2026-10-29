"""Model and reasoning (thinking effort) choice for headless `claude -p` runs.

Measured on Claude Code 2.1.304 (`claude.exe --help`, the binary and real `-p` runs):
  * `--model <alias|full name>`: aliases `sonnet` / `opus` / `haiku` / `fable` resolve to the latest model.
  * `--effort <low|medium|high|xhigh|max>` is a native flag. `CLAUDE_CODE_EFFORT_LEVEL` overrides it for
    the session, so both are set to the same value. A level the model does not support (e.g. `max`) is
    silently downgraded by the CLI.
  * `MAX_THINKING_TOKENS`: `0` disables thinking; `> 0` is a fixed budget (clamped to >= 1024) that only
    models without adaptive thinking use (e.g. claude-haiku-4-5, claude-sonnet-4-5). Adaptive models
    (Opus/Sonnet 4.6+, the 5.x family) ignore the budget and follow the effort level instead.
  * `CLAUDE_CODE_DISABLE_THINKING=1` drops thinking from the request entirely.
  * The keyword `ultrathink` in a prompt adds a "reason as thoroughly as the task warrants" reminder.
Setting the flag and the env vars together makes the choice work on every model in headless mode.
"""

from __future__ import annotations

import re
from enum import StrEnum

MODEL_DEFAULT = "default"
MODEL_PRESETS: tuple[str, ...] = ("sonnet", "opus", "haiku")
MODEL_TITLES: dict[str, str] = {
    "sonnet": "⚡ Sonnet",
    "opus": "🧠 Opus",
    "haiku": "🪶 Haiku",
    MODEL_DEFAULT: "⚙️ Авто",
}
# Known aliases are stored lower-case; a full model name (claude-sonnet-5-5, claude-opus-5-5[1m],
# us.anthropic.claude-...) is kept as typed. No leading "-" so it can never be read as a CLI flag.
_MODEL_ALIASES = frozenset({*MODEL_PRESETS, "fable", "opusplan", "best"})
_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,99}$")
_DEFAULT_WORDS = frozenset({"", "default", "auto", "авто", "none", "по-умолчанию", "сброс", "reset"})

ULTRATHINK = "ultrathink"
_ULTRATHINK_RE = re.compile(r"\bultrathink\b", re.IGNORECASE)

# Env vars this module owns: with an explicit level they are always set or removed, never inherited.
EFFORT_ENV = "CLAUDE_CODE_EFFORT_LEVEL"
THINKING_TOKENS_ENV = "MAX_THINKING_TOKENS"
DISABLE_THINKING_ENV = "CLAUDE_CODE_DISABLE_THINKING"


class ReasoningEffort(StrEnum):
    DEFAULT = "default"  # CLI defaults: no flag, env untouched
    OFF = "off"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    XHIGH = "xhigh"
    MAX = "max"


EFFORT_BUTTONS: tuple[ReasoningEffort, ...] = (
    ReasoningEffort.OFF, ReasoningEffort.LOW, ReasoningEffort.MEDIUM,
    ReasoningEffort.HIGH, ReasoningEffort.MAX, ReasoningEffort.DEFAULT,
)
EFFORT_TITLES: dict[ReasoningEffort, str] = {
    ReasoningEffort.OFF: "🚫 Выкл",
    ReasoningEffort.LOW: "🟢 Low",
    ReasoningEffort.MEDIUM: "🟡 Medium",
    ReasoningEffort.HIGH: "🟠 High",
    ReasoningEffort.XHIGH: "🔴 XHigh",
    ReasoningEffort.MAX: "🔥 Max",
    ReasoningEffort.DEFAULT: "⚙️ Авто",
}
# Fixed thinking budget for models without adaptive thinking (0 = thinking off).
THINKING_BUDGETS: dict[ReasoningEffort, int] = {
    ReasoningEffort.OFF: 0,
    ReasoningEffort.LOW: 4096,
    ReasoningEffort.MEDIUM: 10_000,
    ReasoningEffort.HIGH: 20_000,
    ReasoningEffort.XHIGH: 24_000,
    ReasoningEffort.MAX: 32_000,
}
_EFFORT_ALIASES: dict[str, ReasoningEffort] = {
    **{word: ReasoningEffort.DEFAULT for word in _DEFAULT_WORDS if word},
    "off": ReasoningEffort.OFF, "выкл": ReasoningEffort.OFF, "нет": ReasoningEffort.OFF,
    "0": ReasoningEffort.OFF, "disabled": ReasoningEffort.OFF, "no": ReasoningEffort.OFF,
    "min": ReasoningEffort.LOW, "низкий": ReasoningEffort.LOW,
    "mid": ReasoningEffort.MEDIUM, "средний": ReasoningEffort.MEDIUM,
    "высокий": ReasoningEffort.HIGH,
    "x-high": ReasoningEffort.XHIGH, "extra": ReasoningEffort.XHIGH,
    "максимум": ReasoningEffort.MAX, "ultrathink": ReasoningEffort.MAX,
}


def normalize_model(value: object) -> str:
    """`""` / auto / default -> "default"; aliases lower-cased; anything unsafe -> ValueError."""
    text = str(value or "").strip().strip('"').strip()
    if text.lower() in _DEFAULT_WORDS:
        return MODEL_DEFAULT
    if text.lower() in _MODEL_ALIASES:
        return text.lower()
    if not _MODEL_RE.match(text):
        raise ValueError(f"Недопустимое имя модели: {text!r}")
    return text


def parse_effort(value: object) -> ReasoningEffort:
    if isinstance(value, ReasoningEffort):
        return value
    text = str(value or "").strip().strip('"').strip().lower() or ReasoningEffort.DEFAULT.value
    if text in ReasoningEffort._value2member_map_:
        return ReasoningEffort(text)
    if text in _EFFORT_ALIASES:
        return _EFFORT_ALIASES[text]
    allowed = ", ".join(e.value for e in ReasoningEffort)
    raise ValueError(f"Неизвестный уровень ризонинга: {text!r} (допустимо: {allowed})")


def model_arg(model: str) -> str | None:
    """Value for --model, None = flag omitted (CLI default model)."""
    return None if model == MODEL_DEFAULT else model


def effort_args(effort: ReasoningEffort) -> list[str]:
    if effort == ReasoningEffort.DEFAULT:
        return []
    # "off" still lowers the effort so a model that cannot disable thinking spends as little as possible.
    level = ReasoningEffort.LOW if effort == ReasoningEffort.OFF else effort
    return ["--effort", level.value]


def effort_env(effort: ReasoningEffort) -> dict[str, str | None]:
    """Env overrides for the CLI process; None = remove an inherited value."""
    if effort == ReasoningEffort.DEFAULT:
        return {}
    level = ReasoningEffort.LOW if effort == ReasoningEffort.OFF else effort
    return {
        EFFORT_ENV: level.value,
        THINKING_TOKENS_ENV: str(THINKING_BUDGETS[effort]),
        DISABLE_THINKING_ENV: "1" if effort == ReasoningEffort.OFF else None,
    }


def prompt_for_effort(prompt: str, effort: ReasoningEffort) -> str:
    """`max` appends the `ultrathink` keyword (once) to the text sent through stdin."""
    if effort != ReasoningEffort.MAX or _ULTRATHINK_RE.search(prompt):
        return prompt
    return f"{prompt.rstrip()}\n\n{ULTRATHINK}"


def model_label(model: str | None, resolved: str | None = None) -> str:
    """`sonnet`, `авто (claude-opus-5-5)`, `sonnet (claude-sonnet-5-5)`."""
    name = "авто" if not model or model == MODEL_DEFAULT else model
    if resolved and resolved != name:
        return f"{name} ({resolved})"
    return name


def effort_label(effort: ReasoningEffort) -> str:
    return "авто" if effort == ReasoningEffort.DEFAULT else effort.value


def describe_choice(model: str | None, effort: ReasoningEffort, resolved: str | None = None) -> str:
    """Plain text (escape before HTML): `🧠 Модель: sonnet · 💭 Ризонинг: low`."""
    return f"🧠 Модель: {model_label(model, resolved)} · 💭 Ризонинг: {effort_label(effort)}"


def describe_cli(model: str, effort: ReasoningEffort) -> str:
    """What the next run gets, e.g. `--model sonnet --effort low · MAX_THINKING_TOKENS=4096`."""
    flags: list[str] = []
    arg = model_arg(model)
    if arg:
        flags += ["--model", arg]
    flags += effort_args(effort)
    parts = [" ".join(flags) if flags else "без --model / --effort (настройки CLI)"]
    env = {k: v for k, v in effort_env(effort).items() if v is not None}
    if THINKING_TOKENS_ENV in env:
        parts.append(f"{THINKING_TOKENS_ENV}={env[THINKING_TOKENS_ENV]}")
    if DISABLE_THINKING_ENV in env:
        parts.append(f"{DISABLE_THINKING_ENV}=1")
    if effort == ReasoningEffort.MAX:
        parts.append(f"+ {ULTRATHINK}")
    return " · ".join(parts)
