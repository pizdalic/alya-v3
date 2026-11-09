"""Логирование: JSON или текст, с вырезанием секретов из итоговой строки.

Политика PII: тексты сообщений и памяти в лог не пишутся — только идентификаторы,
длины и типы. Фильтр секретов — страховка, а не разрешение логировать что угодно.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

REDACTED = "[REDACTED]"
MIN_SECRET_LEN = 6

_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"sk-[A-Za-z0-9_\-]{16,}"),  # ключи OpenRouter / OpenAI-совместимых API
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_\-]{30,}\b"),  # токены Bot API
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"),
)
_KEY_VALUE = re.compile(
    r"(?i)\b(api[_-]?hash|api[_-]?key|token|password|secret|authorization)"
    r"(\s*[=:]\s*)([^\s\"',;}]+)"
)

_STD_ATTRS = frozenset(vars(logging.makeLogRecord({})).keys()) | {"message", "asctime"}


class Redactor:
    def __init__(self, secrets: Iterable[str] = ()) -> None:
        # Длинные секреты заменяем первыми, чтобы короткий префикс не оставил хвост.
        self._secrets = sorted(
            {s for s in secrets if len(s) >= MIN_SECRET_LEN}, key=len, reverse=True
        )

    def __call__(self, text: str) -> str:
        for secret in self._secrets:
            text = text.replace(secret, REDACTED)
        for pattern in _PATTERNS:
            text = pattern.sub(REDACTED, text)
        return _KEY_VALUE.sub(lambda m: f"{m.group(1)}{m.group(2)}{REDACTED}", text)


class JsonFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__()
        self._redact = redactor

    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key in _STD_ATTRS or key.startswith("_"):
                continue
            data[key] = value if isinstance(value, str | int | float | bool | None) else repr(value)
        if record.exc_info:
            data["exc"] = self.formatException(record.exc_info)
        return self._redact(json.dumps(data, ensure_ascii=False))


class TextFormatter(logging.Formatter):
    def __init__(self, redactor: Redactor) -> None:
        super().__init__("%(asctime)s %(levelname)s %(name)s: %(message)s")
        self._redact = redactor

    def format(self, record: logging.LogRecord) -> str:
        return self._redact(super().format(record))


def setup_logging(level: str = "INFO", fmt: str = "json", secrets: Iterable[str] = ()) -> None:
    redactor = Redactor(secrets)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter(redactor) if fmt == "json" else TextFormatter(redactor))
    root = logging.getLogger()
    for old in list(root.handlers):
        root.removeHandler(old)
    root.addHandler(handler)
    root.setLevel(level)
