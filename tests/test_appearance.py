"""Внешность Али: аниме-кошкодевочка, селфи — иллюстрация, а не фото."""

from __future__ import annotations

import unittest

from alya.character.engine import CharacterEngine
from alya.character.persona import load_character
from tests.test_character import ROOT


class AppearanceTests(unittest.TestCase):
    def test_core_describes_catgirl(self) -> None:
        core = load_character(ROOT, "shy").core
        for word in ("белые волосы", "синие глаза", "кошачьи ушки", "хвостик"):
            self.assertIn(word, core.appearance)
            self.assertIn(word, core.about)

    def test_selfie_prompt_is_anime(self) -> None:
        ch = load_character(ROOT, "shy")
        engine = CharacterEngine.__new__(CharacterEngine)
        engine.character = ch
        prompt = engine.selfie_prompt("под  одеялом")
        self.assertIn("Аниме-иллюстрация", prompt)
        self.assertIn("не реальный человек", prompt)
        self.assertNotIn("Фотореалистичное", prompt)
        self.assertIn("Сцена: под одеялом.", prompt)
