# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import pytest

from jiuwenswarm.agents.harness.flash.prompt.offline import assemble


@pytest.mark.parametrize("language", ["cn", "en"])
def test_tool_dedup_scope_is_current_user_request(language):
    selection = next(section for section in assemble(language) if section.name == "tool_selection")
    text = selection.render(language)
    if language == "cn":
        assert "工具调用去重仅限本次用户请求内" in text
        assert "即使工具和参数与历史请求相同" in text
        assert "没有状态变化" in text
    else:
        assert "Deduplicate tool calls only within the current user request" in text
        assert "identical tools and arguments from earlier requests" in text
        assert "no state change" in text
