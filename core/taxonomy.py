"""表情包语义标签与轻量文本信号提取。

这里刻意不依赖任何 LLM 或分词库：
- 分类阶段用视觉模型产出标签；
- 发送阶段用本文件的标签/关键词/字符 bigram 做快速匹配。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable

# ---------------------------------------------------------------------------
# 标签表
# ---------------------------------------------------------------------------

TAG_LABELS: dict[str, str] = {
    # 情绪
    "happy": "开心",
    "laugh": "大笑",
    "sad": "难过",
    "cry": "哭",
    "angry": "生气",
    "surprised": "惊讶",
    "confused": "疑惑",
    "shy": "害羞",
    "love": "喜欢",
    "calm": "平静",
    "tired": "疲惫",
    "disgust": "嫌弃",
    "fear": "害怕",
    "excited": "兴奋",
    "awkward": "尴尬",
    "smug": "得意",
    # 场景 / 意图
    "greet": "打招呼",
    "morning": "早安",
    "goodnight": "晚安",
    "agree": "赞同",
    "disagree": "反对",
    "praise": "夸奖",
    "comfort": "安慰",
    "tease": "调侃",
    "apologize": "道歉",
    "thank": "感谢",
    "celebrate": "庆祝",
    "encourage": "鼓励",
    "ask": "提问",
    "refuse": "拒绝",
    "wait": "等待",
    "react": "单纯反应",
}

ALL_TAGS: frozenset[str] = frozenset(TAG_LABELS)

TAG_ALIASES: dict[str, str] = {
    "开心": "happy",
    "高兴": "happy",
    "快乐": "happy",
    "笑": "laugh",
    "大笑": "laugh",
    "笑哭": "laugh",
    "难过": "sad",
    "伤心": "sad",
    "哭": "cry",
    "流泪": "cry",
    "生气": "angry",
    "愤怒": "angry",
    "怒": "angry",
    "惊讶": "surprised",
    "震惊": "surprised",
    "吃惊": "surprised",
    "疑惑": "confused",
    "问号": "confused",
    "无语": "awkward",
    "尴尬": "awkward",
    "害羞": "shy",
    "脸红": "shy",
    "喜欢": "love",
    "爱心": "love",
    "爱": "love",
    "平静": "calm",
    "淡定": "calm",
    "疲惫": "tired",
    "累": "tired",
    "困": "tired",
    "嫌弃": "disgust",
    "恶心": "disgust",
    "害怕": "fear",
    "恐惧": "fear",
    "兴奋": "excited",
    "激动": "excited",
    "得意": "smug",
    "坏笑": "smug",
    "打招呼": "greet",
    "你好": "greet",
    "早安": "morning",
    "早上好": "morning",
    "晚安": "goodnight",
    "赞同": "agree",
    "同意": "agree",
    "反对": "disagree",
    "拒绝": "refuse",
    "夸奖": "praise",
    "夸": "praise",
    "赞": "praise",
    "安慰": "comfort",
    "抱抱": "comfort",
    "调侃": "tease",
    "逗": "tease",
    "嘲笑": "tease",
    "道歉": "apologize",
    "对不起": "apologize",
    "感谢": "thank",
    "谢谢": "thank",
    "庆祝": "celebrate",
    "恭喜": "celebrate",
    "鼓励": "encourage",
    "加油": "encourage",
    "提问": "ask",
    "问": "ask",
    "等待": "wait",
    "等": "wait",
    "反应": "react",
}

TAG_POLARITY: dict[str, float] = {
    "happy": 1.0,
    "laugh": 1.0,
    "praise": 1.0,
    "agree": 0.8,
    "comfort": 0.7,
    "love": 1.0,
    "excited": 1.0,
    "celebrate": 1.0,
    "thank": 0.9,
    "encourage": 0.9,
    "shy": 0.6,
    "smug": 0.4,
    "calm": 0.2,
    "sad": -0.9,
    "cry": -1.0,
    "angry": -1.0,
    "disgust": -0.9,
    "fear": -0.8,
    "awkward": -0.5,
    "tired": -0.4,
    "confused": -0.2,
    "disagree": -0.6,
    "apologize": -0.3,
    "refuse": -0.3,
    "greet": 0.2,
    "morning": 0.2,
    "goodnight": 0.2,
    "ask": 0.0,
    "wait": 0.0,
    "react": 0.0,
}

INTENT_TAGS: frozenset[str] = frozenset(
    {
        "greet",
        "morning",
        "goodnight",
        "agree",
        "disagree",
        "praise",
        "comfort",
        "tease",
        "apologize",
        "thank",
        "celebrate",
        "encourage",
        "ask",
        "refuse",
        "wait",
        "react",
    }
)

# 关键词按“命中即加分”设计，允许一个词出现在多个标签下。
TAG_KEYWORDS: dict[str, tuple[str, ...]] = {
    "happy": (
        "开心", "高兴", "快乐", "好耶", "太好了", "真棒", "舒服", "美滋滋",
        "心情好", "不错", "满意", "幸福", "爽", "嘻嘻", "嘿嘿",
    ),
    "laugh": (
        "哈哈", "笑死", "笑不活", "绷不住", "乐死", "草", "hhh", "233",
        "太好笑", "搞笑", "喜剧", "笑出声",
    ),
    "sad": (
        "难过", "伤心", "失落", "不开心", "郁闷", "低落", "心塞", "委屈",
        "唉", "emo", "破防",
    ),
    "cry": ("哭", "泪", "呜呜", "呜", "抽泣", "眼泪", "大哭", "泪目"),
    "angry": (
        "生气", "愤怒", "气死", "火大", "恼火", "滚", "闭嘴", "烦死",
        "可恶", "混蛋", "气人",
    ),
    "surprised": (
        "惊讶", "震惊", "卧槽", "我靠", "天哪", "居然", "没想到", "啊这",
        "真的假的", "离谱", "逆天",
    ),
    "confused": (
        "疑惑", "困惑", "问号", "为什么", "什么", "怎么", "不理解", "看不懂",
        "啥", "嗯？", "啊？",
    ),
    "shy": ("害羞", "脸红", "不好意思", "羞", "腼腆", "别夸", "讨厌啦"),
    "love": (
        "喜欢", "爱你", "爱你哟", "贴贴", "抱抱", "亲亲", "心动", "可爱",
        "好可爱", "想你", "么么",
    ),
    "calm": ("平静", "淡定", "冷静", "没事", "还好", "佛系", "随缘", "慢慢来"),
    "tired": (
        "累", "困", "疲惫", "没力气", "摆烂", "躺平", "不想动", "睡了",
        "熬夜", "上班", "下班", "好累",
    ),
    "disgust": ("嫌弃", "恶心", "呕", "无语", "离谱", "什么鬼", "辣眼睛", "吐了"),
    "fear": ("害怕", "恐惧", "吓人", "好怕", "不敢", "恐怖", "瑟瑟发抖"),
    "excited": ("激动", "兴奋", "冲", "开冲", "好耶", "期待", "迫不及待"),
    "awkward": ("尴尬", "无语", "社死", "冷场", "空气安静", "这……", "沉默"),
    "smug": ("得意", "坏笑", "呵呵", "就这", "拿捏", "小样", "计划通"),
    "greet": ("你好", "嗨", "hello", "hi", "在吗", "在不在", "早上好", "晚上好"),
    "morning": ("早安", "早上好", "起床", "早", "新的一天"),
    "goodnight": ("晚安", "睡了", "睡觉", "好梦", "明天见", "先睡了"),
    "agree": ("赞同", "同意", "确实", "对的", "是啊", "没错", "有道理", "可以", "行"),
    "disagree": ("反对", "不同意", "不对", "错了", "不是", "不行", "拒绝", "胡说"),
    "praise": ("厉害", "牛", "牛逼", "强", "优秀", "棒", "赞", "好评", "太强了", "高手"),
    "comfort": ("安慰", "抱抱", "别难过", "没事", "会好的", "摸摸", "别哭", "心疼"),
    "tease": ("调侃", "逗", "嘲笑", "就这", "菜", "小丑", "乐", "阴阳", "嘲讽"),
    "apologize": ("道歉", "对不起", "抱歉", "不好意思", "我的错", "怪我"),
    "thank": ("谢谢", "感谢", "多谢", "谢了", "3q", "thanks", "thx"),
    "celebrate": ("庆祝", "恭喜", "生日快乐", "干杯", "撒花", "赢了", "成功", "毕业"),
    "encourage": ("鼓励", "加油", "你可以", "别放弃", "冲鸭", "支持", "坚持"),
    "ask": ("请问", "问一下", "为什么", "怎么", "如何", "是不是", "能不能", "要不要", "吗", "呢"),
    "refuse": ("拒绝", "不要", "不行", "不可以", "免了", "算了", "下次吧"),
    "wait": ("等等", "等一下", "稍等", "等我", "马上", "在等"),
    "react": ("啊", "哦", "嗯", "噢", "诶", "哈", "草", "？", "！"),
}

_EMOJI_RE = re.compile(
    r"[\U0001F000-\U0001FAFF\u2600-\u26FF\u2700-\u27BF]"
)
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_QUESTION_RE = re.compile(r"[?？]|吗|呢|怎么|为什么|什么|是不是|能不能|要不要|如何")
_GREETING_RE = re.compile(r"^(你好|嗨|hi|hello|在吗|在不在|早|早安|早上好|晚上好|晚安)", re.I)
_STOP_CHARS = set("的了是在我你他她它们这那有和就都也还又很吧啊呀哦嗯哈嘿呵嘛呢吗哦噢诶")
_STOP_WORDS = {
    "什么", "怎么", "为什么", "如何", "是不是", "能不能", "要不要", "一个",
    "这个", "那个", "就是", "可以", "不是", "没有", "我们", "你们", "他们",
    "自己", "现在", "已经", "还是", "但是", "因为", "所以", "如果", "虽然",
    "而且", "或者", "不过", "然后", "知道", "觉得", "时候", "问题", "东西",
}


@dataclass(slots=True)
class Signals:
    text: str = ""
    tags: set[str] = field(default_factory=set)
    tag_scores: dict[str, float] = field(default_factory=dict)
    polarity: float = 0.0
    intensity: float = 0.35
    is_question: bool = False
    is_greeting: bool = False

    @property
    def is_positive(self) -> bool:
        return self.polarity > 0.15

    @property
    def is_negative(self) -> bool:
        return self.polarity < -0.15


def normalize_tag(value: str) -> str | None:
    raw = str(value or "").strip().lower()
    if not raw:
        return None
    if raw in ALL_TAGS:
        return raw
    if raw in TAG_ALIASES:
        return TAG_ALIASES[raw]
    # 允许 "happy/laugh" 这类模型输出
    for part in re.split(r"[/,，、|;；\s]+", raw):
        if part in ALL_TAGS:
            return part
        if part in TAG_ALIASES:
            return TAG_ALIASES[part]
    return None


def normalize_tags(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values or []:
        tag = normalize_tag(value)
        if tag and tag not in result:
            result.append(tag)
    return result


def describe_tags(tags: Iterable[str]) -> str:
    return "、".join(TAG_LABELS.get(t, t) for t in tags if t)


def tag_polarity(tags: Iterable[str]) -> float:
    vals = [TAG_POLARITY.get(t, 0.0) for t in tags]
    vals = [v for v in vals if abs(v) > 0.01]
    if not vals:
        return 0.0
    return max(-1.0, min(1.0, sum(vals) / len(vals)))


def _count_emoji(text: str) -> int:
    return len(_EMOJI_RE.findall(text or ""))


def extract_signals(text: str) -> Signals:
    """从一段文本中提取标签、极性、强度、问句/问候信号。"""
    raw = str(text or "")
    compact = _URL_RE.sub(" ", raw).strip()
    lower = compact.lower()
    signals = Signals(text=compact)

    scores: dict[str, float] = {}
    for tag, keywords in TAG_KEYWORDS.items():
        hits = 0
        for kw in keywords:
            if kw and kw in lower:
                hits += 1
        if hits:
            scores[tag] = min(1.0, 0.62 + 0.16 * (hits - 1))

    # 标点/表情补充信号
    if "?" in compact or "？" in compact:
        scores["confused"] = max(scores.get("confused", 0.0), 0.45)
        scores["ask"] = max(scores.get("ask", 0.0), 0.35)
    if "!" in compact or "！" in compact:
        scores["surprised"] = max(scores.get("surprised", 0.0), 0.35)
    if _count_emoji(compact) >= 2:
        scores["happy"] = max(scores.get("happy", 0.0), 0.35)

    signals.tags = set(scores)
    signals.tag_scores = scores

    weighted = 0.0
    total = 0.0
    for tag, score in scores.items():
        weighted += TAG_POLARITY.get(tag, 0.0) * score
        total += score
    signals.polarity = max(-1.0, min(1.0, weighted / total)) if total else 0.0

    intensity = 0.32
    intensity += min(0.32, 0.07 * len(scores))
    intensity += min(0.16, 0.04 * (compact.count("!") + compact.count("！")))
    intensity += min(0.16, 0.03 * _count_emoji(compact))
    signals.intensity = max(0.1, min(1.0, intensity))

    signals.is_question = bool(_QUESTION_RE.search(compact))
    signals.is_greeting = bool(_GREETING_RE.search(compact))
    return signals


def _bigrams(text: str) -> set[str]:
    cleaned = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]+", " ", text or "").lower()
    chars = "".join(ch for ch in cleaned if not ch.isspace())
    grams: set[str] = set()
    for i in range(len(chars) - 1):
        gram = chars[i : i + 2]
        if gram[0] in _STOP_CHARS and gram[1] in _STOP_CHARS:
            continue
        grams.add(gram)
    for word in re.findall(r"[a-z0-9]{2,}", cleaned):
        grams.add(word)
    return grams


def text_similarity(a: str, b: str) -> float:
    """字符 bigram + 英文词的 Jaccard 相似度，0~1。"""
    ga, gb = _bigrams(a), _bigrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / max(1, len(ga | gb))


def keyword_hit_ratio(text: str, keywords: Iterable[str]) -> float:
    text = str(text or "").lower()
    keys = [str(k).lower() for k in (keywords or []) if k]
    if not text or not keys:
        return 0.0
    hits = sum(1 for k in keys if k in text)
    return min(1.0, hits / max(1, min(len(keys), 4)))


def looks_factual(text: str) -> bool:
    """粗略判断一段回复是否在提供事实/操作信息，避免用表情包替换掉有效信息。"""
    raw = str(text or "").strip()
    if not raw:
        return False
    if _URL_RE.search(raw):
        return True
    if re.search(r"\d{2,}", raw):
        return True
    if len(raw) >= 40:
        return True
    if re.search(r"(步骤|方法|代码|命令|链接|地址|价格|时间|几点|怎么弄|如何|第一|第二)", raw):
        return True
    return False


def is_question_text(text: str) -> bool:
    return bool(_QUESTION_RE.search(str(text or "")))
