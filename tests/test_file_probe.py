"""Диагностика /tdprobe и вырезание текстовых меток медиа."""

from __future__ import annotations

import unittest
from typing import Any

from alya.telegram.sanitize import clean_outgoing
from alya.telegram.tdlib.adapter import TdlibAdapter
from alya.telegram.tdlib.client import TdError
from tests.test_tdlib import ME, FakeRequester


class RejectAll(FakeRequester):
    async def request(self, query: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        if query["@type"] == "sendMessage":
            self.requests.append(query)
            raise TdError(400, "InputFile is not specified")
        if query["@type"] == "getOption":
            return {"@type": "optionValueString", "value": "1.8.70"}
        return await super().request(query, timeout=timeout)


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_reports_every_case(self) -> None:
        req = RejectAll()
        ad = TdlibAdapter(req, me_id=ME, send_timeout_s=2)
        req.adapter = ad
        out = await ad.probe_files(5)
        self.assertEqual(len(out), 6)
        self.assertIn("TDLib 1.8.70", out[0])
        for line, letter in zip(out[1:], "ABCDE"):
            self.assertTrue(line.startswith(letter))
            self.assertIn("InputFile is not specified", line)
        types = [q["input_message_content"]["@type"] for q in req.requests]
        self.assertEqual(types, ["inputMessagePhoto", "inputMessagePhoto", "inputMessageAlyaProbe",
                                 "inputMessageDocument", "inputMessageSticker"])


class MarkerTests(unittest.TestCase):
    def test_sticker_set_line_removed(self) -> None:
        self.assertEqual(clean_outgoing("стикер: LINE_White_Cat_Girl"), [])
        self.assertEqual(clean_outgoing("привет\nстикер: 🐱"), ["привет"])

    def test_bracket_marker_removed(self) -> None:
        self.assertEqual(clean_outgoing("хаха [стикер 🐱]"), ["хаха"])
        self.assertEqual(clean_outgoing("[фото]"), [])

    def test_normal_text_kept(self) -> None:
        self.assertEqual(clean_outgoing("стикер классный"), ["стикер классный"])
        self.assertEqual(clean_outgoing("вот стикер: милый котик"), ["вот стикер: милый котик"])
