"""Медиа проверяются до текста ответа; текст не изображает неотправленные файлы."""

from __future__ import annotations

import unittest

from alya.character.allocator import ContextAllocator
from alya.character.emotion import EmotionStore
from alya.character.engine import _NO_PHOTO, _NO_STICKER, CharacterEngine, Turn
from alya.character.persona import load_character
from alya.character.relationships import RelationshipStore
from alya.character.style import Stylizer
from alya.media.limits import UsageLimiter
from alya.memory.models import MemoryContext
from alya.telegram.sanitize import clean_outgoing
from tests.memory_helpers import A, MemoryTestCase
from tests.test_character import ROOT, FakeChat, plan_json


class StarMarkerTests(unittest.TestCase):
    def test_star_markers_removed(self) -> None:
        self.assertEqual(clean_outgoing("вот… *фото*"), ["вот…"])
        self.assertEqual(clean_outgoing("*стикер: LINE_White_Cat_Girl*"), [])
        self.assertEqual(clean_outgoing("ну ладно *отправляет селфи* держи"), ["ну ладно держи"])

    def test_other_emphasis_kept(self) -> None:
        self.assertEqual(clean_outgoing("*обнимает*"), ["*обнимает*"])
        self.assertEqual(clean_outgoing("фото классное"), ["фото классное"])


class GateTests(MemoryTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.character = load_character(ROOT, "shy")
        self.picked: list[str] = []

    def engine(self, chat: FakeChat, *, image: int, sticker_id: str | None) -> CharacterEngine:
        ch = self.character

        async def pick(emoji: str) -> str | None:
            self.picked.append(emoji)
            return sticker_id

        return CharacterEngine(
            ch, chat,
            emotions=EmotionStore(self.db, "shy", ch.persona.emotion, clock=self.clock),
            relationships=RelationshipStore(self.db, A, clock=self.clock),
            stylizer=Stylizer(self.db, ch.core.style.model_copy(update={"typo_per_100": 0}),
                              clock=self.clock),
            allocator=ContextAllocator(4000), memory=self.service, clock=self.clock,
            limiter=UsageLimiter(self.db, {"image": image, "sticker": 5}, clock=self.clock),
            sticker_pick=pick)

    async def ask(self, engine: CharacterEngine, chat: FakeChat):  # type: ignore[no-untyped-def]
        reply = await engine.respond(Turn(MemoryContext.private(A), "кинь фото и стикер", "Миша", []))
        return reply, chat.calls[-1][0][0]["content"]

    async def test_blocked_media_dropped_and_told(self) -> None:
        chat = FakeChat([plan_json(selfie="под одеялом", sticker="🐱"), "ну ладно"])
        with self.assertLogs("alya.character.engine", "INFO") as logs:
            reply, system = await self.ask(self.engine(chat, image=0, sticker_id=None), chat)
        self.assertEqual(reply.actions.selfie, "")
        self.assertEqual(reply.actions.sticker, "")
        self.assertIn(_NO_PHOTO, system)
        self.assertIn(_NO_STICKER, system)
        text = "\n".join(logs.output)
        self.assertIn("селфи не будет", text)
        self.assertIn("нет стикера под", text)

    async def test_available_media_kept(self) -> None:
        chat = FakeChat([plan_json(selfie="под одеялом", sticker="🐱"), "держи"])
        reply, system = await self.ask(self.engine(chat, image=3, sticker_id="RID1"), chat)
        self.assertEqual(reply.actions.selfie, "под одеялом")
        self.assertEqual(reply.actions.sticker, "🐱")
        self.assertEqual(reply.actions.sticker_id, "RID1")
        self.assertNotIn(_NO_PHOTO, system)
        self.assertEqual(self.picked, ["🐱"])

    async def test_limit_check_does_not_consume(self) -> None:
        lim = UsageLimiter(self.db, {"image": 1}, clock=self.clock)
        self.assertIsNone(await lim.check("image"))
        self.assertIsNone(await lim.check("image"))
        self.assertTrue(await lim.take("image", 1))
        self.assertIn("за час исчерпан (1/1)", await lim.check("image") or "")
        self.assertEqual(await lim.check("tts"), "tts выключено")
