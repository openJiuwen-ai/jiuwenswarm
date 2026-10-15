"""model_routing.classifier — auto 档任务分类器（自包含）。

混合策略：规则引擎为主（chat / summarization 强项），TF-IDF 质心兜底
（reasoning / coding / format 强项）。

  1. 续写/确认指令（is_continuation_utterance）→ 短路返回哨兵，rail 跳过重路由
  2. summarization 专项关键词命中 → 直接 summarization
  3. 规则引擎有命中 → 用规则结果
  4. 规则无命中 → TF-IDF 质心余弦：reasoning/coding/format 需 >= _TFIDF_HIGH_CONF，
     chat 需 >= _TFIDF_CHAT_CONF，更低置信度一律兜底 chat

打分：score = SCORE_TABLE[(category, difficulty)]；缺项按难度兜底
（DEFAULT_SCORE_BY_DIFFICULTY），再缺兜底 DEFAULT_SCORE_FALLBACK。

规则 / 阈值 / score 表全部硬编码在本模块，TF-IDF 语料在同目录 classifier_corpus.py。
两个引擎在首次 classify 时惰性构建并缓存。
"""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from jiuwenswarm.common.utils import logger as _log

from .classifier_corpus import CATEGORY_UTTERANCES

# 默认分数：category 无法识别时按难度兜底；难度也无法识别时的最终兜底。
DEFAULT_SCORE_BY_DIFFICULTY: dict[str, int] = {"easy": 25, "medium": 40, "hard": 60}
DEFAULT_SCORE_FALLBACK: int = 60

# 续写/确认指令哨兵：classify 前先短路，返回哨兵让 rail 跳过重路由（保留当前模型）。
# 多轮对话里「继续/同意/好的」这类消息不能触发换模型。
CONTINUATION_CATEGORY = "continuation"
CONTINUATION_SCORE = -1

# 纯续写/确认短语（骨架形式：已去标点/空白/小写、去掉「请/please」）。
_CONTINUATION_PHRASES: frozenset[str] = frozenset(
    {
        # 中文续写动词（裸 + 常见短补语）
        "继续", "继续吧", "继续做", "继续写", "继续改", "继续优化", "继续加油", "继续往下",
        "接着", "接着来", "接着做", "接着写", "往下", "往下走",
        # 中文确认/同意
        "同意", "好的", "好", "行", "可以", "嗯", "对", "是", "是的", "没错", "就这样", "可以了",
        # 英文续写动词（skeleton：空格已去除）
        "continue", "goahead", "goon", "proceed", "keepgoing", "keepitup", "carryon", "pleasecontinue",
        # 英文确认/同意
        "ok", "okay", "yes", "yep", "yeah", "sure", "agreed", "agree", "right", "correct", "gotit",
    }
)

# 中文续写动词前缀：仅短形式（「继续 + ≤2 字补语」）算续写，
# 避免「继续优化这个函数」「往下看第三行的变量」这类带实义内容的长句被误判。
_CONTINUATION_PREFIXES_ZH: tuple[str, ...] = ("继续", "接着", "往下")
_CONTINUATION_PREFIX_MAX_LEN = 4

# 组合「确认词 + 续写动词」：好继续 / 对继续 / 好的请继续 …
_CONFIRMATION_LEADERS: frozenset[str] = frozenset(
    {"对", "好", "好的", "行", "可以", "嗯", "是", "是的", "没错", "同意",
     "ok", "okay", "yes", "yeah", "sure", "right", "correct", "gotit", "agreed"}
)
_CONTINUATION_ACTIONS: frozenset[str] = frozenset(
    {"继续", "接着", "往下", "continue", "goahead", "goon", "proceed"}
)

_STRIP_CHARS = " \t\r\n　。．！？!?.,，、;；:：…~～-—_'\"`()（）[]【】"


def _skeleton(text: str) -> str:
    """去掉空白与标点，返回小写纯字符骨架（用于精确匹配）。"""
    s = (text or "").strip().lower()
    return "".join(ch for ch in s if ch not in _STRIP_CHARS and not ch.isspace())


def _strip_polite_prefix(s: str) -> str:
    """去掉开头的「请 / please」敬语前缀（如「请继续」「please continue」）。"""
    while s.startswith("请"):
        s = s[1:]
    while s.startswith("please"):
        s = s[len("please"):]
    return s


def is_continuation_utterance(text: str) -> bool:
    """判断当前消息是否为纯续写/确认指令（去标点/空白后精确匹配）。

    匹配规则（骨架去掉「请/please」后）：
    1. 精确短语：继续 / 好的 / 同意 / ok / continue …
    2. 短中文续写前缀：继续X（X ≤ 2 字），如 继续做 / 继续优化 …
    3. 组合「确认词 + 续写动词」：好继续 / 对继续 / 好的请继续 …
    """
    s = _skeleton(text)
    if not s:
        return False
    s = _strip_polite_prefix(s)
    if not s:
        return False
    if s in _CONTINUATION_PHRASES:
        return True
    if len(s) <= _CONTINUATION_PREFIX_MAX_LEN and s.startswith(_CONTINUATION_PREFIXES_ZH):
        return True
    for lead in _CONFIRMATION_LEADERS:
        if s.startswith(lead) and _strip_polite_prefix(s[len(lead):]) in _CONTINUATION_ACTIONS:
            return True
    return False


# --------------------------------------------------------------------------- #
# 分类数据：categories / difficulties / score 表 / 规则 / 长度信号 / 默认值
# --------------------------------------------------------------------------- #

CATEGORIES: tuple[str, ...] = ("chat", "reasoning", "coding", "summarization", "format")

DIFFICULTIES: tuple[str, ...] = ("easy", "medium", "hard")

SCORE_TABLE: dict[tuple[str, str], int] = {
    ("chat", "easy"): 5,
    ("chat", "medium"): 20,
    ("chat", "hard"): 40,
    ("reasoning", "easy"): 20,
    ("reasoning", "medium"): 40,
    ("reasoning", "hard"): 60,
    ("coding", "easy"): 25,
    ("coding", "medium"): 45,
    ("coding", "hard"): 65,
    ("summarization", "easy"): 15,
    ("summarization", "medium"): 25,
    ("summarization", "hard"): 45,
    ("format", "easy"): 5,
    ("format", "medium"): 10,
    ("format", "hard"): 30,
}

RULES: list[dict[str, Any]] = [
    {
        "id": "coding.write_program",
        "pattern": (
            "(?i)\\b(write|implement|build|create|develop|design)\\s+(a|an|the|this)?\\s{0,3}(function|class|module|pro"
            "gram|script|algorithm|app|service|api|method|sort|sorting|search|searching)\\b"
        ),
        "targets": [
            {
                "type": "category",
                "name": "coding",
                "score": 3
            },
            {
                "type": "difficulty",
                "name": "medium",
                "score": 1
            }
        ],
        "strength": "strong"
    },
    {
        "id": "coding.debug_fix",
        "pattern": (
            "(?i)\\b(debug|fix|refactor|optimize|troubleshoot)\\s+(this|the|a)?\\s{0,3}(bug|error|issue|code|function|p"
            "roblem|crash|exception)\\b"
        ),
        "targets": [
            {
                "type": "category",
                "name": "coding",
                "score": 3
            },
            {
                "type": "difficulty",
                "name": "hard",
                "score": 2
            }
        ],
        "strength": "strong"
    },
    {
        "id": "coding.chinese",
        "pattern": "(?i)(代码|编程|写代码|调试|重构|实现|修复|报错|算法|排序|函数|变量)",
        "targets": [
            {
                "type": "category",
                "name": "coding",
                "score": 2
            }
        ],
        "strength": "medium"
    },
    {
        "id": "coding.bare_error",
        "pattern": "(?i)\\b(bug|error|exception|crash|syntax)\\b",
        "targets": [
            {
                "type": "category",
                "name": "coding",
                "score": 1
            }
        ],
        "strength": "weak"
    },
    {
        "id": "coding.bare_generic",
        "pattern": "(?i)\\b(code|program|script|compile|runtime)\\b",
        "targets": [
            {
                "type": "category",
                "name": "coding",
                "score": 1
            }
        ],
        "strength": "weak"
    },
    {
        "id": "reasoning.prove",
        "pattern": "(?i)\\b(prove|demonstrate|verify)\\s+that\\b",
        "targets": [
            {
                "type": "category",
                "name": "reasoning",
                "score": 3
            },
            {
                "type": "difficulty",
                "name": "hard",
                "score": 3
            }
        ],
        "strength": "strong"
    },
    {
        "id": "reasoning.analyze",
        "pattern": (
            "(?i)\\b(analyze|examine|investigate|evaluate)\\s+(the|this|a)?\\s{0,3}(problem|issue|data|pattern|trend|re"
            "lationship|situation)\\b"
        ),
        "targets": [
            {
                "type": "category",
                "name": "reasoning",
                "score": 3
            },
            {
                "type": "difficulty",
                "name": "hard",
                "score": 2
            }
        ],
        "strength": "strong"
    },
    {
        "id": "reasoning.compare",
        "pattern": "(?i)\\b(compare|contrast)\\s+(and|with|between)\\b",
        "targets": [
            {
                "type": "category",
                "name": "reasoning",
                "score": 2
            },
            {
                "type": "difficulty",
                "name": "hard",
                "score": 1
            }
        ],
        "strength": "medium"
    },
    {
        "id": "reasoning.chinese",
        "pattern": "(?i)(证明|推理|论证|分析|演绎|推导|比较|评估|逻辑)",
        "targets": [
            {
                "type": "category",
                "name": "reasoning",
                "score": 2
            }
        ],
        "strength": "medium"
    },
    {
        "id": "reasoning.bare",
        "pattern": "(?i)\\b(math|logic|reason|deduce)\\b",
        "targets": [
            {
                "type": "category",
                "name": "reasoning",
                "score": 1
            }
        ],
        "strength": "weak"
    },
    {
        "id": "reasoning.arithmetic",
        "pattern": "\\d+[\\+\\-\\*/\\^]\\d+",
        "targets": [
            {
                "type": "category",
                "name": "reasoning",
                "score": 1
            }
        ],
        "strength": "weak"
    },
    {
        "id": "summarize.action",
        "pattern": (
            "(?i)\\b(summarize|condense|compress|abridge)\\s+(this|the|a)?\\s{0,3}(article|text|document|report|paper|b"
            "ook|content|meeting)\\b"
        ),
        "targets": [
            {
                "type": "category",
                "name": "summarization",
                "score": 3
            }
        ],
        "strength": "strong"
    },
    {
        "id": "summarize.keyword",
        "pattern": "(?i)\\b(tldr|总结|摘要|概括|提炼)\\b",
        "targets": [
            {
                "type": "category",
                "name": "summarization",
                "score": 3
            }
        ],
        "strength": "strong"
    },
    {
        "id": "summarize.partial",
        "pattern": "(?i)\\b(key\\s+points|main\\s+ideas|要点|概述|overview)\\b",
        "targets": [
            {
                "type": "category",
                "name": "summarization",
                "score": 2
            }
        ],
        "strength": "medium"
    },
    {
        "id": "summarize.bare",
        "pattern": "(?i)\\b(brief|abstract)\\b",
        "targets": [
            {
                "type": "category",
                "name": "summarization",
                "score": 1
            }
        ],
        "strength": "weak"
    },
    {
        "id": "format.convert",
        "pattern": "(?i)\\b(convert|transform|translate)\\s+\\w+(\\s+\\w+){0,3}\\s+(to|into|as)\\s+\\w+",
        "targets": [
            {
                "type": "category",
                "name": "format",
                "score": 3
            }
        ],
        "strength": "strong"
    },
    {
        "id": "format.reformat",
        "pattern": "(?i)\\b(format|rewrite|rephrase)\\s+(this|the|data)?\\s{0,3}(as|to|into)\\b",
        "targets": [
            {
                "type": "category",
                "name": "format",
                "score": 3
            }
        ],
        "strength": "strong"
    },
    {
        "id": "format.chinese",
        "pattern": "(?i)(格式化|转换|翻译|改写|排版)",
        "targets": [
            {
                "type": "category",
                "name": "format",
                "score": 2
            }
        ],
        "strength": "medium"
    },
    {
        "id": "format.bare_format_name",
        "pattern": "(?i)\\b(json|csv|xml|yaml|markdown)\\b",
        "targets": [
            {
                "type": "category",
                "name": "format",
                "score": 1
            }
        ],
        "strength": "weak"
    },
    {
        "id": "chat.greeting",
        "pattern": "(?i)^(hello|hi|hey|你好|嗨|good\\s+(morning|afternoon|evening)|greetings|yo|sup)\\b",
        "targets": [
            {
                "type": "category",
                "name": "chat",
                "score": 3
            },
            {
                "type": "difficulty",
                "name": "easy",
                "score": 2
            }
        ],
        "strength": "strong"
    },
    {
        "id": "chat.factual",
        "pattern": "(?i)^(what|who|where|when|how)\\s+(is|are|was|were|does|do|can)\\b",
        "targets": [
            {
                "type": "category",
                "name": "chat",
                "score": 2
            },
            {
                "type": "difficulty",
                "name": "easy",
                "score": 2
            }
        ],
        "strength": "strong"
    },
    {
        "id": "chat.casual",
        "pattern": (
            "(?i)\\b(how\\s+are\\s+you|what'?s\\s+up|nice\\s+to\\s+meet|let'?s\\s+(chat|talk|hang)|just\\s+(chatting|ta"
            "lking|wondering)|casual\\s+(chat|conversation))\\b"
        ),
        "targets": [
            {
                "type": "category",
                "name": "chat",
                "score": 3
            },
            {
                "type": "difficulty",
                "name": "easy",
                "score": 1
            }
        ],
        "strength": "strong"
    },
    {
        "id": "chat.roleplay",
        "pattern": (
            "(?i)\\b(pretend|act\\s+as|roleplay|imagine\\s+(you|yourself)|embrace\\s+the\\s+role|assume\\s+the\\s+role)"
            "\\b"
        ),
        "targets": [
            {
                "type": "category",
                "name": "chat",
                "score": 3
            },
            {
                "type": "difficulty",
                "name": "medium",
                "score": 1
            }
        ],
        "strength": "strong"
    },
    {
        "id": "chat.opinion",
        "pattern": (
            "(?i)\\b(what\\s+(do|is)\\s+your\\s+(opinion|favorite|thought)|do\\s+you\\s+(prefer|like|think|believe)|rec"
            "ommend\\s+(a|me|some))\\b"
        ),
        "targets": [
            {
                "type": "category",
                "name": "chat",
                "score": 3
            },
            {
                "type": "difficulty",
                "name": "easy",
                "score": 1
            }
        ],
        "strength": "strong"
    },
    {
        "id": "chat.compose_letter",
        "pattern": (
            "(?i)\\b(draft|compose|write)\\s+(a|an)?\\s*(professional\\s+)?(email|letter|blog\\s+post|message)\\b"
        ),
        "targets": [
            {
                "type": "category",
                "name": "chat",
                "score": 2
            },
            {
                "type": "difficulty",
                "name": "medium",
                "score": 1
            }
        ],
        "strength": "strong"
    },
    {
        "id": "chat.chinese",
        "pattern": "(?i)(聊天|闲聊|什么是|介绍一下|随便聊聊|怎么样|聊聊|推荐|你觉|想聊|聊聊|心情|爱好|认识)",
        "targets": [
            {
                "type": "category",
                "name": "chat",
                "score": 2
            }
        ],
        "strength": "medium"
    },
    {
        "id": "chat.feeling",
        "pattern": (
            "(?i)\\b(I'?m\\s+(feeling|bored|happy|sad|tired|excited|looking\\s+for)|what\\s+should\\s+I\\s+do|tell\\s+m"
            "e\\s+a\\s+joke)\\b"
        ),
        "targets": [
            {
                "type": "category",
                "name": "chat",
                "score": 2
            },
            {
                "type": "difficulty",
                "name": "easy",
                "score": 1
            }
        ],
        "strength": "medium"
    },
    {
        "id": "chat.tell_me",
        "pattern": (
            "(?i)\\b(tell\\s+me\\s+about|help\\s+me\\s+(understand|find|choose)|can\\s+you\\s+(recommend|suggest|give\\"
            "s+me))\\b"
        ),
        "targets": [
            {
                "type": "category",
                "name": "chat",
                "score": 1
            }
        ],
        "strength": "weak"
    },
    {
        "id": "chat.bare",
        "pattern": "(?i)\\b(what\\s+about|how\\s+about|any\\s+(idea|suggestion|recommendation))\\b",
        "targets": [
            {
                "type": "category",
                "name": "chat",
                "score": 1
            }
        ],
        "strength": "weak"
    },
    {
        "id": "negate.write_creative",
        "pattern": "(?i)\\b(write|create|compose)\\s+a\\s+(poem|story|essay|letter|song|novel|小说|故事|诗歌|文章)\\b",
        "targets": [
            {
                "type": "category",
                "name": "coding",
                "score": -3
            }
        ],
        "strength": "strong"
    },
    {
        "id": "negate.realize_dream",
        "pattern": "(?i)(实现)(梦想|愿望|目标|理想)",
        "targets": [
            {
                "type": "category",
                "name": "coding",
                "score": -2
            }
        ],
        "strength": "medium"
    },
    {
        "id": "diff.easy",
        "pattern": "(?i)\\b(quick|simple|briefly|简单|定义)\\b",
        "targets": [
            {
                "type": "difficulty",
                "name": "easy",
                "score": 2
            }
        ],
        "strength": "medium"
    },
    {
        "id": "diff.medium",
        "pattern": "(?i)\\b(explain|describe|说明|详细|阐述)\\b",
        "targets": [
            {
                "type": "difficulty",
                "name": "medium",
                "score": 2
            }
        ],
        "strength": "medium"
    },
    {
        "id": "diff.hard",
        "pattern": "(?i)\\b(complex|complicated|advanced|复杂|架构)\\b",
        "targets": [
            {
                "type": "difficulty",
                "name": "hard",
                "score": 2
            }
        ],
        "strength": "medium"
    }
]

LENGTH_SIGNAL: dict[str, int] = {
    "easy_threshold": 20,
    "medium_threshold": 80,
    "adjustment_weight": 1
}

DEFAULTS: dict[str, str] = {
    "no_hit_category": "chat"
}


# --------------------------------------------------------------------------- #
# 分类逻辑
# --------------------------------------------------------------------------- #

# TF-IDF 置信度阈值：reasoning/coding/format 需高置信度才采纳，chat 中等即可。
_TFIDF_HIGH_CONF = 0.05
_TFIDF_CHAT_CONF = 0.03

# summarization 专项关键词（优先级最高，命中即判定，不看规则 / TF-IDF）。
_SUMM_PAT = re.compile(
    r"(?i)(summar|tldr|condense|compress|extract.*(key|main|point|idea)"
    r"|overview|takeaway|gist|abstract|brief.*(overview|summary|description)"
    r"|总结|摘要|概括|提炼|概述|要点)",
)

_WORD_PAT = re.compile(r"[a-zA-Z]{2,}")
_NUM_PAT = re.compile(r"\d+")
_CJK_PAT = re.compile(r"[一-鿿㐀-䶿]+")

_compiled_rules: list[tuple[re.Pattern, list[dict]]] | None = None
_tfidf_engine: tuple[dict[str, float], dict[str, dict[str, float]]] | None = None

_ASCII_WORD_START = r"(?<![A-Za-z0-9_])"
_ASCII_WORD_END = r"(?![A-Za-z0-9_])"


def _ascii_word_boundaries(pattern: str) -> str:
    r"""把规则里的 ``\b`` 换成 ASCII-only 词边界环视（修 CJK 关键词永不命中）。

    Python 的 ``\b`` 以 ``\w`` 定义，而 Unicode 模式下 ``\w`` **把 CJK 也算作词字符**，
    于是 ``\b复杂\b`` 在「很复杂的系统」里永不匹配（很/复 都是词字符，其间无边界）；
    「分析架构问题」「架构分析」同样不匹配 —— 中文提示词因此几乎拿不到 hard 档。

    换成「非 ``[A-Za-z0-9_]``」的环视后：CJK 关键词能正常命中，英文语义与 ``\b`` 等价
    （``\b`` 在英文里本就等价于「两侧不全为 ASCII 词字符」）。

    ``\b`` 出现在**组首**（其后是字面量或 ``(``）判为起始边界，出现在**组尾**
    （其后是 ``)`` 或串尾）判为结束边界；本模块规则全部形如 ``\b(...)\b`` / ``\b...\b``，
    该判据成立。``\b`` 出现在字符类 ``[...]`` 内（转义退格）的情形本模块不存在。
    """
    out: list[str] = []
    i, n = 0, len(pattern)
    while i < n:
        if pattern.startswith(r"\b", i):
            nxt = pattern[i + 2] if i + 2 < n else ""
            out.append(_ASCII_WORD_END if nxt in (")", "") else _ASCII_WORD_START)
            i += 2
            continue
        out.append(pattern[i])
        i += 1
    return "".join(out)


def _rules() -> list[tuple[re.Pattern, list[dict]]]:
    """编译并缓存规则引擎（首次调用时编译；单条规则编译失败只告警、不中断）。"""
    global _compiled_rules
    if _compiled_rules is None:
        compiled: list[tuple[re.Pattern, list[dict]]] = []
        for rule in RULES:
            try:
                compiled.append(
                    (re.compile(_ascii_word_boundaries(rule["pattern"])), rule.get("targets", []))
                )
            except re.error as exc:
                _log.warning("[ModelRouting] rule %s compile failed: %s", rule.get("id", "?"), exc)
        _compiled_rules = compiled
        _log.info("[ModelRouting] hybrid: rule engine compiled: %d rules", len(compiled))
    return _compiled_rules


def _tokenize(text: str) -> list[str]:
    """英文词（≥2 字母，小写）+ 数字 + 中文串（整串与单字都入袋）。"""
    tokens = [w.lower() for w in _WORD_PAT.findall(text)]
    tokens.extend(_NUM_PAT.findall(text))
    for seq in _CJK_PAT.findall(text):
        if len(seq) >= 2:
            tokens.append(seq)
        tokens.extend(seq)
    return tokens


def _tfidf() -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    """构建并缓存 (idf, 各类别质心)（首次调用时构建）。"""
    global _tfidf_engine
    if _tfidf_engine is None:
        all_docs: list[list[str]] = []
        cat_token_lists: dict[str, list[list[str]]] = {}
        for cat, utterances in CATEGORY_UTTERANCES.items():
            cat_token_lists[cat] = [_tokenize(u) for u in utterances]
            all_docs.extend(cat_token_lists[cat])
        n_docs = len(all_docs)
        df: Counter = Counter()
        for doc_tokens in all_docs:
            for token in set(doc_tokens):
                df[token] += 1
        idf = {t: math.log((n_docs + 1) / (count + 1)) for t, count in df.items()}
        centroids: dict[str, dict[str, float]] = {}
        for cat, token_lists in cat_token_lists.items():
            term_freq: Counter = Counter()
            for tokens in token_lists:
                term_freq.update(tokens)
            n = len(token_lists)
            centroids[cat] = {t: (term_freq[t] / n) * idf.get(t, 0) for t in term_freq}
        _tfidf_engine = (idf, centroids)
        _log.info(
            "[ModelRouting] hybrid: TF-IDF engine built: %d categories, %d IDF terms",
            len(centroids),
            len(idf),
        )
    return _tfidf_engine


def _cosine(v1: dict[str, float], v2: dict[str, float]) -> float:
    """稀疏向量余弦相似度（任一范数为 0 → 0.0）。"""
    shared = set(v1) & set(v2)
    if not shared:
        return 0.0
    dot = sum(v1[t] * v2[t] for t in shared)
    norm1 = math.sqrt(sum(v * v for v in v1.values()))
    norm2 = math.sqrt(sum(v * v for v in v2.values()))
    if norm1 == 0 or norm2 == 0:
        return 0.0
    return dot / (norm1 * norm2)


def _tfidf_classify(prompt_text: str) -> tuple[str, float]:
    """质心余弦打分 → (最高分类别, 该类别得分)。同分取质心插入序最前者。"""
    idf, centroids = _tfidf()
    query_tf = Counter(_tokenize(prompt_text))
    query_vec = {t: query_tf[t] * idf[t] for t in query_tf if t in idf}
    scores = {cat: _cosine(query_vec, centroid) for cat, centroid in centroids.items()}
    if not scores:
        return "chat", 0.0
    return max(scores, key=scores.get), max(scores.values())


def _count_tokens(prompt_text: str) -> int:
    """token 数（TiktokenCounter 不可用时按 4 字符 / token 粗估）。"""
    try:
        from openjiuwen.core.context_engine import TiktokenCounter

        return int(TiktokenCounter().count(prompt_text))
    except Exception:
        return max(1, len(prompt_text) // 4)


def _lookup_score(category: str, difficulty: str, score_table: dict, default_score: dict) -> int:
    """查 SCORE_TABLE → 难度默认分 → 最终兜底。"""
    value = (score_table or {}).get((category, difficulty))
    if value is not None:
        return value
    return (default_score or DEFAULT_SCORE_BY_DIFFICULTY).get(difficulty, DEFAULT_SCORE_FALLBACK)


def validate_score(raw: Any) -> int:
    """验证 classify 返回的 score：必须是 0-100 的数值；否则兜底 60。"""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SCORE_FALLBACK
    if value < 0 or value > 100:
        return DEFAULT_SCORE_FALLBACK
    return value


async def classify(prompt_text: str) -> tuple[int, str, str]:
    """给任务打分 → (score, category, difficulty)。

    续写/确认指令短路，返回 (CONTINUATION_SCORE, CONTINUATION_CATEGORY, "skip")，
    由 rail 判定为「保留当前模型」。
    """
    if is_continuation_utterance(prompt_text):
        _log.info("[ModelRouting] classifier continuation guard: prompt=%r", str(prompt_text)[:40])
        return CONTINUATION_SCORE, CONTINUATION_CATEGORY, "skip"

    category_scores: dict[str, int] = {c: 0 for c in CATEGORIES}
    difficulty_scores: dict[str, int] = {d: 0 for d in DIFFICULTIES}
    for pattern, targets in _rules():
        if not pattern.search(prompt_text):
            continue
        for target in targets:
            target_type = target.get("type", "")
            target_name = target.get("name", "")
            target_score = target.get("score", 0)
            if target_type == "category":
                category_scores[target_name] = category_scores.get(target_name, 0) + target_score
            elif target_type == "difficulty":
                difficulty_scores[target_name] = difficulty_scores.get(target_name, 0) + target_score

    token_count = _count_tokens(prompt_text)
    easy_threshold = LENGTH_SIGNAL.get("easy_threshold", 20)
    medium_threshold = LENGTH_SIGNAL.get("medium_threshold", 80)
    if token_count > medium_threshold:
        difficulty_scores["hard"] += 1
    elif token_count > easy_threshold:
        difficulty_scores["medium"] += 1

    rule_has_hit = any(v > 0 for v in category_scores.values())
    if rule_has_hit:
        rule_category = max(category_scores, key=category_scores.get)
    else:
        rule_category = DEFAULTS.get("no_hit_category", "chat")

    if all(v == 0 for v in difficulty_scores.values()):
        if token_count > medium_threshold:
            difficulty = "hard"
        elif token_count > easy_threshold:
            difficulty = "medium"
        else:
            difficulty = "easy"
    else:
        difficulty = max(difficulty_scores, key=difficulty_scores.get)

    if _SUMM_PAT.search(prompt_text):
        category = "summarization"
    elif rule_has_hit:
        category = rule_category
    else:
        tfidf_category, tfidf_conf = _tfidf_classify(prompt_text)
        if tfidf_category in ("reasoning", "coding", "format") and tfidf_conf >= _TFIDF_HIGH_CONF:
            category = tfidf_category
        elif tfidf_category == "chat" and tfidf_conf >= _TFIDF_CHAT_CONF:
            category = "chat"
        else:
            category = "chat"  # TF-IDF 低置信度 → 默认 chat

    score = _lookup_score(category, difficulty, SCORE_TABLE, DEFAULT_SCORE_BY_DIFFICULTY)
    _log.debug(
        "[ModelRouting] hybrid classifier: %s/%s score=%d (rule_hit=%s rule_cat=%s)",
        category,
        difficulty,
        score,
        rule_has_hit,
        rule_category,
    )
    return score, category, difficulty
