# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""agent_template / agent_group 适配器单测。

走真实扩展包管理存储链路（全局 conftest 已把数据目录隔离到测试临时
目录）：定位、导入落库、冲突判定、市场登记均为真实行为。仅置换
openjiuwen 的专家团包校验器——其结构校验由 openjiuwen 自身测试覆盖，
不属于本模块的被测行为。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime import extension_package_manager as package_manager
from jiuwenswarm.server.runtime.assets import registry, service
from jiuwenswarm.server.runtime.assets.adapters import agent as adapters
from jiuwenswarm.server.runtime.assets.contract import AssetKind
from jiuwenswarm.server.runtime.assets.errors import AssetError, AssetErrorCode
from jiuwenswarm.server.runtime.assets.staging import assets_upload_root

# ------------------------------------------------------------------- fixtures


@pytest.fixture
def isolated_registry(monkeypatch):
    monkeypatch.setattr(registry, "_ADAPTERS", {})
    return registry


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    root = tmp_path / "ws"
    root.mkdir()
    monkeypatch.setattr("jiuwenswarm.common.utils.get_workspace_dir", lambda: root)
    return root


@pytest.fixture
def template_adapter():
    return adapters.AgentTemplateAdapter()


@pytest.fixture
def group_adapter():
    return adapters.AgentGroupAdapter()


@pytest.fixture
def stub_group_validator(monkeypatch):
    """置换 openjiuwen 专家团包校验器；导入与安装链路各调用一次。"""
    from openjiuwen.agent_teams import experts

    monkeypatch.setattr(experts, "load_agent_group_package", lambda pkg_dir: None)


# -------------------------------------------------------------------- helpers


def _write_manifest(pkg_dir: Path, manifest: dict) -> Path:
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return pkg_dir


def _template_manifest(name: str, **extra) -> dict:
    manifest = {
        "name": name,
        "package_type": "agent_template",
        "description": f"{name} 描述",
        "persona": {"dir": "./persona"},
    }
    manifest.update(extra)
    return manifest


def _write_persona(pkg_dir: Path) -> None:
    persona = pkg_dir / "persona"
    persona.mkdir(parents=True, exist_ok=True)
    (persona / "AGENTS.md").write_text(f"# {pkg_dir.name}\n", encoding="utf-8")


def _seed_template(name: str, *, built_in: bool = False, **manifest_extra) -> Path:
    """在包管理存储内种一个专家包（无 README，与创建链路产物一致）。"""
    root = package_manager.get_local_root("agent_templates")
    if built_in:
        root = root.parent / "built_in"
    pkg_dir = _write_manifest(root / name, _template_manifest(name, **manifest_extra))
    _write_persona(pkg_dir)
    return pkg_dir


def _build_template_package(base: Path, name: str, **manifest_extra) -> Path:
    """构造待导入的专家包目录（临时目录语义：导入后即销毁）。"""
    pkg_dir = _write_manifest(base, _template_manifest(name, **manifest_extra))
    _write_persona(pkg_dir)
    return pkg_dir


def _build_group_package(
    base: Path,
    name: str,
    *,
    display_name: str | None = None,
    member_connectors: tuple[str, ...] = (),
) -> Path:
    """构造最小专家团包：leader + 一个成员，成员包为完整专家包结构。"""
    members = ("leader", "member.a")
    member_templates = {"leader": f"{name}.leader", "member.a": f"{name}.member"}
    pkg_dir = _write_manifest(
        base,
        {
            "name": name,
            "package_type": "agent_group",
            "display_name": display_name or f"{name} 显示名",
            "description": f"{name} 描述",
            "agents": list(members),
            "member_templates": member_templates,
        },
    )
    for member in members:
        extra = (
            {"mcps": [{"connector": connector} for connector in member_connectors]}
            if member == "member.a" and member_connectors
            else {}
        )
        member_dir = _write_manifest(
            pkg_dir / "agents" / member,
            _template_manifest(member_templates[member], **extra),
        )
        _write_persona(member_dir)
    (pkg_dir / "agents" / "leader" / "AGENTS.md").write_text("# leader\n", encoding="utf-8")
    return pkg_dir


# ------------------------------------------------------------------- registry


def test_register_agent_adapters_idempotent(isolated_registry):
    adapters.register_agent_adapters()
    template = isolated_registry.get_asset_adapter(AssetKind.AGENT_TEMPLATE)
    group = isolated_registry.get_asset_adapter(AssetKind.AGENT_GROUP)
    assert isinstance(template, adapters.AgentTemplateAdapter)
    assert isinstance(group, adapters.AgentGroupAdapter)
    # 重复注册幂等：既不抛错也不替换既有实例
    adapters.register_agent_adapters()
    assert isolated_registry.get_asset_adapter(AssetKind.AGENT_TEMPLATE) is template
    assert isolated_registry.get_asset_adapter(AssetKind.AGENT_GROUP) is group
    assert isolated_registry.registered_asset_kinds() == (
        AssetKind.AGENT_TEMPLATE,
        AssetKind.AGENT_GROUP,
    )


# --------------------------------------------------------------------- sniff


def test_sniff_reads_root_manifest_only(tmp_path, template_adapter, group_adapter):
    template_dir = _build_template_package(tmp_path / "t", "expert.t")
    group_dir = _build_group_package(tmp_path / "g", "team.g")
    plain_dir = tmp_path / "plain"
    plain_dir.mkdir()

    assert template_adapter.sniff_package(template_dir)
    assert not group_adapter.sniff_package(template_dir)
    assert group_adapter.sniff_package(group_dir)
    # 专家团内嵌成员专家包的 manifest 不参与嗅探
    assert not template_adapter.sniff_package(group_dir)
    assert not template_adapter.sniff_package(plain_dir)
    assert not group_adapter.sniff_package(plain_dir)


# ------------------------------------------------------- template locate


def test_template_locate_export_resolves_local_package(template_adapter):
    pkg_dir = _seed_template("expert.a")
    source = template_adapter.locate_export("expert.a", None)
    assert source.source_dir == pkg_dir
    assert source.asset_id == "expert.a"
    assert source.version is None
    assert source.exclude_paths == ()
    # manifest 无 display_name 时回退 name（与包目录名一致）
    assert source.display_name == "expert.a"


def test_template_locate_export_display_name_prefers_manifest(template_adapter):
    # 本地创建的包目录名带 -<uuid> 后缀：下载文件名主干取 manifest 显示名
    _seed_template(
        "expert.dn-b4e49ef6-432b-4939-8ba9-d4765440ff5d", display_name="测试显示名"
    )
    source = template_adapter.locate_export(
        "expert.dn-b4e49ef6-432b-4939-8ba9-d4765440ff5d", None
    )
    assert source.display_name == "测试显示名"


def test_template_locate_export_display_name_multilang_dict(template_adapter):
    # display_name 多语言 dict 形态（专家包创建链路写法）：zh 优先，其次 en
    _seed_template(
        "expert.i18n", display_name={"zh": "动态壁纸管家", "en": "Dynamic Wallpaper"}
    )
    assert template_adapter.locate_export("expert.i18n", None).display_name == "动态壁纸管家"

    _seed_template("expert.i18n.en", display_name={"en": "Dynamic Wallpaper"})
    assert (
        template_adapter.locate_export("expert.i18n.en", None).display_name
        == "Dynamic Wallpaper"
    )

    # dict 值全部无效 → 回退 name（模板 fixture 中即包目录名）
    _seed_template("expert.i18n.bad", display_name={"zh": "", "en": 3})
    assert (
        template_adapter.locate_export("expert.i18n.bad", None).display_name
        == "expert.i18n.bad"
    )


def test_template_locate_export_resolves_built_in_package(template_adapter):
    pkg_dir = _seed_template("expert.builtin", built_in=True)
    source = template_adapter.locate_export("expert.builtin", None)
    assert source.source_dir == pkg_dir


def test_template_locate_export_version(template_adapter):
    _seed_template("expert.v", version="1.2.0")
    assert template_adapter.locate_export("expert.v", "1.2.0").version == "1.2.0"
    # 未指定版本：导出当前版本，文件名携带实际版本号
    assert template_adapter.locate_export("expert.v", None).version == "1.2.0"
    with pytest.raises(AssetError) as excinfo:
        template_adapter.locate_export("expert.v", "9.9.9")
    assert excinfo.value.code == AssetErrorCode.NOT_FOUND


def test_template_locate_export_versionless_rejects_version_param(template_adapter):
    # 本地创建的包无版本：带版本请求视为指定版本不存在
    _seed_template("expert.nv")
    with pytest.raises(AssetError) as excinfo:
        template_adapter.locate_export("expert.nv", "1.0")
    assert excinfo.value.code == AssetErrorCode.NOT_FOUND


def test_template_locate_missing_is_not_found(template_adapter):
    with pytest.raises(AssetError) as excinfo:
        template_adapter.locate_export("ghost", None)
    assert excinfo.value.code == AssetErrorCode.NOT_FOUND


# ------------------------------------------------------- template import


def test_template_import_roundtrip(tmp_path, template_adapter):
    pkg = _build_template_package(
        tmp_path / "pkg", "expert.new", mcps=[{"connector": "demo-crm"}]
    )
    outcome = template_adapter.import_package(pkg)
    assert outcome.asset_id == "expert.new"
    # 无 MCP 状态记录 → 视为未就绪，随导入结果返回
    assert outcome.pending_connectors == ("demo-crm",)

    installed = package_manager.get_local_root("agent_templates") / "expert.new"
    assert (installed / "manifest.json").is_file()
    assert (installed / "persona" / "AGENTS.md").is_file()
    # 导入链路强制 README 而创建链路不产出：按 manifest 摘抄补齐（标题+描述）
    readme = (installed / "README.md").read_text(encoding="utf-8")
    assert readme == "# expert.new\n\nexpert.new 描述\n"
    # 导入后可再导出（往返闭环）
    assert template_adapter.locate_export("expert.new", None).source_dir == installed


def test_template_import_existing_readme_is_kept(tmp_path, template_adapter):
    pkg = _build_template_package(tmp_path / "pkg", "expert.doc")
    (pkg / "README.md").write_text("# 自带说明\n", encoding="utf-8")
    template_adapter.import_package(pkg)
    installed = package_manager.get_local_root("agent_templates") / "expert.doc"
    assert (installed / "README.md").read_text(encoding="utf-8") == "# 自带说明\n"


def test_template_export_generates_readme_when_missing(template_adapter):
    """本地创建包无 README：导出随包补 manifest 摘抄，不回写安装目录。"""
    pkg = _seed_template("expert.plain")
    source = template_adapter.locate_export("expert.plain", None)
    assert source.extra_files == {
        "README.md": "# expert.plain\n\nexpert.plain 描述\n".encode("utf-8")
    }
    assert not (pkg / "README.md").exists()


def test_template_export_readme_prefers_display_fields(template_adapter):
    """display_name / display_description 多语言 dict 形态按 zh 优先摘抄。"""
    _seed_template(
        "expert.i18n",
        display_name={"zh": "示例专家", "en": "Demo Expert"},
        display_description={"zh": "示例描述", "en": "demo"},
    )
    source = template_adapter.locate_export("expert.i18n", None)
    assert source.extra_files["README.md"].decode("utf-8") == "# 示例专家\n\n示例描述\n"


def test_template_export_keeps_existing_readme(template_adapter):
    """已有 README 的包（Hub 安装、用户自建）不补生成文件。"""
    pkg = _seed_template("expert.readme")
    (pkg / "README.md").write_text("# 自带说明\n", encoding="utf-8")
    source = template_adapter.locate_export("expert.readme", None)
    assert dict(source.extra_files) == {}


def test_template_import_conflicts(tmp_path, template_adapter):
    _seed_template("expert.dup")
    with pytest.raises(AssetError) as excinfo:
        template_adapter.import_package(_build_template_package(tmp_path / "p1", "expert.dup"))
    assert excinfo.value.code == AssetErrorCode.ALREADY_EXISTS

    _seed_template("expert.bi", built_in=True)
    with pytest.raises(AssetError) as excinfo:
        template_adapter.import_package(_build_template_package(tmp_path / "p2", "expert.bi"))
    assert excinfo.value.code == AssetErrorCode.BUILTIN_READ_ONLY


@pytest.mark.parametrize(
    "build",
    [
        pytest.param(lambda base: _write_manifest(base, _template_manifest("bad.layout")), id="缺 persona"),
        pytest.param(lambda base: base.mkdir(parents=True) or base, id="缺 manifest"),
        pytest.param(
            lambda base: _write_manifest(base, {"name": "w", "package_type": "agent_group"}),
            id="package_type 不匹配",
        ),
    ],
)
def test_template_import_rejects_bad_packages(tmp_path, template_adapter, build):
    pkg = build(tmp_path / "bad")
    with pytest.raises(AssetError) as excinfo:
        template_adapter.import_package(Path(pkg))
    assert excinfo.value.code == AssetErrorCode.INVALID_PACKAGE


# ------------------------------------------------------------ group import


def test_group_import_renames_and_installs(tmp_path, group_adapter, stub_group_validator):
    pkg = _build_group_package(tmp_path / "upload" / "team.a", "team.a", member_connectors=("demo-connector",))
    # 模拟 zip 根目录名与 manifest name 不一致：导入前重命名为规范名
    odd_root = pkg.with_name("odd-root")
    pkg.rename(odd_root)

    outcome = group_adapter.import_package(odd_root)
    assert outcome.asset_id == "team.a"
    assert outcome.pending_connectors == ("demo-connector",)

    installed = package_manager.get_local_root("agent_groups") / "team.a"
    assert (installed / "manifest.json").is_file()
    assert (installed / "agents" / "leader" / "AGENTS.md").is_file()
    assert not odd_root.exists()
    # 导入即安装：resolve_agent_group_dir 仅对已安装专家团成功
    assert package_manager.resolve_agent_group_dir("team.a") == installed


def test_group_import_duplicate_is_conflict(tmp_path, group_adapter, stub_group_validator):
    _build_group_package(tmp_path / "first" / "team.b", "team.b")
    group_adapter.import_package(tmp_path / "first" / "team.b")
    second = _build_group_package(
        tmp_path / "second" / "team.b", "team.b", display_name="另一个显示名"
    )
    with pytest.raises(AssetError) as excinfo:
        group_adapter.import_package(second)
    assert excinfo.value.code == AssetErrorCode.ALREADY_EXISTS


def test_group_import_rejects_unsafe_name(tmp_path, group_adapter):
    pkg = _write_manifest(
        tmp_path / "evil", {"name": "../evil", "package_type": "agent_group"}
    )
    with pytest.raises(AssetError) as excinfo:
        group_adapter.import_package(pkg)
    assert excinfo.value.code == AssetErrorCode.INVALID_PACKAGE
    # 重命名未发生：临时目录保持原样，随工作目录销毁
    assert pkg.exists()

    missing_name = _write_manifest(tmp_path / "noname", {"package_type": "agent_group"})
    with pytest.raises(AssetError) as excinfo:
        group_adapter.import_package(missing_name)
    assert excinfo.value.code == AssetErrorCode.INVALID_PACKAGE


# ----------------------------------------------------------- group locate


def test_group_locate_export(tmp_path, group_adapter, stub_group_validator):
    _build_group_package(tmp_path / "g" / "team.c", "team.c")
    group_adapter.import_package(tmp_path / "g" / "team.c")
    source = group_adapter.locate_export("team.c", None)
    assert source.asset_id == "team.c"
    assert (source.source_dir / "manifest.json").is_file()
    # 专家团 manifest 自带 display_name：优先于包目录名
    assert source.display_name == "team.c 显示名"


def test_group_locate_missing_is_not_found(group_adapter):
    with pytest.raises(AssetError) as excinfo:
        group_adapter.locate_export("ghost.team", None)
    assert excinfo.value.code == AssetErrorCode.NOT_FOUND


def test_group_locate_tenant_required_stays_internal(group_adapter, monkeypatch):
    # 企业租户未绑定属环境问题：不得伪装成 NOT_FOUND，交由入口收敛为内部错误
    def _raise_tenant(_name):
        raise package_manager.AgentGroupPackageError(
            "agent_group requires a bound tenant", "AGENT_GROUP_TENANT_REQUIRED"
        )

    monkeypatch.setattr(package_manager, "resolve_agent_group_publish_dir", _raise_tenant)
    with pytest.raises(package_manager.AgentGroupPackageError):
        group_adapter.locate_export("team.x", None)


def test_group_import_tenant_required_stays_internal(tmp_path, group_adapter, monkeypatch):
    pkg = _build_group_package(tmp_path / "t" / "team.y", "team.y")

    def _raise_tenant(_params):
        raise package_manager.AgentGroupPackageError(
            "agent_group requires a bound tenant", "AGENT_GROUP_TENANT_REQUIRED"
        )

    monkeypatch.setattr(package_manager, "import_agent_group", _raise_tenant)
    with pytest.raises(package_manager.AgentGroupPackageError):
        group_adapter.import_package(pkg)


# --------------------------------------------------------- service wiring


def test_service_export_import_roundtrip_template(workspace, isolated_registry, monkeypatch):
    """编排层全链路：导出打包 → 上传暂存 → 嗅探导入。"""
    captured: dict = {}

    def fake_download_info(file_path, file_name, **kwargs):
        captured.update(file_path=file_path, file_name=file_name)
        return {"download_url": "/file-api/download?token=fake"}

    monkeypatch.setattr(service, "build_file_download_info", fake_download_info)
    _seed_template("expert.rt")

    # 注册表被隔离清空后显式装配真实适配器（生产路径为 service 模块
    # 加载期经 adapters.install_default_adapters 统一安装）
    adapters.register_agent_adapters()
    payload = service.export_asset({"kind": "agent_template", "local_id": "expert.rt"})
    assert payload["filename"] == "expert.rt.zip"
    assert captured["file_name"] == "expert.rt.zip"
    zip_path = Path(captured["file_path"])

    staged_dir = assets_upload_root() / "roundtrip"
    staged_dir.mkdir(parents=True, exist_ok=True)
    staged = staged_dir / "expert.rt.zip"
    shutil.copyfile(zip_path, staged)

    # 本地同 id 未删除：按契约拒绝覆盖
    with pytest.raises(AssetError) as excinfo:
        service.import_asset({"path": str(staged)})
    assert excinfo.value.code == AssetErrorCode.ALREADY_EXISTS

    # 删除本地副本后重导：缺省 kind 由嗅探判定，导出时生成的 README 随包带回
    shutil.rmtree(package_manager.get_local_root("agent_templates") / "expert.rt")
    payload = service.import_asset({"path": str(staged)})
    assert payload == {"kind": "agent_template", "id": "expert.rt"}
    reinstalled = package_manager.get_local_root("agent_templates") / "expert.rt"
    assert (reinstalled / "persona" / "AGENTS.md").is_file()
    assert (reinstalled / "README.md").read_text(encoding="utf-8") == (
        "# expert.rt\n\nexpert.rt 描述\n"
    )
