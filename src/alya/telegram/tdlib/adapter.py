"""Адаптер TDLib → модели Telegram-слоя и транспорт отправки."""

from __future__ import annotations

import asyncio
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from alya.core.timeutil import utcnow
from alya.telegram.models import ChatKind, IncomingMessage, MessageEdited, MessagesDeleted
from alya.telegram.sanitize import clean_incoming
from alya.telegram.tdlib.auth import username_of
from alya.telegram.tdlib.client import TdObject, raise_for_error
from alya.telegram.transport import TransportError

log = logging.getLogger(__name__)

_MEDIA = {
    "messagePhoto": ("photo", "[фото]"),
    "messageVideo": ("video", "[видео]"),
    "messageAnimation": ("animation", "[гифка]"),
    "messageDocument": ("document", "[файл]"),
    "messageAudio": ("audio", "[музыка]"),
    "messageVoiceNote": ("voice", "[голосовое]"),
    "messageVideoNote": ("video_note", "[кружок]"),
    "messageSticker": ("sticker", "[стикер]"),
}


class Requester(Protocol):
    async def request(self, query: TdObject, *, timeout: float | None = None) -> TdObject: ...


def utf16_slice(text: str, offset: int, length: int) -> str:
    raw = text.encode("utf-16-le")
    return raw[offset * 2:(offset + length) * 2].decode("utf-16-le", "ignore")


def content_text(content: TdObject) -> tuple[str, list[TdObject], str | None]:
    kind = content.get("@type")
    if kind == "messageText":
        ft = content.get("text") or {}
        return ft.get("text", ""), list(ft.get("entities") or []), None
    if kind in _MEDIA:
        media, label = _MEDIA[kind]
        caption = content.get("caption") or {}
        text = caption.get("text", "")
        if kind == "messageSticker":
            emoji = (content.get("sticker") or {}).get("emoji", "")
            label = f"[стикер {emoji}]" if emoji else label
        return (text or label), list(caption.get("entities") or []) if text else [], media
    return "", [], None


class TdlibAdapter:
    def __init__(self, client: Requester, *, me_id: int, send_timeout_s: float = 60.0) -> None:
        self._client = client
        self._me = me_id
        self._send_timeout = send_timeout_s
        self._users: dict[int, TdObject] = {}
        self._chats: dict[int, TdObject] = {}
        self._reply_senders: OrderedDict[tuple[int, int], int | None] = OrderedDict()
        self._waiting: dict[int, asyncio.Future[int]] = {}
        self._early: OrderedDict[int, int | TransportError] = OrderedDict()
        self._on_message: Callable[[IncomingMessage], Awaitable[None]] | None = None
        self._on_edit: Callable[[MessageEdited], Awaitable[None]] | None = None
        self._on_delete: Callable[[MessagesDeleted], Awaitable[None]] | None = None

    def bind(self, *, on_message: Callable[[IncomingMessage], Awaitable[None]],
             on_edit: Callable[[MessageEdited], Awaitable[None]] | None = None,
             on_delete: Callable[[MessagesDeleted], Awaitable[None]] | None = None) -> None:
        self._on_message, self._on_edit, self._on_delete = on_message, on_edit, on_delete

    # --- входящие ----------------------------------------------------------

    async def handle_update(self, update: TdObject) -> None:
        kind = update.get("@type")
        if kind == "updateUser":
            user = update["user"]
            self._users[int(user["id"])] = user
        elif kind == "updateNewChat":
            chat = update["chat"]
            self._chats[int(chat["id"])] = chat
        elif kind == "updateChatTitle":
            chat = self._chats.get(int(update["chat_id"]))
            if chat is not None:
                chat["title"] = update.get("title")
        elif kind == "updateNewMessage":
            msg = await self.parse(update["message"])
            if msg is not None and self._on_message is not None:
                await self._on_message(msg)
        elif kind == "updateMessageContent":
            text, _, _ = content_text(update.get("new_content") or {})
            if self._on_edit is not None and text:
                await self._on_edit(MessageEdited(int(update["chat_id"]),
                                                  int(update["message_id"]),
                                                  clean_incoming(text), utcnow()))
        elif kind == "updateDeleteMessages":
            if update.get("is_permanent") and not update.get("from_cache") and self._on_delete:
                await self._on_delete(MessagesDeleted(
                    int(update["chat_id"]), tuple(int(i) for i in update["message_ids"])))
        elif kind == "updateMessageSendSucceeded":
            self._on_send_result(int(update["old_message_id"]), int(update["message"]["id"]))
        elif kind == "updateMessageSendFailed":
            err = update.get("error") or {"code": update.get("error_code"),
                                          "message": update.get("error_message")}
            try:
                raise_for_error({"@type": "error", **err})
            except TransportError as exc:
                self._on_send_result(int(update["old_message_id"]), exc)

    async def _get(self, cache: dict[int, TdObject], key: int, query: TdObject) -> TdObject | None:
        obj = cache.get(key)
        if obj is None:
            try:
                obj = await self._client.request(query)
            except TransportError:
                return None
            cache[key] = obj
        return obj

    async def chat_kind(self, chat_id: int) -> ChatKind | None:
        chat = await self._get(self._chats, chat_id, {"@type": "getChat", "chat_id": chat_id})
        ctype = (chat or {}).get("type") or {}
        kind = ctype.get("@type")
        if kind == "chatTypePrivate":
            return "private"
        if kind == "chatTypeBasicGroup":
            return "group"
        if kind == "chatTypeSupergroup":
            return "channel" if ctype.get("is_channel") else "supergroup"
        return None  # секретные чаты и неизвестное не обрабатываем

    async def _reply_sender(self, chat_id: int, message_id: int) -> int | None:
        key = (chat_id, message_id)
        if key in self._reply_senders:
            return self._reply_senders[key]
        sender: int | None = None
        try:
            msg = await self._client.request({"@type": "getMessage", "chat_id": chat_id,
                                              "message_id": message_id})
            sid = msg.get("sender_id") or {}
            if sid.get("@type") == "messageSenderUser":
                sender = int(sid["user_id"])
        except TransportError:
            sender = None
        self._reply_senders[key] = sender
        while len(self._reply_senders) > 2000:
            self._reply_senders.popitem(last=False)
        return sender

    async def parse(self, m: TdObject) -> IncomingMessage | None:
        if m.get("sending_state") is not None:
            return None  # своё сообщение ещё в пути — учтём после подтверждения
        chat_id = int(m["chat_id"])
        kind = await self.chat_kind(chat_id)
        if kind is None:
            return None
        sid = m.get("sender_id") or {}
        sender_id = int(sid["user_id"]) if sid.get("@type") == "messageSenderUser" else None
        raw, entities, media = content_text(m.get("content") or {})
        if not raw:
            return None
        ids: set[int] = set()
        names: set[str] = set()
        for ent in entities:
            etype = ent.get("type") or {}
            if etype.get("@type") == "textEntityTypeMention":
                word = utf16_slice(raw, int(ent["offset"]), int(ent["length"]))
                names.add(word.lstrip("@").lower())
            elif etype.get("@type") == "textEntityTypeMentionName":
                ids.add(int(etype["user_id"]))
        reply_mid: int | None = None
        reply_to = m.get("reply_to") or {}
        if reply_to.get("@type") == "messageReplyToMessage":
            if int(reply_to.get("chat_id") or chat_id) == chat_id and reply_to.get("message_id"):
                reply_mid = int(reply_to["message_id"])
        elif m.get("reply_to_message_id"):
            reply_mid = int(m["reply_to_message_id"])
        reply_user = await self._reply_sender(chat_id, reply_mid) if reply_mid else None
        user: TdObject = {}
        if sender_id is not None:
            user = await self._get(self._users, sender_id,
                                   {"@type": "getUser", "user_id": sender_id}) or {}
        name = " ".join(p for p in (user.get("first_name"), user.get("last_name")) if p).strip()
        chat = self._chats.get(chat_id) or {}
        return IncomingMessage(
            chat_id=chat_id, message_id=int(m["id"]), sender_id=sender_id, chat_kind=kind,
            text=clean_incoming(raw), date=datetime.fromtimestamp(int(m.get("date") or 0), UTC),
            sender_name=name, sender_username=username_of(user) if user else None,
            is_outgoing=bool(m.get("is_outgoing")), reply_to_message_id=reply_mid,
            reply_to_user_id=reply_user, mentioned_user_ids=frozenset(ids),
            mentioned_usernames=frozenset(names), media_kind=media,
            chat_title=chat.get("title") if kind != "private" else None,
            sender_is_contact=bool(user.get("is_contact")))

    # --- исходящие ---------------------------------------------------------

    def _on_send_result(self, old_id: int, result: int | TransportError) -> None:
        fut = self._waiting.pop(old_id, None)
        if fut is None:
            self._early[old_id] = result  # подтверждение пришло раньше, чем мы стали ждать
            while len(self._early) > 1000:
                self._early.popitem(last=False)
            return
        if not fut.done():
            if isinstance(result, TransportError):
                fut.set_exception(result)
            else:
                fut.set_result(result)

    async def send_text(self, chat_id: int, text: str, *, reply_to: int | None = None) -> int:
        query: TdObject = {
            "@type": "sendMessage", "chat_id": chat_id,
            "input_message_content": {
                "@type": "inputMessageText",
                "text": {"@type": "formattedText", "text": text, "entities": []},
            },
        }
        if reply_to:
            query["reply_to"] = {"@type": "inputMessageReplyToMessage", "message_id": reply_to}
        temp = await self._client.request(query)
        old_id = int(temp["id"])
        if temp.get("sending_state") is None:
            return old_id
        result = self._early.pop(old_id, None)
        if result is None:
            fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
            self._waiting[old_id] = fut
            try:
                result = await asyncio.wait_for(fut, self._send_timeout)
            except TimeoutError:
                self._waiting.pop(old_id, None)
                log.warning("нет подтверждения отправки в чат %s — считаем отправленным", chat_id)
                return old_id
        if isinstance(result, TransportError):
            raise result
        return result

    async def send_typing(self, chat_id: int) -> None:
        await self._client.request({"@type": "sendChatAction", "chat_id": chat_id,
                                    "action": {"@type": "chatActionTyping"}})

    async def mark_read(self, chat_id: int, message_ids: Sequence[int]) -> None:
        if message_ids:
            await self._client.request({"@type": "viewMessages", "chat_id": chat_id,
                                        "message_ids": list(message_ids), "force_read": True})

    def known_user(self, user_id: int) -> dict[str, Any] | None:
        return self._users.get(user_id)
