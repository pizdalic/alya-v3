import asyncio
import json
import logging
import unittest

from alya.observability.health import CheckResult, HealthRegistry, HealthServer
from alya.observability.logs import REDACTED, JsonFormatter, Redactor


class RedactionTests(unittest.TestCase):
    def test_patterns(self) -> None:
        r = Redactor()
        out = r("key sk-or-v1-0123456789abcdef0123 tok 123456789:AAAbbbCCCdddEEEfffGGGhhhIIIjjjKKK")
        self.assertNotIn("sk-or-v1-0123", out)
        self.assertNotIn("AAAbbb", out)
        self.assertIn("api_hash=" + REDACTED, r("api_hash=deadbeef"))
        self.assertIn(REDACTED, r("Authorization: Bearer abc.def"))

    def test_literal_secrets(self) -> None:
        r = Redactor(["my-literal-secret", "abc"])  # короткие не трогаем
        self.assertEqual(r("x my-literal-secret abc"), f"x {REDACTED} abc")

    def test_json_formatter_valid_and_redacted(self) -> None:
        fmt = JsonFormatter(Redactor(["topsecretvalue"]))
        rec = logging.makeLogRecord({"msg": "token=%s", "args": ("topsecretvalue",),
                                     "levelname": "INFO", "name": "t", "chat_id": 5})
        data = json.loads(fmt.format(rec))
        self.assertEqual(data["chat_id"], 5)
        self.assertNotIn("topsecretvalue", json.dumps(data))


async def http_get(port: int, path: str, method: str = "GET") -> tuple[int, dict[str, object]]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"{method} {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    raw = await reader.read()
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


class HealthTests(unittest.IsolatedAsyncioTestCase):
    async def test_registry(self) -> None:
        reg = HealthRegistry(check_timeout_s=0.05)

        async def ok() -> CheckResult:
            return CheckResult(True)

        async def slow() -> CheckResult:
            await asyncio.sleep(1)
            return CheckResult(True)

        async def broken() -> CheckResult:
            raise ValueError("x")

        reg.add("ok", ok)
        self.assertEqual((await reg.run())["status"], "ok")
        reg.add("slow", slow)
        reg.add("broken", broken)
        report = await reg.run()
        self.assertEqual(report["status"], "fail")
        self.assertEqual(report["checks"]["slow"]["detail"], "timeout")
        self.assertEqual(report["checks"]["broken"]["detail"], "ValueError")

    async def test_http(self) -> None:
        state = {"ok": True}

        async def check() -> CheckResult:
            return CheckResult(state["ok"])

        reg = HealthRegistry()
        reg.add("c", check)
        server = HealthServer(reg, "127.0.0.1", 0)
        await server.start()
        try:
            self.assertEqual((await http_get(server.port, "/health"))[0], 200)
            state["ok"] = False
            self.assertEqual((await http_get(server.port, "/health"))[0], 503)
            self.assertEqual((await http_get(server.port, "/live"))[0], 200)
            self.assertEqual((await http_get(server.port, "/nope"))[0], 404)
            self.assertEqual((await http_get(server.port, "/health", "POST"))[0], 405)
        finally:
            await server.stop()


if __name__ == "__main__":
    unittest.main()
