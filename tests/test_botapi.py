import json
import unittest
import urllib.request
from typing import Any

from alya.telegram.botapi import BotApi, BotApiAdapter
from alya.telegram.transport import FloodWait, TransportError

TOKEN = "123:SECRET-token"
ME = 999


class FakeHttp:
    def __init__(self, *responses: Any) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, req: urllib.request.Request, timeout: float) -> bytes:
        method = req.full_url.rsplit("/", 1)[1]
        self.calls.append((method, json.loads(req.data or b"{}")))  # type: ignore[arg-type]
        item = self.responses.pop(0) if self.responses else {"ok": True, "result": True}
        if isinstance(item, BaseException):
            raise item
        return json.dumps(item).encode()


def ok(result: Any) -> dict[str, Any]:
    return {"ok": True, "result": result}


def tg_msg(**kw: Any) -> dict[str, Any]:
    base = {"message_id": 7, "date": 1_700_000_000, "chat": {"id": 5, "type": "private"},
            "from": {"id": 5, "first_name": "Петя", "username": "petya"}, "text": "привет"}
    base.update(kw)
    return base


class BotApiTests(unittest.IsolatedAsyncioTestCase):
    def make(self, *responses: Any) -> tuple[FakeHttp, BotApiAdapter]:
        http = FakeHttp(*responses)
        return http, BotApiAdapter(BotApi(TOKEN, opener=http), me_id=ME, poll_timeout_s=1)

    def test_parse_mentions_reply_and_utf16(self) -> None:
        _, ad = self.make()
        text = "😀 @AlysiaNya привет"
        m = ad.parse(tg_msg(chat={"id": -100, "type": "supergroup", "title": "чат"}, text=text,
                            entities=[{"type": "mention", "offset": 3, "length": 10},
                                      {"type": "text_mention", "offset": 0, "length": 2,
                                       "user": {"id": 42}}],
                            reply_to_message={"message_id": 3, "from": {"id": ME}}))
        assert m is not None
        self.assertEqual(m.mentioned_usernames, frozenset({"alysianya"}))
        self.assertEqual(m.mentioned_user_ids, frozenset({42}))
        self.assertEqual((m.reply_to_message_id, m.reply_to_user_id), (3, ME))
        self.assertTrue(m.is_group)
        self.assertFalse(m.is_outgoing)
        self.assertEqual(m.sender_name, "Петя")

    def test_parse_caption_media_and_garbage(self) -> None:
        _, ad = self.make()
        m = ad.parse(tg_msg(text=None, photo=[{}], caption="смотри"))
        assert m is not None
        self.assertEqual((m.text, m.media_kind), ("смотри", "photo"))
        self.assertIsNone(ad.parse({"chat": {"id": 1, "type": "weird"}, "message_id": 1}))
        self.assertIsNone(ad.parse({}))

    async def test_send_text_with_reply(self) -> None:
        http, ad = self.make(ok({"message_id": 77}))
        self.assertEqual(await ad.send_text(5, "хай", reply_to=7), 77)
        method, params = http.calls[0]
        self.assertEqual(method, "sendMessage")
        self.assertEqual(params["reply_parameters"]["message_id"], 7)
        self.assertTrue(params["reply_parameters"]["allow_sending_without_reply"])

    async def test_errors_map_and_hide_token(self) -> None:
        _, ad = self.make({"ok": False, "error_code": 429, "parameters": {"retry_after": 12}},
                          {"ok": False, "error_code": 403, "description": f"blocked {TOKEN}"},
                          OSError(f"connect to /bot{TOKEN}/sendMessage failed"))
        with self.assertRaises(FloodWait) as fw:
            await ad.send_text(5, "a")
        self.assertEqual(fw.exception.seconds, 12)
        with self.assertRaises(TransportError) as perm:
            await ad.send_text(5, "a")
        self.assertTrue(perm.exception.permanent)
        with self.assertRaises(TransportError) as net:
            await ad.send_text(5, "a")
        self.assertFalse(net.exception.permanent)
        for exc in (perm.exception, net.exception):
            self.assertNotIn("SECRET", str(exc))

    async def test_poll_delivers_and_advances_offset(self) -> None:
        got: list[Any] = []
        edits: list[Any] = []
        http, ad = self.make(
            ok([{"update_id": 10, "message": tg_msg()},
                {"update_id": 11, "edited_message": tg_msg(text="изм", edit_date=1_700_000_100)},
                {"update_id": 12, "message": {"broken": True}}]),
            ok([]))

        async def on_message(m: Any) -> None:
            got.append(m)

        async def on_edit(e: Any) -> None:
            edits.append(e)

        ad.bind(on_message=on_message, on_edit=on_edit)
        self.assertEqual(await ad.poll_once(), 3)
        await ad.poll_once()
        self.assertEqual(len(got), 1)
        self.assertEqual(edits[0].text, "изм")
        self.assertEqual(http.calls[0][1]["offset"], 0)
        self.assertEqual(http.calls[1][1]["offset"], 13)

    async def test_run_retries_then_stops_on_permanent(self) -> None:
        sleeps: list[float] = []
        _, ad = self.make(OSError("down"), OSError("down"),
                          {"ok": False, "error_code": 429, "parameters": {"retry_after": 3}},
                          ok([]), {"ok": False, "error_code": 401, "description": "Unauthorized"})

        async def sleep(s: float) -> None:
            sleeps.append(s)

        await ad.run(sleep=sleep)
        self.assertEqual(sleeps, [1.0, 2.0, 3.0])

    async def test_unsupported_is_permanent_and_mark_read_noop(self) -> None:
        http, ad = self.make()
        await ad.mark_read(5, [1, 2])
        self.assertEqual(http.calls, [])
        for coro in (ad.send_voice(5, "/x.ogg", 3), ad.join_by_link("t.me/+x"),
                     ad.download(1, 10)):
            with self.assertRaises(TransportError) as cm:
                await coro
            self.assertTrue(cm.exception.permanent)

    async def test_typing_errors_are_swallowed_except_flood(self) -> None:
        _, ad = self.make({"ok": False, "error_code": 400, "description": "bad"},
                          {"ok": False, "error_code": 429, "parameters": {"retry_after": 1}})
        await ad.send_typing(5)
        with self.assertRaises(FloodWait):
            await ad.send_typing(5)
