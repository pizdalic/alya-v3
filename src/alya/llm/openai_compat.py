"""OpenAI-совместимый клиент эмбеддингов (POST {base_url}/embeddings).

Только stdlib (urllib в отдельном потоке). Ключ не попадает ни в исключения, ни в логи.
Повторы: 429 / 5xx / сетевые ошибки, с учётом Retry-After (не больше 30 с).
"""

from __future__ import annotations

import asyncio
import json
import math
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any

import numpy as np

from alya import __version__
from alya.core.errors import AlyaError
from alya.memory.vectors import Vector

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})


class LLMHTTPError(AlyaError):
    def __init__(self, message: str, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def _error_message(body: bytes) -> str:
    try:
        data = json.loads(body)
        msg = data.get("error", {}).get("message") if isinstance(data, dict) else None
    except (ValueError, AttributeError):
        msg = None
    return str(msg)[:200] if msg else ""


class OpenAICompatibleEmbedder:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        *,
        timeout_s: float = 60.0,
        batch_size: int = 64,
        max_retries: int = 2,
        extra_headers: Mapping[str, str] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not model:
            raise ValueError("модель эмбеддингов не задана")
        if batch_size <= 0:
            raise ValueError("batch_size > 0")
        self._url = base_url.rstrip("/") + "/embeddings"
        self._key = api_key
        self._model = model
        self._timeout = timeout_s
        self._batch = batch_size
        self._retries = max_retries
        self._headers = dict(extra_headers or {})
        self._sleep = sleep

    @property
    def model_id(self) -> str:
        return self._model

    async def embed(self, texts: Sequence[str]) -> list[Vector]:
        out: list[Vector] = []
        for start in range(0, len(texts), self._batch):
            chunk = list(texts[start : start + self._batch])
            out.extend(await self._embed_batch(chunk))
        dims = {v.shape[0] for v in out}
        if len(dims) > 1:
            raise LLMHTTPError("провайдер вернул векторы разной размерности")
        return out

    async def _embed_batch(self, texts: list[str]) -> list[Vector]:
        body = {"model": self._model, "input": texts, "encoding_format": "float"}
        attempt = 0
        while True:
            try:
                data = await asyncio.to_thread(self._post, body)
                return self._parse(data, len(texts))
            except _Retryable as exc:
                if attempt >= self._retries:
                    raise LLMHTTPError(exc.message, exc.status) from None
                delay = exc.retry_after if exc.retry_after is not None else 0.5 * 2**attempt
                await self._sleep(min(30.0, max(0.0, delay)))
                attempt += 1

    def _post(self, body: dict[str, Any]) -> Any:
        headers = {"Content-Type": "application/json", "User-Agent": f"alya/{__version__}",
                   **self._headers}
        if self._key:
            headers["Authorization"] = f"Bearer {self._key}"
        req = urllib.request.Request(self._url, data=json.dumps(body).encode(), headers=headers,
                                     method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # noqa: S310
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _error_message(exc.read())
            msg = f"HTTP {exc.code} от провайдера эмбеддингов" + (f": {detail}" if detail else "")
            if exc.code in RETRY_STATUSES:
                raise _Retryable(msg, exc.code, _retry_after(exc.headers.get("Retry-After")))
            raise LLMHTTPError(msg, exc.code) from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise _Retryable(f"сетевая ошибка: {type(exc).__name__}", None, None) from None
        try:
            return json.loads(raw)
        except ValueError:
            raise LLMHTTPError("ответ провайдера — не JSON") from None

    @staticmethod
    def _parse(data: Any, expected: int) -> list[Vector]:
        items = data.get("data") if isinstance(data, dict) else None
        if not isinstance(items, list) or len(items) != expected:
            raise LLMHTTPError("неожиданный формат ответа эмбеддингов")
        try:
            ordered = sorted(items, key=lambda it: int(it["index"]))
            vectors = [np.asarray(it["embedding"], dtype=np.float32) for it in ordered]
        except (KeyError, TypeError, ValueError):
            raise LLMHTTPError("неожиданный формат ответа эмбеддингов") from None
        if [int(it["index"]) for it in ordered] != list(range(expected)):
            raise LLMHTTPError("индексы эмбеддингов не совпадают с запросом")
        for v in vectors:
            if v.ndim != 1 or v.size == 0 or not np.all(np.isfinite(v)):
                raise LLMHTTPError("некорректный вектор в ответе")
        return vectors


class _Retryable(Exception):
    def __init__(self, message: str, status: int | None, retry_after: float | None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.retry_after = retry_after


def _retry_after(value: str | None) -> float | None:
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        return None
    return seconds if math.isfinite(seconds) else None
