import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from alya.llm.chat import OpenAICompatibleChat
from alya.llm.openai_compat import LLMHTTPError

KEY = "sk-chat-secret-456"


class Handler(BaseHTTPRequestHandler):
    script: list[tuple[int, Any]] = []
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                             "body": body})
        status, payload = Handler.script.pop(0)
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:
        pass


def ok(content: Any) -> tuple[int, Any]:
    return 200, {"choices": [{"message": {"role": "assistant", "content": content}}]}


class ChatClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        Handler.script, Handler.seen = [], []
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            self.sleeps.append(s)

        self.client = OpenAICompatibleChat(
            f"http://127.0.0.1:{self.server.server_port}/api/v1", KEY, "chat-model",
            max_retries=2, sleep=fake_sleep, timeout_s=5)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    async def test_request_shape(self) -> None:
        Handler.script = [ok("привет")]
        out = await self.client.complete([{"role": "user", "content": "хай"}], json_mode=True,
                                         temperature=0.3, max_tokens=50)
        self.assertEqual(out, "привет")
        seen = Handler.seen[0]
        self.assertEqual(seen["path"], "/api/v1/chat/completions")
        self.assertEqual(seen["auth"], f"Bearer {KEY}")
        self.assertEqual(seen["body"]["model"], "chat-model")
        self.assertEqual(seen["body"]["response_format"], {"type": "json_object"})
        self.assertEqual(seen["body"]["max_tokens"], 50)

    async def test_parts_and_retry(self) -> None:
        Handler.script = [(503, {"error": {"message": "busy"}}),
                          ok([{"type": "text", "text": "а"}, {"type": "text", "text": "б"}])]
        self.assertEqual(await self.client.complete([{"role": "user", "content": "x"}]), "аб")
        self.assertEqual(self.sleeps, [0.5])
        self.assertNotIn("response_format", Handler.seen[0]["body"])

    async def test_errors_hide_key(self) -> None:
        Handler.script = [(401, {"error": {"message": "bad key"}})]
        with self.assertRaises(LLMHTTPError) as cm:
            await self.client.complete([{"role": "user", "content": "x"}])
        self.assertEqual(cm.exception.status, 401)
        self.assertNotIn(KEY, str(cm.exception))
        Handler.script = [ok("   ")]
        with self.assertRaises(LLMHTTPError):
            await self.client.complete([{"role": "user", "content": "x"}])
        Handler.script = [(200, {"choices": []})]
        with self.assertRaises(LLMHTTPError):
            await self.client.complete([{"role": "user", "content": "x"}])
