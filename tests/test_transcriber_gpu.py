"""Real faster-whisper on the GPU: WhisperModel must start on CUDA with the pip cuBLAS/cuDNN wheels.

Skipped without an NVIDIA GPU (GitHub runners) or without a cached model (no downloads in tests).
The model runs in a child interpreter: a broken cuDNN/cuBLAS aborts the process instead of raising,
which would otherwise take the whole pytest session down.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.services import transcriber as tr

ROOT = Path(__file__).resolve().parent.parent
MODEL = os.environ.get("WHISPER_TEST_MODEL", "base")
CASES = (("cuda", "float16"), ("cuda", "int8_float16"), ("auto", ""))

CHILD = r'''
import asyncio, json, math, struct, sys, wave
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from src.services.transcriber import WhisperTranscriber

wav = Path(sys.argv[2])
with wave.open(str(wav), "wb") as out:
    out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
    out.writeframes(b"".join(struct.pack("<h", int(9000 * math.sin(2 * math.pi * 440 * i / 16000)))
                             for i in range(16000)))
results = {}
for case in sys.argv[4:]:
    device, compute_type = case.split(":")
    t = WhisperTranscriber(sys.argv[3], device=device, compute_type=compute_type, language="ru")
    result = asyncio.run(t.transcribe(wav))
    model = t._model.model  # ctranslate2.models.Whisper
    results[case] = {"device": result.device, "ct2_device": model.device,
                     "ct2_compute_type": model.compute_type, "describe": t.describe()}
print("RESULT " + json.dumps(results))
'''


def _model_cached(name: str) -> bool:
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:
        return False
    return isinstance(try_to_load_from_cache(f"Systran/faster-whisper-{name}", "model.bin"), str)


@pytest.fixture(scope="module")
def gpu_results(tmp_path_factory: pytest.TempPathFactory) -> dict[str, dict[str, str]]:
    usable, reason = tr.cuda_status()
    if not usable:
        pytest.skip(f"CUDA недоступна: {reason}")
    if not _model_cached(MODEL):
        pytest.skip(f"модель faster-whisper-{MODEL} не скачана в кэш Hugging Face")
    wav = tmp_path_factory.mktemp("gpu") / "tone.wav"
    cases = [f"{device}:{compute}" for device, compute in CASES]
    proc = subprocess.run(
        [sys.executable, "-c", CHILD, str(ROOT), str(wav), MODEL, *cases],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600,
    )
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert proc.returncode == 0 and lines, f"exit {proc.returncode}\n{proc.stdout[-2000:]}\n{proc.stderr[-3000:]}"
    return json.loads(lines[-1].removeprefix("RESULT "))


@pytest.mark.gpu
@pytest.mark.parametrize(("device", "compute_type"), CASES[:2])
def test_whisper_starts_on_cuda(gpu_results: dict[str, dict[str, str]], device: str, compute_type: str) -> None:
    result = gpu_results[f"{device}:{compute_type}"]
    assert result["device"] == f"cuda {compute_type}"
    assert (result["ct2_device"], result["ct2_compute_type"]) == ("cuda", compute_type)


@pytest.mark.gpu
def test_auto_device_now_picks_cuda(gpu_results: dict[str, dict[str, str]]) -> None:
    result = gpu_results["auto:"]
    assert result["device"] == "cuda float16" and result["ct2_device"] == "cuda"
    assert "CUDA, устройств" in result["describe"]
