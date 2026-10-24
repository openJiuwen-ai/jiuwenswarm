"""plugin_doctor 引擎单元测试：每条规则都要有一个能复现的畸形插件目录。

测试思路：**规则必须能被证伪**。每条 PD 规则都配一个最小目录，断言该规则出现；
同时给「合法写法」配一个反向断言，避免规则变成噪声。
"""

from __future__ import annotations

import json

import jiuwenswarm
from pathlib import Path

import pytest

from jiuwenswarm.extensions.plugin_doctor import doctor_engine

REPO_ROOT = Path(jiuwenswarm.__file__).resolve().parent.parent

HEALTHY_MANIFEST = """\
id: __ID__
name: Good Plugin
version: 1.2.3
description: A healthy application plugin
author: tests
min_jiuwenswarm_version: "0.1.0"
dependencies: {}
package_type: application
"""

HEALTHY_ENTRY = '''\
"""Healthy plugin entry."""

from __future__ import annotations

import sys
from pathlib import Path

_DIR = Path(__file__).resolve().parent
if str(_DIR) not in sys.path:
    sys.path.insert(0, str(_DIR))

from jiuwenswarm.extensions.sdk import (
    ApplicationPluginExtension,
    FrontendContribution,
)


class GoodPlugin(ApplicationPluginExtension):
    plugin_id = "__ID__"

    def frontend_contributions(self):
        return (
            FrontendContribution(
                id="good-page",
                nav_key="app:__ID__",
                title="Good",
                render_mode="iframe",
                entrypoint="index.html",
                position=100,
            ),
        )


async def register_extensions(registry):
    plugin = GoodPlugin()
    registry.register_application_plugin(plugin)
    return [plugin]
'''

MANIFEST_ONLY = """\
id: __ID__
name: Page Only
version: 1.0.0
description: Manifest only plugin
author: tests
package_type: application
frontend:
  - id: __ID__-page
    title: Page
    entrypoint: index.html
"""


def dedent(text: str) -> str:
    return text.strip() + "\n"


def write(root: Path, relative: str, text: str) -> Path:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


_DEFAULT = object()


def make_plugin(
    base: Path,
    name: str = "good-plugin",
    *,
    manifest: object = _DEFAULT,
    entry: object = _DEFAULT,
    asset: bool = True,
) -> Path:
    root = base / name
    root.mkdir(parents=True, exist_ok=True)
    if manifest is _DEFAULT:
        manifest = HEALTHY_MANIFEST
    if manifest:
        write(root, "extension.yaml", str(manifest).replace("__ID__", name))
    if entry is _DEFAULT:
        entry = HEALTHY_ENTRY
    if entry:
        write(root, "extension.py", str(entry).replace("__ID__", name))
    if asset:
        write(root, "frontend/dist/index.html", "<html>ok</html>\n")
    return root


def ids_of(report: doctor_engine.PluginReport) -> set[str]:
    return {finding.rule_id for finding in report.findings}


def scan_ids(report: doctor_engine.DoctorReport) -> set[str]:
    return {finding.rule_id for finding in report.findings}


# --------------------------------------------------------------------------
# 规则目录与健康样例
# --------------------------------------------------------------------------


def test_rule_catalog_is_complete_and_unique() -> None:
    assert len(doctor_engine.RULES) >= 31
    rule_ids = [rule.rule_id for rule in doctor_engine.RULES]
    assert len(rule_ids) == len(set(rule_ids))
    assert rule_ids == sorted(rule_ids)
    assert rule_ids[0] == "PD001"
    for rule in doctor_engine.RULES:
        assert rule.severity in ("error", "warning", "info")
        assert rule.title.strip()
        assert rule.rationale.strip()
        assert rule.evidence.strip(), f"{rule.rule_id} 缺少宿主代码依据"
        assert rule.to_dict()["rule_id"] == rule.rule_id


def test_rules_payload_matches_catalog() -> None:
    payload = doctor_engine.rules_payload()

    assert payload["count"] == len(doctor_engine.RULES)
    assert [item["rule_id"] for item in payload["rules"]] == [
        rule.rule_id for rule in doctor_engine.RULES
    ]


def test_every_rule_evidence_is_repo_relative() -> None:
    """依据必须能定位：仓库相对路径、文件存在、行号在文件范围内。"""

    for rule in doctor_engine.RULES:
        assert rule.evidence.startswith("jiuwenswarm/"), rule.rule_id
        assert "jiuwenswarm/jiuwenswarm" not in rule.evidence
        for part in rule.evidence.split("; "):
            path, _, lines = part.partition(":")
            target = REPO_ROOT / path
            assert target.is_file(), f"{rule.rule_id} 依据文件不存在: {path}"
            total = len(target.read_text(encoding="utf-8").splitlines())
            for spec in lines.split(","):
                start, _, end = spec.partition("-")
                for value in (start, end or start):
                    assert 1 <= int(value) <= total, (
                        f"{rule.rule_id} 行号越界 {path}:{spec}（共 {total} 行）"
                    )


def test_readme_rule_table_matches_catalog() -> None:
    """README 的规则表由 gen_readme.py 从 RULES 生成，这里防止两者漂移。"""

    from jiuwenswarm.extensions.plugin_doctor import extension as extension_module

    readme = (Path(extension_module.__file__).resolve().parent / "README.md").read_text(
        encoding="utf-8"
    )
    labels = {"error": "错误", "warning": "警告", "info": "提示"}
    rows = [line for line in readme.splitlines() if line.startswith("| PD")]
    assert len(rows) == len(doctor_engine.RULES)
    for rule, row in zip(doctor_engine.RULES, rows):
        expected = (
            f"| {rule.rule_id} | {labels[rule.severity]} | {rule.title} | "
            f"`{rule.evidence}` |"
        )
        assert row == expected, rule.rule_id


def test_healthy_plugin_has_no_findings_and_full_score(tmp_path: Path) -> None:
    report = doctor_engine.inspect_directory(make_plugin(tmp_path))

    assert report.findings == ()
    assert report.score == 100
    assert report.status == "ok"
    assert report.kind == doctor_engine.BACKEND
    assert report.plugin_id == "good-plugin"
    assert report.version == "1.2.3"
    assert report.package_type == "application"


def test_healthy_manifest_only_plugin_is_clean(tmp_path: Path) -> None:
    report = doctor_engine.inspect_directory(
        make_plugin(tmp_path, "page-only", manifest=MANIFEST_ONLY, entry=None)
    )

    assert report.findings == ()
    assert report.kind == doctor_engine.MANIFEST_ONLY
    assert report.status == "ok"


def test_inspect_directory_of_missing_path_reports_pd001(tmp_path: Path) -> None:
    report = doctor_engine.inspect_directory(tmp_path / "does-not-exist")

    assert ids_of(report) == {"PD001"}


# --------------------------------------------------------------------------
# PD001-PD005：目录与清单
# --------------------------------------------------------------------------


def test_directory_without_manifest_or_entry_is_pd001(tmp_path: Path) -> None:
    root = tmp_path / "plain"
    (root / "sub").mkdir(parents=True)

    assert ids_of(doctor_engine.inspect_directory(root)) == {"PD001"}


def test_manifest_as_directory_is_pd002(tmp_path: Path) -> None:
    root = tmp_path / "weird"
    (root / "extension.yaml").mkdir(parents=True)

    assert "PD002" in ids_of(doctor_engine.inspect_directory(root))


def test_broken_yaml_is_pd003(tmp_path: Path) -> None:
    root = make_plugin(tmp_path, manifest="id: [unclosed\n", entry=None)

    report = doctor_engine.inspect_directory(root)

    assert "PD003" in ids_of(report)


def test_non_mapping_yaml_root_is_pd004(tmp_path: Path) -> None:
    root = make_plugin(tmp_path, manifest="- one\n- two\n", entry=None)

    assert "PD004" in ids_of(doctor_engine.inspect_directory(root))


def test_missing_yaml_module_is_pd005(tmp_path: Path) -> None:
    root = make_plugin(tmp_path)

    report = doctor_engine.inspect_directory(root, yaml_available=False)

    assert "PD005" in ids_of(report)


def test_crlf_manifest_is_not_flagged(tmp_path: Path) -> None:
    manifest = HEALTHY_MANIFEST.replace("\n", "\r\n").replace("__ID__", "crlf-plugin")
    root = make_plugin(tmp_path, "crlf-plugin", manifest=manifest)

    assert ids_of(doctor_engine.inspect_directory(root)) == set()


# --------------------------------------------------------------------------
# PD006-PD010：manifest-only（package_type: application 且无 extension.py）
# --------------------------------------------------------------------------


def test_manifest_only_without_id_is_pd006(tmp_path: Path) -> None:
    manifest = MANIFEST_ONLY.replace("id: __ID__\n", "")
    root = make_plugin(tmp_path, "no-id", manifest=manifest, entry=None)

    assert "PD006" in ids_of(doctor_engine.inspect_directory(root))


def test_application_without_frontend_is_pd007(tmp_path: Path) -> None:
    manifest = "id: bare\nname: Bare\nversion: 1.0.0\ndescription: d\nauthor: a\n"
    manifest += "package_type: application\n"
    root = make_plugin(tmp_path, "bare", manifest=manifest, entry=None)

    report = doctor_engine.inspect_directory(root)

    assert {"PD007", "PD029"} <= ids_of(report)


def test_manifest_only_render_mode_not_iframe_is_pd008(tmp_path: Path) -> None:
    manifest = MANIFEST_ONLY + "    render_mode: bundled\n"
    root = make_plugin(tmp_path, "bundled-page", manifest=manifest, entry=None)

    assert "PD008" in ids_of(doctor_engine.inspect_directory(root))


def test_manifest_only_empty_entrypoint_is_pd009(tmp_path: Path) -> None:
    manifest = MANIFEST_ONLY.replace("    entrypoint: index.html\n", '    entrypoint: ""\n')
    root = make_plugin(tmp_path, "empty-entry", manifest=manifest, entry=None)

    assert "PD009" in ids_of(doctor_engine.inspect_directory(root))


def test_manifest_only_non_integer_position_is_pd010(tmp_path: Path) -> None:
    manifest = MANIFEST_ONLY + '    position: "abc"\n'
    root = make_plugin(tmp_path, "bad-position", manifest=manifest, entry=None)

    assert "PD010" in ids_of(doctor_engine.inspect_directory(root))


def test_manifest_only_without_asset_is_pd015(tmp_path: Path) -> None:
    root = make_plugin(tmp_path, "page-only", manifest=MANIFEST_ONLY, entry=None, asset=False)

    report = doctor_engine.inspect_directory(root)

    assert "PD015" in ids_of(report)


# --------------------------------------------------------------------------
# PD011-PD018：入口脚本
# --------------------------------------------------------------------------


def test_missing_entry_and_plain_package_type_is_pd011(tmp_path: Path) -> None:
    manifest = "id: legacy\nname: Legacy\nversion: 1.0.0\ndescription: d\nauthor: a\n"
    root = make_plugin(tmp_path, "legacy", manifest=manifest, entry=None)

    assert "PD011" in ids_of(doctor_engine.inspect_directory(root))


def test_application_package_without_entry_is_not_pd011(tmp_path: Path) -> None:
    manifest = (
        "id: app-ish\nname: A\nversion: 1.0.0\ndescription: d\nauthor: a\n"
        "package_type: application\n"
    )
    root = make_plugin(tmp_path, "app-ish", manifest=manifest, entry=None)

    ids = ids_of(doctor_engine.inspect_directory(root))

    assert "PD011" not in ids
    assert "PD007" in ids


def test_syntax_error_entry_is_pd012(tmp_path: Path) -> None:
    root = make_plugin(tmp_path, entry="async def register_extensions(:\n")

    assert "PD012" in ids_of(doctor_engine.inspect_directory(root))


def test_entry_without_register_extensions_is_pd013(tmp_path: Path) -> None:
    root = make_plugin(tmp_path, entry="VALUE = 1\n")

    assert "PD013" in ids_of(doctor_engine.inspect_directory(root))


def test_sync_register_extensions_is_pd014(tmp_path: Path) -> None:
    root = make_plugin(tmp_path, entry="def register_extensions(registry):\n    return []\n")

    assert "PD014" in ids_of(doctor_engine.inspect_directory(root))


def test_async_register_extensions_is_not_flagged(tmp_path: Path) -> None:
    report = doctor_engine.inspect_directory(make_plugin(tmp_path))

    assert "PD014" not in ids_of(report)


def test_backend_entrypoint_missing_file_is_pd015(tmp_path: Path) -> None:
    entry = HEALTHY_ENTRY.replace('entrypoint="index.html"', 'entrypoint="missing.html"')
    root = make_plugin(tmp_path, entry=entry)

    assert "PD015" in ids_of(doctor_engine.inspect_directory(root))


@pytest.mark.parametrize("render_mode", ["none", "bundled"])
def test_non_iframe_contribution_does_not_require_asset_file(
    tmp_path: Path, render_mode: str
) -> None:
    """内置 video_duplex 就是 render_mode=none 且不带 entrypoint，不能误报 PD015。"""

    entry = (
        "from jiuwenswarm.extensions.sdk import FrontendContribution\n\n\n"
        "def contributions():\n"
        "    return (FrontendContribution(\n"
        "        id='video-live', nav_key='app:x', title='X',\n"
        f"        render_mode='{render_mode}', component='x'),)\n\n\n"
        "async def register_extensions(registry):\n"
        "    return []\n"
    )
    root = make_plugin(tmp_path, entry=entry, asset=False)

    ids = ids_of(doctor_engine.inspect_directory(root))

    assert "PD015" not in ids
    assert "PD026" not in ids


def test_iframe_contribution_without_entrypoint_is_not_pd015(tmp_path: Path) -> None:
    entry = (
        "from jiuwenswarm.extensions.sdk import FrontendContribution\n\n\n"
        "def contributions():\n"
        "    return (FrontendContribution(\n"
        "        id='page', nav_key='app:x', title='X'),)\n\n\n"
        "async def register_extensions(registry):\n"
        "    return []\n"
    )
    root = make_plugin(tmp_path, entry=entry, asset=False)

    assert "PD015" not in ids_of(doctor_engine.inspect_directory(root))


def test_duplicate_plugin_ids_are_pd016(tmp_path: Path) -> None:
    manifest = MANIFEST_ONLY.replace("__ID__", "dup-id")
    make_plugin(tmp_path, "dup-one", manifest=manifest, entry=None)
    make_plugin(tmp_path, "dup-two", manifest=manifest, entry=None)

    report = doctor_engine.scan_search_paths([tmp_path])

    assert len(report.plugins) == 2
    for plugin in report.plugins:
        assert ids_of(plugin) == {"PD016"}
    assert report.errors


def test_unique_plugin_ids_do_not_trigger_pd016(tmp_path: Path) -> None:
    make_plugin(tmp_path, "one-plugin")
    make_plugin(tmp_path, "two-plugin")

    report = doctor_engine.scan_search_paths([tmp_path])

    assert "PD016" not in scan_ids(report)


def test_entry_without_manifest_is_pd031(tmp_path: Path) -> None:
    root = tmp_path / "entry-only"
    root.mkdir()
    write(root, "extension.py", HEALTHY_ENTRY.replace("__ID__", "entry-only"))

    ids = ids_of(doctor_engine.inspect_directory(root))

    assert "PD031" in ids
    # 没有清单时不去猜 id 归属，PD017 只针对「有清单但没写 id」
    assert "PD017" not in ids
    # 入口里声明了 iframe 页面，但没有 frontend/dist 资源
    assert "PD015" in ids


def test_manifest_without_id_but_with_entry_is_pd017(tmp_path: Path) -> None:
    manifest = "name: NoId\nversion: 1.0.0\ndescription: d\nauthor: a\n"
    root = make_plugin(tmp_path, "no-id-backend", manifest=manifest)

    ids = ids_of(doctor_engine.inspect_directory(root))

    assert "PD017" in ids
    assert "PD031" not in ids


def test_class_plugin_id_mismatch_is_pd018(tmp_path: Path) -> None:
    entry = HEALTHY_ENTRY.replace('plugin_id = "__ID__"', 'plugin_id = "other-id"')
    root = make_plugin(tmp_path, entry=entry)

    assert "PD018" in ids_of(doctor_engine.inspect_directory(root))


# --------------------------------------------------------------------------
# PD019-PD023：元数据、版本、依赖、开关、导入
# --------------------------------------------------------------------------


def test_missing_metadata_fields_are_pd019(tmp_path: Path) -> None:
    manifest = "id: sparse\npackage_type: application\n"
    root = make_plugin(tmp_path, "sparse", manifest=manifest, entry=None)
    write(root, "frontend/dist/index.html", "<html></html>\n")
    manifest = manifest + "frontend:\n  - id: sparse-page\n    entrypoint: index.html\n"
    write(root, "extension.yaml", manifest)

    assert "PD019" in ids_of(doctor_engine.inspect_directory(root))


def test_min_version_newer_than_host_is_pd020(tmp_path: Path) -> None:
    manifest = HEALTHY_MANIFEST.replace('"0.1.0"', '"9.9.9"')
    root = make_plugin(tmp_path, manifest=manifest)

    report = doctor_engine.inspect_directory(root, running_version="0.2.5")

    assert "PD020" in ids_of(report)


def test_min_version_satisfied_is_not_pd020(tmp_path: Path) -> None:
    root = make_plugin(tmp_path)

    report = doctor_engine.inspect_directory(root, running_version="0.2.5")

    assert "PD020" not in ids_of(report)


def test_min_version_skipped_when_host_version_unknown(tmp_path: Path) -> None:
    manifest = HEALTHY_MANIFEST.replace('"0.1.0"', '"9.9.9"')
    root = make_plugin(tmp_path, manifest=manifest)

    assert "PD020" not in ids_of(doctor_engine.inspect_directory(root))


def test_declared_dependency_missing_is_pd021(tmp_path: Path) -> None:
    manifest = HEALTHY_MANIFEST.replace(
        "dependencies: {}", 'dependencies:\n  definitely-not-installed: ">=1.0.0"'
    )
    root = make_plugin(tmp_path, manifest=manifest)

    report = doctor_engine.inspect_directory(root, is_installed=lambda name: False)

    assert "PD021" in ids_of(report)


def test_declared_dependency_installed_is_not_pd021(tmp_path: Path) -> None:
    manifest = HEALTHY_MANIFEST.replace("dependencies: {}", "dependencies:\n  yaml: \"\"")
    root = make_plugin(tmp_path, manifest=manifest)

    report = doctor_engine.inspect_directory(root, is_installed=lambda name: True)

    assert "PD021" not in ids_of(report)


def test_requires_transport_string_is_pd022(tmp_path: Path) -> None:
    root = make_plugin(tmp_path)
    write(root, "extension.yaml", (root / "extension.yaml").read_text("utf-8") + 'requires_transport: "true"\n')

    assert "PD022" in ids_of(doctor_engine.inspect_directory(root))


def test_requires_transport_true_is_not_pd022(tmp_path: Path) -> None:
    root = make_plugin(tmp_path)
    write(root, "extension.yaml", (root / "extension.yaml").read_text("utf-8") + "requires_transport: true\n")

    assert "PD022" not in ids_of(doctor_engine.inspect_directory(root))


def test_requires_transport_false_is_not_pd022(tmp_path: Path) -> None:
    """显式 false 是合法声明（宿主 is True 判为 False 正是「不要求 transport」）。"""

    root = make_plugin(tmp_path)
    write(root, "extension.yaml", (root / "extension.yaml").read_text("utf-8") + "requires_transport: false\n")

    assert "PD022" not in ids_of(doctor_engine.inspect_directory(root))


def test_broken_manifest_does_not_pile_up_field_findings(tmp_path: Path) -> None:
    """清单读不出来时只报根因（PD003），不再叠加 PD017/PD019 这类字段缺失结论。"""

    root = make_plugin(tmp_path, manifest="id: [unclosed\n")

    ids = ids_of(doctor_engine.inspect_directory(root))

    assert "PD003" in ids
    assert "PD017" not in ids
    assert "PD019" not in ids


def test_plugin_id_prefers_the_class_attribute_over_the_manifest(tmp_path: Path) -> None:
    """宿主 registry 的取值顺序是「类属性 plugin_id → 清单 id」，体检报告要跟它一致。"""

    manifest = HEALTHY_MANIFEST.replace("__ID__", "manifest-id")
    entry = HEALTHY_ENTRY.replace("__ID__", "class-id")
    root = make_plugin(tmp_path, "id-mismatch", manifest=manifest, entry=entry)

    report = doctor_engine.inspect_directory(root)

    assert report.plugin_id == "class-id"


def test_relative_import_is_pd023(tmp_path: Path) -> None:
    entry = "from . import sibling\n\n\nasync def register_extensions(registry):\n    return []\n"
    root = make_plugin(tmp_path, entry=entry)

    assert "PD023" in ids_of(doctor_engine.inspect_directory(root))


def test_sibling_import_without_sys_path_is_pd023(tmp_path: Path) -> None:
    entry = "import sibling_module\n\n\nasync def register_extensions(registry):\n    return []\n"
    root = make_plugin(tmp_path, entry=entry)
    write(root, "sibling_module.py", "VALUE = 1\n")

    assert "PD023" in ids_of(doctor_engine.inspect_directory(root))


def test_sibling_import_with_sys_path_is_not_pd023(tmp_path: Path) -> None:
    entry = (
        "import sys\n"
        "from pathlib import Path\n\n"
        "_DIR = Path(__file__).resolve().parent\n"
        "if str(_DIR) not in sys.path:\n"
        "    sys.path.insert(0, str(_DIR))\n\n"
        "import sibling_module\n\n\n"
        "async def register_extensions(registry):\n"
        "    return []\n"
    )
    root = make_plugin(tmp_path, entry=entry)
    write(root, "sibling_module.py", "VALUE = 1\n")

    report = doctor_engine.inspect_directory(root)

    assert "PD023" not in ids_of(report)
    assert "PD013" not in ids_of(report)


def test_sibling_import_loaded_by_path_is_not_pd023(tmp_path: Path) -> None:
    """按文件路径加载同级模块也算处理了路径（PD023 不再只认改 sys.path）。"""

    entry = (
        "import importlib.util\n"
        "import sibling_module\n"
        "from pathlib import Path\n\n"
        "_DIR = Path(__file__).resolve().parent\n"
        "_spec = importlib.util.spec_from_file_location(\n"
        "    'sibling_module', _DIR / 'sibling_module.py'\n"
        ")\n\n\n"
        "async def register_extensions(registry):\n"
        "    return []\n"
    )
    root = make_plugin(tmp_path, entry=entry)
    write(root, "sibling_module.py", "VALUE = 1\n")

    assert "PD023" not in ids_of(doctor_engine.inspect_directory(root))


# --------------------------------------------------------------------------
# PD024-PD029：扫描深度、权限、动态前端、版本格式、无前端
# --------------------------------------------------------------------------


def test_nested_extension_root_is_pd024(tmp_path: Path) -> None:
    nested = tmp_path / "parent"
    write(nested, "inner-plugin/extension.yaml", HEALTHY_MANIFEST.replace("__ID__", "inner"))

    report = doctor_engine.scan_search_paths([tmp_path])

    nested_findings = [f for f in report.scan_findings if f.rule_id == "PD024"]
    assert nested_findings
    assert nested_findings[0].target.endswith("inner-plugin")
    assert report.plugins == ()


def test_flat_layout_has_no_pd024(tmp_path: Path) -> None:
    make_plugin(tmp_path)

    report = doctor_engine.scan_search_paths([tmp_path])

    assert "PD024" not in {f.rule_id for f in report.scan_findings}


def test_permissions_are_reported_as_pd025(tmp_path: Path) -> None:
    manifest = HEALTHY_MANIFEST + "permissions:\n  - camera\n"
    root = make_plugin(tmp_path, manifest=manifest)

    report = doctor_engine.inspect_directory(root)

    assert "PD025" in ids_of(report)
    assert "PD028" not in ids_of(report)


def test_unsupported_permission_is_pd028(tmp_path: Path) -> None:
    manifest = HEALTHY_MANIFEST + "permissions:\n  - camera\n  - outbound_network\n"
    root = make_plugin(tmp_path, manifest=manifest)

    assert "PD028" in ids_of(doctor_engine.inspect_directory(root))


def test_dynamic_frontend_contribution_is_pd026(tmp_path: Path) -> None:
    entry = (
        "from jiuwenswarm.extensions.sdk import FrontendContribution\n\n"
        "PAGE = 'index.html'\n\n"
        "def contributions():\n"
        "    return (FrontendContribution(\n"
        "        id='dyn', nav_key='app:dyn', title='Dyn',\n"
        "        render_mode='iframe', entrypoint=PAGE),)\n\n\n"
        "async def register_extensions(registry):\n"
        "    return []\n"
    )
    root = make_plugin(tmp_path, entry=entry)

    assert "PD026" in ids_of(doctor_engine.inspect_directory(root))


def test_non_numeric_version_is_pd027(tmp_path: Path) -> None:
    manifest = HEALTHY_MANIFEST.replace("version: 1.2.3", 'version: "abc"')
    root = make_plugin(tmp_path, manifest=manifest)

    assert "PD027" in ids_of(doctor_engine.inspect_directory(root))


def test_non_mapping_config_schema_is_pd030(tmp_path: Path) -> None:
    manifest = HEALTHY_MANIFEST + "config_schema: []\n"
    root = make_plugin(tmp_path, manifest=manifest)

    assert "PD030" in ids_of(doctor_engine.inspect_directory(root))


def test_mapping_config_schema_is_not_pd030(tmp_path: Path) -> None:
    manifest = HEALTHY_MANIFEST + "config_schema:\n  type: object\n"
    root = make_plugin(tmp_path, manifest=manifest)

    assert "PD030" not in ids_of(doctor_engine.inspect_directory(root))


def test_backend_without_frontend_contribution_is_pd029(tmp_path: Path) -> None:
    entry = "async def register_extensions(registry):\n    return []\n"
    root = make_plugin(tmp_path, entry=entry, asset=False)

    report = doctor_engine.inspect_directory(root)

    assert "PD029" in ids_of(report)
    assert "PD015" not in ids_of(report)


def test_plugin_with_frontend_assets_is_not_pd029(tmp_path: Path) -> None:
    report = doctor_engine.inspect_directory(make_plugin(tmp_path))

    assert "PD029" not in ids_of(report)


# --------------------------------------------------------------------------
# 扫描、评分与输出
# --------------------------------------------------------------------------


def test_scan_records_missing_search_paths(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    report = doctor_engine.scan_search_paths([tmp_path, missing])

    assert report.missing_paths == (str(missing),)
    assert str(tmp_path) in report.search_paths


def test_scan_dedupes_search_paths(tmp_path: Path) -> None:
    make_plugin(tmp_path)

    report = doctor_engine.scan_search_paths([tmp_path, str(tmp_path), str(tmp_path).upper()])

    assert len(report.search_paths) == 1
    assert len(report.plugins) == 1


def test_scan_ignores_non_directory_entries(tmp_path: Path) -> None:
    write(tmp_path, "loose-extension.yaml", "id: loose\n")

    report = doctor_engine.scan_search_paths([tmp_path])

    assert report.plugins == ()
    assert report.scan_findings == ()


def test_scan_counts_errors_and_score(tmp_path: Path) -> None:
    make_plugin(tmp_path)

    report = doctor_engine.scan_search_paths([tmp_path], running_version="0.2.5")

    assert report.running_version == "0.2.5"
    assert report.score == 100
    assert report.errors == ()
    assert report.to_dict()["scanned"] == 1


def test_score_penalties_and_floor() -> None:
    def finding(rule_id: str, severity: str) -> doctor_engine.Finding:
        return doctor_engine.Finding(
            rule_id=rule_id, severity=severity, target="x", message="m", hint=""
        )

    assert doctor_engine._score_of([finding("X", "error")]) == 80
    assert doctor_engine._score_of([finding("X", "warning")]) == 93
    assert doctor_engine._score_of([finding("X", "info")]) == 98
    assert doctor_engine._score_of([finding("X", "error")] * 6) == 0


def test_report_payload_is_json_serializable(tmp_path: Path) -> None:
    make_plugin(tmp_path)
    report = doctor_engine.scan_search_paths([tmp_path], running_version="0.2.5")

    encoded = json.dumps(report.to_dict(), ensure_ascii=False)

    assert "good-plugin" in encoded
    payload = json.loads(encoded)
    assert payload["plugins"][0]["status"] == "ok"
    assert payload["plugins"][0]["findings"] == []


def test_markdown_report_has_sections_and_escapes_pipes(tmp_path: Path) -> None:
    root = make_plugin(tmp_path, entry="def register_extensions(registry):\n    return a | b\n")
    report = doctor_engine.scan_search_paths([tmp_path])

    markdown = doctor_engine.render_markdown(report)

    assert markdown.startswith("# 应用插件体检报告")
    assert "## 插件" in markdown
    assert "## 明细" in markdown
    assert "good-plugin" in markdown
    assert "| PD014 |" in markdown


def test_markdown_lists_missing_paths(tmp_path: Path) -> None:
    report = doctor_engine.scan_search_paths([tmp_path / "nope"])

    markdown = doctor_engine.render_markdown(report)

    assert "## 不存在的搜索目录" in markdown
    assert "nope" in markdown


def test_version_helpers() -> None:
    assert doctor_engine.parse_version("1.2.3") == (1, 2, 3)
    assert doctor_engine.parse_version("0.2.5.beta1") == (0, 2, 5)
    assert doctor_engine.parse_version("") == ()
    assert doctor_engine.parse_version(None) == ()
    assert doctor_engine.version_is_newer("1.0.0", "0.9.9") is True
    assert doctor_engine.version_is_newer("0.2.5", "0.2.5") is False
    assert doctor_engine.version_is_newer("abc", "0.2.5") is False


def test_extension_root_detection(tmp_path: Path) -> None:
    assert doctor_engine.is_extension_root(make_plugin(tmp_path)) is True
    plain = tmp_path / "plain"
    plain.mkdir()
    assert doctor_engine.is_extension_root(plain) is False
    assert doctor_engine.is_extension_root(tmp_path / "absent") is False


def test_frontend_asset_root(tmp_path: Path) -> None:
    assert doctor_engine.frontend_asset_root(make_plugin(tmp_path)) is not None
    plain = tmp_path / "plain2"
    plain.mkdir()
    assert doctor_engine.frontend_asset_root(plain) is None


# --------------------------------------------------------------------------
# 自我体检（dogfooding）：规则不能误伤真实的内置插件
# --------------------------------------------------------------------------


def test_plugin_doctor_itself_is_clean() -> None:
    from jiuwenswarm.extensions.plugin_doctor import extension as extension_module

    root = Path(extension_module.__file__).resolve().parent

    report = doctor_engine.inspect_directory(root, running_version="0.2.5")

    assert report.findings == (), [f.to_dict() for f in report.findings]
    assert report.plugin_id == "plugin-doctor"
    assert report.score == 100


def test_builtin_extensions_have_no_error_findings() -> None:
    extensions_dir = Path(jiuwenswarm.__file__).resolve().parent / "extensions"

    report = doctor_engine.scan_search_paths([extensions_dir], running_version="0.2.5")

    error_findings = [
        (plugin.plugin_id or plugin.root, finding.to_dict())
        for plugin in report.plugins
        for finding in plugin.errors
    ]
    assert error_findings == []
    scanned_ids = {plugin.plugin_id for plugin in report.plugins}
    assert {"agentos_router", "agent_client", "video-duplex", "plugin-doctor"} <= scanned_ids


@pytest.mark.parametrize(
    "name", ["plugin_doctor", "agentos", "video_duplex"]
)
def test_builtin_plugin_directories_are_recognized(name: str) -> None:
    extensions_dir = Path(jiuwenswarm.__file__).resolve().parent / "extensions"

    assert doctor_engine.is_extension_root(extensions_dir / name) is True
