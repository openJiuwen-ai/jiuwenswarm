# 运行测试

本文说明如何运行 AgentSSAS 的单元测试、集成测试、代码检查工具，以及阶段三端到端验证脚本。

## 运行前提

进入 AgentSSAS 仓库根目录并安装依赖：

```bash
cd AgentSecurity/AgentSSAS
uv pip install -e .
```

测试本身依赖 `pytest` 与 `pytest-asyncio`（声明在 `pyproject.toml` 的 `test` 扩展中）：

```bash
uv pip install -e ".[test]"
```

HTTP 模式相关测试（`tests/agent_ssas/core/integration/test_e2e_http.py`、`test_http_server.py`、`test_remote_backend.py` 等）额外依赖 `fastapi`、`uvicorn`、`httpx`，需要安装 `http` 扩展：

```bash
uv pip install -e ".[http]"
```

两种扩展也可以同时安装：

```bash
uv pip install -e ".[test,http]"
```

若使用 uv 运行脚本，可通过 `--extra` 指定扩展，例如：

```bash
uv run --extra http python examples/http_server_demo/start_server.py
```

## 直接使用 pytest

```bash
cd AgentSecurity/AgentSSAS
python -m pytest tests/ --tb=short -q
```

`pyproject.toml` 中已内置 pytest 配置：`testpaths = ["tests"]`、`asyncio_mode = "auto"`（异步测试无需逐个标记）、`addopts = "-v --strict-markers --tb=short"`。因此在仓库根目录直接执行 `pytest` 也会收集 `tests/` 下的全部用例。

运行单个文件或单个用例：

```bash
# 单个文件
python -m pytest tests/agent_ssas/core/framework/test_config.py -q

# 单个用例
python -m pytest tests/agent_ssas/core/framework/test_config.py::TestAgentSSASConfig::test_from_dict_none_returns_default -q

# 按标记过滤（见下文 markers 说明）
python -m pytest tests/ -m unit -q
python -m pytest tests/ -m "not slow" -q
```

## pytest markers

`pyproject.toml` 的 `[tool.pytest.ini_options]` 中声明了以下标记（配合 `--strict-markers` 使用，未声明的标记会报错）：

| 标记 | 说明 |
|------|------|
| `unit` | 快速确定性单元测试 |
| `integration` | 集成测试 |
| `system` | 系统测试（需真实环境，CI 跳过） |
| `level0` | 冒烟/快乐路径，PR 门禁必须绿色 |
| `level1` | 功能分支/错误路径/边缘场景 |
| `slow` | 耗时较长的测试 |

## 使用 Makefile

仓库根目录的 `Makefile` 提供统一入口（依赖 ruff、pylint、mypy、codespell 工具链）：

```bash
make install      # 安装 lint/test 工具链（ruff, pylint, mypy, codespell）
make test         # 运行测试（等价于 pytest tests/ --tb=short -q）
make lint         # 代码检查（ruff + pylint + mypy，容错执行）
make format       # 代码格式化（ruff format src/ tests/）
make typecheck    # 类型检查（mypy src/agent_ssas）
```

另外还有两个辅助目标：

```bash
make lint-fix     # 自动修复 ruff 可修复的问题
make check        # 完整检查（当前为 lint）
```

Windows 环境如未安装 make，可直接执行 `Makefile` 中的对应命令，例如 `pytest tests/ --tb=short -q`、`ruff check src/ tests/`。

## 阶段三验证脚本

`tests/agent_ssas/core/integration/test_verify_phase3.py` 提供独立可执行的端到端验证命令，覆盖进程内模式、HTTP 服务模式、配置关闭三种场景：

```bash
cd AgentSecurity/AgentSSAS
python -m tests.verify_phase3 all
```

可用子命令：

| 子命令 | 说明 |
|--------|------|
| `inprocess` | 进程内模式验证：Rail 注册 + 事件上报 + 日志生成 |
| `http-server` | 启动 HTTP 服务端（前台运行，Ctrl+C 退出） |
| `http-client` | HTTP 模式验证：向已启动的服务端发送事件 |
| `disabled` | 配置关闭验证 |
| `all` | 依次执行 inprocess + disabled（不含 http-server/http-client） |

说明：

- 脚本将 `SSAS_HOME` 指向系统临时目录（`$TEMP/agent_ssas/phase3/<tag>`）并在每次运行前清理旧数据，不会污染 `~/.jiuwenswarm`。
- `http-server` / `http-client` 子命令需要安装 `http` 扩展依赖，且需在两个终端分别执行。

## 常见问题

- **收集到 0 个用例**：确认当前目录是 `AgentSecurity/AgentSSAS`（`pyproject.toml` 所在目录），pytest 依据其中的 `testpaths` 配置收集。
- **HTTP 测试报 `ModuleNotFoundError: fastapi`（或 uvicorn/httpx）**：安装 `http` 扩展，见上文运行前提。
- **异步测试失败或被跳过**：确认安装了 `pytest-asyncio`；`asyncio_mode = "auto"` 已在配置中启用。
- **Windows 下文件锁定导致清理失败**：验证脚本内置了兜底策略（重命名被锁定的目录），若仍失败，关闭占用 SQLite 文件的进程后重试。

## 相关文档

- [HTTP API 参考](../reference/http-api.md)
- [配置参考](../reference/configuration.md)
- 仓库根 [Makefile](../../../Makefile) 的 `test` 目标与 [pyproject.toml](../../../pyproject.toml) 的 `[tool.pytest.ini_options]` 配置
