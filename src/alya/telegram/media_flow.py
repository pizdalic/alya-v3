"""Медиа в потоке ответа: расшифровка входящих вложений и сборка исходящих действий.

Порядок исходящего: реакция → текст (или голосовое вместо него) → стикер → селфи.
Каждый шаг ограничен UsageLimiter; любая ошибка медиа откатывается к обычному тексту.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from alya.media import audio
from alya.media.clients import image_mime
from alya.media.limits import UsageLimiter
from alya.media.models import ReplyActions
from alya.media.processor import MediaProcessor
from alya.media.stickers import StickerStore
from alya.telegram.commands import OwnerCommands
from alya.telegram.models import IncomingMessage

log = logging.getLogger(__name__)

type Payload = dict[str, Any]
type VoiceEncoder = Callable[[bytes], Awaitable[tuple[bytes, int]]]


class TextToSpeech(Protocol):
    async def synthesize(self, text: str) -> bytes: ...


class ImageGen(Protocol):
    async def generate(self, prompt: str) -> bytes: ...


def _default_ffmpeg() -> bool:
    return audio.ffmpeg_path() is not None


@dataclass
class MediaKit:
    limiter: UsageLimiter
    processor: MediaProcessor | None = None
    stickers: StickerStore | None = None
    tts: TextToSpeech | None = None
    images: ImageGen | None = None
    selfie_prompt: Callable[[str], str] | None = None
    commands: OwnerCommands | None = None
    media_dir: Path = Path("runtime/media")
    selfie_private_only: bool = True
    max_voice_chars: int = 400
    encode_voice: VoiceEncoder = field(default=audio.to_voice_note)
    has_ffmpeg: Callable[[], bool] = field(default=_default_ffmpeg)

    def out_path(self, key: str, ext: str) -> Path:
        out = self.media_dir / "out"
        out.mkdir(parents=True, exist_ok=True)
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in key)
        return out / f"{safe}.{ext}"


async def outgoing(kit: MediaKit | None, *, chat_id: int, sender_id: int,
                   last: IncomingMessage, actions: ReplyActions | None, parts: Sequence[str],
                   quote: bool, typing: Callable[[str], float]) -> list[tuple[str, Payload]]:
    """Список (ключ outbox, payload) в порядке отправки."""
    base = f"{chat_id}:{last.message_id}"
    who = [sender_id]
    reply_to = last.message_id if quote else None
    items: list[tuple[str, Payload]] = []
    acts = actions if isinstance(actions, ReplyActions) else ReplyActions()
    if kit is not None and acts.reaction and await kit.limiter.take("reaction", chat_id):
        items.append((f"{base}:r", {"kind": "reaction", "message_id": last.message_id,
                                    "emoji": acts.reaction, "typing_s": 0, "addressees": who}))
    voice = None
    if kit is not None and acts.voice and parts:
        voice = await _voice(kit, base, chat_id, parts, reply_to, who)
    if voice is not None:
        items.append(voice)
    else:
        for i, part in enumerate(parts):
            items.append((f"{base}:{i}", {"text": part, "reply_to": reply_to if i == 0 else None,
                                          "typing_s": round(typing(part), 2),
                                          "addressees": who}))
    wants = bool(acts.sticker or acts.selfie)
    if wants:
        log.info("медиа %s: sticker=%r sticker_id=%r selfie=%r", base, acts.sticker,
                 acts.sticker_id[:16], acts.selfie)
    if kit is None or not parts:
        if wants:
            log.info("медиа %s пропущено: %s", base,
                     "медиа выключены" if kit is None else "нет текста ответа")
        return items
    if acts.sticker:
        sticker = await _sticker(kit, base, chat_id, acts, who)
        if sticker is not None:
            items.append(sticker)
    if acts.selfie:
        if last.is_private or not kit.selfie_private_only:
            photo = await _selfie(kit, base, chat_id, acts.selfie, who)
            if photo is not None:
                items.append(photo)
        else:
            log.info("селфи %s пропущено: только в личке", base)
    return items


async def _sticker(kit: MediaKit, base: str, chat_id: int, acts: ReplyActions,
                   who: list[int]) -> tuple[str, Payload] | None:
    if kit.stickers is None:
        log.info("стикер %s пропущен: стикеры выключены", base)
        return None
    remote = acts.sticker_id or await kit.stickers.pick(acts.sticker)
    if not remote:
        log.info("стикер %s пропущен: в базе нет стикера под %r", base, acts.sticker)
        return None
    if not await kit.limiter.take("sticker", chat_id):
        log.info("стикер %s пропущен: %s", base,
                 await kit.limiter.check("sticker") or "лимит sticker")
        return None
    key = f"{base}:s"
    log.info("стикер %s в очереди: %r -> %s", key, acts.sticker, remote[:16])
    return key, {"kind": "sticker", "remote_id": remote, "emoji": acts.sticker,
                 "typing_s": 0, "addressees": who}


async def _voice(kit: MediaKit, base: str, chat_id: int, parts: Sequence[str],
                 reply_to: int | None, who: list[int]) -> tuple[str, Payload] | None:
    text = " ".join(p.strip() for p in parts if p.strip())
    if kit.tts is None or not text or len(text) > kit.max_voice_chars or not kit.has_ffmpeg():
        return None
    if not await kit.limiter.take("tts", chat_id):
        return None
    key = f"{base}:v"
    try:
        ogg, seconds = await kit.encode_voice(await kit.tts.synthesize(text))
        path = kit.out_path(key, "ogg")
        path.write_bytes(ogg)
    except Exception as exc:
        log.warning("голосовое не получилось, отвечаю текстом: %s", type(exc).__name__)
        return None
    return key, {"kind": "voice", "path": str(path), "duration_s": seconds, "text": text,
                 "reply_to": reply_to, "typing_s": 0, "addressees": who}


async def _selfie(kit: MediaKit, base: str, chat_id: int, scene: str,
                  who: list[int]) -> tuple[str, Payload] | None:
    if kit.images is None or kit.selfie_prompt is None:
        log.info("селфи %s пропущено: генерация изображений выключена", base)
        return None
    if not await kit.limiter.take("image", chat_id):
        log.info("селфи %s пропущено: %s", base,
                 await kit.limiter.check("image") or "лимит image")
        return None
    key = f"{base}:p"
    try:
        data = await kit.images.generate(kit.selfie_prompt(scene))
        ext = {"image/png": "png", "image/webp": "webp"}.get(image_mime(data) or "", "jpg")
        path = kit.out_path(key, ext)
        path.write_bytes(data)
    except Exception as exc:
        log.warning("селфи %s пропущено: фото не сгенерировалось: %s", key, type(exc).__name__)
        return None
    log.info("селфи %s в очереди: %s", key, path)
    return key, {"kind": "photo", "path": str(path), "caption": "", "scene": scene,
                 "typing_s": 0, "addressees": who}


def sent_text(payload: Payload) -> tuple[str, str | None] | None:
    """Что записать в историю об отправленном: (текст, media_kind) или None — не писать."""
    kind = str(payload.get("kind") or "text")
    if payload.get("system") or kind == "reaction":
        return None
    if kind == "voice":
        return f"[голосовое] {payload.get('text', '')}".strip(), "voice"
    if kind == "sticker":
        return f"[стикер {payload.get('emoji', '')}]".replace(" ]", "]"), "sticker"
    if kind == "photo":
        return f"[фото себя: {payload.get('scene', '')}]", "photo"
    return str(payload.get("text", "")), None
