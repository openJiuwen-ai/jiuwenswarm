from __future__ import annotations

import asyncio
import io
import json

import pytest

from jiuwenswarm.channels.process_cli.render import EventRenderer
from jiuwenswarm.channels.process_cli import ui as ui_module
from jiuwenswarm.channels.process_cli.ui import HumanRunUI, ProcessCliUI
from jiuwenswarm.runtime.events import RuntimeEvent


@pytest.mark.parametrize("output_format", ["human", "json", "jsonl"])
def test_team_member_text_does_not_interleave_with_human_leader_answer(
    output_format,
) -> None:
    stdout = io.StringIO()
    stderr = io.StringIO()
    renderer = EventRenderer(
        output_format,
        stdout=stdout,
        stderr=stderr,
        show_reasoning=True,
        show_tools=True,
    )
    for payload in (
        {"event_type": "chat.delta", "role": "leader", "delta": "Leader "},
        {"event_type": "chat.delta", "role": "teammate", "delta": "MEMBER_DELTA"},
        {"event_type": "chat.reasoning", "role": "teammate", "text": "MEMBER_THINKING"},
        {"event_type": "chat.final", "role": "teammate", "content": "MEMBER_FINAL"},
        {"event_type": "chat.tool_result", "role": "teammate", "text": "MEMBER_TOOL"},
        {"event_type": "chat.delta", "role": "leader", "delta": "answer."},
        {"event_type": "chat.final", "role": "leader", "content": "Leader answer."},
    ):
        renderer.render(RuntimeEvent("request", "process_cli", "session", payload))
    renderer.finish(session_id="session", request_id="request")
    assert len(renderer.events) == 7
    if output_format == "human":
        assert stdout.getvalue().strip() == "Leader answer."
        assert "MEMBER_THINKING" not in stderr.getvalue()
        assert "MEMBER_TOOL" in stderr.getvalue()
    else:
        assert "MEMBER_DELTA" in stdout.getvalue()
        assert "MEMBER_FINAL" in stdout.getvalue()
        assert "MEMBER_THINKING" in stdout.getvalue()


class TtyBuffer(io.StringIO):
    @property
    def encoding(self) -> str:
        return "utf-8"

    def isatty(self) -> bool:
        return True


@pytest.mark.parametrize("unicode", [False, True])
def test_reasoning_deltas_form_one_continuous_block(monkeypatch, unicode):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(ui_module, "_supports_unicode", lambda stream: unicode)
    output = TtyBuffer()
    renderer = EventRenderer("human", stdout=output, stderr=output, show_reasoning=True)
    for text in ("The", " user is simply", " asking to", ' reply with "CLI', '_OK".'):
        renderer.render(
            RuntimeEvent(
                request_id="request",
                channel_id="process_cli",
                session_id="session",
                payload={"event_type": "chat.reasoning", "text": text},
            )
        )
    renderer.render(
        RuntimeEvent(
            request_id="request",
            channel_id="process_cli",
            session_id="session",
            payload={"event_type": "chat.delta", "delta": "CLI_OK"},
        )
    )
    renderer.finish(session_id="session", request_id="request")
    text = output.getvalue()
    assert text.count("思考") == 1
    assert 'The user is simply asking to reply with "CLI_OK".\nCLI_OK\n' in text


@pytest.mark.parametrize("unicode", [False, True])
def test_reasoning_preserves_line_breaks_split_across_chunks(monkeypatch, unicode):
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setattr(ui_module, "_supports_unicode", lambda stream: unicode)
    output = TtyBuffer()
    ui = HumanRunUI(output, output)
    for text in ("first", "\r", "\n", "\n", " sec", "ond\n", "third"):
        ui.reasoning(text)
    ui.end_reasoning()
    prefix = "  │  " if unicode else "     "
    assert output.getvalue().count("思考") == 1
    assert output.getvalue().endswith(
        f"{prefix}first\r\n\n{prefix} second\n{prefix}third\n"
    )


@pytest.mark.parametrize("show_tools", [False, True])
def test_tool_round_starts_a_new_reasoning_block(monkeypatch, show_tools):
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    renderer = EventRenderer(
        "human",
        stdout=output,
        stderr=output,
        show_reasoning=True,
        show_tools=show_tools,
    )
    for payload in (
        {"event_type": "chat.reasoning", "text": "Before tool."},
        {"event_type": "context.usage", "tokens": 10},
        {"event_type": "chat.reasoning", "text": " Still thinking."},
        {"event_type": "chat.tool_call", "content": "read_file"},
        {"event_type": "chat.tool_result", "content": "FILE_CONTENT"},
        {"event_type": "chat.reasoning", "text": "After tool."},
        {"event_type": "chat.final", "content": "ANSWER"},
    ):
        renderer.render(
            RuntimeEvent(
                request_id="request",
                channel_id="process_cli",
                session_id="session",
                payload=payload,
            )
        )
    renderer.finish(session_id="session", request_id="request")
    text = output.getvalue()
    assert text.count("思考") == 2
    assert "Before tool. Still thinking.\n" in text
    assert "After tool.\nANSWER\n" in text
    assert ("FILE_CONTENT" in text) == show_tools


@pytest.mark.parametrize("ending", ["finish", "error", "interrupt", "interaction"])
def test_reasoning_closes_before_terminal_or_interaction_output(monkeypatch, ending):
    monkeypatch.setenv("NO_COLOR", "1")
    output = TtyBuffer()
    renderer = EventRenderer("human", stdout=output, stderr=output, show_reasoning=True)
    renderer.render(
        RuntimeEvent(
            request_id="request",
            channel_id="process_cli",
            session_id="session",
            payload={"event_type": "chat.reasoning", "text": "Thinking"},
        )
    )
    if ending == "error":
        renderer.render(
            RuntimeEvent.error(
                request_id="request",
                channel_id="process_cli",
                session_id="session",
                error=RuntimeError("failed"),
            )
        )
    elif ending == "interrupt":
        renderer.interrupted()
    elif ending == "interaction":
        renderer.prepare_interaction()
        output.write("QUESTION\n")
    else:
        renderer.finish(
            session_id="session", request_id="request", show_completion=False
        )
    text = output.getvalue()
    assert "Thinking\n" in text
    assert text.count("思考") == 1
    if ending == "interaction":
        assert "Thinking\nQUESTION\n" in text


@pytest.mark.parametrize("output_format", ["json", "jsonl"])
def test_machine_formats_keep_each_reasoning_event(output_format):
    stdout, stderr = io.StringIO(), io.StringIO()
    renderer = EventRenderer(
        output_format, stdout=stdout, stderr=stderr, show_reasoning=True
    )
    events = [
        RuntimeEvent(
            request_id="request",
            channel_id="process_cli",
            session_id="session",
            payload={"event_type": "chat.reasoning", "text": text},
        )
        for text in ("first", " second")
    ]
    for event in events:
        renderer.render(event)
    renderer.finish(session_id="session", request_id="request")
    if output_format == "json":
        actual = json.loads(stdout.getvalue())["events"]
    else:
        actual = [json.loads(line) for line in stdout.getvalue().splitlines()]
    assert actual == [event.to_dict() for event in events]
    assert stderr.getvalue() == ""


@pytest.mark.parametrize("show_reasoning", [False, True])
def test_reasoning_keeps_stdout_answer_and_stderr_thoughts_separate(show_reasoning):
    stdout, stderr = io.StringIO(), io.StringIO()
    renderer = EventRenderer(
        "human", stdout=stdout, stderr=stderr, show_reasoning=show_reasoning
    )
    for payload in (
        {"event_type": "chat.reasoning", "text": "seg"},
        {"event_type": "chat.reasoning", "text": "ment"},
        {"event_type": "chat.final", "content": "ANSWER"},
    ):
        renderer.render(
            RuntimeEvent(
                request_id="request",
                channel_id="process_cli",
                session_id="session",
                payload=payload,
            )
        )
    renderer.finish(session_id="session", request_id="request")
    assert stdout.getvalue() == "ANSWER\n"
    if show_reasoning:
        assert stderr.getvalue().count("思考") == 1
        assert stderr.getvalue().endswith("segment\n")
    else:
        assert stderr.getvalue() == ""


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

    assert output.getvalue() == ("\n工作进程诊断信息：\nworker failure\ntrace tail\n")


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


def test_human_renderer_summarizes_provider_denial_without_dumping_response() -> None:
    output = TtyBuffer()
    renderer = EventRenderer("human", stdout=output, stderr=output, concise_errors=True)
    message = (
        "[181001] model call failed: Error code: 400 - "
        "{'error': {'message': 'Access denied, please check your account', "
        "'details': '" + "x" * 500 + "'}}"
    )

    renderer.render(
        RuntimeEvent(
            request_id="denied-request",
            channel_id="process_cli",
            session_id="runtime-session",
            payload={"event_type": "chat.error", "message": message},
        )
    )

    assert "模型服务拒绝访问（HTTP 400）" in output.getvalue()
    assert "please check your account" not in output.getvalue()
    assert len(output.getvalue()) < 200

    interactive_output = TtyBuffer()
    EventRenderer("human", stdout=interactive_output, stderr=interactive_output).render(
        RuntimeEvent(
            request_id="interactive-denied-request",
            channel_id="process_cli",
            session_id="runtime-session",
            payload={"event_type": "chat.error", "message": message},
        )
    )
    assert "please check your account" in interactive_output.getvalue()


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
