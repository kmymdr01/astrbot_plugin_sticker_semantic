"""表情包打分与选择。纯函数，方便单元测试。"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .store import Sticker
from .taxonomy import (
    ALL_TAGS,
    INTENT_TAGS,
    Signals,
    keyword_hit_ratio,
    tag_polarity,
    text_similarity,
)

MODE_LIGHT = "light"
MODE_APPEND = "append"


@dataclass(slots=True)
class ScoredSticker:
    sticker: Sticker
    score: float
    reasons: list[str]


def _usage_fits(sticker: Sticker, mode: str) -> bool:
    if mode == MODE_LIGHT:
        return sticker.usage in ("reaction", "both")
    return sticker.usage in ("augment", "both")


def _tag_score(sticker: Sticker, ctx: Signals, reply: Signals) -> float:
    tags = set(sticker.tags)
    if not tags:
        return 0.0
    score = 0.0
    for tag in tags:
        if tag in reply.tags:
            score += 0.55
        if tag in ctx.tags:
            score += 0.35
    base = min(1.0, score / max(1.0, min(len(tags), 3)))

    # "react" 是兜底标签，不应压过具体情绪/意图。
    # 如果上下文有明确情绪，而这张图完全没有覆盖，就明显降权。
    target = (ctx.tags | reply.tags) & (ALL_TAGS - {"react"})
    if target:
        coverage = len(tags & target) / len(target)
        base *= 0.45 + 0.55 * min(1.0, coverage)
    return base


def _keyword_score(sticker: Sticker, ctx: Signals, reply: Signals) -> float:
    corpus = " ".join(
        [sticker.description, " ".join(sticker.keywords), " ".join(sticker.tags)]
    )
    if not corpus.strip():
        return 0.0
    sim_ctx = text_similarity(ctx.text, corpus)
    sim_reply = text_similarity(reply.text, corpus)
    hit_ctx = keyword_hit_ratio(ctx.text, sticker.keywords)
    hit_reply = keyword_hit_ratio(reply.text, sticker.keywords)
    return max(sim_ctx, sim_reply, hit_ctx * 0.9, hit_reply * 0.9)


def _intent_score(sticker: Sticker, ctx: Signals, reply: Signals) -> float:
    tags = (set(sticker.tags) & INTENT_TAGS) - {"react"}
    if not tags:
        return 0.0
    wanted = ((ctx.tags | reply.tags) & INTENT_TAGS) - {"react"}
    return 1.0 if tags & wanted else 0.0


def score_sticker(
    sticker: Sticker,
    ctx: Signals,
    reply: Signals,
    *,
    mode: str,
    recent_ids: set[str] | None = None,
    allow_usage_mismatch: bool = False,
) -> float:
    if not sticker.ready or not sticker.safe:
        return -1.0
    if not allow_usage_mismatch and not _usage_fits(sticker, mode):
        return -1.0

    recent_ids = recent_ids or set()
    tag = _tag_score(sticker, ctx, reply)
    keyword = _keyword_score(sticker, ctx, reply)
    intent = _intent_score(sticker, ctx, reply)
    intensity = 1.0 - abs((sticker.intensity / 5.0) - max(ctx.intensity, reply.intensity))

    sticker_pol = tag_polarity(sticker.tags)
    target_pol = reply.polarity * 0.65 + ctx.polarity * 0.35
    polarity = 1.0 - abs(sticker_pol - target_pol) / 2.0
    polarity_multiplier = 1.0
    if abs(target_pol) > 0.18 and abs(sticker_pol) > 0.18 and sticker_pol * target_pol < 0:
        polarity_multiplier = 0.28

    freshness = 0.15 if sticker.id in recent_ids else 1.0
    usage = 1.0 if sticker.usage in ("both", "reaction" if mode == MODE_LIGHT else "augment") else 0.7
    popularity = 1.0 / (1.0 + min(sticker.use_count, 50) * 0.02)

    score = (
        0.34 * tag
        + 0.24 * keyword
        + 0.14 * intent
        + 0.12 * polarity
        + 0.08 * intensity
        + 0.05 * freshness
        + 0.03 * usage
    ) * popularity * polarity_multiplier

    return max(-1.0, min(1.0, score))


def rank_stickers(
    stickers: list[Sticker],
    ctx: Signals,
    reply: Signals,
    *,
    mode: str = MODE_APPEND,
    recent_ids: set[str] | None = None,
    allow_usage_mismatch: bool = False,
) -> list[ScoredSticker]:
    ranked: list[ScoredSticker] = []
    for sticker in stickers:
        if not sticker.ready or not sticker.safe:
            continue
        if not allow_usage_mismatch and not _usage_fits(sticker, mode):
            continue
        score = score_sticker(
            sticker,
            ctx,
            reply,
            mode=mode,
            recent_ids=recent_ids,
            allow_usage_mismatch=allow_usage_mismatch,
        )
        if score < 0:
            continue
        reasons = []
        if _tag_score(sticker, ctx, reply) >= 0.3:
            reasons.append("标签")
        if _keyword_score(sticker, ctx, reply) >= 0.15:
            reasons.append("关键词")
        if _intent_score(sticker, ctx, reply) > 0:
            reasons.append("意图")
        ranked.append(ScoredSticker(sticker=sticker, score=score, reasons=reasons))
    ranked.sort(key=lambda item: item.score, reverse=True)
    return ranked


def choose_sticker(
    stickers: list[Sticker],
    ctx: Signals,
    reply: Signals,
    *,
    mode: str = MODE_APPEND,
    threshold: float = 0.55,
    recent_ids: set[str] | None = None,
    rng: random.Random | None = None,
    allow_usage_mismatch: bool = False,
) -> ScoredSticker | None:
    ranked = rank_stickers(
        stickers,
        ctx,
        reply,
        mode=mode,
        recent_ids=recent_ids,
        allow_usage_mismatch=allow_usage_mismatch,
    )
    eligible = [item for item in ranked if item.score >= threshold]
    if not eligible:
        return None

    top = eligible[:3]
    if len(top) == 1:
        return top[0]

    rng = rng or random.Random()
    weights = [max(0.01, item.score) ** 2 for item in top]
    return rng.choices(top, weights=weights, k=1)[0]


def best_sticker_score(
    stickers: list[Sticker],
    ctx: Signals,
    reply: Signals,
    *,
    mode: str = MODE_APPEND,
) -> float:
    ranked = rank_stickers(stickers, ctx, reply, mode=mode)
    return ranked[0].score if ranked else 0.0
