import unittest
from datetime import timedelta

import numpy as np

from alya.memory.fts import build_match
from alya.memory.indexer import EmbeddingIndexer
from alya.memory.models import MemoryContext
from alya.memory.search import rrf
from alya.memory.vectors import (
    NumpyBackend,
    SqliteVecBackend,
    cosine_top_k,
    decode,
    encode,
    load_sqlite_vec,
)
from tests.memory_helpers import GROUP, A, MemoryTestCase

A_DM = MemoryContext.private(A)


class FtsQueryTests(unittest.TestCase):
    def test_injection_is_neutralized(self) -> None:
        q = build_match('кот" OR * NEAR( AND -- ; DROP TABLE memories')
        assert q is not None
        self.assertNotIn("NEAR(", q)
        for term in q.split(" OR "):
            self.assertRegex(term, r'^"[^"\s]+"\*?$')

    def test_stopwords_and_empty(self) -> None:
        self.assertIsNone(build_match("и в на ?!"))
        self.assertIsNone(build_match(""))

    def test_prefix_and_yo(self) -> None:
        self.assertEqual(build_match("Ёжики"), '"ежик"*')
        self.assertEqual(build_match("кот кот"), '"кот"')

    def test_rrf(self) -> None:
        s = rrf([["a", "b"], ["b", "c"]], k=60)
        self.assertGreater(s["b"], s["a"])
        self.assertAlmostEqual(s["a"], 1 / 61)


class SearchTests(MemoryTestCase):
    async def test_fts_without_embeddings(self) -> None:
        m = await self.repo.add(A_DM, "Любимый напиток — малиновый чай", scope="private")
        await self.repo.add(A_DM, "работает программистом", scope="private")
        res = await self.search.search(A_DM, "какой чай с малиной она любит?")
        self.assertTrue(res.used_fts)
        self.assertFalse(res.used_vectors)
        self.assertIsNotNone(res.degraded)
        self.assertEqual(res.hits[0].memory.id, m.id)

    async def test_yo_folding_in_content(self) -> None:
        m = await self.repo.add(A_DM, "у неё есть ёжик", scope="private")
        res = await self.search.search(A_DM, "ежик")
        self.assertEqual([h.memory.id for h in res.hits], [m.id])

    async def test_touch_and_expiry(self) -> None:
        m = await self.repo.add(A_DM, "сегодня болит голова", scope="private", layer="working")
        res = await self.search.search(A_DM, "голова")
        self.assertEqual(len(res.hits), 1)
        got = await self.repo.get(A_DM, m.id)
        assert got is not None
        self.assertEqual(got.usage_count, 1)
        self.clock.advance(timedelta(hours=7))
        self.assertEqual((await self.search.search(A_DM, "голова")).hits, [])
        self.assertEqual((await self.repo.purge_expired())[0], 1)

    async def test_superseded_hidden(self) -> None:
        old = await self.repo.add(A_DM, "живёт в Москве", scope="private")
        new = await self.repo.add(A_DM, "переехала в Казань, раньше жила в Москве",
                                  scope="private")
        self.assertTrue(await self.repo.supersede(A_DM, old.id, new.id))
        ids = [h.memory.id for h in (await self.search.search(A_DM, "Москва")).hits]
        self.assertEqual(ids, [new.id])

    async def test_update_reindexes_fts(self) -> None:
        m = await self.repo.add(A_DM, "любит кошек", scope="private")
        await self.repo.update_content(A_DM, m.id, "любит попугаев")
        self.assertEqual((await self.search.search(A_DM, "кошек")).hits, [])
        self.assertEqual(len((await self.search.search(A_DM, "попугаев")).hits), 1)
        rows = await self.db.fetchall(
            "INSERT INTO memories_fts(memories_fts, rank) VALUES('integrity-check', 1)")
        self.assertEqual(rows, [])


class VectorSearchTests(MemoryTestCase):
    use_embedder = True

    async def test_hybrid_and_indexer(self) -> None:
        assert self.embedder is not None
        m = await self.repo.add(A_DM, "обожает горный туризм", scope="private")
        await self.repo.add(A_DM, "сегодня встреча", scope="private", layer="working")
        indexer = EmbeddingIndexer(self.db, self.embedder, clock=self.clock)
        self.assertEqual(await indexer.embed_pending(), 1)  # working не индексируется
        self.assertEqual(await indexer.embed_pending(), 0)
        res = await self.search.search(A_DM, "горный поход")
        self.assertTrue(res.used_vectors)
        self.assertIsNone(res.degraded)
        self.assertEqual(res.hits[0].memory.id, m.id)
        self.assertIsNotNone(res.hits[0].vector_rank)

    async def test_update_drops_embedding(self) -> None:
        assert self.embedder is not None
        m = await self.repo.add(A_DM, "любит джаз", scope="private")
        indexer = EmbeddingIndexer(self.db, self.embedder, clock=self.clock)
        await indexer.embed_pending()
        await self.repo.update_content(A_DM, m.id, "любит рок")
        rows = await self.db.fetchall("SELECT COUNT(*) FROM memory_embeddings")
        self.assertEqual(rows[0][0], 0)

    async def test_degraded_on_failure(self) -> None:
        assert self.embedder is not None
        await self.repo.add(A_DM, "любит чай", scope="private")
        self.embedder.fail = True
        res = await self.search.search(A_DM, "чай")
        self.assertFalse(res.used_vectors)
        self.assertIn("недоступны", res.degraded or "")
        self.assertEqual(len(res.hits), 1)

    async def test_group_vectors_isolated(self) -> None:
        assert self.embedder is not None
        await self.repo.add(A_DM, "секретный пароль от сейфа", scope="private")
        await EmbeddingIndexer(self.db, self.embedder, clock=self.clock).embed_pending()
        res = await self.search.search(MemoryContext.group(GROUP, A), "секретный пароль сейфа")
        self.assertEqual(res.hits, [])


class VectorMathTests(MemoryTestCase):
    def test_roundtrip_and_topk(self) -> None:
        v = np.array([1.0, 0.0, 2.5], dtype=np.float32)
        self.assertTrue(np.array_equal(decode(encode(v), 3), v))
        m = np.array([[1, 0], [0, 1], [1, 1]], dtype=np.float32)
        self.assertEqual([i for i, _ in cosine_top_k(np.array([1, 0.1], np.float32), m, 2)],
                         [0, 2])
        with self.assertRaises(ValueError):
            encode(np.array([np.nan], dtype=np.float32))

    async def test_numpy_matches_sqlite_vec(self) -> None:
        if not load_sqlite_vec(self.db.conn):
            self.skipTest("sqlite-vec не установлен")
        rng = np.random.default_rng(7)
        from alya.memory.indexer import store_embedding
        now = self.repo.now_iso()
        for i in range(200):
            m = await self.repo.add(A_DM, f"запись {i}", scope="private")
            vec = rng.standard_normal(32).astype(np.float32)
            await self.db.transaction(
                lambda c, m=m, vec=vec: store_embedding(c, m.id, "x", vec, m.content, now))
        q = rng.standard_normal(32).astype(np.float32)
        a = await self.db.read(lambda c: NumpyBackend().top_k(c, A_DM, q, "x", 10, now))
        b = await self.db.read(lambda c: SqliteVecBackend().top_k(c, A_DM, q, "x", 10, now))
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main()
