"""Voice-to-Prompt: speech recognition with faster-whisper (lazy model, inference off the event loop).

Telegram .ogg/Opus (and mp3/m4a/wav) is decoded by decode_audio() through PyAV, which bundles its own
FFmpeg, so no system-wide ffmpeg is needed on Windows.

CUDA on Windows: ctranslate2 reports a CUDA device as soon as the NVIDIA driver is present, but it
also needs cuBLAS 12 and cuDNN 9 DLLs. A missing cuDNN aborts the whole process on the first
inference (not an exception), so WHISPER_DEVICE=auto picks CUDA only when those DLLs really load.
They can come from a CUDA toolkit on PATH or from `pip install nvidia-cublas-cu12 nvidia-cudnn-cu12`.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.config import Settings
from src.utils.winpath import IS_WINDOWS

log = logging.getLogger(__name__)

_CUDA_DLLS_WINDOWS = ("cublas64_12.dll", "cudnn_ops64_9.dll")
_CUDA_LIBS_POSIX = ("libcublas.so.12", "libcudnn_ops.so.9")


class TranscriberError(Exception):
    """Speech recognition failed."""


class TranscriberUnavailable(TranscriberError):
    """faster-whisper is not installed or the model cannot be loaded."""


@dataclass(frozen=True, slots=True)
class Transcript:
    text: str
    language: str | None
    language_probability: float | None
    duration: float
    elapsed: float
    device: str


def decode_audio(path: Path, sampling_rate: int = 16_000) -> Any:
    """Decode any audio PyAV/FFmpeg understands to mono float32 PCM for Whisper.

    faster-whisper 1.2.1 has its own decode_audio() that calls av.open(..., metadata_errors=...),
    an argument PyAV 19 removed (TypeError on every file). Decoding here with the stable PyAV API
    and passing a numpy array keeps the bot independent from that version skew.
    """
    import av
    import numpy as np

    resampler = av.AudioResampler(format="s16", layout="mono", rate=sampling_rate)
    chunks: list[Any] = []
    try:
        with av.open(str(path), mode="r") as container:
            if not container.streams.audio:
                raise TranscriberError("В файле нет аудиодорожки")
            stream = container.streams.audio[0]
            for packet in container.demux(stream):
                try:
                    frames = packet.decode()
                except av.error.InvalidDataError:
                    continue  # skip a broken packet, keep the rest of the recording
                for frame in frames:
                    frame.pts = None
                    chunks.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(frame))
            chunks.extend(out.to_ndarray().reshape(-1) for out in resampler.resample(None))
    except av.error.FFmpegError as exc:
        raise TranscriberError(f"Не удалось декодировать аудио: {exc}") from exc
    if not chunks:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(chunks).astype(np.float32) / 32768.0


def _register_nvidia_pip_dirs() -> None:
    """Make DLLs from nvidia-* pip wheels (site-packages/nvidia/*/bin) visible to ctranslate2."""
    for base in {Path(p) for p in sys.path if p.endswith("site-packages")}:
        nvidia = base / "nvidia"
        if not nvidia.is_dir():
            continue
        for bin_dir in nvidia.glob("*/bin"):
            text = str(bin_dir)
            if text not in os.environ.get("PATH", ""):
                os.environ["PATH"] = text + os.pathsep + os.environ.get("PATH", "")
            if hasattr(os, "add_dll_directory"):
                try:
                    os.add_dll_directory(text)
                except OSError:
                    continue


def cuda_status() -> tuple[bool, str]:
    """(usable, reason). Usable only if a GPU is visible AND cuBLAS/cuDNN libraries load."""
    try:
        import ctranslate2

        count = ctranslate2.get_cuda_device_count()
    except Exception as exc:  # noqa: BLE001 - any import/driver problem means "no CUDA"
        return False, f"ctranslate2 недоступен: {exc}"
    if count <= 0:
        return False, "CUDA-устройств нет"
    missing: list[str] = []
    if IS_WINDOWS:
        _register_nvidia_pip_dirs()
        for dll in _CUDA_DLLS_WINDOWS:
            try:
                ctypes.WinDLL(dll)
            except OSError:
                missing.append(dll)
    else:
        for lib in _CUDA_LIBS_POSIX:
            try:
                ctypes.CDLL(lib)
            except OSError:
                missing.append(lib)
    if missing:
        return False, f"GPU есть, но не найдены {', '.join(missing)}"
    return True, f"CUDA, устройств: {count}"


class WhisperTranscriber:
    def __init__(
        self,
        model_name: str,
        device: str = "auto",
        compute_type: str = "",
        language: str = "",
        download_dir: Path | None = None,
        initial_prompt: str = "",
        beam_size: int = 5,
    ) -> None:
        self.model_name = model_name
        self._device_setting = device
        self._compute_type = compute_type.strip()
        self._language = language.strip() or None
        self._download_dir = download_dir
        self._initial_prompt = initial_prompt.strip() or None
        self._beam_size = beam_size
        self._model: Any = None
        self._device = ""
        self._device_note = ""
        self._lock = asyncio.Lock()

    @classmethod
    def from_settings(cls, settings: Settings) -> "WhisperTranscriber":
        return cls(
            model_name=settings.WHISPER_MODEL,
            device=settings.WHISPER_DEVICE,
            compute_type=settings.WHISPER_COMPUTE_TYPE,
            language=settings.WHISPER_LANGUAGE,
            download_dir=settings.WHISPER_DOWNLOAD_DIR,
            initial_prompt=settings.WHISPER_INITIAL_PROMPT,
            beam_size=settings.WHISPER_BEAM_SIZE,
        )

    @property
    def loaded(self) -> bool:
        return self._model is not None

    def describe(self) -> str:
        if not self.loaded:
            return f"{self.model_name}, {self._device_setting} (модель загрузится при первом голосовом)"
        note = f" ({self._device_note})" if self._device_note else ""
        return f"{self.model_name}, {self._device}{note}"

    # ---------- sync parts (run in a worker thread) ----------

    def _choose_device(self) -> tuple[str, str, str]:
        if self._device_setting == "cpu":
            return "cpu", self._compute_type or "int8", "задано WHISPER_DEVICE=cpu"
        if self._device_setting == "cuda":
            return "cuda", self._compute_type or "float16", "задано WHISPER_DEVICE=cuda"
        usable, reason = cuda_status()
        if usable:
            return "cuda", self._compute_type or "float16", reason
        return "cpu", self._compute_type or "int8", reason

    def _create_model(self, device: str, compute_type: str) -> Any:
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise TranscriberUnavailable(
                "faster-whisper не установлен: .venv\\Scripts\\python.exe -m pip install faster-whisper"
            ) from exc
        kwargs: dict[str, Any] = {"device": device, "compute_type": compute_type}
        if self._download_dir is not None:
            kwargs["download_root"] = str(self._download_dir)
        return WhisperModel(self.model_name, **kwargs)

    def _load_sync(self) -> None:
        device, compute_type, reason = self._choose_device()
        started = time.monotonic()
        try:
            self._model = self._create_model(device, compute_type)
        except TranscriberUnavailable:
            raise
        except (RuntimeError, ValueError, OSError) as exc:
            if device != "cuda" or self._device_setting != "auto":
                raise TranscriberUnavailable(f"Не удалось загрузить модель {self.model_name}: {exc}") from exc
            log.warning("Whisper on CUDA failed (%s), falling back to CPU int8", exc)
            device, compute_type, reason = "cpu", self._compute_type or "int8", f"CUDA не запустилась: {exc}"
            self._model = self._create_model(device, compute_type)
        self._device = f"{device} {compute_type}"
        self._device_note = reason
        log.info(
            "Whisper model %s loaded on %s %s in %.1fs (%s)",
            self.model_name, device, compute_type, time.monotonic() - started, reason,
        )

    def _run_model(self, audio: Any) -> tuple[str, Any]:
        segments, info = self._model.transcribe(
            audio,
            language=self._language,
            initial_prompt=self._initial_prompt,
            beam_size=self._beam_size,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
        )
        # `segments` is a lazy generator: the actual decoding happens while iterating.
        text = " ".join(seg.text.strip() for seg in segments if seg.text and seg.text.strip())
        return text, info

    def _transcribe_sync(self, path: Path) -> Transcript:
        started = time.monotonic()
        audio = decode_audio(path)
        if audio.size == 0:
            return Transcript("", None, None, 0.0, time.monotonic() - started, self._device)
        try:
            text, info = self._run_model(audio)
        except RuntimeError as exc:
            if not self._device.startswith("cuda") or self._device_setting != "auto":
                raise TranscriberError(f"Ошибка распознавания: {exc}") from exc
            log.warning("Whisper CUDA inference failed (%s), reloading on CPU", exc)
            compute_type = self._compute_type or "int8"
            self._model = self._create_model("cpu", compute_type)
            self._device, self._device_note = f"cpu {compute_type}", f"CUDA упала при распознавании: {exc}"
            text, info = self._run_model(audio)
        except (ValueError, OSError) as exc:
            raise TranscriberError(f"Не удалось декодировать аудио: {exc}") from exc
        return Transcript(
            text=text.strip(),
            language=getattr(info, "language", None),
            language_probability=getattr(info, "language_probability", None),
            duration=float(getattr(info, "duration", 0.0) or 0.0),
            elapsed=time.monotonic() - started,
            device=self._device,
        )

    # ---------- async API ----------

    async def ensure_loaded(self) -> None:
        async with self._lock:
            if self._model is None:
                await asyncio.to_thread(self._load_sync)

    async def transcribe(self, path: Path) -> Transcript:
        """Lazy-load the model on first use, then transcribe in a worker thread (one at a time)."""
        async with self._lock:
            if self._model is None:
                await asyncio.to_thread(self._load_sync)
            return await asyncio.to_thread(self._transcribe_sync, path)
