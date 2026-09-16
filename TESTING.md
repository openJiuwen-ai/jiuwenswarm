# JiuwenSwarm 测试指南

本文档介绍 JiuwenSwarm 的测试目录结构、本地测试运行方式和测试编写规范。

## 📦 测试目录结构

```
jiuwenswarm/
├── pytest.ini                           # Pytest 配置
├── pyproject.toml                       # 测试依赖（[test] 可选依赖组 / test 依赖组）
├── run_tests.sh                         # 测试运行脚本（可执行）
└── tests/
    ├── __init__.py
    ├── conftest.py                      # 共享 fixtures
    ├── README.md                        # 详细测试指南
    ├── unit_tests/                      # 单元测试主目录
    │   ├── a2ui/                        # A2UI 协议测试
    │   ├── acp/                         # ACP 客户端测试
    │   ├── agents/                      # Agent 编排测试
    │   ├── agentserver/                 # Agent 服务端测试
    │   ├── auto_harness/                # Auto Harness 测试
    │   ├── channel/                     # 频道适配器测试
    │   ├── cli/                         # CLI 测试
    │   ├── common/                      # 公共模块测试
    │   ├── e2a/                         # E2A 协议测试
    │   ├── evolution/                   # Skill 自演进测试
    │   ├── extensions/                  # 扩展体系测试
    │   ├── gateway/                     # 网关测试
    │   ├── server/                      # 服务端测试
    │   ├── symphony/                    # Symphony 技能编排测试
    │   └── test_*.py                    # 顶层单元测试文件
    ├── unit/                            # 补充单元测试（agentserver / channel / deep_agent）
    ├── integration/                     # 集成测试
    ├── symphony/                        # Symphony 子系统测试
    ├── system_tests/                    # 系统测试
    ├── ui_e2e/                          # UI 端到端测试
    └── agents/                          # Agent 相关测试
```

---

## 🚀 本地测试

### 方式 1: 使用测试脚本（推荐）

```bash
# 在仓库根目录下执行

# 运行所有测试
./run_tests.sh

# 生成 HTML 覆盖率报告
./run_tests.sh -c

# 只运行单元测试
./run_tests.sh -u

# 只运行集成测试
./run_tests.sh -i

# 并行运行测试（需要 pytest-xdist）
./run_tests.sh -p

# 查看帮助
./run_tests.sh -h
```

### 方式 2: 直接使用 pytest

```bash
# 首先安装测试依赖（uv 环境下 test 依赖组默认随 dev 组安装）
uv sync
# 或使用 pip
pip install -e ".[test]"

# 运行所有测试
uv run pytest -v

# 运行单元测试目录
uv run pytest tests/unit_tests/ -v

# 运行特定文件
uv run pytest tests/unit_tests/test_config.py -v

# 运行特定测试
uv run pytest tests/unit_tests/test_config.py::TestResolveEnvVars::test_resolve_string_with_env_var -v

# 生成覆盖率报告
uv run pytest --cov=jiuwenswarm --cov-report=html --cov-report=term-missing
```

---

## 🛠️ 测试框架配置

### pytest.ini 要点

完整配置见仓库根目录 [pytest.ini](pytest.ini)，关键项：

- **测试发现**：`test_*.py` / `*_test.py`，类 `Test*`，函数 `test_*`，测试路径 `tests/`
- **默认参数**（`addopts`）：`-v --strict-markers --tb=short`，并默认开启覆盖率
  （`--cov=jiuwenswarm`，输出 term-missing / html / xml 三种报告）
- **异步测试**：`--asyncio-mode=auto`，`async def` 测试函数无需手动标记
- **标记**（markers）：`unit` / `integration` / `system` / `slow` / `async`
- **警告策略**：`filterwarnings = error`（未忽略的警告会导致测试失败），
  忽略 `DeprecationWarning` 与 `PendingDeprecationWarning`

### conftest.py 共享 Fixtures

定义于 [tests/conftest.py](tests/conftest.py)：

```python
@pytest.fixture
def temp_workspace() -> Path:
    """创建临时工作区"""

@pytest.fixture
def temp_config_file(temp_workspace) -> Path:
    """创建临时配置文件"""

@pytest.fixture
def mock_env_vars(monkeypatch) -> None:
    """设置模拟环境变量"""

@pytest.fixture
def sample_skill_md(temp_workspace) -> Path:
    """创建示例 SKILL.md 文件"""

@pytest.fixture
def sample_messages() -> List[dict]:
    """示例消息列表"""
```

---

## 📝 开发新功能时的测试工作流

```bash
# 1. 编写测试（放在对应模块的子目录下）
# tests/unit_tests/<module>/test_new_feature.py

# 2. 运行新测试
uv run pytest tests/unit_tests/<module>/test_new_feature.py -v

# 3. 查看覆盖率
uv run pytest --cov=jiuwenswarm.<module> --cov-report=term-missing

# 4. 运行单元测试确保没有破坏现有功能
uv run pytest tests/unit_tests/

# 5. 提交代码（遵循 Conventional Commits）
git add <changed files>
git commit -m "feat(<scope>): add new feature with tests"
git push
```

### 添加新的 Fixture

```python
# 在 tests/conftest.py 中添加

@pytest.fixture
def my_custom_fixture():
    """自定义 fixture."""
    # 设置
    data = {"key": "value"}
    yield data
    # 清理（可选）
```

---

## 🤖 CI 状态

仓库当前尚未包含 CI 工作流配置文件。PR 的自动化检查与评审流程以
[贡献指南](docs/zh/贡献指南.md)（[Contributing](docs/en/Contributing.md)）为准：
PR 提交至 `develop` 分支，经至少两名 Committer 批准后合入。

提交 PR 前请在本地确保单元测试通过：

```bash
uv run pytest tests/unit_tests/
```

---

## 📞 需要帮助？

- 查看 [tests/README.md](tests/README.md) 获取详细指南
- 运行 `./run_tests.sh -h` 查看测试脚本帮助
- 查看 pytest 文档: https://docs.pytest.org/

---

**Happy Testing! 🎉**
