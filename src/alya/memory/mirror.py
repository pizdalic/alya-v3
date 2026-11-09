"""Markdown-зеркало памяти (ADR-0004): только для чтения человеком, источник истины — БД.

Файл пишется атомарно (временный файл + fsync + rename), права 0600, каталоги 0700.
Пути строятся только из целых ID — обход каталогов невозможен.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Sequence
from pathlib import Path

from alya.memory.models import Memory, ScopeKey

_LAYER_TITLES = {"long": "Долговременная", "short": "Краткосрочная", "working": "Рабочая"}
_HEADER = "> Сгенерировано автоматически из базы данных. Правки в этом файле не применяются.\n"


def _title(key: ScopeKey) -> str:
    if key.scope == "private":
        return f"Личка с пользователем {key.owner_user_id}"
    if key.scope == "chat":
        return f"Групповой чат {key.chat_id}"
    return "Персонаж"


def _one_line(text: str) -> str:
    return " ".join(text.split())


def render(key: ScopeKey, memories: Sequence[Memory]) -> str:
    lines = [f"# Память: {_title(key)}", "", _HEADER]
    for layer in ("long", "short", "working"):
        items = [m for m in memories if m.layer == layer]
        if not items:
            continue
        lines += [f"## {_LAYER_TITLES[layer]}", ""]
        for m in items:
            flags = f"уверенность {m.confidence:.2f}, {m.created_at[:10]}"
            if m.is_permanent:
                flags += ", постоянная"
            if m.subject_user_id is not None and key.scope == "chat":
                flags += f", о пользователе {m.subject_user_id}"
            lines.append(f"- [{m.category}] {_one_line(m.content)} _({flags})_ `{m.id}`")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


class MarkdownMirror:
    def __init__(self, root: Path) -> None:
        self._root = Path(root)

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, key: ScopeKey) -> Path:
        if key.scope == "private":
            name = f"u{int(key.owner_user_id or 0)}_c{int(key.chat_id or 0)}.md"
            return self._root / "private" / name
        if key.scope == "chat":
            return self._root / "chats" / f"c{int(key.chat_id or 0)}.md"
        return self._root / "character.md"

    def write(self, key: ScopeKey, memories: Sequence[Memory]) -> Path | None:
        """Перезаписать файл области. Пустая область — файл удаляется."""
        path = self.path_for(key)
        if not memories:
            self.remove(key)
            return None
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".md")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(render(key, memories))
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise
        return path

    def remove(self, key: ScopeKey) -> None:
        self.path_for(key).unlink(missing_ok=True)

    def remove_user(self, user_id: int) -> None:
        folder = self._root / "private"
        if folder.is_dir():
            for path in folder.glob(f"u{int(user_id)}_c*.md"):
                path.unlink(missing_ok=True)

    def files(self) -> list[Path]:
        if not self._root.is_dir():
            return []
        return sorted(p for p in self._root.rglob("*.md") if not p.name.startswith(".tmp-"))
