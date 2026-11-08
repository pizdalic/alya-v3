"""OpenAI-совместимый чат-клиент (POST {base_url}/chat/completions).

Как и клиент эмбеддингов: только stdlib, ключ не попадает в ошибки и логи,
повторы на 429/5xx/сетевых ошибках с учётом Retry-After (не больше 30 с).
"""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from collections.abc import Awaitable, Callable, Mapping, Sequence
from typing import Any, Protocol

from alya import __version__
from alya.llm.openai_compat import (
    RETRY_STATUSES,
    LLMHTTPError,
    _error_message,
    _Retryable,
    _retry_after,
)

type ChatMessage = Mapping[str, str]


class ChatModel(Protocol):
    @property
    def model_id(self) -> str: ...

    async def complete(self, messages: Sequence[ChatMessage], *, temperature: float = 0.8,
                       max_tokens: int = 400, json_mode: bool = False) -> str: ...


class OpenAICompatibleChat:
    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        *,
        timeout_s: float = 60.0,
        max_retries: int = 2,
        extra_headers: Mapping[str, str] | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if not model:
            raise ValueError("модель чата не задана")
        self._url = base_url.rstrip("/") + "/chat/completions"
        self._key = api_key
        self._model = model
        self._timeout = timeout_s
        self._retries = max_retries
        self._headers = dict(extra_headers or {})
        self._sleep = sleep

    @property
    def model_id(self) -> str:
        return self._model

    async def complete(self, messages: Sequence[ChatMessage], *, temperature: float = 0.8,
                       max_tokens: int = 400, json_mode: bool = False) -> str:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": [dict(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        attempt = 0
        while True:
            try:
                data = await asyncio.to_thread(self._post, body)
                return self._parse(data)
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
            msg = f"HTTP {exc.code} от провайдера чата" + (f": {detail}" if detail else "")
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
    def _parse(data: Any) -> str:
        try:
            message = data["choices"][0]["message"]
            content = message.get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            raise LLMHTTPError("неожиданный формат ответа чата") from None
        if isinstance(content, list):  # некоторые провайдеры отдают части
            content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
        if not isinstance(content, str) or not content.strip():
            raise LLMHTTPError("пустой ответ модели")
        return content
