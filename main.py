# -*- coding: utf-8 -*-
"""语义表情包插件（独立版）。

只做三件事：
1. 把表情包收进自己的库，并用视觉模型离线打标签；
2. 在 LLM 请求前注入当前语境匹配的表情包候选；
3. 在回复发出前，根据“上下文 + 将要发出的回复”选择表情包：
   - 轻回复：整条替换成表情包；
   - 普通回复：按概率在末尾附加一张。

不修改任何其他插件的配置、数据库或文件。
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import random
import re
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from PIL import Image as PILImage

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.api.web import error_response, json_response, request
from astrbot.core.message.components import Image, Plain, Reply
from astrbot.core.message.message_event_result import MessageChain

from .core.classifier import StickerClassifier
from .core.light_reply import (
    is_light_reply,
    reply_allows_sticker_only,
    strip_sticker_markers,
)
from .core.selection import (
    MODE_APPEND,
    MODE_LIGHT,
    ScoredSticker,
    choose_sticker,
    rank_stickers,
)
from .core.store import (
    ALLOWED_EXTS,
    STATUS_DISABLED,
    STATUS_PENDING,
    STATUS_READY,
    Sticker,
    StickerStore,
)
from .core.taxonomy import (
    ALL_TAGS,
    TAG_LABELS,
    describe_tags,
    extract_signals,
    is_question_text,
    looks_factual,
    normalize_tags,
)

PLUGIN_NAME = "astrbot_plugin_sticker_semantic"

_EXTRA_CONTEXT = "_sticker_semantic_context"
_EXTRA_CANDIDATES = "_sticker_semantic_candidates"
_EXTRA_PICK = "_sticker_semantic_pick"
_EXTRA_USED = "_sticker_semantic_used"
_EXTRA_INJECTED = "_sticker_semantic_injected"

_STICKER_MARKERS = (
    "[[STICKER]]",
    "[STICKER]",
    "[[sticker]]",
    "[sticker]",
    "【表情包】",
    "[表情包]",
    "（表情包）",
    "(表情包)",
)


class StickerSemanticPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.config: dict = config or {}
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.store = StickerStore(self.data_dir)
        self.classifier = StickerClassifier(self, self.store)

        self._history: dict[str, deque[tuple[str, str, float]]] = {}
        self._send_times: dict[str, deque[float]] = {}
        self._last_learn: dict[str, float] = {}
        self._cache: list[Sticker] = []
        self._cache_at = 0.0
        self._cache_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._rng = random.Random()

    # ==================================================================
    # 生命周期
    # ==================================================================

    async def initialize(self) -> None:
        await self.store.init()
        (self.data_dir / "imports").mkdir(parents=True, exist_ok=True)
        await self._ready_stickers(force=True)
        self._register_web_apis()
        count = await self.store.count()
        logger.info(
            f"[StickerSemantic] 已加载，表情包 {count} 张，数据目录 {self.data_dir}"
        )

    async def terminate(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        await self.classifier.cancel_all()
        await self.store.close()

    # ==================================================================
    # 配置
    # ==================================================================

    def _section(self, name: str) -> dict:
        value = self.config.get(name) if isinstance(self.config, dict) else None
        return value if isinstance(value, dict) else {}

    def cfg_bool(self, section: str, key: str, default: bool = False) -> bool:
        value = self._section(section).get(key, default)
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return default

    def cfg_int(self, section: str, key: str, default: int) -> int:
        try:
            return int(self._section(section).get(key, default))
        except (TypeError, ValueError):
            return default

    def cfg_float(self, section: str, key: str, default: float) -> float:
        try:
            return float(self._section(section).get(key, default))
        except (TypeError, ValueError):
            return default

    def cfg_str(self, section: str, key: str, default: str = "") -> str:
        value = self._section(section).get(key, default)
        return str(value) if value is not None else default

    def cfg_list(self, section: str, key: str) -> list[str]:
        value = self._section(section).get(key, [])
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            return [
                item.strip()
                for item in re.split(r"[,，、|;；\s]+", value)
                if item.strip()
            ]
        return []

    def _enabled(self) -> bool:
        return self.cfg_bool("general", "enabled", True)

    def _set_safety(self, enabled: bool) -> None:
        learning = dict(self._section("learning"))
        learning["safety_filter_enabled"] = enabled
        save = getattr(self.config, "save_config", None)
        if callable(save):
            save({"learning": learning})
        else:
            self.config.setdefault("learning", {})[
                "safety_filter_enabled"
            ] = enabled

    def _scope(self, event: AstrMessageEvent) -> str:
        group_id = str(event.get_group_id() or "")
        if group_id:
            return group_id
        return f"private_{event.get_sender_id()}"

    def _in_targets(self, event: AstrMessageEvent) -> bool:
        targets = self.cfg_list("general", "target_scopes")
        if not targets:
            return True
        scope = self._scope(event)
        group_id = str(event.get_group_id() or "")
        return scope in targets or (bool(group_id) and group_id in targets)

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        if event.is_admin():
            return True
        admins = set(self.cfg_list("general", "admin_users"))
        return str(event.get_sender_id()) in admins

    # ==================================================================
    # 缓存
    # ==================================================================

    def _invalidate_cache(self) -> None:
        self._cache_at = 0.0

    async def _ready_stickers(self, force: bool = False) -> list[Sticker]:
        now = time.monotonic()
        async with self._cache_lock:
            if not force and self._cache and now - self._cache_at < 30.0:
                return self._cache
            self._cache = await self.store.list_ready(confirmed_only=True)
            self._cache_at = now
            return self._cache

    def _recent_ids(self, stickers: list[Sticker]) -> set[str]:
        cooldown = max(0, self.cfg_int("selection", "sticker_cooldown_seconds", 180))
        if not cooldown:
            return set()
        now = time.time()
        return {
            s.id
            for s in stickers
            if s.last_used_at and now - s.last_used_at < cooldown
        }

    # ==================================================================
    # 上下文
    # ==================================================================

    def _remember(self, scope: str, sender: str, text: str) -> None:
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if not text:
            return
        keep = max(6, self.cfg_int("selection", "recent_history", 6) * 2)
        dq = self._history.get(scope)
        if dq is None:
            dq = deque(maxlen=keep)
            self._history[scope] = dq
        dq.append((str(sender or "?"), text[:240], time.time()))

    @staticmethod
    def _context_message_text(item: Any) -> str:
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            content = item.get("content", "")
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                parts = []
                for part in content:
                    if isinstance(part, dict):
                        text = part.get("text")
                        if isinstance(text, str):
                            parts.append(text)
                    elif isinstance(part, str):
                        parts.append(part)
                return " ".join(parts)
        return ""

    def _build_context(
        self, event: AstrMessageEvent | None, req: ProviderRequest | None = None
    ) -> str:
        if event is None:
            return ""
        scope = self._scope(event)
        limit = max(1, self.cfg_int("selection", "recent_history", 6))
        parts: list[str] = []

        history = list(self._history.get(scope, ()))[-limit:]
        for sender, text, _ts in history:
            parts.append(f"{sender}: {text}")

        current = (event.message_str or event.get_message_outline() or "").strip()
        if current and not current.startswith("/"):
            parts.append(f"当前: {current}")

        if req is not None and req.prompt:
            prompt = str(req.prompt).strip()
            if prompt and prompt not in current:
                parts.append(f"请求: {prompt}")

        if not history and req is not None and req.contexts:
            for item in req.contexts[-limit:]:
                text = self._context_message_text(item).strip()
                if text:
                    parts.append(text)

        return "\n".join(parts)[:1800]

    # ==================================================================
    # Web API（插件页面）
    # ==================================================================

    def _register_web_apis(self) -> None:
        register = getattr(self.context, "register_web_api", None)
        if not callable(register):
            logger.warning("[StickerSemantic] 当前 AstrBot 不支持插件 Web API")
            return
        base = f"/{PLUGIN_NAME}"
        routes = [
            (f"{base}/list", self.api_list, ["GET"], "表情包列表"),
            (f"{base}/preview/<sticker_id>", self.api_preview, ["GET"], "表情包预览"),
            (f"{base}/update", self.api_update, ["POST"], "更新表情包"),
            (f"{base}/retag", self.api_retag, ["POST"], "按描述重算标签"),
            (f"{base}/confirm", self.api_confirm, ["POST"], "确认新加入的表情包"),
            (f"{base}/delete", self.api_delete, ["POST"], "删除表情包"),
            (f"{base}/reclassify", self.api_reclassify, ["POST"], "重新分类"),
            (f"{base}/safety", self.api_safety, ["POST"], "安全过滤开关"),
            (f"{base}/upload", self.api_upload, ["POST"], "上传表情包"),
        ]
        for route, handler, methods, desc in routes:
            try:
                register(route, handler, methods, desc)
            except Exception as e:
                logger.warning(f"[StickerSemantic] 注册 Web API 失败 {route}: {e}")

    @staticmethod
    def _web_denied():
        if not getattr(request, "username", None):
            return error_response("未登录或登录已失效", status_code=401)
        return None

    @staticmethod
    def _as_bool(value: Any, default: bool = True) -> bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "yes", "on", "是")
        return default

    def _sticker_payload(
        self, sticker: Sticker, *, thumb: str = ""
    ) -> dict:
        return {
            "id": sticker.id,
            "filename": sticker.filename,
            "description": sticker.description,
            "tags": sticker.tags,
            "tag_labels": [TAG_LABELS.get(t, t) for t in sticker.tags],
            "keywords": sticker.keywords,
            "usage": sticker.usage,
            "intensity": sticker.intensity,
            "safe": sticker.safe,
            "status": sticker.status,
            "confirmed": sticker.confirmed,
            "use_count": sticker.use_count,
            "last_used_at": sticker.last_used_at,
            "thumb": thumb,
        }

    @staticmethod
    def _make_thumb(src: Path, dest: Path, max_side: int) -> None:
        with PILImage.open(src) as im:
            im.seek(0)
            if im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info
            ):
                background = PILImage.new("RGB", im.size, (255, 255, 255))
                rgba = im.convert("RGBA")
                background.paste(rgba, mask=rgba.split()[-1])
                im = background
            else:
                im = im.convert("RGB")
            im.thumbnail((max_side, max_side))
            dest.parent.mkdir(parents=True, exist_ok=True)
            im.save(dest, format="JPEG", quality=82)

    async def _image_data_url(
        self, sticker: Sticker, max_side: int = 240, suffix: str = "thumb"
    ) -> str:
        path = Path(sticker.path)
        if not path.is_file():
            return ""
        cache = self.data_dir / "thumbs" / f"{sticker.id}_{suffix}.jpg"
        try:
            if not cache.is_file():
                await asyncio.to_thread(self._make_thumb, path, cache, max_side)
            raw = await asyncio.to_thread(cache.read_bytes)
        except Exception as e:
            logger.debug(f"[StickerSemantic] 生成缩略图失败 {sticker.id}: {e}")
            return ""
        return "data:image/jpeg;base64," + base64.b64encode(raw).decode()

    async def api_list(self):
        denied = self._web_denied()
        if denied:
            return denied
        query = request.query
        try:
            page = max(1, int(query.get("page", "1")))
        except (TypeError, ValueError):
            page = 1
        try:
            page_size = max(1, min(60, int(query.get("page_size", "24"))))
        except (TypeError, ValueError):
            page_size = 24
        status_filter = str(query.get("status", "") or "").strip()
        tag_filter = str(query.get("tag", "") or "").strip()
        q = str(query.get("q", "") or "").strip().lower()

        stickers = await self.store.list_all()
        inbox_all = [s for s in stickers if not s.confirmed]
        stickers = [s for s in stickers if s.confirmed]
        if status_filter in (STATUS_READY, STATUS_PENDING, STATUS_DISABLED):
            stickers = [s for s in stickers if s.status == status_filter]
        if tag_filter:
            stickers = [s for s in stickers if tag_filter in s.tags]
        if q:
            stickers = [
                s
                for s in stickers
                if q
                in " ".join(
                    [
                        s.id,
                        s.filename,
                        s.description,
                        " ".join(s.tags),
                        " ".join(s.keywords),
                    ]
                ).lower()
            ]

        total = len(stickers)
        start = (page - 1) * page_size
        page_items = stickers[start : start + page_size]
        thumbs = await asyncio.gather(
            *(self._image_data_url(s, 240, "thumb") for s in page_items)
        )
        items = [
            self._sticker_payload(s, thumb=thumbs[i])
            for i, s in enumerate(page_items)
        ]
        inbox_page = inbox_all[:24]
        inbox_thumbs = await asyncio.gather(
            *(self._image_data_url(s, 160, "thumb") for s in inbox_page)
        )
        inbox_items = [
            self._sticker_payload(s, thumb=inbox_thumbs[i])
            for i, s in enumerate(inbox_page)
        ]
        stats = await self.store.stats()
        library_status = {"ready": 0, "pending": 0, "disabled": 0}
        for sticker in stickers:
            library_status[sticker.status] = (
                library_status.get(sticker.status, 0) + 1
            )
        stats["library_status"] = library_status
        return json_response(
            {
                "items": items,
                "total": total,
                "page": page,
                "page_size": page_size,
                "inbox": inbox_items,
                "inbox_total": len(inbox_all),
                "stats": stats,
                "settings": {
                    "enabled": self._enabled(),
                    "safety_filter_enabled": self.cfg_bool(
                        "learning", "safety_filter_enabled", True
                    ),
                },
                "tags": TAG_LABELS,
            }
        )

    async def api_preview(self, sticker_id: str):
        denied = self._web_denied()
        if denied:
            return denied
        sticker = await self.store.get(sticker_id)
        if not sticker:
            return error_response("表情包不存在", status_code=404)
        data_url = await self._image_data_url(sticker, 720, "preview")
        return json_response(
            {"id": sticker.id, "data_url": data_url}
        )

    async def api_update(self):
        denied = self._web_denied()
        if denied:
            return denied
        payload = await request.json({})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON")
        sticker_id = str(payload.get("id", "")).strip()
        sticker = await self.store.get(sticker_id)
        if not sticker:
            return error_response("表情包不存在", status_code=404)

        tags_arg = None
        if "tags" in payload:
            raw_tags = payload.get("tags")
            if isinstance(raw_tags, str):
                raw_tags = re.split(r"[,，、|;；\s]+", raw_tags)
            tags_arg = normalize_tags(raw_tags or []) or ["react"]

        keywords_arg = None
        if "keywords" in payload:
            raw_keywords = payload.get("keywords")
            if isinstance(raw_keywords, str):
                raw_keywords = re.split(r"[,，、|;；\s]+", raw_keywords)
            keywords_arg = [
                str(k).strip()
                for k in (raw_keywords or [])
                if str(k).strip()
            ][:8]

        usage_arg = payload.get("usage")
        if usage_arg not in ("reaction", "augment", "both", None):
            usage_arg = None
        intensity_arg = None
        if payload.get("intensity") is not None:
            try:
                intensity_arg = max(1, min(5, int(payload["intensity"])))
            except (TypeError, ValueError):
                intensity_arg = None
        status_arg = payload.get("status")
        if status_arg not in (STATUS_READY, STATUS_PENDING, STATUS_DISABLED, None):
            status_arg = None
        safe_arg = (
            self._as_bool(payload.get("safe"), sticker.safe)
            if "safe" in payload
            else None
        )

        updated = await self.store.update_fields(
            sticker.id,
            description=(
                str(payload.get("description", "")).strip()[:300]
                if "description" in payload
                else None
            ),
            tags=tags_arg,
            keywords=keywords_arg,
            usage=usage_arg,
            intensity=intensity_arg,
            safe=safe_arg,
            status=status_arg,
        )
        if not updated:
            return error_response("更新失败", status_code=500)
        self._invalidate_cache()
        thumb = await self._image_data_url(updated, 240, "thumb")
        return json_response({"item": self._sticker_payload(updated, thumb=thumb)})

    async def api_retag(self):
        denied = self._web_denied()
        if denied:
            return denied
        payload = await request.json({})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON")
        sticker = await self.store.get(str(payload.get("id", "")).strip())
        if not sticker:
            return error_response("表情包不存在", status_code=404)

        description_arg = None
        if "description" in payload:
            description_arg = str(payload.get("description", "")).strip()[:300]
        keywords_arg = None
        if "keywords" in payload:
            raw_keywords = payload.get("keywords")
            if isinstance(raw_keywords, str):
                raw_keywords = re.split(r"[,，、|;；\s]+", raw_keywords)
            keywords_arg = [
                str(k).strip()
                for k in (raw_keywords or [])
                if str(k).strip()
            ][:8]
        update_keywords = self._as_bool(
            payload.get("update_keywords"), True
        )

        result = await self.classifier.retag_from_text(
            sticker,
            None,
            description=description_arg,
            keywords=keywords_arg,
        )
        if not result:
            return error_response(
                "按描述重算标签失败，请确认已配置文字模型或 learning.retag_provider_id"
            )
        keywords_final = keywords_arg
        if update_keywords:
            keywords_final = result.get("keywords") or keywords_arg
        updated = await self.store.update_fields(
            sticker.id,
            description=description_arg,
            keywords=keywords_final,
            tags=result["tags"],
            intensity=result["intensity"],
            usage=result["usage"],
        )
        if not updated:
            return error_response("更新标签失败", status_code=500)
        self._invalidate_cache()
        thumb = await self._image_data_url(updated, 240, "thumb")
        return json_response(
            {
                "item": self._sticker_payload(updated, thumb=thumb),
                "provider_id": self.cfg_str(
                    "learning", "retag_provider_id", ""
                ),
            }
        )

    async def api_confirm(self):
        denied = self._web_denied()
        if denied:
            return denied
        payload = await request.json({})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON")
        if payload.get("all"):
            stickers = await self.store.list_all()
            ids = [s.id for s in stickers if not s.confirmed]
        else:
            sticker_id = str(payload.get("id", "")).strip()
            sticker = await self.store.get(sticker_id)
            if not sticker:
                return error_response("表情包不存在", status_code=404)
            ids = [sticker.id]
        for sticker_id in ids:
            await self.store.set_confirmed(sticker_id, True)
        self._invalidate_cache()
        return json_response({"confirmed": len(ids)})

    async def api_delete(self):
        denied = self._web_denied()
        if denied:
            return denied
        payload = await request.json({})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON")
        sticker_id = str(payload.get("id", "")).strip()
        if not sticker_id:
            return error_response("缺少表情包 ID")
        ok = await self.store.delete(sticker_id)
        self._invalidate_cache()
        if not ok:
            return error_response("表情包不存在", status_code=404)
        return json_response({"ok": True, "id": sticker_id})

    async def api_reclassify(self):
        denied = self._web_denied()
        if denied:
            return denied
        payload = await request.json({})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON")
        if payload.get("all"):
            stickers = await self.store.list_all()
            ids = [s.id for s in stickers if s.status != STATUS_DISABLED]
        else:
            sticker_id = str(payload.get("id", "")).strip()
            sticker = await self.store.get(sticker_id)
            if not sticker:
                return error_response("表情包不存在", status_code=404)
            ids = [sticker.id]
        for sticker_id in ids:
            self.classifier.enqueue(
                sticker_id, None, force=True
            )
        return json_response({"queued": len(ids)})

    async def api_safety(self):
        denied = self._web_denied()
        if denied:
            return denied
        payload = await request.json({})
        if not isinstance(payload, dict):
            return error_response("请求体必须是 JSON")
        enabled = self._as_bool(payload.get("enabled"), True)
        try:
            self._set_safety(enabled)
        except Exception as e:
            return error_response(f"保存配置失败：{e}", status_code=500)
        return json_response({"enabled": enabled})

    async def api_upload(self):
        denied = self._web_denied()
        if denied:
            return denied
        files = await request.files()
        upload = files.get("file")
        if not upload:
            return error_response("没有收到文件")
        filename = upload.filename or "upload.png"
        suffix = Path(filename).suffix.lower()
        if suffix not in ALLOWED_EXTS:
            return error_response("只支持 png/jpg/jpeg/gif/webp/bmp")
        max_mb = max(1, self.cfg_int("learning", "max_file_mb", 8))
        if upload.content_length and upload.content_length > max_mb * 1024 * 1024:
            return error_response(f"文件超过 {max_mb} MB")
        uploads = self.data_dir / "uploads"
        uploads.mkdir(parents=True, exist_ok=True)
        temp_path = uploads / f"{uuid.uuid4().hex}{suffix}"
        sticker = None
        created = False
        try:
            await upload.save(temp_path)
            sticker, created = await self.store.add_file(
                temp_path, source="web_upload", original_name=filename
            )
        finally:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        if not sticker:
            return error_response("导入失败，可能不是有效图片")
        self._invalidate_cache()
        if created:
            self.classifier.enqueue(sticker.id, None)
        thumb = await self._image_data_url(sticker, 240, "thumb")
        return json_response(
            {
                "created": created,
                "item": self._sticker_payload(sticker, thumb=thumb),
            }
        )

    # ==================================================================
    # 消息监听 / 自动学习
    # ==================================================================

    @filter.event_message_type(filter.EventMessageType.ALL)
    async def on_message(self, event: AstrMessageEvent) -> None:
        if not self._enabled() or not self._in_targets(event):
            return
        scope = self._scope(event)
        sender = str(event.get_sender_id() or "")
        self_id = str(event.get_self_id() or "")
        if sender and self_id and sender == self_id:
            return

        text = (event.message_str or event.get_message_outline() or "").strip()
        if text and not text.startswith("/"):
            self._remember(scope, sender, text)

        if self.cfg_bool("learning", "auto_learn", True):
            try:
                await self._maybe_learn(event, scope)
            except Exception as e:
                logger.debug(f"[StickerSemantic] 自动学习失败: {e}")

    def _raw_image_data(
        self, event: AstrMessageEvent, image: Image, index: int, total: int
    ) -> dict | None:
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        if not isinstance(raw, dict):
            return None
        segments = raw.get("message")
        if not isinstance(segments, list):
            return None

        image_file = str(getattr(image, "file", "") or "")
        image_path = str(getattr(image, "path", "") or "")
        candidates = [
            seg
            for seg in segments
            if isinstance(seg, dict) and str(seg.get("type", "")).lower() == "image"
        ]
        if not candidates:
            return None

        for seg in candidates:
            data = seg.get("data")
            if not isinstance(data, dict):
                continue
            file_value = str(data.get("file", "") or "")
            if not file_value:
                continue
            if file_value == image_file or (
                image_path and file_value == Path(image_path).name
            ):
                return data

        if len(candidates) == total:
            data = candidates[index].get("data")
            return data if isinstance(data, dict) else None
        first = candidates[0].get("data")
        return first if isinstance(first, dict) else None

    def _looks_like_sticker(self, data: dict | None) -> bool:
        if not isinstance(data, dict):
            return False
        sub_type = data.get("sub_type")
        try:
            if int(sub_type) == 1:
                return True
        except (TypeError, ValueError):
            pass
        summary = str(data.get("summary", "") or "").lower()
        if "表情" in summary or "sticker" in summary or "动画表情" in summary:
            return True
        file_value = str(data.get("file", "") or "").lower()
        return "sticker" in file_value or "emoji" in file_value

    async def _maybe_learn(self, event: AstrMessageEvent, scope: str) -> None:
        images = [c for c in event.get_messages() if isinstance(c, Image)]
        if not images:
            return

        now = time.time()
        if now - self._last_learn.get(scope, 0.0) < 3.0:
            return
        self._last_learn[scope] = now

        max_count = max(1, self.cfg_int("general", "max_stickers", 500))
        if await self.store.count() >= max_count:
            return

        learn_users = set(self.cfg_list("learning", "learn_target_users"))
        sender = str(event.get_sender_id() or "")
        if learn_users and sender not in learn_users:
            return

        learn_all = self.cfg_bool("learning", "learn_all_images", False)
        max_mb = max(1, self.cfg_int("learning", "max_file_mb", 8))
        for index, image in enumerate(images):
            if await self.store.count() >= max_count:
                break
            data = self._raw_image_data(event, image, index, len(images))
            if not learn_all and not self._looks_like_sticker(data):
                continue
            path = await self._resolve_image_path(image)
            if not path:
                continue
            try:
                if path.stat().st_size > max_mb * 1024 * 1024:
                    continue
            except OSError:
                continue
            sticker, created = await self.store.add_file(
                path, source="message", original_name=path.name
            )
            if created and sticker:
                self._invalidate_cache()
                logger.info(
                    f"[StickerSemantic] 学到新表情包: {sticker.id} ({path.name})"
                )
                self.classifier.enqueue(sticker.id, event.unified_msg_origin)

    async def _resolve_image_path(self, image: Image) -> Path | None:
        try:
            raw = await image.convert_to_file_path()
            path = Path(raw)
            return path if path.is_file() else None
        except Exception:
            return None

    # ==================================================================
    # LLM 请求：注入候选
    # ==================================================================

    @filter.on_llm_request()
    async def on_llm_request(
        self, event: AstrMessageEvent, req: ProviderRequest
    ) -> None:
        if not self._enabled() or not self._in_targets(event):
            return
        if not self.cfg_bool("prompt", "inject_enabled", True):
            return
        if event.get_extra(_EXTRA_INJECTED):
            return
        event.set_extra(_EXTRA_INJECTED, True)

        stickers = await self._ready_stickers()
        if not stickers:
            return

        context_text = self._build_context(event, req)
        signals = extract_signals(context_text)
        ranked = rank_stickers(
            stickers,
            signals,
            signals,
            mode=MODE_APPEND,
            recent_ids=self._recent_ids(stickers),
        )
        count = max(1, min(5, self.cfg_int("prompt", "inject_candidates", 3)))
        top = [item for item in ranked[:count] if item.score > 0.04]
        if not top:
            return

        event.set_extra(_EXTRA_CONTEXT, context_text)
        event.set_extra(_EXTRA_CANDIDATES, [item.sticker.id for item in top])
        block = self._build_prompt_block(top)
        max_chars = max(120, self.cfg_int("prompt", "inject_max_chars", 600))
        if len(block) > max_chars:
            block = block[:max_chars].rstrip() + "…"
        req.system_prompt = (req.system_prompt or "") + block

    def _build_prompt_block(self, top: list[ScoredSticker]) -> str:
        lines = ["\n\n[表情包]"]
        lines.append("你可以用表情包参与聊天。当前语境较匹配：")
        for item in top:
            sticker = item.sticker
            tags = describe_tags(sticker.tags) or "未分类"
            desc = (sticker.description or sticker.filename)[:46]
            lines.append(f"- {sticker.id}: {tags}｜{desc}")
        if self.cfg_bool("prompt", "tool_enabled", True):
            lines.append(
                "需要其他表情包可调用 search_stickers；选定后调用 pick_sticker。"
                "如果一句话或一个表情就够，先 pick_sticker，再只输出 [STICKER]；"
                "否则正常回复，插件会按语境决定是否附加。"
            )
        else:
            lines.append("如果一句话就够，可以只输出 [STICKER]。")
        return "\n".join(lines) + "\n"

    # ==================================================================
    # 发送前：选择并附加/替换
    # ==================================================================

    @filter.on_decorating_result()
    async def on_decorating_result(self, event: AstrMessageEvent) -> None:
        if not self._enabled() or not self._in_targets(event):
            return
        result = event.get_result()
        if result is None or not result.chain:
            return
        is_llm = getattr(result, "is_llm_result", None)
        if callable(is_llm) and not is_llm():
            return
        if event.get_extra(_EXTRA_USED):
            return
        if any(isinstance(c, Image) for c in result.chain):
            return

        text = "".join(c.text for c in result.chain if isinstance(c, Plain) and c.text)
        body, marker = strip_sticker_markers(text)
        body = re.sub(r"\s+", " ", body).strip()
        if marker:
            self._strip_markers(result.chain)
        pick_id = str(event.get_extra(_EXTRA_PICK, "") or "")
        scope = self._scope(event)
        if not self._can_send(scope):
            return

        stickers = await self._ready_stickers()
        if not stickers:
            return

        context_text = (
            str(event.get_extra(_EXTRA_CONTEXT, "") or "")
            or self._build_context(event)
        )
        ctx = extract_signals(context_text)
        reply = extract_signals(body or context_text)
        recent_ids = self._recent_ids(stickers)
        max_chars = max(4, self.cfg_int("selection", "light_reply_max_chars", 12))

        # 1) 模型显式选了表情包
        if pick_id:
            sticker = next((s for s in stickers if s.id == pick_id), None)
            if sticker is None:
                sticker = await self.store.get(pick_id)
            if sticker and sticker.safe and sticker.status == STATUS_READY:
                light = (
                    (bool(marker) and (not body or is_light_reply(body, max_chars)))
                    or (not body)
                    or reply_allows_sticker_only(body, max_chars)
                )
                if light:
                    await self._replace_with_sticker(event, result, sticker, scope)
                else:
                    await self._append_sticker(event, result, sticker, scope)
                return

        # 2) 模型只输出占位符，或回复本身是轻回复
        if is_light_reply(body, max_chars=max_chars):
            if not body and not marker:
                return
            if looks_factual(body):
                return
            if not self.cfg_bool("selection", "light_reply_enabled", True):
                return
            threshold = self.cfg_float("selection", "light_min_score", 0.30)
            chance = self.cfg_float("selection", "light_trigger_probability", 0.9)
            if self._rng.random() > max(0.0, min(1.0, chance)):
                return
            explicit_marker = bool(marker) and not body
            choice = choose_sticker(
                stickers,
                ctx,
                reply,
                mode=MODE_LIGHT,
                threshold=0.0 if explicit_marker else threshold,
                recent_ids=recent_ids,
                rng=self._rng,
                allow_usage_mismatch=explicit_marker,
            )
            if choice:
                await self._replace_with_sticker(
                    event, result, choice.sticker, scope
                )
            return

        # 3) 普通回复：按概率附加
        if not self.cfg_bool("selection", "append_enabled", True):
            return
        if is_question_text(body):
            return
        min_chars = max(1, self.cfg_int("selection", "append_min_text_chars", 8))
        if len(body) < min_chars:
            return
        chance = self.cfg_float("selection", "append_probability", 0.25)
        if self._rng.random() > max(0.0, min(1.0, chance)):
            return
        threshold = self.cfg_float("selection", "append_min_score", 0.45)
        choice = choose_sticker(
            stickers,
            ctx,
            reply,
            mode=MODE_APPEND,
            threshold=threshold,
            recent_ids=recent_ids,
            rng=self._rng,
        )
        if choice:
            await self._append_sticker(event, result, choice.sticker, scope)

    async def _replace_with_sticker(
        self, event: AstrMessageEvent, result, sticker: Sticker, scope: str
    ) -> None:
        path = Path(sticker.path)
        if not path.is_file():
            await self.store.set_status(sticker.id, STATUS_DISABLED)
            self._invalidate_cache()
            return
        result.chain = [Image.fromFileSystem(str(path))]
        event.set_extra(_EXTRA_USED, sticker.id)
        await self._mark_used(sticker)
        self._record_send(scope)
        logger.info(
            f"[StickerSemantic] 只发表情包 scope={scope} id={sticker.id} "
            f"tags={sticker.tags} desc={sticker.description[:30]}"
        )

    async def _append_sticker(
        self, event: AstrMessageEvent, result, sticker: Sticker, scope: str
    ) -> None:
        path = Path(sticker.path)
        if not path.is_file():
            await self.store.set_status(sticker.id, STATUS_DISABLED)
            self._invalidate_cache()
            return
        result.chain.append(Image.fromFileSystem(str(path)))
        event.set_extra(_EXTRA_USED, sticker.id)
        await self._mark_used(sticker)
        self._record_send(scope)
        logger.info(
            f"[StickerSemantic] 附加表情包 scope={scope} id={sticker.id} "
            f"tags={sticker.tags} desc={sticker.description[:30]}"
        )

    async def _mark_used(self, sticker: Sticker) -> None:
        now = time.time()
        sticker.last_used_at = now
        sticker.use_count += 1
        try:
            await self.store.mark_used([sticker.id])
        except Exception as e:
            logger.debug(f"[StickerSemantic] 记录使用失败: {e}")

    def _can_send(self, scope: str) -> bool:
        limit = self.cfg_int("selection", "max_per_hour", 10)
        if limit <= 0:
            return True
        now = time.time()
        dq = self._send_times.setdefault(scope, deque())
        while dq and now - dq[0] > 3600:
            dq.popleft()
        return len(dq) < limit

    def _record_send(self, scope: str) -> None:
        self._send_times.setdefault(scope, deque()).append(time.time())

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent) -> None:
        if not self._enabled() or not self._in_targets(event):
            return
        scope = self._scope(event)
        result = event.get_result()
        if result is None or not result.chain:
            return
        texts = [
            c.text.strip()
            for c in result.chain
            if isinstance(c, Plain) and c.text and c.text.strip()
        ]
        if texts:
            self._remember(scope, "bot", " ".join(texts)[:240])
        elif any(isinstance(c, Image) for c in result.chain):
            self._remember(scope, "bot", "[表情包]")

    # ==================================================================
    # LLM 工具
    # ==================================================================

    @filter.llm_tool(name="search_stickers")
    async def search_stickers_tool(
        self, event: AstrMessageEvent, query: str, limit: int = 5
    ) -> str:
        """根据情绪、场景或关键词搜索可用表情包。

        Args:
            query(string): 想表达的情绪/场景/关键词，如“开心”“无语”“安慰”
            limit(number): 返回数量，默认 5，最大 10（可选）
        """
        if not self._enabled():
            return "表情包插件未启用。"
        if not self.cfg_bool("prompt", "tool_enabled", True):
            return "表情包工具未启用。"
        stickers = await self._ready_stickers()
        if not stickers:
            return "表情包库为空。"
        try:
            limit = max(1, min(10, int(limit)))
        except (TypeError, ValueError):
            limit = 5
        signals = extract_signals(query)
        ranked = rank_stickers(
            stickers,
            signals,
            signals,
            mode=MODE_APPEND,
            recent_ids=self._recent_ids(stickers),
            allow_usage_mismatch=True,
        )
        if not ranked:
            return "没有找到匹配的表情包。"
        lines = []
        for item in ranked[:limit]:
            sticker = item.sticker
            lines.append(
                f"- {sticker.id} | {describe_tags(sticker.tags) or '未分类'} | "
                f"{sticker.description[:50]} | {sticker.usage}"
            )
        event.set_extra(_EXTRA_CANDIDATES, [x.sticker.id for x in ranked[:limit]])
        return "可用表情包：\n" + "\n".join(lines)

    @filter.llm_tool(name="pick_sticker")
    async def pick_sticker_tool(
        self, event: AstrMessageEvent, sticker_id: str, reason: str = ""
    ) -> str:
        """选定一张表情包，插件会在最终回复时发送它。只选定，不直接发送。

        Args:
            sticker_id(string): 表情包 ID，来自 search_stickers 或注入的候选列表
            reason(string): 选择理由（可选）
        """
        if not self._enabled():
            return "表情包插件未启用。"
        if not self.cfg_bool("prompt", "tool_enabled", True):
            return "表情包工具未启用。"
        sticker = await self.store.get(str(sticker_id).strip())
        if not sticker or sticker.status != STATUS_READY or not sticker.safe:
            return f"找不到可用的表情包 {sticker_id}，请先调用 search_stickers。"
        event.set_extra(_EXTRA_PICK, sticker.id)
        return (
            f"已选定表情包 {sticker.id}（{describe_tags(sticker.tags)}："
            f"{sticker.description[:40]}）。最终回复只输出 [STICKER] 即可只发表情包。"
        )

    # ==================================================================
    # 命令
    # ==================================================================

    @filter.command_group("sk")
    def sk_group(self):
        """语义表情包管理"""

    @sk_group.command("help")
    async def sk_help(self, event: AstrMessageEvent):
        """查看表情包插件帮助"""
        yield event.plain_result(
            "【语义表情包】\n"
            "/sk list [页]  查看表情包\n"
            "/sk stats  统计\n"
            "/sk test <文本>  测试匹配（管理员）\n"
            "/sk import [路径]  导入目录/文件（管理员）\n"
            "/sk add  把当前图片加入库（管理员）\n"
            "/sk tag <ID> <标签>  手动改标签（管理员）\n"
            "/sk desc <ID|回复> <描述>  改描述（管理员）\n"
            "/sk keywords <ID|回复> <词1,词2>  改匹配关键词（管理员）\n"
            "/sk retag <ID|回复>  按描述重算标签（管理员）\n"
            "/sk info <ID|回复>  查看详情（管理员）\n"
            "/sk usage <ID> reaction|augment|both（管理员）\n"
            "/sk safe <ID> on|off（管理员）\n"
            "/sk safety on|off  开关不适宜内容过滤（管理员）\n"
            "/sk enableall  恢复所有 disabled（管理员）\n"
            "/sk enable|disable|delete <ID>（管理员）\n"
            "/sk reclassify <ID|all>  重新分类（管理员）\n"
            "/sk reload  刷新缓存（管理员）\n"
            "标签示例：happy,laugh,tease,comfort"
        )

    @sk_group.command("list")
    async def sk_list(self, event: AstrMessageEvent, page: str = "1"):
        """分页查看表情包"""
        try:
            page_num = max(1, int(page))
        except (TypeError, ValueError):
            page_num = 1
        size = max(1, min(20, self.cfg_int("commands", "page_size", 8)))
        stickers = await self.store.list_all()
        if not stickers:
            yield event.plain_result(
                "表情包库为空。用 /sk import <路径> 导入，或直接发一张表情包让我学习。"
            )
            return
        total_pages = max(1, (len(stickers) + size - 1) // size)
        page_num = min(page_num, total_pages)
        start = (page_num - 1) * size
        lines = [f"[表情包 {page_num}/{total_pages}] 共 {len(stickers)} 张"]
        for s in stickers[start : start + size]:
            lines.append(
                f"{s.id} | {describe_tags(s.tags) or '未分类'} | "
                f"{(s.description or s.filename)[:34]} | {s.usage} | {s.status}"
            )
        yield event.plain_result("\n".join(lines))

    @sk_group.command("stats")
    async def sk_stats(self, event: AstrMessageEvent):
        """查看表情包统计"""
        stats = await self.store.stats()
        status = stats["status"]
        lines = [
            f"总数 {stats['total']} | 新加入 {stats.get('new', 0)} | "
            f"ready {status.get('ready', 0)} | "
            f"pending {status.get('pending', 0)} | disabled {status.get('disabled', 0)}"
        ]
        tags = stats["tags"]
        if tags:
            top = list(tags.items())[:12]
            lines.append("标签: " + ", ".join(f"{k}:{v}" for k, v in top))
        yield event.plain_result("\n".join(lines))

    @sk_group.command("test")
    async def sk_test(self, event: AstrMessageEvent, text: str = ""):
        """测试一段文本的表情包匹配（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        query = (
            self._raw_after(event, "test") or text or event.message_str or ""
        ).strip()
        if not query:
            yield event.plain_result("用法：/sk test <文本>")
            return
        stickers = await self._ready_stickers()
        if not stickers:
            yield event.plain_result("表情包库为空。")
            return
        signals = extract_signals(query)
        ranked = rank_stickers(
            stickers,
            signals,
            signals,
            mode=MODE_APPEND,
            recent_ids=self._recent_ids(stickers),
            allow_usage_mismatch=True,
        )
        if not ranked:
            yield event.plain_result("没有匹配结果。")
            return
        lines = [f"文本：{query[:60]}"]
        for item in ranked[:5]:
            s = item.sticker
            lines.append(
                f"{item.score:.3f} | {s.id} | {describe_tags(s.tags)} | "
                f"{s.description[:40]} | {s.usage}"
            )
        yield event.plain_result("\n".join(lines))

    @sk_group.command("import")
    async def sk_import(self, event: AstrMessageEvent, path: str = ""):
        """导入一个目录或图片文件（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        raw_path = self._raw_after(event, "import") or path
        raw_path = raw_path.strip().strip("\"'")
        if not raw_path:
            raw_path = self.cfg_str("general", "import_dir", "") or str(
                self.data_dir / "imports"
            )
        root = Path(raw_path).expanduser()
        if not root.exists():
            yield event.plain_result(f"路径不存在：{root}")
            return
        files = self._scan_images(root)
        if not files:
            yield event.plain_result(f"没有找到图片：{root}")
            return

        new_ids: list[str] = []
        limit = max(1, self.cfg_int("general", "max_stickers", 500))
        current = await self.store.count()
        for file_path in files:
            if current + len(new_ids) >= limit:
                break
            try:
                sticker, created = await self.store.add_file(
                    file_path, source="import", original_name=file_path.name
                )
            except Exception as e:
                logger.warning(f"[StickerSemantic] 导入失败 {file_path}: {e}")
                continue
            if created and sticker:
                new_ids.append(sticker.id)
        self._invalidate_cache()

        if not new_ids:
            yield event.plain_result(
                f"扫描到 {len(files)} 个文件，但没有新增"
                f"（可能都已存在或达到上限 {limit}）。"
            )
            return
        yield event.plain_result(
            f"已导入 {len(new_ids)} 张，开始后台分类。完成后会在这里通知。"
        )
        self._spawn(
            self._import_job(
                new_ids,
                event.unified_msg_origin,
                total_scanned=len(files),
            )
        )

    @sk_group.command("add")
    async def sk_add(self, event: AstrMessageEvent):
        """把当前消息中的图片加入表情包库（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        images = [c for c in event.get_messages() if isinstance(c, Image)]
        if not images:
            yield event.plain_result("请在发送图片后使用：/sk add")
            return
        added = 0
        for image in images:
            path = await self._resolve_image_path(image)
            if not path:
                continue
            sticker, created = await self.store.add_file(
                path, source="manual", original_name=path.name
            )
            if created and sticker:
                added += 1
                self.classifier.enqueue(sticker.id, event.unified_msg_origin)
        self._invalidate_cache()
        if not added:
            yield event.plain_result("没有新增（可能已存在）。")
            return
        yield event.plain_result(f"已加入 {added} 张，后台分类中。用 /sk list 查看。")

    @sk_group.command("tag")
    async def sk_tag(
        self, event: AstrMessageEvent, sticker_id: str, tags: str = ""
    ):
        """手动设置标签（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        raw = self._raw_after(event, "tag")
        parts = raw.split(None, 1) if raw else []
        if len(parts) >= 2:
            sticker_id, tags = parts[0], parts[1]
        sticker = await self.store.get(sticker_id)
        if not sticker:
            yield event.plain_result(f"找不到 {sticker_id}")
            return
        normalized = normalize_tags(re.split(r"[,，、|;；\s]+", tags or ""))
        if not normalized:
            yield event.plain_result(
                f"没有有效标签。可用标签：{', '.join(sorted(ALL_TAGS))}"
            )
            return
        await self.store.set_tags(sticker.id, normalized, status=STATUS_READY)
        self._invalidate_cache()
        yield event.plain_result(
            f"{sticker.id} 标签已设为：{describe_tags(normalized)}"
        )

    @sk_group.command("desc")
    async def sk_desc(
        self, event: AstrMessageEvent, sticker_id: str = "", description: str = ""
    ):
        """手动设置描述；可以回复表情包省略 ID（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        raw = self._raw_after(event, "desc") or " ".join(
            x for x in (sticker_id, description) if x
        )
        sticker, payload = await self._resolve_sticker_target(event, raw)
        if not sticker:
            yield event.plain_result(
                "没找到目标表情包。用法：/sk desc <ID> <描述>，"
                "或回复一张表情包后发送 /sk desc <描述>。"
            )
            return
        if not payload:
            yield event.plain_result("请提供新描述：/sk desc <ID> <描述>")
            return
        await self.store.set_description(sticker.id, payload)
        self._invalidate_cache()
        yield event.plain_result(
            f"{sticker.id} 描述已更新为：{payload[:60]}"
        )

    @sk_group.command("keywords")
    async def sk_keywords(
        self, event: AstrMessageEvent, sticker_id: str = "", keywords: str = ""
    ):
        """手动设置匹配关键词；可以回复表情包省略 ID（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        raw = self._raw_after(event, "keywords") or " ".join(
            x for x in (sticker_id, keywords) if x
        )
        sticker, payload = await self._resolve_sticker_target(event, raw)
        if not sticker:
            yield event.plain_result(
                "没找到目标表情包。用法：/sk keywords <ID> <词1,词2>，"
                "或回复一张表情包后发送 /sk keywords <词1,词2>。"
            )
            return
        words = [
            item.strip()
            for item in re.split(r"[,，、|;；\s]+", payload or "")
            if item.strip()
        ]
        if not words:
            yield event.plain_result(
                "请提供关键词，例如：/sk keywords <ID> 哈哈哈,笑死,绷不住"
            )
            return
        await self.store.set_keywords(sticker.id, words)
        self._invalidate_cache()
        yield event.plain_result(
            f"{sticker.id} 关键词已更新为：{', '.join(words[:8])}"
        )

    @sk_group.command("retag")
    async def sk_retag(self, event: AstrMessageEvent, sticker_id: str = ""):
        """按当前描述/关键词用文字模型重算标签（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        raw = self._raw_after(event, "retag") or sticker_id
        sticker, _payload = await self._resolve_sticker_target(event, raw)
        if not sticker:
            yield event.plain_result(
                "没找到目标表情包。用法：/sk retag <ID>，或回复一张表情包后发送 /sk retag。"
            )
            return
        result = await self.classifier.retag_from_text(
            sticker, event.unified_msg_origin
        )
        if not result:
            yield event.plain_result(
                "按描述重算标签失败，请确认已配置文字模型或 learning.retag_provider_id。"
            )
            return
        updated = await self.store.update_fields(
            sticker.id,
            keywords=result.get("keywords") or None,
            tags=result["tags"],
            intensity=result["intensity"],
            usage=result["usage"],
        )
        self._invalidate_cache()
        if not updated:
            yield event.plain_result("更新标签失败。")
            return
        yield event.plain_result(
            f"{sticker.id} 标签已按描述重算：{describe_tags(updated.tags)}"
        )

    @sk_group.command("info")
    async def sk_info(self, event: AstrMessageEvent, sticker_id: str = ""):
        """查看表情包详情；可以回复表情包省略 ID（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        raw = self._raw_after(event, "info") or sticker_id
        sticker, _payload = await self._resolve_sticker_target(event, raw)
        if not sticker:
            yield event.plain_result(
                "没找到目标表情包。用法：/sk info <ID>，或回复一张表情包后发送 /sk info。"
            )
            return
        lines = [
            f"ID: {sticker.id}",
            f"文件: {sticker.filename}",
            f"状态: {sticker.status} | safe: {sticker.safe} | usage: {sticker.usage}",
            f"标签: {describe_tags(sticker.tags) or '无'}",
            f"强度: {sticker.intensity}/5 | 使用次数: {sticker.use_count}",
            f"描述: {sticker.description or '无'}",
            f"关键词: {', '.join(sticker.keywords) if sticker.keywords else '无'}",
        ]
        yield event.plain_result("\n".join(lines))

    @sk_group.command("usage")
    async def sk_usage(
        self, event: AstrMessageEvent, sticker_id: str, usage: str = ""
    ):
        """设置用法：reaction / augment / both（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        if usage not in ("reaction", "augment", "both"):
            yield event.plain_result("用法：/sk usage <ID> reaction|augment|both")
            return
        sticker = await self.store.get(sticker_id)
        if not sticker:
            yield event.plain_result(f"找不到 {sticker_id}")
            return
        await self.store.set_usage(sticker.id, usage)
        self._invalidate_cache()
        yield event.plain_result(f"{sticker.id} 用法已设为 {usage}。")

    @sk_group.command("safe")
    async def sk_safe(
        self, event: AstrMessageEvent, sticker_id: str, value: str = ""
    ):
        """标记表情包是否安全（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        if value.lower() not in (
            "on",
            "off",
            "1",
            "0",
            "true",
            "false",
            "是",
            "否",
        ):
            yield event.plain_result("用法：/sk safe <ID> on|off")
            return
        sticker = await self.store.get(sticker_id)
        if not sticker:
            yield event.plain_result(f"找不到 {sticker_id}")
            return
        safe = value.lower() in ("on", "1", "true", "是")
        await self.store.set_safe(sticker.id, safe)
        await self.store.set_status(
            sticker.id, STATUS_READY if safe and sticker.tags else sticker.status
        )
        self._invalidate_cache()
        yield event.plain_result(f"{sticker.id} safe={safe}")

    @sk_group.command("enable")
    async def sk_enable(self, event: AstrMessageEvent, sticker_id: str):
        """启用表情包（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        sticker = await self.store.get(sticker_id)
        if not sticker:
            yield event.plain_result(f"找不到 {sticker_id}")
            return
        status = STATUS_READY if sticker.tags else STATUS_PENDING
        await self.store.set_status(sticker.id, status)
        self._invalidate_cache()
        yield event.plain_result(f"{sticker.id} 已启用（{status}）。")

    @sk_group.command("disable")
    async def sk_disable(self, event: AstrMessageEvent, sticker_id: str):
        """停用表情包（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        sticker = await self.store.get(sticker_id)
        if not sticker:
            yield event.plain_result(f"找不到 {sticker_id}")
            return
        await self.store.set_status(sticker.id, STATUS_DISABLED)
        self._invalidate_cache()
        yield event.plain_result(f"{sticker.id} 已停用。")

    @sk_group.command("delete")
    async def sk_delete(self, event: AstrMessageEvent, sticker_id: str):
        """删除表情包（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        ok = await self.store.delete(sticker_id)
        self._invalidate_cache()
        yield event.plain_result(
            f"{sticker_id} 已删除。" if ok else f"找不到 {sticker_id}"
        )

    @sk_group.command("reclassify")
    async def sk_reclassify(self, event: AstrMessageEvent, target: str = "all"):
        """重新分类一张或全部表情包（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        if target == "all":
            stickers = await self.store.list_all()
            ids = [s.id for s in stickers if s.status != STATUS_DISABLED]
        else:
            sticker = await self.store.get(target)
            if not sticker:
                yield event.plain_result(f"找不到 {target}")
                return
            ids = [sticker.id]
        if not ids:
            yield event.plain_result("没有可重新分类的表情包。")
            return
        yield event.plain_result(f"开始重新分类 {len(ids)} 张，完成后通知。")
        self._spawn(self._reclassify_job(ids, event.unified_msg_origin))

    @sk_group.command("reload")
    async def sk_reload(self, event: AstrMessageEvent):
        """刷新缓存（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        self._invalidate_cache()
        stickers = await self._ready_stickers(force=True)
        yield event.plain_result(f"缓存已刷新，可用表情包 {len(stickers)} 张。")

    @sk_group.command("safety")
    async def sk_safety(self, event: AstrMessageEvent, value: str = ""):
        """查看或开关不适宜内容过滤（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        current = self.cfg_bool("learning", "safety_filter_enabled", True)
        normalized = value.strip().lower()
        if not normalized:
            yield event.plain_result(
                f"不适宜内容过滤：{'开启' if current else '关闭'}\n"
                "用法：/sk safety on|off\n"
                "关闭只影响之后的分类；已 disabled 的用 /sk enableall 或 /sk safe <ID> on 恢复。"
            )
            return
        if normalized not in ("on", "off", "enable", "disable"):
            yield event.plain_result("用法：/sk safety on|off")
            return

        enabled = normalized in ("on", "enable")
        try:
            self._set_safety(enabled)
        except Exception as e:
            yield event.plain_result(f"保存配置失败：{e}")
            return

        if enabled:
            yield event.plain_result("不适宜内容过滤已开启。")
        else:
            yield event.plain_result(
                "不适宜内容过滤已关闭。之后分类不会再自动禁用表情包；"
                "已禁用的可以用 /sk enableall 恢复。"
            )

    @sk_group.command("enableall")
    async def sk_enableall(self, event: AstrMessageEvent):
        """恢复所有 disabled 表情包（管理员）"""
        if not self._is_admin(event):
            yield event.plain_result("权限拒绝：仅管理员可用。")
            return
        disabled = await self.store.list_all(STATUS_DISABLED)
        if not disabled:
            yield event.plain_result("当前没有 disabled 的表情包。")
            return
        for sticker in disabled:
            await self.store.set_safe(sticker.id, True)
            await self.store.set_status(
                sticker.id, STATUS_READY if sticker.tags else STATUS_PENDING
            )
        self._invalidate_cache()
        yield event.plain_result(
            f"已恢复 {len(disabled)} 张 disabled 表情包（包括手动停用的）。"
        )

    # ==================================================================
    # 导入/重分类任务
    # ==================================================================

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _notify(self, umo: str, text: str) -> None:
        try:
            await StarTools.send_message(umo, MessageChain([Plain(text)]))
        except Exception as e:
            logger.debug(f"[StickerSemantic] 通知发送失败: {e}")

    async def _import_job(
        self, ids: list[str], umo: str, *, total_scanned: int
    ) -> None:
        last_pct = -1

        async def _progress(done: int, total: int) -> None:
            nonlocal last_pct
            pct = int(done * 100 / max(1, total))
            if pct // 25 > last_pct // 25:
                last_pct = pct
                await self._notify(umo, f"[表情包] 分类进度 {done}/{total}")

        try:
            result = await self.classifier.classify_many(
                ids, umo, progress=_progress
            )
            await self._notify(
                umo,
                f"[表情包] 导入完成：扫描 {total_scanned}，新增 {len(ids)}，"
                f"分类成功 {result['success']}，失败 {result['failed']}。",
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[StickerSemantic] 导入任务失败: {e}")
            await self._notify(umo, f"[表情包] 导入任务失败：{e}")

    async def _reclassify_job(self, ids: list[str], umo: str) -> None:
        try:
            result = await self.classifier.classify_many(ids, umo)
            await self._notify(
                umo,
                f"[表情包] 重新分类完成：成功 {result['success']}，"
                f"失败 {result['failed']}。",
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[StickerSemantic] 重分类任务失败: {e}")
            await self._notify(umo, f"[表情包] 重新分类失败：{e}")

    # ==================================================================
    # 工具函数
    # ==================================================================

    def _raw_after(self, event: AstrMessageEvent, subcommand: str) -> str:
        text = str(event.message_str or "").strip()
        if not text:
            return ""
        match = re.search(
            rf"\bsk\s+{re.escape(subcommand)}\s+(.*)$", text, flags=re.I | re.S
        )
        if match:
            return match.group(1).strip()
        return ""

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    async def _sticker_from_reply(self, event: AstrMessageEvent) -> Sticker | None:
        """从当前消息引用的图片反查表情包（用于回复表情包后直接编辑）。"""
        for comp in event.get_messages():
            if not isinstance(comp, Reply):
                continue
            chain = getattr(comp, "chain", None) or []
            for inner in chain:
                if not isinstance(inner, Image):
                    continue
                path = await self._resolve_image_path(inner)
                if not path:
                    continue
                try:
                    sha = await asyncio.to_thread(self._file_sha256, path)
                except OSError:
                    continue
                sticker = await self.store.get_by_sha(sha)
                if sticker:
                    return sticker
        return None

    async def _resolve_sticker_target(
        self, event: AstrMessageEvent, raw: str
    ) -> tuple[Sticker | None, str]:
        """解析编辑目标：优先 16 位 ID；否则回退到当前引用的表情包。

        Returns:
            (sticker, 剩余参数)。找不到 sticker 时返回 (None, raw)。
        """
        text = str(raw or "").strip()
        if not text:
            return await self._sticker_from_reply(event), ""
        token = text.split(None, 1)[0]
        if re.fullmatch(r"[0-9a-fA-F]{16}", token):
            return await self.store.get(token.lower()), text[len(token) :].strip()
        return await self._sticker_from_reply(event), text

    def _strip_markers(self, chain: list) -> None:
        for comp in chain:
            if isinstance(comp, Plain) and comp.text:
                for marker in _STICKER_MARKERS:
                    comp.text = comp.text.replace(marker, "")
                comp.text = comp.text.strip()

    def _scan_images(self, root: Path) -> list[Path]:
        exts = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
        recursive = self.cfg_bool("general", "import_recursive", False)
        if root.is_file():
            return [root] if root.suffix.lower() in exts else []
        iterator = root.rglob("*") if recursive else root.glob("*")
        files = [
            p
            for p in iterator
            if p.is_file() and p.suffix.lower() in exts and not p.name.startswith(".")
        ]
        # 兼容 astrbot_plugin_self_evolution 的布局：stickers/files/
        if not files and (root / "files").is_dir():
            iterator = (root / "files").rglob("*") if recursive else (root / "files").glob("*")
            files = [
                p
                for p in iterator
                if p.is_file() and p.suffix.lower() in exts and not p.name.startswith(".")
            ]
        files.sort()
        return files
