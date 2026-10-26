"""Static, unverified capability metadata for the main LLM; never a selection gate."""
from dataclasses import dataclass
import re
from .execution import checkpoint

_FORMAT_QUANTITY = r"(?=[0-9０-９]+\s*(?:页|张|份|pages?\b|slides?\b|sheets?\b))"

_EXTENSIONS = (
    'pptx ppt potx docx doc odt xlsx xls xlsm csv tsv pdf '
    'png jpg jpeg svg webp html md txt json mp3 wav mp4'
).split()
_TOKEN = re.compile(
    r'(?:\.(?P<extension>' + '|'.join(_EXTENSIONS) + r')'
    r'|(?<![a-z0-9_-])(?:(?P<bare>' + '|'.join(_EXTENSIONS) + r')|(?P<app>powerpoint|word|excel|markdown)))'
    r'(?:(?![a-z0-9_-])|' + _FORMAT_QUANTITY + r')', re.I)
_ALIASES = {'powerpoint': ('pptx', 'ppt'), 'word': ('docx', 'doc'),
            'excel': ('xlsx', 'xls', 'xlsm'), 'markdown': ('md',), 'ppt': ('pptx', 'ppt')}
_ACTIONS = {
    'create': r'创建|生成|制作|新建|起草|编写|撰写|导出|输出|交付|保存为|另存为|整理成|做成|转换为|转换成|转为|转成|'
              r'\b(?:creat(?:e|es|ing|ion)|generat(?:e|es|ing|ion)|make|build|write|draft|'
              r'export(?:ing)?|deliver(?:ing)?|output|produce|save(?:d)? as)\b',
    'edit': r'(?<!可)(?<!可以)编辑|修改|更新|重写|改写|替换|纠正|修正|润色|增删|精简|缩减|删减|扩写|'
            r'\b(?:edit(?:ing)?|modify|modifying|update|revise|rewrite|correct|fix)\b',
    'read': r'读取|阅读|提取|抽取|解析|总结|摘要|分析|'
            r'\b(?:read(?:ing)?|pars(?:e|ing)|extract(?:ing)?|summari[sz](?:e|ing)|inspect)\b',
    'format': r'排版|美化|格式调整|字体|版式|重排|\b(?:restyle|format(?:ting)?|fonts?|spacing|layouts?)\b',
}
_ACTION = re.compile('|'.join('(?P<' + k + '>' + v + ')' for k, v in _ACTIONS.items()), re.I)
_NEGATIVE = re.compile(r'不支持|不能|不用于|不适用|不要|禁止(?:以|用)?|不得|不提供|'
                       r'\b(?:not for|no support|does not support|not support|cannot|can.t|do not|don.t|never)\b', re.I)
_CAPABILITY_NEGATIVE = re.compile(r'不支持|不能|不用于|不适用|不提供|'
                                 r'\b(?:not for|no support|does not support|not support|cannot|can.t)\b', re.I)
_INPUT = re.compile(r'输入|来源|源文件|\b(?:input|source|from)\b', re.I)
_OUTPUT = re.compile(r'输出|产物|交付|导出|\b(?:outputs?|artifacts?|deliverables?|exports?)\b', re.I)
_OUTPUT_LABEL = re.compile(r'^\s*[#*\-\s]*(?:(?:所有|最终|唯一|本技能的|the)\s*)?'
    r'(?:输出|产物|交付文件|导出格式|outputs?|artifacts?|deliverables?|export formats?)'
    r'(?:\s*[:：]|.{0,12}(?:为|是|格式|\bare\b|\binclude\b))', re.I)
_CONVERT = re.compile(r'\b(?:convert|turn|export|save)\b.*?\b(?:to|into|as)\b|'
                      r'(?:转换|导出|保存|另存|转)(?:成|为)', re.I)
_SPLIT = re.compile(r'[。！？!?;；\n]|\.(?=\s|$)')
_MACRO_WRITE = re.compile(r'(?:编写|生成|创建|制作|\b(?:write|create|generate|build)\b)'
                          r'.{0,80}?(?:宏|\b(?:macros?|vba)\b)', re.I)
_MACRO_RUN = re.compile(r'(?:运行|执行|\b(?:run|execute)\b)\s*(?:VBA\s*)?(?:宏|macros?|VBA)', re.I)


@dataclass(frozen=True)
class Requirement:
    operation: str
    formats: tuple[str, ...]  # Alternatives, not a requirement to produce all.


@dataclass(frozen=True)
class ArtifactContract:
    supported: frozenset[tuple[str, str]] = frozenset()
    denied: frozenset[tuple[str, str]] = frozenset()
    outputs: frozenset[str] = frozenset()
    closed_outputs: bool = False
    level: str = 'declared'
    version: str = ''


def _formats(text, *, declarations=False):
    for m in _TOKEN.finditer(text):
        token = m.group('extension') or m.group('bare') or m.group('app')
        token = token.lower()
        # In a Skill description bare "PPT" may mean an HTML/image presentation.
        # Require an extension, PPTX or PowerPoint to assert an actual Office file.
        if declarations and m.group('bare') and token == 'ppt':
            continue
        values = (token,) if m.group('extension') else _ALIASES.get(token, (token,))
        yield m, values


def _clean(text):
    # Code samples, links and Skill-name strings cannot manufacture capabilities.
    text = re.sub(r'```[\s\S]*?```', '', text)
    text = re.sub(r'\[([^\]]+)\]\([^)]*\)', r'\1', text)
    return text.replace('`', '').replace('**', '').replace('__', '')


def _bindings(clause, *, declarations=False):
    """Bind operations to their object, not to every file type in a sentence."""
    actions = list(_ACTION.finditer(clause))
    formats = list(_formats(clause, declarations=declarations))
    previous_operations = ()
    for i, (mention, values) in enumerate(formats):
        previous_end = formats[i - 1][0].end() if i else 0
        before = []
        for action in actions:
            if previous_end <= action.start() < mention.start() and mention.start() - action.end() <= 110:
                before.append(action)
        operations = ()
        if before:
            # A comma/conjunction list of verbs can share an object; an intervening
            # object cannot. e.g. "create/edit PPTX", not "create images from PPTX".
            nearest = before[-1]
            between = clause[nearest.end():mention.start()]
            if _INPUT.search(between) and nearest.lastgroup == 'create':
                previous_operations = ()
                continue
            if re.search(r'图片|图像|images?|PNG|PDF', between, re.I) and not any(f in ('png', 'pdf') for f in values):
                previous_operations = ()
                continue
            chosen = [nearest]
            for action in reversed(before[:-1]):
                gap = clause[action.end():chosen[0].start()]
                if re.fullmatch(r'[\s,/、或与和及]*(?:(?:and|or)\s*)?', gap, re.I):
                    chosen.insert(0, action)
                else:
                    break
            operations = tuple(action.lastgroup for action in chosen)
        elif i and re.fullmatch(r'[\s,/、或与和及]*(?:(?:and|or)\s*)?', clause[previous_end:mention.start()], re.I):
            # "Create PPTX / POTX": the second object shares the same action.
            operations = previous_operations
        elif not actions[:1] or all(a.start() > mention.end() for a in actions):
            after = [a for a in actions if 0 <= a.start() - mention.end() <= 45]
            # "DOCX: editing" / "将 DOCX 中的文字修改...".
            operations = tuple(action.lastgroup for action in after
                               if not any(mention.end() < other.start() < action.end() for other, _ in formats))
        for operation in operations:
            yield Requirement(operation, values)
        previous_operations = operations


def declared_contract(description, sources=()):
    supported, denied, outputs = set(), set(), set()
    closed = False
    closed_outputs = None
    # Sources are separate: a format in an unrelated reference never supplies
    # an object for an action in the description or another reference.
    for raw in (description, *sources):
        checkpoint()
        text = _clean(raw)
        clauses = []
        for sentence in _SPLIT.split(text):
            checkpoint()
            # "Generate DOCX, cannot read DOC" has two different assertions.
            boundaries = [m.start() for m in _CAPABILITY_NEGATIVE.finditer(sentence) if m.start()]
            starts = [0, *boundaries, len(sentence)]
            clauses.extend(sentence[a:b].strip(' ,，') for a, b in zip(starts, starts[1:]))
        for clause in clauses:
            checkpoint()
            # Workflow prohibitions ("do not bypass this Skill using openpyxl")
            # are not statements that the Skill lacks workbook creation.
            if _NEGATIVE.search(clause) and not _CAPABILITY_NEGATIVE.search(clause):
                continue
            bindings = tuple(_bindings(clause, declarations=True))
            negative = bool(_CAPABILITY_NEGATIVE.search(clause))
            target = denied if negative else supported
            for bound in bindings:
                target.update((bound.operation, f) for f in bound.formats)
                if bound.operation == 'create' and not negative:
                    outputs.update(bound.formats)
            if any('xlsm' in values for _, values in _formats(clause, declarations=True)):
                if _MACRO_WRITE.search(clause):
                    target.add(('create_macros', 'xlsm'))
                if _MACRO_RUN.search(clause):
                    target.add(('run_macros', 'xlsm'))
            # Explicit output labels need no extra "create" verb.
            if _OUTPUT_LABEL.search(clause) and not _INPUT.search(clause):
                fs = {f for _, values in _formats(clause, declarations=True) for f in values}
                target.update(('create', f) for f in fs)
                if not negative:
                    outputs.update(fs)
                    exclusive = bool(fs and re.search(
                        r'仅|只有|只(?:能)?|\bonly\b|所有输出.*必须|all outputs.*must', clause, re.I))
                    if exclusive:
                        closed = True
                        closed_outputs = fs if closed_outputs is None else closed_outputs.intersection(fs)
        # A scoped declaration such as "any .pptx file ... This includes creating
        # presentations" supports operations on that family, not arbitrary files.
        scope = re.search(r'(?:any time|any|凡涉及).{0,100}?(?:file|task|任务)', text, re.I)
        if scope:
            fs = {f for _, vals in _formats(scope.group(), declarations=True) for f in vals}
            continuation = text[scope.end():scope.end() + 650]
            if fs and re.search(r'This includes|——|—', continuation, re.I):
                for clause in _SPLIT.split(continuation):
                    checkpoint()
                    if _NEGATIVE.search(clause):
                        continue
                    # Only broad objects from the declared file family.
                    if re.search(r'presentations?|slides?|decks?|workbooks?|spreadsheets?|工作簿|表格', clause, re.I):
                        for action in _ACTION.finditer(clause):
                            for f in fs:
                                supported.add((action.lastgroup, f))
                                if action.lastgroup == 'create':
                                    outputs.add(f)
    if closed:
        outputs = closed_outputs
        supported = {(op, f) for op, f in supported if op != 'create' or f in outputs}
    return ArtifactContract(frozenset(supported), frozenset(denied), frozenset(outputs), closed)
