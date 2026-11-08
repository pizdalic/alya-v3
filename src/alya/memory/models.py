"""Модели памяти: контекст запроса, запись, ключ области видимости."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

type Scope = Literal["private", "chat", "character"]
type Layer = Literal["working", "short", "long"]
type ChatKind = Literal["private", "group"]

LAYERS: tuple[Layer, ...] = ("working", "short", "long")


class MemoryScopeError(Exception):
    """Операция с памятью нарушает правило области видимости."""


@dataclass(frozen=True, slots=True)
class MemoryContext:
    """Кто и где спрашивает. Всё чтение и запись памяти идут только через контекст."""

    chat_id: int
    user_id: int
    chat_kind: ChatKind

    @classmethod
    def private(cls, user_id: int, chat_id: int | None = None) -> MemoryContext:
        return cls(chat_id=user_id if chat_id is None else chat_id, user_id=user_id,
                   chat_kind="private")

    @classmethod
    def group(cls, chat_id: int, user_id: int) -> MemoryContext:
        return cls(chat_id=chat_id, user_id=user_id, chat_kind="group")


@dataclass(frozen=True, slots=True)
class ScopeKey:
    """Единица зеркала и инвалидации: личка пользователя, групповой чат или персонаж."""

    scope: Scope
    chat_id: int | None = None
    owner_user_id: int | None = None

    @classmethod
    def for_private(cls, owner_user_id: int, chat_id: int) -> ScopeKey:
        return cls("private", chat_id, owner_user_id)

    @classmethod
    def for_chat(cls, chat_id: int) -> ScopeKey:
        return cls("chat", chat_id, None)

    @classmethod
    def character(cls) -> ScopeKey:
        return cls("character", None, None)

    def sort_key(self) -> tuple[str, int, int]:
        return (self.scope, self.chat_id or 0, self.owner_user_id or 0)


@dataclass(frozen=True, slots=True)
class Memory:
    id: str
    scope: Scope
    owner_user_id: int | None
    chat_id: int | None
    subject_user_id: int | None
    layer: Layer
    category: str
    content: str
    confidence: float
    is_permanent: bool
    usage_count: int
    created_at: str
    updated_at: str
    expires_at: str | None = None
    last_used_at: str | None = None
    superseded_by: str | None = None
    provenance: dict[str, Any] = field(default_factory=dict)

    @property
    def scope_key(self) -> ScopeKey:
        return ScopeKey(self.scope, self.chat_id, self.owner_user_id)
