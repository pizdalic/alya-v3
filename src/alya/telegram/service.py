"""TelegramService: компонент жизненного цикла, связывающий TDLib, pipeline и outbox."""

from __future__ import annotations

import logging
from collections.abc import Callable

from alya.core.config import Settings
from alya.llm.chat import ChatModel
from alya.storage.db import Database
from alya.telegram.addressee import AddresseeDetector, LLMAddresseeClassifier
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
                 loader: Callable[[], TdJson] = load_tdjson) -> None:
        self._settings = settings
        self._db = db
        self._engine = engine
        self._classifier_chat = classifier_chat
        self._loader = loader
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
        adapter = TdlibAdapter(client, me_id=me_id)
        pipeline = Pipeline(self._db, self._engine, detector, outbox, transport=adapter,
                            config=PipelineConfig(
                                me_id=me_id, owner_id=self._settings.app.owner_user_id,
                                strangers=tg.strangers,
                                group_context_hours=tg.group_context_hours,
                                private_history_days=tg.private_history_days,
                                replies_per_10min=tg.replies_per_10min,
                                typing_chars_per_s=tg.typing_chars_per_s,
                                max_typing_delay_s=tg.max_typing_delay_s,
                                debounce_s=tg.debounce_s,
                                debounce_max_wait_s=tg.debounce_max_wait_s))
        sender = Sender(outbox, adapter, on_sent=pipeline.on_sent,
                        chat_interval_s=tg.chat_send_interval_s)
        pipeline.set_wake(sender.wake)
        adapter.bind(on_message=pipeline.on_message, on_edit=pipeline.on_edit,
                     on_delete=pipeline.on_delete)
        client.on_update(adapter.handle_update)
        await sender.start()
        self._sender, self._pipeline = sender, pipeline
        self.ready = True
        log.info("Telegram подключён: %s", "@" + username if username else me_id)

    async def stop(self) -> None:
        self.ready = False
        if self._sender is not None:
            await self._sender.stop()
        if self._pipeline is not None:
            await self._pipeline.close()
        if self._client is not None:
            await self._client.close()
