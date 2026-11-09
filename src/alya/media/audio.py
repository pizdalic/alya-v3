"""Перекодирование аудио через ffmpeg (в Docker-образе установлен; без него голос выключается).

- входящее голосовое (ogg/opus) или кружок (mp4) → mp3 16 кГц моно для распознавания;
- ответ синтеза (mp3) → ogg/opus для voice note Telegram.
Вход пишется во временный файл: mp4 с moov в конце не читается из pipe.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import shutil
import tempfile

from alya.core.errors import AlyaError

_TIME = re.compile(r"time=(\d+):(\d{2}):(\d{2}(?:\.\d+)?)")
MAX_OUTPUT = 20 * 1024 * 1024


class AudioError(AlyaError):
    pass


def ffmpeg_path() -> str | None:
    return shutil.which("ffmpeg")


def _duration(stderr: str) -> float:
    found = _TIME.findall(stderr)
    if not found:
        return 0.0
    h, m, s = found[-1]
    return int(h) * 3600 + int(m) * 60 + float(s)


async def _run(data: bytes, args: list[str], *, timeout_s: float = 60.0) -> tuple[bytes, float]:
    exe = ffmpeg_path()
    if exe is None:
        raise AudioError("ffmpeg не найден")
    fd, path = tempfile.mkstemp(prefix="alya-in-")
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        proc = await asyncio.create_subprocess_exec(
            exe, "-hide_banner", "-nostdin", "-y", "-i", path, *args, "pipe:1",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise AudioError("ffmpeg не уложился во время") from None
    finally:
        with contextlib.suppress(OSError):
            os.unlink(path)
    if proc.returncode != 0 or not out:
        raise AudioError(f"ffmpeg завершился с кодом {proc.returncode}")
    if len(out) > MAX_OUTPUT:
        raise AudioError("слишком большой результат перекодирования")
    return out, _duration(err.decode("utf-8", "replace"))


async def speech_to_mp3(data: bytes, *, max_seconds: int) -> bytes:
    out, _ = await _run(data, ["-vn", "-t", str(max_seconds), "-ac", "1", "-ar", "16000",
                               "-b:a", "48k", "-f", "mp3"])
    return out


async def to_voice_note(data: bytes) -> tuple[bytes, int]:
    """(ogg/opus, длительность в секундах)."""
    out, seconds = await _run(data, ["-vn", "-ac", "1", "-ar", "48000", "-c:a", "libopus",
                                     "-b:a", "32k", "-application", "voip", "-f", "ogg"])
    return out, max(1, round(seconds))
