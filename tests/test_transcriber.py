"""WhisperTranscriber unit tests with a fake `faster_whisper` module (no model download)."""

from __future__ import annotations

import asyncio
import os
import sys
import threading
import types
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from src.config import DEFAULT_WHISPER_PROMPT
from src.services import transcriber as tr
from src.services.transcriber import TranscriberError, TranscriberUnavailable, WhisperTranscriber, decode_audio


class FakeModel:
    instances: list["FakeModel"] = []
    fail_cuda_load = False
    fail_cuda_inference = False

    def __init__(self, name: str, device: str, compute_type: str, **kwargs: object) -> None:
        if device == "cuda" and FakeModel.fail_cuda_load:
            raise RuntimeError("Library cublas64_12.dll is not found")
        self.name, self.device, self.compute_type, self.kwargs = name, device, compute_type, kwargs
        self.calls: list[tuple[object, dict, int]] = []
        FakeModel.instances.append(self)

    def transcribe(self, audio: object, **kwargs: object):
        if self.device == "cuda" and FakeModel.fail_cuda_inference:
            raise RuntimeError("CUDA error: no kernel image is available")
        self.calls.append((audio, kwargs, threading.get_ident()))
        segments = iter([SimpleNamespace(text=" Claude, "), SimpleNamespace(text="  "),
                         SimpleNamespace(text=" запусти pytest. ")])
        return segments, SimpleNamespace(language="ru", language_probability=0.97, duration=3.5)


@pytest.fixture(autouse=True)
def fake_faster_whisper(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest):
    if "real_decoder" not in request.keywords:
        monkeypatch.setattr(tr, "decode_audio", lambda path: np.full(16_000, 0.1, dtype=np.float32))
    FakeModel.instances = []
    FakeModel.fail_cuda_load = False
    FakeModel.fail_cuda_inference = False
    module = types.ModuleType("faster_whisper")
    module.WhisperModel = FakeModel  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", module)
    return module


def _make(**kwargs: object) -> WhisperTranscriber:
    params = dict(model_name="medium", device="cpu", language="ru", initial_prompt=DEFAULT_WHISPER_PROMPT)
    params.update(kwargs)
    return WhisperTranscriber(**params)  # type: ignore[arg-type]


def test_lazy_load_once_and_off_event_loop(tmp_path: Path) -> None:
    t = _make()
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"x")
    assert not t.loaded and FakeModel.instances == []  # nothing loaded at construction time
    main_thread = threading.get_ident()

    async def scenario():
        return await t.transcribe(audio), await t.transcribe(audio)

    first, second = asyncio.run(scenario())
    assert len(FakeModel.instances) == 1 and t.loaded
    model = FakeModel.instances[0]
    assert (model.name, model.device, model.compute_type) == ("medium", "cpu", "int8")
    pcm, kwargs, thread_id = model.calls[0]
    assert isinstance(pcm, np.ndarray) and pcm.dtype == np.float32 and pcm.size == 16_000
    assert thread_id != main_thread
    assert kwargs["initial_prompt"] == DEFAULT_WHISPER_PROMPT and "Figma" in kwargs["initial_prompt"]
    assert kwargs["language"] == "ru" and kwargs["vad_filter"] is True and kwargs["beam_size"] == 5
    assert first.text == "Claude, запусти pytest." and first.language == "ru" and first.device == "cpu int8"
    assert second.text == first.text


def test_auto_device_selection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"x")

    monkeypatch.setattr(tr, "cuda_status", lambda: (False, "GPU есть, но не найдены cudnn_ops64_9.dll"))
    cpu = _make(device="auto")
    asyncio.run(cpu.transcribe(audio))
    assert cpu.describe().startswith("medium, cpu int8") and "cudnn_ops64_9.dll" in cpu.describe()

    monkeypatch.setattr(tr, "cuda_status", lambda: (True, "CUDA, устройств: 1"))
    gpu = _make(device="auto")
    assert asyncio.run(gpu.transcribe(audio)).device == "cuda float16"

    FakeModel.fail_cuda_load = True
    fallback = _make(device="auto")
    assert asyncio.run(fallback.transcribe(audio)).device == "cpu int8"
    assert "CUDA не запустилась" in fallback.describe()

    FakeModel.fail_cuda_load, FakeModel.fail_cuda_inference = False, True
    retried = _make(device="auto")
    result = asyncio.run(retried.transcribe(audio))
    assert result.device == "cpu int8" and result.text == "Claude, запусти pytest."

    forced = _make(device="cuda", compute_type="int8_float16")
    FakeModel.fail_cuda_inference = False
    assert asyncio.run(forced.transcribe(audio)).device == "cuda int8_float16"


def test_register_nvidia_dll_dirs(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    site_dir = tmp_path / "site-packages"
    bins = [site_dir / "nvidia" / name / "bin" for name in ("cublas", "cudnn")]
    for bin_dir in bins:
        bin_dir.mkdir(parents=True)
    (site_dir / "nvidia" / "cudnn" / "include").mkdir()
    calls: list[str] = []
    monkeypatch.setattr(tr, "IS_WINDOWS", True)
    monkeypatch.setattr(tr, "_DLL_DIR_HANDLES", {})
    monkeypatch.setattr(tr, "_site_dirs", lambda: [site_dir, tmp_path / "missing"])
    monkeypatch.setattr(tr.os, "add_dll_directory", lambda path: calls.append(path) or object(), raising=False)
    monkeypatch.setenv("PATH", r"C:\Windows")

    added = tr.register_nvidia_dll_dirs()
    assert added == [str(b.resolve()) for b in bins] and calls == added
    path = os.environ["PATH"].split(os.pathsep)
    assert path[:2] == added and path[-1] == r"C:\Windows"

    assert tr.register_nvidia_dll_dirs() == [] and len(calls) == 2  # idempotent: no duplicates
    assert os.environ["PATH"].split(os.pathsep) == path


def test_cuda_model_registers_dll_dirs_before_whisper(monkeypatch: pytest.MonkeyPatch, fake_faster_whisper) -> None:
    order: list[object] = []
    monkeypatch.setattr(tr, "register_nvidia_dll_dirs", lambda: order.append("register") or [])

    def whisper_model(name: str, **kwargs: object) -> FakeModel:
        order.append(("WhisperModel", kwargs["device"], kwargs["compute_type"]))
        return FakeModel(name, **kwargs)  # type: ignore[arg-type]

    fake_faster_whisper.WhisperModel = whisper_model
    asyncio.run(_make(device="cuda", compute_type="int8_float16").ensure_loaded())
    assert order == ["register", ("WhisperModel", "cuda", "int8_float16")]

    order.clear()
    asyncio.run(_make(device="cpu").ensure_loaded())
    assert order == [("WhisperModel", "cpu", "int8")]  # CPU never touches the NVIDIA DLLs


def test_missing_package_is_reported(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setitem(sys.modules, "faster_whisper", None)
    audio = tmp_path / "v.ogg"
    audio.write_bytes(b"x")
    with pytest.raises(TranscriberUnavailable, match="pip install faster-whisper"):
        asyncio.run(_make().transcribe(audio))


def test_cuda_status_never_crashes() -> None:
    usable, reason = tr.cuda_status()
    assert isinstance(usable, bool) and reason


def test_from_settings(make_settings) -> None:
    settings = make_settings(WHISPER_MODEL="small", WHISPER_DEVICE="CPU", WHISPER_LANGUAGE="",
                             WHISPER_DOWNLOAD_DIR="")
    t = WhisperTranscriber.from_settings(settings)
    assert settings.WHISPER_DEVICE == "cpu" and settings.WHISPER_DOWNLOAD_DIR is None
    assert t.model_name == "small" and t._language is None and t._initial_prompt == DEFAULT_WHISPER_PROMPT
    assert "загрузится при первом голосовом" in t.describe()


def _write_tone(path: Path, codec: str, rate: int, seconds: float = 1.0) -> None:
    import av

    with av.open(str(path), "w") as out:
        stream = out.add_stream(codec, rate=rate)
        stream.layout = "mono"
        samples = int(rate * seconds)
        t = np.arange(samples) / rate
        pcm = (0.5 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16).reshape(1, -1)
        frame = av.AudioFrame.from_ndarray(pcm, format="s16", layout="mono")
        frame.sample_rate = rate
        resampler = av.AudioResampler(format=stream.format.name, layout="mono", rate=rate)
        for chunk in resampler.resample(frame):
            for packet in stream.encode(chunk):
                out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)


@pytest.mark.real_decoder
@pytest.mark.parametrize(("name", "codec", "rate"), [
    ("voice.ogg", "libopus", 48_000),   # what Telegram sends for voice messages
    ("memo.wav", "pcm_s16le", 44_100),
    ("memo.mp3", "libmp3lame", 44_100),
])
def test_decode_audio_real_pyav(tmp_path: Path, name: str, codec: str, rate: int) -> None:
    path = tmp_path / name
    try:
        _write_tone(path, codec, rate)
    except Exception as exc:  # noqa: BLE001 - an encoder missing from the PyAV build is not our bug
        pytest.skip(f"PyAV cannot encode {codec}: {exc}")
    pcm = decode_audio(path)
    assert pcm.dtype == np.float32 and pcm.ndim == 1
    assert abs(pcm.size - 16_000) < 1_600  # ~1 s at 16 kHz (codec padding allowed)
    assert 0.3 < float(np.abs(pcm).max()) <= 1.0


@pytest.mark.real_decoder
def test_decode_audio_rejects_garbage(tmp_path: Path) -> None:
    bad = tmp_path / "bad.ogg"
    bad.write_bytes(b"not an ogg file at all" * 10)
    with pytest.raises(TranscriberError):
        decode_audio(bad)


def test_empty_audio_skips_model(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tr, "decode_audio", lambda path: np.zeros(0, dtype=np.float32))
    audio = tmp_path / "silence.ogg"
    audio.write_bytes(b"x")
    t = _make()
    result = asyncio.run(t.transcribe(audio))
    assert result.text == "" and FakeModel.instances[0].calls == []
