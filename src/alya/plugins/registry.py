"""Реестр плагинов и песочница разрешений.

Плагин — файл plugins/<name>.py с двумя именами:

    MANIFEST = {"name": "dice", "version": "1.0", "description": "бросить кубик",
                "commands": ["/dice"], "permissions": ["reply"], "audience": "all"}

    async def handle(ctx, command, args): await ctx.reply("выпало 4")

Плагин получает ТОЛЬКО PluginContext: ни базы, ни транспорта Telegram, ни сервиса
памяти целиком. Эффективные разрешения = заявленные в манифесте ∩ выданные
владельцем в [plugins.grants]. Плагин без записи в grants не загружается.

Честное ограничение: код плагина исполняется в том же процессе Python, поэтому
разрешения — защита от ошибок и контракт, а не песочница от злонамеренного кода.
Ставь только плагины, код которых прочитал (docs/SECURITY.md, T10).
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import re
import sys
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any, Protocol

from alya.core.timeutil import Clock, iso, utcnow
from alya.memory.models import MemoryContext
from alya.storage.db import Database
from alya.telegram.models import IncomingMessage

log = logging.getLogger(__name__)

PERMISSIONS: dict[str, str] = {
    "reply": "отвечать в чат, где вызвали команду (до 3 сообщений)",
    "recall": "искать в памяти ТОЛЬКО того, кто вызвал команду",
    "storage": "своё хранилище ключ-значение (до 64 КБ)",
}
# Заявка на любое из этих — отказ в загрузке: таких возможностей у плагинов нет.
FORBIDDEN = frozenset({"account", "memory_all", "db", "send_any", "owner_commands",
                       "secrets", "shell"})
_NAME = re.compile(r"^[a-z][a-z0-9_]{1,31}$")
_COMMAND = re.compile(r"^/[a-z][a-z0-9_]{0,31}$")
MAX_REPLIES = 3
MAX_REPLY_CHARS = 4000
STORAGE_LIMIT = 64 * 1024


class PluginError(Exception):
    """Некорректный плагин (манифест, импорт)."""


class PluginPermissionError(PermissionError):
    """Плагин обратился к тому, чего ему не выдали."""


class Recaller(Protocol):
    async def recall(self, ctx: MemoryContext, query: str, *, k: int = 8) -> Any: ...


Handler = Callable[["PluginContext", str, str], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class LoadedPlugin:
    name: str
    version: str
    description: str
    commands: tuple[str, ...]
    permissions: frozenset[str]
    audience: str  # owner | all
    handler: Handler


def _manifest(module: ModuleType, path: Path) -> dict[str, Any]:
    raw = getattr(module, "MANIFEST", None)
    if not isinstance(raw, Mapping):
        raise PluginError(f"{path.name}: нет MANIFEST")
    return dict(raw)


def validate(raw: Mapping[str, Any], *, granted: Iterable[str] | None,
             reserved: Iterable[str] = ()) -> tuple[str, str, str, tuple[str, ...],
                                                    frozenset[str], str]:
    name = str(raw.get("name", ""))
    if not _NAME.match(name):
        raise PluginError(f"плохое имя плагина: {name!r}")
    commands = tuple(str(c).lower() for c in raw.get("commands") or ())
    if not commands:
        raise PluginError(f"{name}: не заявлено ни одной команды")
    bad = [c for c in commands if not _COMMAND.match(c)]
    if bad:
        raise PluginError(f"{name}: плохие команды {bad}")
    clash = sorted(set(commands) & set(reserved))
    if clash:
        raise PluginError(f"{name}: команды заняты встроенными: {clash}")
    declared = frozenset(str(p) for p in raw.get("permissions") or ())
    forbidden = sorted(declared & FORBIDDEN)
    if forbidden:
        raise PluginError(f"{name}: запрещённые разрешения {forbidden}")
    unknown = sorted(declared - set(PERMISSIONS))
    if unknown:
        raise PluginError(f"{name}: неизвестные разрешения {unknown}")
    if granted is None:
        raise PluginError(f"{name}: владелец не выдал разрешений ([plugins.grants])")
    audience = str(raw.get("audience", "owner"))
    if audience not in ("owner", "all"):
        raise PluginError(f"{name}: audience должен быть owner или all")
    return (name, str(raw.get("version", "0")), str(raw.get("description", ""))[:200],
            commands, declared & frozenset(granted), audience)


def load_plugins(directory: Path, grants: Mapping[str, Iterable[str]], *,
                 reserved: Iterable[str] = ()) -> tuple[list[LoadedPlugin], list[str]]:
    """Загрузить плагины из каталога. Ошибка одного не мешает остальным."""
    loaded: list[LoadedPlugin] = []
    errors: list[str] = []
    if not directory.is_dir():
        return loaded, errors
    taken: set[str] = set(reserved)
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        stem = path.stem
        if stem not in grants:
            errors.append(f"{path.name}: не включён в [plugins.grants] — пропущен")
            continue
        try:
            spec = importlib.util.spec_from_file_location(f"alya_plugin_{stem}", path)
            if spec is None or spec.loader is None:
                raise PluginError(f"{path.name}: не импортируется")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            raw = _manifest(module, path)
            if raw.get("name") != stem:
                raise PluginError(f"{path.name}: MANIFEST.name должно совпадать с именем файла")
            name, version, desc, commands, perms, audience = validate(
                raw, granted=grants.get(stem), reserved=taken)
            handler = getattr(module, "handle", None)
            if not asyncio.iscoroutinefunction(handler):
                raise PluginError(f"{name}: нужна async def handle(ctx, command, args)")
        except Exception as exc:  # noqa: BLE001 — чужой код, любая ошибка = отказ
            sys.modules.pop(f"alya_plugin_{stem}", None)
            errors.append(str(exc) if isinstance(exc, PluginError)
                          else f"{path.name}: {type(exc).__name__}: {exc}")
            continue
        taken.update(commands)
        loaded.append(LoadedPlugin(name, version, desc, commands, perms, audience, handler))
    return loaded, errors


@dataclass
class PluginContext:
    """Единственное, что видит плагин."""

    plugin: str
    chat_id: int
    user_id: int
    is_owner: bool
    _granted: frozenset[str]
    _memory: Recaller | None
    _storage: Path
    _audit: Callable[[str, dict[str, Any]], Awaitable[None]]
    replies: list[str] = field(default_factory=list)

    async def _need(self, perm: str) -> None:
        if perm not in self._granted:
            await self._audit("plugin_denied", {"plugin": self.plugin, "permission": perm})
            raise PluginPermissionError(f"плагину {self.plugin} не выдано «{perm}»")

    async def reply(self, text: str) -> None:
        await self._need("reply")
        text = str(text).strip()
        if text and len(self.replies) < MAX_REPLIES:
            self.replies.append(text[:MAX_REPLY_CHARS])

    async def recall(self, query: str, *, limit: int = 5) -> list[str]:
        await self._need("recall")
        if self._memory is None:
            return []
        ctx = MemoryContext.private(self.user_id)
        result = await self._memory.recall(ctx, str(query)[:500], k=max(1, min(limit, 10)))
        return [h.memory.content for h in result.hits]

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self._storage.read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    async def get(self, key: str, default: Any = None) -> Any:
        await self._need("storage")
        return self._load().get(str(key), default)

    async def set(self, key: str, value: Any) -> None:
        await self._need("storage")
        data = self._load()
        if value is None:
            data.pop(str(key), None)
        else:
            data[str(key)] = value
        blob = json.dumps(data, ensure_ascii=False)
        if len(blob.encode("utf-8")) > STORAGE_LIMIT:
            raise PluginPermissionError(f"хранилище {self.plugin} больше 64 КБ")
        self._storage.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._storage.with_suffix(".tmp")
        tmp.write_text(blob, encoding="utf-8")
        tmp.replace(self._storage)


class PluginHost:
    """Маршрутизация команд к плагинам, тайм-аут, аудит. Только личные чаты."""

    def __init__(self, db: Database, plugins: Iterable[LoadedPlugin], *, owner_id: int | None,
                 data_dir: Path, memory: Recaller | None = None, timeout_s: float = 10.0,
                 clock: Clock = utcnow) -> None:
        self._db = db
        self._owner = owner_id
        self._data = data_dir
        self._memory = memory
        self._timeout = timeout_s
        self._clock = clock
        self.plugins = list(plugins)
        self._by_command = {c: p for p in self.plugins for c in p.commands}

    def _route(self, msg: IncomingMessage) -> tuple[LoadedPlugin, str, str] | None:
        if not msg.is_private or msg.sender_id is None or msg.is_outgoing:
            return None
        name, _, args = (msg.text or "").strip().partition(" ")
        command = name.split("@", 1)[0].lower()
        plugin = self._by_command.get(command)
        if plugin is None:
            return None
        if plugin.audience == "owner" and msg.sender_id != self._owner:
            return None
        return plugin, command, args.strip()

    def accepts(self, msg: IncomingMessage) -> bool:
        return self._route(msg) is not None

    async def _audit(self, action: str, details: dict[str, Any], actor: int | None) -> None:
        now = iso(self._clock())
        await self._db.transaction(lambda c: c.execute(
            "INSERT INTO audit_log (actor_user_id, action, target, details, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (actor, action, details.get("plugin"), json.dumps(details, ensure_ascii=False),
             now)))

    async def handle(self, msg: IncomingMessage) -> list[str]:
        route = self._route(msg)
        if route is None:
            return []
        plugin, command, args = route
        uid = msg.sender_id
        assert uid is not None

        async def audit(action: str, details: dict[str, Any]) -> None:
            await self._audit(action, details, uid)

        ctx = PluginContext(plugin=plugin.name, chat_id=msg.chat_id, user_id=uid,
                            is_owner=uid == self._owner, _granted=plugin.permissions,
                            _memory=self._memory,
                            _storage=self._data / f"{plugin.name}.json", _audit=audit)
        status = "ok"
        try:
            await asyncio.wait_for(plugin.handler(ctx, command, args), self._timeout)
        except PluginPermissionError as exc:
            status = "denied"
            log.warning("plugin %s: %s", plugin.name, exc)
        except TimeoutError:
            status = "timeout"
            log.warning("plugin %s: тайм-аут %.0f с", plugin.name, self._timeout)
        except Exception:  # noqa: BLE001 — падение плагина не роняет Алю
            status = "error"
            log.exception("plugin %s упал", plugin.name)
        await audit("plugin_run", {"plugin": plugin.name, "command": command, "status": status})
        if status != "ok" and not ctx.replies and "reply" in plugin.permissions:
            return ["что-то не получилось"]
        return list(ctx.replies)

    def describe(self) -> str:
        if not self.plugins:
            return "плагинов нет"
        return "\n".join(f"{p.name} {p.version}: {', '.join(p.commands)} — {p.description} "
                         f"[{', '.join(sorted(p.permissions)) or 'без разрешений'}]"
                         for p in self.plugins)
