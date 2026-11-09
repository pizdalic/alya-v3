"""Сборка приложения: контейнер, компоненты, проверки здоровья."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, cast

from alya.character.allocator import ContextAllocator
from alya.character.emotion import EmotionStore
from alya.character.engine import CharacterEngine
from alya.character.persona import Character, load_character
from alya.character.relationships import RelationshipStore
from alya.character.style import Stylizer
from alya.core.config import Settings
from alya.core.container import Container
from alya.core.events import Event, EventBus
from alya.core.lifecycle import Lifecycle
from alya.dream.engine import DreamConfig, DreamEngine
from alya.dream.judge import LLMJudge
from alya.dream.scheduler import DreamScheduler
from alya.llm.chat import ChatModel, OpenAICompatibleChat
from alya.llm.openai_compat import OpenAICompatibleEmbedder
from alya.media import audio
from alya.media.clients import (
    ChatSTT,
    ImageClient,
    SpeechTTS,
    TranscriptionsSTT,
    VisionClient,
    WebSearchClient,
    http_client,
)
from alya.media.limits import KINDS, UsageLimiter
from alya.media.processor import MediaProcessor, ProcessorLimits
from alya.media.stickers import StickerStore
from alya.memory.indexer import Embedder, EmbeddingIndexer
from alya.memory.mirror import MarkdownMirror
from alya.memory.repository import LayerPolicy, MemoryRepository
from alya.memory.search import MemorySearch
from alya.memory.service import MemoryService
from alya.memory.vectors import NumpyBackend, SqliteVecBackend, VectorBackend, load_sqlite_vec
from alya.observability.health import Check as HealthCheck
from alya.observability.health import CheckResult, HealthRegistry, HealthServer
from alya.storage.backup import BackupScheduler
from alya.storage.db import Database
from alya.storage.migrator import apply_migrations, load_migrations, pending
from alya.telegram.media_flow import MediaKit

log = logging.getLogger(__name__)


class StorageComponent:
    """Открывает БД и применяет миграции при старте."""

    name = "storage"

    def __init__(self, db: Database) -> None:
        self._db = db

    async def start(self) -> None:
        await asyncio.to_thread(self._db.connect)
        if not self._db.fts5_available():
            log.warning("SQLite собран без FTS5: полнотекстовый поиск будет недоступен")
        applied = await asyncio.to_thread(apply_migrations, self._db.conn)
        if applied:
            log.info("применены миграции: %s", applied)

    async def stop(self) -> None:
        self._db.close()


class EventBusComponent:
    name = "events"

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        await self._bus.aclose()


class MemoryMaintenance:
    """Фоновое обслуживание памяти: удаление истёкшего и досчёт эмбеддингов."""

    name = "memory-maintenance"

    def __init__(self, service: MemoryService, indexer: EmbeddingIndexer | None,
                 interval_s: float) -> None:
        self._service = service
        self._indexer = indexer
        self._interval = interval_s
        self._task: asyncio.Task[None] | None = None

    async def run_once(self) -> None:
        memories, messages = await self._service.purge_expired()
        if memories or messages:
            log.info("удалено по сроку: память=%d сообщения=%d", memories, messages)
        if self._indexer is not None:
            stored = await self._indexer.embed_pending()
            if stored:
                log.info("посчитано эмбеддингов: %d", stored)

    async def _loop(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("ошибка обслуживания памяти")
            await asyncio.sleep(self._interval)

    async def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._loop())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


def build_embedder(settings: Settings) -> Embedder | None:
    role = settings.llm.roles.get("embedding")
    if role is None or settings.llm.api_key is None:
        return None
    return OpenAICompatibleEmbedder(
        role.base_url or settings.llm.base_url,
        settings.llm.api_key.get_secret_value(),
        role.model,
        timeout_s=settings.llm.timeout_s,
        batch_size=settings.memory.embedding_batch,
    )


def build_memory(settings: Settings, db: Database) -> tuple[MemoryService, EmbeddingIndexer | None]:
    mem = settings.memory
    repo = MemoryRepository(db, LayerPolicy(timedelta(hours=mem.working_ttl_hours),
                                            timedelta(days=mem.short_ttl_days)))
    embedder = build_embedder(settings)
    backend: VectorBackend = NumpyBackend()
    if mem.vector_backend == "sqlite_vec":
        backend = _SqliteVecLazy(db)
    search = MemorySearch(repo, embedder, backend, rrf_k=mem.rrf_k,
                          candidates=mem.search_candidates,
                          half_life_days=mem.recency_half_life_days)
    mirror = MarkdownMirror(mem.mirror_dir) if mem.mirror_enabled else None
    indexer = (EmbeddingIndexer(db, embedder, batch_size=mem.embedding_batch)
               if embedder is not None else None)
    return MemoryService(repo, search, mirror), indexer


def build_chat_model(settings: Settings, role_name: str) -> ChatModel | None:
    role = settings.llm.roles.get(role_name)
    if role is None or settings.llm.api_key is None:
        return None
    return OpenAICompatibleChat(role.base_url or settings.llm.base_url,
                                settings.llm.api_key.get_secret_value(), role.model,
                                timeout_s=settings.llm.timeout_s)


def _raw(settings: Settings, role_name: str) -> Any:
    """Чат-клиент с низкоуровневым raw() для мультимодальных запросов."""
    return cast(Any, build_chat_model(settings, role_name))


def _http(settings: Settings, role_name: str) -> tuple[Any, str] | None:
    role = settings.llm.roles.get(role_name)
    if role is None or settings.llm.api_key is None:
        return None
    return http_client(role.base_url or settings.llm.base_url,
                       settings.llm.api_key.get_secret_value(),
                       timeout_s=settings.llm.timeout_s), role.model


def build_limiter(settings: Settings, db: Database) -> UsageLimiter:
    """Выключенная в [media] функция получает лимит 0 — и не срабатывает никогда."""
    ms = settings.media
    flags = {"vision": ms.vision, "stt": ms.stt, "tts": ms.tts, "image": ms.images,
             "search": ms.web_search, "reaction": ms.reactions, "sticker": ms.stickers}
    per_hour = {k: (ms.per_hour.get(k, 0) if flags[k] else 0) for k in KINDS}
    return UsageLimiter(db, per_hour, per_day=dict(ms.per_day))


def build_searcher(settings: Settings) -> WebSearchClient | None:
    if not settings.media.web_search:
        return None
    chat = _raw(settings, "utility") or _raw(settings, "chat")
    return WebSearchClient(chat, max_results=settings.media.web_search_results) if chat else None


def build_media(settings: Settings, db: Database, limiter: UsageLimiter,
                engine: CharacterEngine | None) -> MediaKit:
    ms = settings.media
    vision_chat = _raw(settings, "vision") if ms.vision else None
    stt: Any = None
    if ms.stt:
        if ms.stt_api == "chat":
            chat = _raw(settings, "stt")
            stt = ChatSTT(chat) if chat else None
        else:
            http = _http(settings, "stt")
            stt = TranscriptionsSTT(*http) if http else None
    tts = None
    if ms.tts:
        http = _http(settings, "tts")
        if http:
            tts = SpeechTTS(http[0], http[1], voice=ms.tts_voice,
                            instructions=ms.tts_instructions)
    image_chat = _raw(settings, "image") if ms.images else None
    if (stt or tts) and audio.ffmpeg_path() is None:
        log.warning("ffmpeg не найден: голосовые не будут распознаваться и озвучиваться")
    processor = MediaProcessor(
        limiter, vision=VisionClient(vision_chat) if vision_chat else None, stt=stt,
        limits=ProcessorLimits(max_image_bytes=ms.max_image_bytes,
                               max_voice_seconds=ms.max_voice_seconds,
                               max_voice_bytes=ms.max_voice_bytes))
    return MediaKit(
        limiter, processor=processor,
        stickers=StickerStore(db) if ms.stickers else None, tts=tts,
        images=ImageClient(image_chat, max_bytes=ms.max_image_bytes) if image_chat else None,
        selfie_prompt=engine.selfie_prompt if engine is not None else None,
        media_dir=ms.media_dir, selfie_private_only=ms.selfie_private_only)


def build_dream(settings: Settings, db: Database, memory: MemoryService) -> DreamEngine:
    """Движок Dream. Без LLM он только сливает явные дубли и следит за устареванием."""
    ds = settings.dream
    judge = None
    if ds.use_llm:
        chat = build_chat_model(settings, "utility") or build_chat_model(settings, "chat")
        judge = LLMJudge(chat) if chat is not None else None
    embedding = settings.llm.roles.get("embedding")
    config = DreamConfig(
        dup_threshold=ds.dup_threshold, exact_threshold=ds.exact_threshold,
        related_threshold=ds.related_threshold, max_cluster=ds.max_cluster,
        max_clusters_per_run=ds.max_clusters_per_run, max_pairs_per_run=ds.max_pairs_per_run,
        stale_days=ds.stale_days, decay_step=ds.decay_step,
        confidence_floor=ds.confidence_floor, contradiction_factor=ds.contradiction_factor,
        retention_days=ds.retention_days,
    )
    return DreamEngine(db, judge, model_id=embedding.model if embedding else None,
                       config=config, on_change=memory.refresh_mirror)


def build_character(settings: Settings, db: Database, memory: MemoryService | None,
                    character: Character | None = None, *,
                    limiter: UsageLimiter | None = None) -> CharacterEngine | None:
    """Персонаж грузится и проверяется всегда; движок — только если задана модель chat."""
    cs = settings.character
    character = character or load_character(cs.dir, cs.persona)
    chat = build_chat_model(settings, "chat")
    if chat is None:
        return None
    return CharacterEngine(
        character, chat,
        planner=build_chat_model(settings, "utility"),
        emotions=EmotionStore(db, character.persona.id, character.persona.emotion),
        relationships=RelationshipStore(db, settings.app.owner_user_id),
        stylizer=Stylizer(db, character.core.style),
        allocator=ContextAllocator(cs.context_budget_tokens),
        memory=memory,
        timezone=settings.app.timezone,
        recall_k=cs.recall_k,
        temperature=cs.temperature,
        max_reply_tokens=cs.max_reply_tokens,
        searcher=build_searcher(settings) if limiter is not None else None,
        limiter=limiter,
        sticker_pick=StickerStore(db).pick if settings.media.stickers else None,
    )


class _SqliteVecLazy:
    """sqlite-vec подключается при первом запросе; если его нет — честный откат на NumPy."""

    name = "sqlite_vec"

    def __init__(self, db: Database) -> None:
        self._db = db
        self._impl: VectorBackend | None = None

    def top_k(self, conn, ctx, query, model_id, k, now_iso):  # type: ignore[no-untyped-def]
        if self._impl is None:
            if load_sqlite_vec(conn):
                self._impl = SqliteVecBackend()
            else:
                log.warning("sqlite-vec недоступен, используется NumPy")
                self._impl = NumpyBackend()
        return self._impl.top_k(conn, ctx, query, model_id, k, now_iso)


@dataclass(slots=True)
class App:
    settings: Settings
    container: Container
    lifecycle: Lifecycle

    async def run(self) -> None:
        bus = self.container.resolve(EventBus)
        try:
            await self.lifecycle.start()
            await bus.publish(Event("app.started"))
            problems = self.settings.problems_for_run()
            for problem in problems:
                log.warning("конфигурация: %s", problem)
            log.info("основа запущена")
            await self.lifecycle.run_until_stopped()
        finally:
            await self.lifecycle.stop()
            await self.container.aclose()


def build_app(settings: Settings) -> App:
    container = Container()
    container.register_instance(Settings, settings)
    container.register(EventBus, lambda c: EventBus())
    container.register(
        Database,
        lambda c: Database(settings.storage.db_path, settings.storage.busy_timeout_ms),
    )
    container.register(HealthRegistry, lambda c: HealthRegistry())
    container.register(MemoryService, lambda c: memory_service)

    db = container.resolve(Database)
    memory_service, indexer = build_memory(settings, db)
    character = load_character(settings.character.dir, settings.character.persona)
    limiter = build_limiter(settings, db)
    engine = build_character(settings, db, memory_service, character, limiter=limiter)
    container.register_instance(Character, character)
    if engine is not None:
        container.register_instance(CharacterEngine, engine)
    else:
        log.warning("llm.roles.chat не задана: персонаж загружен, но отвечать не сможет")
    registry = container.resolve(HealthRegistry)
    registry.add("database", _database_check(db))
    registry.add("migrations", _migrations_check(db))

    lifecycle = Lifecycle()
    lifecycle.add(EventBusComponent(container.resolve(EventBus)))
    lifecycle.add(StorageComponent(db))
    if settings.backup.enabled:
        bk = settings.backup
        lifecycle.add(BackupScheduler(settings.storage.db_path, bk.dir,
                                      interval_h=bk.interval_hours, keep=bk.keep,
                                      retain_days=bk.retain_days))
    lifecycle.add(MemoryMaintenance(memory_service, indexer,
                                    settings.memory.maintenance_interval_s))
    if settings.dream.enabled:
        dream = build_dream(settings, db, memory_service)
        ds = settings.dream
        lifecycle.add(DreamScheduler(
            dream, timezone=settings.app.timezone, hour_local=ds.hour_local,
            check_interval_s=ds.check_interval_s,
            new_memories_threshold=ds.new_memories_threshold, min_gap_hours=ds.min_gap_hours))
        registry.add("dream", _dream_check(dream))
    _add_telegram(lifecycle, settings, db, engine,
                  build_media(settings, db, limiter, engine), memory=memory_service)
    if settings.health.enabled:
        lifecycle.add(HealthServer(registry, settings.health.host, settings.health.port))
    return App(settings=settings, container=container, lifecycle=lifecycle)


def _add_telegram(lifecycle: Lifecycle, settings: Settings, db: Database,
                  engine: CharacterEngine | None, media: MediaKit | None = None,
                  memory: MemoryService | None = None) -> None:
    tg = settings.telegram
    if tg.mode == "none":
        return
    if tg.mode == "bot":
        log.warning("режим telegram.mode=bot пока не реализован; Telegram не подключён")
        return
    if tg.api_id is None or tg.api_hash is None or engine is None:
        log.warning("Telegram не подключён: нужны api_id, api_hash и модель chat")
        return
    from alya.telegram.service import TelegramService

    classifier = build_chat_model(settings, "utility") or build_chat_model(settings, "chat")
    lifecycle.add(TelegramService(settings, db, engine, classifier_chat=classifier,
                                  media=media, memory=memory))


def _database_check(db: Database) -> HealthCheck:
    async def check() -> CheckResult:
        rows = await db.fetchall("SELECT 1")
        return CheckResult(ok=rows[0][0] == 1)

    return check


def _migrations_check(db: Database) -> HealthCheck:
    migrations = load_migrations()

    async def check() -> CheckResult:
        left = await asyncio.to_thread(pending, db.conn, migrations)
        if left:
            return CheckResult(False, f"не применены: {[m.version for m in left]}")
        return CheckResult(True, f"версия {migrations[-1].version if migrations else 0}")

    return check



def _dream_check(engine: DreamEngine) -> HealthCheck:
    """Последний прогон Dream; ошибка консолидации не роняет здоровье, но видна в detail."""

    async def check() -> CheckResult:
        last = await engine.last_run()
        if last is None:
            return CheckResult(True, "ещё не запускался")
        return CheckResult(True, f"прогон {last['id']}: {last['status']} ({last['started_at']})")

    return check
