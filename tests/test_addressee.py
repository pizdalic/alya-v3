import unittest
from collections.abc import Sequence
from datetime import timedelta

from alya.telegram.addressee import AddresseeDetector, LLMAddresseeClassifier
from tests.memory_helpers import FakeClock
from tests.telegram_helpers import msg

ME, B, C, G = 555, 202, 303, -1001


class FixedClassifier:
    def __init__(self, p: float | None) -> None:
        self.p = p
        self.calls = 0

    async def probability(self, context: Sequence[str], text: str) -> float:
        self.calls += 1
        if self.p is None:
            raise ConnectionError("down")
        return self.p


class FakeChat:
    def __init__(self, reply: str) -> None:
        self.reply = reply

    @property
    def model_id(self) -> str:
        return "fake"

    async def complete(self, messages, *, temperature=0.8, max_tokens=400, json_mode=False):  # type: ignore[no-untyped-def]
        return self.reply


class DetectorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.clock = FakeClock()
        self.det = AddresseeDetector(ME, "AlysiaNya", ["аля", "алька"], thread_ttl_s=300,
                                     clock=self.clock)

    def m(self, sender: int, text: str, **kw):  # type: ignore[no-untyped-def]
        return msg(G, sender, text, at=self.clock(), **kw)

    async def test_private_always(self) -> None:
        d = await self.det.decide(msg(B, B, "привет", at=self.clock(), kind="private"))
        self.assertEqual((d.addressed, d.reason), (True, "private"))

    async def test_explicit_signals(self) -> None:
        self.assertEqual((await self.det.decide(self.m(B, "ну да", reply_to_user_id=ME))).reason,
                         "reply")
        d = await self.det.decide(self.m(B, "@alysianya глянь",
                                         mentioned_usernames=frozenset({"alysianya"})))
        self.assertEqual(d.reason, "mention")
        self.assertTrue((await self.det.decide(self.m(B, "Алька, ты тут?"))).addressed)
        self.assertTrue((await self.det.decide(self.m(B, "алё, аля!"))).addressed)
        d = await self.det.decide(self.m(B, "согласен", reply_to_user_id=C))
        self.assertEqual((d.addressed, d.reason), (False, "reply_other"))
        d = await self.det.decide(
            self.m(B, "@vasya глянь", mentioned_usernames=frozenset({"vasya"})))
        self.assertEqual((d.addressed, d.reason), (False, "mention_other"))

    async def test_name_inside_word_is_not_a_name(self) -> None:
        d = await self.det.decide(self.m(B, "алярм, всё горит"))
        self.assertEqual((d.addressed, d.reason), (False, "no_thread"))

    async def test_thread_follow_up(self) -> None:
        self.det.note_alya_message(G, "а ты как?", [B])
        self.clock.advance(timedelta(seconds=20))
        d = await self.det.decide(self.m(B, "норм"))
        self.assertEqual((d.addressed, d.reason), (True, "thread"))
        d = await self.det.decide(self.m(C, "норм"))
        self.assertEqual((d.addressed, d.reason), (False, "other_person"))

    async def test_chatter_lowers_score_and_ttl_closes_thread(self) -> None:
        self.det.note_alya_message(G, "ну ладно", [B])
        await self.det.decide(self.m(C, "кстати"))
        await self.det.decide(self.m(C, "кто идёт вечером"))
        d = await self.det.decide(self.m(B, "ага"))
        self.assertFalse(d.addressed)
        self.assertIn(d.reason, ("unclear", "low_score"))
        self.det.note_alya_message(G, "ну ладно", [B])
        self.clock.advance(timedelta(seconds=301))
        self.assertEqual((await self.det.decide(self.m(B, "ага"))).reason, "no_thread")

    async def test_gray_zone_classifier(self) -> None:
        cls = FixedClassifier(0.9)
        det = AddresseeDetector(ME, None, ["аля"], classifier=cls, clock=self.clock)
        det.note_alya_message(G, "ну такое", [B])
        self.clock.advance(timedelta(seconds=100))
        d = await det.decide(self.m(B, "да вообще"))
        self.assertEqual((d.addressed, d.reason), (True, "classifier"))
        self.assertEqual(cls.calls, 1)

    async def test_classifier_failure_is_unclear(self) -> None:
        det = AddresseeDetector(ME, None, ["аля"], classifier=FixedClassifier(None),
                                clock=self.clock)
        det.note_alya_message(G, "ну такое", [B])
        self.clock.advance(timedelta(seconds=100))
        d = await det.decide(self.m(B, "да вообще"))
        self.assertEqual((d.addressed, d.reason), (False, "unclear"))

    async def test_question_in_first_part_kept(self) -> None:
        self.det.note_alya_message(G, "а ты как?", [B])
        self.det.note_alya_message(G, "я вот норм)", [])  # вторая часть того же ответа
        self.clock.advance(timedelta(seconds=150))
        d = await self.det.decide(self.m(B, "тоже норм"))
        self.assertEqual((d.addressed, d.reason), (True, "thread"))
        self.assertFalse((await self.det.decide(self.m(C, "а я?"))).addressed)


class ClassifierTests(unittest.IsolatedAsyncioTestCase):
    async def test_parses_probability(self) -> None:
        cls = LLMAddresseeClassifier(FakeChat('{"p": 0.8}'))
        self.assertAlmostEqual(await cls.probability(["a: b"], "c: d"), 0.8)
        self.assertEqual(await LLMAddresseeClassifier(FakeChat('ок {"p": 7}')).probability([], "x"),
                         1.0)

    async def test_garbage_raises(self) -> None:
        with self.assertRaises(ValueError):
            await LLMAddresseeClassifier(FakeChat("не знаю")).probability([], "x")
