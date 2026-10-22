"""Automatic CLI capabilities must not publish unrelated colleague drafts."""

from dataclasses import replace
import json

import pytest

from tests.unit_tests.server.jiuwenmate.test_resource_assembly import MapCatalog, service, settings, write_skill
from jiuwenswarm.server.jiuwenmate.models import PersistentAgentError
from jiuwenswarm.server.jiuwenmate.spec_provider import publish_spec


def prepare(tmp_path):
    host = service(tmp_path, MapCatalog(), iter(["agent-a"]))
    host.create_colleague("local", settings())
    skill = write_skill(tmp_path / "library/lark-doc", "lark-doc", "Create documents with lark-cli.")
    return host, skill


def refresh(host, skill):
    assert hasattr(host, "refresh_feishu_capability"), "CLI capability publication is missing"
    return host.refresh_feishu_capability("local", "agent-a", skill_directories=(skill,))


def test_cli_refresh_keeps_unpublished_settings(tmp_path):
    host, skill = prepare(tmp_path)
    old = host.current_snapshot("local", "agent-a")
    draft = replace(host.read_settings("local", "agent-a"), name="未发布的名字", persona="未发布的人设")
    host.update_settings("local", "agent-a", draft)
    refresh(host, skill)
    current = host.current_snapshot("local", "agent-a").load_spec()
    assert current.card.name != "未发布的名字"
    assert "未发布的人设" not in current.system_prompt
    assert host.read_settings("local", "agent-a").name == "未发布的名字"
    assert host.get_colleague("local", "agent-a").has_unpublished_changes
    assert host.snapshot("local", "agent-a", old.config_version) == old
    assert "lark-doc" in current.skills


def test_identical_cli_assets_do_not_create_another_version(tmp_path):
    host, skill = prepare(tmp_path)
    first = refresh(host, skill)
    second = refresh(host, skill)
    assert first.config_version == second.config_version


@pytest.mark.parametrize("nested", [True, False])
def test_refresh_removes_retired_calendar_without_publishing_draft(tmp_path, nested):
    from jiuwenswarm.server.jiuwenmate.bash_runtime import BASH_PROMPT

    host, skill = prepare(tmp_path)
    repo = host._repository
    current = host.current_snapshot("local", "agent-a")
    fields = json.loads(current.spec_json)
    retired = {"type": "jiuwenmate.personal_calendar", "params": {}}
    plugin = {"type": "test.selected_plugin", "params": {"selection": "keep"}}
    fields["tools"][0]["params"]["tools"].append(plugin)
    if nested:
        fields["tools"][0]["params"]["tools"].append(retired)
    else:
        fields["tools"].append(retired)
    fields["system_prompt"] = fields["system_prompt"].replace(
        BASH_PROMPT,
        "\n\n## 命令使用约定（非技术隔离）\n\n旧版只能调用 query_my_calendar。"
        "\n\n## 用户补充约定\n\n保留这个用户提示段。",
    )
    legacy = publish_spec("agent-a", repo.next_version("local", "agent-a"), fields)
    repo.save_capability_version("local", legacy, previous_version=current.config_version, published_at=host._clock())
    draft = replace(host.read_settings("local", "agent-a"), name="未发布的名字", persona="未发布的人设")
    host.update_settings("local", "agent-a", draft)

    refreshed = refresh(host, skill)

    snapshot = host.current_snapshot("local", "agent-a")
    spec = snapshot.load_spec()
    assert snapshot.config_version != legacy.config_version
    assert [tool.type for tool in spec.tools] == ["jiuwenmate.bash"]
    assert spec.tools[0].params["tools"] == [{"type": "core.web_fetch"}, plugin]
    assert "query_my_calendar" not in spec.system_prompt
    assert "保留这个用户提示段。" in spec.system_prompt
    assert "未发布的人设" not in spec.system_prompt
    assert spec.card.name != draft.name
    assert "lark-doc" in spec.skills
    assert host.read_settings("local", "agent-a").name == draft.name
    assert host.get_colleague("local", "agent-a").has_unpublished_changes
    assert host.snapshot("local", "agent-a", legacy.config_version) == legacy
    assert refresh(host, skill).config_version == refreshed.config_version


def test_concurrent_capability_refresh_conflicts_atomically(tmp_path):
    host, skill = prepare(tmp_path)
    repo = host._repository
    old = host.current_snapshot("local", "agent-a")
    refresh(host, skill)
    current = host.current_snapshot("local", "agent-a")
    stale = publish_spec("agent-a", repo.next_version("local", "agent-a"), json.loads(old.spec_json))
    with pytest.raises(PersistentAgentError) as error:
        repo.save_capability_version("local", stale, previous_version=old.config_version, published_at=host._clock())
    assert error.value.code == "CONFLICT"
    assert host.current_snapshot("local", "agent-a") == current


def test_fresh_publication_keeps_automatic_feishu_skills(tmp_path, monkeypatch):
    host, skill = prepare(tmp_path)
    refresh(host, skill)
    monkeypatch.setattr(host.cli_runtime, "skill_directories", lambda *_: (skill,))
    host.update_settings("local", "agent-a", replace(host.read_settings("local", "agent-a"), name="已发布新名字"))
    host.publish("local", "agent-a")
    assert "lark-doc" in host.current_snapshot("local", "agent-a").load_spec().skills
    monkeypatch.setattr(host.cli_runtime, "skill_directories", lambda *_: ())
    host.refresh_feishu_capability("local", "agent-a", skill_directories=())
    assert "lark-doc" not in host.current_snapshot("local", "agent-a").load_spec().skills


def test_refresh_upgrades_an_existing_publication_without_publishing_its_draft(tmp_path, monkeypatch):
    from jiuwenswarm.server.jiuwenmate import service as service_module

    with monkeypatch.context() as old_host:
        old_host.setattr(service_module, "with_bash_tool", lambda fields: fields)
        old_host.setattr(service_module, "BASH_PROMPT", "")
        host, skill = prepare(tmp_path)
    old = host.current_snapshot("local", "agent-a")
    assert not old.load_spec().tools
    draft = replace(host.read_settings("local", "agent-a"), name="未发布的名字")
    host.update_settings("local", "agent-a", draft)
    refresh(host, skill)
    spec = host.current_snapshot("local", "agent-a").load_spec()
    assert any(tool.type == "jiuwenmate.bash" for tool in spec.tools or [])
    assert spec.card.name != "未发布的名字"
    assert host.get_colleague("local", "agent-a").has_unpublished_changes
