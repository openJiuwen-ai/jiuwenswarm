"""应用插件体检引擎（plugin_doctor 的核心逻辑，不依赖宿主运行时）。

设计约束：

* **只读**：只读扩展目录里的 ``extension.yaml`` / ``extension.py``，不导入被测插件、
  不执行命令、不访问网络。
* **复刻宿主真实加载语义**：每条规则都对应宿主代码里的一个具体分支（见 :data:`RULES`
  的 ``evidence``），不引入自造规范。
* **可注入**：宿主版本与"依赖是否已安装"都由调用方传入，因此单元测试可以用 ``tmp_path``
  构造任意畸形插件，不需要真的装一个宿主。
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Sequence

MANIFEST_FILENAME = "extension.yaml"
ENTRY_FILENAME = "extension.py"
FRONTEND_ASSET_DIR = ("frontend", "dist")
DEFAULT_ENTRYPOINT = "index.html"

SEVERITY_ERROR = "error"
SEVERITY_WARNING = "warning"
SEVERITY_INFO = "info"

_SEVERITY_PENALTY = {SEVERITY_ERROR: 20, SEVERITY_WARNING: 7, SEVERITY_INFO: 2}
_SEVERITY_ORDER = {SEVERITY_ERROR: 0, SEVERITY_WARNING: 1, SEVERITY_INFO: 2}

#: 仅清单插件（无 extension.py）唯一支持的前端渲染模式。
MANIFEST_ONLY_RENDER_MODES = ("iframe",)
#: 宿主前端归一化接受的 render_mode（applicationPlugins/manifest.ts:12）。
KNOWN_RENDER_MODES = ("bundled", "iframe", "none")
#: iframe 的 allow 只认这三项，其余 permissions 会被静默丢弃
#: （ApplicationPluginOutlet.tsx:66-70）。
IFRAME_ALLOW_PERMISSIONS = ("camera", "microphone", "display_capture")

_SEMVER_RE = re.compile(r"^\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?")

MANIFEST_ONLY = "manifest-only"
BACKEND = "backend"


@dataclass(frozen=True)
class Rule:
    """一条体检规则的元信息；``evidence`` 指向宿主代码里被复刻的那个分支。"""

    rule_id: str
    severity: str
    title: str
    rationale: str
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "title": self.title,
            "rationale": self.rationale,
            "evidence": self.evidence,
        }


@dataclass(frozen=True)
class Finding:
    """一条体检结论。"""

    rule_id: str
    severity: str
    target: str
    message: str
    hint: str = ""

    @property
    def rule(self) -> Rule:
        return RULES_BY_ID[self.rule_id]

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "severity": self.severity,
            "target": self.target,
            "message": self.message,
            "hint": self.hint,
            "title": self.rule.title,
            "evidence": self.rule.evidence,
        }

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (_SEVERITY_ORDER.get(self.severity, 9), self.target, self.rule_id)


@dataclass(frozen=True)
class PluginReport:
    """单个扩展目录的体检结果。"""

    root: str
    kind: str
    plugin_id: str
    name: str
    version: str
    package_type: str
    findings: tuple[Finding, ...]
    score: int

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == SEVERITY_ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == SEVERITY_WARNING)

    @property
    def infos(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == SEVERITY_INFO)

    @property
    def status(self) -> str:
        if self.errors:
            return "broken"
        if self.warnings:
            return "degraded"
        return "ok"

    def to_dict(self) -> dict[str, Any]:
        return {
            "root": self.root,
            "kind": self.kind,
            "plugin_id": self.plugin_id,
            "name": self.name,
            "version": self.version,
            "package_type": self.package_type,
            "status": self.status,
            "score": self.score,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "infos": len(self.infos),
            "findings": [f.to_dict() for f in self.findings],
        }


@dataclass(frozen=True)
class DoctorReport:
    """一次完整的扫描结果。"""

    search_paths: tuple[str, ...]
    plugins: tuple[PluginReport, ...]
    scan_findings: tuple[Finding, ...]
    missing_paths: tuple[str, ...]
    running_version: str
    generated_at: str
    score: int

    @property
    def findings(self) -> tuple[Finding, ...]:
        items: list[Finding] = list(self.scan_findings)
        for plugin in self.plugins:
            items.extend(plugin.findings)
        return tuple(sorted(items, key=lambda f: f.sort_key))

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == SEVERITY_ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == SEVERITY_WARNING)

    @property
    def infos(self) -> tuple[Finding, ...]:
        return tuple(f for f in self.findings if f.severity == SEVERITY_INFO)

    def to_dict(self) -> dict[str, Any]:
        return {
            "generated_at": self.generated_at,
            "running_version": self.running_version,
            "search_paths": list(self.search_paths),
            "missing_paths": list(self.missing_paths),
            "scanned": len(self.plugins),
            "score": self.score,
            "errors": len(self.errors),
            "warnings": len(self.warnings),
            "infos": len(self.infos),
            "scan_findings": [f.to_dict() for f in self.scan_findings],
            "plugins": [p.to_dict() for p in self.plugins],
        }


# --------------------------------------------------------------------------
# 规则目录：每条规则都对应宿主代码里的一个具体分支
# --------------------------------------------------------------------------

_RULE_DEFS: tuple[Rule, ...] = (
    Rule(
        "PD001",
        SEVERITY_ERROR,
        "目录不是扩展",
        "搜索目录下只有含 extension.yaml 或 extension.py 的子目录会被认成扩展。",
        "jiuwenswarm/extensions/loader.py:29-30",
    ),
    Rule(
        "PD002",
        SEVERITY_ERROR,
        "清单不是普通文件",
        "loader 用 Path.exists() 判断清单（目录也算存在），随后 read_text 会失败。",
        "jiuwenswarm/extensions/loader.py:19-21,167-174",
    ),
    Rule(
        "PD003",
        SEVERITY_ERROR,
        "清单 YAML 无法解析",
        "yaml.safe_load 抛错会让该插件加载失败，而 manager 只记日志、插件静默消失。",
        "jiuwenswarm/extensions/loader.py:171-174; "
        "jiuwenswarm/extensions/manager.py:136-137",
    ),
    Rule(
        "PD004",
        SEVERITY_ERROR,
        "清单根节点不是映射",
        "清单根节点是非空的列表/标量时，元数据加载会对它调用 .get() 而抛 AttributeError（null/空列表会被 `or {}` "
        "归一，不会报错）；manifest-only 场景更早失败在 loader 读 package_type 的那一步。",
        "jiuwenswarm/extensions/loader.py:71; "
        "jiuwenswarm/extensions/sdk/base.py:71-74",
    ),
    Rule(
        "PD005",
        SEVERITY_ERROR,
        "PyYAML 不可用",
        "缺 yaml 时清单被静默当成空字典，id/name/前端声明全部丢失。",
        "jiuwenswarm/extensions/loader.py:175-176",
    ),
    Rule(
        "PD006",
        SEVERITY_ERROR,
        "id 为空",
        "仅清单插件的 id 会直接决定 plugin_id，为空时 loader 抛 ValueError。",
        "jiuwenswarm/extensions/loader.py:73-74; "
        "jiuwenswarm/extensions/registry.py:144-148",
    ),
    Rule(
        "PD007",
        SEVERITY_ERROR,
        "仅清单插件未声明前端页",
        "package_type=application 且无 extension.py 时必须至少声明一个前端，否则 loader 抛错。",
        "jiuwenswarm/extensions/loader.py:71-78",
    ),
    Rule(
        "PD008",
        SEVERITY_ERROR,
        "仅清单插件用了非 iframe 渲染模式",
        "manifest-only 只支持 iframe，其它 render_mode 直接抛 ValueError。",
        "jiuwenswarm/extensions/sdk/application_plugin.py:133-137",
    ),
    Rule(
        "PD009",
        SEVERITY_ERROR,
        "前端入口为空",
        "entrypoint 为空会抛 ValueError。",
        "jiuwenswarm/extensions/sdk/application_plugin.py:138-140",
    ),
    Rule(
        "PD010",
        SEVERITY_ERROR,
        "前端 position 不是整数",
        "position 无法转 int 会抛 ValueError。",
        "jiuwenswarm/extensions/sdk/application_plugin.py:144-147",
    ),
    Rule(
        "PD011",
        SEVERITY_ERROR,
        "缺少 extension.py 且不是 application 包",
        "既没有入口脚本、package_type 又不是 application 时，loader 会尝试按文件导入并抛 FileNotFoundError。",
        "jiuwenswarm/extensions/loader.py:70-83,146-152",
    ),
    Rule(
        "PD012",
        SEVERITY_ERROR,
        "extension.py 存在语法错误",
        "入口脚本按文件路径 exec_module，语法错误会让插件加载失败。",
        "jiuwenswarm/extensions/loader.py:146-164",
    ),
    Rule(
        "PD013",
        SEVERITY_WARNING,
        "入口脚本未导出 register_extensions",
        "没有 register_extensions 时模块能导入，但什么都不会注册（静默空转）。",
        "jiuwenswarm/extensions/loader.py:85-99",
    ),
    Rule(
        "PD014",
        SEVERITY_ERROR,
        "register_extensions 不是 async",
        "loader 直接 await 该函数的返回值，同步函数返回列表会抛 TypeError。",
        "jiuwenswarm/extensions/loader.py:85-86",
    ),
    Rule(
        "PD015",
        SEVERITY_ERROR,
        "前端入口文件不存在",
        "前端资源目录固定为 frontend/dist，入口文件缺失时页面取不到资源。",
        "jiuwenswarm/extensions/sdk/application_plugin.py:105-110; "
        "jiuwenswarm/gateway/extensions/application_host.py:96-110",
    ),
    Rule(
        "PD016",
        SEVERITY_ERROR,
        "plugin_id 重复",
        "registry 以 plugin_id 为键，重复注册会抛 ValueError，后一个插件被跳过。",
        "jiuwenswarm/extensions/registry.py:140-151",
    ),
    Rule(
        "PD017",
        SEVERITY_WARNING,
        "清单未声明 id",
        "注册用的 plugin_id 取代码里的类属性，清单 id 只是兜底；清单缺 id 会让元数据 id 与注册 id 脱节。",
        "jiuwenswarm/extensions/registry.py:144-148",
    ),
    Rule(
        "PD018",
        SEVERITY_WARNING,
        "类属性 plugin_id 与清单 id 不一致",
        "两处各有一份 id，注册用类属性、元数据读清单，容易出现「改名只改了一半」。",
        "jiuwenswarm/extensions/registry.py:144-148; "
        "jiuwenswarm/extensions/sdk/base.py:73-92",
    ),
    Rule(
        "PD019",
        SEVERITY_WARNING,
        "元数据字段缺失",
        "name/version/author/description 缺失时元数据静默变成空串，面板与列表会显示空白。",
        "jiuwenswarm/extensions/sdk/base.py:73-92",
    ),
    Rule(
        "PD020",
        SEVERITY_WARNING,
        "min_jiuwenswarm_version 高于当前宿主",
        "该字段宿主只声明不比较（唯一一处比较在体检工具自己这里），写高了不会有任何提示。",
        "jiuwenswarm/extensions/types.py:15; jiuwenswarm/extensions/sdk/base.py:79",
    ),
    Rule(
        "PD021",
        SEVERITY_WARNING,
        "声明的依赖未安装",
        "加载时会用 uv/pip 自动安装，120 秒超时；失败只记日志、不阻断加载。",
        "jiuwenswarm/extensions/loader.py:101-144",
    ),
    Rule(
        "PD022",
        SEVERITY_WARNING,
        "requires_transport 不是布尔真值",
        "宿主用 is True 判断，字符串「true」之类不会被识别。",
        "jiuwenswarm/extensions/manager.py:120-123",
    ),
    Rule(
        "PD023",
        SEVERITY_WARNING,
        "入口脚本的导入方式在按路径加载时会失败",
        "loader 用 spec_from_file_location 按文件加载，插件目录不在 sys.path "
        "上：相对导入会失败，导入同级模块要自己处理加载路径（包内绝对导入或按文件路径加载）。",
        "jiuwenswarm/extensions/loader.py:146-164",
    ),
    Rule(
        "PD024",
        SEVERITY_WARNING,
        "扩展被套在更深一层目录里",
        "搜索目录只扫一层子目录，嵌套放置的扩展不会被发现。",
        "jiuwenswarm/extensions/loader.py:47-58",
    ),
    Rule(
        "PD025",
        SEVERITY_INFO,
        "permissions 只做透传",
        "permissions 只会透传到前端 manifest 与 iframe 的 allow 白名单，宿主不做访问控制；插件需要自己守住安全边界。",
        "jiuwenswarm/gateway/extensions/application_host.py:35,50; "
        "jiuwenswarm/channels/web/frontend/src/applicationPlugins/ApplicationPluginOu"
        "tlet.tsx:66-70",
    ),
    Rule(
        "PD026",
        SEVERITY_INFO,
        "前端贡献无法静态判定",
        "前端贡献在代码里动态构造时，本工具无法离线核对入口文件是否齐备。",
        "jiuwenswarm/extensions/sdk/application_plugin.py:102-110",
    ),
    Rule(
        "PD027",
        SEVERITY_INFO,
        "version 不是数字版本号",
        "宿主对扩展版本没有任何语义比较；写非数字版本不利于后续工具链排序与比较，建议 x.y.z。",
        "jiuwenswarm/extensions/sdk/base.py:76",
    ),
    Rule(
        "PD028",
        SEVERITY_WARNING,
        "permissions 含 iframe 白名单外的值",
        "iframe 的 allow 只映射 camera/microphone/display_capture，其余值会被静默丢弃。",
        "jiuwenswarm/channels/web/frontend/src/applicationPlugins/ApplicationPluginOu"
        "tlet.tsx:66-70",
    ),
    Rule(
        "PD029",
        SEVERITY_INFO,
        "没有前端贡献",
        "没有前端贡献的插件会以 render_mode=none 的管理占位项出现在扩展列表里。",
        "jiuwenswarm/gateway/extensions/application_host.py:30-44",
    ),
    Rule(
        "PD030",
        SEVERITY_INFO,
        "config_schema 不是映射",
        "config_schema 只会被原样存进元数据、宿主不做任何校验与使用。",
        "jiuwenswarm/extensions/sdk/base.py:81",
    ),
    Rule(
        "PD031",
        SEVERITY_ERROR,
        "缺少 extension.yaml（宿主读元数据会抛 FileNotFoundError）",
        "目录里没有清单时宿主读元数据会直接抛 FileNotFoundError（扩展目录未定则 "
        "ValueError）；/api/application-plugins 会对每个应用插件读一次元数据，所以这种插件会让整个扩展列表接口 500。",
        "jiuwenswarm/extensions/sdk/base.py:64-68; "
        "jiuwenswarm/gateway/extensions/application_host.py:27-29",
    ),
)

# 定义顺序是按主题分组的，对外展示统一按规则编号升序。
RULES: tuple[Rule, ...] = tuple(
    sorted(_RULE_DEFS, key=lambda item: item.rule_id)
)

RULES_BY_ID: dict[str, Rule] = {rule.rule_id: rule for rule in RULES}


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------


def _finding(rule_id: str, target: str, message: str, hint: str = "") -> Finding:
    return Finding(
        rule_id=rule_id,
        severity=RULES_BY_ID[rule_id].severity,
        target=target,
        message=message,
        hint=hint,
    )


def _score_of(findings: Sequence[Finding]) -> int:
    penalty = sum(_SEVERITY_PENALTY.get(f.severity, 0) for f in findings)
    return max(0, 100 - penalty)


def parse_version(value: Any) -> tuple[int, ...]:
    """把 ``0.2.5`` / ``v1.2`` 这类版本号解析成可比较的元组；无法解析时返回空元组。"""

    if not isinstance(value, str):
        return ()
    match = _SEMVER_RE.match(value)
    if not match:
        return ()
    return tuple(int(part) for part in match.groups() if part is not None)


def version_is_newer(candidate: Any, current: Any) -> bool:
    """判断 ``candidate`` 是否比 ``current`` 新；任一无法解析时返回 False（不臆测）。"""

    left = parse_version(candidate)
    right = parse_version(current)
    if not left or not right:
        return False
    length = max(len(left), len(right))
    return left + (0,) * (length - len(left)) > right + (0,) * (length - len(right))


def is_extension_root(path: Path) -> bool:
    """与宿主 ``_is_extension_root`` 同义：存在清单或入口脚本（注意是 exists 而非 is_file）。"""

    return (path / MANIFEST_FILENAME).exists() or (path / ENTRY_FILENAME).exists()


def frontend_asset_root(path: Path) -> Path | None:
    """与宿主 ``frontend_asset_root()`` 同义。"""

    candidate = path.joinpath(*FRONTEND_ASSET_DIR)
    return candidate if candidate.is_dir() else None


# --------------------------------------------------------------------------
# 清单校验
# --------------------------------------------------------------------------


def _load_manifest(
    root: Path, *, yaml_available: bool
) -> tuple[dict[str, Any] | None, list[Finding]]:
    findings: list[Finding] = []
    manifest_path = root / MANIFEST_FILENAME
    if not manifest_path.exists():
        return None, findings
    if not manifest_path.is_file():
        findings.append(
            _finding(
                "PD002",
                str(manifest_path),
                f"{MANIFEST_FILENAME} 不是普通文件（宿主用 exists() 判定，会在读取时报错）",
                f"把 {MANIFEST_FILENAME} 改成普通文件，或删掉这个目录",
            )
        )
        return None, findings
    if not yaml_available:
        findings.append(
            _finding(
                "PD005",
                str(manifest_path),
                "当前环境找不到 yaml 模块，宿主会把这份清单静默读成空字典",
                "在宿主环境安装 PyYAML",
            )
        )
        return None, findings

    import yaml

    try:
        raw = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        findings.append(
            _finding(
                "PD003",
                str(manifest_path),
                f"清单读取失败：{exc.__class__.__name__}: {exc}",
                "确认文件是 UTF-8 编码的普通文本",
            )
        )
        return None, findings

    try:
        data = yaml.safe_load(raw)
    except Exception as exc:  # noqa: BLE001 - 解析器异常类型由 PyYAML 决定
        findings.append(
            _finding(
                "PD003",
                str(manifest_path),
                f"YAML 解析失败：{exc.__class__.__name__}: {exc}",
                "修正 YAML 语法",
            )
        )
        return None, findings

    if data is None:
        data = {}
    if not isinstance(data, dict):
        findings.append(
            _finding(
                "PD004",
                str(manifest_path),
                f"清单根节点是 {type(data).__name__}，元数据加载会调用 .get() 而抛 AttributeError",
                "把清单写成键值映射",
            )
        )
        return None, findings
    return data, findings


def _manifest_frontends(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    raw = manifest.get("frontend")
    if not isinstance(raw, (list, tuple)):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _validate_manifest(
    root: Path,
    manifest: dict[str, Any],
    *,
    has_entry: bool,
    running_version: str = "",
) -> list[Finding]:
    findings: list[Finding] = []
    target = str(root / MANIFEST_FILENAME)
    package_type = str(manifest.get("package_type", "extension")).strip()
    plugin_id = str(manifest.get("id", "") or "").strip()

    min_version = str(manifest.get("min_jiuwenswarm_version", "") or "").strip()
    if min_version and version_is_newer(min_version, running_version):
        findings.append(
            _finding(
                "PD020",
                target,
                f"min_jiuwenswarm_version={min_version!r} 高于当前宿主 "
                f"{running_version!r}，而宿主只声明不比较该字段",
                "确认宿主版本，或下调该字段",
            )
        )

    missing = [
        key
        for key in ("name", "version", "author", "description")
        if not str(manifest.get(key, "") or "").strip()
    ]
    if missing:
        findings.append(
            _finding(
                "PD019",
                target,
                f"缺少字段：{', '.join(missing)}（元数据会静默变成空串）",
                "补齐这些字段，面板/列表才能正常显示",
            )
        )

    if not plugin_id:
        findings.append(
            _finding(
                "PD017",
                target,
                "清单未声明 id",
                f"在 {MANIFEST_FILENAME} 里补 id（同时与类属性 plugin_id 保持一致）",
            )
        )

    version = manifest.get("version")
    if version is not None and str(version).strip() and not parse_version(str(version)):
        findings.append(
            _finding(
                "PD027",
                target,
                f"version={version!r} 不是数字版本号",
                "建议写成 x.y.z",
            )
        )

    if "config_schema" in manifest and not isinstance(
        manifest.get("config_schema"), dict
    ):
        findings.append(
            _finding(
                "PD030",
                target,
                "config_schema 不是映射，宿主不会做任何校验",
                "改成 JSON Schema 对象或删掉该字段",
            )
        )

    permissions = manifest.get("permissions")
    if isinstance(permissions, (list, tuple)) and permissions:
        findings.append(
            _finding(
                "PD025",
                target,
                f"已声明 {len(permissions)} 项 permissions（只透传到前端 manifest 与 iframe 的 "
                "allow 白名单，宿主不做访问控制）",
                "保留即可，但不要依赖它做安全边界",
            )
        )
        unsupported = [
            str(item)
            for item in permissions
            if str(item).strip() and str(item).strip() not in IFRAME_ALLOW_PERMISSIONS
        ]
        if unsupported:
            findings.append(
                _finding(
                    "PD028",
                    target,
                    f"permissions 里的 {', '.join(unsupported)} 不在 iframe allow 白名单"
                    f"（{'/'.join(IFRAME_ALLOW_PERMISSIONS)}）内，会被静默丢弃",
                    "需要这些能力时在插件内部自行校验或降级",
                )
            )

    requires_transport = manifest.get("requires_transport")
    # 布尔值都是合法声明：显式 false 就是「不要求 transport」，宿主 is True 判为 False 正是预期。
    if "requires_transport" in manifest and not isinstance(requires_transport, bool):
        findings.append(
            _finding(
                "PD022",
                target,
                f"requires_transport={requires_transport!r} 不是布尔值，"
                "宿主用 is True 判断，因此该声明不会生效",
                "改成布尔 true/false，或删掉该字段",
            )
        )

    if package_type == "application" and not has_entry:
        frontends = _manifest_frontends(manifest)
        if not frontends:
            findings.append(
                _finding(
                    "PD007",
                    target,
                    "package_type=application 且没有 extension.py，却没有声明任何前端页",
                    "补 frontend 列表，或加上 extension.py",
                )
            )
        for index, item in enumerate(frontends):
            where = f"{target}#frontend[{index}]"
            render_mode = str(item.get("render_mode", "iframe")).strip() or "iframe"
            if render_mode not in MANIFEST_ONLY_RENDER_MODES:
                findings.append(
                    _finding(
                        "PD008",
                        where,
                        f"render_mode={render_mode!r}，仅清单插件只支持 iframe",
                        "改成 iframe，或加上 extension.py 自行提供渲染模式",
                    )
                )
            if not str(item.get("entrypoint", DEFAULT_ENTRYPOINT)).strip():
                findings.append(
                    _finding(
                        "PD009",
                        where,
                        "entrypoint 为空",
                        f"补 entrypoint（默认 {DEFAULT_ENTRYPOINT}）",
                    )
                )
            position = item.get("position")
            if position is not None:
                try:
                    int(position)
                except (TypeError, ValueError):
                    findings.append(
                        _finding(
                            "PD010",
                            where,
                            f"position={position!r} 无法转成整数",
                            "改成整数",
                        )
                    )
            if not plugin_id:
                findings.append(
                    _finding(
                        "PD006",
                        where,
                        "仅清单插件的 id 为空，loader 会直接抛 ValueError",
                        f"在 {MANIFEST_FILENAME} 里补 id",
                    )
                )
    return findings


def _validate_frontend_files(
    root: Path,
    manifest: dict[str, Any],
    entry_source: str | None,
) -> list[Finding]:
    """校验前端入口文件是否真的存在（仅清单插件 + 静态可判定的代码贡献）。"""

    findings: list[Finding] = []
    asset_root = frontend_asset_root(root)
    declared: list[tuple[str, str]] = []

    if entry_source is None:
        for index, item in enumerate(_manifest_frontends(manifest)):
            render_mode = str(item.get("render_mode", "iframe")).strip() or "iframe"
            entrypoint = str(item.get("entrypoint", DEFAULT_ENTRYPOINT)).strip()
            if render_mode == "iframe":
                declared.append(
                    (f"{root / MANIFEST_FILENAME}#frontend[{index}]", entrypoint)
                )
    else:
        declared.extend(_static_frontend_entrypoints(root, entry_source, findings))

    for where, entrypoint in declared:
        if not entrypoint:
            continue
        if asset_root is None:
            findings.append(
                _finding(
                    "PD015",
                    where,
                    f"声明了前端入口 {entrypoint}，但缺少 frontend/dist 目录",
                    "把页面放到 frontend/dist 下",
                )
            )
            continue
        if not (asset_root / entrypoint).is_file():
            findings.append(
                _finding(
                    "PD015",
                    where,
                    f"前端入口 {entrypoint} 在 frontend/dist 下不存在",
                    "补齐该文件，或修正 entrypoint",
                )
            )
    return findings


def _static_frontend_entrypoints(
    root: Path, source: str, findings: list[Finding]
) -> list[tuple[str, str]]:
    """从 extension.py 里提取静态可判定的 FrontendContribution 入口。"""

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    results: list[tuple[str, str]] = []
    dynamic_seen = False
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "FrontendContribution":
            continue
        values: dict[str, Any] = {}
        dynamic = False
        for keyword in node.keywords:
            if keyword.arg is None:
                dynamic = True
                continue
            if isinstance(keyword.value, ast.Constant):
                values[keyword.arg] = keyword.value.value
            else:
                dynamic = True
        where = f"{root / ENTRY_FILENAME}:{getattr(node, 'lineno', 0)}"
        if dynamic:
            dynamic_seen = True
            findings.append(
                _finding(
                    "PD026",
                    where,
                    "该前端贡献用了动态字段，离线无法核对入口文件是否存在",
                    "可用字面量声明 entrypoint/render_mode 以便静态体检",
                )
            )
        render_mode = str(values.get("render_mode", "iframe") or "iframe")
        if render_mode not in KNOWN_RENDER_MODES:
            findings.append(
                _finding(
                    "PD026",
                    where,
                    f"render_mode={render_mode!r} 不在宿主前端接受的范围内",
                    f"改成 {', '.join(KNOWN_RENDER_MODES)} 之一",
                )
            )
        entrypoint = str(values.get("entrypoint", "") or "").strip()
        # 只有 iframe 贡献才会去 frontend/dist 取页面；bundled/none 不落地资源文件。
        if render_mode == "iframe" and entrypoint:
            results.append((where, entrypoint))
    if dynamic_seen and not results:
        return []
    return results


def _validate_dependencies(
    manifest: dict[str, Any], is_installed: Callable[[str], bool]
) -> list[Finding]:
    dependencies = manifest.get("dependencies")
    if not isinstance(dependencies, dict) or not dependencies:
        return []
    findings: list[Finding] = []
    for package in dependencies:
        name = str(package).strip()
        if not name:
            continue
        if not is_installed(name):
            findings.append(
                _finding(
                    "PD021",
                    name,
                    f"声明的依赖 {name!r} 当前环境查不到（宿主会尝试 uv/pip 安装，失败只记日志）",
                    "确认包名正确，或先手工安装",
                )
            )
    return findings


# --------------------------------------------------------------------------
# 入口脚本静态分析
# --------------------------------------------------------------------------


def _class_plugin_ids(tree: ast.AST) -> list[str]:
    ids: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if not isinstance(stmt, ast.Assign):
                continue
            for target in stmt.targets:
                if isinstance(target, ast.Name) and target.id == "plugin_id":
                    if isinstance(stmt.value, ast.Constant) and isinstance(
                        stmt.value.value, str
                    ):
                        ids.append(stmt.value.value)
    return ids


#: PD023 里判「已经自己处理了同级模块加载路径」的写法。
#: 不把 ``sys.path`` 当唯一正解：CodeCheck G.PSL.03 会对 ``sys.path.insert(0, ...)`` 告警。
_PATH_REMEDIES = ("sys.path", "spec_from_file_location", "SourceFileLoader")


def _sibling_modules(root: Path) -> set[str]:
    names: set[str] = set()
    try:
        entries = list(root.iterdir())
    except OSError:
        return names
    for item in entries:
        if item.name.startswith("__") or item.name == ENTRY_FILENAME:
            continue
        if item.is_dir():
            names.add(item.name)
        elif item.suffix == ".py":
            names.add(item.stem)
    return names


def _validate_entry(
    root: Path, source: str, manifest: dict[str, Any] | None
) -> tuple[list[Finding], str]:
    """校验 extension.py；返回（结论, 入口脚本里的 plugin_id）。"""

    findings: list[Finding] = []
    entry_path = root / ENTRY_FILENAME
    try:
        tree = ast.parse(source)
    except SyntaxError as exc:
        findings.append(
            _finding(
                "PD012",
                f"{entry_path}:{exc.lineno or 0}",
                f"语法错误：{exc.msg}",
                "修掉语法错误后再发布",
            )
        )
        return findings, ""

    register = None
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
            node.name == "register_extensions"
        ):
            register = node
            break

    if register is None:
        findings.append(
            _finding(
                "PD013",
                str(entry_path),
                "没有导出 register_extensions，宿主加载该模块后不会注册任何东西",
                "新增 async def register_extensions(registry) 并注册扩展",
            )
        )
    elif not isinstance(register, ast.AsyncFunctionDef):
        findings.append(
            _finding(
                "PD014",
                f"{entry_path}:{register.lineno}",
                "register_extensions 不是 async 函数",
                "改成 async def",
            )
        )

    siblings = _sibling_modules(root)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level and node.level > 0:
                findings.append(
                    _finding(
                        "PD023",
                        f"{entry_path}:{node.lineno}",
                        "使用了相对导入，但宿主是按文件路径加载入口脚本的，相对导入会失败",
                        "改成包内绝对导入，或按文件路径加载同级模块（不建议改 sys.path）",
                    )
                )
            elif node.module and node.module.split(".")[0] in siblings:
                if not any(marker in source for marker in _PATH_REMEDIES):
                    findings.append(
                        _finding(
                            "PD023",
                            f"{entry_path}:{node.lineno}",
                            f"导入了同级模块 {node.module!r}，但脚本里没有处理加载路径",
                            "改用包内绝对导入，或 importlib 按文件路径加载（不建议改 sys.path）",
                        )
                    )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in siblings and not any(
                    marker in source for marker in _PATH_REMEDIES
                ):
                    findings.append(
                        _finding(
                            "PD023",
                            f"{entry_path}:{node.lineno}",
                            f"导入了同级模块 {alias.name!r}，但脚本里没有处理加载路径",
                            "改用包内绝对导入，或 importlib 按文件路径加载（不建议改 sys.path）",
                        )
                    )

    class_ids = _class_plugin_ids(tree)
    entry_id = class_ids[0] if class_ids else ""
    manifest_id = ""
    if manifest is not None:
        manifest_id = str(manifest.get("id", "") or "").strip()
    if class_ids and manifest_id and class_ids[0] != manifest_id:
        findings.append(
            _finding(
                "PD018",
                f"{entry_path}:{register.lineno if register else 1}",
                f"类属性 plugin_id={class_ids[0]!r} 与清单 id={manifest_id!r} 不一致",
                "去掉其中一个真值来源，或让两者一致",
            )
        )
    return findings, entry_id


# --------------------------------------------------------------------------
# 单目录 / 多目录扫描
# --------------------------------------------------------------------------


def inspect_directory(
    root: Path,
    *,
    running_version: str | None = None,
    is_installed: Callable[[str], bool] | None = None,
    yaml_available: bool = True,
) -> PluginReport:
    """体检单个扩展目录（也用于「目录根本不像插件」的诊断）。"""

    root = Path(root)
    installed = is_installed or (lambda _name: True)
    findings: list[Finding] = []

    if not root.is_dir():
        findings.append(
            _finding(
                "PD001",
                str(root),
                "路径不存在或不是目录",
                "确认路径是否正确",
            )
        )
        return _build_plugin_report(
            root, findings, manifest=None, entry_id="", kind="unknown"
        )

    manifest_path = root / MANIFEST_FILENAME
    entry_path = root / ENTRY_FILENAME
    if not manifest_path.exists() and not entry_path.exists():
        findings.append(
            _finding(
                "PD001",
                str(root),
                f"目录里既没有 {MANIFEST_FILENAME} 也没有 {ENTRY_FILENAME}，宿主不会把它认成扩展",
                "补上清单或入口脚本",
            )
        )
        return _build_plugin_report(
            root, findings, manifest=None, entry_id="", kind="unknown"
        )

    manifest, manifest_findings = _load_manifest(root, yaml_available=yaml_available)
    findings.extend(manifest_findings)

    source: str | None = None
    entry_id = ""
    if entry_path.exists():
        if not entry_path.is_file():
            findings.append(
                _finding(
                    "PD012",
                    str(entry_path),
                    f"{ENTRY_FILENAME} 不是普通文件",
                    "改成普通文件",
                )
            )
        else:
            try:
                source = entry_path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                findings.append(
                    _finding(
                        "PD012",
                        str(entry_path),
                        f"入口脚本读取失败：{exc.__class__.__name__}: {exc}",
                        "确认文件是 UTF-8 编码的普通文本",
                    )
                )

    if manifest is not None:
        findings.extend(
            _validate_manifest(
                root,
                manifest,
                has_entry=source is not None,
                running_version=running_version or "",
            )
        )
    elif source is not None and manifest_path.exists():
        # 清单读不出来（PD002-PD005 已报根因）：不再拿空 dict 去校验，避免叠加
        # PD017/PD019 这类「字段缺失」结论把根因盖住。
        manifest = {}
    else:
        manifest = {}

    if source is not None and not manifest_path.exists():
        findings.append(
            _finding(
                "PD031",
                str(root),
                "目录里没有 extension.yaml：宿主读元数据会抛 FileNotFoundError，"
                "扩展列表接口会整体 500",
                "补一份 extension.yaml（id 与代码里的类属性 plugin_id 一致）",
            )
        )

    if source is not None:
        entry_findings, entry_id = _validate_entry(root, source, manifest)
        findings.extend(entry_findings)
        findings.extend(_validate_frontend_files(root, manifest, source))
    elif not entry_path.exists():
        package_type = str(manifest.get("package_type", "extension")).strip()
        if package_type != "application":
            findings.append(
                _finding(
                    "PD011",
                    str(root),
                    "没有 extension.py，package_type 也不是 application，宿主会尝试按文件导入并抛 "
                    "FileNotFoundError",
                    "加上 extension.py，或把 package_type 改成 application",
                )
            )
        findings.extend(_validate_frontend_files(root, manifest, None))

    findings.extend(_validate_dependencies(manifest, installed))

    has_frontend = bool(_manifest_frontends(manifest))
    if source is not None:
        # 只有带入口脚本（后端插件）时，代码与 frontend/dist 才算前端贡献来源；
        # 仅清单插件的页面只能来自清单，光有 dist 文件宿主也不会展示页面。
        if "FrontendContribution" in source:
            has_frontend = True
        if frontend_asset_root(root) is not None:
            has_frontend = True
    if not has_frontend:
        findings.append(
            _finding(
                "PD029",
                str(root),
                "没有任何前端贡献（清单未声明 frontend，代码里也没有 FrontendContribution），"
                "宿主会以 render_mode=none 的管理占位项展示该插件",
                "需要页面时补 FrontendContribution 与 frontend/dist/index.html",
            )
        )

    kind = BACKEND if source is not None else MANIFEST_ONLY
    if manifest_path.exists() and source is None:
        kind = MANIFEST_ONLY
    return _build_plugin_report(
        root, findings, manifest=manifest, entry_id=entry_id, kind=kind
    )


def _build_plugin_report(
    root: Path,
    findings: list[Finding],
    *,
    manifest: dict[str, Any] | None,
    entry_id: str,
    kind: str,
) -> PluginReport:
    manifest = manifest or {}
    # 与宿主 registry 的取值顺序一致：类属性 plugin_id 优先，清单 id 只在缺类属性时兜底
    # （registry.py:144-151 以该值作 _application_plugins 的唯一键，PD016 判重靠它）。
    plugin_id = entry_id.strip() or str(manifest.get("id", "") or "").strip()
    deduped = _dedupe_findings(findings)
    return PluginReport(
        root=str(root),
        kind=kind,
        plugin_id=plugin_id,
        name=str(manifest.get("name", "") or "").strip(),
        version=str(manifest.get("version", "") or "").strip(),
        package_type=str(manifest.get("package_type", "extension")).strip(),
        findings=deduped,
        score=_score_of(deduped),
    )


def _dedupe_findings(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    seen: set[tuple[str, str, str]] = set()
    result: list[Finding] = []
    for item in findings:
        key = (item.rule_id, item.target, item.message)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return tuple(sorted(result, key=lambda f: f.sort_key))


def scan_search_paths(
    search_paths: Iterable[Any],
    *,
    running_version: str | None = None,
    is_installed: Callable[[str], bool] | None = None,
    yaml_available: bool = True,
    generated_at: str | None = None,
) -> DoctorReport:
    """扫描宿主全部扩展搜索目录。"""

    installed = is_installed or (lambda _name: True)
    paths: list[Path] = []
    missing: list[str] = []
    seen: set[str] = set()
    for raw in search_paths:
        path = Path(raw)
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if path.is_dir():
            paths.append(path)
        else:
            missing.append(str(path))

    plugins: list[PluginReport] = []
    scan_findings: list[Finding] = []
    for base in paths:
        try:
            subdirs = sorted(
                (item for item in base.iterdir() if item.is_dir()),
                key=lambda item: item.name.lower(),
            )
        except OSError as exc:
            scan_findings.append(
                _finding(
                    "PD024",
                    str(base),
                    f"搜索目录无法读取：{exc.__class__.__name__}: {exc}",
                    "检查目录权限",
                )
            )
            continue
        for subdir in subdirs:
            if is_extension_root(subdir):
                plugins.append(
                    inspect_directory(
                        subdir,
                        running_version=running_version,
                        is_installed=installed,
                        yaml_available=yaml_available,
                    )
                )
                continue
            nested = _nested_extension_roots(subdir)
            for item in nested:
                scan_findings.append(
                    _finding(
                        "PD024",
                        str(item),
                        f"扩展藏在 {subdir.name}/ 里，宿主只扫搜索目录的下一层，加载不到",
                        "把扩展目录直接放到搜索目录下",
                    )
                )

    plugins = _apply_duplicate_id_rule(plugins)

    all_findings: list[Finding] = list(scan_findings)
    for plugin in plugins:
        all_findings.extend(plugin.findings)
    return DoctorReport(
        search_paths=tuple(str(p) for p in paths),
        plugins=tuple(plugins),
        scan_findings=tuple(sorted(scan_findings, key=lambda f: f.sort_key)),
        missing_paths=tuple(missing),
        running_version=str(running_version or ""),
        generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
        score=_score_of(all_findings),
    )


def _nested_extension_roots(parent: Path) -> list[Path]:
    try:
        children = list(parent.iterdir())
    except OSError:
        return []
    return [
        child
        for child in children
        if child.is_dir() and is_extension_root(child)
    ]


def _apply_duplicate_id_rule(plugins: Sequence[PluginReport]) -> list[PluginReport]:
    grouped: dict[str, list[PluginReport]] = {}
    for plugin in plugins:
        if plugin.plugin_id:
            grouped.setdefault(plugin.plugin_id, []).append(plugin)

    duplicates: dict[str, list[str]] = {}
    for plugin_id, items in grouped.items():
        if len(items) > 1:
            duplicates[plugin_id] = [item.root for item in items]

    if not duplicates:
        return list(plugins)

    result: list[PluginReport] = []
    for plugin in plugins:
        roots = duplicates.get(plugin.plugin_id)
        if not roots:
            result.append(plugin)
            continue
        others = [item for item in roots if item != plugin.root]
        findings = list(plugin.findings)
        findings.append(
            _finding(
                "PD016",
                plugin.root,
                f"plugin_id={plugin.plugin_id!r} 与其它插件重复：{', '.join(others)}",
                "同一 id 只有第一个能注册成功，改成全局唯一的 id",
            )
        )
        deduped = _dedupe_findings(findings)
        result.append(
            PluginReport(
                root=plugin.root,
                kind=plugin.kind,
                plugin_id=plugin.plugin_id,
                name=plugin.name,
                version=plugin.version,
                package_type=plugin.package_type,
                findings=deduped,
                score=_score_of(deduped),
            )
        )
    return result


# --------------------------------------------------------------------------
# 报告输出
# --------------------------------------------------------------------------


def rules_payload() -> dict[str, Any]:
    """给前端的规则目录。"""

    return {
        "count": len(RULES),
        "rules": [rule.to_dict() for rule in RULES],
    }


def render_markdown(report: DoctorReport) -> str:
    """把体检报告渲染成可粘贴进 MR/工单的 Markdown。"""

    lines: list[str] = []
    lines.append("# 应用插件体检报告")
    lines.append("")
    lines.append(f"- 生成时间：{report.generated_at}")
    lines.append(f"- 宿主版本：{report.running_version or '未知'}")
    lines.append(f"- 扫描目录：{len(report.search_paths)} 个")
    lines.append(f"- 插件数量：{len(report.plugins)}")
    lines.append(
        f"- 结论：错误 {len(report.errors)} / 警告 {len(report.warnings)} / "
        f"提示 {len(report.infos)}，健康分 {report.score}"
    )
    lines.append("")
    if report.missing_paths:
        lines.append("## 不存在的搜索目录")
        lines.append("")
        for path in report.missing_paths:
            lines.append(f"- `{path}`")
        lines.append("")
    lines.append("## 插件")
    lines.append("")
    lines.append("| 插件 | 类型 | 状态 | 分数 | 错误 | 警告 | 提示 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for plugin in report.plugins:
        lines.append(
            f"| `{plugin.plugin_id or plugin.root}` | {plugin.kind} | "
            f"{plugin.status} | "
            f"{plugin.score} | {len(plugin.errors)} | {len(plugin.warnings)} | "
            f"{len(plugin.infos)} |"
        )
    lines.append("")
    if report.findings:
        lines.append("## 明细")
        lines.append("")
        lines.append("| 规则 | 级别 | 目标 | 说明 | 建议 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for finding in report.findings:
            message = finding.message.replace("|", "\\|")
            hint = (finding.hint or "-").replace("|", "\\|")
            lines.append(
                f"| {finding.rule_id} | {finding.severity} | `{finding.target}` | "
                f"{message} | {hint} |"
            )
    else:
        lines.append("## 明细")
        lines.append("")
        lines.append("未发现问题。")
    lines.append("")
    return "\n".join(lines)
