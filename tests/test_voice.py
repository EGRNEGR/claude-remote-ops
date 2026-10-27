"""Voice-to-Prompt handler tests: real Dispatcher + fake CLI, faster-whisper replaced by a fake."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from conftest import SESSION_ID
from tg_harness import AUDIO_BYTES, STRANGER

from src.keyboards.inline import FileCb, VoiceCb
from src.services.state import RunMode
from src.services.transcriber import Transcript, TranscriberError, TranscriberUnavailable
from src.utils.winpath import IS_WINDOWS

pytestmark = pytest.mark.skipif(not IS_WINDOWS, reason="uses the Windows cmd shim fake CLI")

SPOKEN = "Claude, запусти pytest и сделай рефакторинг модуля API"


class FakeTranscriber:
    model_name = "medium"

    def __init__(self, text: str = SPOKEN, error: Exception | None = None) -> None:
        self.text = text
        self.error = error
        self.loaded = False
        self.paths: list[Path] = []
        self.payloads: list[bytes] = []

    def describe(self) -> str:
        return "fake"

    async def transcribe(self, path: Path) -> Transcript:
        self.paths.append(path)
        self.payloads.append(path.read_bytes())  # the downloaded file exists while transcribing
        self.loaded = True
        if self.error is not None:
            raise self.error
        return Transcript(text=self.text, language="ru", language_probability=0.99,
                          duration=4.0, elapsed=0.4, device="cpu int8")


def _answers(calls: list) -> list[SendMessage]:
    """Claude answers are sent as plain text (parse_mode=None)."""
    return [c for c in calls if isinstance(c, SendMessage) and c.parse_mode is None]


async def _select_session(h, allowed: Path) -> None:
    await h.ctx.store.load()
    await h.ctx.store.update(
        active_cwd=str(allowed), active_session_id=SESSION_ID,
        active_session_cwd=str(allowed), run_mode=RunMode.RESUME,
    )


def test_voice_auto_run_uses_current_session(harness_factory, tmp_path: Path) -> None:
    h = harness_factory()
    fake = FakeTranscriber()
    h.ctx.transcriber = fake
    allowed = (tmp_path / "allowed").resolve()

    async def scenario() -> None:
        await _select_session(h, allowed)
        await h.voice()
        await h.drain()
        calls = h.session.take()
        sent = [c for c in calls if isinstance(c, SendMessage)]
        assert sent[0].text.startswith("🎙 Распознаю голосовое...") and "загружаю модель Whisper" in sent[0].text
        assert sent[0].reply_parameters is not None  # reply to the voice message
        transcript_edit = next(c for c in calls if isinstance(c, EditMessageText) and "Распознанный запрос" in c.text)
        assert SPOKEN in transcript_edit.text and "cpu int8" in transcript_edit.text
        assert transcript_edit.message_id == 1000  # the status message itself is edited
        answer = _answers(calls)[0]
        # Same pipeline as a typed prompt: current chat is resumed with the transcribed text via stdin.
        assert f"echo: {SPOKEN}" in answer.text and f"--resume {SESSION_ID}" in answer.text
        assert "--dangerously-skip-permissions" in answer.text

    asyncio.run(scenario())
    assert fake.payloads == [AUDIO_BYTES]
    assert fake.paths[0].suffix == ".ogg" and not fake.paths[0].exists(), "temp .ogg must be deleted"


def test_voice_confirm_buttons(harness_factory, tmp_path: Path) -> None:
    h = harness_factory(VOICE_AUTO_RUN=False)
    h.ctx.transcriber = FakeTranscriber()
    allowed = (tmp_path / "allowed").resolve()

    async def scenario() -> None:
        await _select_session(h, allowed)
        await h.voice()
        await h.drain()
        calls = h.session.take()
        edit = next(c for c in calls if isinstance(c, EditMessageText) and "Распознанный запрос" in c.text)
        labels = [b.text for row in edit.reply_markup.inline_keyboard for b in row]
        assert labels == ["▶️ Запустить", "❌ Отмена"] and not _answers(calls)

        await h.callback(VoiceCb(action="run").pack(), message_id=edit.message_id)
        await h.drain()
        calls = h.session.take()
        assert f"echo: {SPOKEN}" in _answers(calls)[0].text

        # Second voice -> cancel: nothing is launched.
        await h.voice()
        edit2 = next(c for c in h.session.take() if isinstance(c, EditMessageText) and "Распознанный" in c.text)
        await h.callback(VoiceCb(action="cancel").pack(), message_id=edit2.message_id)
        await h.drain()
        calls = h.session.take()
        assert any(isinstance(c, EditMessageText) and "❌ Отменено" in c.text for c in calls)
        assert not _answers(calls)

        # Pressing a button of an already handled transcript -> alert, no run.
        await h.callback(VoiceCb(action="run").pack(), message_id=edit2.message_id)
        calls = h.session.take()
        alert = next(c for c in calls if isinstance(c, AnswerCallbackQuery))
        assert alert.show_alert and "устарел" in alert.text and not _answers(calls)

    asyncio.run(scenario())


def test_audio_file_keeps_extension(harness_factory, tmp_path: Path) -> None:
    h = harness_factory()
    fake = FakeTranscriber()
    h.ctx.transcriber = fake

    async def scenario() -> None:
        await _select_session(h, (tmp_path / "allowed").resolve())
        await h.audio("заметка.m4a")
        await h.drain()

    asyncio.run(scenario())
    assert fake.paths[0].suffix == ".m4a" and not fake.paths[0].exists()


def test_voice_becomes_answer_to_file_question(harness_factory, tmp_path: Path) -> None:
    h = harness_factory()
    h.ctx.transcriber = FakeTranscriber("объясни, что делает этот файл")
    allowed = (tmp_path / "allowed").resolve()
    target = allowed / "src" / "main.py"

    async def scenario() -> None:
        await _select_session(h, allowed)
        await h.callback(FileCb(action="ask", token=h.ctx.token(target)).pack())
        h.session.take()
        await h.voice()
        await h.drain()
        answer = _answers(h.session.take())[0]
        assert "Работаем с файлом `src\\main.py`" in answer.text
        assert "объясни, что делает этот файл" in answer.text

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TranscriberError("битый файл"), "❌ Не удалось распознать голосовое: битый файл"),
        (TranscriberUnavailable("faster-whisper не установлен"), "⚠️ Распознавание речи недоступно"),
    ],
)
def test_voice_errors_cleanup(harness_factory, tmp_path: Path, error: Exception, expected: str) -> None:
    h = harness_factory()
    fake = FakeTranscriber(error=error)
    h.ctx.transcriber = fake

    async def scenario() -> None:
        await _select_session(h, (tmp_path / "allowed").resolve())
        await h.voice()
        await h.drain()
        calls = h.session.take()
        assert any(isinstance(c, EditMessageText) and expected in c.text for c in calls)
        assert not _answers(calls)

    asyncio.run(scenario())
    assert not fake.paths[0].exists(), "temp file must be deleted even on errors"


def test_voice_guards(harness_factory, tmp_path: Path) -> None:
    async def scenario() -> None:
        # Silence -> no run.
        h = harness_factory()
        h.ctx.transcriber = FakeTranscriber(text="")
        await _select_session(h, (tmp_path / "allowed").resolve())
        await h.voice()
        calls = h.session.take()
        assert any(isinstance(c, EditMessageText) and "Речь не распознана" in c.text for c in calls)
        assert not _answers(calls)

        # Too long, too big, stranger, disabled: the transcriber is never called.
        fake = FakeTranscriber()
        h.ctx.transcriber = fake
        await h.voice(duration=10_000)
        assert "Слишком длинное аудио" in h.session.texts()[0]
        h.session.take()
        await h.voice(file_size=30 * 1024 * 1024)
        assert "Bot API позволяет скачать максимум" in h.session.texts()[0]
        h.session.take()
        await h.voice(uid=STRANGER)
        assert h.session.take() == []
        assert fake.paths == []

        off = harness_factory(VOICE_ENABLED=False)
        off.ctx.transcriber = fake
        await off.ctx.store.load()
        await off.voice()
        assert "VOICE_ENABLED=false" in off.session.texts()[0] and fake.paths == []

    asyncio.run(scenario())
