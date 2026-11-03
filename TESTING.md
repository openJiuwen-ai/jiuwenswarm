# 测试指南

本文说明 JiuwenSwarm 的测试分层、运行方式和编写约定。所有命令从仓库根目录执行。

## 测试分层

| 目录 | 用途 | 说明 |
|---|---|---|
| `tests/unit_tests/` | 单元测试 | 主力目录，约 980 个测试文件 |
| `tests/unit/` | 早期单元测试 | 8 个测试文件，`make test-unit` 不包含 |
| `tests/integration/` | 集成测试 | 5 个测试文件，`make test-integration` 的筛选目录 |
| `tests/system_tests/` | 系统测试 | 需要完整运行环境 |
| `tests/ui_e2e/` | UI 端到端 | 需要 Playwright 和已启动的服务 |
| `tests/electron/` | Electron 桌面端 | Node/CJS 用例 |
| `tests/frontend/` | 前端单点用例 | `.mjs` 文件，由 Node 运行 |
| `tests/agents/`、`tests/auth/`、`tests/symphony/` | 模块级测试 | 按领域划分 |

运行前请注意两点：

- `make test-unit` 只运行 `tests/unit_tests/`。`tests/unit/` 只在 `make test` 中执行。
- `make test-integration` 按目录筛选。部分标记为 `integration` 的用例位于 `tests/unit_tests/`。需要按标记运行时，使用 `uv run pytest -m integration`。

## 环境准备

支持的 Python 版本为 3.11、3.12、3.13 和 3.14。

```bash
uv sync
source .venv/bin/activate
export PYTHONPATH=.:$PYTHONPATH
```

`uv sync` 默认安装 `dev` 组。该组包含 test 和 lint 依赖。

## 运行 Python 测试

| 命令 | 范围 |
|---|---|
| `make test` | 全部 `tests/` |
| `make test-unit` | `tests/unit_tests/` |
| `make test-integration` | `tests/integration/` |
| `make test-cov` | 全部测试，并生成 HTML 覆盖率 |

等价的 pytest 命令：

```bash
# 单个目录
uv run pytest tests/unit_tests/ -v

# 单个文件
uv run pytest tests/unit_tests/server/test_xxx.py -v

# 单个用例
uv run pytest tests/unit_tests/server/test_xxx.py::test_name -v

# 按关键字筛选
uv run pytest tests/unit_tests/ -k keyword

# 按标记筛选
uv run pytest -m integration

# 快速运行，跳过覆盖率
uv run pytest tests/unit_tests/ --no-cov
```

`run_tests.sh` 提供相同的运行能力：

| 选项 | 作用 |
|---|---|
| 无参数 | 运行 `tests/` |
| `-u` | 只运行 `tests/unit_tests/` |
| `-i` | 只运行 `tests/integration/` |
| `-c` | 生成 HTML 覆盖率报告 |
| `-v` | 提高输出详细度 |
| `-p` | 并行执行，需要额外安装 `pytest-xdist` |
| `-h` | 显示帮助 |

`pytest-xdist` 不在默认依赖中。未安装时，`-p` 回退为串行执行。

## 前端测试

```bash
cd jiuwenswarm/channels/web/frontend
npm ci
npm run test:<功能名>
npm run build
```

- `package.json` 按功能提供 `test:*` 脚本。请选择与改动相关的脚本运行。
- 当前没有汇总的 `npm test` 入口。
- `npm run build` 先执行 `tsc` 类型检查，再执行 Vite 构建。

## 覆盖率

`pytest.ini` 的 `addopts` 默认开启覆盖率，并输出三种报告：

- 终端：`--cov-report=term-missing`
- HTML：`htmlcov/index.html`
- XML：`coverage.xml`

`pytest.ini` 未设置 `fail_under`。80% 是团队目标，不会阻断测试。调试单个文件时加 `--no-cov`，避免覆盖完整报告。

## 编写约定

- 文件名使用 `test_*.py` 或 `*_test.py`。
- 测试函数使用 `test_*`，测试类使用 `Test*`。
- 异步用例由 `pytest-asyncio` 的 auto 模式处理，不需要加 `@pytest.mark.asyncio`。
- 已注册的标记为 `unit`、`integration`、`system`、`slow` 和 `async`。`--strict-markers` 已开启，新增标记必须先写入 `pytest.ini`。
- `filterwarnings = error` 已开启。第三方告警必须在 `pytest.ini` 中按消息精确放行，不要全局关闭。
- 共享 fixture 位于 `tests/conftest.py`：`temp_workspace`、`temp_config_file`、`mock_env_vars`、`sample_skill_md`、`sample_messages`。

## 持续集成状态

仓库当前没有配置 CI 工作流。测试、lint 和类型检查需要在本地执行：

```bash
make test-unit
make lint
make typecheck
```

提交 PR 前，请至少运行与改动相关的测试，并在 PR 描述中写明命令和结果。
