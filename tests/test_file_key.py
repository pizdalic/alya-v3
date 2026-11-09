"""Формат медиа для новой (обёртки inputPhoto/inputSticker/...) и старой схемы TDLib."""

from __future__ import annotations

import unittest
from typing import Any

from alya.telegram.tdlib.adapter import TdlibAdapter
from alya.telegram.tdlib.client import TdError
from alya.telegram.transport import TransportError
from tests.test_tdlib import ME, FakeRequester

FIELDS = {"inputMessagePhoto": "photo", "inputMessageSticker": "sticker",
          "inputMessageDocument": "document", "inputMessageVoiceNote": "voice_note"}
NO_FILE = "InputFile is not specified"


def check_file(f: Any) -> None:
    if not isinstance(f, dict):
        raise TdError(400, NO_FILE)
    t = str(f.get("@type", ""))
    if t not in ("inputFileLocal", "inputFileId", "inputFileRemote"):
        raise TdError(400, f'Failed to parse JSON object as TDLib request: Unknown class "{t}"')


def parse(content: dict[str, Any], wrapped: bool) -> None:
    """Как JSON-парсер TDLib: у конкретной обёртки @type не проверяется, читаются поля."""
    key = FIELDS[content["@type"]]
    value = content.get(key)
    if wrapped:
        if not isinstance(value, dict):
            raise TdError(400, NO_FILE)
        check_file(value.get(key))
    else:
        check_file(value)


class SchemaTd(FakeRequester):
    def __init__(self, wrapped: bool, reject_all: bool = False) -> None:
        super().__init__()
        self.wrapped = wrapped
        self.reject_all = reject_all
        self.sent: list[dict[str, Any]] = []

    async def request(self, query: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        if query["@type"] == "sendMessage" and query["input_message_content"]["@type"] in FIELDS:
            content = query["input_message_content"]
            try:
                if self.reject_all:
                    raise TdError(400, NO_FILE)
                parse(content, self.wrapped)
            except TdError:
                self.requests.append(query)
                raise
            self.sent.append(content)  # успешный sendMessage запишет родитель
        return await super().request(query, timeout=timeout)


LOCAL = {"@type": "inputFileLocal", "path": "/tmp/a.jpg"}


class MediaFormatTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, req: SchemaTd) -> TdlibAdapter:
        ad = TdlibAdapter(req, me_id=ME, send_timeout_s=2)
        req.adapter = ad
        return ad

    async def test_new_schema_photo_wrapped(self) -> None:
        req = SchemaTd(wrapped=True)
        ad = self.adapter(req)
        await ad.send_photo(5, "/tmp/a.jpg", caption="привет")
        self.assertEqual(len(req.requests), 1)
        self.assertEqual(req.sent[-1]["photo"], {"@type": "inputPhoto", "photo": LOCAL,
                                                "width": 0, "height": 0})
        self.assertEqual(req.sent[-1]["caption"]["text"], "привет")

    async def test_new_schema_sticker_matches_doc(self) -> None:
        req = SchemaTd(wrapped=True)
        ad = self.adapter(req)
        await ad._send_sticker_step(5, {"@type": "inputFileId", "id": 1240}, None, "x", "inputFileId")
        self.assertEqual(req.sent[-1], {
            "@type": "inputMessageSticker",
            "sticker": {"@type": "inputSticker", "sticker": {"@type": "inputFileId", "id": 1240},
                        "width": 0, "height": 0},
            "emoji": ""})

    async def test_new_schema_voice(self) -> None:
        req = SchemaTd(wrapped=True)
        ad = self.adapter(req)
        await ad.send_voice(5, "/tmp/v.ogg", 3)
        self.assertEqual(req.sent[-1]["voice_note"]["@type"], "inputVoiceNote")
        self.assertEqual(req.sent[-1]["voice_note"]["duration"], 3)

    async def test_old_schema_falls_back_and_remembers(self) -> None:
        req = SchemaTd(wrapped=False)
        ad = self.adapter(req)
        await ad.send_photo(5, "/tmp/a.jpg")
        self.assertEqual(req.sent[-1]["photo"], LOCAL)
        self.assertEqual(req.sent[-1]["width"], 0)
        n = len(req.requests)
        await ad.send_photo(5, "/tmp/a.jpg")
        self.assertEqual(len(req.requests), n + 1)  # формат запомнен

    async def test_both_fail_raise(self) -> None:
        req = SchemaTd(wrapped=True, reject_all=True)
        ad = self.adapter(req)
        with self.assertRaises(TransportError) as cm:
            await ad.send_photo(5, "/tmp/a.jpg")
        self.assertIn(NO_FILE, str(cm.exception))
        self.assertEqual(len(req.requests), 2)

    async def test_file_keys_inside_wrappers(self) -> None:
        req = SchemaTd(wrapped=True)
        ad = self.adapter(req)
        keys = await ad.file_keys()
        self.assertEqual(keys, {"inputPhoto": "photo", "inputSticker": "sticker",
                                "inputVoiceNote": "voice_note", "inputDocument": "document"})
