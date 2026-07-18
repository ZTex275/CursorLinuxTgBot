from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# Подсказка для Whisper: типичные слова в голосовых задачах бота (RU).
_DEFAULT_INITIAL_PROMPT = (
    "Команды для Linux-сервера: статус, перезапуск, логи, git commit, push, pull, "
    "установить пакет, проверить диск, память, процессы, systemd, docker, nginx."
)

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass
class TranscriptionResult:
    text: str
    language: str | None = None


class VoiceTranscriber:
    """Offline speech-to-text for Telegram voice messages (OGG/Opus → text)."""

    def __init__(
        self,
        *,
        model: str = "base",
        language: str = "ru",
        device: str = "cpu",
        compute_type: str = "int8",
        beam_size: int = 5,
        best_of: int = 5,
        initial_prompt: str = "",
    ) -> None:
        self._model_name = model
        self._language = language or None
        self._device = device
        self._compute_type = compute_type
        self._beam_size = max(1, beam_size)
        self._best_of = max(1, best_of)
        self._initial_prompt = (initial_prompt or _DEFAULT_INITIAL_PROMPT).strip() or None
        self._model = None
        self._lock = asyncio.Lock()

    async def _get_model(self):
        if self._model is not None:
            return self._model
        async with self._lock:
            if self._model is not None:
                return self._model
            self._model = await asyncio.to_thread(self._load_model)
            return self._model

    def _load_model(self):
        from faster_whisper import WhisperModel

        logger.info(
            "Loading Whisper model %s (device=%s, compute=%s, beam=%s)",
            self._model_name,
            self._device,
            self._compute_type,
            self._beam_size,
        )
        return WhisperModel(
            self._model_name,
            device=self._device,
            compute_type=self._compute_type,
        )

    def _convert_to_wav(self, source: Path, target: Path) -> None:
        # 16 kHz mono + лёгкая фильтрация и нормализация громкости для STT.
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(source),
            "-af",
            "highpass=f=80,lowpass=f=8000,loudnorm=I=-16:TP=-1.5:LRA=11",
            "-ar",
            "16000",
            "-ac",
            "1",
            "-c:a",
            "pcm_s16le",
            str(target),
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            raise RuntimeError(f"ffmpeg failed: {stderr or proc.returncode}")

    def _transcribe_file(self, wav_path: Path) -> TranscriptionResult:
        model = self._model
        if model is None:
            raise RuntimeError("Whisper model is not loaded")

        segments, info = model.transcribe(
            str(wav_path),
            language=self._language,
            task="transcribe",
            beam_size=self._beam_size,
            best_of=self._best_of,
            patience=1.0,
            temperature=[0.0, 0.2, 0.4],
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": 400,
                "speech_pad_ms": 200,
            },
            condition_on_previous_text=True,
            initial_prompt=self._initial_prompt,
            without_timestamps=True,
        )
        parts = [segment.text.strip() for segment in segments if segment.text.strip()]
        text = _WHITESPACE_RE.sub(" ", " ".join(parts)).strip()
        return TranscriptionResult(text=text, language=getattr(info, "language", None))

    async def transcribe_bytes(self, data: bytes, *, suffix: str = ".ogg") -> TranscriptionResult:
        if not data:
            raise ValueError("empty audio")

        with tempfile.TemporaryDirectory(prefix="tg-voice-") as tmp:
            tmp_path = Path(tmp)
            source = tmp_path / f"input{suffix}"
            wav = tmp_path / "audio.wav"
            source.write_bytes(data)

            await asyncio.to_thread(self._convert_to_wav, source, wav)
            await self._get_model()
            return await asyncio.to_thread(self._transcribe_file, wav)
