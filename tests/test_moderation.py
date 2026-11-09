import json
from datetime import timedelta

from alya.core.config import ModerationSection
from alya.telegram.moderation import ModerationRules, Moderator
from tests.telegram_helpers import DbTestCase, msg

GROUP = -1001
OWNER, B = 101, 202


class ModerationTests(DbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.mod = Moderator(self.db, ModerationRules(banned_words=("Казино",),
                                                      exempt_user_ids=(OWNER,)), clock=self.clock)

    def rows(self) -> list[dict]:  # type: ignore[type-arg]
        cur = self.db.conn.execute("SELECT details FROM audit_log WHERE action = "
                                   "'moderation_dry_run' ORDER BY id")
        return [json.loads(r[0]) for r in cur.fetchall()]

    async def test_flood_logged_once_per_window(self) -> None:
        t0 = self.clock()
        for i in range(10):
            await self.mod.check(msg(GROUP, B, f"а {i}", at=t0 + timedelta(seconds=i)))
        rows = self.rows()
        self.assertEqual([r["rule"] for r in rows], ["flood"])
        self.assertEqual(rows[0]["would"], "mute")
        self.assertTrue(rows[0]["dry_run"])

    async def test_slow_chat_is_not_flood(self) -> None:
        t0 = self.clock()
        for i in range(20):
            await self.mod.check(msg(GROUP, B, "привет", at=t0 + timedelta(seconds=10 * i)))
        self.assertEqual(self.rows(), [])

    async def test_links_and_banned_words_without_text_in_log(self) -> None:
        text = "лучшее казино http://a.io http://b.io www.c.io t.me/d"
        verdicts = await self.mod.check(msg(GROUP, B, text, at=self.clock()))
        self.assertEqual(sorted(v.rule for v in verdicts), ["banned_words", "links"])
        raw = self.db.conn.execute("SELECT group_concat(details) FROM audit_log").fetchone()[0]
        self.assertNotIn("казино", raw)
        self.assertNotIn("a.io", raw)

    async def test_exempt_private_and_outgoing(self) -> None:
        bad = "казино"
        self.assertEqual(await self.mod.check(msg(GROUP, OWNER, bad, at=self.clock())), [])
        self.assertEqual(await self.mod.check(msg(B, B, bad, at=self.clock(), kind="private")), [])
        self.assertEqual(await self.mod.check(msg(GROUP, B, bad, at=self.clock(),
                                                  is_outgoing=True)), [])
        self.assertEqual(self.rows(), [])

    def test_dry_run_cannot_be_disabled(self) -> None:
        self.assertTrue(ModerationSection().dry_run)
        with self.assertRaises(ValueError):
            ModerationSection(dry_run=False)  # type: ignore[arg-type]
