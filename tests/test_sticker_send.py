"""Отправка стикера через TDLib: getRemoteFile -> inputFileId -> (downloadFile -> inputFileLocal)."""

from __future__ import annotations

import unittest
from typing import Any

from alya.telegram.tdlib.adapter import TdlibAdapter
from alya.telegram.tdlib.client import TdError
from alya.telegram.transport import TransportError
from tests.test_tdlib import ME, FakeRequester

LOG = "alya.telegram.tdlib.adapter"
RID = "CAACAgIAAxkBAAEstickerremoteid"


class StickerRequester(FakeRequester):
    def __init__(self) -> None:
        super().__init__()
        self.remote: dict[str, Any] | TdError = {"@type": "file", "id": 77}
        self.reject: dict[str, TdError] = {}   # тип InputFile -> синхронная ошибка sendMessage
        self.fail_async: set[str] = set()      # тип InputFile -> updateMessageSendFailed 400
        self.download: dict[str, Any] | TdError = {
            "@type": "file", "id": 77,
            "local": {"path": "/tmp/td/stickers/s.webp", "is_downloading_completed": True}}

    async def request(self, query: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        kind = query["@type"]
        if kind in ("getRemoteFile", "downloadFile"):
            self.requests.append(query)
            res = self.remote if kind == "getRemoteFile" else self.download
            if isinstance(res, TdError):
                raise res
            return res
        if kind == "sendMessage":
            how = query["input_message_content"]["sticker"]["@type"]
            if how in self.reject:
                self.requests.append(query)
                raise self.reject[how]
            if how in self.fail_async:
                self.requests.append(query)
                self._later({"@type": "updateMessageSendFailed", "old_message_id": 1,
                             "error": {"code": 400, "message": "InputFile is not specified"}})
                return {"id": 1, "sending_state": {"@type": "messageSendingStatePending"}}
        return await super().request(query, timeout=timeout)

    def sent(self) -> list[dict[str, Any]]:
        return [q["input_message_content"]["sticker"] for q in self.requests
                if q["@type"] == "sendMessage"]


def _input_file_error() -> TdError:
    return TdError(400, "InputFile is not specified")


class StickerSendTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.req = StickerRequester()
        self.ad = TdlibAdapter(self.req, me_id=ME, send_timeout_s=2)
        self.req.adapter = self.ad

    async def test_file_id_path(self) -> None:
        await self.ad.send_sticker(5, " " + RID + " ")
        get = self.req.requests[0]
        self.assertEqual(get["@type"], "getRemoteFile")
        self.assertEqual(get["remote_file_id"], RID)
        self.assertEqual(get["file_type"], {"@type": "fileTypeSticker"})
        self.assertEqual(self.req.sent(), [{"@type": "inputFileId", "id": 77}])

    async def test_sync_reject_falls_back_to_local_file(self) -> None:
        self.req.reject["inputFileId"] = _input_file_error()
        with self.assertLogs(LOG, "INFO") as logs:
            msg = await self.ad.send_sticker(5, RID)
        self.assertEqual(msg, 6 << 20)
        self.assertEqual(self.req.sent(), [
            {"@type": "inputFileId", "id": 77},
            {"@type": "inputFileLocal", "path": "/tmp/td/stickers/s.webp"}])
        dl = [q for q in self.req.requests if q["@type"] == "downloadFile"][0]
        self.assertEqual(dl["file_id"], 77)
        self.assertTrue(dl["synchronous"])
        text = "\n".join(logs.output)
        self.assertIn("sendMessage (inputFileId)", text)
        self.assertIn("через inputFileLocal", text)

    async def test_async_reject_falls_back_too(self) -> None:
        self.req.fail_async.add("inputFileId")
        await self.ad.send_sticker(5, RID)
        self.assertEqual([s["@type"] for s in self.req.sent()], ["inputFileId", "inputFileLocal"])

    async def test_get_remote_file_error_is_logged_separately(self) -> None:
        self.req.remote = TdError(400, "Wrong remote file identifier specified")
        with self.assertLogs(LOG, "WARNING") as logs, self.assertRaises(TransportError):
            await self.ad.send_sticker(5, RID)
        self.assertIn("getRemoteFile отклонён", "\n".join(logs.output))
        self.assertEqual(self.req.sent(), [])

    async def test_no_download_on_forbidden(self) -> None:
        self.req.reject["inputFileId"] = TdError(403, "CHAT_WRITE_FORBIDDEN")
        with self.assertRaises(TransportError):
            await self.ad.send_sticker(5, RID)
        self.assertFalse([q for q in self.req.requests if q["@type"] == "downloadFile"])

    async def test_incomplete_download_fails(self) -> None:
        self.req.reject["inputFileId"] = _input_file_error()
        self.req.download = {"@type": "file", "id": 77,
                             "local": {"path": "", "is_downloading_completed": False}}
        with self.assertLogs(LOG, "WARNING") as logs, self.assertRaises(TransportError):
            await self.ad.send_sticker(5, RID)
        self.assertIn("downloadFile не докачал", "\n".join(logs.output))
        self.assertEqual(len(self.req.sent()), 1)

    async def test_local_reject_is_logged(self) -> None:
        self.req.reject["inputFileId"] = _input_file_error()
        self.req.reject["inputFileLocal"] = _input_file_error()
        with self.assertLogs(LOG, "WARNING") as logs, self.assertRaises(TransportError):
            await self.ad.send_sticker(5, RID)
        self.assertIn("sendMessage (inputFileLocal)", "\n".join(logs.output))

    async def test_no_file_id_uses_remote(self) -> None:
        self.req.remote = {"@type": "ok"}
        await self.ad.send_sticker(5, RID)
        self.assertEqual(self.req.sent(), [{"@type": "inputFileRemote", "id": RID}])

    async def test_empty_id_is_permanent(self) -> None:
        with self.assertRaises(TransportError) as err:
            await self.ad.send_sticker(5, "  ")
        self.assertTrue(err.exception.permanent)
        self.assertEqual(self.req.requests, [])


class StickerShapeTests(unittest.IsolatedAsyncioTestCase):
    async def test_content_is_minimal(self) -> None:
        req = StickerRequester()
        ad = TdlibAdapter(req, me_id=ME, send_timeout_s=2)
        req.adapter = ad
        await ad.send_sticker(5, RID)
        send = [q for q in req.requests if q["@type"] == "sendMessage"][0]
        self.assertEqual(send["input_message_content"],
                         {"@type": "inputMessageSticker", "sticker": {"@type": "inputFileId", "id": 77}})

    async def test_failure_logs_exact_content(self) -> None:
        req = StickerRequester()
        req.reject["inputFileId"] = TdError(403, "CHAT_WRITE_FORBIDDEN")
        ad = TdlibAdapter(req, me_id=ME, send_timeout_s=2)
        req.adapter = ad
        with self.assertLogs(LOG, "WARNING") as logs, self.assertRaises(TransportError):
            await ad.send_sticker(5, RID)
        self.assertIn('input_message_content={"@type": "inputMessageSticker"', "\n".join(logs.output))

    async def test_td_version(self) -> None:
        class V(StickerRequester):
            async def request(self, query, *, timeout=None):  # type: ignore[no-untyped-def]
                if query["@type"] == "getOption":
                    return {"@type": "optionValueString", "value": "1.8.70"}
                return await super().request(query, timeout=timeout)
        ad = TdlibAdapter(V(), me_id=ME, send_timeout_s=2)
        self.assertEqual(await ad.td_version(), "1.8.70")
