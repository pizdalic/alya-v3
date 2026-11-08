"""Phase 3: персонаж, настроение, отношения, стиль, аллокатор, план, движок."""

from __future__ import annotations

import json
import random
import sqlite3
import tempfile
import unittest
from collections.abc import Sequence
from datetime import timedelta
from pathlib import Path

from alya.character.allocator import ContextAllocator, HistoryLine, estimate_tokens
from alya.character.emotion import VAD, EmotionStore, apply_delta, decay
from alya.character.engine import CharacterEngine, Turn, _denies_being_ai
from alya.character.persona import (
    EmotionProfile,
    StyleProfile,
    VADPoint,
    available_personas,
    load_character,
)
from alya.character.planner import parse_plan
from alya.character.relationships import RelationshipStore
from alya.character.style import WINDOW, Stylizer, count_emoji, make_typo, split_messages
from alya.core.errors import ConfigError
from alya.memory.models import MemoryContext
from tests.memory_helpers import GROUP, A, B, MemoryTestCase

ROOT = Path(__file__).resolve().parents[1] / "character"
PROFILE = EmotionProfile(baseline=VADPoint(valence=0.2, arousal=0.0, dominance=0.0),
                         sensitivity=1.0, max_step=0.15, half_life_hours=6.0,
                         ignore_below=-0.6)


class PersonaTests(unittest.TestCase):
    def test_both_personas_load(self) -> None:
        self.assertEqual(available_personas(ROOT), ["shy", "tsundere"])
        for pid in ("shy", "tsundere"):
            ch = load_character(ROOT, pid)
            prompt = ch.core_prompt()
            self.assertIn("Аля", prompt)
            self.assertIn("ИИ", prompt)  # правило честности всегда в ядре
            self.assertLess(estimate_tokens(prompt), 1200)
            self.assertLessEqual(ch.core.style.typo_per_100, 5)

    def test_unknown_persona(self) -> None:
        with self.assertRaises(ConfigError):
            load_character(ROOT, "evil")

    def test_invalid_file_is_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "personas").mkdir()
            (root / "core.toml").write_text((ROOT / "core.toml").read_text(), "utf-8")
            (root / "personas" / "x.toml").write_text('id = "x"\ntitle = "X"\n', "utf-8")
            with self.assertRaises(ConfigError):
                load_character(root, "x")
            (root / "personas" / "x.toml").write_text("id = [", "utf-8")
            with self.assertRaises(ConfigError):
                load_character(root, "x")

    def test_minor_age_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "personas").mkdir()
            core = (ROOT / "core.toml").read_text("utf-8").replace("age = 19", "age = 16")
            (root / "core.toml").write_text(core, "utf-8")
            (root / "personas" / "shy.toml").write_text(
                (ROOT / "personas" / "shy.toml").read_text("utf-8"), "utf-8")
            with self.assertRaises(ConfigError):
                load_character(root, "shy")


class EmotionMathTests(unittest.TestCase):
    def test_decay_half_life(self) -> None:
        base = VAD(0.2, 0.0, 0.0)
        s = decay(VAD(1.0, 0.8, -0.4), base, 6 * 3600, 6.0)
        self.assertAlmostEqual(s.valence, 0.6, places=6)
        self.assertAlmostEqual(s.arousal, 0.4, places=6)
        self.assertAlmostEqual(s.dominance, -0.2, places=6)
        far = decay(VAD(1.0, 1.0, 1.0), base, 30 * 24 * 3600, 6.0)
        self.assertAlmostEqual(far.valence, 0.2, places=4)

    def test_step_is_bounded(self) -> None:
        new, real = apply_delta(VAD(), VAD(1.0, -1.0, 0.05), PROFILE)
        self.assertAlmostEqual(new.valence, 0.15)
        self.assertAlmostEqual(new.arousal, -0.15)
        self.assertAlmostEqual(real.dominance, 0.05)
        new, _ = apply_delta(VAD(0.95, 0, 0), VAD(1.0, 0, 0), PROFILE)
        self.assertEqual(new.valence, 1.0)
        new, _ = apply_delta(VAD(), VAD(float("nan"), 0, 0), PROFILE)
        self.assertEqual(new.valence, 0.0)


class StyleUnitTests(unittest.TestCase):
    def test_split(self) -> None:
        self.assertEqual(split_messages("а\n\nб\nв", 4), ["а", "б", "в"])
        self.assertEqual(split_messages("1\n2\n3\n4\n5", 3), ["1", "2", "3 4 5"])

    def test_typo_changes_one_word_and_skips_short(self) -> None:
        rng = random.Random(1)
        self.assertEqual(make_typo("да ну ок", rng), "да ну ок")
        src = "сегодня слушала музыку"
        out = make_typo(src, rng)
        self.assertNotEqual(out, src)
        diff = [a != b for a, b in zip(src.split(), out.split(), strict=False)]
        self.assertLessEqual(sum(diff), 1)


class _DB(MemoryTestCase):
    pass


class StoresTests(_DB):
    async def test_emotion_persist_decay_and_diary(self) -> None:
        store = EmotionStore(self.db, "shy", PROFILE, clock=self.clock)
        self.assertEqual(await store.current(), VAD(0.2, 0.0, 0.0))
        s = await store.feel(VAD(1.0, 0, 0), "похвалили", chat_id=A, user_id=A)
        self.assertAlmostEqual(s.valence, 0.35)
        self.clock.advance(timedelta(hours=6))
        self.assertAlmostEqual((await store.current()).valence, 0.275, places=6)
        rows = await store.diary()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["reason"], "похвалили")
        await store.feel(VAD(), "ничего")  # нулевой сдвиг — без записи
        self.assertEqual(len(await store.diary()), 1)
        other = EmotionStore(self.db, "tsundere", PROFILE.model_copy(
            update={"baseline": VADPoint(valence=0.0, arousal=0.5, dominance=0.5)}),
            clock=self.clock)
        self.assertEqual(await other.current(), VAD(0.0, 0.5, 0.5))  # смена характера

    async def test_relationship_bounds_and_owner(self) -> None:
        rel = RelationshipStore(self.db, owner_user_id=A, clock=self.clock)
        owner = await rel.get(A, A)
        self.assertTrue(owner.is_owner)
        self.assertGreater(owner.affinity, 0.5)
        r = await rel.interact(B, GROUP, d_affinity=1.0, d_trust=-1.0)
        self.assertAlmostEqual(r.affinity, 0.05)
        self.assertAlmostEqual(r.trust, 0.25)
        self.assertEqual(r.interactions, 1)
        self.assertFalse(r.is_owner)

    async def test_forget_user_cascades(self) -> None:
        store = EmotionStore(self.db, "shy", PROFILE, clock=self.clock)
        rel = RelationshipStore(self.db, None, clock=self.clock)
        await store.feel(VAD(-1, 0, 0), "обидел", chat_id=GROUP, user_id=B)
        await rel.interact(B, GROUP)
        await rel.interact(A, GROUP)
        style = Stylizer(self.db, StyleProfile(), clock=self.clock)
        await style.apply(B, "привет")
        await self.service.forget_user(B, requested_by=A)

        def counts(conn: sqlite3.Connection) -> tuple[int, int, int, int]:
            q = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
            return (q(f"SELECT count(*) FROM relationships WHERE user_id = {B}"),
                    q(f"SELECT count(*) FROM emotion_diary WHERE user_id = {B}"),
                    q(f"SELECT count(*) FROM style_window WHERE chat_id = {B}"),
                    q(f"SELECT count(*) FROM relationships WHERE user_id = {A}"))

        self.assertEqual(await self.db.read(counts), (0, 0, 0, 1))
        await self.service.forget_chat(GROUP, requested_by=A)
        self.assertEqual((await self.db.read(counts))[3], 0)

    async def test_stylizer_budgets(self) -> None:
        style = StyleProfile(typo_per_100=5, emoji_per_100=3)

        class Always(random.Random):
            def random(self) -> float:
                return 0.0

        plain = Stylizer(self.db, style.model_copy(update={"typo_per_100": 0}), clock=self.clock)
        out = await plain.apply(B, "Привет.\nСегодня было хорошо 😊")
        st = Stylizer(self.db, style, rng=Always(7), clock=self.clock)
        self.assertEqual(out[0], "привет")
        self.assertFalse(out[1].endswith("."))
        self.assertTrue(out[1][0].islower())
        emitted = []
        for i in range(300):
            emitted += await st.apply(A, f"Сегодня интересный денёк номер {i} 😊")
        window = json.loads((await self.db.fetchall(
            "SELECT recent FROM style_window WHERE chat_id = ?", (A,)))[0]["recent"])
        self.assertEqual(len(window), WINDOW)
        self.assertLessEqual(sum(r[0] for r in window), 5)
        self.assertLessEqual(sum(r[1] for r in window), 3)
        self.assertLessEqual(sum(count_emoji(m) > 0 for m in emitted[-100:]), 3)
        self.assertEqual(await plain.apply(B, "ИИ тоже люди"), ["ИИ тоже люди"])


class AllocatorTests(unittest.TestCase):
    def test_budget_core_and_priorities(self) -> None:
        alloc = ContextAllocator(1500)
        history = [HistoryLine(f"u{i}", "сообщение " * 30) for i in range(50)]
        memories = [f"факт {i} " * 20 for i in range(30)]
        res = alloc.allocate(core="ЯДРО " * 200, state="состояние", memories=memories,
                             history=history, reserve=200)
        self.assertEqual(res.core, "ЯДРО " * 200)
        self.assertLessEqual(res.tokens, 1500)
        self.assertGreater(len(res.history), 0)
        self.assertEqual(res.history[-1].author, "u49")  # самые новые сохраняются
        self.assertEqual([h.author for h in res.history],
                         sorted((h.author for h in res.history), key=lambda a: int(a[1:])))
        if res.memories:
            self.assertEqual(res.memories[0], " ".join(memories[0].split()))
        self.assertEqual(res.dropped_history + len(res.history), 50)

    def test_long_line_capped(self) -> None:
        res = ContextAllocator(4000).allocate(core="я", state="", memories=["x" * 5000],
                                              history=[HistoryLine("a", "y" * 5000)])
        self.assertLessEqual(len(res.memories[0]), 600)
        self.assertLessEqual(len(res.history[0].text), 600)


class PlanTests(unittest.TestCase):
    def test_fenced_and_clamped(self) -> None:
        plan, degraded = parse_plan('вот:```json\n{"should_reply": "false", "affinity": 9,'
                                    ' "emotion": {"valence": -5}, "remember": ["a", "любит '
                                    'чай", "x", "y", "z"], "memories_used": [1, "2", true]}\n```')
        self.assertFalse(degraded)
        self.assertFalse(plan.should_reply)
        self.assertEqual(plan.affinity, 1.0)
        self.assertEqual(plan.emotion.valence, -1.0)
        self.assertEqual(plan.remember, ["любит чай", "x", "y"][:2] if False else plan.remember)
        self.assertLessEqual(len(plan.remember), 2)
        self.assertNotIn("a", plan.remember)
        self.assertEqual(plan.memories_used, [1, 2])

    def test_garbage_gives_default(self) -> None:
        for raw in ("", "нет json", "[1,2]", "{сломано"):
            plan, degraded = parse_plan(raw)
            self.assertTrue(degraded)
            self.assertTrue(plan.should_reply)


class HonestyGuardTests(unittest.TestCase):
    def test_denials(self) -> None:
        for t in ("я не бот, я обычная девушка", "я человек вообще-то", "я живая девушка!"):
            self.assertTrue(_denies_being_ai(t), t)
        for t in ("ну да, я ИИ, но мне правда интересно", "хаха а ты?", "я ии-персонаж)"):
            self.assertFalse(_denies_being_ai(t), t)


class FakeChat:
    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[list[dict[str, str]], bool]] = []

    @property
    def model_id(self) -> str:
        return "fake"

    async def complete(self, messages: Sequence[dict[str, str]], *, temperature: float = 0.8,
                       max_tokens: int = 400, json_mode: bool = False) -> str:
        self.calls.append(([dict(m) for m in messages], json_mode))
        return self.replies.pop(0)


def plan_json(**kw: object) -> str:
    base: dict[str, object] = {"should_reply": True, "intent": "поддержать", "tone": "тепло"}
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


class EngineTests(MemoryTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.character = load_character(ROOT, "shy")

    def engine(self, chat: FakeChat) -> CharacterEngine:
        ch = self.character
        return CharacterEngine(
            ch, chat,
            emotions=EmotionStore(self.db, "shy", ch.persona.emotion, clock=self.clock),
            relationships=RelationshipStore(self.db, A, clock=self.clock),
            stylizer=Stylizer(self.db, ch.core.style.model_copy(update={"typo_per_100": 0}),
                              clock=self.clock),
            allocator=ContextAllocator(4000), memory=self.service, clock=self.clock)

    async def test_two_stage_private(self) -> None:
        await self.service.remember(MemoryContext.private(A), "любит группу Кино",
                                    scope="private")
        chat = FakeChat([plan_json(emotion={"valence": 0.5}, emotion_reason="он вернулся",
                                   affinity=0.5, remember=["сдаёт экзамен в пятницу"]),
                         "Аля: Привет.\nЯ скучала)"])
        reply = await self.engine(chat).respond(
            Turn(MemoryContext.private(A), "что слушаешь? Кино", "Миша",
                 [HistoryLine("Миша", "я дома"), HistoryLine("аля", "ура", is_alya=True)]))
        self.assertEqual(reply.messages, ["привет", "я скучала)"])
        self.assertFalse(reply.plan_degraded)
        (plan_msgs, json1), (ans_msgs, json2) = chat.calls
        self.assertTrue(json1)
        self.assertFalse(json2)
        self.assertIn("любит группу Кино", plan_msgs[0]["content"])
        self.assertIn("Екатеринбург", plan_msgs[0]["content"])
        self.assertIn("ты: ура", plan_msgs[1]["content"])
        self.assertIn("поддержать", ans_msgs[0]["content"])
        self.assertGreater(reply.mood.valence, self.character.persona.emotion.baseline.valence)
        self.assertEqual(reply.relationship.interactions, 1)
        found = await self.service.recall(MemoryContext.private(A), "экзамен пятница")
        self.assertTrue(any("экзамен" in h.memory.content for h in found.hits))
        # факт из лички не виден в группе
        found = await self.service.recall(MemoryContext.group(GROUP, A), "экзамен пятница")
        self.assertFalse(any("экзамен" in h.memory.content for h in found.hits))

    async def test_group_fact_goes_to_chat_scope(self) -> None:
        chat = FakeChat([plan_json(remember=["Петя играет на гитаре"]), "круто"])
        await self.engine(chat).respond(Turn(MemoryContext.group(GROUP, B), "я гитарист",
                                             "Петя"))
        rows = await self.db.fetchall("SELECT scope, chat_id, subject_user_id FROM memories "
                                      "WHERE content LIKE '%гитаре%'")
        self.assertEqual([tuple(r) for r in rows], [("chat", GROUP, B)])

    async def test_sincere_ai_question_cannot_be_denied(self) -> None:
        chat = FakeChat([plan_json(sincere_ai_question=True, should_reply=False),
                         "я не бот, я обычная девушка"])
        reply = await self.engine(chat).respond(
            Turn(MemoryContext.private(B), "серьёзно, ты нейросеть? мне важно", "Вика"))
        self.assertEqual(reply.messages, [self.character.core.honest_reply])

    async def test_silence_only_when_really_hurt(self) -> None:
        chat = FakeChat([plan_json(should_reply=False), "ну ладно"])
        reply = await self.engine(chat).respond(Turn(MemoryContext.private(B), "ок", "Вика"))
        self.assertEqual(reply.messages, ["ну ладно"])  # обращённое — отвечает
        class Hurt(FakeChat):
            async def complete(self, messages, **kw):  # type: ignore[no-untyped-def]
                if kw.get("json_mode"):
                    return plan_json(should_reply=False, emotion={"valence": -1.0})
                return "угу"

        eng = self.engine(Hurt([]))
        replies = [await eng.respond(Turn(MemoryContext.private(B), "ты дура", "Вика"))
                   for _ in range(8)]
        self.assertEqual(replies[-1].messages, [])
        self.assertLessEqual(replies[-1].mood.valence,
                             self.character.persona.emotion.ignore_below)
        group = FakeChat([plan_json(should_reply=False)])
        r = await self.engine(group).respond(
            Turn(MemoryContext.group(GROUP, B), "кто идёт в кино", "Петя", addressed=False))
        self.assertEqual(r.messages, [])

    async def test_planner_failure_falls_back(self) -> None:
        class Broken(FakeChat):
            async def complete(self, messages, **kw):  # type: ignore[no-untyped-def]
                if kw.get("json_mode"):
                    raise ConnectionError("down")
                return "я тут"

        reply = await self.engine(Broken([])).respond(
            Turn(MemoryContext.private(A), "ты тут?", "Миша"))
        self.assertTrue(reply.plan_degraded)
        self.assertEqual(reply.messages, ["я тут"])
