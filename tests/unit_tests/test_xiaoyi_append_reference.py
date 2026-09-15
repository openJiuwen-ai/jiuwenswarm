import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from jiuwenswarm.agents.harness.common.tools.xiaoyi_append_reference import (
    XiaoyiAppendReferenceToolkit,
)
from jiuwenswarm.common.xiaoyi_reference import (
    build_a2a_reference_items,
    coerce_references,
)


def test_coerce_references_accepts_list_and_json_string():
    row = {
        "title": "标题",
        "url": "https://example.com/a",
        "source": "web_search",
        "name": "示例站",
    }
    assert coerce_references([row])[0]["url"] == "https://example.com/a"
    parsed = coerce_references('[{"title":"标题","url":"https://example.com/a","source":"web_search","name":"示例站"}]')
    assert len(parsed) == 1
    assert coerce_references([]) == []
    assert coerce_references([{"title": "缺字段"}]) == []


def test_build_a2a_reference_items_matches_phone_card():
    items = build_a2a_reference_items(
        [
            {
                "title": "标题",
                "url": "https://example.com/a",
                "source": "web_search",
                "name": "百度百科",
                "imageUrl": "https://example.com/logo.png",
            }
        ]
    )
    assert items[0]["params"] == {"name": "百度百科", "source": "web_search"}
    params = items[0]["card"]["params"]
    assert items[0]["card"]["type"] == "leftPictureRightText"
    assert params["title"] == "标题"
    assert params["subTitle"] == "百度百科"
    assert params["link"]["webLink"] == {"startMode": 0, "url": "https://example.com/a"}
    assert params["imageInfo"]["small"]["url"] == "https://example.com/logo.png"


def test_append_reference_send_push(tmp_path):
    toolkit = XiaoyiAppendReferenceToolkit(
        request_id="r1",
        session_id="sess-1",
        channel_id="xiaoyi",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock()
    refs = [
        {
            "title": "标题",
            "url": "https://example.com/a",
            "source": "web_search",
            "name": "示例站",
        }
    ]

    with patch(
        "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
        return_value=mock_server,
    ), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ):
        result = asyncio.run(toolkit.append_reference(refs))

    assert "成功发送 1 条引用来源" in result
    assert mock_server.send_push.await_count == 1
    payload = mock_server.send_push.await_args.args[0]["payload"]
    assert payload["event_type"] == "chat.reference"
    assert payload["references"][0]["url"] == "https://example.com/a"


def test_append_reference_rejects_empty():
    toolkit = XiaoyiAppendReferenceToolkit(
        request_id="r1",
        session_id="sess-1",
        channel_id="xiaoyi",
    )
    result = asyncio.run(toolkit.append_reference([]))
    assert result.startswith("发送引用来源失败")
