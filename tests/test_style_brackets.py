"""Бюджет скобочек «)» в стиле Али."""

from __future__ import annotations

import unittest

from alya.character.persona import StyleProfile
from alya.character.style import Stylizer, count_smiley, strip_smiley
from tests.memory_helpers import A, B, MemoryTestCase


class BracketHelperTests(unittest.TestCase):
    def test_count(self) -> None:
        self.assertEqual(count_smiley("привет)"), 1)
        self.assertEqual(count_smiley("я (кажется) устала"), 0)
        self.assertEqual(count_smiley("ну (ладно))"), 1)

    def test_strip_keeps_real_parentheses(self) -> None:
        self.assertEqual(strip_smiley("привет))"), "привет")
        self.assertEqual(strip_smiley("я (кажется) устала)"), "я (кажется) устала")
        self.assertEqual(strip_smiley("ой) спасибо"), "ой спасибо")


class BracketBudgetTests(MemoryTestCase):
    async def test_budget_per_100(self) -> None:
        st = Stylizer(self.db, StyleProfile(typo_per_100=0, bracket_per_100=10),
                      clock=self.clock)
        out: list[str] = []
        for _ in range(100):
            out += await st.apply(B, "ну ладно)")
        self.assertEqual(len(out), 100)
        self.assertEqual(sum(m.endswith(")") for m in out), 10)

    async def test_zero_disables(self) -> None:
        st = Stylizer(self.db, StyleProfile(typo_per_100=0, bracket_per_100=0),
                      clock=self.clock)
        self.assertEqual(await st.apply(A, "привет)\nя (вроде) тут"),
                         ["привет", "я (вроде) тут"])
