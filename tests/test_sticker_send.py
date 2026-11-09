"""Отправка стикера через TDLib: remote_id -> getRemoteFile -> inputFileId."""

from __future__ import annotations

import unittest
from typing import Any

from alya.telegram.tdlib.adapter import TdlibAdapter
from alya.telegram.transport import TransportError
from tests.test_tdlib import ME, FakeRequester


class StickerRequester(FakeRequester):
    def __init__(self, file_id: int | None = 77) -> None:
        super().__init__()
        self.file_id = file_id

    async def request(self, query: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        if query["@type"] == "getRemoteFile":
            self.requests.append(query)
            return {"@type": "file", "id": self.file_id} if self.file_id else {"@type": "ok"}
        return await super().request(query, timeout=timeout)


class StickerSendTests(unittest.IsolatedAsyncioTestCase):
    def _make(self, file_id: int | None = 77) -> tuple[StickerRequester, TdlibAdapter]:
        req = StickerRequester(file_id)
        ad = TdlibAdapter(req, me_id=ME, send_timeout_s=2)
        req.adapter = ad
        return req, ad

    def _sent(self, req: StickerRequester) -> dict[str, Any]:
        sends = [q for q in req.requests if q["@type"] == "sendMessage"]
        self.assertEqual(len(sends), 1)
        return sends[0]["input_message_content"]["sticker"]

    async def test_resolves_remote_id_to_file_id(self) -> None:
        req, ad = self._make()
        await ad.send_sticker(5, " CAACAgIAAxkBAAE ")
        get = [q for q in req.requests if q["@type"] == "getRemoteFile"][0]
        self.assertEqual(get["remote_file_id"], "CAACAgIAAxkBAAE")
        self.assertEqual(get["file_type"], {"@type": "fileTypeSticker"})
        self.assertEqual(self._sent(req), {"@type": "inputFileId", "id": 77})

    async def test_fallback_to_remote(self) -> None:
        req, ad = self._make(None)
        await ad.send_sticker(5, "CAACAgIAAxkBAAE")
        self.assertEqual(self._sent(req), {"@type": "inputFileRemote", "id": "CAACAgIAAxkBAAE"})

    async def test_empty_id_is_permanent(self) -> None:
        req, ad = self._make()
        with self.assertRaises(TransportError) as err:
            await ad.send_sticker(5, "  ")
        self.assertTrue(err.exception.permanent)
        self.assertEqual(req.requests, [])
