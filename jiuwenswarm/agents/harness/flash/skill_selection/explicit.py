"""Exact, request-scoped Skill selection without retrieval or model inference."""

from dataclasses import dataclass
from pathlib import Path
import re
from types import SimpleNamespace

from .catalog import directory_id
from .query import application_envelope


@dataclass(frozen=True)
class ExplicitRequest:
    source: str
    names: tuple[str, ...]
    error: str | None = None


# Only affirmative directives at a clause boundary count as a name request.
# A mention, quoted example, negation, or discussion about a Skill is not one.
_DIRECTIVE = re.compile(
    r'(?:^|[\n。！？!?;；，,])\s*'
    r'(?:(?:请你|请|帮我|请帮我|我想要|我想|我要|我需要|我希望|麻烦你|这次|本次|现在)\s*)*'
    r'(?:使用|调用|加载|用|(?:(?:please\s+)?(?:i\s+(?:want|would like)\s+to\s+)?)'
    r'(?:use|invoke|load)\s+)\s*'
    r'(?:the\s+)?(?:(?:名为|名称为)\s*)?(?:(?P<skill_prefix>skill\s+|技能\s*)(?:名为\s*)?[:：]?\s*)?'
    r'(?P<name>`[^`\n]+`|"[^"\n]+"|\x27[^\x27\n]+\x27|“[^”\n]+”|「[^」\n]+」|'
    r'[a-z0-9_][a-z0-9_.-]*|[\u4e00-\u9fff]+)', re.I)
_UNCERTAIN = re.compile(r'是否|要不要|如果|假如|举例|例如|\b(?:if|whether|example)\b', re.I)


def explicit_task_text(text):
    """Exclude marked source material only when detecting explicit Skill names.

    This does not produce retrieval queries or alter the model's user message.
    """
    text = re.sub(r'(?:#{1,6}\s*)?(?:输入素材|原始材料|参考资料|任务内容|待处理内容|原文|输入内容|正文)'
                  r'\s*[:：]?\s*(?=`{3,}|~{3,})', '', text)
    text = re.sub(r'(`{3,}|~{3,})[\s\S]*?(?:\1|$)', ' ', text)
    lines = re.split(r'\n|(?<!#)(?=#{1,6}\s)', text)
    result, material = [], False
    for line in lines:
        heading = re.sub(r'^\s*#{1,6}\s*', '', line).strip()
        if re.match(r'^(?:输入素材|原始材料|参考资料|任务内容|待处理内容|原文|输入内容|正文)'
                    r'(?:\s*[:：]|\s*$)', heading):
            material = True
        elif re.match(r'^(?:任务要求|输出要求|交付要求|补充要求|需求|任务)(?:\s*[:：]|\s*$)', heading):
            material = False
            result.append(line)
        elif not material:
            result.append(line)
    return '\n'.join(result).strip()


def explicit_request(content, query, known_names=()) -> ExplicitRequest | None:
    blocks = content if isinstance(content, list) else [{"text": content}]
    requests = []
    for block in blocks:
        envelope = application_envelope(block.get("text")) if isinstance(block, dict) else None
        if envelope and "skills_to_use" in envelope:
            value = envelope["skills_to_use"]
            if not isinstance(value, list):
                return ExplicitRequest("frontend", (), "invalid_names")
            if any(not isinstance(n, str) or not n.strip() or len(n) > 128 for n in value):
                return ExplicitRequest("frontend", (), "invalid_names")
            requests.extend(n.strip() for n in value)
    if requests:
        names = tuple(dict.fromkeys(n.casefold() for n in requests))
        return ExplicitRequest("frontend", names, "too_many_names" if len(names) > 20 else None)

    names = []
    known = {n.casefold() for n in known_names}
    query = re.sub(r'```[^\n]*\n.*?(?:```|$)|~~~[^\n]*\n.*?(?:~~~|$)', '', query, flags=re.S)
    for match in _DIRECTIVE.finditer(query):
        # Questions and conditional/quoted instructions need normal reasoning.
        clause = re.split(r'[\n。！？!?;；，,]', query[match.end():], maxsplit=1)[0]
        if _UNCERTAIN.search(query[:match.start()] + clause) or re.search(r'吗|么|\?', clause):
            continue
        raw = match['name']
        name = raw.strip('`"\x27“”「」').strip()
        skill_suffix = raw == name and name.endswith('技能')
        if name.casefold() not in known and skill_suffix:
            name = name[:-2].removesuffix('这个').removesuffix('的')
        if re.match(r'\s*(?:格式|文件|format\b|file\b)', clause, re.I):
            continue
        marked = (bool(match['skill_prefix']) or skill_suffix
                  or bool(re.match(r'\s*(?:的|这个)?(?:技能|skills?\b)', clause, re.I)))
        # Punctuation and quoting also describe libraries, filenames and titles;
        # only an installed name or an adjacent Skill label establishes intent.
        if name.casefold() in known or marked:
            names.append(name.casefold())
            # A choice/list is not a uniquely specified textual name.
            remainder = re.sub(r'^\s*(?:的|这个)?(?:技能|skills?)\s*', '', clause, flags=re.I)
            if re.match(r'\s*(?:和|或|或者|以及|、|\band\b|\bor\b)', remainder, re.I):
                return ExplicitRequest("text", tuple(names), "ambiguous_names")
            tail = query[match.end():]
            following = re.match(r'\s*[,，]\s*([a-z0-9_][a-z0-9_.-]*)', tail, re.I)
            if following and following[1].casefold() in known:
                return ExplicitRequest("text", tuple(names), "ambiguous_names")
    unique = tuple(dict.fromkeys(names))
    if not unique:
        return None
    return ExplicitRequest("text", unique, "ambiguous_names" if len(unique) != 1 else None)


def resolve_names(request, native):
    try:
        return _resolve_names(request, native)
    except (OSError, ValueError, RuntimeError):
        return (), 'catalog_unavailable'


def _resolve_names(request, native):
    """Validate against the live permitted catalog; never turn input into a path."""
    if request.error:
        return (), request.error
    if native is None:
        return (), "catalog_unavailable"
    roots = tuple(Path(p).resolve() for p in native.normalize_skill_dirs(native.skills_dir))
    enabled = set(getattr(native, 'enabled_skills', ()) or ())
    disabled = set(getattr(native, 'disabled_skills', ()) or ())
    selected = []
    for name in request.names:
        matches = [s for s in native.skills if s.name.casefold() == name.casefold()]
        if len(matches) != 1:
            return (), "ambiguous_names" if matches else "missing_or_not_allowed"
        skill = matches[0]
        path = Path(skill.directory)
        if path.name in disabled or skill.name in disabled:
            return (), "missing_or_not_allowed"
        if enabled and path.name not in enabled:
            return (), "missing_or_not_allowed"
        target = (path / 'SKILL.md').resolve()
        if not any(target.is_relative_to(root) for root in roots) or not target.is_file():
            return (), "missing_or_not_allowed"
        selected.append(SimpleNamespace(id=directory_id(path), name=skill.name, directory=str(path)))
    return tuple(selected), None
