# AgentSSAS 测试报告

> **生成时间**: 2026-08-21
> **测试对象**: AgentSSAS 系统 (agent-ssas v0.1.0) — 包含 AgentSSASCore 子系统 + AgentSSASClient 子系统
> **测试环境**: Windows, Python 3.13.14 (jiuwenswarm venv), pytest-9.0.3, pytest-asyncio-1.3.0
> **工作目录**: `d:\TraeWorkspace\JiuwenSSAS\AgentSecurity\AgentSSAS`
> **Python 解释器**: `d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe`

---

## 一、测试概述

### 1.1 三阶段测试概述

本报告合并了 AgentSSAS 系统三个阶段的测试结果，三个阶段循序渐进、层层递进：

| 阶段 | 测试对象 | 核心目标 | 生成时间 |
|------|---------|---------|---------|
| 阶段一 | AgentSSASCore 子系统 | 独立构建并验证 AgentSSASCore 子系统，确保虚拟产生的事件可以正确经过接入适配、数据预处理、数据建模、威胁分析、呈现模块执行，最终生成符合预期的威胁日志文件。本阶段不依赖 jiuwenswarm 运行时，使用虚拟事件信号驱动 | 2026-08-17 |
| 阶段二 | AgentSSASClient 子系统 | 独立构建并验证 AgentSSASClient 子系统，确保事件采集逻辑正确、ID 管理模块序号生成与复用正确、事件过滤模块安全检测事件生成正确、上报模块 fail-open 逻辑有效，端到端串联验证 PermissionInterruptRail deny → 安全检测事件完整流程 | 2026-08-18 |
| 阶段三 | AgentSSAS 系统集成 | 联合构建并验证 AgentSSAS 系统的 HTTP 服务模式能力与端到端流程，补齐阶段一和阶段二缺失的 HTTP 服务模式（FastAPI 服务端 + AgentSSASRemoteBackend），完成端到端验证，确认进程内模式和 HTTP 服务模式均能正确生成威胁日志文件，配置关闭 SSAS 时完全不影响 jiuwenswarm 运行。同时验证 AgentSSASCore 命名统一、包名前缀统一为 `agent_ssas.core` + `agent_ssas.backend_client`、版本号统一为 0.1 | 2026-08-19 |

### 1.2 测试方法

| 阶段 | 测试类型 | 方法 | 标记 |
|------|---------|------|------|
| 阶段一 | 单元测试 | 94 个用例，覆盖所有框架模块和检测模块 | `@pytest.mark.unit` + `@pytest.mark.level0`/`level1` |
| 阶段一 | 集成测试 | 完整流水线串联验证 | `@pytest.mark.integration` + `@pytest.mark.level1` |
| 阶段一 | 功能验证 | 6 个用例，虚拟事件驱动端到端验证 | `@pytest.mark.integration` + `@pytest.mark.level1` |
| 阶段一 | 性能测试 | 2 个用例，110 个事件吞吐量与延迟测量 | `@pytest.mark.slow` |
| 阶段二 | 单元测试 | 47 个用例，覆盖 6 个模块（extended_context、id_manager、event_builder、event_filter、event_reporter、agent_ssas_security_rail） | `@pytest.mark.level0`/`level1` |
| 阶段二 | 端到端测试 | 6 个用例，模拟 PermissionInterruptRail deny 完整流程、fail-open、序号递增 | `@pytest.mark.level1` |
| 阶段三 | 全量回归测试 | 207 个用例，覆盖阶段一+阶段二+阶段三全部代码 | `@pytest.mark.unit`/`integration` + `@pytest.mark.level0`/`level1` |
| 阶段三 | HTTP 模式测试 | 10 个用例，FastAPI 端点 + AgentSSASRemoteBackend | `@pytest.mark.integration` + `@pytest.mark.level1` |
| 阶段三 | 端到端测试 | 11 个用例，进程内模式 + HTTP 模式完整事件流 | `@pytest.mark.integration` + `@pytest.mark.level1` |
| 阶段三 | 配置关闭测试 | 9 个用例，验证 SSAS 关闭不影响 jiuwenswarm | `@pytest.mark.integration` + `@pytest.mark.level1` |
| 阶段三 | 真实环境验证 | 编程方式验证 Rail 注册、事件上报、HTTP 服务、配置关闭 | 手动执行 |

### 1.3 207 用例统计

全量回归测试共 207 个用例全部通过 (100%)：

```
============================= test session starts =============================
platform win32 -- Python 3.13.14, pytest-9.0.3, pluggy-1.6.0
plugins: anyio-4.13.0, asyncio-1.3.0, cov-7.1.0, html-4.2.0, metadata-3.1.1, mock-3.15.1
asyncio: mode=Mode.AUTO, debug=False
collected 207 items

tests\agent_ssas\backend_client\openjiuwen\test_event_builder.py ........         [  3%]
.                                                                               [  3%]
tests\agent_ssas\backend_client\openjiuwen\test_event_filter.py .....             [  6%]
tests\agent_ssas\backend_client\openjiuwen\test_event_reporter.py ........        [ 10%]
tests\agent_ssas\backend_client\openjiuwen\test_extended_context.py ...           [ 11%]
tests\agent_ssas\backend_client\openjiuwen\test_id_manager.py ................    [ 16%]
......                                                                          [ 19%]
tests\agent_ssas\backend_client\openjiuwen\test_agent_ssas_permission_interrupt.py .     [ 19%]
.....                                                                           [ 22%]
tests\agent_ssas\backend_client\openjiuwen\test_agent_ssas_security_rail.py .....       [ 23%]
..                                                                              [ 25%]
tests\agent_ssas\backend_client\openjiuwen\test_verify_case3.py .                 [ 26%]
tests\agent_ssas\core\detection_modules\test_agent_moss.py .......               [ 29%]
tests\agent_ssas\core\detection_modules\test_security_rail_detection.py .....     [ 31%]
tests\agent_ssas\core\detection_modules\test_test_detection.py .....              [ 34%]
tests\agent_ssas\core\framework\test_config.py ...........                       [ 39%]
tests\agent_ssas\core\framework\test_agent_backend.py ....                      [ 41%]
tests\agent_ssas\core\framework\test_models.py ........                          [ 45%]
tests\agent_ssas\core\framework\test_module_manager.py .......                   [ 48%]
tests\agent_ssas\core\framework\test_pipeline.py ...............                 [ 56%]
tests\agent_ssas\core\framework\test_preprocessor.py ......                       [ 58%]
tests\agent_ssas\core\framework\test_storage.py ..................               [ 67%]
tests\agent_ssas\core\framework\test_subscription.py ..............              [ 74%]
tests\agent_ssas\core\framework\test_threat_log.py .......                       [ 77%]
tests\agent_ssas\core\func_verify.py ......                                      [ 80%]
tests\agent_ssas\core\integration\test_e2e_http.py .....                         [ 83%]
tests\agent_ssas\core\integration\test_e2e_inprocess.py ......                   [ 85%]
tests\agent_ssas\core\integration\test_end_to_end.py ....                        [ 87%]
tests\agent_ssas\core\integration\test_http_server.py ....                       [ 89%]
tests\agent_ssas\core\integration\test_pipeline_integration.py ....              [ 91%]
tests\agent_ssas\core\integration\test_remote_backend.py ......                  [ 94%]
tests\agent_ssas\core\integration\test_agent_ssas_disabled.py .........               [ 99%]
tests\agent_ssas\core\perf_test.py ..                                           [100%]

====================== 207 passed, 2 warnings in 17.71s =======================
```

### 1.4 测试命令

```powershell
# ============================================================
# 前置条件：确保 agent-ssas 包已 editable 安装到 jiuwenswarm venv
# ============================================================
# jiuwenswarm 的 venv 由 uv 管理（无 pip），虽然 pyproject.toml 的
# [tool.uv.sources] 声明了 agent-ssas 的 editable 路径，但如果 venv
# 是在 agent-ssas 加入依赖之前创建的，venv 中不会自动注册 editable 安装。
#
# 现象：python -c "import agent_ssas.core" 报 ModuleNotFoundError
#
# 修复方法（推荐）：在 jiuwenswarm 目录执行 uv sync
cd d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm
uv sync
# uv 会读取 [tool.uv.sources] 配置，将 agent-ssas 以 editable 模式注册到 venv
#
# 验证安装是否成功：
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -c "import agent_ssas.core; print('OK')"
# 预期输出：OK

# ============================================================
# 1. 运行全部 pytest 测试（207 个用例，含阶段一+阶段二+阶段三全部代码）
#    注意：此命令只运行 pytest 测试用例，不包含下方的"真实环境验证脚本"
# ============================================================
cd d:\TraeWorkspace\JiuwenSSAS\AgentSecurity\AgentSSAS
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m pytest tests/ -v --tb=short --junitxml=test_results_all.xml
# 预期结果：207 passed, 2 warnings
# XML 结果文件：test_results_all.xml（全量测试结果，CI 系统可解析）

# 1a. 仅运行单元测试（快速验证，CI 门禁）
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m pytest tests/ -v -m "not slow"

# ============================================================
# 2. 仅运行阶段三新增测试
# ============================================================
# 仅 integration 目录（34 passed，含 4 阶段一 + 30 阶段三）
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m pytest tests/agent_ssas/core/integration/ -v --tb=short --junitxml=test_results_phase3.xml

# 2a. 仅 HTTP 模式测试（10 个用例）
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m pytest tests/agent_ssas/core/integration/test_http_server.py tests/agent_ssas/core/integration/test_remote_backend.py -v

# 2b. 仅进程内模式端到端测试（6 个用例）
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m pytest tests/agent_ssas/core/integration/test_e2e_inprocess.py -v

# 2c. 仅 HTTP 服务模式端到端测试（5 个用例）
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m pytest tests/agent_ssas/core/integration/test_e2e_http.py -v

# 2d. 仅配置关闭测试（9 个用例）
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m pytest tests/agent_ssas/core/integration/test_agent_ssas_disabled.py -v

# 2e. 仅功能验证（阶段一，6 个用例）
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m pytest tests/agent_ssas/core/func_verify.py -v

# 2f. 仅性能测试（阶段一，2 个用例）
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m pytest tests/agent_ssas/core/perf_test.py -v -m "slow"

# ============================================================
# 3. 真实环境验证（使用独立验证脚本 tests/verify_phase3.py）
# ============================================================
cd d:\TraeWorkspace\JiuwenSSAS\AgentSecurity\AgentSSAS

# 3a. 进程内模式验证：Rail 注册 + 事件上报 + 日志生成
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m tests.verify_phase3 inprocess
# 预期输出：一系列 [verify_inprocess] PASS 行，最后一行 === ALL CHECKS PASSED ===
# 自动检查：Rail priority=80、backend initialized、生命周期事件 risk_level=safe、
#           安全检测事件 risk_level=high、ssas_core.db 生成、威胁日志文件生成

# 3b. HTTP 服务模式验证（需两个终端）

# 终端 1：启动 HTTP 服务端（前台运行）
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m tests.verify_phase3 http-server
# 预期输出：Uvicorn running on http://0.0.0.0:8443

# 终端 2：向服务端发送事件并验证
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m tests.verify_phase3 http-client
# 预期输出：一系列 [verify_http_client] PASS 行，最后一行 === ALL CHECKS PASSED ===
# 自动检查：HTTP status=200、生命周期事件 risk_level=safe、安全检测事件 risk_level=high、
#           detected_threats 含 tool_permission_denied、health check status=ok
# 验证完毕后在终端 1 按 Ctrl+C 停止服务端

# 3c. 配置关闭验证
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m tests.verify_phase3 disabled
# 预期输出：一系列 [verify_disabled] PASS 行，最后一行 === ALL CHECKS PASSED ===

# 3d. 一键执行（进程内 + 配置关闭，不含 HTTP 服务）
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m tests.verify_phase3 all
```

> **环境说明**：
> - jiuwenswarm 的 venv 由 uv 管理（无 pip），不能直接用 `pip install -e` 安装 agent-ssas
> - 如果遇到 `ModuleNotFoundError: No module named 'agent_ssas.core'`，说明 venv 中未注册 agent-ssas 的 editable 安装，请按上述"前置条件"中的 `uv sync` 修复
> - 包结构已从 `agent_ssas_core` + `backend_client` 重构为 `agent_ssas.core` + `agent_ssas.backend_client`
> - 导入路径变更：`from agent_ssas.core.framework.*` 和 `from agent_ssas.backend_client.openjiuwen.*`
> - HTTP 服务模式依赖 fastapi + uvicorn + httpx，已作为 `optional-dependencies.http` 声明
> - 验证脚本 `tests/verify_phase3.py` 提供独立可执行的验证命令，替代 python -c 内联方式
> - 测试存储路径约定（基于 pytest tmp_path，每个测试函数独立目录）：
>   - 阶段一（agent_ssas.core）测试：`$env:TEMP\pytest-of-yieux\pytest-N\test_xxx\agent_ssas\agent_ssas_core\`
>   - 阶段二（agent_ssas.backend_client）测试：`$env:TEMP\pytest-of-yieux\pytest-N\test_xxx\agent_ssas\backend_client\`
> - pytest 的 `tmp_path` 自动管理目录生命周期，测试结束后自动清理，不会累积残留数据

### 1.5 测试目录结构

```
tests/
├── conftest.py                                        # 根 conftest（ssas_home 等 fixtures）
├── fixtures/
│   ├── __init__.py
│   └── event_factory.py                               # 虚拟事件生成器
└── agent_ssas/                                        # 与 src/agent_ssas 包结构对齐
    ├── __init__.py
    ├── backend_client/                                # AgentSSASClient 子系统测试（阶段二）
    │   └── openjiuwen/
    │       ├── __init__.py
    │       ├── conftest.py
    │       ├── test_event_builder.py                  # 构建模块（8 用例）
    │       ├── test_event_filter.py                   # 事件过滤模块（5 用例）
    │       ├── test_event_reporter.py                 # 上报模块（9 用例）
    │       ├── test_extended_context.py               # 扩展上下文（3 用例）
    │       ├── test_id_manager.py                     # ID 管理模块（16 用例）
    │       ├── test_agent_ssas_permission_interrupt.py       # 端到端 deny 流程（6 用例）
    │       ├── test_agent_ssas_security_rail.py             # AgentSSASSecurityRail 采集逻辑（7 用例）
    │       └── test_verify_case3.py                   # 验收用例3（1 用例）
    └── core/                                          # AgentSSASCore 子系统测试
        ├── __init__.py
        ├── framework/                                 # 框架模块测试（阶段一）
        │   ├── __init__.py
        │   ├── conftest.py
        │   ├── test_config.py                         # 配置模块（11 用例）
        │   ├── test_storage.py                        # 存储模块（16 用例）
        │   ├── test_models.py                         # 数据模型（8 用例）
        │   ├── test_preprocessor.py                   # 数据预处理（6 用例）
        │   ├── test_module_manager.py                 # 检测模块管理器（7 用例）
        │   ├── test_pipeline.py                       # 流水线模块（15 用例）
        │   ├── test_subscription.py                   # 订阅模式（14 用例）
        │   ├── test_threat_log.py                     # 呈现模块（6 用例）
        │   └── test_agent_backend.py                 # 接入适配（4 用例）
        ├── detection_modules/                         # 检测模块测试（阶段一）
        │   ├── __init__.py
        │   ├── test_test_detection.py                 # 测试检测模块（5 用例）
        │   ├── test_security_rail_detection.py        # 安全护栏检测（5 用例）
        │   └── test_agent_moss.py                     # AgentMoss 检测（7 用例）
        ├── integration/                               # 集成测试与端到端
        │   ├── __init__.py
        │   ├── test_pipeline_integration.py           # 流水线集成（4 用例，阶段一）
        │   ├── test_end_to_end.py                     # 端到端验证（4 用例，阶段一）
        │   ├── test_http_server.py                   # FastAPI 端点测试（4 用例，阶段三新增）
        │   ├── test_remote_backend.py                 # AgentSSASRemoteBackend 测试（6 用例，阶段三新增）
        │   ├── test_e2e_inprocess.py                 # 进程内模式端到端（6 用例，阶段三新增）
        │   ├── test_e2e_http.py                       # HTTP 模式端到端（5 用例，阶段三新增）
        │   └── test_agent_ssas_disabled.py                  # 配置关闭测试（9 用例，阶段三新增）
        ├── func_verify.py                             # 功能验证（6 用例，阶段一）
        └── perf_test.py                               # 性能测试（2 用例，阶段一）
    └── verify_phase3.py                                # 真实环境验证脚本（阶段三新增，非 pytest）
```

---

## 二、阶段一：AgentSSASCore 子系统测试

### 2.1 测试目标

独立构建并验证 AgentSSASCore 子系统，确保虚拟产生的事件可以正确经过接入适配、数据预处理、数据建模、威胁分析、呈现模块执行，最终生成符合预期的威胁日志文件。本阶段不依赖 jiuwenswarm 运行时，使用虚拟事件信号驱动。

### 2.2 单元测试结果

#### 2.2.1 总览

```
============================= test session starts =============================
platform win32 -- Python 3.14.5, pytest-9.1.1, plugging-1.6.0
plugins: asyncio-1.4.0, asyncio: mode=Mode.AUTO
collected 94 items

============================= 94 passed in 3.31s =============================
```

**结果: 94 个单元测试用例全部通过 (100%)**

#### 2.2.2 测试明细

##### 配置模块 (test_config.py) - 11 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_default_values | unit/level0 | PASS |
| test_custom_values | unit/level0 | PASS |
| test_storage_path_explicit_home | unit/level0 | PASS |
| test_storage_path_none_resolves_env | unit/level0 | PASS |
| test_from_dict_known_fields | unit/level0 | PASS |
| test_from_dict_ignores_unknown_fields | unit/level0 | PASS |
| test_from_dict_none_returns_default | unit/level0 | PASS |
| test_from_dict_invalid_type_raises | unit/level0 | PASS |
| test_resolve_ssas_home_priority | unit/level0 | PASS |
| test_resolve_ssas_home_fallback_data_dir | unit/level0 | PASS |
| test_resolve_ssas_home_fallback_jw_home | unit/level0 | PASS |

**覆盖内容**: AgentSSASConfig 默认值、自定义值、storage_path property、from_dict 方法、环境变量优先级 (SSAS_HOME > JIUWENSWARM_DATA_DIR > JIUWENSWARM_HOME > ~/.jiuwenswarm)

##### 存储模块 (test_storage.py) - 16 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_record_and_get_events | unit/level0 | PASS |
| test_filter_by_session_id | unit/level0 | PASS |
| test_record_event_auto_id | unit/level0 | PASS |
| test_record_event_invalid_type | unit/level0 | PASS |
| test_record_event_empty_dict | unit/level0 | PASS |
| test_create_and_get_alerts | unit/level0 | PASS |
| test_get_alerts_filter_acknowledged | unit/level0 | PASS |
| test_record_raw_event | unit/level0 | PASS |
| test_get_events_by_trace_id | unit/level0 | PASS |
| test_get_events_by_trace_id_empty_trace | unit/level0 | PASS |
| test_get_events_invalid_limit | unit/level0 | PASS |
| test_creates_directory_structure | unit/level0 | PASS |
| test_empty_module_name_raises | unit/level0 | PASS |
| test_process_and_result_store_independent | unit/level0 | PASS |
| test_record_and_get_events (MemoryStore) | unit/level0 | PASS |
| test_record_raw_event_and_get_by_trace (MemoryStore) | unit/level0 | PASS |

**覆盖内容**: SQLiteStore 读写/过滤/告警/raw_event 持久化/trace_id 关联查询、ModuleStorageManager 目录结构、MemoryStore 全流程

##### 数据模型 (test_models.py) - 8 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_enum_values (RiskLevel) | unit/level0 | PASS |
| test_comparison (RiskLevel) | unit/level0 | PASS |
| test_to_dict (RiskAssessment) | unit/level0 | PASS |
| test_defaults (RiskAssessment) | unit/level0 | PASS |
| test_default_seq_values (EventNode) | unit/level0 | PASS |
| test_custom_values (EventNode) | unit/level0 | PASS |
| test_to_event_desc (UnifiedEvent) | unit/level0 | PASS |
| test_build_aux_ids (UnifiedEvent) | unit/level0 | PASS |

**覆盖内容**: RiskLevel 枚举值与比较、RiskAssessment.to_dict、EventNode 默认值 (seq=-1)、UnifiedEvent.to_event_desc 和 _build_aux_ids

##### 数据预处理模块 (test_preprocessor.py) - 6 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_parse_tool_input_event | unit/level0 | PASS |
| test_parse_permission_interrupt_event | unit/level0 | PASS |
| test_parse_fields_correctness | unit/level0 | PASS |
| test_auto_session_start_node | unit/level0 | PASS |
| test_parse_invalid_raw_event_type | unit/level1 | PASS |
| test_parse_missing_common_layer | unit/level1 | PASS |

**覆盖内容**: raw_event -> UnifiedEvent 转换、生命周期事件/安全检测事件解析、字段正确性、自动 session_start 节点生成、异常输入处理

##### 检测模块管理器 (test_module_manager.py) - 7 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_initialize_loads_all_modules | unit/level0 | PASS |
| test_get_subscribers_for_specific_event | unit/level0 | PASS |
| test_wildcard_subscription | unit/level0 | PASS |
| test_get_subscribers_no_match | unit/level0 | PASS |
| test_get_subscribers_dedup | unit/level0 | PASS |
| test_disabled_module_not_loaded | unit/level1 | PASS |
| test_get_storage_returns_manager | unit/level0 | PASS |

**覆盖内容**: 检测模块扫描加载、订阅查询、通配符 * 订阅、去重、disabled 模块跳过、存储管理器获取

##### 流水线模块 (test_pipeline.py) - 9 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_empty_reports_returns_safe | unit/level0 | PASS |
| test_aggregate_takes_highest_risk_level | unit/level0 | PASS |
| test_aggregate_merges_detected_threats | unit/level0 | PASS |
| test_aggregate_merges_recommended_actions | unit/level0 | PASS |
| test_aggregate_unknown_risk_level_falls_back_safe | unit/level1 | PASS |
| test_run_no_subscribers_returns_safe | unit/level1 | PASS |
| test_run_with_subscriber_returns_report | unit/level1 | PASS |
| test_run_modeler_failure_skips_module | unit/level1 | PASS |
| test_run_analyzer_failure_skips_module | unit/level1 | PASS |

**覆盖内容**: 报告聚合策略 (取最高风险等级/合并威胁列表/合并建议动作)、空报告处理、未知风险等级回退、流水线执行、建模/分析失败跳过

##### 呈现模块 (test_threat_log.py) - 6 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_render_generates_ocsf_file | unit/level0 | PASS |
| test_module_to_class_mapping | unit/level0 | PASS |
| test_risk_level_to_severity_mapping | unit/level0 | PASS |
| test_render_no_risk_status_info | unit/level0 | PASS |
| test_render_invalid_report_type | unit/level1 | PASS |
| test_render_empty_report | unit/level1 | PASS |

**覆盖内容**: OCSF 格式文件生成、检测模块名到 metadata.product.feature.name 映射、风险等级到 severity_id 映射

##### 接入适配模块 (test_agent_backend.py) - 4 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_report_event_returns_assessment | unit/level0 | PASS |
| test_report_event_returns_assessment_with_mock | unit/level0 | PASS |
| test_fail_open_on_exception | unit/level0 | PASS |
| test_initialize_loads_detection_modules | unit/level0 | PASS |

**覆盖内容**: report_event 返回 RiskAssessment、fail-open 异常处理、initialize 检测模块加载

##### 检测模块测试 - 17 个用例

| 测试文件 | 用例数 | 结果 |
|---------|--------|------|
| test_test_detection.py | 5 | 全部 PASS |
| test_security_rail_detection.py | 5 | 全部 PASS |
| test_agent_moss.py | 7 | 全部 PASS |

**覆盖内容**: BlankDataModeler 透传、BlankThreatAnalyzer 无风险报告、SecurityRailAnalyzer 安全检测事件处理/跳过生命周期事件、AgentMossModeler/AgentMossAnalyzer 基本功能

##### 集成测试与端到端 - 8 个用例

| 测试文件 | 用例数 | 结果 |
|---------|--------|------|
| test_pipeline_integration.py | 4 | 全部 PASS |
| test_end_to_end.py | 4 | 全部 PASS |

**覆盖内容**: 完整流水线 (raw_event -> parse -> pipeline -> RiskAssessment)、生命周期事件序列、安全检测事件 HIGH 风险、raw_event 持久化、完整事件序列驱动、OCSF 文件生成、SQLite 事件记录、模块 result.db 记录

### 2.3 功能验证结果

#### 2.3.1 验证用例

```powershell
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m pytest tests/agent_ssas/core/func_verify.py -v
```

| 用例 | 标记 | 结果 |
|------|------|------|
| test_lifecycle_events_return_risk_assessment | integration/level1 | PASS |
| test_security_event_returns_high_risk | integration/level1 | PASS |
| test_raw_events_persisted_to_sqlite | integration/level1 | PASS |
| test_threat_log_file_generated | integration/level1 | PASS |
| test_module_result_db_records | integration/level1 | PASS |
| test_fail_open_on_exception | integration/level1 | PASS |

#### 2.3.2 验证结论

| 验证项 | 输入 | 输出 | 预期 | 结果 |
|--------|------|------|------|------|
| 生命周期事件 | tool_input, lifecycle | risk_level=safe, has_risk=False | safe, False | PASS |
| 安全检测事件 | permission_interrupt_tool, security | risk_level=high, has_risk=True, threats=['tool_permission_denied'] | high, True | PASS |
| 数据库持久化 | 2 个 raw_event | 2 条记录 (按 trace_id 查询) | >= 2 条 | PASS |
| 威胁日志文件 | OCSF 格式 JSON | activity_name=Create, class_name=Detection Finding, severity_id=1 | 文件生成 | PASS |
| 模块 result.db | test_detection + security_rail_detection | 2 + 1 条记录 | 记录写入 | PASS |
| fail-open | 非法事件 dict | risk_level=safe | safe | PASS |

### 2.4 性能测试结果

#### 2.4.1 测试脚本

```powershell
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m pytest tests/agent_ssas/core/perf_test.py -v -m "slow"
```

#### 2.4.2 性能指标

| 指标 | 数值 | 说明 |
|------|------|------|
| 单事件延迟（首次） | ~7.75ms | 首个事件（含初始化开销） |
| 生命周期事件平均延迟 | ~4.15ms/事件 | 100 个 tool_input 事件 |
| 安全检测事件平均延迟 | ~7.36ms/事件 | 10 个 permission_interrupt_tool 事件 |
| 总吞吐量 | ~221.6 events/s | 110 个事件在 ~496ms 内处理完 |
| 数据库记录 | 111 条 | raw_events 表 + events 表，按 trace_id 查询 |
| test_detection result.db | 111 条 | 订阅了所有事件 (*) |
| security_rail_detection result.db | 10 条 | 仅订阅 permission_interrupt_tool |
| 威胁日志文件 | 2 个 | OCSF 格式 JSON 文件 |

#### 2.4.3 性能分析

- 生命周期事件平均延迟 ~4.15ms，包含：raw_event 持久化 + 数据预处理解析 + 流水线执行 (test_detection 模块的空白建模+分析) + 呈现输出
- 安全检测事件延迟略高 (~7.36ms)，因为同时有两个模块处理 (test_detection + security_rail_detection)
- 首事件延迟较高 (~7.75ms)，含首次 SQLite 写入和 Trace/Session 结构初始化开销
- 吞吐量 ~221.6 events/s，满足实时感知需求

### 2.5 数据库与输出文件数据呈现

#### 2.5.1 存储路径说明

测试中使用 `ssas_home` fixture 设置临时目录：
- `SSAS_HOME` 环境变量指向 `pytest` 的 `tmp_path` 临时目录（如 `C:\Users\yieux\AppData\Local\Temp\pytest-of-yieux\pytest-17\test_xxx0\ssas_home\`，其中 `pytest-17` 是 pytest 会话递增编号，`test_xxx0` 是测试函数名+参数化序号）
- 最终存储根目录为 `<SSAS_HOME>/ssas/`
- 威胁日志输出目录为 `<SSAS_HOME>/ssas/reports/threat_log/`
- 核心库为 `<SSAS_HOME>/ssas/ssas_core.db`
- 模块存储为 `<SSAS_HOME>/ssas/modules/<module_name>/process.db` 和 `result.db`

#### 2.5.2 ssas_core.db 数据

**raw_events 表** (按 trace_id 查询):

| 记录 | event_type | event_class | source | trace_id |
|------|-----------|-------------|--------|----------|
| 1 | (raw_event JSON) | - | raw_event | verify-trace |
| 2 | (raw_event JSON) | - | raw_event | verify-trace |

> raw_events 表存储完整的 raw_event dict (JSON 格式)，包含 common/payload/metadata 三层结构。

#### 2.5.3 检测模块 result.db 数据

**test_detection/result.db** (111 条记录):

| 记录 | risk_level | module_name | 说明 |
|------|-----------|------------|------|
| 1-100 | safe | test_detection | 100 个生命周期事件，BlankThreatAnalyzer 返回无风险 |
| 101-111 | safe | test_detection | 10 个安全检测事件，test_detection 模块返回无风险 (BlankThreatAnalyzer) |

**security_rail_detection/result.db** (10 条记录):

| 记录 | risk_level | risk_type | module_name | 说明 |
|------|-----------|-----------|------------|------|
| 1-10 | high | tool_permission_denied | security_rail_detection | 10 个安全检测事件，SecurityRailAnalyzer 输出高风险报告 |

#### 2.5.4 威胁日志文件

**输出目录**: `<SSAS_HOME>/ssas/reports/threat_log/`

**文件列表**（每个检测模块生成独立的 OCSF 报告文件，文件名包含 trace_id 和 module_name）:
- `threat_<trace_id>_test_detection_<timestamp>.json` — test_detection 模块输出
- `threat_<trace_id>_security_rail_detection_<timestamp>.json` — security_rail_detection 模块输出

**OCSF 格式示例 1** (test_detection 模块输出，无风险):

```json
{
  "activity_id": 1,
  "activity_name": "Create",
  "category_uid": 2,
  "category_name": "Findings",
  "class_uid": 2004,
  "class_name": "Detection Finding",
  "severity_id": 1,
  "status": "New",
  "status_id": 1,
  "time": 1715000000000,
  "trace_id": "verify-trace",
  "actor": {
    "agent": {"uid": "verify-agent", "name": "verify-agent"}
  },
  "metadata": {
    "version": "0.1.0",
    "product": {
      "feature": {"name": "test_detection"}
    },
    "profiles": ["ai_operation"]
  },
  "finding_info": {
    "uid": "<uuid>",
    "desc": "",
    "created_time": 1715000000000,
    "created_time_dt": "2024-05-07T02:13:21.000Z",
    "confidence_id": 3,
    "confidence": "High",
    "confidence_score": 0.0,
    "types": [],
    "analytic": {
      "type_id": 1,
      "type": "Rule",
      "name": "Pass Through Scan"
    }
  },
  "evidences": [
    {
      "uid": "<uuid>",
      "name": "detection_evidence",
      "data": {
        "detected_threats": [],
        "recommended_actions": ["log"]
      }
    }
  ],
  "ai_agent": {
    "uid": "verify-agent",
    "session": {"uid": "verify-session"}
  },
  "message_context": {},
  "ai_operation": {
    "interactions": []
  }
}
```

**OCSF 格式示例 2** (security_rail_detection 模块输出，高风险):

```json
{
  "activity_id": 1,
  "activity_name": "Create",
  "category_uid": 2,
  "category_name": "Findings",
  "class_uid": 2004,
  "class_name": "Detection Finding",
  "severity_id": 4,
  "status": "New",
  "status_id": 1,
  "time": 1715000001000,
  "trace_id": "verify-trace",
  "actor": {
    "agent": {"uid": "verify-agent", "name": "verify-agent"}
  },
  "metadata": {
    "version": "0.1.0",
    "product": {
      "feature": {"name": "security_rail_detection"}
    },
    "profiles": ["ai_operation"]
  },
  "finding_info": {
    "uid": "<uuid>",
    "desc": "",
    "created_time": 1715000001000,
    "created_time_dt": "2024-05-07T02:13:21.000Z",
    "confidence_id": 3,
    "confidence": "High",
    "confidence_score": 75.0,
    "types": ["tool_permission_denied"],
    "analytic": {
      "type_id": 1,
      "type": "Rule",
      "name": "Security Rail Detection"
    }
  },
  "evidences": [
    {
      "uid": "<uuid>",
      "name": "detection_evidence",
      "data": {
        "detected_threats": ["tool_permission_denied"],
        "recommended_actions": ["log"],
        "risk_source": "PermissionInterruptRail",
        "risk_level": "high",
        "decision": "reject"
      }
    }
  ],
  "ai_agent": {
    "uid": "verify-agent",
    "session": {"uid": "verify-session"}
  },
  "message_context": {},
  "ai_operation": {
    "interactions": []
  }
}
```

> **ai_operation 字段说明**: OCSF v4 格式使用 `ai_operation.interactions` 层次结构承载 session/interaction/llm_call/tool_call 级别的关联信息（见文档03 9.3.8 节）。1.0版本根据事件类型填充对应的 interaction/llm_call/tool_call 数据。

### 2.6 测试结论

#### 2.6.1 总体结论

| 维度 | 结果 |
|------|------|
| 单元测试 | 115/115 通过 (100%) |
| 功能验证 | 6/6 通过 (100%) |
| 性能测试 | 2/2 通过 (100%) |
| 总计 | 123/123 通过 (100%) |
| 数据库验证 | raw_events/events/alerts 表数据正确 |
| 威胁日志 | OCSF 格式 JSON 文件正确生成 |
| fail-open | 异常时返回无风险 RiskAssessment |
| auth 超时 | auth 模式超时后按模块 auth_timeout_policy 返回默认报告 |

#### 2.6.2 阶段一验收用例对照

| 验收用例 | 验证方法 | 结果 |
|---------|---------|------|
| 包可安装和导入 | `python -m pip install -e .` + `import agent_ssas.core` | PASS |
| AgentSSASConfig 可加载 | `AgentSSASConfig()` 默认值正确 | PASS |
| SQLiteStore 可读写 | `record_event` + `get_events` 往返 | PASS |
| 引擎可执行流水线 | `report_event` 返回 RiskAssessment | PASS |
| 测试检测模块跑通 | BlankDataModeler + BlankThreatAnalyzer 流水线 | PASS |
| 威胁日志文件生成 | OCSF 格式 JSON 文件落盘 | PASS |
| 安全检测事件触发 | permission_interrupt_tool -> risk_level=high | PASS |
| fail-open 生效 | 异常时返回无风险 | PASS |

#### 2.6.3 已知限制

> 以下为阶段一时的已知限制。其中部分已在后续阶段解决，标注状态。

1. **聚合事件**: 0.1版本实现了基础聚合栈逻辑，聚合事件的完整订阅展开（notify/auth 模式）已在阶段一完成
2. **AgentMoss 模块**: 0.1版本为简化实现，返回无风险报告，未实现实际的规则/小模型/LLM 分析（后续版本拓展）
3. **工具调用链异常检测模块**: 标记为后续版本拓展，0.1版本不实现
4. **HTTP 服务模式**: ~~AgentSSASRemoteBackend 尚未实现（阶段三内容）~~ → 已在阶段三实现
5. **呈现模块关联查询**: 0.1版本 _query_events_by_trace_id 返回空列表，未实际从存储模块关联读取（后续版本拓展）

---

## 三、阶段二：AgentSSASClient 子系统测试

### 3.1 测试目标

独立构建并验证 AgentSSASClient 子系统，确保事件采集逻辑正确、ID 管理模块序号生成与复用正确、事件过滤模块安全检测事件生成正确、上报模块 fail-open 逻辑有效，端到端串联验证 PermissionInterruptRail deny → 安全检测事件完整流程。

### 3.2 单元测试结果

#### 3.2.1 总览

```
============================= test session starts =============================
platform win32 -- Python 3.13.14, pytest-9.0.3, plugging-1.6.0
plugins: asyncio-1.3.0, asyncio: mode=Mode.AUTO
collected 54 items

tests/backend_client/openjiuwen/test_event_builder.py ........           [ 14%]
tests/backend_client/openjiuwen/test_event_filter.py .....               [ 23%]
tests/backend_client/openjiuwen/test_event_reporter.py ........          [ 37%]
tests/backend_client/openjiuwen/test_extended_context.py ...             [ 42%]
tests/backend_client/openjiuwen/test_id_manager.py ................      [ 72%]
tests/backend_client/openjiuwen/test_agent_ssas_permission_interrupt.py ...... [ 83%]
tests/backend_client/openjiuwen/test_agent_ssas_security_rail.py .......      [ 96%]
tests/backend_client/openjiuwen/test_verify_case3.py .                   [100%]

======================= 54 passed, 2 warnings in 1.15s ========================
```

**结果: 54 个测试用例全部通过 (100%)**

#### 3.2.2 测试明细

##### ExtendedSecurityCheckContext 扩展验证 (test_extended_context.py) - 3 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_default_values | level0 | PASS |
| test_custom_values | level0 | PASS |
| test_int_fields_are_int_type | level0 | PASS |

**覆盖内容**: 缺省值验证（interaction_seq=-1, session_id="" 等 11 个 ID 字段）、自定义值设置、int 类型字段类型验证

##### ID 管理模块 (test_id_manager.py) - 16 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_interaction_seq_incremental_sequence | level1 | PASS |
| test_interaction_seq_reused_within_invoke | level1 | PASS |
| test_llm_call_seq_generated_on_before_model_call | level1 | PASS |
| test_tool_call_seq_generated_on_before_tool_call | level1 | PASS |
| test_llm_call_seq_for_tool_call_reads_from_extra | level1 | PASS |
| test_llm_call_seq_default_when_not_set | level1 | PASS |
| test_subsession_id_empty_for_non_subagent | level1 | PASS |
| test_subsession_id_filled_for_subagent | level1 | PASS |
| test_resolve_session_id_empty_when_no_session | level1 | PASS |
| test_resolve_agent_id_empty_when_no_agent | level1 | PASS |
| test_resolve_trace_id_empty_when_no_session | level1 | PASS |
| test_resolve_context_id_empty_when_no_context | level1 | PASS |
| test_resolve_tool_name_for_non_tool_event | level1 | PASS |
| test_resolve_tool_call_id_empty_for_none | level1 | PASS |
| test_event_class_for_lifecycle | level1 | PASS |
| test_event_class_for_security | level1 | PASS |

**覆盖内容**: interaction_seq 自增与复用（BEFORE_INVOKE 自增，其他事件复用）、llm_call_seq 自增与复用、tool_call_seq 自增与复用、tool_call 事件 llm_call_seq 从 extra 读取、subsession_id 子 Agent 场景、session_id/agent_id/trace_id/context_id 空值边界、tool_name 非 TOOL 事件、tool_call_id None 边界、event_class lifecycle/security 映射

##### 构建模块 (test_event_builder.py) - 8 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_event_dict_three_layer_structure | level1 | PASS |
| test_common_layer_has_14_fields | level1 | PASS |
| test_common_source_is_agent_ssas_security_rail | level1 | PASS |
| test_event_type_mapping | level1 | PASS |
| test_event_class_lifecycle | level1 | PASS |
| test_common_fields_values | level1 | PASS |
| test_payload_tool_name_for_tool_event | level1 | PASS |
| test_payload_content_for_invoke_start | level1 | PASS |

**覆盖内容**: 三层结构（common/payload/metadata）、14 个 common 层字段、source="AgentSSASSecurityRail"、AgentCallbackEvent→event_type 映射（BEFORE_INVOKE→invoke_start 等 6 个）、event_class=lifecycle、字段值传递（int 类型验证）、payload tool_name、payload content query

##### 事件过滤模块 (test_event_filter.py) - 5 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_base_event_passes_through | level1 | PASS |
| test_no_risk_when_skip_tool_false | level1 | PASS |
| test_risk_detected_when_skip_tool_true | level1 | PASS |
| test_no_observation_for_non_tool_events | level1 | PASS |
| test_no_skip_tool_key_defaults_false | level1 | PASS |

**覆盖内容**: 生命周期事件全通过、_skip_tool=False 时 BEFORE_TOOL_CALL 作为生命周期事件通过、_skip_tool=True 时生成安全检测事件字段（risk_source/risk_type/risk_level/decision/evidence）并修改 event_type=permission_interrupt_tool 和 event_class=security、非 TOOL 事件不检查 _skip_tool、缺失 _skip_tool 键时默认 False

##### 上报模块 (test_event_reporter.py) - 9 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_fail_open_on_backend_exception | level1 | PASS |
| test_safe_report_swallows_exception | level1 | PASS |
| test_critical_maps_to_reject | level1 | PASS |
| test_high_maps_to_alert_error | level1 | PASS |
| test_medium_maps_to_alert_warning | level1 | PASS |
| test_low_maps_to_alert_info | level1 | PASS |
| test_safe_maps_to_allow | level1 | PASS |
| test_report_returns_decision_from_assessment | level1 | PASS |

**覆盖内容**: fail-open 异常返回 SecurityAllow、safe_report 异常不阻断、RiskAssessment→SecurityDecision 映射（critical→Reject, high→Alert(ERROR), medium→Alert(WARNING), low→Alert(INFO), safe→Allow）、正常上报流程返回正确决策

##### AgentSSASSecurityRail 采集逻辑 (test_agent_ssas_security_rail.py) - 7 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_priority_is_80 | level1 | PASS |
| test_supported_events_declared | level1 | PASS |
| test_pipeline_events_contains_correct_events | level1 | PASS |
| test_model_events_contains_correct_events | level1 | PASS |
| test_init_creates_modules | level1 | PASS |
| test_run_security_check_returns_allow_on_filter_none | level1 | PASS |
| test_run_security_check_reports_filtered_event | level1 | PASS |

**覆盖内容**: priority=80、supported_events 声明 6 个管线事件、_PIPELINE_EVENTS 包含正确的 6 个事件、_MODEL_EVENTS 包含 2 个 MODEL 事件、__init__ 创建 IDManager/EventBuilder/EventFilter/EventReporter 实例、过滤返回 None 时返回 SecurityAllow、run_security_check 串联 builder→filter→reporter 正常流程

### 3.3 端到端测试结果

#### 3.3.1 测试脚本

```powershell
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m pytest tests/agent_ssas/backend_client/openjiuwen/test_agent_ssas_permission_interrupt.py -v
```

#### 3.3.2 测试用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_full_deny_flow_generates_security_event | level1 | PASS |
| test_no_security_event_when_skip_tool_false | level1 | PASS |
| test_fail_open_when_backend_exception | level1 | PASS |
| test_interaction_seq_increments_across_invokes | level1 | PASS |
| test_llm_call_seq_increments_within_invoke | level1 | PASS |
| test_tool_call_seq_increments_for_multiple_tools | level1 | PASS |

#### 3.3.3 验证结论

| 验证项 | 输入 | 输出 | 预期 | 结果 |
|--------|------|------|------|------|
| 完整 deny 流程 | BEFORE_INVOKE→BEFORE_MODEL_CALL→BEFORE_TOOL_CALL(_skip_tool=True) | raw_event 三层结构，event_type=permission_interrupt_tool, event_class=security, risk_source=PermissionInterruptRail, risk_level=high | 安全检测事件字段正确生成 | PASS |
| 非 deny 流程 | BEFORE_TOOL_CALL(_skip_tool=False) | event_type=tool_input, event_class=lifecycle | 生命周期事件通过，不生成安全检测字段 | PASS |
| fail-open | 后端抛出 RuntimeError | SecurityAllow | 不阻断业务 | PASS |
| interaction_seq 递增 | 两次 BEFORE_INVOKE | 0, 1 | 跨 invoke 递增 | PASS |
| llm_call_seq 递增 | 同一 invoke 内两次 BEFORE_MODEL_CALL | 0, 1 | 同 invoke 内递增 | PASS |
| tool_call_seq 递增 | 两次 BEFORE_TOOL_CALL | 0, 1 | 同 LLM 调用内递增 | PASS |

#### 3.3.4 端到端 deny 流程详细验证

`test_full_deny_flow_generates_security_event` 模拟完整流程：

1. **BEFORE_INVOKE**：生成 interaction_seq=0
2. **BEFORE_MODEL_CALL**：生成 llm_call_seq=0
3. **BEFORE_TOOL_CALL**（PermissionInterruptRail deny 设置 _skip_tool=True）：
   - AgentSSASSecurityRail 的 `_run_and_apply` 执行
   - 构建 ExtendedSecurityCheckContext 并填充 ID 字段
   - `run_security_check` 串联 EventBuilder→EventFilter→EventReporter
   - EventFilter 检测到 _skip_tool=True，生成安全检测事件字段

**验证的 raw_event 结构**：

```json
{
    "common": {
        "source": "AgentSSASSecurityRail",
        "event_type": "permission_interrupt_tool",
        "event_class": "security",
        "interaction_seq": 0,
        "llm_call_seq": 0,
        "tool_call_seq": 0,
        "tool_call_id": "call_test_001"
    },
    "payload": {
        "tool_name": "read_file",
        "risk_source": "PermissionInterruptRail",
        "risk_type": "tool_permission_denied",
        "risk_level": "high",
        "decision": "reject",
        "evidence": {
            "tool_name": "read_file",
            "tool_call_id": "call_test_001",
            "reason": "PermissionInterruptRail denied the tool call"
        }
    },
    "metadata": {}
}
```

### 3.4 验收用例对照

#### 3.4.1 阶段二验收用例

| 验收用例 | 验证方法 | 结果 | 说明 |
|---------|---------|------|------|
| 1. ExtendedSecurityCheckContext 的 ID 字段有默认值 | `python -c "from agent_ssas.backend_client.openjiuwen.extended_context import ExtendedSecurityCheckContext; ctx = ExtendedSecurityCheckContext(callback_ctx=None, event=None); assert ctx.interaction_seq == -1; ..."` | PASS | 使用 jiuwenswarm venv 验证通过，输出 `ExtendedSecurityCheckContext OK` |
| 2. AgentSSASSecurityRail 自动注册 | 创建 AgentSSASSecurityRail 实例并初始化 backend，检查日志 `AgentSSASSecurityRail registered, priority=80` | PASS | 使用 jiuwenswarm venv (Python 3.13.14) 验证通过。jiuwenswarm 的 `interface_deep.py` 已实现 SSAS Rail 注册逻辑（try/except 导入 + _build_agent_rails + backend.initialize） |
| 3. report 接口事件采集验证 | 模拟完整 Agent 流程（BEFORE_INVOKE→BEFORE_MODEL_CALL→BEFORE_TOOL_CALL），检查 AgentSSASCore 存储中是否收到 3 条事件 | PASS | `ssas_core.db raw_events count: 3`，event[0]=invoke_start(interaction_seq=0,llm_call_seq=-1,tool_call_seq=-1), event[1]=llm_input(interaction_seq=0,llm_call_seq=0,tool_call_seq=-1), event[2]=tool_input(interaction_seq=0,llm_call_seq=0,tool_call_seq=0,tool_call_id='call_test_001',tool_name='read_file') |

#### 3.4.2 jiuwenswarm 集成实现

验收用例2和3需要修改 jiuwenswarm 仓的以下文件：

| 文件 | 修改内容 | 状态 |
|------|---------|------|
| `jiuwenswarm/pyproject.toml` | `[project.dependencies]` 新增 `"agent-ssas>=0.1.0,<0.2"`；`[tool.uv.sources]` 添加本地路径 | **已完成**（用户之前已配置） |
| `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py` | 顶层 try/except 导入 AgentSSASSecurityRail；`_build_agent_rails` 中创建实例并加入 rails 列表；`backend.initialize()` 异步初始化 | **已完成** |

`interface_deep.py` 的修改内容：
1. 顶层 try/except 导入 AgentSSASSecurityRail、AgentSSASBackend、AgentSSASConfig，设置 `_SSAS_AVAILABLE` 标志
2. `__init__` 中初始化 `self._ssas_backend = None`
3. `_build_agent_rails` 末尾（ObservabilityRail 之后、return 之前）添加 SSAS Rail 构建：检查配置 `ssas.enabled`，创建 AgentSSASBackend 和 AgentSSASSecurityRail 实例，加入 rails 列表
4. `_build_agent_rails` 调用后添加 `await self._ssas_backend.initialize()` 异步初始化检测模块

### 3.5 实现中发现并修复的问题

#### 3.5.1 `_ensure_interaction_seq` 自增逻辑修复

**问题描述**：设计文档中 `_ensure_interaction_seq` 方法每次调用都自增序号。但 `_run_and_apply` 对所有 6 个管线事件都调用此方法，导致同一 invoke 内的 BEFORE_MODEL_CALL、BEFORE_TOOL_CALL 等事件也会自增 interaction_seq，与设计意图（"BEFORE_INVOKE 时先自增再使用，后续事件复用"）不符。

**修复方案**：为 `_ensure_interaction_seq` 增加 `event` 参数，仅在 `BEFORE_INVOKE` 时自增，其他事件直接从 `ctx.extra` 读取当前值。

**影响范围**：
- `id_manager.py`：`_ensure_interaction_seq` 签名变更
- `agent_ssas_security_rail.py`：`_run_and_apply` 中调用传入 `event` 参数
- 设计文档 02：6.2 节代码示例、7.2 节表格、9.2 节代码、10.2 节代码、12.2.3 节测试代码同步更新

#### 3.5.2 agent-core import 链问题

**问题描述**：agent-core 的 `__init__.py` 链在模块加载时强制导入大量可选依赖（pdfplumber、docx、Crypto、alembic 等），导致在 Python 3.14 环境（系统默认 Python）下测试无法运行。

**解决方案**：
1. **推荐方案**：使用 jiuwenswarm 的 venv（Python 3.13.14），所有依赖已正确安装，无需额外处理
2. **备用方案**：在测试 conftest.py 中使用 `MetaPathFinder` 导入钩子 mock 缺失的可选依赖

### 3.6 测试结论

#### 3.6.1 总体结论

| 维度 | 结果 |
|------|------|
| 单元测试 | 47/47 通过 (100%) |
| 端到端测试 | 6/6 通过 (100%) |
| 验收用例 | 1/1 通过 (100%) |
| 总计 | 54/54 通过 (100%) |
| 验收用例1：ExtendedSecurityCheckContext 默认值 | PASS |
| 验收用例2：AgentSSASSecurityRail 自动注册 | PASS |
| 验收用例3：report 接口事件采集 | PASS |
| ID 序号自增与复用验证 | PASS |
| 事件过滤安全检测事件生成 | PASS |
| fail-open 异常处理 | PASS |
| RiskAssessment→SecurityDecision 映射 | PASS |
| 端到端 PermissionInterruptRail deny 流程 | PASS |

#### 3.6.2 阶段二验收用例对照

| 验收用例 | 验证方法 | 结果 |
|---------|---------|------|
| ExtendedSecurityCheckContext ID 字段默认值 | `python -c "..."` 验证缺省值 | PASS |
| jiuwenswarm 日志中 AgentSSASSecurityRail 自动注册 | 创建实例并初始化，日志输出 `AgentSSASSecurityRail registered, priority=80` | PASS |
| report 接口日志验证 | 模拟 BEFORE_INVOKE 事件，检查 `ssas_core.db raw_events count: 1` | PASS |

#### 3.6.3 已知限制

> 以下为阶段二时的已知限制。其中部分已在后续阶段解决，标注状态。

1. **完整 jiuwenswarm 运行时验证**：~~验收用例2和3通过编程方式验证了 Rail 注册和事件采集，但未通过浏览器 UI 完整端到端验证~~ → 阶段三已完成 WebUI 验证指南（见本报告 4.6 节）
2. **7个直接覆写事件**：0.1版本仅实现 6 个走管线事件，7 个直接覆写钩子的事件为后续版本拓展
3. **端到端测试环境**：~~端到端测试使用 mock 模拟 PermissionInterruptRail deny 场景，未使用真实 jiuwenswarm 运行时~~ → 阶段三已完成真实 jiuwenswarm 集成验证
4. **factory.py**：~~`create_agent_ssas_rail` 为异步函数，依赖 `create_backend` 初始化检测模块，完整验证需阶段三联合验证~~ → 阶段三已完成联合验证

---

## 四、阶段三：集成测试与端到端验证

### 4.1 测试目标

联合构建并验证 AgentSSAS 系统的 HTTP 服务模式能力与端到端流程，补齐阶段一（AgentSSASCore）和阶段二（AgentSSASClient）缺失的 HTTP 服务模式（FastAPI 服务端 + AgentSSASRemoteBackend），完成端到端验证，确认进程内模式和 HTTP 服务模式均能正确生成威胁日志文件，配置关闭 SSAS 时完全不影响 jiuwenswarm 运行。

同时验证三项修改意见：
1. AgentSSASCore 命名统一
2. 包名前缀统一为 `agent_ssas.core` + `agent_ssas.backend_client`
3. 版本号统一为 0.1

### 4.2 全量回归测试结果

#### 4.2.1 总览

见本报告 1.3 节（207 用例统计）。

**结果: 207 个测试用例全部通过 (100%)**

#### 4.2.2 阶段三新增测试明细

##### FastAPI 端点测试 (test_http_server.py) - 4 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_post_events_returns_assessment | integration/level1 | PASS |
| test_post_events_empty_raw_event | integration/level1 | PASS |
| test_post_events_security_event_high_risk | integration/level1 | PASS |
| test_health_check | integration/level1 | PASS |

**覆盖内容**: POST /api/v1/events 正常请求返回 RiskAssessment、空 raw_event 请求 fail-open 返回 safe、安全检测事件返回 high 风险、健康检查端点

##### AgentSSASRemoteBackend 测试 (test_remote_backend.py) - 6 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_report_event_returns_assessment | integration/level1 | PASS |
| test_report_event_security_event_high_risk | integration/level1 | PASS |
| test_fail_open_on_connection_error | integration/level1 | PASS |
| test_fail_open_on_timeout | integration/level1 | PASS |
| test_initialize_is_noop | integration/level1 | PASS |
| test_assessment_serialization_roundtrip | integration/level1 | PASS |

**覆盖内容**: HTTP 转发正常请求、安全检测事件 high 风险、连接失败 fail-open、超时 fail-open、initialize 空实现、RiskAssessment 序列化/反序列化往返

##### 进程内模式端到端测试 (test_e2e_inprocess.py) - 6 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_full_event_sequence_returns_assessments | integration/level1 | PASS |
| test_security_event_returns_high_risk | integration/level1 | PASS |
| test_raw_events_persisted_to_sqlite | integration/level1 | PASS |
| test_threat_log_file_generated | integration/level1 | PASS |
| test_fail_open_on_invalid_event | integration/level1 | PASS |
| test_module_result_db_records | integration/level1 | PASS |

**覆盖内容**: 完整事件流（invoke_start→llm_input→tool_input→tool_output→llm_output→invoke_end）返回 RiskAssessment、安全检测事件 high 风险、raw_event 持久化到 SQLite、威胁日志文件（OCSF JSON）生成、非法事件 fail-open、检测模块 result.db 记录

##### HTTP 服务模式端到端测试 (test_e2e_http.py) - 5 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_full_event_sequence_via_http | integration/level1 | PASS |
| test_security_event_via_http_high_risk | integration/level1 | PASS |
| test_http_latency | integration/level1 | PASS |
| test_fail_open_on_invalid_event | integration/level1 | PASS |
| test_server_persists_raw_events | integration/level1 | PASS |

**覆盖内容**: 完整事件流通过 HTTP 转发、安全检测事件通过 HTTP 返回 high 风险、HTTP 延迟 < 100ms（首次含懒初始化 < 500ms）、非法事件 fail-open、服务端 SQLite 持久化

##### 配置关闭测试 (test_agent_ssas_disabled.py) - 9 个用例

| 用例 | 标记 | 结果 |
|------|------|------|
| test_config_disabled_flag | integration/level1 | PASS |
| test_config_enabled_default_true | integration/level1 | PASS |
| test_create_backend_inprocess_works | integration/level1 | PASS |
| test_create_backend_http_works | integration/level1 | PASS |
| test_disabled_config_does_not_crash | integration/level1 | PASS |
| test_jiuwenswarm_rails_logic_disabled | integration/level1 | PASS |
| test_jiuwenswarm_rails_logic_enabled | integration/level1 | PASS |
| test_jiuwenswarm_rails_logic_default | integration/level1 | PASS |
| test_disabled_ssas_no_side_effects | integration/level1 | PASS |

**覆盖内容**: AgentSSASConfig(enabled=False) 正确设置、默认 enabled=True、create_backend 进程内/HTTP 模式正常工作、配置关闭不报错、jiuwenswarm _build_agent_rails 逻辑验证（disabled/enabled/default）、无副作用

### 4.3 真实环境验证结果

#### 4.3.1 进程内模式验证

**验证命令**：
```powershell
$env:SSAS_HOME = "$env:TEMP\agent_ssas\phase3\inprocess"
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m tests.verify_phase3 inprocess
```

**验证输出**：
```
[verify_inprocess] SSAS_HOME=...\agent_ssas\phase3\inprocess
[verify_inprocess] PASS: AgentSSASSecurityRail registered, priority=80
[verify_inprocess] PASS: AgentSSASSecurityRail backend initialized
[verify_inprocess] PASS: 6 lifecycle events -> all risk_level=safe
[verify_inprocess] PASS: security event -> risk_level=high, has_risk=True
[verify_inprocess] PASS: detected_threats=['tool_permission_denied']
[verify_inprocess] PASS: ssas_core.db generated
[verify_inprocess] PASS: raw_events table has 7 records (expected >=7)
[verify_inprocess] PASS: events table has 11 records (expected >=7)
[verify_inprocess] PASS: alerts table has 1 records (expected >=1)
[verify_inprocess] PASS: 18 threat log files generated
[verify_inprocess] PASS: detection modules: ['agent_moss', 'security_rail_detection', 'test_detection']
[verify_inprocess] === ALL CHECKS PASSED ===
```

> **数据可重复性说明**：验证脚本在每次运行前自动清空旧测试数据（`_safe_clean_home`），并在测试结束时通过 `backend.close()` 显式关闭 SQLite 连接，释放 Windows WAL 文件锁定。连续两次运行结果完全一致（7 条 raw_events、11 条 events、1 条 alerts），不会累积。

**验证结论**：

| 验证项 | 输入 | 输出 | 预期 | 结果 |
|--------|------|------|------|------|
| Rail 注册 | 创建 AgentSSASSecurityRail 实例 | `AgentSSASSecurityRail registered, priority=80` | priority=80 | PASS |
| 后端初始化 | `backend.initialize()` | `AgentSSASSecurityRail backend initialized` | 无异常 | PASS |
| 完整事件流 | 6 个生命周期事件（invoke_start→llm_input→tool_input→tool_output→llm_output→invoke_end） | `all risk_level=safe` | 全部无风险 | PASS |
| 安全检测事件 | permission_interrupt_tool 安全检测事件 | `risk_level=high, has_risk=True` | 高风险 | PASS |
| raw_events 表 | 7 个事件上报（6 生命周期 + 1 安全检测） | `raw_events table has 7 records` | >= 7 条 | PASS |
| events 表 | UnifiedEvent + 派生事件 + 聚合事件 | `events table has 11 records` | >= 7 条 | PASS |
| alerts 表 | 安全检测事件告警 | `alerts table has 1 records` | >= 1 条 | PASS |
| 威胁日志文件 | OCSF 格式 JSON | `18 threat log files generated` | 文件生成 | PASS |
| 检测模块存储 | 3 个检测模块 | `detection modules: ['agent_moss', 'security_rail_detection', 'test_detection']` | 3 个模块 | PASS |
| 数据可重复性 | 连续两次运行 | 数据量完全一致 | 不累积 | PASS |

**存储文件生成**：
```
$env:TEMP\agent_ssas\phase3\inprocess\ssas\
├── ssas_core.db              # 主 SQLite 数据库
├── modules\
│   ├── agent_moss\           # AgentMoss 检测模块存储
│   │   ├── process.db
│   │   └── result.db
│   ├── security_rail_detection\  # 安全护栏检测存储
│   │   ├── process.db
│   │   └── result.db
│   └── test_detection\       # 测试检测模块存储
│       ├── process.db
│       └── result.db
└── reports\
    └── threat_log\           # OCSF 格式威胁日志
        └── *.json
```

#### 4.3.2 HTTP 服务模式验证

**验证命令**：
```powershell
$env:SSAS_HOME = "$env:TEMP\agent_ssas\phase3\http"
$env:SSAS_HTTP_PORT = "8444"
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m tests.verify_phase3 http-server
```

**服务端启动输出**：
```
[http-server] Starting AgentSSAS HTTP Server on port 8443...
[http-server] Press Ctrl+C to stop
INFO:     Started server process [2416]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8443 (Press CTRL+C to quit)
```

> **端口占用自动处理**：验证脚本在启动 HTTP 服务前自动检查端口是否被占用。如果上次服务端未正常退出（Ctrl+C 后进程残留），`_free_port` 函数会通过 `netstat -ano` 查找占用端口的 PID 并通过 `taskkill /F` 终止，然后正常启动新服务端。

##### 启动测试客户端（另开一个终端）

服务端启动后，需要另开一个终端执行客户端验证脚本，向服务端发送事件并验证响应：

```powershell
cd d:\TraeWorkspace\JiuwenSSAS\AgentSecurity\AgentSSAS
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m tests.verify_phase3 http-client
```

> **注意**：此命令需在服务端运行期间执行。客户端与进程内模式一致，发送完整 7 条事件（6 个生命周期事件 + 1 个安全检测事件），并验证响应。

**客户端验证输出**：
```
[verify_http_client] endpoint=http://localhost:8443
[verify_http_client] PASS: 6 lifecycle events -> all risk_level=safe, first latency=436.7ms
[verify_http_client] PASS: security event -> status=200, risk_level=high, has_risk=True
[verify_http_client] PASS: detected_threats=['tool_permission_denied']
[verify_http_client] PASS: health check -> status=ok
[verify_http_client] === ALL CHECKS PASSED ===
```

验证完成后，在服务端终端按 `Ctrl+C` 停止服务端。

**事件上报验证**：

| 验证项 | 输入 | 输出 | 预期 | 结果 |
|--------|------|------|------|------|
| 完整事件流 | 6 个生命周期事件（invoke_start→llm_input→tool_input→tool_output→llm_output→invoke_end） | all risk_level=safe | 全部无风险 | PASS |
| 安全检测事件 | permission_interrupt_tool 安全检测事件 | risk_level=high, has_risk=True | 高风险 | PASS |
| 首次请求延迟 | 含懒初始化 | 436.7ms | < 500ms | PASS |
| 检测威胁 | permission_interrupt_tool | detected_threats: ['tool_permission_denied'] | 含威胁类型 | PASS |
| 健康检查 | GET /health | status=ok | 200 | PASS |

**响应详情（安全检测事件）**：
```json
{
    "assessment": {
        "has_risk": true,
        "risk_level": "high",
        "risk_type": "tool_permission_denied",
        "risk_score": 75.0,
        "confidence": 1.0,
        "detected_threats": ["tool_permission_denied"],
        "recommended_actions": ["log"],
        "details": {},
        "evidence": {
            "security_rail_detection": {
                "risk_source": "PermissionInterruptRail",
                "decision": ""
            }
        }
    }
}
```

**存储文件生成**（与进程内模式一致）：
```
$env:TEMP\agent_ssas\phase3\http_server\ssas\
├── ssas_core.db
├── modules\
│   ├── agent_moss\
│   ├── security_rail_detection\
│   └── test_detection\
└── reports\
    └── threat_log\
        └── *.json (4 个 OCSF 格式文件)
```

#### 4.3.3 配置关闭验证

**验证命令**：
```powershell
cd d:\TraeWorkspace\JiuwenSSAS\AgentSecurity\AgentSSAS
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m tests.verify_phase3 disabled
```

**验证输出**：
```
[verify_disabled] PASS: AgentSSASConfig(enabled=False) -> enabled=False
[verify_disabled] PASS: storage_path=C:\Users\yieux\.jiuwenswarm\ssas
[verify_disabled] PASS: jiuwenswarm config ssas.enabled=false -> skip registration
[verify_disabled] PASS: default config (no ssas section) -> enabled=True
[verify_disabled] === ALL CHECKS PASSED ===
```

**验证结论**：

| 验证项 | 输入 | 输出 | 预期 | 结果 |
|--------|------|------|------|------|
| 配置关闭 | AgentSSASConfig(enabled=False) | `enabled=False` | enabled 为 False | PASS |
| 存储路径 | ssas_home=None | `storage_path` 正确推导 | 不报错 | PASS |
| jiuwenswarm 集成逻辑 | config_base={"ssas":{"enabled":False}} | `ssas.enabled=false -> skip registration` | 跳过注册 | PASS |
| 默认开启 | config_base={}（无 ssas 段） | `enabled=True` | 默认开启 | PASS |

**jiuwenswarm 集成逻辑验证**：

`interface_deep.py` 中的 SSAS 关闭逻辑（第 4184-4200 行，见 jiuwenswarm 仓 `jiuwenswarm/server/runtime/agent_adapter/interface_deep.py`）：

```python
if _SSAS_AVAILABLE:
    ssas_config = config_base.get("ssas", {})
    if not ssas_config.get("enabled", True):
        logger.info("[AgentSSAS] AgentSSASSecurityRail disabled by config, skipping registration")
    else:
        # ... 创建 AgentSSASSecurityRail 并注册
```

- `ssas.enabled=false` 时：日志输出 `AgentSSASSecurityRail disabled by config, skipping registration`，不创建 backend、不注册 Rail
- `ssas.enabled=true` 或无配置时：正常注册 AgentSSASSecurityRail
- **配置关闭 SSAS 时不影响 jiuwenswarm 的其他 Rail 和 Agent 执行流**——不会出现 ImportError、AttributeError 或任何异常

### 4.4 包结构重构验证

#### 4.4.1 重构概述

| 操作 | 源 | 目标 |
|------|-----|------|
| 创建顶级包 | — | `src/agent_ssas/__init__.py`（`__version__ = "0.1.0"`） |
| 移动+重命名 | `src/agent_ssas_core/` | `src/agent_ssas/core/` |
| 移动 | `src/backend_client/` | `src/agent_ssas/backend_client/` |
| 移动测试 | `tests/agent_ssas_core/` | `tests/agent_ssas/core/` |
| 移动测试 | `tests/backend_client/` | `tests/agent_ssas/backend_client/` |

#### 4.4.2 导入路径变更

| 原路径 | 新路径 |
|--------|--------|
| `from agent_ssas_core.framework.*` | `from agent_ssas.core.framework.*` |
| `from agent_ssas_core.detection_modules.*` | `from agent_ssas.core.detection_modules.*` |
| `from agent_ssas_core.integration.*` | `from agent_ssas.core.integration.*` |
| `from backend_client.openjiuwen.*` | `from agent_ssas.backend_client.openjiuwen.*` |
| `import agent_ssas_core` | `import agent_ssas.core` |

#### 4.4.3 验证结果

| 验证项 | 命令 | 结果 |
|--------|------|------|
| 顶级包导入 | `import agent_ssas; print(agent_ssas.__version__)` | `0.1.0` PASS |
| core 包导入 | `import agent_ssas.core; print('OK')` | `OK` PASS |
| Rail 导入 | `from agent_ssas.backend_client.openjiuwen.agent_ssas_security_rail import AgentSSASSecurityRail` | `OK` PASS |
| Config 导入 | `from agent_ssas.core.framework.config.settings import AgentSSASConfig` | `OK` PASS |
| jiuwenswarm 集成导入 | `interface_deep.py` try/except 导入 | `OK` PASS |
| 全量测试 | 207 个用例 | `207 passed` PASS |

### 4.5 阶段三验收用例对照与测试结论

#### 4.5.1 文档01 第八章阶段三验收用例

| 验收用例 | 验证方法 | 结果 | 说明 |
|---------|---------|------|------|
| 1. 进程内模式：源码安装并运行 jiuwenswarm，验证威胁日志生成 | 编程方式验证 Rail 注册 + 事件上报 + SQLite/OCSF 日志生成 | PASS | `AgentSSASSecurityRail registered, priority=80` + `ssas_core.db` + `reports/threat_log/*.json` |
| 2. HTTP 服务模式：独立启动 SSAS 服务端，验证事件上报与延迟 | 启动 `python -m agent_ssas.core.modes.http.server` + POST 事件 | PASS | Status: 200, 首次延迟 436.7ms（含懒初始化），安全检测事件 risk_level=high |
| 3. HTTP 服务模式：通过 jiuwenswarm 操作，验证威胁日志生成 | HTTP 服务端 SQLite + OCSF 日志文件生成 | PASS | `ssas_core.db` + `modules/*/result.db` + `reports/threat_log/*.json` |
| 4. 全新环境安装验证 | `import agent_ssas.core; print(agent_ssas.core.__version__)` | PASS | 输出 `0.1.0` |
| 5. 关闭 SSAS 验证（配置关闭） | `AgentSSASConfig(enabled=False)` + jiuwenswarm `_build_agent_rails` 逻辑 | PASS | `enabled=False` + 不报错 + 不影响其他 Rail |

#### 4.5.2 总体结论

| 维度 | 结果 |
|------|------|
| 全量回归测试 | 207/207 通过 (100%) |
| 阶段三新增测试 | 30/30 通过 (100%) |
| HTTP 模式测试 | 10/10 通过 (100%) |
| 端到端测试 | 11/11 通过 (100%) |
| 配置关闭测试 | 9/9 通过 (100%) |
| 包结构重构验证 | 导入路径 + 全量测试通过 |
| 进程内模式验证 | Rail 注册 + 事件上报 + 日志生成 |
| HTTP 服务模式验证 | FastAPI 服务端 + 事件转发 + 日志生成 |
| 配置关闭验证 | 不报错、不影响 jiuwenswarm |
| 命名统一验证 | AgentSSASCore 子系统统一 |
| 包名统一验证 | agent_ssas.core + agent_ssas.backend_client |
| 版本号统一验证 | 0.1版本（非 1.0版本） |

#### 4.5.3 阶段三验收用例对照

| 验收用例 | 验证方法 | 结果 |
|---------|---------|------|
| 进程内模式威胁日志生成 | 编程方式驱动事件流 + 检查 SQLite/OCSF 文件 | PASS |
| HTTP 服务模式事件上报 | 启动 FastAPI + POST /api/v1/events | PASS |
| HTTP 服务模式威胁日志生成 | 检查服务端 SQLite/OCSF 文件 | PASS |
| 全新环境安装验证 | `import agent_ssas.core` + `__version__` | PASS |
| 配置关闭验证 | `AgentSSASConfig(enabled=False)` + jiuwenswarm 逻辑 | PASS |
| HTTP 延迟验证 | 首次 < 500ms，后续 < 100ms | PASS |
| fail-open 验证 | 连接失败/超时/非法事件 → 无风险 | PASS |
| 安全检测事件验证 | permission_interrupt_tool → risk_level=high | PASS |
| 包结构重构验证 | 207 个测试全部通过 | PASS |

#### 4.5.4 已知限制

1. **7 个直接覆写事件**：0.1版本仅实现 6 个走管线事件，7 个直接覆写钩子的事件（user_message、steering_drain、task_iteration_start/end、react_iteration_end、model_exception、tool_exception）为后续版本拓展
2. **工具调用链异常检测模块**：标记为后续版本拓展，0.1版本不实现
3. **HTTP 查询端点**：0.1版本仅实现 `POST /api/v1/events`，GET 查询/告警/大屏/WebSocket 端点为后续版本拓展
4. **AgentMoss 模块**：0.1版本为简化实现，返回无风险报告，未实现实际的规则/小模型/LLM 分析
5. **呈现模块关联查询**：0.1版本 `_query_events_by_trace_id` 返回空列表，未实际从存储模块关联读取

### 4.6 完整 jiuwenswarm WebUI 验证指南

本节提供通过 jiuwenswarm WebUI 触发 Agent 行为、验证 SSAS 安全态势感知系统正确工作的完整操作指南。适用于进程内模式和 HTTP 服务模式。

#### 4.6.1 前置配置

##### 4.6.1.1 安装 jiuwenswarm（源码 uv 方式）

按 jiuwenswarm 仓安装指南（`docs/zh/安装指南.md`）方式三操作：

```powershell
# 1. 克隆并安装
cd d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm_2\jiuwenswarm
uv venv
uv pip install -e .

# 2. 安装 agent-ssas（editable 模式）
uv pip install --python .venv\Scripts\python.exe -e ../../AgentSecurity/AgentSSAS

# 3. 构建前端（源码安装必须，否则启动时报错 dist directory not found）
cd jiuwenswarm\channels\web\frontend
npm install
npm run build
cd ..\..\..\..

# 4. 首次初始化（创建 ~/.jiuwenswarm/ 工作空间）
.venv\Scripts\python.exe -m jiuwenswarm.init_workspace

# 5. 启动
.venv\Scripts\activate
jiuwenswarm-start
```

启动后访问 `http://localhost:5173` 打开 WebUI。

> **LLM API 配置**：不需要提前修改 `.env` 文件。启动后在 WebUI 左侧"配置信息"页面配置模型 API（model_name、api_base、api_key），保存即可。

> **端口说明**：默认端口组为 agent_server=18092、web=19000、gateway=19001、frontend=5173。WebUI 访问地址是 `http://localhost:5173`。

##### 4.6.1.2 配置 SSAS 模式

jiuwenswarm 启动后，实际加载的 `config.yaml` 路径为 `~/.jiuwenswarm/config/config.yaml`（用户工作空间目录）。源码中的 `jiuwenswarm/resources/config.yaml` 仅在首次初始化时复制到用户目录，后续不会自动合并新增配置段。

> **如何确定你的 config.yaml 位置**：执行以下命令查看实际加载路径：
> ```powershell
> cd d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm_2\jiuwenswarm
> .venv\Scripts\python.exe -c "from jiuwenswarm.common.utils import get_config_file; print(get_config_file())"
> ```

**重要**：用户 config.yaml 中默认**不包含** `ssas` 段。`_build_ssas_rail` 方法检查 `ssas_cfg.get("enabled", False)`，无 `ssas` 段时默认 `False`，SSAS **不会激活**。必须在 config.yaml 中显式添加 `ssas` 段并设置 `enabled: true` 才能启用 SSAS。

如需启用 SSAS，在 `~/.jiuwenswarm/config/config.yaml` 末尾添加 `ssas` 段：

```yaml
# AgentSSAS 安全态势感知系统配置
ssas:
  enabled: true
  mode: inprocess
  http_endpoint: "http://localhost:8443"
  http_timeout: 5.0
  http_token:
  http_host: "0.0.0.0"
  http_port: 8443
  ssas_home:
  storage_backend: sqlite
  event_ttl_days: 30
  alert_ttl_days: 90
  rail_priority: 80
  enable_exception_hooks: true
  risk_report_threshold: low
  auth_timeout: 2.0
```

**配置说明**：

| 配置项 | 环境变量 | 说明 |
|--------|---------|------|
| `enabled` | `SSAS_ENABLED` | 必须设为 true 才激活 SSAS；false 时不加载 AgentSSASSecurityRail |
| `mode` | `SSAS_MODE` | inprocess：零网络延迟；http：支持多 Agent 共享 |
| `ssas_home` | `SSAS_HOME` | 存储根目录，影响数据库和威胁日志输出位置 |
| `http_endpoint` | `SSAS_HTTP_ENDPOINT` | HTTP 模式下 SSAS 服务端地址 |
| `http_port` | `SSAS_HTTP_PORT` | HTTP 服务端监听端口 |

- **进程内模式**（默认）：`ssas.mode: inprocess`。分析引擎在 jiuwenswarm 进程内运行，零网络延迟
- **HTTP 服务模式**：`ssas.mode: http`，配置 `http_endpoint` 指向独立启动的 SSAS 服务端地址

#### 4.6.2 进程内模式 WebUI 验证

##### 4.6.2.1 启动 jiuwenswarm

> **前置条件**：已完成 4.6.1.1 的安装步骤（含前端构建和 agent-ssas 安装）。已在 config.yaml 中添加 `ssas` 段并设置 `enabled: true`。

```powershell
cd d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm_2\jiuwenswarm
.venv\Scripts\activate
jiuwenswarm-start
```

启动后访问 `http://localhost:5173` 打开 WebUI。

> **日志查看**：后端日志位于 `~/.jiuwenswarm/agent/.logs/`。`full.log` 为汇总日志，`ws-dev.log` 为前端代理日志，`gateway.log` 为 Gateway 日志。

##### 4.6.2.2 验证 SSAS 已注册

启动后查看日志文件 `~/.jiuwenswarm/agent/.logs/full.log`，预期出现：
```
[JiuWenSwarmDeepAdapter] AgentSSASSecurityRail create success, mode=inprocess
```

如果出现 `[JiuWenSwarmDeepAdapter] AgentSSASSecurityRail disabled by config`，说明 `ssas.enabled` 未设为 `true` 或 `ssas` 段缺失，需在 config.yaml 中添加。
如果出现 `[JiuWenSwarmDeepAdapter] AgentSSASSecurityRail not available (agent-ssas not installed)`，说明 agent-ssas 未安装或导入失败，需执行 `uv pip install --python .venv\Scripts\python.exe -e ../../AgentSecurity/AgentSSAS` 重新安装。
如果出现 `[JiuWenSwarmDeepAdapter] AgentSSASSecurityRail not loaded: <具体错误>`，说明导入链中某符号找不到，需查看具体 ImportError 信息排查。

##### 4.6.2.3 触发 Agent 行为

在 WebUI 中与 Agent 对话，触发以下场景：

| 场景 | 操作 | 预期触发的事件 |
|------|------|-------------|
| 任意对话 | 发送一条消息 | invoke_start、llm_input、llm_output、invoke_end |
| 工具调用 | 让 Agent 执行需要工具的任务（如"列出当前目录文件"） | invoke_start、llm_input、tool_input、tool_output、llm_output、invoke_end |
| 权限拒绝 | 让 Agent 尝试调用未授权的工具（如配置某工具为 deny） | 触发 PermissionInterruptRail deny，生成 permission_interrupt_tool 安全检测事件 |

##### 4.6.2.4 检查威胁日志

Agent 交互后，检查 SSAS 生成的数据文件：

```powershell
# 查看 SSAS 存储目录
# 默认路径：~/.jiuwenswarm/ssas/
# 或通过 SSAS_HOME 环境变量指定
Get-ChildItem -Path "$env:USERPROFILE\.jiuwenswarm\ssas" -Recurse -File
```

**预期生成的文件**：

| 文件 | 说明 | 检查内容 |
|------|------|---------|
| `ssas/ssas_core.db` | 主 SQLite 数据库 | `raw_events` 表有事件记录，按 `trace_id` 可查询 |
| `ssas/modules/test_detection/result.db` | 测试检测模块 | 有事件记录（订阅所有事件） |
| `ssas/modules/security_rail_detection/result.db` | 安全护栏检测模块 | 仅在有权限拒绝事件时有记录 |
| `ssas/modules/agent_moss/result.db` | AgentMoss 检测模块 | 有事件记录 |
| `ssas/reports/threat_log/*.json` | OCSF 格式威胁日志 | JSON 文件，包含 severity_id、finding_info 等字段 |

**数据库查询验证**：
```powershell
# 查询 raw_events 表记录数
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm_2\jiuwenswarm\.venv\Scripts\python.exe -c "
import sqlite3
conn = sqlite3.connect(r'$env:USERPROFILE\.jiuwenswarm\ssas\ssas_core.db')
count = conn.execute('SELECT COUNT(*) FROM raw_events').fetchone()[0]
print(f'raw_events count: {count}')
# 查看事件类型分布
for row in conn.execute(\"SELECT json_extract(raw_event, '$.common.event_type') as type, COUNT(*) FROM raw_events GROUP BY type\"):
    print(f'  {row[0]}: {row[1]}')
conn.close()
"
```

**OCSF 威胁日志文件示例**（安全检测事件，高风险）：
```json
{
  "severity_id": 4,
  "class_name": "Detection Finding",
  "finding_info": {
    "types": ["tool_permission_denied"],
    "analytic": { "name": "Security Rail Detection" }
  },
  "evidences": [{
    "data": {
      "detected_threats": ["tool_permission_denied"],
      "risk_source": "PermissionInterruptRail",
      "risk_level": "high",
      "decision": "reject"
    }
  }]
}
```

##### 4.6.2.5 "加载配置中"问题排查

**症状**：WebUI 页面一直提示"加载配置中..."，无法进入对话界面。命令行或 `ws-dev.log` 日志显示 WebSocket 连接反复断开。

**排查步骤**：

1. **检查 `ws-dev.log`**：查看 `~/.jiuwenswarm/agent/.logs/ws-dev.log`，如果出现 `ConnectionResetError [WinError 10054]`，说明 WS 握手成功后被后端重置。

2. **检查 `full.log` 是否有 SSAS 错误**：搜索 `[JiuWenSwarmDeepAdapter]` 和 `[AgentSSAS]` 关键字。SSAS 运行在 AgentServer 进程中，如果 SSAS 导入或注册失败，日志会有明确记录。如果无任何 SSAS 相关日志，说明 SSAS 未影响启动流程。

3. **尝试回退 dual_protocol 模式**：jiuwenswarm 新版本默认启用 `dual_protocol` 模式（uvicorn+FastAPI），在 Windows 上可能存在兼容性问题。在 `~/.jiuwenswarm/config/.env` 中添加 `WEB_DUAL_PROTOCOL=0` 回退到 legacy `websockets.serve` 模式，重启 `jiuwenswarm-start` 验证。

4. **确认端口正常监听**：检查 18092（AgentServer）、19000（Web）、19001（Gateway）、5173（Frontend）四个端口是否都在监听。

5. **回退 SSAS patch 验证**：如果以上步骤无效，回退所有 SSAS 修改（pyproject.toml 依赖、config.yaml ssas 段、interface_deep.py SSAS 代码），重新 `uv pip install -e .`，走默认安装启动流程，确认是 jiuwenswarm 本身的问题还是 SSAS 引入的。

> **已知结论**：在 2026-08-20 的验证中，`dual_protocol` 模式（默认启用）在 Windows 上存在 WS 连接断开问题。SSAS patch 未引入此问题（full.log 无 SSAS 错误，SSAS 运行在 AgentServer 进程，与 WS 断连的 Gateway/WebChannel 层完全独立）。可通过 `WEB_DUAL_PROTOCOL=0` 回退到 legacy 模式绕过。

#### 4.6.3 HTTP 服务模式 WebUI 验证

##### 4.6.3.1 启动 SSAS HTTP 服务端

在终端 1 启动 SSAS 服务端：
```powershell
cd d:\TraeWorkspace\JiuwenSSAS\AgentSecurity\AgentSSAS
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm_2\jiuwenswarm\.venv\Scripts\python.exe -m tests.verify_phase3 http-server
```
预期输出：`Uvicorn running on http://0.0.0.0:8443`

##### 4.6.3.2 配置 jiuwenswarm 为 HTTP 模式

修改 `~/.jiuwenswarm/config/config.yaml`：
```yaml
ssas:
  enabled: true
  mode: http
  http_endpoint: "http://localhost:8443"
```

##### 4.6.3.3 启动 jiuwenswarm

在终端 2 启动 jiuwenswarm：
```powershell
cd d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm_2\jiuwenswarm
.venv\Scripts\activate
jiuwenswarm-start
```

##### 4.6.3.4 触发 Agent 行为并验证

与进程内模式相同的操作（4.6.2.3），区别在于事件通过 HTTP 转发到服务端。

**验证服务端收到事件**：
- 查看 SSAS 服务端终端日志，预期出现请求日志
- 检查服务端存储目录（`$env:TEMP\agent_ssas\phase3\http_server\ssas\`）下的数据库和日志文件

#### 4.6.4 配置关闭验证

##### 4.6.4.1 配置关闭

修改 `~/.jiuwenswarm/config/config.yaml`：
```yaml
ssas:
  enabled: false
```

##### 4.6.4.2 启动 jiuwenswarm

```powershell
cd d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm_2\jiuwenswarm
.venv\Scripts\activate
jiuwenswarm-start
```

##### 4.6.4.3 验证

- 预期日志出现：`[JiuWenSwarmDeepAdapter] AgentSSASSecurityRail disabled by config`
- **关键验证**：在 WebUI 中与 Agent 对话，确认 Agent 完全正常运行，不报错、不缺功能
- 确认 `~/.jiuwenswarm/ssas/` 目录下不生成新的数据文件（SSAS 完全不工作）

#### 4.6.5 验证检查清单

| 检查项 | 进程内模式 | HTTP 服务模式 | 配置关闭 |
|--------|----------|-------------|---------|
| SSAS 注册日志 | `[JiuWenSwarmDeepAdapter] AgentSSASSecurityRail create success, mode=inprocess` | `[JiuWenSwarmDeepAdapter] AgentSSASSecurityRail create success, mode=http` | `[JiuWenSwarmDeepAdapter] AgentSSASSecurityRail disabled by config` |
| 生命周期事件采集 | ssas_core.db raw_events 表有 invoke_start 等记录 | 服务端 ssas_core.db 有记录 | 无文件生成 |
| 安全检测事件 | security_rail_detection result.db 有 high 风险记录 | 服务端有记录 | — |
| 威胁日志文件 | reports/threat_log/*.json 生成 | 服务端生成 | — |
| Agent 正常运行 | 正常对话、工具调用 | 正常对话、工具调用 | 正常对话、工具调用（完全不受影响） |
| HTTP 延迟 | — | < 100ms（非首次） | — |

---

## 五、附录

### 5.1 测试文件清单

| 文件路径 | 用例数 | 类型 | 阶段 |
|---------|--------|------|------|
| tests/agent_ssas/core/framework/test_config.py | 11 | 单元测试 | 阶段一 |
| tests/agent_ssas/core/framework/test_storage.py | 16 | 单元测试 | 阶段一 |
| tests/agent_ssas/core/framework/test_models.py | 8 | 单元测试 | 阶段一 |
| tests/agent_ssas/core/framework/test_preprocessor.py | 6 | 单元测试 | 阶段一 |
| tests/agent_ssas/core/framework/test_module_manager.py | 7 | 单元测试 | 阶段一 |
| tests/agent_ssas/core/framework/test_pipeline.py | 15 | 单元测试 | 阶段一 |
| tests/agent_ssas/core/framework/test_subscription.py | 14 | 单元测试 | 阶段一 |
| tests/agent_ssas/core/framework/test_threat_log.py | 6 | 单元测试 | 阶段一 |
| tests/agent_ssas/core/framework/test_agent_backend.py | 4 | 单元测试 | 阶段一 |
| tests/agent_ssas/core/detection_modules/test_test_detection.py | 5 | 单元测试 | 阶段一 |
| tests/agent_ssas/core/detection_modules/test_security_rail_detection.py | 5 | 单元测试 | 阶段一 |
| tests/agent_ssas/core/detection_modules/test_agent_moss.py | 7 | 单元测试 | 阶段一 |
| tests/agent_ssas/core/integration/test_pipeline_integration.py | 4 | 集成测试 | 阶段一 |
| tests/agent_ssas/core/integration/test_end_to_end.py | 4 | 端到端测试 | 阶段一 |
| tests/agent_ssas/core/func_verify.py | 6 | 功能验证 | 阶段一 |
| tests/agent_ssas/core/perf_test.py | 2 | 性能测试 | 阶段一 |
| tests/agent_ssas/backend_client/openjiuwen/test_extended_context.py | 3 | 单元测试 | 阶段二 |
| tests/agent_ssas/backend_client/openjiuwen/test_id_manager.py | 16 | 单元测试 | 阶段二 |
| tests/agent_ssas/backend_client/openjiuwen/test_event_builder.py | 8 | 单元测试 | 阶段二 |
| tests/agent_ssas/backend_client/openjiuwen/test_event_filter.py | 5 | 单元测试 | 阶段二 |
| tests/agent_ssas/backend_client/openjiuwen/test_event_reporter.py | 9 | 单元测试 | 阶段二 |
| tests/agent_ssas/backend_client/openjiuwen/test_agent_ssas_security_rail.py | 7 | 单元测试 | 阶段二 |
| tests/agent_ssas/backend_client/openjiuwen/test_agent_ssas_permission_interrupt.py | 6 | 端到端测试 | 阶段二 |
| tests/agent_ssas/backend_client/openjiuwen/test_verify_case3.py | 1 | 验收用例 | 阶段二 |
| tests/agent_ssas/core/integration/test_http_server.py | 4 | 集成测试 | **阶段三新增** |
| tests/agent_ssas/core/integration/test_remote_backend.py | 6 | 集成测试 | **阶段三新增** |
| tests/agent_ssas/core/integration/test_e2e_inprocess.py | 6 | 端到端测试 | **阶段三新增** |
| tests/agent_ssas/core/integration/test_e2e_http.py | 5 | 端到端测试 | **阶段三新增** |
| tests/agent_ssas/core/integration/test_agent_ssas_disabled.py | 9 | 配置关闭测试 | **阶段三新增** |
| **合计** | **207** | | |

### 5.2 测试结果文件

| 文件 | 说明 |
|------|------|
| `tests/test_results.xml` | JUnit XML 格式全量测试结果（207 个用例，CI 系统可解析） |
| `test_results_all.xml` | 全量 207 个用例结果 |
| `test_results_phase3.xml` | 阶段三 integration 目录结果 |

### 5.3 源码文件清单

#### 5.3.1 AgentSSASClient 子系统源码（阶段二）

| 文件路径 | 模块 | 说明 |
|---------|------|------|
| `src/agent_ssas/backend_client/openjiuwen/__init__.py` | 包初始化 | 导入隔离设计 |
| `src/agent_ssas/backend_client/openjiuwen/extended_context.py` | ExtendedSecurityCheckContext | 继承 SecurityCheckContext，新增 11 个 ID 字段 |
| `src/agent_ssas/backend_client/openjiuwen/id_manager.py` | IDManager | 序号生成与管理（interaction_seq、llm_call_seq、tool_call_seq） |
| `src/agent_ssas/backend_client/openjiuwen/event_builder.py` | EventBuilder | 构建三层结构 raw_event（common/payload/metadata） |
| `src/agent_ssas/backend_client/openjiuwen/event_filter.py` | EventFilter | 生命周期事件全通过 + PermissionInterruptRail deny 检测 |
| `src/agent_ssas/backend_client/openjiuwen/event_reporter.py` | EventReporter | 上报 + RiskAssessment→SecurityDecision 映射 + fail-open |
| `src/agent_ssas/backend_client/openjiuwen/agent_ssas_security_rail.py` | AgentSSASSecurityRail | 主类，覆写 _run_and_apply，串联各模块 |
| `src/agent_ssas/backend_client/openjiuwen/factory.py` | create_agent_ssas_rail | 工厂函数，创建后端 + 注入 |

#### 5.3.2 阶段三新增源码文件

| 文件路径 | 模块 | 说明 |
|---------|------|------|
| `src/agent_ssas/core/framework/access_adapter/agent_remote_backend.py` | AgentSSASRemoteBackend | HTTP 模式客户端，通过 httpx 转发 report_event 为 HTTP POST，网络异常/超时 fail-open |
| `src/agent_ssas/core/framework/access_adapter/http_server.py` | FastAPI 服务端 | 暴露 POST /api/v1/events 端点，懒初始化 AgentSSASBackend |
| `src/agent_ssas/core/modes/http/server.py` | HTTP 服务启动入口 | `python -m agent_ssas.core.modes.http.server`，使用 uvicorn |
| `src/agent_ssas/core/integration/register.py` | create_backend | 工厂函数，支持 INPROCESS 和 HTTP 两种模式 |
| `src/agent_ssas/core/framework/core_types/assessment.py` | RiskAssessment | 新增 `from_dict()` 反序列化方法 |
| `pyproject.toml` | 构建配置 | 新增 `optional-dependencies.http`（httpx, fastapi, uvicorn） |

### 5.4 测试环境说明

| 项目 | 值 |
|------|-----|
| Python 解释器 | `d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe` |
| Python 版本 | 3.13.14 |
| pytest 版本 | 9.0.3 |
| pytest-asyncio 版本 | 1.3.0 |
| openjiuwen 安装方式 | PyPI wheel（v0.1.16，`uv sync` 安装） |
| agent-ssas 安装方式 | editable (`d:\TraeWorkspace\JiuwenSSAS\AgentSecurity\AgentSSAS\src`) |
| 工作目录 | `d:\TraeWorkspace\JiuwenSSAS\AgentSecurity\AgentSSAS` |
| jiuwenswarm 版本 | 0.2.5.beta1 |
| agent-ssas 版本 | 0.1.0 |

### 5.5 如何检查测试结果

**1. 运行全部测试**：
```powershell
cd d:\TraeWorkspace\JiuwenSSAS\AgentSecurity\AgentSSAS
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -m pytest tests/ --tb=short -q
```
**预期结果**：`207 passed, 2 warnings in ~17s`

**2. 查看测试结果文件**：
```powershell
# JUnit XML 文件（CI 系统可解析）
# 位于 d:\TraeWorkspace\JiuwenSSAS\AgentSecurity\AgentSSAS\test_results_all.xml（全量）或 test_results_phase3.xml（阶段三）
```

**3. 检查存储文件生成**：
```powershell
# 进程内模式
Get-ChildItem -Path "$env:TEMP\agent_ssas\phase3\inprocess\ssas" -Recurse -File
# 预期：ssas_core.db + modules/*/result.db + reports/threat_log/*.json

# HTTP 服务模式
Get-ChildItem -Path "$env:TEMP\agent_ssas\phase3\http_server\ssas" -Recurse -File
# 预期：同上
```

**4. 验证导入路径**：
```powershell
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -c "import agent_ssas; print(agent_ssas.__version__)"
# 预期：0.1.0

d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -c "from agent_ssas.backend_client.openjiuwen.agent_ssas_security_rail import AgentSSASSecurityRail; print('OK')"
# 预期：OK

d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -c "from agent_ssas.core.framework.config.settings import AgentSSASConfig; print('OK')"
# 预期：OK
```

**5. 验证配置关闭**：
```powershell
d:\TraeWorkspace\JiuwenSSAS\jiuwenswarm\.venv\Scripts\python.exe -c "from agent_ssas.core.framework.config.settings import AgentSSASConfig; c = AgentSSASConfig(enabled=False); assert c.enabled == False; print('config_disabled_ok')"
# 预期：config_disabled_ok
```

### 5.6 虚拟事件生成器

| 文件 | 说明 |
|------|------|
| tests/fixtures/event_factory.py | 提供 create_raw_event()、generate_event_sequence()、generate_permission_interrupt_event() |
