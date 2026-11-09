"""Настроение: шум не пишется, чужое ослаблено, причины — только из своего чата."""

from __future__ import annotations

import unittest

from alya.character.emotion import VAD, EmotionStore
from alya.character.engine import NOT_ADDRESSED_FACTOR, mood_delta
from alya.character.persona import EmotionProfile, VADPoint
from tests.memory_helpers import GROUP, A, MemoryTestCase

PROFILE = EmotionProfile(baseline=VADPoint(valence=0.2, arousal=0.0, dominance=0.0),
                         sensitivity=1.0, max_step=0.15, half_life_hours=6.0,
                         ignore_below=-0.6)


class MoodDeltaTests(unittest.TestCase):
    def test_noise_is_dropped(self) -> None:
        self.assertIsNone(mood_delta(VAD(0.05, -0.03, 0.0), addressed=True))

    def test_not_addressed_is_weakened(self) -> None:
        d = mood_delta(VAD(-0.8, 0.0, 0.0), addressed=False)
        assert d is not None
        self.assertAlmostEqual(d.valence, -0.8 * NOT_ADDRESSED_FACTOR)

    def test_addressed_is_full(self) -> None:
        d = mood_delta(VAD(-0.8, 0.0, 0.0), addressed=True)
        assert d is not None
        self.assertAlmostEqual(d.valence, -0.8)


class ReasonsTests(MemoryTestCase):
    async def test_small_shift_not_in_diary(self) -> None:
        store = EmotionStore(self.db, "shy", PROFILE, clock=self.clock)
        await store.feel(VAD(0.03, 0, 0), "ничего особенного", chat_id=GROUP)
        self.assertEqual(await store.diary(), [])

    async def test_reasons_are_chat_scoped(self) -> None:
        store = EmotionStore(self.db, "shy", PROFILE, clock=self.clock)
        await store.feel(VAD(0.5, 0, 0), "личное: похвалили", chat_id=A, user_id=A)
        await store.feel(VAD(-0.5, 0, 0), "в группе спорили", chat_id=GROUP, user_id=A)
        self.assertEqual(await store.reasons(GROUP), ["в группе спорили"])
        self.assertEqual(await store.reasons(A), ["личное: похвалили"])
