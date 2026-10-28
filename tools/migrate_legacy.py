#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 astrbot_plugin_self_evolution 的表情包迁移到本插件并分类。

用法（在插件根目录执行）：

    python tools/migrate_legacy.py --model <vision-model-id>
    ... --limit 2          # 先试两张
    ... --reclassify-all   # 已分类的也重做

脚本只读取旧插件目录，复制到本插件自己的 stickers/，不会修改旧插件。
默认从 AstrBot 配置的 anthropic provider source 读取 API Key 和 Base URL。
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anthropic import Anthropic  # noqa: E402
from PIL import Image as PILImage  # noqa: E402

from core.store import STATUS_DISABLED, STATUS_PENDING, STATUS_READY, StickerStore  # noqa: E402
from core.taxonomy import TAG_LABELS, normalize_tags  # noqa: E402

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}

TAG_PROMPT = "、".join(f"{k}({v})" for k, v in TAG_LABELS.items())

CLASSIFY_PROMPT = f"""看这张聊天表情包，判断它在聊天中表达什么。

要求：
1. description：画面内容 + 情绪，30 字以内；图上有字就转写关键文字。
2. tags：1~4 个标签，只能从下面列表里选：{TAG_PROMPT}。
3. keywords：2~6 个适合这张图的聊天短语或口头禅，比如“哈哈哈”“笑死”“贴贴”。
4. intensity：1~5，1 很平淡，5 非常强烈。
5. usage：reaction（只适合单独发）、augment（适合跟在文字后）、both。
6. safe：false 表示色情、血腥、违法违规或明显不适合群聊。

只输出 JSON：
{{"description":"...","tags":["happy","laugh"],"keywords":["哈哈哈","笑死"],"intensity":3,"usage":"both","safe":true}}"""


def find_default_paths() -> tuple[Path, Path, Path]:
    astrbot_data = Path.home() / ".astrbot" / "data"
    legacy = (
        astrbot_data
        / "plugin_data"
        / "astrbot_plugin_self_evolution"
        / "self_evolution"
        / "stickers"
    )
    target = astrbot_data / "plugin_data" / "astrbot_plugin_sticker_semantic"
    config = astrbot_data / "cmd_config.json"
    return legacy, target, config


def load_anthropic(config_path: Path) -> tuple[Anthropic, str]:
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    source = next(
        (s for s in config.get("provider_sources", []) if s.get("id") == "anthropic"),
        None,
    )
    if not source:
        raise SystemExit("AstrBot 配置里没有 anthropic provider source")
    key = source.get("key", "")
    if isinstance(key, list):
        key = key[0] if key else ""
    if not key:
        raise SystemExit("anthropic provider source 没有 key")
    base = str(source.get("api_base", "")).rstrip("/")
    base = base.removesuffix("/v1")
    return Anthropic(api_key=str(key), base_url=base, timeout=120.0), base


def image_to_jpeg(path: Path, max_side: int = 1024) -> bytes:
    with PILImage.open(path) as im:
        im.seek(0)
        if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
            background = PILImage.new("RGB", im.size, (255, 255, 255))
            rgba = im.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            im = background
        else:
            im = im.convert("RGB")
        im.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=90)
        return buf.getvalue()


def parse_json(text: str) -> dict | None:
    raw = str(text or "").strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    raw = re.sub(r"\s*```$", "", raw)
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(raw[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


def as_bool(value: object, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "是", "safe")
    return default


def normalize_usage(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw in ("reaction", "react", "only", "单独", "只发", "表情"):
        return "reaction"
    if raw in ("augment", "append", "with_text", "附加", "跟文字"):
        return "augment"
    return "both"


def clean_keywords(value: object) -> list[str]:
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


def classify_one(
    client: Anthropic,
    model: str,
    image_path: Path,
) -> dict | None:
    try:
        data = base64.b64encode(image_to_jpeg(image_path)).decode()
    except Exception as exc:  # noqa: BLE001
        print(f"    image decode failed: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
        return None
    last_error: Exception | None = None
    for attempt in range(4):
        try:
            response = client.messages.create(
                model=model,
                max_tokens=1200,
                thinking={"type": "disabled"},
                system="你是聊天表情包分类器。只输出一个 JSON 对象，不输出 Markdown 或解释。",
                messages=[
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": data,
                                },
                            },
                            {"type": "text", "text": CLASSIFY_PROMPT},
                        ],
                    }
                ],
            )
            text = "".join(
                getattr(block, "text", "")
                for block in response.content
                if getattr(block, "type", "") == "text"
            )
            parsed = parse_json(text)
            if parsed:
                return parsed
            last_error = ValueError("model returned no parseable JSON")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            status = getattr(exc, "status_code", 0)
            if status == 429 or status >= 500:
                wait = 8 * (attempt + 1)
                print(f"    rate/5xx, wait {wait}s: {type(exc).__name__}", flush=True)
                time.sleep(wait)
                continue
            print(f"    request failed: {type(exc).__name__}: {str(exc)[:160]}", flush=True)
            time.sleep(2)
        time.sleep(1.0)
    if last_error:
        print(f"    give up: {type(last_error).__name__}: {str(last_error)[:160]}", flush=True)
    return None


def collect_images(root: Path) -> list[Path]:
    if not root.exists():
        return []
    files = [
        p
        for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS and not p.name.startswith(".")
    ]
    files.sort()
    return files


async def run(args: argparse.Namespace) -> int:
    legacy, target, config_path = find_default_paths()
    legacy = Path(args.legacy_dir).expanduser() if args.legacy_dir else legacy
    target = Path(args.data_dir).expanduser() if args.data_dir else target
    config_path = Path(args.config).expanduser() if args.config else config_path
    if not str(args.model or "").strip():
        print("missing --model <vision-model-id>")
        return 1

    files = collect_images(legacy)
    if not files:
        print(f"no images under {legacy}")
        return 1
    if args.limit:
        files = files[: args.limit]
    print(f"legacy={legacy}")
    print(f"target={target}")
    print(f"images={len(files)}")

    store = StickerStore(target)
    await store.init()

    imported: list[str] = []
    for path in files:
        sticker, created = await store.add_file(
            path,
            source="self_evolution_import",
            original_name=path.name,
            confirmed=True,
        )
        if sticker and created:
            imported.append(sticker.id)
    print(f"newly imported={len(imported)} total={await store.count()}")

    client, base = load_anthropic(config_path)
    print(f"provider={base} model={args.model}")

    pending = await store.list_all(STATUS_PENDING)
    if args.reclassify_all:
        candidates = await store.list_all()
    else:
        candidates = pending
        imported_set = set(imported)
        # 已存在但尚未分类的也纳入；新导入的必然在 pending 中
        candidates = [s for s in candidates if s.id in imported_set or s.status == STATUS_PENDING]
    if args.limit:
        candidates = candidates[: args.limit]

    print(f"to classify={len(candidates)}")
    success = 0
    failed = 0
    for index, sticker in enumerate(candidates, start=1):
        print(
            f"[{index}/{len(candidates)}] {sticker.id} {sticker.filename}",
            flush=True,
        )
        result = classify_one(client, args.model, Path(sticker.path))
        if not result:
            failed += 1
            continue
        tags = normalize_tags(result.get("tags", [])) or ["react"]
        safe = as_bool(result.get("safe"), True)
        description = str(result.get("description", "") or "").strip().replace("\n", " ")[:300]
        if not description:
            description = sticker.filename
        try:
            intensity = max(1, min(5, int(result.get("intensity", 3))))
        except (TypeError, ValueError):
            intensity = 3
        await store.update_classification(
            sticker.id,
            description=description,
            tags=tags,
            keywords=clean_keywords(result.get("keywords")),
            intensity=intensity,
            usage=normalize_usage(result.get("usage")),
            safe=safe,
            status=STATUS_READY if safe else STATUS_DISABLED,
        )
        success += 1
        time.sleep(0.6)

    stats = await store.stats()
    print(
        json.dumps(
            {
                "success": success,
                "failed": failed,
                "total": stats["total"],
                "status": stats["status"],
                "top_tags": list(stats["tags"].items())[:10],
            },
            ensure_ascii=True,
        )
    )
    await store.close()
    return 0 if failed == 0 else 2


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--legacy-dir", default="")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--config", default="")
    parser.add_argument("--model", default="")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--reclassify-all", action="store_true")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
