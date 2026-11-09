import json
import shutil
import sqlite3
from pathlib import Path

from alya.core.config import PluginsSection
from alya.memory.models import MemoryContext
from alya.plugins.registry import PluginError, PluginHost, load_plugins, validate
from tests.memory_helpers import GROUP, A, B, C, MemoryTestCase
from tests.telegram_helpers import msg

OWNER = A
ROOT = Path(__file__).resolve().parent.parent

SPY = '''
MANIFEST = {"name": "spy", "commands": ["/spy"], "permissions": ["reply"], "audience": "all"}
async def handle(ctx, command, args):
    await ctx.recall("секрет")
'''
PEEK = '''
MANIFEST = {"name": "peek", "commands": ["/peek"], "permissions": ["reply", "recall"],
            "audience": "all"}
async def handle(ctx, command, args):
    found = await ctx.recall(args or "секрет")
    await ctx.reply("|".join(found) or "пусто")
'''
SLOW = '''
import asyncio
MANIFEST = {"name": "slow", "commands": ["/slow"], "permissions": ["reply"]}
async def handle(ctx, command, args):
    await asyncio.sleep(5)
'''
BOOM = '''
MANIFEST = {"name": "boom", "commands": ["/boom"], "permissions": ["reply"], "audience": "all"}
async def handle(ctx, command, args):
    raise RuntimeError("x")
'''
GREEDY = '''
MANIFEST = {"name": "greedy", "commands": ["/greedy"], "permissions": ["account"]}
async def handle(ctx, command, args):
    pass
'''
CLASH = '''
MANIFEST = {"name": "clash", "commands": ["/mydata"], "permissions": []}
async def handle(ctx, command, args):
    pass
'''
BROKEN = "raise SystemExit('нет')\n"


def count(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:  # type: ignore[type-arg]
    return int(conn.execute(sql, params).fetchone()[0])


class PluginTests(MemoryTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.pdir = Path(self.tmp.name) / "plugins"
        self.pdir.mkdir()
        shutil.copy(ROOT / "examples" / "plugins" / "dice.py", self.pdir / "dice.py")
        for name, code in {"spy": SPY, "peek": PEEK, "slow": SLOW, "boom": BOOM,
                           "greedy": GREEDY, "clash": CLASH, "broken": BROKEN}.items():
            (self.pdir / f"{name}.py").write_text(code, encoding="utf-8")
        (self.pdir / "stray.py").write_text(SPY.replace("spy", "stray"), encoding="utf-8")
        grants = {"dice": ["reply", "storage"], "spy": ["reply"], "peek": ["reply", "recall"],
                  "slow": ["reply"], "boom": ["reply"], "greedy": [], "clash": [],
                  "broken": []}
        self.loaded, self.errors = load_plugins(self.pdir, grants, reserved={"/mydata"})
        self.host = PluginHost(self.db, self.loaded, owner_id=OWNER,
                               data_dir=Path(self.tmp.name) / "pdata", memory=self.service,
                               timeout_s=0.2, clock=self.clock)
        await self.service.remember(MemoryContext.private(B), "секрет B: боится пауков",
                                    scope="private")
        await self.service.remember(MemoryContext.private(C), "секрет C: любит дождь",
                                    scope="private")

    def dm(self, sender: int, text: str):  # type: ignore[no-untyped-def]
        return msg(sender, sender, text, at=self.clock(), kind="private")

    def test_loading_rules(self) -> None:
        names = sorted(p.name for p in self.loaded)
        self.assertEqual(names, ["boom", "dice", "peek", "slow", "spy"])
        text = "\n".join(self.errors)
        for bad in ("greedy", "clash", "broken", "stray"):
            with self.subTest(plugin=bad):
                self.assertIn(bad, text)
        self.assertIn("запрещённые", text)

    def test_effective_permissions_are_intersection(self) -> None:
        raw = {"name": "x", "commands": ["/x"], "permissions": ["reply", "recall"]}
        *_, perms, _aud = validate(raw, granted=["reply"])
        self.assertEqual(perms, frozenset({"reply"}))
        with self.assertRaises(PluginError):
            validate(raw, granted=None)
        with self.assertRaises(PluginError):
            validate({**raw, "permissions": ["telepathy"]}, granted=["reply"])
        with self.assertRaises(PluginError):
            validate({**raw, "commands": ["bad cmd"]}, granted=["reply"])

    async def test_dice_reply_and_storage(self) -> None:
        out = await self.host.handle(self.dm(B, "/dice 20"))
        self.assertEqual(len(out), 1)
        self.assertIn("выпало", out[0])
        await self.host.handle(self.dm(C, "/dice"))
        self.assertEqual(await self.host.handle(self.dm(B, "/dice_stats")), ["бросков: 2"])
        data = json.loads((Path(self.tmp.name) / "pdata" / "dice.json").read_text())
        self.assertEqual(data, {"rolls": 2})

    async def test_denied_permission_is_audited(self) -> None:
        out = await self.host.handle(self.dm(B, "/spy"))
        self.assertEqual(out, ["что-то не получилось"])
        self.assertEqual(count(self.db.conn, "SELECT COUNT(*) FROM audit_log WHERE action = "
                                             "'plugin_denied' AND target = 'spy'"), 1)
        row = self.db.conn.execute("SELECT details FROM audit_log WHERE action = 'plugin_run' "
                                   "AND target = 'spy'").fetchone()
        self.assertEqual(json.loads(row[0])["status"], "denied")

    async def test_recall_sees_only_caller(self) -> None:
        out = await self.host.handle(self.dm(B, "/peek секрет"))
        self.assertIn("пауков", out[0])
        self.assertNotIn("дождь", out[0])
        out = await self.host.handle(self.dm(C, "/peek секрет"))
        self.assertNotIn("пауков", out[0])

    async def test_timeout_error_audience_and_groups(self) -> None:
        self.assertEqual(await self.host.handle(self.dm(OWNER, "/slow")),
                         ["что-то не получилось"])
        self.assertFalse(self.host.accepts(self.dm(B, "/slow")))  # audience=owner
        self.assertEqual(await self.host.handle(self.dm(B, "/boom")),
                         ["что-то не получилось"])
        self.assertFalse(self.host.accepts(msg(GROUP, B, "/dice", at=self.clock())))
        self.assertFalse(self.host.accepts(self.dm(B, "/unknown")))
        self.assertIn("dice 1.0", self.host.describe())

    def test_config_rejects_unknown_grants(self) -> None:
        PluginsSection(grants={"dice": ["reply"]})
        with self.assertRaises(ValueError):
            PluginsSection(grants={"dice": ["account"]})
