"""WeLink images land in the office, and the colleague can point a vision tool at them."""

from pathlib import Path

import pytest


def test_downloaded_image_is_rewritten_to_a_workspace_relative_path(tmp_path: Path):
    from jiuwenswarm.server.jiuwenmate.welink_inbound import place_welink_media

    media = tmp_path / "welink" / "mate" / "incoming" / "mid"
    media.mkdir(parents=True)
    source = media / "shore.jpg"
    source.write_bytes(b"\xff\xd8\xff")
    office = tmp_path / "office"
    office.mkdir()
    text = f"这张图片是哪里\n本地文件：{source}"

    placed = place_welink_media(office, "mid", (str(source),), text, media_root=tmp_path / "welink")

    assert "incoming/mid/shore.jpg" in placed
    assert str(source) not in placed
    assert (office / "incoming" / "mid" / "shore.jpg").read_bytes() == b"\xff\xd8\xff"


def test_file_outside_the_welink_cache_is_left_alone(tmp_path: Path):
    from jiuwenswarm.server.jiuwenmate.welink_inbound import place_welink_media

    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"\xff\xd8")
    office = tmp_path / "office"
    office.mkdir()
    text = f"本地文件：{outside}"

    placed = place_welink_media(office, "mid", (str(outside),), text, media_root=tmp_path / "welink")

    assert placed == text
    assert not (office / "incoming").exists()


def test_vision_path_accepts_the_workspace_relative_image(tmp_path: Path):
    from jiuwenswarm.server.jiuwenmate.vision_runtime import resolve_office_image

    office = tmp_path / "office"
    image = office / "incoming" / "mid" / "shore.jpg"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"\xff\xd8")

    assert resolve_office_image(office, "incoming/mid/shore.jpg") == str(image.resolve())


def test_vision_path_rejects_escape_and_non_images(tmp_path: Path):
    from jiuwenswarm.server.jiuwenmate.vision_runtime import resolve_office_image

    office = tmp_path / "office"
    office.mkdir()
    (office / "notes.txt").write_text("secret", encoding="utf-8")

    with pytest.raises(ValueError):
        resolve_office_image(office, "../notes.txt")
    with pytest.raises(ValueError):
        resolve_office_image(office, "notes.txt")


def test_vision_tools_join_the_colleague_when_configured(tmp_path: Path, monkeypatch):
    from openjiuwen.core.sys_operation import SysOperation, SysOperationCard
    from openjiuwen.harness.schema.build_context import PARENT_SYS_OPERATION_EXTRAS_KEY
    from jiuwenswarm.server.jiuwenmate.bash_runtime import bind_bash_runtime, build_bash_tool
    from jiuwenswarm.server.jiuwenmate.lark_cli_runtime import FeishuCliRuntime
    from types import SimpleNamespace

    monkeypatch.setenv("VISION_ENABLED", "true")
    monkeypatch.setenv("VISION_API_KEY", "test-key")
    monkeypatch.setenv("VISION_API_BASE", "https://example.invalid/v1/")
    monkeypatch.setenv("VISION_MODEL_NAME", "Qwen3-VL-30B-A3B-Instruct")
    office = tmp_path / "agent-a"
    session = office / "sessions" / "session-a"
    session.mkdir(parents=True)
    (office / "output").mkdir()
    context = SimpleNamespace(
        extras={PARENT_SYS_OPERATION_EXTRAS_KEY: SysOperation(SysOperationCard(id="agent-a"))},
        execution=SimpleNamespace(
            namespace_id="local", agent_id="agent-a", session_id="session-a", execution_id="execution-a"
        ),
        language="cn",
    )
    bind_bash_runtime(context, office=office, cli=FeishuCliRuntime(tmp_path / "jm", config_loader=lambda: {}))

    built = build_bash_tool({}, context)
    names = [item.card.name for item in (built if isinstance(built, list) else [built])]

    assert "visual_question_answering" in names
    assert "image_ocr" in names
    assert "bash" in names


def test_vision_tools_stay_off_without_the_vision_slot(tmp_path: Path, monkeypatch):
    from jiuwenswarm.server.jiuwenmate.vision_runtime import office_vision_tools, vision_guidance

    monkeypatch.delenv("VISION_ENABLED", raising=False)
    monkeypatch.delenv("VISION_API_KEY", raising=False)
    monkeypatch.delenv("VISION_API_BASE", raising=False)
    monkeypatch.delenv("VISION_MODEL_NAME", raising=False)

    assert office_vision_tools(tmp_path) == []
    assert vision_guidance() == ""
