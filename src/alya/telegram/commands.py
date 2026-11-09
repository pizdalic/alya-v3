"""Служебные команды владельца в личке: /join, /bio, /avatar, /stickers, /media.

Команды принимаются только от owner_user_id и только в личном чате. Всё остальное —
обычные сообщения. Ответ команды — короткий служебный текст, в память он не попадает.
"""

from __future__ import annotations

import contextlib
import logging
import re
from pathlib import Path
from typing import Protocol

from alya.media.limits import KINDS, UsageLimiter
from alya.media.stickers import StickerStore
from alya.telegram.models import IncomingMessage
from alya.telegram.transport import TransportError

log = logging.getLogger(__name__)

_INVITE = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me)/(?:\+|joinchat/)[\w-]+$")
_SET_NAME = re.compile(r"^(?:https?://)?(?:t\.me/addstickers/)?([A-Za-z0-9_]{1,64})$")
BIO_LIMIT = 70

HELP = ("команды:\n"
        "/join <ссылка-приглашение> — вступить в чат\n"
        "/bio <текст> — сменить описание профиля\n"
        "/avatar — пришли фото с подписью /avatar\n"
        "/stickers add|remove <набор> · /stickers list\n"
        "/media — что включено и сколько потрачено за сутки")


class AccountTransport(Protocol):
    async def join_by_link(self, link: str) -> int: ...

    async def set_bio(self, text: str) -> None: ...

    async def set_avatar(self, path: str) -> None: ...

    async def sticker_set(self, name: str) -> list[tuple[str, str]]: ...

    async def download(self, file_id: int, max_bytes: int) -> bytes: ...


class OwnerCommands:
    def __init__(self, transport: AccountTransport, *, owner_id: int | None,
                 limiter: UsageLimiter | None = None, stickers: StickerStore | None = None,
                 media_dir: Path = Path("runtime/media"),
                 max_image_bytes: int = 10 * 1024 * 1024) -> None:
        self._t = transport
        self._owner = owner_id
        self._limiter = limiter
        self._stickers = stickers
        self._dir = media_dir
        self._max_image = max_image_bytes

    def accepts(self, msg: IncomingMessage) -> bool:
        if not (msg.is_private and self._owner is not None and msg.sender_id == self._owner):
            return False
        text = (msg.caption if msg.media is not None else msg.text).strip()
        return text.startswith("/")

    async def handle(self, msg: IncomingMessage) -> str | None:
        """Ответ на команду или None, если это не команда (тогда сообщение идёт дальше)."""
        if not self.accepts(msg):
            return None
        text = (msg.caption if msg.media is not None else msg.text).strip()
        name, _, arg = text.partition(" ")
        name = name.split("@", 1)[0].lower()
        arg = arg.strip()
        handler = {"/join": self._join, "/bio": self._bio, "/avatar": self._avatar,
                   "/stickers": self._sticker_cmd, "/media": self._media,
                   "/help": self._help}.get(name)
        if handler is None:
            return None  # неизвестная команда — пусть Аля просто ответит
        try:
            return await handler(msg, arg)
        except TransportError as exc:
            log.warning("команда %s не выполнена: %s", name, exc)
            return f"не получилось: {exc}"

    async def _help(self, msg: IncomingMessage, arg: str) -> str:
        return HELP

    async def _join(self, msg: IncomingMessage, arg: str) -> str:
        if not _INVITE.match(arg):
            return "нужна ссылка-приглашение вида t.me/+abc или t.me/joinchat/abc"
        chat_id = await self._t.join_by_link(arg)
        return f"вступила, чат {chat_id}"

    async def _bio(self, msg: IncomingMessage, arg: str) -> str:
        if len(arg) > BIO_LIMIT:
            return f"слишком длинно: {len(arg)} из {BIO_LIMIT} символов"
        await self._t.set_bio(arg)
        return "описание обновлено" if arg else "описание очищено"

    async def _avatar(self, msg: IncomingMessage, arg: str) -> str:
        media = msg.media
        if media is None or media.kind != "photo" or media.file_id is None:
            return "пришли фото с подписью /avatar"
        data = await self._t.download(media.file_id, self._max_image)
        out = self._dir / "out"
        out.mkdir(parents=True, exist_ok=True)
        path = out / f"avatar-{msg.message_id}.jpg"
        path.write_bytes(data)
        try:
            await self._t.set_avatar(str(path))
        finally:
            with contextlib.suppress(OSError):
                path.unlink()
        return "аватарка обновлена"

    async def _sticker_cmd(self, msg: IncomingMessage, arg: str) -> str:
        if self._stickers is None:
            return "стикеры выключены в конфиге"
        action, _, rest = arg.partition(" ")
        action = action.lower()
        if action == "list" or not action:
            sets = await self._stickers.sets()
            if not sets:
                return "наборов пока нет"
            return "\n".join(f"{name} — {count}" for name, count in sets)
        found = _SET_NAME.match(rest.strip())
        if action not in ("add", "remove") or not found:
            return "формат: /stickers add <набор> · /stickers remove <набор> · /stickers list"
        set_name = found.group(1)
        if action == "remove":
            removed = await self._stickers.remove_set(set_name)
            return f"убрала {removed} стикеров" if removed else "такого набора нет"
        stickers = await self._t.sticker_set(set_name)
        if not stickers:
            return "набор пустой или не найден"
        added = await self._stickers.add_set(set_name, stickers)
        return f"добавила {added} стикеров из {set_name}"

    async def _media(self, msg: IncomingMessage, arg: str) -> str:
        if self._limiter is None:
            return "медиа выключено"
        lines = []
        for kind in KINDS:
            state = "вкл" if self._limiter.enabled(kind) else "выкл"
            used = await self._limiter.used(kind, hours=24)
            lines.append(f"{kind}: {state}, за сутки {used}")
        return "\n".join(lines)
