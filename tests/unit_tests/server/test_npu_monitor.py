"""npu_monitor 单元测试。

覆盖：npu-smi 输出解析（双 NPU 样例/表头跳过/字段缺失/孤立 chip 行）、
query_npu_status 降级路径（命令不存在/超时/非零退出/空解析）、
async handler 与 web 白名单注册。subprocess 全部 mock，不触碰真实命令。
"""

import subprocess

import pytest

from jiuwenswarm.server.runtime.hardware import npu_monitor as nm


_SAMPLE_910B = """
+-------------------------------------------------------------------------------------------+
| npu-smi 23.0.6                                  Version: 23.0.6                           |
+-------------------------------+-----------------+-------------------------------------------+
| NPU     Name                  | Health          | Power(W)     Temp(C)     Hugepages-Usage(page) |
| Chip    Device                | Bus-Id          | AICore(%)    Memory-Usage(MB)                |
+===============================+=================+===========================================+
| 0       910B3                 | OK              | 92.5         42          0    / 0           |
| 0       0                     | 0000:C1:00.0    | 12           2620 / 65536                   |
+-------------------------------+-----------------+-------------------------------------------+
| 1       910B3                 | OK              | 88.1         39          0    / 0           |
| 1       0                     | 0000:C2:00.0    | 5            1024 / 65536                   |
+===============================+=================+===========================================+
"""

# npu-smi 25.5.0（910B3）真实输出：带 HBM-Usage 列，Memory-Usage(DDR) 列恒为 0 / 0，
# 真实显存在 HBM 列；末尾附进程表（4 段行 + "No running processes" 行）。
_SAMPLE_910B_HBM = """
+------------------------------------------------------------------------------------------------+
| npu-smi 25.5.0                   Version: 25.5.0                                               |
+---------------------------+---------------+----------------------------------------------------+
| NPU   Name                | Health        | Power(W)    Temp(C)           Hugepages-Usage(page)|
| Chip                      | Bus-Id        | AICore(%)   Memory-Usage(MB)  HBM-Usage(MB)        |
+===========================+===============+====================================================+
| 0     910B3               | OK            | 100.0       50                0    / 0             |
| 0                         | 0000:C1:00.0  | 0           0    / 0          3546 / 65536         |
+===========================+===============+====================================================+
| 1     910B3               | OK            | 91.3        37                0    / 0             |
| 0                         | 0000:C2:00.0  | 0           0    / 0          59048/ 65536         |
+===========================+===============+====================================================+
+---------------------------+---------------+----------------------------------------------------+
| NPU     Chip              | Process id    | Process name             | Process memory(MB)      |
+===========================+===============+====================================================+
| 0       0                 | 1538118       | python                   | 60                      |
+===========================+===============+====================================================+
| No running processes found in NPU 1                                                            |
+===========================+===============+====================================================+
"""


class _ProcResult:
    def __init__(self, returncode: int = 0, stdout: str = ""):
        self.returncode = returncode
        self.stdout = stdout


_FAKE_NPU_SMI = "/usr/local/Ascend/driver/tools/npu-smi"


def _patch_npu_smi_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """模拟 npu-smi 已安装（路径解析成功），使测试不依赖真实环境。"""
    monkeypatch.setattr(nm, "_npu_smi_path", lambda: _FAKE_NPU_SMI)


# ---------------------------------------------------------------------------
# parse_npu_smi_info
# ---------------------------------------------------------------------------

def test_parse_two_npus():
    npus = nm.parse_npu_smi_info(_SAMPLE_910B)
    assert len(npus) == 2

    npu0 = npus[0]
    assert npu0["id"] == 0
    assert npu0["name"] == "910B3"
    assert npu0["health"] == "OK"
    assert npu0["power_w"] == 92.5
    assert npu0["temp_c"] == 42.0
    assert npu0["aicore_percent"] == 12.0
    assert npu0["memory_used_mb"] == 2620.0
    assert npu0["memory_total_mb"] == 65536.0
    assert npu0["bus_id"] == "0000:C1:00.0"
    assert len(npu0["chips"]) == 1

    npu1 = npus[1]
    assert npu1["id"] == 1
    assert npu1["aicore_percent"] == 5.0


def test_parse_hbm_fallback_when_ddr_memory_zero():
    """910B（npu-smi 25.5.0）DDR 内存列为 0 / 0 时，显存回退取 HBM-Usage 列。"""
    npus = nm.parse_npu_smi_info(_SAMPLE_910B_HBM)
    assert len(npus) == 2  # 进程表行不产生假 NPU 记录

    npu0 = npus[0]
    assert npu0["id"] == 0
    assert npu0["name"] == "910B3"
    assert npu0["power_w"] == 100.0
    assert npu0["temp_c"] == 50.0
    assert npu0["aicore_percent"] == 0.0
    assert npu0["memory_used_mb"] == 3546.0
    assert npu0["memory_total_mb"] == 65536.0
    assert npu0["bus_id"] == "0000:C1:00.0"

    # `59048/ 65536` 数字与斜杠间无空格也要能解析
    npu1 = npus[1]
    assert npu1["memory_used_mb"] == 59048.0
    assert npu1["memory_total_mb"] == 65536.0


def test_parse_empty_output():
    assert nm.parse_npu_smi_info("") == []
    assert nm.parse_npu_smi_info("+---+\n| NPU  Name | Health | Power |\n+---+\n") == []


def test_parse_skips_orphan_chip_rows():
    """没有先行 NPU 行的 chip 行被跳过，不产生记录。"""
    output = "| 0       0                     | 0000:C1:00.0    | 12           2620 / 65536  |"
    assert nm.parse_npu_smi_info(output) == []


def test_parse_missing_numeric_fields():
    """第三段数字缺失时字段为 None，不中断解析。"""
    output = (
        "| 0       910B3                 | OK              |                             |\n"
        "| 0       0                     | 0000:C1:00.0    |                             |"
    )
    npus = nm.parse_npu_smi_info(output)
    assert len(npus) == 1
    assert npus[0]["power_w"] is None
    assert npus[0]["temp_c"] is None
    assert npus[0]["aicore_percent"] is None
    assert npus[0]["memory_used_mb"] is None
    assert npus[0]["bus_id"] == "0000:C1:00.0"


# ---------------------------------------------------------------------------
# query_npu_status
# ---------------------------------------------------------------------------

def test_query_command_not_found(monkeypatch: pytest.MonkeyPatch):
    """npu-smi 未安装：路径解析返回 None，优雅降级。"""
    monkeypatch.setattr(nm, "_npu_smi_path", lambda: None)
    result = nm.query_npu_status()
    assert result["available"] is False
    assert result["npus"] == []
    assert "npu-smi" in result["reason"]


def test_query_command_exec_fails(monkeypatch: pytest.MonkeyPatch):
    """路径解析成功但执行时报 FileNotFoundError（如驱动被卸载）也要优雅降级。"""
    _patch_npu_smi_found(monkeypatch)

    def _raise(*args, **kwargs):
        raise FileNotFoundError("npu-smi")

    monkeypatch.setattr(nm.subprocess, "run", _raise)
    result = nm.query_npu_status()
    assert result["available"] is False
    assert result["npus"] == []
    assert "npu-smi" in result["reason"]


def test_query_timeout(monkeypatch: pytest.MonkeyPatch):
    _patch_npu_smi_found(monkeypatch)

    def _raise(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd="npu-smi", timeout=10)

    monkeypatch.setattr(nm.subprocess, "run", _raise)
    result = nm.query_npu_status()
    assert result["available"] is False
    assert "超时" in result["reason"]


def test_query_nonzero_exit(monkeypatch: pytest.MonkeyPatch):
    _patch_npu_smi_found(monkeypatch)
    monkeypatch.setattr(
        nm.subprocess, "run", lambda *a, **k: _ProcResult(returncode=1, stdout="perm denied")
    )
    result = nm.query_npu_status()
    assert result["available"] is False
    assert "perm denied" in result["reason"]


def test_query_unparseable_output(monkeypatch: pytest.MonkeyPatch):
    _patch_npu_smi_found(monkeypatch)
    monkeypatch.setattr(nm.subprocess, "run", lambda *a, **k: _ProcResult(stdout="garbage"))
    result = nm.query_npu_status()
    assert result["available"] is False
    assert "解析" in result["reason"]


def test_query_success(monkeypatch: pytest.MonkeyPatch):
    _patch_npu_smi_found(monkeypatch)
    monkeypatch.setattr(nm.subprocess, "run", lambda *a, **k: _ProcResult(stdout=_SAMPLE_910B))
    result = nm.query_npu_status()
    assert result["available"] is True
    assert len(result["npus"]) == 2
    assert result["reason"] == ""


# ---------------------------------------------------------------------------
# handler 与注册
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_hardware_npu_status(monkeypatch: pytest.MonkeyPatch):
    _patch_npu_smi_found(monkeypatch)
    monkeypatch.setattr(nm.subprocess, "run", lambda *a, **k: _ProcResult(stdout=_SAMPLE_910B))
    payload = await nm.handle_hardware_npu_status({})
    assert payload["available"] is True
    assert payload["npus"][0]["name"] == "910B3"


def test_npu_rpc_registered():
    from jiuwenswarm.common.schema.message import ReqMethod
    from jiuwenswarm.gateway.channel_manager.web import app_web_handlers
    from jiuwenswarm.server.runtime.agent_adapter import interface

    assert ReqMethod.HARDWARE_NPU_STATUS.value == "hardware.npu.status"
    assert ReqMethod.HARDWARE_NPU_STATUS in interface._STATELESS_FUNC_ROUTES
    assert "hardware.npu.status" in app_web_handlers._FORWARD_REQ_METHODS
    assert "hardware.npu.status" in app_web_handlers._FORWARD_NO_LOCAL_HANDLER_METHODS
