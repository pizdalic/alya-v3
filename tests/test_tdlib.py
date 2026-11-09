import asyncio
import json
import queue
import tempfile
import unittest
from typing import Any

from alya.core.config import load_settings
from alya.core.errors import ConfigError
from alya.telegram.tdlib.adapter import TdlibAdapter, utf16_slice
from alya.telegram.tdlib.auth import AuthRequired, authorize, tdlib_parameters
from alya.telegram.tdlib.client import TdClient, TdError, raise_for_error
from alya.telegram.transport import FloodWait

ME = 777


def state(name: str) -> dict[str, Any]:
    return {"@type": "updateAuthorizationState",
            "authorization_state": {"@type": "authorizationState" + name}}


class FakeTdJson:
    """Имитация libtdjson: отвечает на запросы и шлёт смену состояний авторизации."""

    def __init__(self) -> None:
        self.q: queue.Queue[str] = queue.Queue()
        self.requests: list[dict[str, Any]] = []
        self.started = False

    def create_client_id(self) -> int:
        return 1

    def execute(self, request: str) -> str | None:
        return None

    def receive(self, timeout: float) -> str | None:
        try:
            return self.q.get(timeout=min(timeout, 0.05))
        except queue.Empty:
            return None

    def _push(self, obj: dict[str, Any]) -> None:
        obj["@client_id"] = 1
        self.q.put(json.dumps(obj))

    def send(self, client_id: int, request: str) -> None:
        req = json.loads(request)
        self.requests.append(req)
        kind, extra = req["@type"], req.get("@extra")
        ok: dict[str, Any] = {"@type": "ok", "@extra": extra}
        follow: dict[str, Any] | None = None
        if kind == "getOption":
            ok = {"@type": "optionValueString", "value": "1.8", "@extra": extra}
            if not self.started:
                self.started = True
                follow = state("WaitTdlibParameters")
        elif kind == "setTdlibParameters":
            follow = state("WaitPhoneNumber")
        elif kind == "setAuthenticationPhoneNumber":
            follow = state("WaitCode")
        elif kind == "checkAuthenticationCode":
            if req["code"] != "12345":
                ok = {"@type": "error", "code": 400, "message": "PHONE_CODE_INVALID",
                      "@extra": extra}
            else:
                follow = state("WaitPassword")
        elif kind == "checkAuthenticationPassword":
            follow = state("Ready")
        elif kind == "getMe":
            ok = {"@type": "user", "id": ME, "first_name": "Аля",
                  "usernames": {"active_usernames": ["AlysiaNya"]}, "@extra": extra}
        elif kind == "close":
            follow = state("Closed")
        self._push(ok)
        if follow is not None:
            self._push(follow)


def settings(tmp: str):  # type: ignore[no-untyped-def]
    return load_settings(None, env={"ALYA_TELEGRAM__API_ID": "12345",
                                    "ALYA_TELEGRAM__API_HASH": "0123456789abcdef",
                                    "ALYA_TELEGRAM__SESSION_DIR": tmp})


class AuthTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()

    async def asyncTearDown(self) -> None:
        self.tmp.cleanup()

    async def test_full_login_with_wrong_code_and_2fa(self) -> None:
        td = FakeTdJson()
        client = TdClient(td, receive_timeout=0.05)
        answers = {"phone": ["+70000000000"], "code": ["1", "12345"], "password": ["pw"]}
        asked: list[str] = []
        notes: list[str] = []

        async def prompt(kind: str, hint: str) -> str:
            asked.append(kind)
            return answers[kind].pop(0)

        me = await authorize(client, settings(self.tmp.name), prompt, notify=notes.append)
        await client.close(timeout=2)
        self.assertEqual(me["id"], ME)
        self.assertEqual(asked, ["phone", "code", "code", "password"])
        self.assertEqual(len(notes), 1)
        params = next(r for r in td.requests if r["@type"] == "setTdlibParameters")
        self.assertEqual(params["api_id"], 12345)
        self.assertNotIn("parameters", params)  # параметры передаются плоско

    async def test_no_prompt_requires_login(self) -> None:
        client = TdClient(FakeTdJson(), receive_timeout=0.05)
        with self.assertRaises(AuthRequired):
            await authorize(client, settings(self.tmp.name), None)
        await client.close(timeout=2)

    def test_parameters_need_api_credentials(self) -> None:
        with self.assertRaises(ConfigError):
            tdlib_parameters(load_settings(None, env={}))


class ErrorTests(unittest.TestCase):
    def test_flood_and_permanent(self) -> None:
        with self.assertRaises(FloodWait) as ctx:
            raise_for_error({"@type": "error", "code": 429,
                             "message": "Too Many Requests: retry after 7"})
        self.assertEqual(ctx.exception.seconds, 7)
        with self.assertRaises(TdError) as err:
            raise_for_error({"@type": "error", "code": 403, "message": "CHAT_WRITE_FORBIDDEN"})
        self.assertTrue(err.exception.permanent)
        self.assertEqual(raise_for_error({"@type": "ok"}), {"@type": "ok"})


class FakeRequester:
    def __init__(self) -> None:
        self.adapter: TdlibAdapter | None = None
        self.mode = "late"
        self.requests: list[dict[str, Any]] = []

    def _later(self, update: dict[str, Any]) -> None:
        ad = self.adapter
        assert ad is not None
        asyncio.get_running_loop().call_later(
            0.01, lambda: asyncio.ensure_future(ad.handle_update(update)))

    async def request(
        self, query: dict[str, Any], *, timeout: float | None = None
    ) -> dict[str, Any]:
        self.requests.append(query)
        kind = query["@type"]
        if kind == "getChat":
            if query["chat_id"] > 0:
                return {"id": query["chat_id"], "type": {"@type": "chatTypePrivate"}}
            return {"id": query["chat_id"], "title": "группа",
                    "type": {"@type": "chatTypeSupergroup", "is_channel": False}}
        if kind == "getUser":
            return {"id": query["user_id"], "first_name": "Боб", "is_contact": True,
                    "usernames": {"active_usernames": ["bob"]}}
        if kind == "getMessage":
            return {"id": query["message_id"],
                    "sender_id": {"@type": "messageSenderUser", "user_id": ME}}
        if kind == "sendMessage":
            assert self.adapter is not None
            if self.mode == "early":
                self.adapter._on_send_result(1, 5 << 20)
            elif self.mode == "late":
                self._later({"@type": "updateMessageSendSucceeded", "old_message_id": 1,
                             "message": {"id": 6 << 20}})
            else:
                self._later({"@type": "updateMessageSendFailed", "old_message_id": 1,
                             "error": {"code": 403, "message": "FORBIDDEN"}})
            return {"id": 1, "sending_state": {"@type": "messageSendingStatePending"}}
        return {"@type": "ok"}


class AdapterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.req = FakeRequester()
        self.ad = TdlibAdapter(self.req, me_id=ME, send_timeout_s=2)
        self.req.adapter = self.ad

    def test_utf16_slice(self) -> None:
        self.assertEqual(utf16_slice("😀 @AlysiaNya привет", 3, 10), "@AlysiaNya")

    async def test_parse_group_message(self) -> None:
        text = "😀 @AlysiaNya привет"
        m = await self.ad.parse({
            "id": 10 << 20, "chat_id": -100, "date": 1_700_000_000, "is_outgoing": False,
            "sender_id": {"@type": "messageSenderUser", "user_id": 202},
            "reply_to": {"@type": "messageReplyToMessage", "chat_id": -100, "message_id": 9 << 20},
            "content": {"@type": "messageText", "text": {"text": text, "entities": [
                {"offset": 3, "length": 10, "type": {"@type": "textEntityTypeMention"}},
                {"offset": 14, "length": 6,
                 "type": {"@type": "textEntityTypeMentionName", "user_id": 303}}]}}})
        assert m is not None
        self.assertEqual(m.chat_kind, "supergroup")
        self.assertEqual(m.mentioned_usernames, frozenset({"alysianya"}))
        self.assertEqual(m.mentioned_user_ids, frozenset({303}))
        self.assertEqual(m.reply_to_user_id, ME)
        self.assertEqual((m.sender_name, m.sender_username, m.chat_title), ("Боб", "bob", "группа"))
        self.assertTrue(m.sender_is_contact)

    async def test_parse_sticker_and_skip_pending(self) -> None:
        base = {"id": 1, "chat_id": 202, "date": 0,
                "sender_id": {"@type": "messageSenderUser", "user_id": 202}}
        m = await self.ad.parse({**base, "content": {"@type": "messageSticker",
                                                     "sticker": {"emoji": "😀"}}})
        assert m is not None
        self.assertEqual((m.chat_kind, m.text), ("private", "[стикер 😀]"))
        self.assertIsNone(await self.ad.parse({
            **base, "sending_state": {"@type": "messageSendingStatePending"},
            "content": {"@type": "messageText", "text": {"text": "x", "entities": []}}}))

    async def test_send_result_paths(self) -> None:
        self.req.mode = "early"
        self.assertEqual(await self.ad.send_text(202, "привет"), 5 << 20)
        self.req.mode = "late"
        self.assertEqual(await self.ad.send_text(202, "привет", reply_to=9 << 20), 6 << 20)
        sent = [r for r in self.req.requests if r["@type"] == "sendMessage"][-1]
        self.assertEqual(sent["reply_to"]["message_id"], 9 << 20)
        self.req.mode = "fail"
        with self.assertRaises(TdError) as ctx:
            await self.ad.send_text(202, "привет")
        self.assertTrue(ctx.exception.permanent)

    async def test_new_message_update_reaches_pipeline(self) -> None:
        got: list[Any] = []

        async def on_message(m):  # type: ignore[no-untyped-def]
            got.append(m)

        async def noop(ev):  # type: ignore[no-untyped-def]
            return None

        self.ad.bind(on_message=on_message, on_edit=noop, on_delete=noop)
        await self.ad.handle_update({"@type": "updateNewMessage", "message": {
            "id": 3 << 20, "chat_id": 202, "date": 0,
            "sender_id": {"@type": "messageSenderUser", "user_id": 202},
            "content": {"@type": "messageText", "text": {"text": "привет", "entities": []}}}})
        self.assertEqual([m.text for m in got], ["привет"])
