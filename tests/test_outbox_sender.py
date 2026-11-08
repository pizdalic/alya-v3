import unittest
from datetime import timedelta

from alya.telegram.outbox import OutboxStore
from alya.telegram.sanitize import TELEGRAM_LIMIT, clean_incoming, clean_outgoing
from alya.telegram.sender import Sender
from alya.telegram.transport import FloodWait, TransportError
from tests.telegram_helpers import DbTestCase, FakeTransport, Sleeps


class OutboxSenderTests(DbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.store = OutboxStore(self.db, clock=self.clock)
        self.tr = FakeTransport()
        self.sleeps = Sleeps()
        self.sent_ids: list[int] = []

        async def on_sent(item, tg_id):  # type: ignore[no-untyped-def]
            self.sent_ids.append(tg_id)

        self.sender = Sender(self.store, self.tr, on_sent=on_sent, clock=self.clock,
                             chat_interval_s=0, global_interval_s=0, sleep=self.sleeps)

    async def test_idempotent_enqueue(self) -> None:
        self.assertTrue(await self.store.enqueue(1, "k1", {"text": "a"}))
        self.assertFalse(await self.store.enqueue(1, "k1", {"text": "a"}))
        self.assertEqual(await self.store.counts(), {"pending": 1})

    async def test_order_within_chat(self) -> None:
        await self.store.enqueue(1, "a", {"text": "первое"})
        await self.store.enqueue(1, "b", {"text": "второе"})
        await self.store.enqueue(2, "c", {"text": "другой чат"})
        self.assertEqual(await self.sender.run_once(), 2)
        self.assertEqual(await self.sender.run_once(), 1)
        self.assertEqual([t for _, t, _ in self.tr.sent], ["первое", "другой чат", "второе"])
        self.assertEqual(len(self.sent_ids), 3)
        self.assertEqual(await self.store.counts(), {"sent": 3})

    async def test_permanent_error_fails_and_unblocks_chat(self) -> None:
        await self.store.enqueue(1, "a", {"text": "x"})
        await self.store.enqueue(1, "b", {"text": "y"})
        self.tr.errors = [TransportError("нет доступа", permanent=True)]
        self.assertEqual(await self.sender.run_once(), 0)
        self.assertEqual(await self.sender.run_once(), 1)
        self.assertEqual(await self.store.counts(), {"failed": 1, "sent": 1})

    async def test_transient_error_retries_later(self) -> None:
        await self.store.enqueue(1, "a", {"text": "x"})
        self.tr.errors = [ConnectionError("сеть")]
        self.assertEqual(await self.sender.run_once(), 0)
        self.assertEqual(await self.sender.run_once(), 0)  # ещё рано
        self.clock.advance(timedelta(seconds=3))
        self.assertEqual(await self.sender.run_once(), 1)

    async def test_gives_up_after_max_attempts(self) -> None:
        sender = Sender(self.store, self.tr, clock=self.clock, chat_interval_s=0,
                        global_interval_s=0, max_attempts=2, sleep=self.sleeps)
        await self.store.enqueue(1, "a", {"text": "x"})
        self.tr.errors = [ConnectionError(), ConnectionError()]
        await sender.run_once()
        self.clock.advance(timedelta(seconds=10))
        await sender.run_once()
        self.assertEqual(await self.store.counts(), {"failed": 1})

    async def test_flood_wait_pauses_everything(self) -> None:
        await self.store.enqueue(1, "a", {"text": "x"})
        await self.store.enqueue(2, "b", {"text": "y"})
        self.tr.errors = [FloodWait(10)]
        self.assertEqual(await self.sender.run_once(), 0)
        self.clock.advance(timedelta(seconds=5))
        self.assertEqual(await self.sender.run_once(), 0)
        self.clock.advance(timedelta(seconds=7))
        self.assertEqual(await self.sender.run_once(), 2)

    async def test_typing_delay(self) -> None:
        await self.store.enqueue(1, "a", {"text": "x", "typing_s": 1.5})
        await self.sender.run_once()
        self.assertEqual(self.tr.typing, [1])
        self.assertEqual(self.sleeps.calls, [1.5])

    async def test_chat_interval(self) -> None:
        sender = Sender(self.store, self.tr, clock=self.clock, chat_interval_s=1,
                        global_interval_s=0, sleep=self.sleeps)
        await self.store.enqueue(1, "a", {"text": "x"})
        await self.store.enqueue(1, "b", {"text": "y"})
        self.assertEqual(await sender.run_once(), 1)
        self.assertEqual(await sender.run_once(), 0)
        self.clock.advance(timedelta(seconds=1))
        self.assertEqual(await sender.run_once(), 1)

    async def test_recover_and_cancel(self) -> None:
        await self.store.enqueue(1, "a", {"text": "x"})
        await self.store.enqueue(1, "b", {"text": "y"})
        self.assertEqual(len(await self.store.claim_due()), 1)
        self.assertEqual(await self.store.recover(), 1)
        self.assertEqual(await self.store.cancel_chat(1), 2)
        self.assertEqual(await self.store.counts(), {"cancelled": 2})


class SanitizeTests(unittest.TestCase):
    def test_clean(self) -> None:
        self.assertEqual(clean_incoming("при\u202eвет\x00 "), "привет")
        self.assertEqual(clean_outgoing("  а   б \n\n"), ["а б"])
        self.assertEqual(clean_outgoing("👩\u200d💻"), ["👩\u200d💻"])
        parts = clean_outgoing(("слово " * 1500).strip())
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(len(p) <= TELEGRAM_LIMIT for p in parts))
