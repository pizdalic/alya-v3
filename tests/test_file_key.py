"""TDLib с переименованным полем файла: адаптер узнаёт имя и повторяет отправку."""

from __future__ import annotations

import unittest
from typing import Any

from alya.telegram.tdlib.adapter import TdlibAdapter
from alya.telegram.tdlib.client import TdError
from alya.telegram.transport import TransportError
from tests.test_tdlib import ME, FakeRequester

SCHEMA = {"inputMessagePhoto": "photo_file", "inputMessageSticker": "sticker_file",
          "inputMessageDocument": "document_file", "inputMessageVoiceNote": "voice_note_file",
          "inputChatPhotoStatic": "photo_file"}


def parse_file(obj: dict[str, Any], schema: dict[str, str]) -> None:
    """Как JSON-парсер TDLib: читает только известное поле, лишние ключи игнорирует."""
    key = schema[obj["@type"]]
    file = obj.get(key)
    if file is None:
        raise TdError(400, "InputFile is not specified")
    if not str(file.get("@type", "")).startswith("inputFile") or "AlyaKey" in file["@type"]:
        raise TdError(400, f'Failed to parse JSON object as TDLib request: Unknown class "{file["@type"]}"')


class RenamedTd(FakeRequester):
    def __init__(self, schema: dict[str, str] | None = None) -> None:
        super().__init__()
        self.schema = SCHEMA if schema is None else schema
        self.sent: list[dict[str, Any]] = []

    async def request(self, query: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        if query["@type"] == "sendMessage":
            content = query["input_message_content"]
            if content["@type"] in self.schema:
                try:
                    parse_file(content, self.schema)
                except TdError:
                    self.requests.append(query)
                    raise
                self.sent.append(content)  # успешный sendMessage запишет родитель
        if query["@type"] == "setProfilePhoto":
            self.requests.append(query)
            parse_file(query["photo"], self.schema)
            self.sent.append(query["photo"])
            return {"@type": "ok"}
        return await super().request(query, timeout=timeout)


class FileKeyTests(unittest.IsolatedAsyncioTestCase):
    def adapter(self, req: RenamedTd) -> TdlibAdapter:
        ad = TdlibAdapter(req, me_id=ME, send_timeout_s=2)
        req.adapter = ad
        return ad

    async def test_photo_discovers_key_and_retries(self) -> None:
        req = RenamedTd()
        ad = self.adapter(req)
        await ad.send_photo(5, "/tmp/a.jpg", caption="привет")
        self.assertEqual(req.sent[-1]["photo_file"], {"@type": "inputFileLocal", "path": "/tmp/a.jpg"})
        self.assertEqual(req.sent[-1]["caption"]["text"], "привет")
        n = len(req.requests)
        await ad.send_photo(5, "/tmp/b.jpg")
        self.assertEqual(len(req.requests), n + 1)  # имя запомнено, без повторной пробы

    async def test_sticker_and_voice(self) -> None:
        req = RenamedTd()
        ad = self.adapter(req)
        await ad._send_sticker_step(5, {"@type": "inputFileId", "id": 7}, None, "x", "inputFileId")
        self.assertIn("sticker_file", req.sent[-1])
        await ad.send_voice(5, "/tmp/v.ogg", 3)
        self.assertIn("voice_note_file", req.sent[-1])
        self.assertEqual(req.sent[-1]["duration"], 3)

    async def test_avatar(self) -> None:
        req = RenamedTd()
        ad = self.adapter(req)
        await ad.set_avatar("/tmp/a.jpg")
        self.assertIn("photo_file", req.sent[-1])

    async def test_normal_schema_no_probe(self) -> None:
        req = RenamedTd({"inputMessagePhoto": "photo"})
        ad = self.adapter(req)
        await ad.send_photo(5, "/tmp/a.jpg")
        self.assertEqual(len(req.requests), 1)

    async def test_unknown_name_gives_original_error(self) -> None:
        req = RenamedTd({"inputMessagePhoto": "zzz"})
        ad = self.adapter(req)
        with self.assertRaises(TransportError) as cm:
            await ad.send_photo(5, "/tmp/a.jpg")
        self.assertIn("InputFile is not specified", str(cm.exception))

    async def test_file_keys_report(self) -> None:
        req = RenamedTd()
        ad = self.adapter(req)
        keys = await ad.file_keys()
        self.assertEqual(keys["inputMessagePhoto"], "photo_file")
        self.assertEqual(keys["inputMessageVoiceNote"], "voice_note_file")
