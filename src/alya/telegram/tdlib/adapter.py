"""Адаптер TDLib → модели Telegram-слоя и транспорт отправки."""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections import OrderedDict
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from alya.core.timeutil import utcnow
from alya.media.models import MediaRef
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


_MAX_PHOTO_PICK = 10 * 1024 * 1024


def _file(obj: TdObject | None) -> tuple[int | None, int]:
    f = obj or {}
    if not f.get("id"):
        return None, 0
    return int(f["id"]), int(f.get("size") or f.get("expected_size") or 0)


def media_ref(content: TdObject) -> MediaRef | None:
    """Вложение TDLib → MediaRef (только метаданные, без скачивания)."""
    kind = content.get("@type")
    if kind == "messagePhoto":
        sizes = [s for s in ((content.get("photo") or {}).get("sizes") or []) if s.get("photo")]
        if not sizes:
            return MediaRef("photo")
        sizes.sort(key=lambda s: int(s.get("width") or 0) * int(s.get("height") or 0))
        fitting = [s for s in sizes if _file(s["photo"])[1] <= _MAX_PHOTO_PICK]
        fid, size = _file((fitting or sizes)[-1]["photo"])
        return MediaRef("photo", fid, size, "image/jpeg")
    if kind == "messageDocument":
        doc = content.get("document") or {}
        fid, size = _file(doc.get("document"))
        return MediaRef("document", fid, size, str(doc.get("mime_type") or ""))
    if kind == "messageVoiceNote":
        v = content.get("voice_note") or {}
        fid, size = _file(v.get("voice"))
        return MediaRef("voice", fid, size, str(v.get("mime_type") or "audio/ogg"),
                        int(v.get("duration") or 0))
    if kind == "messageVideoNote":
        v = content.get("video_note") or {}
        fid, size = _file(v.get("video"))
        return MediaRef("video_note", fid, size, "video/mp4", int(v.get("duration") or 0))
    if kind == "messageSticker":
        st = content.get("sticker") or {}
        fid, size = _file(st.get("sticker"))
        set_id = st.get("set_id")
        return MediaRef("sticker", fid, size, emoji=str(st.get("emoji") or ""),
                        set_id=int(set_id) if set_id else None)
    simple = {"messageVideo": ("video", "video"), "messageAnimation": ("animation", "animation"),
              "messageAudio": ("audio", "audio")}
    if kind in simple:
        media, key = simple[kind]
        obj = content.get(key) or {}
        fid, size = _file(obj.get(key))
        return MediaRef(media, fid, size, str(obj.get("mime_type") or ""),  # type: ignore[arg-type]
                        int(obj.get("duration") or 0))
    return None


# В сборке TDLib у владельца поле файла (photo/sticker/document) не читается: проба с неизвестным
# классом файла не даёт ошибки разбора. Значит, поле называется иначе. Имя узнаём у самого
# TDLib: шлём запрос, где каждый кандидат содержит свой выдуманный класс; парсер падает на первом
# настоящем поле и называет его класс. Сообщение при этом не отправляется.
_FILE_KEY_CANDIDATES = (
    "photo", "sticker", "document", "voice_note", "animation", "video", "audio", "voice",
    "file", "input_file", "media", "input_media", "content", "source", "data", "image",
    "local_file", "uploaded_file", "file_to_send", "attachment",
    "input_photo", "input_sticker", "input_document", "input_voice_note",
    "photo_file", "sticker_file", "document_file", "voice_note_file", "photo_input_file",
)
_KEY_PROBE = "inputFileAlyaKey_"
_KEY_FOUND = re.compile(r'Unknown class "inputFileAlyaKey_(\w+)"')
_NO_FILE = "InputFile is not specified"
# В новой схеме TDLib файл лежит внутри объекта-обёртки:
# inputMessagePhoto.photo -> inputPhoto.photo -> InputFile
# (и так же для стикера, документа, голосового).
# В старой — InputFile прямо в поле. Пробуем новую, при ошибке формата — старую, и запоминаем.
_WRAPPERS = {"inputMessagePhoto": "inputPhoto", "inputMessageSticker": "inputSticker",
             "inputMessageVoiceNote": "inputVoiceNote", "inputMessageDocument": "inputDocument"}
_MEDIA_FIELD = {"inputMessagePhoto": "photo", "inputMessageSticker": "sticker",
                "inputMessageVoiceNote": "voice_note", "inputMessageDocument": "document"}


def _format_error(exc: Exception) -> bool:
    text = str(exc)
    return _NO_FILE in text or 'Unknown class "input' in text


def _local(path: str) -> TdObject:
    return {"@type": "inputFileLocal", "path": path}


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


def _read_limited(path: str, max_bytes: int) -> bytes:
    with open(path, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise TransportError("файл слишком большой", permanent=True)
    return data


class TdlibAdapter:
    def __init__(self, client: Requester, *, me_id: int, send_timeout_s: float = 60.0) -> None:
        self._client = client
        self._me = me_id
        self._send_timeout = send_timeout_s
        self._file_keys: dict[str, str | None] = {}
        self._media_mode: dict[str, str] = {}
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
        content = m.get("content") or {}
        ref = media_ref(content) if media else None
        caption = (clean_incoming(((content.get("caption") or {}).get("text") or ""))
                   if media else "")
        return IncomingMessage(
            chat_id=chat_id, message_id=int(m["id"]), sender_id=sender_id, chat_kind=kind,
            text=clean_incoming(raw), date=datetime.fromtimestamp(int(m.get("date") or 0), UTC),
            sender_name=name, sender_username=username_of(user) if user else None,
            is_outgoing=bool(m.get("is_outgoing")), reply_to_message_id=reply_mid,
            reply_to_user_id=reply_user, mentioned_user_ids=frozenset(ids),
            mentioned_usernames=frozenset(names), media_kind=media,
            chat_title=chat.get("title") if kind != "private" else None,
            sender_is_contact=bool(user.get("is_contact")), media=ref, caption=caption)

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
        return await self._send(chat_id, {
            "@type": "inputMessageText",
            "text": {"@type": "formattedText", "text": text, "entities": []},
        }, reply_to)

    async def send_voice(self, chat_id: int, path: str, duration_s: int, *,
                         reply_to: int | None = None) -> int:
        return await self._send_media(chat_id, "inputMessageVoiceNote", "voice_note", _local(path),
                                      {}, reply_to,
                                      inner={"duration": int(duration_s), "waveform": ""})

    async def send_photo(self, chat_id: int, path: str, *, caption: str = "",
                         reply_to: int | None = None) -> int:
        extra: TdObject = {}
        if caption:
            extra["caption"] = {"@type": "formattedText", "text": caption, "entities": []}
        return await self._send_media(chat_id, "inputMessagePhoto", "photo", _local(path),
                                      extra, reply_to, inner={"width": 0, "height": 0})

    async def send_sticker(self, chat_id: int, remote_id: str, *,
                           reply_to: int | None = None) -> int:
        remote_id = remote_id.strip()
        if not remote_id:
            raise TransportError("у стикера нет идентификатора", permanent=True)
        short = remote_id[:16] + "…"

        # шаг 1: remote_id -> файл TDLib
        try:
            file = await self._client.request({
                "@type": "getRemoteFile", "remote_file_id": remote_id,
                "file_type": {"@type": "fileTypeSticker"}})
        except TransportError as exc:
            log.warning("стикер %s: getRemoteFile отклонён: %s", short, exc)
            raise
        file_id = file.get("id")
        if not isinstance(file_id, int) or file_id <= 0:
            log.warning("стикер %s: getRemoteFile вернул %s без id файла, шлю по remote_id",
                        short, file.get("@type"))
            return await self._send_sticker_step(
                chat_id, {"@type": "inputFileRemote", "id": remote_id}, reply_to, short,
                "inputFileRemote")
        log.info("стикер %s: getRemoteFile ok, file_id=%s", short, file_id)

        # шаг 2: sendMessage с inputFileId
        try:
            return await self._send_sticker_step(
                chat_id, {"@type": "inputFileId", "id": file_id}, reply_to, short, "inputFileId")
        except TransportError as exc:
            if getattr(exc, "code", None) != 400:
                raise  # например 403: скачивание не поможет

        # шаг 3: скачать файл и отправить как inputFileLocal
        path = await self._download_sticker(file_id, short)
        return await self._send_sticker_step(
            chat_id, _local(path), reply_to, short, "inputFileLocal")

    async def _send_sticker_step(self, chat_id: int, sticker: TdObject, reply_to: int | None,
                                 short: str, how: str) -> int:
        try:
            msg_id = await self._send_media(chat_id, "inputMessageSticker", "sticker", sticker,
                                            {"emoji": ""}, reply_to,
                                            inner={"width": 0, "height": 0})
        except TransportError as exc:
            log.warning("стикер %s: sendMessage (%s) отклонён: %s", short, how, exc)
            raise
        log.info("стикер %s: отправлен в чат %s через %s", short, chat_id, how)
        return msg_id

    async def _download_sticker(self, file_id: int, short: str) -> str:
        try:
            f = await self._client.request({"@type": "downloadFile", "file_id": file_id,
                                            "priority": 32, "offset": 0, "limit": 0,
                                            "synchronous": True}, timeout=120)
        except TransportError as exc:
            log.warning("стикер %s: downloadFile отклонён: %s", short, exc)
            raise
        local = f.get("local") or {}
        path = local.get("path")
        if not path or not local.get("is_downloading_completed"):
            log.warning("стикер %s: downloadFile не докачал файл (path=%r)", short, path)
            raise TransportError("стикер не скачался")
        log.info("стикер %s: скачан в %s", short, path)
        return str(path)

    def _media_content(self, msg_type: str, key: str, file: TdObject, extra: TdObject,
                       inner: TdObject, mode: str) -> TdObject:
        if mode == "wrapped":
            wrapper = _WRAPPERS[msg_type]
            inner_key = self._file_keys.get(wrapper) or key
            return {"@type": msg_type, key: {"@type": wrapper, inner_key: file, **inner}, **extra}
        return {"@type": msg_type, key: file, **inner, **extra}

    async def _send_media(self, chat_id: int, msg_type: str, key: str, file: TdObject,
                          extra: TdObject, reply_to: int | None,
                          inner: TdObject | None = None) -> int:
        """sendMessage с файлом: сначала формат с обёрткой, при ошибке формата — старый."""
        inner = inner or {}
        cached = self._media_mode.get(msg_type)
        if cached:
            modes = [cached]
        else:
            modes = ["wrapped", "flat"] if msg_type in _WRAPPERS else ["flat"]
        first: TransportError | None = None
        for mode in modes:
            content = self._media_content(msg_type, key, file, extra, inner, mode)
            try:
                msg_id = await self._send(chat_id, content, reply_to)
            except TransportError as exc:
                log.warning("%s (%s): sendMessage отклонён: %s; input_message_content=%s",
                            msg_type, mode, exc, json.dumps(content, ensure_ascii=False))
                if cached or not _format_error(exc):
                    raise
                first = first or exc
                continue
            if not cached:
                self._media_mode[msg_type] = mode
                log.info("TDLib %s: формат %s", msg_type, mode)
            return msg_id
        assert first is not None
        raise first

    async def _discover_file_key(self, cls: str,
                                 build: Callable[[TdObject], TdObject]) -> str | None:
        """Имя поля InputFile в классе cls для текущей сборки TDLib (или None)."""
        probe: TdObject = {k: {"@type": _KEY_PROBE + k} for k in _FILE_KEY_CANDIDATES}
        found: str | None = None
        try:
            await self._client.request(build(probe))
            log.warning("TDLib %s: проба поля файла неожиданно принята", cls)
        except TransportError as exc:
            m = _KEY_FOUND.search(str(exc))
            if m:
                found = m.group(1)
                log.info("TDLib %s: поле файла называется %r", cls, found)
            else:
                log.warning("TDLib %s: поле файла не найдено: %s", cls, exc)
        self._file_keys[cls] = found
        return found

    async def file_keys(self) -> dict[str, str | None]:
        """Имя поля файла внутри каждой обёртки (для /tdprobe)."""
        out: dict[str, str | None] = {}
        for cls, wrapper in _WRAPPERS.items():
            key = _MEDIA_FIELD[cls]
            out[wrapper] = await self._discover_file_key(
                wrapper, lambda probe, cls=cls, key=key, wrapper=wrapper: {
                    "@type": "sendMessage", "chat_id": self._me,
                    "input_message_content": {"@type": cls, key: {"@type": wrapper, **probe}}})
        return out

    async def td_version(self) -> str:
        try:
            opt = await self._client.request({"@type": "getOption", "name": "version"})
        except TransportError:
            return "?"
        return str(opt.get("value") or "?")

    async def probe_files(self, chat_id: int) -> list[str]:
        """Диагностика формата файлов. Пути несуществующие — ничего настоящего не уйдёт."""
        missing: TdObject = {"@type": "inputFileLocal", "path": "/nonexistent/alya-probe.jpg"}

        def wrap(cls: str, file: TdObject) -> TdObject:
            key = _MEDIA_FIELD[cls]
            return {"@type": cls, key: {"@type": _WRAPPERS[cls], key: file}}

        probes: list[tuple[str, TdObject]] = [
            ("A фото в inputPhoto, несуществующий путь", wrap("inputMessagePhoto", missing)),
            ("B фото в inputPhoto, неизвестный класс файла",
             wrap("inputMessagePhoto", {"@type": "inputFileAlyaProbe"})),
            ("C неизвестный класс сообщения", {"@type": "inputMessageAlyaProbe"}),
            ("D документ в inputDocument, несуществующий путь",
             wrap("inputMessageDocument", missing)),
            ("E стикер в inputSticker, несуществующий путь", wrap("inputMessageSticker", missing)),
            ("F голосовое в inputVoiceNote, несуществующий путь",
             wrap("inputMessageVoiceNote", missing)),
            ("G фото без обёртки (старый формат)",
             {"@type": "inputMessagePhoto", "photo": missing}),
        ]
        binding = type(getattr(self._client, '_td', None)).__name__
        out = [f"TDLib {await self.td_version()}, привязка {binding}"]
        for label, content in probes:
            try:
                msg_id = await self._send(chat_id, content, None)
            except TransportError as exc:
                out.append(f"{label}: {exc}")
            else:
                out.append(f"{label}: принято (id {msg_id})")
        for wrapper, key in (await self.file_keys()).items():
            out.append(f"поле файла внутри {wrapper}: {key or 'не найдено'}")
        log.info("tdprobe: %s", " | ".join(out))
        return out

    async def react(self, chat_id: int, message_id: int, emoji: str) -> None:
        await self._client.request({
            "@type": "addMessageReaction", "chat_id": chat_id, "message_id": message_id,
            "reaction_type": {"@type": "reactionTypeEmoji", "emoji": emoji},
            "is_big": False, "update_recent_reactions": True})

    async def download(self, file_id: int, max_bytes: int) -> bytes:
        info = await self._client.request({"@type": "getFile", "file_id": file_id})
        size = int(info.get("size") or info.get("expected_size") or 0)
        if size > max_bytes:
            raise TransportError("файл слишком большой", permanent=True)
        f = await self._client.request({"@type": "downloadFile", "file_id": file_id,
                                        "priority": 16, "offset": 0, "limit": 0,
                                        "synchronous": True}, timeout=180)
        local = f.get("local") or {}
        path = local.get("path")
        if not path or not local.get("is_downloading_completed"):
            raise TransportError("файл не скачался")
        data = await asyncio.to_thread(_read_limited, str(path), max_bytes)
        try:
            await self._client.request({"@type": "deleteFile", "file_id": file_id})
        except TransportError:
            pass  # кэш TDLib чистится и сам
        return data

    async def join_by_link(self, link: str) -> int:
        chat = await self._client.request({"@type": "joinChatByInviteLink", "invite_link": link})
        return int(chat.get("id") or 0)

    async def set_bio(self, text: str) -> None:
        await self._client.request({"@type": "setBio", "bio": text})

    async def set_avatar(self, path: str) -> None:
        def req(key: str, file: TdObject) -> TdObject:
            return {"@type": "setProfilePhoto", "is_public": False,
                    "photo": {"@type": "inputChatPhotoStatic", key: file}}

        key = self._file_keys.get("inputChatPhotoStatic") or "photo"
        try:
            await self._client.request(req(key, _local(path)))
            return
        except TransportError as exc:
            if _NO_FILE not in str(exc) or "inputChatPhotoStatic" in self._file_keys:
                raise
            real = await self._discover_file_key(
                "inputChatPhotoStatic",
                lambda probe: {"@type": "setProfilePhoto", "is_public": False,
                               "photo": {"@type": "inputChatPhotoStatic", **probe}})
            if real is None or real == key:
                raise
        await self._client.request(req(real, _local(path)))

    async def sticker_set(self, name: str) -> list[tuple[str, str]]:
        found = await self._client.request({"@type": "searchStickerSet", "name": name})
        out: list[tuple[str, str]] = []
        for st in found.get("stickers") or []:
            remote = ((st.get("sticker") or {}).get("remote") or {}).get("id")
            if remote:
                out.append((str(remote), str(st.get("emoji") or "")))
        return out

    async def _send(self, chat_id: int, content: TdObject, reply_to: int | None) -> int:
        query: TdObject = {"@type": "sendMessage", "chat_id": chat_id,
                           "input_message_content": content}
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
