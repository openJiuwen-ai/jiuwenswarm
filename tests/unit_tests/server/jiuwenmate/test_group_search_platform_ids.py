"""Platform chat IDs resolve only within this Agent's authorized source set."""

from types import SimpleNamespace

import pytest

from openjiuwen.harness.persistent_agent import GroupMessageQuery
from jiuwenswarm.server.jiuwenmate.ports.group_chat import HostGroupChatPort, _SearchUnavailable


def authorized():
    return {"grp_one": {"name": "Group one", "group": SimpleNamespace(platform_chat_id="oc_one")},
            "grp_two": {"name": "Group two", "group": SimpleNamespace(platform_chat_id="oc_two")}}


@pytest.mark.parametrize("types", [("messages",), ("files",), ("online_docs",)])
def test_group_filter_is_a_bounded_search_criterion(types):
    query = GroupMessageQuery(group_ids=("oc_one",), content_types=types)
    assert HostGroupChatPort._resolve_targets(query, authorized()) == (("grp_one",), False)


def test_platform_ids_remain_scoped_and_ambiguous_ids_require_canonical_id():
    sources = authorized()
    with pytest.raises(_SearchUnavailable):
        HostGroupChatPort._resolve_targets(GroupMessageQuery(query="file", group_ids=("oc_private",)), sources)
    sources["grp_other"] = {"name": "Other", "group": SimpleNamespace(platform_chat_id="oc_one")}
    with pytest.raises(_SearchUnavailable):
        HostGroupChatPort._resolve_targets(GroupMessageQuery(query="file", group_ids=("oc_one",)), sources)
    assert HostGroupChatPort._resolve_targets(GroupMessageQuery(group_ids=("grp_one",)), sources) == (("grp_one",), False)


@pytest.mark.parametrize("types", [("messages",), ("files",), ("online_docs",)])
def test_platform_filter_disambiguates_same_group_name_and_external_id(types):
    sources = {platform: {"name": "项目群", "group": SimpleNamespace(
        platform=platform, platform_chat_id="same-id",
    )} for platform in ("feishu", "dingtalk")}
    query = GroupMessageQuery(group_names=("项目群",), platforms=("dingtalk",), content_types=types)
    assert HostGroupChatPort._resolve_targets(query, sources) == (("dingtalk",), False)
    with pytest.raises(_SearchUnavailable):
        HostGroupChatPort._resolve_targets(query.model_copy(update={"group_names": (), "group_ids": ("feishu",)}), sources)


def test_ambiguous_cross_platform_name_shows_platform_with_each_internal_id():
    sources = {f"grp_{platform}": {"name": "项目群", "group": SimpleNamespace(
        platform=platform, platform_chat_id="same-id",
    )} for platform in ("feishu", "dingtalk")}
    with pytest.raises(_SearchUnavailable) as error:
        HostGroupChatPort._resolve_targets(GroupMessageQuery(group_names=("项目群",)), sources)
    assert "grp_feishu (feishu)" in str(error.value)
    assert "grp_dingtalk (dingtalk)" in str(error.value)
