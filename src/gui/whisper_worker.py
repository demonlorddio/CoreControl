"""
Standalone Whisper transcription worker.

Run this as a separate process to avoid CTranslate2 segfaulting with PyQt6.
Communication: JSON lines via stdin/stdout.
  Input:  {"audio": "<base64-encoded-float32-bytes>", "len": N}
  Output: {"text": "<transcribed text>"} or {"error": "..."}
  Stop:   {"stop": true}
"""
from __future__ import annotations

import base64
import json
import sys
import numpy as np

try:
    from faster_whisper import WhisperModel
except ImportError:
    print(json.dumps({"error": "faster-whisper not installed"}), flush=True)
    sys.exit(1)

_MODEL_NAME = sys.argv[1] if len(sys.argv) > 1 else "base.en"
_model = WhisperModel(_MODEL_NAME, device="cpu", compute_type="int8")


def _transcribe(audio_bytes: bytes, length: int) -> str:
    audio = np.frombuffer(audio_bytes[: length * 4], dtype=np.float32)
    segments, _ = _model.transcribe(audio, language="en", vad_filter=True)
    return " ".join(seg.text for seg in segments).strip()


for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        req = json.loads(line)
    except json.JSONDecodeError:
        continue

    if req.get("stop"):
        break

    audio_b64 = req.get("audio", "")
    audio_len = req.get("len", 0)
    try:
        audio_raw = base64.b64decode(audio_b64)
        text = _transcribe(audio_raw, audio_len)
        print(json.dumps({"text": text}), flush=True)
    except Exception as exc:
        print(json.dumps({"error": str(exc)}), flush=True)
