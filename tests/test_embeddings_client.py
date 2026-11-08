import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

from alya.llm.openai_compat import LLMHTTPError, OpenAICompatibleEmbedder

KEY = "sk-test-secret-123"


class Handler(BaseHTTPRequestHandler):
    script: list[tuple[int, Any, dict[str, str]]] = []
    seen: list[dict[str, Any]] = []

    def do_POST(self) -> None:  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        Handler.seen.append({"path": self.path, "auth": self.headers.get("Authorization"),
                             "body": body})
        status, payload, headers = Handler.script.pop(0)
        if payload == "ECHO":
            payload = {"data": [{"index": i, "embedding": [float(len(t)), 1.0]}
                                for i, t in reversed(list(enumerate(body["input"])))]}
        raw = json.dumps(payload).encode()
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args: Any) -> None:
        pass


class EmbedderClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        Handler.script, Handler.seen = [], []
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.sleeps: list[float] = []

        async def fake_sleep(s: float) -> None:
            self.sleeps.append(s)

        self.client = OpenAICompatibleEmbedder(
            f"http://127.0.0.1:{self.server.server_port}/api/v1/", KEY, "emb-model",
            batch_size=2, max_retries=2, sleep=fake_sleep, timeout_s=5)

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    async def test_batches_order_and_auth(self) -> None:
        Handler.script = [(200, "ECHO", {}), (200, "ECHO", {})]
        vecs = await self.client.embed(["a", "bbb", "cc"])
        self.assertEqual([float(v[0]) for v in vecs], [1.0, 3.0, 2.0])
        self.assertEqual(Handler.seen[0]["path"], "/api/v1/embeddings")
        self.assertEqual(Handler.seen[0]["auth"], f"Bearer {KEY}")
        self.assertEqual(Handler.seen[0]["body"]["model"], "emb-model")
        self.assertEqual(len(Handler.seen), 2)

    async def test_retry_after(self) -> None:
        Handler.script = [(429, {"error": {"message": "slow"}}, {"Retry-After": "120"}),
                          (503, {}, {}), (200, "ECHO", {})]
        vecs = await self.client.embed(["x"])
        self.assertEqual(len(vecs), 1)
        self.assertEqual(self.sleeps, [30.0, 1.0])

    async def test_errors_hide_key(self) -> None:
        Handler.script = [(401, {"error": {"message": "bad key " + "z" * 500}}, {})]
        with self.assertRaises(LLMHTTPError) as cm:
            await self.client.embed(["x"])
        self.assertEqual(cm.exception.status, 401)
        self.assertNotIn(KEY, str(cm.exception))
        self.assertLess(len(str(cm.exception)), 260)

    async def test_bad_payloads(self) -> None:
        for payload in ({"data": []}, {"data": [{"index": 5, "embedding": [1.0]}]},
                        {"data": [{"index": 0, "embedding": []}]}, {"nope": 1}):
            Handler.script = [(200, payload, {})]
            with self.subTest(payload=payload), self.assertRaises(LLMHTTPError):
                await self.client.embed(["x"])

    async def test_gives_up(self) -> None:
        Handler.script = [(500, {}, {})] * 3
        with self.assertRaises(LLMHTTPError) as cm:
            await self.client.embed(["x"])
        self.assertEqual(cm.exception.status, 500)
        self.assertEqual(len(self.sleeps), 2)


if __name__ == "__main__":
    unittest.main()
