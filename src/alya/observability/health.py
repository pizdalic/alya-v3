"""Healthcheck: реестр проверок и минимальный HTTP-сервер (GET /health, GET /live)."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

log = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 8192
READ_TIMEOUT_S = 5.0


@dataclass(frozen=True, slots=True)
class CheckResult:
    ok: bool
    detail: str = ""


type Check = Callable[[], Awaitable[CheckResult]]


class HealthRegistry:
    def __init__(self, check_timeout_s: float = 5.0) -> None:
        self._checks: dict[str, Check] = {}
        self._timeout = check_timeout_s

    def add(self, name: str, check: Check) -> None:
        if name in self._checks:
            raise ValueError(f"проверка уже есть: {name}")
        self._checks[name] = check

    async def run(self) -> dict[str, Any]:
        names = list(self._checks)
        results = await asyncio.gather(*(self._run_one(self._checks[n]) for n in names))
        checks = {n: {"ok": r.ok, "detail": r.detail} for n, r in zip(names, results, strict=True)}
        status = "ok" if all(r.ok for r in results) else "fail"
        return {"status": status, "checks": checks}

    async def _run_one(self, check: Check) -> CheckResult:
        try:
            return await asyncio.wait_for(check(), timeout=self._timeout)
        except TimeoutError:
            return CheckResult(False, "timeout")
        except Exception as exc:  # проверка не должна ронять сервер
            return CheckResult(False, type(exc).__name__)


class HealthServer:
    name = "health"

    def __init__(self, registry: HealthRegistry, host: str, port: int) -> None:
        self._registry = registry
        self._host = host
        self._port = port
        self._server: asyncio.Server | None = None

    @property
    def port(self) -> int:
        """Фактический порт (важно при port=0 в тестах)."""
        if self._server is None or not self._server.sockets:
            return self._port
        return int(self._server.sockets[0].getsockname()[1])

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self._host, self._port)
        log.info("health слушает %s:%s", self._host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            try:
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), READ_TIMEOUT_S)
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                await self._respond(writer, 400, {"error": "bad request"})
                return
            if len(head) > MAX_REQUEST_BYTES:
                await self._respond(writer, 431, {"error": "headers too large"})
                return
            parts = head.split(b"\r\n", 1)[0].decode("latin-1").split(" ")
            if len(parts) != 3 or parts[0] != "GET":
                await self._respond(writer, 405, {"error": "method not allowed"})
                return
            path = parts[1].split("?", 1)[0]
            if path == "/live":
                await self._respond(writer, 200, {"status": "ok"})
            elif path == "/health":
                report = await self._registry.run()
                await self._respond(writer, 200 if report["status"] == "ok" else 503, report)
            else:
                await self._respond(writer, 404, {"error": "not found"})
        except ConnectionError:
            pass
        finally:
            writer.close()

    @staticmethod
    async def _respond(writer: asyncio.StreamWriter, code: int, body: dict[str, Any]) -> None:
        reasons = {200: "OK", 400: "Bad Request", 404: "Not Found", 405: "Method Not Allowed",
                   431: "Request Header Fields Too Large", 503: "Service Unavailable"}
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8")
        head = (
            f"HTTP/1.1 {code} {reasons.get(code, 'Error')}\r\n"
            "Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(payload)}\r\n"
            "Connection: close\r\n\r\n"
        )
        writer.write(head.encode("ascii") + payload)
        await writer.drain()
