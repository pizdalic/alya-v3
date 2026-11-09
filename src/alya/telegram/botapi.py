"""Bot API: запасной транспорт и стенд для проверки (telegram.mode = "bot").

Только stdlib (urllib в отдельном потоке). Токен не попадает ни в исключения, ни в логи.

Ограничения Bot API по сравнению с userbot: в группах бот видит все сообщения только
с отключённым privacy mode (@BotFather → /setprivacy → Disable); нет вступления по ссылке,
bio/аватара, отметки «прочитано», уведомлений об удалении; голос, фото и скачивание
вложений в этом режиме не поддерживаются (Аля отвечает текстом).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any

from alya import __version__
from alya.telegram.models import IncomingMessage, MessageEdited
from alya.telegram.transport import FloodWait, TransportError

log = logging.getLogger(__name__)

API = "https://api.telegram.org"
KINDS = ("private", "group", "supergroup", "channel")
MEDIA_KEYS = ("photo", "voice", "sticker", "video", "video_note", "audio", "animation",
              "document")
PERMANENT = (400, 401, 403, 404)
Opener = Callable[[urllib.request.Request, float], bytes]


def _default_open(req: urllib.request.Request, timeout: float) -> bytes:
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 — фиксированный https
            return bytes(resp.read())
    except urllib.error.HTTPError as exc:
        return bytes(exc.read())  # Bot API кладёт описание ошибки в тело ответа


def _u16(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le", errors="ignore")


class BotApi:
    def __init__(self, token: str, *, base: str = API, timeout_s: float = 30.0,
                 opener: Opener | None = None) -> None:
        self._token = token
        self._base = base.rstrip("/")
        self._timeout = timeout_s
        self._open = opener or _default_open

    def _clean(self, text: str) -> str:
        return text.replace(self._token, "***") if self._token else text

    async def call(self, method: str, params: dict[str, Any] | None = None, *,
                   timeout: float | None = None) -> Any:
        req = urllib.request.Request(
            f"{self._base}/bot{self._token}/{method}",
            data=json.dumps(params or {}, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", "User-Agent": f"alya/{__version__}"})
        try:
            raw = await asyncio.to_thread(self._open, req, timeout or self._timeout)
        except (OSError, TimeoutError) as exc:
            raise TransportError(self._clean(f"{method}: сеть: {type(exc).__name__}: {exc}")) from None
        try:
            body = json.loads(raw)
        except ValueError:
            raise TransportError(f"{method}: ответ не JSON") from None
        if not isinstance(body, dict):
            raise TransportError(f"{method}: странный ответ")
        if body.get("ok"):
            return body.get("result")
        code = int(body.get("error_code") or 0)
        desc = self._clean(str(body.get("description") or "ошибка"))
        if code == 429:
            retry = (body.get("parameters") or {}).get("retry_after") or 5
            raise FloodWait(float(retry))
        raise TransportError(f"{method}: {code} {desc}", permanent=code in PERMANENT)


class BotApiAdapter:
    """Тот же контракт, что у TdlibAdapter: bind() + входящие события, отправка через Transport."""

    def __init__(self, api: BotApi, *, me_id: int, poll_timeout_s: int = 50) -> None:
        self._api = api
        self._me = me_id
        self._poll = poll_timeout_s
        self._offset = 0
        self._on_message: Callable[[IncomingMessage], Awaitable[None]] | None = None
        self._on_edit: Callable[[MessageEdited], Awaitable[None]] | None = None
        self._task: asyncio.Task[None] | None = None

    def bind(self, *, on_message: Callable[[IncomingMessage], Awaitable[None]],
             on_edit: Callable[[MessageEdited], Awaitable[None]],
             on_delete: Callable[[Any], Awaitable[None]] | None = None) -> None:
        # об удалениях Bot API не сообщает — on_delete не вызывается
        self._on_message, self._on_edit = on_message, on_edit

    # --- входящие ---

    def parse(self, m: dict[str, Any]) -> IncomingMessage | None:
        chat = m.get("chat") or {}
        kind = chat.get("type")
        if kind not in KINDS or "message_id" not in m or "id" not in chat:
            return None
        frm = m.get("from") or {}
        sender = None if m.get("sender_chat") else frm.get("id")
        text = str(m.get("text") or "")
        caption = str(m.get("caption") or "")
        source, ents = (text, m.get("entities")) if text else (caption, m.get("caption_entities"))
        ids: set[int] = set()
        names: set[str] = set()
        for e in ents or []:
            if e.get("type") == "mention":
                names.add(_u16(source, int(e["offset"]), int(e["length"])).lstrip("@").lower())
            elif e.get("type") == "text_mention" and (e.get("user") or {}).get("id"):
                ids.add(int(e["user"]["id"]))
        reply = m.get("reply_to_message") or {}
        reply_from = (reply.get("from") or {}).get("id")
        media_kind = next((k for k in MEDIA_KEYS if k in m), None)
        name = " ".join(p for p in (frm.get("first_name"), frm.get("last_name")) if p)
        return IncomingMessage(
            chat_id=int(chat["id"]), message_id=int(m["message_id"]),
            sender_id=int(sender) if sender is not None else None,
            chat_kind=kind, text=text or caption,
            date=datetime.fromtimestamp(int(m.get("date") or 0), UTC),
            sender_name=name, sender_username=frm.get("username") or None,
            is_outgoing=sender is not None and int(sender) == self._me,
            reply_to_message_id=reply.get("message_id"),
            reply_to_user_id=int(reply_from) if reply_from is not None else None,
            mentioned_user_ids=frozenset(ids), mentioned_usernames=frozenset(names),
            media_kind=media_kind, chat_title=chat.get("title"), caption=caption)

    async def handle_update(self, upd: dict[str, Any]) -> None:
        uid = upd.get("update_id")
        if isinstance(uid, int):
            self._offset = max(self._offset, uid + 1)
        try:
            if "message" in upd and self._on_message is not None:
                parsed = self.parse(upd["message"])
                if parsed is not None:
                    await self._on_message(parsed)
            elif "edited_message" in upd and self._on_edit is not None:
                m = upd["edited_message"]
                await self._on_edit(MessageEdited(
                    chat_id=int(m["chat"]["id"]), message_id=int(m["message_id"]),
                    text=str(m.get("text") or m.get("caption") or ""),
                    edited_at=datetime.fromtimestamp(int(m.get("edit_date") or 0), UTC)))
        except Exception:  # noqa: BLE001 — одно плохое событие не останавливает опрос
            log.exception("bot api: ошибка обработки события %s", uid)

    async def poll_once(self) -> int:
        updates = await self._api.call("getUpdates", {
            "offset": self._offset, "timeout": self._poll,
            "allowed_updates": ["message", "edited_message"]}, timeout=self._poll + 15)
        for upd in updates or []:
            await self.handle_update(upd)
        return len(updates or [])

    async def run(self, *, sleep: Callable[[float], Awaitable[None]] = asyncio.sleep) -> None:
        backoff = 1.0
        while True:
            try:
                await self.poll_once()
                backoff = 1.0
            except FloodWait as fw:
                await sleep(fw.seconds)
            except TransportError as exc:
                if exc.permanent:
                    log.error("bot api: опрос остановлен: %s", exc)
                    return
                log.warning("bot api: %s, повтор через %.0f с", exc, backoff)
                await sleep(backoff)
                backoff = min(backoff * 2, 60.0)

    async def start(self) -> None:
        self._task = asyncio.create_task(self.run(), name="botapi-poll")

    async def close(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    # --- отправка ---

    @staticmethod
    def _reply(params: dict[str, Any], reply_to: int | None) -> dict[str, Any]:
        if reply_to:
            params["reply_parameters"] = {"message_id": reply_to,
                                          "allow_sending_without_reply": True}
        return params

    async def send_text(self, chat_id: int, text: str, *, reply_to: int | None = None) -> int:
        res = await self._api.call("sendMessage", self._reply({"chat_id": chat_id, "text": text},
                                                              reply_to))
        return int(res["message_id"])

    async def send_typing(self, chat_id: int) -> None:
        try:
            await self._api.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except FloodWait:
            raise
        except TransportError as exc:
            log.debug("bot api: typing: %s", exc)

    async def mark_read(self, chat_id: int, message_ids: Sequence[int]) -> None:
        return None  # боты не отмечают прочитанное

    async def send_sticker(self, chat_id: int, remote_id: str, *,
                           reply_to: int | None = None) -> int:
        res = await self._api.call("sendSticker", self._reply(
            {"chat_id": chat_id, "sticker": remote_id}, reply_to))
        return int(res["message_id"])

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        await self._api.call("setMessageReaction", {
            "chat_id": chat_id, "message_id": message_id,
            "reaction": [{"type": "emoji", "emoji": emoji}]})

    async def sticker_set(self, name: str) -> list[tuple[str, str]]:
        found = await self._api.call("getStickerSet", {"name": name}) or {}
        return [(str(s["file_id"]), str(s.get("emoji") or ""))
                for s in found.get("stickers") or [] if s.get("file_id")]

    @staticmethod
    def _unsupported(what: str) -> TransportError:
        return TransportError(f"в режиме bot не поддерживается: {what}", permanent=True)

    async def send_voice(self, chat_id: int, path: str, duration_s: int, *,
                         reply_to: int | None = None) -> int:
        raise self._unsupported("голосовые")

    async def send_photo(self, chat_id: int, path: str, *, caption: str = "",
                         reply_to: int | None = None) -> int:
        raise self._unsupported("фото")

    async def download(self, file_id: int, max_bytes: int) -> bytes:
        raise self._unsupported("скачивание вложений")

    async def join_by_link(self, link: str) -> int:
        raise self._unsupported("вступление по ссылке")

    async def set_bio(self, text: str) -> None:
        raise self._unsupported("bio")

    async def set_avatar(self, path: str) -> None:
        raise self._unsupported("аватар")

    def known_user(self, user_id: int) -> dict[str, Any] | None:
        return None
