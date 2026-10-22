# coding: utf-8
"""stream_utils.parse_stream_chunk：controller_output/task_failed 的 degraded 标记透传。

degraded（会话级降级，如 build_team 熔断后主理人单干）必须随 chat.error 事件
透传给 team_helpers——它据此把告警挡在 leader 错误史/死亡探针之外，
让降级轮次以"完成+告警"收尾而不是整轮失败。
"""

from types import SimpleNamespace

from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk


def _task_failed_chunk(text: str, metadata=None):
    payload = {
        "type": "task_failed",
        "data": [{"type": "text", "text": text}],
    }
    if metadata is not None:
        payload["metadata"] = metadata
    return SimpleNamespace(type="controller_output", payload=payload)


def test_task_failed_with_degraded_metadata_passes_flag_through():
    out = parse_stream_chunk(_task_failed_chunk("[132001] build_team failed: x", {"degraded": True}))
    assert out is not None
    assert out["event_type"] == "chat.error"
    assert "132001" in out["error"]
    assert out["degraded"] is True


def test_task_failed_without_metadata_has_no_flag():
    out = parse_stream_chunk(_task_failed_chunk("[181001] model call failed"))
    assert out is not None
    assert out["event_type"] == "chat.error"
    assert "degraded" not in out


def test_task_failed_with_unrelated_metadata_has_no_flag():
    out = parse_stream_chunk(_task_failed_chunk("boom", {"other": 1}))
    assert out is not None
    assert "degraded" not in out


def test_task_failed_object_payload_degraded():
    """payload 为对象形态（pydantic ControllerOutputPayload）时同样透传。"""
    payload = SimpleNamespace(
        type="task_failed",
        data=[SimpleNamespace(text="[132001] build_team failed: x")],
        metadata={"degraded": True},
    )
    chunk = SimpleNamespace(type="controller_output", payload=payload)
    out = parse_stream_chunk(chunk)
    assert out is not None
    assert out["event_type"] == "chat.error"
    assert out["degraded"] is True
