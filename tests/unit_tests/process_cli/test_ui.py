from __future__ import annotations

import asyncio
import io

import pytest

from jiuwenswarm.channels.process_cli.render import EventRenderer
from jiuwenswarm.channels.process_cli import ui as ui_module
from jiuwenswarm.channels.process_cli.ui import HumanRunUI, ProcessCliUI
from jiuwenswarm.runtime.events import RuntimeEvent


class TtyBuffer(io.StringIO):
    @property
    def encoding(self) -> str:
        return "utf-8"

    def isatty(self) -> bool:
        return True


def test_startup_uses_chinese_card_and_jiuwenswarm_title(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    ui = ProcessCliUI(output, columns=80)

    ui.startup(
        model_name="gpt-5.6-sol",
        mode="code.normal",
        cwd="D:\\work_space\\jiuwenswarm",
        session_id=None,
    )

    text = output.getvalue()
    assert ">_ JiuwenSwarm" in text
    assert "进程式 CLI · 本地 Runtime" in text
    assert "模型（配置推断）：  gpt-5.6-sol" in text
    assert "目录：  D:\\work_space\\jiuwenswarm" in text
    assert "模式（请求推断）：  code.normal" in text
    assert "会话：  尚未创建" in text
    assert "工作模式" not in text
    assert "输入 / 查看可用命令。" in text
    assert "/help      查看所有命令" not in text
    assert "\033[" not in text


def test_narrow_terminal_falls_back_to_plain_layout(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    ui = ProcessCliUI(output, columns=40)

    ui.startup(
        model_name="gpt-5.6-sol",
        mode="code.normal",
        cwd="D:\\work_space\\jiuwenswarm",
        session_id="runtime-session",
    )

    text = output.getvalue()
    assert "模型（配置推断）：gpt-5.6-sol" in text
    assert "模式（请求推断）：code.normal" in text
    assert "会话：runtime-session" in text
    assert "工作模式" not in text
    assert "╭" not in text


@pytest.mark.parametrize("columns", [32, 40, 47, 48, 67, 68, 80])
def test_startup_never_exceeds_terminal_width(monkeypatch, columns: int) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    ui = ProcessCliUI(output, columns=columns)

    ui.startup(
        model_name="a-very-long-model-name-for-width-regression",
        mode="code.normal",
        cwd="D:\\very\\long\\workspace\\directory\\with\\many\\nested\\segments",
        session_id="runtime-session-with-a-long-identifier",
    )

    for line in output.getvalue().splitlines():
        assert ui_module._display_width(line) <= columns, (columns, line)


def test_status_shows_model_and_canonical_mode_without_work_mode(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    ui = ProcessCliUI(output, columns=80)

    ui.status(
        model_name="gpt-5.6-sol",
        mode="code.normal",
        cwd="D:\\work_space\\jiuwenswarm",
        session_id="runtime-session",
    )

    text = output.getvalue()
    assert "gpt-5.6-sol · code.normal" in text
    assert "工作模式" not in text


def test_process_cli_diagnostics_use_the_configured_stream() -> None:
    output = TtyBuffer()
    ui = ProcessCliUI(output, columns=80)

    ui.diagnostics(["worker failure", "trace tail"])

    assert output.getvalue() == (
        "\n工作进程诊断信息：\nworker failure\ntrace tail\n"
    )


def test_process_cli_renders_installed_and_available_skills(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    ui = ProcessCliUI(output, columns=80)

    ui.skills(
        [
            {
                "name": "local-skill",
                "source": "local",
                "description": "本地技能",
                "installed": True,
            },
            {
                "name": "builtin-skill",
                "is_builtin_source": True,
                "description": "内置技能",
                "installed": False,
            },
        ]
    )

    text = output.getvalue()
    assert "已安装技能（1）" in text
    assert "- local-skill [local] · 本地技能" in text
    assert "可安装技能（1）" in text
    assert "- builtin-skill [内置] · 内置技能" in text


def test_human_renderer_shows_chinese_runtime_states(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    renderer = EventRenderer("human", stdout=output, stderr=output)
    renderer.start()
    renderer.working()
    renderer.render(
        RuntimeEvent(
            request_id="request-1",
            channel_id="process_cli",
            session_id="runtime-session",
            payload={"event_type": "chat.delta", "delta": "你好"},
        )
    )
    renderer.finish(session_id="runtime-session", request_id="request-1")

    text = output.getvalue()
    assert "正在启动本地 Runtime" not in text
    assert "正在处理" in text
    assert "• JiuwenSwarm" in text
    assert "你好" in text
    assert "✓ 执行完成 · 会话 runtime-sess…" in text
    assert "\033[" not in text


@pytest.mark.parametrize(
    ("event_type", "payload", "expected"),
    [
        (
            "session.created",
            {"session_id": "process_cli_created"},
            "已创建并切换到会话 process_cli_created",
        ),
        (
            "session.switched",
            {"session_id": "process_cli_resumed"},
            "已恢复会话 process_cli_resumed",
        ),
        (
            "session.forked",
            {"session_id": "process_cli_forked", "title": "实验分支"},
            "已创建并切换到会话分支 process_cli_forked · 实验分支",
        ),
        (
            "session.deleted",
            {"session_id": "process_cli_deleted"},
            "已删除会话 process_cli_deleted",
        ),
    ],
)
def test_human_renderer_shows_session_lifecycle_results(
    monkeypatch,
    event_type: str,
    payload: dict[str, str],
    expected: str,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    renderer = EventRenderer("human", stdout=output, stderr=output)

    renderer.render(
        RuntimeEvent.control(
            request_id="session-request",
            channel_id="process_cli",
            session_id=payload["session_id"],
            payload={"event_type": event_type, **payload},
        )
    )

    assert expected in output.getvalue()


def test_human_renderer_ignores_none_terminal_sentinel() -> None:
    output = TtyBuffer()
    renderer = EventRenderer("human", stdout=output, stderr=output)

    renderer.render(
        RuntimeEvent(
            request_id="terminal-sentinel",
            channel_id="process_cli",
            session_id="runtime-session",
            payload=None,
            is_complete=True,
        )
    )

    assert output.getvalue() == ""
    assert renderer.events[0]["payload"] is None


def test_human_renderer_displays_skills_list_without_chat_completion(
    monkeypatch,
) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    renderer = EventRenderer("human", stdout=output, stderr=output)
    event = RuntimeEvent(
        request_id="skills-request",
        channel_id="process_cli",
        session_id=None,
        payload={
            "skills": [
                {
                    "name": "demo",
                    "source": "local",
                    "description": "示例技能",
                    "installed": True,
                }
            ]
        },
        is_complete=True,
    )

    renderer.working()
    renderer.render(event, view="skills.list")
    renderer.finish(
        session_id="",
        request_id="skills-request",
        show_completion=False,
    )

    text = output.getvalue()
    assert "已安装技能（1）" in text
    assert "demo [local] · 示例技能" in text
    assert "执行完成 · 会话" not in text
    assert renderer.events == [event.to_dict()]


@pytest.mark.asyncio
async def test_human_run_ui_animates_spinner_on_color_tty(monkeypatch) -> None:
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(ui_module, "_supports_color", lambda _stream: True)
    output = TtyBuffer()
    ui = HumanRunUI(output, output)

    ui.start()
    assert output.getvalue() == ""
    ui.working()
    await asyncio.sleep(0.14)
    ui.clear_status()
    await asyncio.sleep(0)

    text = output.getvalue()
    assert "正在启动本地 Runtime" not in text
    assert "正在处理" in text
    assert "⠋" in text
    assert any(frame in text for frame in "⠙⠹⠸⠼⠴⠦⠧⠇⠏")
    assert "\r\033[2K" in text


def test_human_renderer_shows_interrupted_state(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    renderer = EventRenderer("human", stdout=output, stderr=output)

    renderer.start()
    renderer.working()
    renderer.interrupted()

    assert "! 已中断" in output.getvalue()


def test_human_renderer_translates_cli_timeout_only_for_display(monkeypatch) -> None:
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    renderer = EventRenderer("human", stdout=output, stderr=output)

    renderer.render(
        RuntimeEvent.error(
            request_id="request-1",
            channel_id="process_cli",
            session_id="runtime-session",
            error=TimeoutError("process CLI execution timed out"),
        )
    )

    text = output.getvalue()
    assert "进程式 CLI 执行超时" in text
    assert "process CLI execution timed out" not in text


def test_jsonl_output_has_no_human_interface() -> None:
    output = TtyBuffer()
    renderer = EventRenderer("jsonl", stdout=output, stderr=output)
    renderer.start()
    renderer.render(
        RuntimeEvent(
            request_id="request-1",
            channel_id="process_cli",
            session_id="runtime-session",
            payload={"event_type": "chat.delta", "delta": "hello"},
        )
    )

    text = output.getvalue()
    assert '"event_type": "chat.delta"' in text
    assert "JiuwenSwarm" not in text
    assert "正在启动" not in text
