"""Deterministic capability excerpts, built once during catalog refresh.

No model, translated aliases, or per-skill rules. Execution instructions remain
in SKILL.md for SkillTool; retrieval uses a bounded excerpt of the description.
"""

import re

_SENTENCES = re.compile(r"(?<=[。！？!?])\s*|(?<=\.)\s+(?=[A-Z])|\n+")
_PROCEDURE = re.compile(
    r"交付总结|输出骨架|逐字照抄|第一行[‘\"「]|必须输出全部|产物摘要|"
    r"加载方式|加载成功前|禁止绕过|手写.*(?:代码|XML|CSV)|"
    r"(?:安装|install).*(?:依赖|dependencies)|"
    r"(?:输出|交付|格式|output|response)\s*(?:模板|骨架|template|format)",
    re.IGNORECASE,
)
_LOAD_SUFFIX = re.compile(
    r"[，,;；—\s]*(?:必须|务必|应当|请先|应先)\s*(?:第[一1]步|先)?\s*加载本技能.*$"
)


def capability_card(name: str, description: str, body: str = "", *, max_chars=600):
    """Keep introductory capabilities and explicit restrictions, not a summary.

    Extraction cannot infer capabilities missing from the original description.
    Short descriptions, including unsupported-task restrictions, remain intact.
    """
    source = description.strip() or body.strip()
    source = re.sub(r"```.*?```", " ", source, flags=re.DOTALL)
    source = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", source)
    source = source.replace("**", "").replace("`", "")
    source = re.sub(r"【[^】]{0,30}】", "", source)
    kept = []
    for sentence in _SENTENCES.split(source):
        sentence = " ".join(sentence.strip(" #\t").split())
        if _PROCEDURE.search(sentence) and kept:
            break
        if not sentence or sentence == name or _PROCEDURE.search(sentence):
            continue
        sentence = _LOAD_SUFFIX.sub("", sentence).strip()
        if sentence:
            kept.append(sentence)
        if len(kept) == 3 or sum(map(len, kept)) >= max_chars:
            break
    excerpt = " ".join(kept) or " ".join(source.split())[:max_chars]
    return f"{name}\n{excerpt[:max_chars]}"
