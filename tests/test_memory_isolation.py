import unittest

from alya.memory.models import MemoryContext, MemoryScopeError
from tests.memory_helpers import GROUP, GROUP2, A, B, MemoryTestCase

A_DM = MemoryContext.private(A)
B_DM = MemoryContext.private(B)
A_G = MemoryContext.group(GROUP, A)
B_G = MemoryContext.group(GROUP, B)
A_G2 = MemoryContext.group(GROUP2, A)


class IsolationTests(MemoryTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.a_secret = await self.repo.add(A_DM, "у меня аллергия на арахис", scope="private")
        self.b_secret = await self.repo.add(B_DM, "боюсь собак с детства", scope="private")
        self.g_fact = await self.repo.add(A_G, "в этом чате обсуждают аниме", scope="chat",
                                          subject_user_id=A)
        self.self_fact = await self.repo.add_character("я люблю дождливые вечера")

    async def ids(self, ctx: MemoryContext) -> set[str]:
        return {m.id for m in await self.repo.list(ctx)}

    async def test_dm_sees_only_own_private(self) -> None:
        self.assertEqual(await self.ids(A_DM), {self.a_secret.id, self.self_fact.id})
        self.assertEqual(await self.ids(B_DM), {self.b_secret.id, self.self_fact.id})

    async def test_group_never_sees_private(self) -> None:
        for ctx in (A_G, B_G):
            self.assertEqual(await self.ids(ctx), {self.g_fact.id, self.self_fact.id})
        self.assertEqual(await self.ids(A_G2), {self.self_fact.id})

    async def test_get_invisible_is_none(self) -> None:
        self.assertIsNone(await self.repo.get(B_DM, self.a_secret.id))
        self.assertIsNone(await self.repo.get(A_G, self.a_secret.id))
        self.assertIsNone(await self.repo.get(A_DM, self.g_fact.id))
        self.assertIsNotNone(await self.repo.get(A_DM, self.a_secret.id))

    async def test_search_isolation(self) -> None:
        for ctx, forbidden in ((B_DM, self.a_secret.id), (A_G, self.a_secret.id),
                               (B_G, self.b_secret.id), (A_DM, self.b_secret.id)):
            res = await self.search.search(ctx, "аллергия арахис боюсь собак", k=20)
            self.assertNotIn(forbidden, {h.memory.id for h in res.hits})
        res = await self.search.search(A_DM, "арахис")
        self.assertEqual([h.memory.id for h in res.hits], [self.a_secret.id])

    async def test_cannot_modify_foreign(self) -> None:
        self.assertIsNone(await self.repo.update_content(B_DM, self.a_secret.id, "взлом"))
        self.assertFalse(await self.repo.supersede(B_DM, self.a_secret.id, self.b_secret.id))
        self.assertFalse(await self.repo.supersede(A_DM, self.a_secret.id, self.b_secret.id))
        m = await self.repo.get(A_DM, self.a_secret.id)
        assert m is not None
        self.assertEqual(m.content, "у меня аллергия на арахис")

    async def test_write_rules(self) -> None:
        with self.assertRaises(MemoryScopeError):
            await self.repo.add(A_G, "x", scope="private")
        with self.assertRaises(MemoryScopeError):
            await self.repo.add(A_DM, "x", scope="chat")
        with self.assertRaises(MemoryScopeError):
            await self.repo.add(A_DM, "x", scope="private", subject_user_id=B)
        with self.assertRaises(ValueError):
            await self.repo.add(A_DM, "   ", scope="private")
        with self.assertRaises(ValueError):
            await self.repo.add(A_DM, "x", scope="private", category="Плохая")
        with self.assertRaises(ValueError):
            await self.repo.add(A_DM, "x" * 4001, scope="private")

    async def test_db_constraint_blocks_bad_rows(self) -> None:
        import sqlite3
        with self.assertRaises(sqlite3.IntegrityError):
            await self.db.execute(
                "INSERT INTO memories (id, scope, owner_user_id, chat_id, layer, category, "
                "content, provenance, confidence, created_at, updated_at) "
                "VALUES ('x', 'private', NULL, ?, 'long', 'fact', 'c', '{}', 0.5, 't', 't')",
                (A,))

    async def test_forget_permissions(self) -> None:
        with self.assertRaises(MemoryScopeError):
            await self.service.forget_memory(A_DM, self.self_fact.id)
        g_about_a = self.g_fact
        with self.assertRaises(MemoryScopeError):
            await self.service.forget_memory(B_G, g_about_a.id)
        self.assertIsNone(await self.service.forget_memory(B_DM, self.a_secret.id))
        report = await self.service.forget_memory(A_G, g_about_a.id)
        assert report is not None
        self.assertEqual(report.memories, 1)


if __name__ == "__main__":
    unittest.main()
