"""third_agent_toolkits 单元测试。

覆盖：注册表加载（正常/缺失/损坏）、状态文件读写与损坏回退、
install/list/uninstall 三工具的成功路径与错误场景、web RPC handler。
subprocess 与端口/pid 探测全部 mock，不触碰真实进程与网络。
"""

import json
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.common.tools import third_agent_toolkits as tat


_REGISTRY_ENTRY = {
    "name": "openclaw",
    "display_name": "openClaw",
    "description": "开源个人 AI 助手",
    "install_cmd": "npm install -g openclaw",
    "start_cmd": "openclaw gateway",
    "stop_cmd": "openclaw gateway stop",
    "port": 18789,
    "web_path": "/",
}


@pytest.fixture()
def state_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """把状态文件重定向到临时目录。"""
    path = tmp_path / "third_agents.json"
    monkeypatch.setattr(tat, "_state_file", lambda: path)
    return path


@pytest.fixture()
def registry(monkeypatch: pytest.MonkeyPatch) -> list[dict]:
    """注册表替换为内存版本，避免依赖真实 yaml。"""
    entries = [dict(_REGISTRY_ENTRY)]
    monkeypatch.setattr(tat, "_load_registry", lambda: [dict(e) for e in entries])
    return entries


class _ProcResult:
    def __init__(self, returncode: int = 0, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout


# ---------------------------------------------------------------------------
# 注册表加载
# ---------------------------------------------------------------------------

def test_load_registry_from_real_file():
    entries = tat._load_registry()
    names = [e.get("name") for e in entries]
    assert "openclaw" in names
    entry = next(e for e in entries if e["name"] == "openclaw")
    for field in ("install_cmd", "start_cmd", "port"):
        assert entry.get(field), field


def test_load_registry_missing_file(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(tat, "_registry_path", lambda: tmp_path / "not-exists.yaml")
    assert tat._load_registry() == []


def test_load_registry_broken_yaml(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    bad = tmp_path / "third_agents.yaml"
    bad.write_text("agents: [unclosed", encoding="utf-8")
    monkeypatch.setattr(tat, "_registry_path", lambda: bad)
    assert tat._load_registry() == []


def test_find_registry_entry_case_insensitive(registry):
    assert tat._find_registry_entry("openclaw")["name"] == "openclaw"
    assert tat._find_registry_entry("OpenClaw")["name"] == "openclaw"
    assert tat._find_registry_entry(" openClaw ")["name"] == "openclaw"
    assert tat._find_registry_entry("unknown") is None
    assert tat._find_registry_entry("") is None


# ---------------------------------------------------------------------------
# 状态文件
# ---------------------------------------------------------------------------

def test_state_roundtrip(state_file: Path):
    entry = {"name": "openclaw", "port": 18789, "pid": 1234, "status": "running"}
    tat._upsert_state(entry)
    items = tat._read_state()
    assert len(items) == 1
    assert items[0]["name"] == "openclaw"
    # upsert 同名覆盖而非追加
    tat._upsert_state({**entry, "pid": 5678})
    items = tat._read_state()
    assert len(items) == 1
    assert items[0]["pid"] == 5678


def test_read_state_missing_file(state_file: Path):
    assert tat._read_state() == []


def test_read_state_broken_json(state_file: Path):
    state_file.write_text("{not json", encoding="utf-8")
    assert tat._read_state() == []


def test_read_state_non_list(state_file: Path):
    state_file.write_text(json.dumps({"name": "x"}), encoding="utf-8")
    assert tat._read_state() == []


def test_remove_state(state_file: Path):
    tat._upsert_state({"name": "openclaw", "port": 18789})
    tat._remove_state("openclaw")
    assert tat._read_state() == []
    # 删除不存在的条目不报错
    tat._remove_state("nope")


# ---------------------------------------------------------------------------
# 状态探测
# ---------------------------------------------------------------------------

def test_detect_running(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(tat, "_port_open", lambda port: True)
    monkeypatch.setattr(tat, "_pid_alive", lambda pid: True)
    assert tat._detect_running({"port": 18789, "pid": 1}) is True

    # pid 探测不确定（None）时以端口为准
    monkeypatch.setattr(tat, "_pid_alive", lambda pid: None)
    assert tat._detect_running({"port": 18789, "pid": None}) is True

    # pid 明确退出 → stopped
    monkeypatch.setattr(tat, "_pid_alive", lambda pid: False)
    assert tat._detect_running({"port": 18789, "pid": 1}) is False

    # 端口不通 → stopped
    monkeypatch.setattr(tat, "_port_open", lambda port: False)
    monkeypatch.setattr(tat, "_pid_alive", lambda pid: True)
    assert tat._detect_running({"port": 18789, "pid": 1}) is False


# ---------------------------------------------------------------------------
# install_third_agent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_install_unregistered_agent_requires_params(registry, state_file: Path):
    """注册表外的 Agent 且未提供 start_cmd/port：返回参数提示而非"暂不支持"。"""
    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.install_third_agent("deepseek-harness")
    assert result["success"] is False
    assert "start_cmd" in result["detail"]
    assert "port" in result["detail"]
    assert tat._read_state() == []  # 无残留


@pytest.mark.asyncio
async def test_install_unregistered_agent_with_params(
    registry, state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """注册表外的 Agent：提供 start_cmd/port 即可安装并写入状态。"""
    run_cmds = []
    monkeypatch.setattr(
        tat, "_run_cmd", lambda cmd, timeout: run_cmds.append(cmd) or _ProcResult()
    )
    monkeypatch.setattr(tat, "_start_process", lambda entry: 4321)
    monkeypatch.setattr(tat, "_wait_port_ready", lambda port, timeout: True)

    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.install_third_agent(
        "DeepSeek-Harness",
        display_name="DeepSeek Harness",
        install_cmd="pip install deepseek-harness",
        start_cmd="deepseek-harness serve",
        stop_cmd="deepseek-harness stop",
        port=9000,
        web_path="/ui",
    )
    assert result["success"] is True
    assert result["url"] == "http://localhost:9000/ui"
    assert run_cmds == ["pip install deepseek-harness"]

    state = tat._read_state()
    assert len(state) == 1
    entry = state[0]
    assert entry["name"] == "deepseek-harness"  # name 归一化为小写
    assert entry["display_name"] == "DeepSeek Harness"
    assert entry["start_cmd"] == "deepseek-harness serve"
    assert entry["stop_cmd"] == "deepseek-harness stop"


@pytest.mark.asyncio
async def test_install_unregistered_agent_without_install_cmd(
    registry, state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """install_cmd 为空时跳过安装步骤，直接启动。"""
    run_cmds = []
    monkeypatch.setattr(
        tat, "_run_cmd", lambda cmd, timeout: run_cmds.append(cmd) or _ProcResult()
    )
    monkeypatch.setattr(tat, "_start_process", lambda entry: 4321)
    monkeypatch.setattr(tat, "_wait_port_ready", lambda port, timeout: True)

    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.install_third_agent(
        "deepseek-harness", start_cmd="deepseek-harness serve", port=9000,
    )
    assert result["success"] is True
    assert run_cmds == []  # 未执行任何安装命令


@pytest.mark.asyncio
async def test_restart_unregistered_agent_uses_state(
    registry, state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """注册表外的 Agent 进程停止后：仅凭 name 即可用状态中保存的 start_cmd 重启。"""
    tat._upsert_state({
        "name": "deepseek-harness", "display_name": "DeepSeek Harness",
        "port": 9000, "web_path": "/", "pid": 1234, "status": "running",
        "start_cmd": "deepseek-harness serve", "stop_cmd": "",
    })
    monkeypatch.setattr(tat, "_detect_running", lambda entry: False)
    started = []
    monkeypatch.setattr(tat, "_start_process", lambda entry: started.append(entry) or 9999)
    monkeypatch.setattr(tat, "_wait_port_ready", lambda port, timeout: True)

    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.install_third_agent("deepseek-harness")
    assert result["success"] is True
    assert started and started[0]["start_cmd"] == "deepseek-harness serve"
    assert tat._read_state()[0]["pid"] == 9999


def test_stop_process_falls_back_to_state_stop_cmd(monkeypatch: pytest.MonkeyPatch):
    """注册表外的 Agent（reg_entry 为 None）：stop_cmd 取自状态文件。"""
    ran = []
    monkeypatch.setattr(tat, "_run_cmd", lambda cmd, timeout: ran.append(cmd) or _ProcResult())
    monkeypatch.setattr(tat, "_pid_alive", lambda pid: False)  # stop_cmd 后进程已退出
    tat._stop_process(
        {"pid": 1234, "port": 9000, "stop_cmd": "deepseek-harness stop"}, None
    )
    assert ran == ["deepseek-harness stop"]


@pytest.mark.asyncio
async def test_install_already_running(
    registry, state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    tat._upsert_state({
        "name": "openclaw", "display_name": "openClaw",
        "port": 18789, "web_path": "/", "pid": 1234, "status": "running",
    })
    monkeypatch.setattr(tat, "_detect_running", lambda entry: True)
    called = []
    monkeypatch.setattr(tat, "_run_cmd", lambda *a, **k: called.append(a) or _ProcResult())

    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.install_third_agent("openclaw")
    assert result["success"] is True
    assert result["already_installed"] is True
    assert result["url"] == "http://localhost:18789/"
    assert called == []  # 幂等：不执行任何命令


@pytest.mark.asyncio
async def test_install_restart_when_stopped(
    registry, state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    tat._upsert_state({
        "name": "openclaw", "display_name": "openClaw",
        "port": 18789, "web_path": "/", "pid": 1234, "status": "running",
    })
    monkeypatch.setattr(tat, "_detect_running", lambda entry: False)
    monkeypatch.setattr(tat, "_start_process", lambda entry: 9999)
    monkeypatch.setattr(tat, "_wait_port_ready", lambda port, timeout: True)
    run_cmds = []
    monkeypatch.setattr(tat, "_run_cmd", lambda *a, **k: run_cmds.append(a) or _ProcResult())

    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.install_third_agent("openclaw")
    assert result["success"] is True
    assert run_cmds == []  # 进程已装，只重启不跑 install_cmd
    state = tat._read_state()
    assert len(state) == 1
    assert state[0]["pid"] == 9999  # pid 已更新


@pytest.mark.asyncio
async def test_install_success(
    registry, state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    run_cmds = []
    monkeypatch.setattr(
        tat, "_run_cmd", lambda cmd, timeout: run_cmds.append(cmd) or _ProcResult()
    )
    monkeypatch.setattr(tat, "_start_process", lambda entry: 4321)
    monkeypatch.setattr(tat, "_wait_port_ready", lambda port, timeout: True)

    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.install_third_agent("openClaw")
    assert result["success"] is True
    assert result["url"] == "http://localhost:18789/"
    assert run_cmds == ["npm install -g openclaw"]

    state = tat._read_state()
    assert len(state) == 1
    entry = state[0]
    assert entry["name"] == "openclaw"
    assert entry["display_name"] == "openClaw"
    assert entry["pid"] == 4321
    assert entry["status"] == "running"
    assert entry["installed_at"]


@pytest.mark.asyncio
async def test_install_cmd_failure(
    registry, state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(tat, "_run_cmd", lambda cmd, timeout: _ProcResult(returncode=1, stdout="boom"))
    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.install_third_agent("openclaw")
    assert result["success"] is False
    assert "boom" in result["detail"]
    assert tat._read_state() == []  # 无残留


@pytest.mark.asyncio
async def test_install_start_timeout_rolls_back(
    registry, state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(tat, "_run_cmd", lambda cmd, timeout: _ProcResult())
    monkeypatch.setattr(tat, "_start_process", lambda entry: 4321)
    monkeypatch.setattr(tat, "_wait_port_ready", lambda port, timeout: False)
    stopped = []
    monkeypatch.setattr(tat, "_stop_process", lambda state, reg: stopped.append(state))

    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.install_third_agent("openclaw")
    assert result["success"] is False
    assert "未就绪" in result["detail"]
    assert stopped and stopped[0]["pid"] == 4321  # 已清理进程
    assert tat._read_state() == []  # 无残留


# ---------------------------------------------------------------------------
# list_third_agents
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_empty(state_file: Path):
    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.list_third_agents()
    assert result["success"] is True
    assert result["items"] == []
    assert "未安装" in result["detail"]


@pytest.mark.asyncio
async def test_list_with_entries(state_file: Path, monkeypatch: pytest.MonkeyPatch):
    tat._upsert_state({
        "name": "openclaw", "display_name": "openClaw",
        "port": 18789, "web_path": "/", "pid": 1, "status": "running",
        "installed_at": "2026-08-12T10:00:00+00:00",
    })
    monkeypatch.setattr(tat, "_detect_running", lambda entry: True)

    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.list_third_agents()
    assert result["success"] is True
    assert len(result["items"]) == 1
    item = result["items"][0]
    assert item["display_name"] == "openClaw"
    assert item["status"] == "running"
    assert item["url"] == "http://localhost:18789/"


# ---------------------------------------------------------------------------
# uninstall_third_agent
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_uninstall_not_installed(registry, state_file: Path):
    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.uninstall_third_agent("openclaw")
    assert result["success"] is False
    assert "未安装" in result["detail"]


@pytest.mark.asyncio
async def test_uninstall_success(
    registry, state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    tat._upsert_state({
        "name": "openclaw", "display_name": "openClaw",
        "port": 18789, "web_path": "/", "pid": 1234, "status": "running",
    })
    stopped = []
    monkeypatch.setattr(tat, "_stop_process", lambda state, reg: stopped.append(state))

    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.uninstall_third_agent("openClaw")
    assert result["success"] is True
    assert stopped and stopped[0]["name"] == "openclaw"
    assert tat._read_state() == []  # 状态条目已摘除


@pytest.mark.asyncio
async def test_uninstall_dead_process_still_removable(
    registry, state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """进程已死但状态在：卸载仍可正常摘除条目。"""
    tat._upsert_state({"name": "openclaw", "port": 18789, "pid": 99999})
    monkeypatch.setattr(tat, "_stop_process", lambda state, reg: None)
    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.uninstall_third_agent("openclaw")
    assert result["success"] is True
    assert tat._read_state() == []


# ---------------------------------------------------------------------------
# web RPC handler
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_third_agents_list(state_file: Path, monkeypatch: pytest.MonkeyPatch):
    tat._upsert_state({
        "name": "openclaw", "display_name": "openClaw",
        "port": 18789, "web_path": "/", "pid": 1, "status": "running",
    })
    monkeypatch.setattr(tat, "_detect_running", lambda entry: False)
    payload = await tat.handle_third_agents_list({})
    assert "agents" in payload
    assert len(payload["agents"]) == 1
    assert payload["agents"][0]["status"] == "stopped"


def test_tools_registration():
    tools = tat.get_third_agent_tools()
    names = {t.card.name for t in tools}
    assert names == {"install_third_agent", "list_third_agents", "uninstall_third_agent"}


def test_web_channel_whitelist():
    """third_agents.list 已注册进 web 通道转发白名单（无本地 handler）。"""
    from jiuwenswarm.gateway.channel_manager.web import app_web_handlers

    assert "third_agents.list" in app_web_handlers._FORWARD_REQ_METHODS
    assert "third_agents.list" in app_web_handlers._FORWARD_NO_LOCAL_HANDLER_METHODS


def test_req_method_registered():
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.server.runtime.agent_adapter import interface

    assert ReqMethod.THIRD_AGENTS_LIST.value == "third_agents.list"
    assert ReqMethod.THIRD_AGENTS_LIST in interface._STATELESS_FUNC_ROUTES


# ---------------------------------------------------------------------------
# ThirdAgentPromptRail：已安装状态注入系统提示
# ---------------------------------------------------------------------------

from types import SimpleNamespace  # noqa: E402

from jiuwenswarm.agents.harness.common.rails import third_agent_prompt_rail as tapr  # noqa: E402


class _FakePromptBuilder:
    def __init__(self, language: str = "cn") -> None:
        self.language = language
        self.sections = {}

    def add_section(self, section) -> None:
        self.sections[section.name] = section

    def remove_section(self, name: str) -> None:
        self.sections.pop(name, None)


def _install_fake_entry(state_file: Path, monkeypatch: pytest.MonkeyPatch, *, running: bool):
    tat._upsert_state({
        "name": "openclaw", "display_name": "openClaw",
        "port": 18789, "web_path": "/", "pid": 1, "status": "running",
    })
    monkeypatch.setattr(tat, "_detect_running", lambda entry: running)


def test_render_prompt_empty(state_file: Path):
    assert tapr.render_third_agent_prompt("cn") == ""


def test_render_prompt_zh(state_file: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_entry(state_file, monkeypatch, running=True)
    content = tapr.render_third_agent_prompt("cn")
    assert "openClaw" in content
    assert "运行中" in content
    assert "http://localhost:18789/" in content
    assert "install_third_agent" in content


def test_render_prompt_en(state_file: Path, monkeypatch: pytest.MonkeyPatch):
    _install_fake_entry(state_file, monkeypatch, running=False)
    content = tapr.render_third_agent_prompt("en")
    assert "Installed third-party agents" in content
    assert "stopped" in content


@pytest.mark.asyncio
async def test_rail_injects_and_removes_section(state_file: Path, monkeypatch: pytest.MonkeyPatch):
    rail = tapr.ThirdAgentPromptRail()
    builder = _FakePromptBuilder(language="cn")
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    ctx = SimpleNamespace(agent=None)

    # 无已安装 Agent：不注入
    await rail.before_model_call(ctx)
    assert tapr.ThirdAgentPromptRail.SECTION_NAME not in builder.sections

    # 安装后：注入段落
    _install_fake_entry(state_file, monkeypatch, running=True)
    await rail.before_model_call(ctx)
    section = builder.sections.get(tapr.ThirdAgentPromptRail.SECTION_NAME)
    assert section is not None
    assert "openClaw" in section.content["cn"]

    # 卸载后：段落移除
    tat._remove_state("openclaw")
    await rail.before_model_call(ctx)
    assert tapr.ThirdAgentPromptRail.SECTION_NAME not in builder.sections

    rail.uninit(SimpleNamespace(system_prompt_builder=builder))


# ---------------------------------------------------------------------------
# install_third_agent 指定 user（start_cmd 凭据占位符替换）
# ---------------------------------------------------------------------------

_USER_RECORD = {
    "username": "alice",
    "password": "secret-pw",
    "api_key": "sk-1234567890",
    "api_base": "https://api.example.com/v1",
    "model": "gpt-x",
    "created_at": "2026-08-24T00:00:00+00:00",
}


def test_apply_user_credentials():
    cmd, replaced = tat._apply_user_credentials(
        "mycli --api-key {api_key} --base-url {api_base} --model {model} serve",
        _USER_RECORD,
    )
    assert cmd == (
        "mycli --api-key sk-1234567890 --base-url https://api.example.com/v1 "
        "--model gpt-x serve"
    )
    assert replaced == ["{api_key}", "{api_base}", "{model}"]

    # 无占位符时原样返回
    cmd, replaced = tat._apply_user_credentials("openclaw gateway", _USER_RECORD)
    assert cmd == "openclaw gateway"
    assert replaced == []

    # 缺字段替换为空串
    cmd, replaced = tat._apply_user_credentials(
        "mycli --model {model}", {**_USER_RECORD, "model": ""}
    )
    assert cmd == "mycli --model "
    assert replaced == ["{model}"]


@pytest.mark.asyncio
async def test_install_with_user_replaces_placeholders(
    registry, state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """指定 user 时，start_cmd 占位符被该用户凭据替换并落入状态文件。"""
    started = []

    def fake_start(entry):
        started.append(dict(entry))
        return 4321

    monkeypatch.setattr(tat, "_start_process", fake_start)
    monkeypatch.setattr(tat, "_wait_port_ready", lambda port, timeout: True)
    monkeypatch.setattr(tat, "get_user", lambda name: dict(_USER_RECORD))

    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.install_third_agent(
        "mycli-agent",
        start_cmd="mycli --api-key {api_key} --base-url {api_base} --model {model} serve",
        port=9100,
        user="alice",
    )
    assert result["success"] is True
    assert "alice" in result["detail"]
    expected = (
        "mycli --api-key sk-1234567890 --base-url https://api.example.com/v1 "
        "--model gpt-x serve"
    )
    assert started[0]["start_cmd"] == expected
    state = tat._read_state()
    assert state[0]["start_cmd"] == expected
    assert state[0]["user"] == "alice"


@pytest.mark.asyncio
async def test_install_with_unknown_user_fails(
    registry, state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """指定的用户不存在：安装失败且不启动进程、不留状态。"""
    started = []
    monkeypatch.setattr(tat, "_start_process", lambda entry: started.append(entry) or 1)
    monkeypatch.setattr(tat, "get_user", lambda name: None)

    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.install_third_agent(
        "mycli-agent", start_cmd="mycli serve", port=9100, user="nobody"
    )
    assert result["success"] is False
    assert "不存在" in result["detail"]
    assert started == []
    assert tat._read_state() == []


@pytest.mark.asyncio
async def test_install_with_user_without_placeholders(
    registry, state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """指定了 user 但 start_cmd 无占位符：正常安装，detail 提示凭据未注入。"""
    monkeypatch.setattr(tat, "_start_process", lambda entry: 4321)
    monkeypatch.setattr(tat, "_wait_port_ready", lambda port, timeout: True)
    monkeypatch.setattr(tat, "get_user", lambda name: dict(_USER_RECORD))

    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.install_third_agent(
        "mycli-agent", start_cmd="mycli serve", port=9100, user="alice"
    )
    assert result["success"] is True
    assert "未注入" in result["detail"]
    assert tat._read_state()[0]["start_cmd"] == "mycli serve"


@pytest.mark.asyncio
async def test_install_without_user_keeps_placeholders(
    registry, state_file: Path, monkeypatch: pytest.MonkeyPatch
):
    """未指定 user：占位符不做替换，按原样启动。"""
    started = []

    def fake_start(entry):
        started.append(dict(entry))
        return 4321

    monkeypatch.setattr(tat, "_start_process", fake_start)
    monkeypatch.setattr(tat, "_wait_port_ready", lambda port, timeout: True)

    toolkit = tat.ThirdAgentToolkit()
    result = await toolkit.install_third_agent(
        "mycli-agent", start_cmd="mycli --api-key {api_key} serve", port=9100
    )
    assert result["success"] is True
    assert started[0]["start_cmd"] == "mycli --api-key {api_key} serve"
    assert "user" not in tat._read_state()[0]
