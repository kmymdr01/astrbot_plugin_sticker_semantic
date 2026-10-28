# -*- coding: utf-8 -*-

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.light_reply import (  # noqa: E402
    is_light_reply,
    reply_allows_sticker_only,
    strip_sticker_markers,
)
from core.selection import (  # noqa: E402
    MODE_APPEND,
    MODE_LIGHT,
    choose_sticker,
    rank_stickers,
)
from core.store import Sticker, StickerStore  # noqa: E402
from core.taxonomy import (  # noqa: E402
    extract_signals,
    looks_factual,
    normalize_tags,
    tag_polarity,
    text_similarity,
)


class TaxonomyTests(unittest.TestCase):
    def test_laugh_is_positive(self):
        signals = extract_signals("哈哈哈哈笑死我了")
        self.assertIn("laugh", signals.tags)
        self.assertGreater(signals.polarity, 0.3)

    def test_angry_is_negative(self):
        signals = extract_signals("气死我了，真烦")
        self.assertIn("angry", signals.tags)
        self.assertLess(signals.polarity, -0.3)

    def test_question(self):
        signals = extract_signals("为什么会这样？")
        self.assertTrue(signals.is_question)
        self.assertIn("confused", signals.tags)

    def test_normalize_tags(self):
        self.assertEqual(normalize_tags(["开心", "laugh", "unknown"]), ["happy", "laugh"])

    def test_polarity(self):
        self.assertGreater(tag_polarity(["happy", "laugh"]), 0.5)
        self.assertLess(tag_polarity(["sad", "cry"]), -0.5)

    def test_similarity(self):
        self.assertGreater(text_similarity("猫咪开心大笑", "猫咪开心大笑"), 0.9)
        self.assertGreater(
            text_similarity("哈哈哈笑死", "笑死 哈哈哈 大笑"), 0.1
        )

    def test_factual_guard(self):
        self.assertTrue(looks_factual("明天 10 点开会，地址在 3 楼"))
        self.assertTrue(looks_factual("https://example.com"))
        self.assertFalse(looks_factual("哈哈"))


class LightReplyTests(unittest.TestCase):
    def test_light(self):
        for text in ("哈哈", "嗯嗯", "？", "好耶", "谢谢", "[STICKER]"):
            self.assertTrue(is_light_reply(text), text)

    def test_not_light(self):
        for text in (
            "吃了吗？",
            "这个问题我需要解释一下，原因有三点",
            "明天10点开会",
            "https://example.com",
        ):
            self.assertFalse(is_light_reply(text), text)

    def test_marker_strip(self):
        body, found = strip_sticker_markers("哈哈[STICKER]")
        self.assertTrue(found)
        self.assertEqual(body, "哈哈")

    def test_reply_allows(self):
        self.assertTrue(reply_allows_sticker_only("哈哈哈"))
        self.assertFalse(reply_allows_sticker_only("明天10点开会"))


def _sticker(
    sid: str,
    tags: list[str],
    *,
    usage: str = "both",
    intensity: int = 3,
    description: str = "",
    keywords: list[str] | None = None,
) -> Sticker:
    return Sticker(
        id=sid,
        filename=f"{sid}.png",
        sha256=sid,
        description=description or " ".join(tags),
        tags=tags,
        keywords=keywords or [],
        intensity=intensity,
        usage=usage,
        safe=True,
        status="ready",
        path=f"/tmp/{sid}.png",
    )


class SelectionTests(unittest.TestCase):
    def test_positive_prefers_happy(self):
        stickers = [
            _sticker("happy1", ["happy", "laugh"], description="猫咪大笑"),
            _sticker("sad1", ["sad", "cry"], description="猫咪流泪"),
        ]
        ctx = extract_signals("今天太开心了，哈哈哈")
        reply = extract_signals("哈哈哈笑死")
        ranked = rank_stickers(stickers, ctx, reply, mode=MODE_APPEND)
        self.assertEqual(ranked[0].sticker.id, "happy1")
        self.assertGreater(ranked[0].score, ranked[1].score)

    def test_negative_prefers_sad(self):
        stickers = [
            _sticker("happy1", ["happy", "laugh"], description="猫咪大笑"),
            _sticker("sad1", ["sad", "cry"], description="猫咪流泪"),
        ]
        ctx = extract_signals("好难过，想哭")
        reply = extract_signals("呜呜别哭了")
        ranked = rank_stickers(stickers, ctx, reply, mode=MODE_APPEND)
        self.assertEqual(ranked[0].sticker.id, "sad1")

    def test_usage_filters(self):
        stickers = [
            _sticker("only_react", ["happy"], usage="reaction"),
            _sticker("only_aug", ["happy"], usage="augment"),
        ]
        ctx = extract_signals("哈哈哈")
        self.assertEqual(
            [x.sticker.id for x in rank_stickers(stickers, ctx, ctx, mode=MODE_LIGHT)],
            ["only_react"],
        )
        self.assertEqual(
            [x.sticker.id for x in rank_stickers(stickers, ctx, ctx, mode=MODE_APPEND)],
            ["only_aug"],
        )

    def test_allow_usage_mismatch(self):
        stickers = [_sticker("only_react", ["happy"], usage="reaction")]
        ctx = extract_signals("哈哈哈")
        ranked = rank_stickers(
            stickers, ctx, ctx, mode=MODE_APPEND, allow_usage_mismatch=True
        )
        self.assertEqual(len(ranked), 1)

    def test_recent_penalty(self):
        stickers = [
            _sticker("a", ["happy", "laugh"], description="大笑"),
            _sticker("b", ["happy", "laugh"], description="大笑"),
        ]
        ctx = extract_signals("哈哈哈")
        ranked = rank_stickers(
            stickers, ctx, ctx, mode=MODE_APPEND, recent_ids={"a"}
        )
        self.assertEqual(ranked[0].sticker.id, "b")

    def test_choose_threshold(self):
        stickers = [_sticker("a", ["sad"], description="流泪")]
        ctx = extract_signals("哈哈哈")
        self.assertIsNone(
            choose_sticker(stickers, ctx, ctx, mode=MODE_LIGHT, threshold=0.9)
        )


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.store = StickerStore(self.root)
        await self.store.init()

    async def asyncTearDown(self):
        await self.store.close()
        self.tmp.cleanup()

    async def test_add_dedupe_tag_delete(self):
        src = self.root / "cat.png"
        src.write_bytes(b"fake-png-data")

        sticker, created = await self.store.add_file(src, source="test")
        self.assertTrue(created)
        self.assertIsNotNone(sticker)
        assert sticker is not None
        self.assertEqual(await self.store.count(), 1)
        self.assertTrue(Path(sticker.path).is_file())

        duplicate, created2 = await self.store.add_file(src, source="test")
        self.assertFalse(created2)
        self.assertEqual(duplicate.id, sticker.id)
        self.assertEqual(await self.store.count(), 1)

        await self.store.set_tags(sticker.id, ["happy", "laugh"], description="猫")
        updated = await self.store.get(sticker.id)
        assert updated is not None
        self.assertEqual(updated.tags, ["happy", "laugh"])
        self.assertEqual(updated.description, "猫")
        self.assertEqual(updated.status, "ready")

        self.assertTrue(await self.store.set_description(sticker.id, "猫笑到打滚"))
        self.assertTrue(await self.store.set_keywords(sticker.id, ["哈哈哈", "笑死"]))
        edited = await self.store.get(sticker.id)
        assert edited is not None
        self.assertEqual(edited.description, "猫笑到打滚")
        self.assertEqual(edited.keywords, ["哈哈哈", "笑死"])
        self.assertEqual(edited.status, "ready")

        await self.store.set_status(sticker.id, "disabled")
        await self.store.set_description(sticker.id, "已停用的描述")
        disabled = await self.store.get(sticker.id)
        assert disabled is not None
        self.assertEqual(disabled.status, "disabled")
        self.assertEqual(disabled.description, "已停用的描述")

        await self.store.mark_used([sticker.id])
        used = await self.store.get(sticker.id)
        assert used is not None
        self.assertEqual(used.use_count, 1)
        self.assertGreater(used.last_used_at, 0)

        stats = await self.store.stats()
        self.assertEqual(stats["total"], 1)
        self.assertEqual(stats["tags"]["happy"], 1)

        self.assertTrue(await self.store.delete(sticker.id))
        self.assertEqual(await self.store.count(), 0)
        self.assertFalse(Path(sticker.path).exists())

    async def test_confirmed_flow(self):
        src = self.root / "new.png"
        src.write_bytes(b"new-image")
        sticker, created = await self.store.add_file(src, source="test")
        self.assertTrue(created)
        assert sticker is not None
        self.assertFalse(sticker.confirmed)
        await self.store.set_tags(sticker.id, ["happy"])

        ready = await self.store.list_ready(confirmed_only=True)
        self.assertEqual(ready, [])

        self.assertTrue(await self.store.set_confirmed(sticker.id, True))
        ready = await self.store.list_ready(confirmed_only=True)
        self.assertEqual([s.id for s in ready], [sticker.id])
        stats = await self.store.stats()
        self.assertEqual(stats["new"], 0)
        self.assertEqual(stats["confirmed"], 1)

    async def test_bad_extension(self):
        src = self.root / "note.txt"
        src.write_text("no")
        _, created = await self.store.add_file(src)
        self.assertFalse(created)


if __name__ == "__main__":
    unittest.main()
