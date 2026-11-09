import base64
import itertools
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from alya.media.clients import ImageClient, VisionClient, WebSearchClient
from alya.media.limits import UsageLimiter
from alya.media.models import MediaRef, ReplyActions
from alya.media.processor import MediaProcessor, ProcessorLimits
from alya.media.stickers import StickerStore
from alya.telegram.addressee import AddresseeDetector
from alya.telegram.commands import HELP, OwnerCommands
from alya.telegram.media_flow import MediaKit, outgoing, sent_text
from alya.telegram.outbox import OutboxStore
from alya.telegram.pipeline import Pipeline, PipelineConfig
from alya.telegram.sender import Sender
from tests.telegram_helpers import DbTestCase, FakeTransport, Sleeps, msg

ME, OWNER, G = 555, 101, -1001
PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16


class FakeRaw:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.reply = reply
        self.calls: list[tuple[Any, dict[str, Any]]] = []

    @property
    def model_id(self) -> str:
        return "fake"

    async def raw(self, messages, **kw):  # type: ignore[no-untyped-def]
        self.calls.append((messages, kw))
        return self.reply


class FakeVision:
    def __init__(self, text: str = "котик") -> None:
        self.text = text
        self.calls = 0

    async def describe(self, data: bytes, *, caption: str = "") -> str:
        self.calls += 1
        return self.text


class FakeTTS:
    async def synthesize(self, text: str) -> bytes:
        return b"mp3"


class FakeImages:
    async def generate(self, prompt: str) -> bytes:
        return PNG


class MediaTransport(FakeTransport):
    def __init__(self) -> None:
        super().__init__()
        self.media: list[tuple[str, tuple[Any, ...]]] = []
        self.bio: str | None = None
        self._mids = itertools.count(1)

    async def _rec(self, name: str, *args: Any) -> int:
        self.media.append((name, args))
        return (20_000 + next(self._mids)) << 20

    async def send_voice(self, chat_id, path, duration_s, *, reply_to=None):  # type: ignore[no-untyped-def]
        return await self._rec("voice", chat_id, duration_s)

    async def send_photo(self, chat_id, path, *, caption="", reply_to=None):  # type: ignore[no-untyped-def]
        return await self._rec("photo", chat_id)

    async def send_sticker(self, chat_id, remote_id, *, reply_to=None):  # type: ignore[no-untyped-def]
        return await self._rec("sticker", chat_id, remote_id)

    async def react(self, chat_id, message_id, emoji):  # type: ignore[no-untyped-def]
        self.media.append(("react", (chat_id, message_id, emoji)))

    async def download(self, file_id: int, max_bytes: int) -> bytes:
        return PNG

    async def join_by_link(self, link: str) -> int:
        return G

    async def set_bio(self, text: str) -> None:
        self.bio = text

    async def set_avatar(self, path: str) -> None:
        pass

    async def sticker_set(self, name: str) -> list[tuple[str, str]]:
        return [("r1", "😊")]


def limiter(db, clock, **per_hour):  # type: ignore[no-untyped-def]
    return UsageLimiter(db, per_hour, clock=clock)


class LimiterTests(DbTestCase):
    async def test_hourly_and_disabled(self) -> None:
        lim = limiter(self.db, self.clock, tts=2)
        self.assertEqual([await lim.take("tts", 1) for _ in range(3)], [True, True, False])
        self.assertFalse(await lim.take("image", 1))
        self.assertFalse(lim.enabled("image"))
        self.clock.advance(timedelta(minutes=61))
        self.assertTrue(await lim.take("tts", 1))
        self.assertEqual(await lim.used("tts"), 3)

    async def test_daily_limit(self) -> None:
        lim = UsageLimiter(self.db, {"image": 10}, per_day={"image": 2}, clock=self.clock)
        self.assertTrue(await lim.take("image"))
        self.clock.advance(timedelta(hours=2))
        self.assertTrue(await lim.take("image"))
        self.clock.advance(timedelta(hours=2))
        self.assertFalse(await lim.take("image"))

    async def test_unknown_kind_rejected(self) -> None:
        with self.assertRaises(ValueError):
            UsageLimiter(self.db, {"teleport": 1})


class ClientTests(DbTestCase):
    async def test_vision_sends_data_url(self) -> None:
        chat = FakeRaw({"content": "  девочка\nс котом "})
        self.assertEqual(await VisionClient(chat).describe(PNG, caption="смотри"),
                         "девочка с котом")
        parts = chat.calls[0][0][0]["content"]
        self.assertTrue(parts[1]["image_url"]["url"].startswith("data:image/png;base64,"))
        with self.assertRaises(Exception):
            await VisionClient(chat).describe(b"not an image")

    async def test_image_client_decodes(self) -> None:
        url = "data:image/png;base64," + base64.b64encode(PNG).decode()
        chat = FakeRaw({"content": "", "images": [{"image_url": {"url": url}}]})
        self.assertEqual(await ImageClient(chat).generate("селфи"), PNG)
        self.assertEqual(chat.calls[0][1]["extra"], {"modalities": ["image", "text"]})
        with self.assertRaises(Exception):
            await ImageClient(FakeRaw({"content": "нет"})).generate("x")

    async def test_search_sources(self) -> None:
        chat = FakeRaw({"content": "факт", "annotations": [
            {"url_citation": {"url": "https://a.example", "title": "A"}},
            {"url_citation": {"url": "https://a.example", "title": "A2"}},
            {"url_citation": {"url": "javascript:alert(1)"}}]})
        found = await WebSearchClient(chat, max_results=2).search("новости")
        self.assertEqual((found.summary, found.sources), ("факт", [("A", "https://a.example")]))


class ProcessorTests(DbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.vision = FakeVision()
        self.lim = limiter(self.db, self.clock, vision=5, stt=5)
        self.p = MediaProcessor(self.lim, vision=self.vision, stt=None,
                                limits=ProcessorLimits(max_image_bytes=1000),
                                has_ffmpeg=lambda: False)

    async def download(self, file_id: int, max_bytes: int) -> bytes:
        return PNG

    async def test_photo_described(self) -> None:
        out = await self.p.describe(MediaRef("photo", file_id=1, size=10), "подпись",
                                    self.download, chat_id=1)
        self.assertEqual(out, "[фото: котик] подпись")

    async def test_too_big_and_limits(self) -> None:
        out = await self.p.describe(MediaRef("photo", file_id=1, size=5000), "", self.download)
        self.assertIn("слишком большое", out)
        for _ in range(5):
            await self.lim.take("vision")
        out = await self.p.describe(MediaRef("photo", file_id=1, size=10), "", self.download)
        self.assertEqual(out, "[фото]")
        self.assertEqual(self.vision.calls, 0)

    async def test_sticker_and_voice_without_stt(self) -> None:
        self.assertEqual(await self.p.describe(MediaRef("sticker", emoji="😊"), "", self.download),
                         "[стикер 😊]")
        self.assertEqual(await self.p.describe(MediaRef("voice", file_id=2, duration_s=3), "",
                                               self.download), "[голосовое]")


class StickerTests(DbTestCase):
    async def test_add_pick_remove(self) -> None:
        store = StickerStore(self.db, clock=self.clock)
        self.assertEqual(await store.add_set("cute_set", [("r1", "😊"), ("r2", "😭")]), 2)
        self.assertEqual(await store.pick("😊"), "r1")
        self.assertIsNone(await store.pick(""))
        self.assertEqual(await store.sets(), [("cute_set", 2)])
        self.assertEqual(await store.remove_set("cute_set"), 2)
        self.assertIsNone(await store.pick("😊"))
        with self.assertRaises(ValueError):
            await store.add_set("bad name!", [])


class MediaFlowTests(DbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.stickers = StickerStore(self.db, clock=self.clock)
        await self.stickers.add_set("cute_set", [("r1", "😊")])

        async def encode(data: bytes) -> tuple[bytes, int]:
            return b"ogg", 3

        self.kit = MediaKit(limiter(self.db, self.clock, tts=5, reaction=5, sticker=5, image=5),
                            stickers=self.stickers, tts=FakeTTS(), images=FakeImages(),
                            selfie_prompt=lambda scene: "фото: " + scene,
                            media_dir=Path(self.tmp.name) / "media", encode_voice=encode,
                            has_ffmpeg=lambda: True)

    async def run_flow(self, acts, parts, kind="private"):  # type: ignore[no-untyped-def]
        last = msg(OWNER if kind == "private" else G, OWNER, "привет", at=self.clock(),
                   kind=kind)
        return await outgoing(self.kit, chat_id=last.chat_id, sender_id=OWNER, last=last,
                              actions=acts, parts=parts, quote=False, typing=lambda p: 0.5)

    async def test_full_set(self) -> None:
        items = await self.run_flow(ReplyActions(reaction="❤", sticker="😊", voice=True,
                                                 selfie="в худи"), ["привет", "как ты"])
        kinds = [p.get("kind", "text") for _, p in items]
        self.assertEqual(kinds, ["reaction", "voice", "sticker", "photo"])
        voice = items[1][1]
        self.assertEqual((voice["text"], voice["duration_s"]), ("привет как ты", 3))
        self.assertTrue(Path(voice["path"]).is_file())
        self.assertTrue(items[3][1]["path"].endswith(".png"))
        self.assertEqual(items[2][1]["remote_id"], "r1")

    async def test_fallbacks(self) -> None:
        self.kit.has_ffmpeg = lambda: False
        items = await self.run_flow(ReplyActions(voice=True, selfie="селфи"), ["а", "б"],
                                    kind="supergroup")
        self.assertEqual([p["text"] for _, p in items], ["а", "б"])
        plain = await self.run_flow(None, ["x"])
        self.assertEqual([p["text"] for _, p in plain], ["x"])
        self.kit.has_ffmpeg = lambda: True
        long_text = ["а" * 500]
        items = await self.run_flow(ReplyActions(voice=True), long_text)
        self.assertEqual([p.get("kind", "text") for _, p in items], ["text"])

    async def test_no_kit_is_plain_text(self) -> None:
        last = msg(OWNER, OWNER, "хай", at=self.clock(), kind="private")
        items = await outgoing(None, chat_id=OWNER, sender_id=OWNER, last=last,
                               actions=ReplyActions(reaction="❤"), parts=["а"], quote=True,
                               typing=lambda p: 1.234)
        self.assertEqual(items, [(f"{OWNER}:{last.message_id}:0", {
            "text": "а", "reply_to": last.message_id, "typing_s": 1.23, "addressees": [OWNER]})])

    def test_sent_text(self) -> None:
        self.assertIsNone(sent_text({"kind": "reaction"}))
        self.assertIsNone(sent_text({"text": "ok", "system": True}))
        self.assertEqual(sent_text({"kind": "voice", "text": "привет"}),
                         ("[голосовое] привет", "voice"))
        self.assertEqual(sent_text({"kind": "sticker", "emoji": ""}), ("[стикер]", "sticker"))
        self.assertEqual(sent_text({"kind": "photo", "scene": "дома"}),
                         ("[фото себя: дома]", "photo"))
        self.assertEqual(sent_text({"text": "хай"}), ("хай", None))


class ActionEngine:
    def __init__(self, actions: ReplyActions | None = None) -> None:
        self.actions = actions
        self.turns: list[Any] = []

    async def respond(self, turn: Any) -> SimpleNamespace:
        self.turns.append(turn)
        return SimpleNamespace(messages=["ок"], actions=self.actions)


class PipelineMediaTests(DbTestCase):
    async def build(self, actions=None) -> None:  # type: ignore[no-untyped-def]
        self.engine = ActionEngine(actions)
        self.tr = MediaTransport()
        lim = limiter(self.db, self.clock, vision=5, reaction=5)
        self.vision = FakeVision()
        kit = MediaKit(lim, processor=MediaProcessor(lim, vision=self.vision,
                                                     has_ffmpeg=lambda: False),
                       media_dir=Path(self.tmp.name) / "media")
        kit.commands = OwnerCommands(self.tr, owner_id=OWNER, limiter=lim,
                                     media_dir=kit.media_dir)
        det = AddresseeDetector(ME, "AlysiaNya", ["аля"], clock=self.clock)
        outbox = OutboxStore(self.db, clock=self.clock)
        self.p = Pipeline(self.db, self.engine, det, outbox, transport=self.tr,
                          clock=self.clock, media=kit,
                          config=PipelineConfig(me_id=ME, owner_id=OWNER, debounce_s=0.01,
                                                debounce_max_wait_s=0.05))
        self.sender = Sender(outbox, self.tr, on_sent=self.p.on_sent, clock=self.clock,
                             chat_interval_s=0, global_interval_s=0, sleep=Sleeps())

    async def feed(self, *msgs) -> None:  # type: ignore[no-untyped-def]
        for m in msgs:
            await self.p.on_message(m)
        await self.p.drain()
        while await self.sender.run_once():
            pass

    async def texts(self) -> list[str]:
        rows = await self.db.fetchall("SELECT text FROM messages WHERE chat_id = ? ORDER BY id",
                                      (OWNER,))
        return [r[0] for r in rows]

    async def test_owner_command_bypasses_character(self) -> None:
        await self.build()
        await self.feed(msg(OWNER, OWNER, "/help", at=self.clock(), kind="private"))
        await self.feed(msg(OWNER, OWNER, "/bio люблю аниме", at=self.clock(), kind="private"))
        self.assertEqual(self.engine.turns, [])
        self.assertEqual(self.tr.sent[0][1], HELP)
        self.assertEqual(self.tr.bio, "люблю аниме")
        self.assertEqual(await self.texts(), [])

    async def test_stranger_command_is_ordinary_text(self) -> None:
        await self.build()
        await self.feed(msg(G, 202, "/bio взлом", at=self.clock()))
        self.assertIsNone(self.tr.bio)

    async def test_photo_described_before_reply(self) -> None:
        await self.build(ReplyActions(reaction="❤"))
        await self.feed(msg(OWNER, OWNER, "[фото]", at=self.clock(), kind="private",
                            media=MediaRef("photo", file_id=7, size=10), media_kind="photo"))
        self.assertEqual(self.engine.turns[0].text, "[фото: котик]")
        self.assertEqual([m[0] for m in self.tr.media], ["react"])
        self.assertEqual([t for _, t, _ in self.tr.sent], ["ок"])
        self.assertEqual(await self.texts(), ["[фото: котик]", "ок"])


class SenderMediaTests(DbTestCase):
    async def asyncSetUp(self) -> None:
        await super().asyncSetUp()
        self.store = OutboxStore(self.db, clock=self.clock)
        self.tr = MediaTransport()
        self.sender = Sender(self.store, self.tr, clock=self.clock, chat_interval_s=0,
                             global_interval_s=0, sleep=Sleeps())

    async def test_voice_sent_and_file_removed(self) -> None:
        path = Path(self.tmp.name) / "v.ogg"
        path.write_bytes(b"ogg")
        await self.store.enqueue(1, "v", {"kind": "voice", "path": str(path), "duration_s": 4})
        self.assertEqual(await self.sender.run_once(), 1)
        self.assertEqual(self.tr.media, [("voice", (1, 4))])
        self.assertFalse(path.exists())

    async def test_missing_file_fails_permanently(self) -> None:
        await self.store.enqueue(1, "p", {"kind": "photo", "path": "/nonexistent/x.png"})
        self.assertEqual(await self.sender.run_once(), 0)
        self.assertEqual(await self.store.counts(), {"failed": 1})

    async def test_unsupported_transport(self) -> None:
        sender = Sender(self.store, FakeTransport(), clock=self.clock, chat_interval_s=0,
                        global_interval_s=0, sleep=Sleeps())
        await self.store.enqueue(1, "s", {"kind": "sticker", "remote_id": "r1"})
        self.assertEqual(await sender.run_once(), 0)
        self.assertEqual(await self.store.counts(), {"failed": 1})


import io  # noqa: E402
import unittest  # noqa: E402
import wave  # noqa: E402

from alya.media import audio  # noqa: E402


def _wav(seconds: float = 1.5) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\x00\x01" * int(16000 * seconds))
    return buf.getvalue()


@unittest.skipUnless(audio.ffmpeg_path(), "ffmpeg не установлен")
class AudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_voice_note(self) -> None:
        ogg, seconds = await audio.to_voice_note(_wav())
        self.assertTrue(ogg.startswith(b"OggS"))
        self.assertIn(seconds, (1, 2))

    async def test_speech_to_mp3(self) -> None:
        mp3 = await audio.speech_to_mp3(_wav(), max_seconds=10)
        self.assertTrue(mp3[:3] == b"ID3" or mp3[0] == 0xFF)

    async def test_garbage_raises(self) -> None:
        with self.assertRaises(audio.AudioError):
            await audio.to_voice_note(b"definitely not audio")
