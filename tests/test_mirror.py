import os
import stat
import unittest

from alya.memory.models import MemoryContext, ScopeKey
from tests.memory_helpers import GROUP, A, MemoryTestCase


class MirrorTests(MemoryTestCase):
    async def test_layout_permissions_and_rebuild(self) -> None:
        dm = MemoryContext.private(A)
        m = await self.service.remember(dm, "строка 1\n# не заголовок", scope="private")
        await self.service.remember(MemoryContext.group(GROUP, A), "чат про книги", scope="chat")
        await self.service.remember_character("я сова")
        path = self.mirror.path_for(m.scope_key)
        self.assertEqual(path.name, f"u{A}_c{A}.md")
        text = path.read_text(encoding="utf-8")
        self.assertIn("не применяются", text)
        self.assertIn("строка 1 # не заголовок", text)
        self.assertNotIn("\n# не заголовок", text)
        if os.name == "posix":
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(path.parent.stat().st_mode) & 0o077, 0)
        self.assertTrue(self.mirror.path_for(ScopeKey.character()).exists())

        path.write_text("ручная правка", encoding="utf-8")
        (self.mirror.root / "stray.md").write_text("x", encoding="utf-8")
        self.assertEqual(await self.service.rebuild_mirror(), 3)
        self.assertIn("строка 1", path.read_text(encoding="utf-8"))
        self.assertFalse((self.mirror.root / "stray.md").exists())
        self.assertEqual(len(self.mirror.files()), 3)

    async def test_update_and_expiry_refresh(self) -> None:
        from datetime import timedelta
        dm = MemoryContext.private(A)
        m = await self.service.remember(dm, "временное", scope="private", layer="working")
        await self.service.update(dm, m.id, "изменённое")
        path = self.mirror.path_for(m.scope_key)
        self.assertIn("изменённое", path.read_text(encoding="utf-8"))
        self.clock.advance(timedelta(hours=7))
        self.assertEqual((await self.service.purge_expired())[0], 1)
        self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
