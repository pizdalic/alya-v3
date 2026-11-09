"""TelegramService: компонент жизненного цикла, связывающий TDLib, pipeline и outbox."""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from alya.autonomy.scheduler import AutonomyConfig, AutonomyScheduler
from alya.autonomy.store import AutonomyStore

from alya.core.config import Settings
from alya.llm.chat import ChatModel
from alya.storage.db import Database
from alya.telegram.addressee import AddresseeDetector, LLMAddresseeClassifier
from alya.telegram.commands import OwnerCommands
from alya.telegram.media_flow import MediaKit
from alya.telegram.outbox import OutboxStore
from alya.telegram.pipeline import Pipeline, PipelineConfig, Responder
from alya.telegram.sender import Sender
from alya.telegram.tdlib.adapter import TdlibAdapter
from alya.telegram.tdlib.auth import authorize, interactive_login, username_of
from alya.telegram.tdlib.client import TdClient
from alya.telegram.tdlib.tdjson import TdJson, load_tdjson

__all__ = ["TelegramService", "interactive_login"]

log = logging.getLogger(__name__)


class TelegramService:
    name = "telegram"

    def __init__(self, settings: Settings, db: Database, engine: Responder, *,
                 classifier_chat: ChatModel | None = None,
                 loader: Callable[[], TdJson] = load_tdjson,
                 media: MediaKit | None = None, memory: Any = None) -> None:
        self._memory = memory
        self._autonomy: AutonomyScheduler | None = None
        self._settings = settings
        self._db = db
        self._engine = engine
        self._classifier_chat = classifier_chat
        self._loader = loader
        self._media = media
        self._client: TdClient | None = None
        self._sender: Sender | None = None
        self._pipeline: Pipeline | None = None
        self.ready = False

    async def start(self) -> None:
        tg = self._settings.telegram
        client = TdClient(self._loader())
        self._client = client
        me = await authorize(client, self._settings, prompt=None)
        me_id = int(me["id"])
        username = username_of(me)
        classifier = (LLMAddresseeClassifier(self._classifier_chat)
                      if tg.gray_zone_llm and self._classifier_chat is not None else None)
        detector = AddresseeDetector(me_id, username, tg.alya_names,
                                     thread_ttl_s=tg.thread_ttl_s, classifier=classifier)
        outbox = OutboxStore(self._db)
        au = self._settings.autonomy
        autonomy = AutonomyStore(self._db) if au.enabled else None
        adapter = TdlibAdapter(client, me_id=me_id)
        if self._media is not None:
            self._media.commands = OwnerCommands(
                adapter, owner_id=self._settings.app.owner_user_id,
                limiter=self._media.limiter, stickers=self._media.stickers,
                media_dir=self._media.media_dir,
                max_image_bytes=self._settings.media.max_image_bytes,
                autonomy=autonomy, timezone=self._settings.app.timezone)
        pipeline = Pipeline(self._db, self._engine, detector, outbox, transport=adapter,
                            media=self._media, autonomy=autonomy, config=PipelineConfig(
                                me_id=me_id, owner_id=self._settings.app.owner_user_id,
                                strangers=tg.strangers,
                                group_context_hours=tg.group_context_hours,
                                private_history_days=tg.private_history_days,
                                replies_per_10min=tg.replies_per_10min,
                                typing_chars_per_s=tg.typing_chars_per_s,
                                max_typing_delay_s=tg.max_typing_delay_s,
                                debounce_s=tg.debounce_s,
                                debounce_max_wait_s=tg.debounce_max_wait_s,
                                read_channels=au.enabled and au.read_channels))
        sender = Sender(outbox, adapter, on_sent=pipeline.on_sent,
                        chat_interval_s=tg.chat_send_interval_s)
        pipeline.set_wake(sender.wake)
        adapter.bind(on_message=pipeline.on_message, on_edit=pipeline.on_edit,
                     on_delete=pipeline.on_delete)
        client.on_update(adapter.handle_update)
        await sender.start()
        if autonomy is not None and hasattr(self._engine, "initiate"):
            core = getattr(getattr(self._engine, "character", None), "core", None)
            self._autonomy = AutonomyScheduler(
                autonomy, self._engine, outbox,  # type: ignore[arg-type]
                me_id=me_id, owner_id=self._settings.app.owner_user_id,
                timezone=self._settings.app.timezone,
                interests=list(getattr(core, "interests", []) or []),
                config=AutonomyConfig(**au.model_dump(exclude={"enabled"})),
                history=lambda chat_id: pipeline._history(chat_id, []),
                mood=getattr(self._engine, "mood", None),
                typing=lambda part: min(len(part) / tg.typing_chars_per_s,
                                        tg.max_typing_delay_s),
                wake=sender.wake, memory=self._memory, digester=self._classifier_chat)
            await self._autonomy.start()
        self._sender, self._pipeline = sender, pipeline
        self.ready = True
        log.info("Telegram подключён: %s", "@" + username if username else me_id)

    async def stop(self) -> None:
        self.ready = False
        if self._autonomy is not None:
            await self._autonomy.stop()
        if self._sender is not None:
            await self._sender.stop()
        if self._pipeline is not None:
            await self._pipeline.close()
        if self._client is not None:
            await self._client.close()
