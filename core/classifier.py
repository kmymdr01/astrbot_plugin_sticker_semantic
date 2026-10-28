"""用视觉模型给表情包打标签。

分类只在导入/学习/手动重分类时发生，发送时不再调用 LLM。
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Awaitable, Callable

from astrbot.api import logger

from .store import STATUS_DISABLED, STATUS_PENDING, STATUS_READY, Sticker, StickerStore
from .taxonomy import ALL_TAGS, TAG_LABELS, normalize_tags

_TAG_LIST = "、".join(f"{key}({label})" for key, label in TAG_LABELS.items())

CLASSIFY_SYSTEM = (
    "你是聊天表情包分类器。你只输出一个 JSON 对象，不输出 Markdown、解释或多余文字。"
)

CLASSIFY_PROMPT = f"""看这张聊天表情包，判断它在聊天中表达什么。

要求：
1. description：画面内容 + 情绪，30 字以内；图上有字就转写关键文字。
2. tags：1~4 个标签，只能从下面列表里选：{_TAG_LIST}。
3. keywords：2~6 个适合这张图的聊天短语或口头禅，比如“哈哈哈”“笑死”“贴贴”。
4. intensity：1~5，1 很平淡，5 非常强烈。
5. usage：reaction（只适合单独发）、augment（适合跟在文字后）、both。
6. safe：false 表示色情、血腥、违法违规或明显不适合群聊。

只输出：
{{
  "description": "...",
  "tags": ["happy", "laugh"],
  "keywords": ["哈哈哈", "笑死"],
  "intensity": 3,
  "usage": "both",
  "safe": true
}}"""


def _extract_json(text: str) -> dict | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    start = raw.find("{")
    end = raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def _as_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "是", "安全", "safe")
    return default


def _normalize_usage(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("reaction", "react", "only", "单独", "只发", "表情"):
        return "reaction"
    if raw in ("augment", "append", "with_text", "附加", "跟文字"):
        return "augment"
    return "both"


def _clean_description(value: object, fallback: str) -> str:
    text = str(value or "").strip().replace("\n", " ")
    text = re.sub(r"\s+", " ", text)
    return text[:300] or fallback


def _clean_keywords(value: object) -> list[str]:
    if isinstance(value, str):
        values = re.split(r"[,，、|/;；\s]+", value)
    elif isinstance(value, list):
        values = [str(v) for v in value]
    else:
        values = []
    result: list[str] = []
    for item in values:
        text = str(item).strip()[:24]
        if text and text not in result:
            result.append(text)
    return result[:8]


class StickerClassifier:
    def __init__(self, plugin, store: StickerStore):
        self.plugin = plugin
        self.store = store
        self._sem = asyncio.Semaphore(max(1, plugin.cfg_int("learning", "classify_concurrency", 2)))
        self._tasks: set[asyncio.Task] = set()
        self._inflight: set[str] = set()

    # ------------------------------------------------------------------
    # provider
    # ------------------------------------------------------------------

    async def _get_provider(self, umo: str | None):
        cfg_id = str(self.plugin.cfg_str("learning", "classify_provider_id", "") or "").strip()
        if cfg_id:
            provider = self.plugin.context.get_provider_by_id(cfg_id)
            if provider:
                return provider
            logger.warning(f"[StickerSemantic] classify_provider_id={cfg_id} 不存在，回退")

        caption_id = ""
        try:
            settings = self.plugin.context.get_config(umo=umo).get("provider_settings", {})
            caption_id = str(settings.get("default_image_caption_provider_id", "") or "")
        except Exception:
            pass
        if caption_id:
            provider = self.plugin.context.get_provider_by_id(caption_id)
            if provider:
                return provider

        try:
            return await self.plugin.context.get_using_provider_async(umo=umo)
        except Exception:
            return None

    async def _get_retag_provider(self, umo: str | None):
        cfg_id = str(
            self.plugin.cfg_str("learning", "retag_provider_id", "") or ""
        ).strip()
        if cfg_id:
            provider = self.plugin.context.get_provider_by_id(cfg_id)
            if provider:
                return provider
            logger.warning(
                f"[StickerSemantic] retag_provider_id={cfg_id} 不存在，回退当前模型"
            )
        try:
            return await self.plugin.context.get_using_provider_async(umo=umo)
        except Exception:
            return None

    async def retag_from_text(
        self,
        sticker: Sticker,
        umo: str | None = None,
        *,
        description: str | None = None,
        keywords: list[str] | None = None,
    ) -> dict | None:
        """只根据描述/关键词重算标签，不覆盖描述，不调用视觉模型。"""
        description = (
            str(description).strip()
            if description is not None
            else str(sticker.description or "").strip()
        )
        if keywords is None:
            keywords = [str(k).strip() for k in sticker.keywords if str(k).strip()]
        else:
            keywords = [str(k).strip() for k in keywords if str(k).strip()]
        if not description and not keywords:
            return None

        provider = await self._get_retag_provider(umo)
        if not provider:
            logger.warning("[StickerSemantic] 没有可用于按描述重算标签的文字 Provider")
            return None
        try:
            meta = provider.meta()
            logger.info(
                f"[StickerSemantic] retag {sticker.id} provider={meta.id}/{meta.model}"
            )
        except Exception:
            pass

        prompt = f"""请根据下面的表情包描述，重新选择它的聊天标签和匹配关键词。

描述：{description or "（无）"}
旧关键词：{"、".join(keywords) or "（无）"}

只输出一个 JSON 对象，不要输出 Markdown 或解释：
{{
  "tags": ["happy", "laugh"],
  "keywords": ["哈哈哈", "笑死"],
  "intensity": 3,
  "usage": "both"
}}

要求：
1. tags 1~4 个，只能从以下列表选：{_TAG_LIST}。
2. keywords 2~6 个，必须和新描述一致，能在聊天里自然匹配这张图。
3. intensity 1~5。
4. usage：reaction / augment / both。
5. 不要改写描述。"""

        timeout = max(10, self.plugin.cfg_int("learning", "classify_timeout", 60))
        try:
            response = await asyncio.wait_for(
                provider.text_chat(
                    prompt=prompt,
                    system_prompt="你是聊天表情包标签整理器。只输出 JSON。",
                    contexts=[],
                    persist=False,
                ),
                timeout=timeout,
            )
        except Exception as e:
            logger.warning(f"[StickerSemantic] retag 请求失败: {e}")
            return None

        data = _extract_json(getattr(response, "completion_text", "") or "")
        if not data:
            return None
        tags = normalize_tags(data.get("tags", []))
        if not tags:
            return None
        try:
            intensity = max(1, min(5, int(data.get("intensity", 3))))
        except (TypeError, ValueError):
            intensity = sticker.intensity
        return {
            "tags": tags,
            "keywords": _clean_keywords(data.get("keywords")),
            "intensity": intensity,
            "usage": _normalize_usage(data.get("usage")),
        }

    async def _call_model(
        self,
        sticker: Sticker,
        umo: str | None,
        *,
        prompt: str = CLASSIFY_PROMPT,
        system_prompt: str = CLASSIFY_SYSTEM,
    ) -> str:
        provider = await self._get_provider(umo)
        if not provider:
            return ""
        try:
            meta = provider.meta()
            logger.debug(
                f"[StickerSemantic] classify {sticker.id} provider={meta.id}/{meta.model}"
            )
        except Exception:
            pass
        timeout = max(10, self.plugin.cfg_int("learning", "classify_timeout", 60))
        response = await asyncio.wait_for(
            provider.text_chat(
                prompt=prompt,
                system_prompt=system_prompt,
                image_urls=[sticker.path],
                contexts=[],
                persist=False,
            ),
            timeout=timeout,
        )
        return getattr(response, "completion_text", "") or ""

    # ------------------------------------------------------------------
    # classify
    # ------------------------------------------------------------------

    async def classify(self, sticker_id: str, umo: str | None = None, *, force: bool = False) -> bool:
        sticker = await self.store.get(sticker_id)
        if not sticker:
            return False
        if not force and sticker.status != STATUS_PENDING:
            return True

        async with self._sem:
            text = ""
            try:
                text = await self._call_model(sticker, umo)
            except asyncio.TimeoutError:
                logger.warning(f"[StickerSemantic] 分类超时: {sticker.id}")
            except Exception as e:
                logger.warning(f"[StickerSemantic] 分类失败: {sticker.id}: {e}")

            data = _extract_json(text)
            if not data:
                # 一次严格重试：部分模型会先输出解释再输出 JSON
                if text:
                    try:
                        strict_prompt = (
                            CLASSIFY_PROMPT
                            + "\n\n上次输出无法解析。只输出 JSON 对象，不要任何其他字符。"
                        )
                        strict = await self._call_model(
                            sticker, umo, prompt=strict_prompt
                        )
                        data = _extract_json(strict)
                    except Exception:
                        data = None
            if not data:
                logger.info(
                    f"[StickerSemantic] 保留 pending（无可用分类）: {sticker.id}"
                )
                return False

        tags = normalize_tags(data.get("tags", []))
        description = _clean_description(
            data.get("description"), sticker.filename
        )
        keywords = _clean_keywords(data.get("keywords"))
        try:
            intensity = int(data.get("intensity", 3))
        except (TypeError, ValueError):
            intensity = 3
        intensity = max(1, min(5, intensity))
        usage = _normalize_usage(data.get("usage"))
        safety_enabled = self.plugin.cfg_bool("learning", "safety_filter_enabled", True)
        safe = _as_bool(data.get("safe"), True) if safety_enabled else True

        if not tags:
            tags = ["react"]

        status = STATUS_READY if safe else STATUS_DISABLED
        await self.store.update_classification(
            sticker.id,
            description=description,
            tags=tags,
            keywords=keywords,
            intensity=intensity,
            usage=usage,
            safe=safe,
            status=status,
        )
        logger.info(
            f"[StickerSemantic] 分类完成 {sticker.id}: {description[:36]} "
            f"tags={tags} usage={usage} safe={safe} "
            f"safety_filter={'on' if safety_enabled else 'off'}"
        )
        return True

    async def classify_many(
        self,
        sticker_ids: list[str],
        umo: str | None = None,
        *,
        progress: Callable[[int, int], Awaitable[None]] | None = None,
    ) -> dict:
        total = len(sticker_ids)
        ok = 0
        done = 0
        lock = asyncio.Lock()

        async def _one(sid: str) -> None:
            nonlocal ok, done
            result = await self.classify(sid, umo, force=True)
            async with lock:
                done += 1
                if result:
                    ok += 1
                if progress and (done == total or done % 5 == 0):
                    await progress(done, total)

        await asyncio.gather(*(_one(sid) for sid in sticker_ids))
        return {"total": total, "success": ok, "failed": total - ok}

    def enqueue(
        self, sticker_id: str, umo: str | None = None, *, force: bool = False
    ) -> None:
        if not force and not self.plugin.cfg_bool(
            "learning", "auto_classify", True
        ):
            return
        if sticker_id in self._inflight:
            return
        self._inflight.add(sticker_id)

        async def _run() -> None:
            try:
                await self.classify(sticker_id, umo, force=force)
            finally:
                self._inflight.discard(sticker_id)

        task = asyncio.create_task(_run())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def cancel_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
