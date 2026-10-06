# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep a localized substitution on the object the user named.

The model may rewrite a whole field. The server aligns that text with the
original and keeps only the requested substitution. Every other difference is
rolled back, including a same-class object that carries an extra modifier.
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from copy import deepcopy
from difflib import SequenceMatcher
from typing import Any

logger = logging.getLogger(__name__)

_CN_CHANGE = re.compile(
    r"(?:把|将)(?P<old>.{1,80}?)(?:统一)?(?:改成|改为|换成|变成)(?P<new>.{1,40}?)(?:[，。,.;；]|$)"
)
_EN_CHANGE = re.compile(
    r"\b(?:change|recolor|turn)\s+(?P<old>.{1,80}?)\s+(?:in)?to\s+(?P<new>.{1,40}?)(?:[.,;]|$)",
    re.I,
)
_REWRITE = re.compile(r"重写|改写|改得|重新设计|整段|rewrite|restyle", re.I)
_ALL_SCOPE = re.compile(r"所有|全部|每一|全都|凡是|\ball\b|\bevery\b|\beach\b", re.I)
_BOUNDARY = set("的中里及与和、，。；：,.; \n\t把将")
_MEASURE = set("只个条辆张件台些双对")
_NUMBER = set("0123456789一二两三四五六七八九十几")
_IGNORE = _MEASURE | _NUMBER | set("的")
_STOP = frozenset({
    "the", "a", "an", "this", "that", "these", "those", "one", "its", "his", "her", "their", "same",
})
_CLAUSE = set("，。；、！？,.!?;\n")


class _Spec:
    def __init__(self, old_core: str, new_core: str, source: str, dest: str) -> None:
        self.old_core = old_core
        self.new_core = new_core
        self.source = source
        self.dest = dest
        self.allowed_remove: Counter[str] = Counter()
        self.allowed_add: Counter[str] = Counter()
        self.qualifiers: list[str] = []


def restore_graph_attribute_scope(
    before: dict[str, Any],
    after: dict[str, Any],
    message: str,
) -> dict[str, Any]:
    """Roll unrequested wording back before document sync and again before save."""
    spec = _spec_for(message, _graph_corpus(before))
    if spec is None:
        return after
    restored = deepcopy(after)
    if isinstance(restored.get("description"), str):
        restored["description"] = _finish(str(before.get("description") or ""), restored["description"], spec)
    before_nodes = {node.get("id"): node for node in before.get("nodes") or [] if isinstance(node, dict)}
    for node in restored.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        old = before_nodes.get(node.get("id")) or {}
        if isinstance(node.get("config"), dict):
            node["config"] = _align(old.get("config") or {}, node["config"], spec)
    if _graph_corpus(restored) == _graph_corpus(after):
        return after
    logger.info("Rolled back edits outside the named substitution")
    return restored


def restore_document_attribute_scope(
    before_graph: dict[str, Any],
    before_documents: dict[str, str],
    texts: dict[str, str],
    message: str,
) -> dict[str, str]:
    """Apply the same rollback to brief and storyboard text after the prose pass."""
    corpus = "\n".join([_graph_corpus(before_graph), *before_documents.values()])
    spec = _spec_for(message, corpus)
    if spec is None:
        return texts
    restored = {
        key: _finish(before_documents.get(key, ""), value, spec)
        for key, value in texts.items()
    }
    if restored == texts:
        return texts
    logger.info("Rolled back edits outside the named substitution")
    return restored


def _spec_for(message: str, corpus: str) -> _Spec | None:
    parsed = _parse(message)
    if parsed is None:
        return None
    cores = _cores(*parsed)
    if cores is None:
        return None
    old_core, new_core = cores
    source, dest, removed, added = _allowed_edit(old_core, new_core)
    if not source and not dest and not removed and not added:
        return None
    spec = _Spec(old_core, new_core, source, dest)
    spec.allowed_remove = removed
    spec.allowed_add = added
    spec.qualifiers = _learn(corpus, spec)
    return spec


def _parse(message: str) -> tuple[str, str] | None:
    text = str(message or "").strip()
    if not text or _REWRITE.search(text):
        return None
    match = _CN_CHANGE.search(text) or _EN_CHANGE.search(text)
    if match is None or _ALL_SCOPE.search(match.group("old")):
        return None
    old, new = match.group("old").strip(), match.group("new").strip()
    if not old or not new:
        return None
    return old, new


def _cores(old_span: str, new_span: str) -> tuple[str, str] | None:
    if _mostly_ascii(old_span) or _mostly_ascii(new_span):
        return _english_cores(old_span, new_span)
    shared = _longest_common(old_span, new_span)
    if len(shared) >= 2:
        return _expand(old_span, shared), _expand(new_span, shared)
    old_core, new_core = _last_cjk(old_span), _last_cjk(new_span)
    if not old_core or not new_core:
        return None
    return old_core, new_core


def _english_cores(old_span: str, new_span: str) -> tuple[str, str] | None:
    old_words = [word for word in re.findall(r"[A-Za-z]+", old_span) if word.lower() not in _STOP]
    new_words = [word for word in re.findall(r"[A-Za-z]+", new_span) if word.lower() not in _STOP]
    if not old_words or not new_words:
        return None
    shared = {word.lower() for word in old_words} & {word.lower() for word in new_words}
    if shared:
        return " ".join(old_words), " ".join(new_words)
    noun = old_words[-1]
    new_core = " ".join(new_words) if noun.lower() in {word.lower() for word in new_words} else " ".join([*new_words, noun])
    return " ".join(old_words), new_core


def _allowed_edit(old_core: str, new_core: str) -> tuple[str, str, Counter[str], Counter[str]]:
    removed: Counter[str] = Counter()
    added: Counter[str] = Counter()
    source = dest = ""
    best: tuple[str, str] | None = None
    for tag, i1, i2, j1, j2 in _char_opcodes(old_core, new_core):
        if tag == "equal":
            continue
        old_piece, new_piece = old_core[i1:i2], new_core[j1:j2]
        removed.update(old_piece)
        added.update(new_piece)
        if old_piece and new_piece and (best is None or len(old_piece) + len(new_piece) < len(best[0]) + len(best[1])):
            best = (old_piece, new_piece)
    if best is not None:
        source, dest = best
    return source, dest, removed, added


def _learn(corpus: str, spec: _Spec) -> list[str]:
    if not spec.source:
        return []
    found: list[str] = []
    start = 0
    while True:
        at = corpus.find(spec.source, start)
        if at < 0:
            break
        start = at + max(1, len(spec.source))
        if not _right_is_noun(corpus, at + len(spec.source), spec):
            continue
        qualifier = _attached_token(corpus, at, spec.old_core)
        if qualifier and qualifier not in found and not _used_as_verb(corpus, qualifier, spec):
            found.append(qualifier)
    return found


def _finish(before: str, after: str, spec: _Spec) -> str:
    if before == after:
        return after
    if not before:
        return _sweep(after, spec)
    text, spans = _reconcile(before, after, spec)
    return _sweep(text, spec, spans)


def _reconcile(before: str, after: str, spec: _Spec) -> tuple[str, list[tuple[int, int]]]:
    """Return the aligned text and the ranges that still come from the model."""
    parts: list[str] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    for tag, i1, i2, j1, j2 in _char_opcodes(before, after):
        changed = False
        if tag == "equal":
            piece = before[i1:i2]
        elif tag == "insert":
            inserted = after[j1:j2]
            if _keep_insert(before, i1, inserted, spec):
                piece = inserted
                changed = True
            else:
                piece = ""
        elif _keep_change(before, i1, i2, after, j1, j2, spec):
            piece = after[j1:j2]
            changed = True
        else:
            piece = before[i1:i2]
        if changed and piece:
            spans.append((cursor, cursor + len(piece)))
        parts.append(piece)
        cursor += len(piece)
    return "".join(parts), spans


def _keep_change(before: str, i1: int, i2: int, after: str, j1: int, j2: int, spec: _Spec) -> bool:
    removed, added = before[i1:i2], after[j1:j2]
    if _slice_is_foreign(removed, spec) or _foreign_left(before, i1, spec):
        return False
    net_remove, net_add = _net(removed, added)
    if not net_remove and not net_add:
        return True
    return _subset(net_remove, spec.allowed_remove) and _subset(net_add, spec.allowed_add)


def _keep_insert(before: str, at: int, inserted: str, spec: _Spec) -> bool:
    if inserted[:1] in _CLAUSE:
        return True
    return not _foreign_left(before, at, spec)


def _sweep(text: str, spec: _Spec, spans: list[tuple[int, int]] | None = None) -> str:
    """Roll a modifier plus the new wording back to the old wording.

    ``spans`` are the ranges the model actually changed. A modifier that was
    already next to the new wording in the original text is left alone.
    """
    if not spec.qualifiers or not spec.source or not spec.dest:
        return text
    if spans is not None and not spans:
        return text
    replacements: list[tuple[int, int, str]] = []
    for qualifier in spec.qualifiers:
        if spec.source.isascii():
            pattern = re.compile(
                rf"\b{re.escape(qualifier)}\s+{re.escape(spec.dest)}\b",
                re.I,
            )
        else:
            pattern = re.compile(rf"{re.escape(qualifier)}(的?){re.escape(spec.dest)}")
        for match in pattern.finditer(text):
            dest_at = match.end() - len(spec.dest)
            if spans is not None and not _overlaps(dest_at, match.end(), spans):
                continue
            if spec.source.isascii():
                piece = f"{qualifier} {spec.source}"
            else:
                piece = qualifier + match.group(1) + spec.source
            replacements.append((match.start(), match.end(), piece))
    occupied = len(text) + 1
    for start, end, piece in sorted(replacements, reverse=True):
        if end > occupied:
            continue
        text = text[:start] + piece + text[end:]
        occupied = start
    return text


def _align(before: Any, after: Any, spec: _Spec) -> Any:
    if isinstance(after, str):
        return _finish(before, after, spec) if isinstance(before, str) else _sweep(after, spec)
    if isinstance(after, dict):
        old = before if isinstance(before, dict) else {}
        return {key: _align(old.get(key), value, spec) if key in old else _sweep_value(value, spec) for key, value in after.items()}
    if isinstance(after, list):
        return _align_list(before if isinstance(before, list) else [], after, spec)
    return after


def _align_list(before: list[Any], after: list[Any], spec: _Spec) -> list[Any]:
    if all(isinstance(item, str) for item in [*before, *after]):
        return _align_string_list([str(item) for item in before], [str(item) for item in after], spec)
    aligned = [
        _align(before[index], item, spec) if index < len(before) else _sweep_value(item, spec)
        for index, item in enumerate(after)
    ]
    aligned.extend(before[len(after):])
    return aligned


def _align_string_list(before: list[str], after: list[str], spec: _Spec) -> list[str]:
    aligned: list[str] = []
    for tag, i1, i2, j1, j2 in SequenceMatcher(a=before, b=after, autojunk=False).get_opcodes():
        if tag == "equal":
            aligned.extend(after[j1:j2])
        elif tag == "replace":
            width = min(i2 - i1, j2 - j1)
            aligned.extend(_finish(before[i1 + offset], after[j1 + offset], spec) for offset in range(width))
            aligned.extend(before[i1 + width:i2])
            aligned.extend(_sweep(item, spec) for item in after[j1 + width:j2])
        elif tag == "insert":
            aligned.extend(_sweep(item, spec) for item in after[j1:j2])
        else:
            aligned.extend(before[i1:i2])
    return aligned


def _sweep_value(value: Any, spec: _Spec) -> Any:
    if isinstance(value, str):
        return _sweep(value, spec)
    if isinstance(value, dict):
        return {key: _sweep_value(item, spec) for key, item in value.items()}
    if isinstance(value, list):
        return [_sweep_value(item, spec) for item in value]
    return value


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and span_start < end for span_start, span_end in spans)


def _foreign_left(text: str, index: int, spec: _Spec) -> bool:
    token = _attached_token(text, index, spec.old_core)
    return bool(token) and token in spec.qualifiers


def _attached_token(text: str, index: int, old_core: str) -> str:
    cursor = index
    while cursor > 0 and text[cursor - 1].isspace():
        cursor -= 1
    if cursor > 0 and text[cursor - 1] == "的":
        cursor -= 1
        while cursor > 0 and text[cursor - 1].isspace():
            cursor -= 1
    if cursor <= 0 or text[cursor - 1] in _IGNORE:
        return ""
    if text[cursor - 1].isascii() and text[cursor - 1].isalpha():
        word = _ascii_word_before(text, cursor)
        if word.lower() in _STOP or word.lower() in old_core.lower():
            return ""
        return word
    token = re.split(r"[与和及跟、]", _cjk_token_before(text, cursor))[-1].strip("的")
    token = token[-2:]
    while token and token[-1] in old_core:
        token = token[:-1]
    if len(token) < 2 or token in old_core:
        return ""
    return token


def _used_as_verb(corpus: str, token: str, spec: _Spec) -> bool:
    """A word that also takes the named object after a measure is a verb, not a second object."""
    if spec.source.isascii():
        return re.search(
            rf"\b{re.escape(token)}\s+(?:a|an|the|one|several|some)\s+{re.escape(spec.source)}\b",
            corpus,
            re.I,
        ) is not None
    numbers = "".join(sorted(_NUMBER))
    measures = "".join(sorted(_MEASURE))
    return re.search(
        re.escape(token) + rf"(?:[\u4e00-\u9fff的]{{0,2}})?(?:[{numbers}]+)?[{measures}]" + re.escape(spec.source),
        corpus,
    ) is not None


def _slice_is_foreign(piece: str, spec: _Spec) -> bool:
    return any(qualifier in piece for qualifier in spec.qualifiers)


def _right_is_noun(text: str, pos: int, spec: _Spec) -> bool:
    if spec.source not in spec.old_core:
        return False
    tail = spec.old_core.split(spec.source, 1)[1]
    if tail and text.startswith(tail, pos):
        return True
    if spec.source.isascii():
        match = re.match(r"\s*([A-Za-z]+)", text[pos:])
        noun = spec.old_core.split()[-1]
        return bool(match and match.group(1).lower().rstrip("s") == noun.lower().rstrip("s"))
    if pos < len(text) and (text[pos] in tail or text[pos] == spec.old_core[-1]):
        return True
    return False


def _char_opcodes(before: str, after: str):
    """Align ASCII words as one unit so a word is not split on a shared letter."""
    before_spans = _pieces(before)
    after_spans = _pieces(after)
    before_tokens = [before[start:end] for start, end in before_spans]
    after_tokens = [after[start:end] for start, end in after_spans]
    for tag, i1, i2, j1, j2 in SequenceMatcher(a=before_tokens, b=after_tokens, autojunk=False).get_opcodes():
        yield tag, *_bounds(before_spans, len(before), i1, i2), *_bounds(after_spans, len(after), j1, j2)


def _pieces(text: str) -> list[tuple[int, int]]:
    spans: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isascii() and char.isalpha():
            end = index + 1
            while end < len(text) and text[end].isascii() and text[end].isalpha():
                end += 1
            spans.append((index, end))
            index = end
        else:
            spans.append((index, index + 1))
            index += 1
    return spans


def _bounds(spans: list[tuple[int, int]], length: int, start: int, end: int) -> tuple[int, int]:
    if start == end:
        pos = spans[start][0] if start < len(spans) else length
        return pos, pos
    return spans[start][0], spans[end - 1][1]


def _expand(span: str, shared: str) -> str:
    start = span.rfind(shared)
    if start < 0:
        return shared
    end = start + len(shared)
    while start > 0 and span[start - 1] not in _BOUNDARY and span[start - 1] not in _IGNORE and _is_cjk(span[start - 1]):
        start -= 1
    while end < len(span) and span[end] not in _BOUNDARY and _is_cjk(span[end]):
        end += 1
    return span[start:end]


def _longest_common(left: str, right: str) -> str:
    if not left or not right:
        return ""
    best = ""
    previous = [0] * (len(right) + 1)
    for i, left_char in enumerate(left, 1):
        current = [0]
        for j, right_char in enumerate(right, 1):
            if left_char == right_char:
                length = previous[j - 1] + 1
                current.append(length)
                if length > len(best):
                    best = left[i - length:i]
            else:
                current.append(0)
        previous = current
    return best


def _last_cjk(text: str) -> str:
    chunks = re.findall(r"[\u4e00-\u9fff]+", text)
    return chunks[-1] if chunks else ""


def _mostly_ascii(text: str) -> bool:
    letters = [char for char in text if char.isalpha()]
    if not letters:
        return False
    return sum(char.isascii() for char in letters) * 2 >= len(letters)


def _cjk_token_before(text: str, index: int) -> str:
    start = index
    while start > 0 and _is_cjk(text[start - 1]) and index - start < 6:
        start -= 1
    return text[start:index]


def _ascii_word_before(text: str, index: int) -> str:
    start = index
    while start > 0 and text[start - 1].isalpha():
        start -= 1
    return text[start:index]


def _net(removed: str, added: str) -> tuple[Counter[str], Counter[str]]:
    old, new = Counter(removed), Counter(added)
    net_remove, net_add = Counter(), Counter()
    for char in set(old) | set(new):
        delta = old[char] - new[char]
        if delta > 0:
            net_remove[char] = delta
        elif delta < 0:
            net_add[char] = -delta
    return net_remove, net_add


def _subset(part: Counter[str], whole: Counter[str]) -> bool:
    return all(whole[key] >= count for key, count in part.items())


def _graph_corpus(graph: dict[str, Any]) -> str:
    parts = [str(graph.get("description") or "")]
    for node in graph.get("nodes") or []:
        if isinstance(node, dict):
            parts.append(json.dumps(node.get("config") or {}, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts)


def _is_cjk(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"
