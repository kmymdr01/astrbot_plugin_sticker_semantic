"""轻回复判定：一条回复是否适合只发一张表情包。"""

from __future__ import annotations

import re

from .taxonomy import looks_factual

STICKER_MARKERS: tuple[str, ...] = (
    "[[STICKER]]",
    "[STICKER]",
    "[[sticker]]",
    "[sticker]",
    "【表情包】",
    "[表情包]",
    "（表情包）",
    "(表情包)",
)

LIGHT_PHRASES: frozenset[str] = frozenset(
    {
        "哈", "哈哈", "哈哈哈", "哈哈哈哈", "笑死", "笑死我了", "绷不住",
        "嗯", "嗯嗯", "哦", "哦哦", "噢", "噢噢", "诶", "诶嘿", "嘿",
        "好", "好耶", "好滴", "好哒", "行", "行吧", "可以", "可",
        "对", "对的", "是", "是的", "确实", "有道理", "没错",
        "谢谢", "感谢", "收到", "了解", "明白", "懂了",
        "晚安", "早安", "早上好", "晚上好", "在", "在吗",
        "？", "?", "！", "!", "……", "...", "。。。", "~", "～",
        "啊", "啊这", "这", "就这", "离谱", "草", "6", "牛", "赞",
    }
)

_EMOJI_RE = re.compile(r"[\U0001F000-\U0001FAFF\u2600-\u26FF\u2700-\u27BF]")
_PUNCT_ONLY_RE = re.compile(r"^[\s\W_]+$", re.UNICODE)
_LIGHT_CHAIN_RE = re.compile(
    r"^(?:哈+|嘿+|呵+|嘻+|嗯+|哦+|噢+|诶+|唉+|好|行|对|是|可以|可|确实|"
    r"收到|了解|明白|懂了|谢谢|感谢|晚安|早安|早上好|晚上好|在|不|别|"
    r"[?？!！~～…。，,.、\s])+$"
)
_LIGHT_START_RE = re.compile(
    r"^(哈+|嘿+|呵+|嘻+|嗯+|哦+|噢+|诶+|唉+|好耶|好滴|好哒|行|对|是|"
    r"可以|确实|收到|谢谢|感谢|晚安|早安|在吗|在不在)"
)


def strip_sticker_markers(text: str) -> tuple[str, bool]:
    """去掉模型可能输出的表情包占位符，返回 (清理后文本, 是否出现占位符)。"""
    raw = str(text or "")
    found = False
    for marker in STICKER_MARKERS:
        if marker in raw:
            raw = raw.replace(marker, "")
            found = True
    return raw.strip(), found


def _clean(text: str) -> str:
    raw = str(text or "").strip()
    raw = re.sub(r"^[\s>*#\-]+", "", raw)
    raw = raw.strip(" \t\r\n\"'“”‘’")
    return raw


def is_light_reply(text: str, max_chars: int = 12) -> bool:
    """判断回复是否属于“一句表情包就够了”的轻回复。

    宁可保守：问句、含数字/链接、较长文本都不会被判为轻回复。
    """
    raw = _clean(text)
    if not raw:
        return True

    body, had_marker = strip_sticker_markers(raw)
    if had_marker and not body:
        return True
    if not body:
        return True

    if looks_factual(body):
        return False

    # 单独一个问号可以当反应；但“吃了吗？”这类短问句不能直接换成表情包
    if re.search(r"[?？]", body) and len(body) > 1 and not _PUNCT_ONLY_RE.match(body):
        return False

    if len(body) > max(1, int(max_chars)):
        return False

    if body in LIGHT_PHRASES:
        return True

    if _PUNCT_ONLY_RE.match(body):
        return True

    if _LIGHT_CHAIN_RE.match(body):
        return True

    # “哈哈好”“嗯嗯嗯”这类短组合
    if _LIGHT_START_RE.match(body) and len(body) <= max(3, max_chars // 2):
        return True

    # 纯表情/表情+少量语气词
    emoji_count = len(_EMOJI_RE.findall(body))
    if emoji_count >= 1 and len(body) <= 6:
        return True

    return False


def reply_allows_sticker_only(text: str, max_chars: int = 12) -> bool:
    """轻回复 + 不携带需要保留的事实信息。"""
    body, _ = strip_sticker_markers(text)
    if not is_light_reply(body, max_chars=max_chars):
        return False
    return not looks_factual(body)
