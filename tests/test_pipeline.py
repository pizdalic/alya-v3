from datetime import datetime, timedelta

from alya.telegram.addressee import AddresseeDetector
from alya.telegram.models import MessageEdited, MessagesDeleted
from alya.telegram.outbox import OutboxStore
from alya.telegram.pipeline import Pipeline, PipelineConfig
from alya.telegram.sender import Sender
from tests.telegram_helpers import DbTestCase, FakeEngine, FakeTransport, Sleeps, msg

ME, OWNER, B, C, STRANGER, G = 555, 101, 202, 303, 909, -1001


class PipelineTests(DbTestCase):
    async def build(self, replies=None, **cfg) -> None:  # type: ignore[no-untyped-def]
        self.engine = FakeEngine(replies)
        self.tr = FakeTransport()
        self.det = AddresseeDetector(ME, "AlysiaNya", ["аля"], clock=self.clock)
        self.outbox = OutboxStore(self.db, clock=self.clock)
        config = PipelineConfig(me_id=ME, owner_id=OWNER, debounce_s=0.01,
                                debounce_max_wait_s=0.05, **cfg)
        self.p = Pipeline(self.db, self.engine, self.det, self.outbox, config=config,
                          transport=self.tr, clock=self.clock)
        self.sender = Sender(self.outbox, self.tr, on_sent=self.p.on_sent, clock=self.clock,
                             chat_interval_s=0, global_interval_s=0, sleep=Sleeps())

    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        await self.build()

    async def feed(self, *msgs) -> None:  # type: ignore[no-untyped-def]
        for m in msgs:
            await self.p.on_message(m)
        await self.p.drain()
        while await self.sender.run_once():
            pass

    def at(self):  # type: ignore[no-untyped-def]
        return self.clock()

    async def rows(self, chat_id: int):  # type: ignore[no-untyped-def]
        return await self.db.fetchall(
            "SELECT text, is_outgoing, addressed_to_alya, context_only, expires_at "
            "FROM messages WHERE chat_id = ? ORDER BY id", (chat_id,))

    async def test_owner_dm_end_to_end(self) -> None:
        await self.build([["привет)", "как ты"]])
        await self.feed(msg(OWNER, OWNER, "привет", at=self.at(), kind="private"),
                        msg(OWNER, OWNER, "ты тут?", at=self.at(), kind="private"))
        self.assertEqual(len(self.engine.turns), 1)  # debounce склеил
        self.assertEqual(self.engine.turns[0].text, "привет\nты тут?")
        self.assertEqual([(t, r) for _, t, r in self.tr.sent],
                         [("привет)", None), ("как ты", None)])
        rows = await self.rows(OWNER)
        self.assertEqual([r[1] for r in rows], [0, 0, 1, 1])
        self.assertTrue(all(r[2] for r in rows[:2]))
        self.assertEqual(len(self.tr.read), 1)

    async def test_strangers_policy(self) -> None:
        await self.feed(msg(STRANGER, STRANGER, "привет", at=self.at(), kind="private"))
        self.assertEqual((len(self.engine.turns), await self.rows(STRANGER)), (0, []))
        await self.feed(msg(B, B, "привет", at=self.at(), kind="private", sender_is_contact=True))
        self.assertEqual(len(self.engine.turns), 1)
        await self.feed(msg(G, STRANGER, "всем привет", at=self.at()))  # теперь общая группа
        await self.feed(msg(STRANGER, STRANGER, "привет", at=self.at(), kind="private"))
        self.assertEqual(len(self.engine.turns), 2)

    async def test_owner_only_policy(self) -> None:
        await self.build(strangers="owner_only")
        await self.feed(msg(B, B, "привет", at=self.at(), kind="private", sender_is_contact=True))
        self.assertEqual(len(self.engine.turns), 0)

    async def test_group_thread_and_context(self) -> None:
        await self.build([["привет", "а ты как?"], ["я вот норм)"]])
        await self.feed(msg(G, C, "всем ку", at=self.at()))
        self.assertEqual(len(self.engine.turns), 0)
        ctx = (await self.rows(G))[0]
        self.assertEqual((ctx[2], ctx[3]), (0, 1))
        self.assertEqual(datetime.fromisoformat(ctx[4]), self.at() + timedelta(hours=48))
        first = msg(G, B, "аля привет", at=self.at())
        await self.feed(first)
        self.assertEqual(self.tr.sent[0][2], first.message_id)  # цитата в группе
        self.assertIsNone(self.tr.sent[1][2])
        self.clock.advance(timedelta(seconds=20))
        await self.feed(msg(G, B, "норм", at=self.at()))  # без имени — по нити
        self.assertEqual(len(self.engine.turns), 2)
        self.assertIsNone(self.tr.sent[2][2])
        history = [h.text for h in self.engine.turns[1].history]
        self.assertIn("а ты как?", history)
        await self.feed(msg(G, C, "а мне?", at=self.at()))
        self.assertEqual(len(self.engine.turns), 2)

    async def test_context_rows_expire(self) -> None:
        await self.feed(msg(G, C, "болтовня", at=self.at()))
        self.clock.advance(timedelta(hours=49))
        self.assertEqual(await self.p._history(G, []), [])

    async def test_owner_manual_message_opens_thread(self) -> None:
        await self.feed(msg(G, ME, "боб, ты идёшь?", at=self.at(), is_outgoing=True,
                            reply_to_user_id=B))
        await self.feed(msg(G, B, "ну да", at=self.at()))
        self.assertEqual(len(self.engine.turns), 1)
        self.assertEqual((await self.rows(G))[0][1], 1)

    async def test_edit_and_delete(self) -> None:
        m = msg(G, C, "опечатка", at=self.at())
        await self.feed(m)
        await self.p.on_edit(MessageEdited(G, m.message_id, "исправлено", self.at()))
        self.assertEqual((await self.rows(G))[0][0], "исправлено")
        await self.p.on_delete(MessagesDeleted(G, (m.message_id,)))
        self.assertEqual(await self.rows(G), [])

    async def test_rate_limit(self) -> None:
        await self.build(replies_per_10min=1)
        await self.feed(msg(OWNER, OWNER, "раз", at=self.at(), kind="private"))
        await self.feed(msg(OWNER, OWNER, "два", at=self.at(), kind="private"))
        self.assertEqual(len(self.engine.turns), 1)
        self.clock.advance(timedelta(minutes=11))
        await self.feed(msg(OWNER, OWNER, "три", at=self.at(), kind="private"))
        self.assertEqual(len(self.engine.turns), 2)

    async def test_channels_and_anonymous_ignored(self) -> None:
        await self.feed(msg(-1009, None, "пост", at=self.at(), kind="channel"),
                        msg(G, None, "аноним", at=self.at()))
        self.assertEqual(len(self.engine.turns), 0)
