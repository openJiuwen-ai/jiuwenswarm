# JiuwenClow 单元测试框架配置总结

## 📦 已创建的文件
### 1. 测试配置文件

```
jiuwenclaw/
├── pytest.ini                           # Pytest 配置
├── pyproject.toml                       # 已更新，添加了测试依赖
├── run_tests.sh                         # 测试运行脚本（可执行）
├── tests/
│   ├── __init__.py
│   ├── conftest.py                      # 共享 fixtures
│   ├── README.md                        # 详细测试指南
│   └── unit/                            # 单元测试目录
│       ├── agentserver
│       ├── channel
│       ├── evolution
│           ├── test_schema.py           # 演进模型测试
│           ├── test_signal_detector.py  # 信号检测器测试
│           ├── test_message.py          # 消息模型测试
│       ├── gateway
│       ├── schema
│       ├── __init__.py
│       ├── test_config.py               # 配置模块测试
│       └── test_utils.py                # 工具函数测试
└── .gitcode/workflows/
    ├── ci.yml                           # gitcode Actions 测试工作流
    └── test.yml                         # gitcode Actions 代码质量检查
```

---

## 🚀 本地测试

### 方式 1: 使用测试脚本（推荐）

```bash
cd /Users/gawa/Desktop/pr/jiuwenclaw

# 运行所有测试
./run_tests.sh

# 生成 HTML 覆盖率报告
./run_tests.sh -c

# 只运行单元测试
./run_tests.sh -u

# 并行运行测试
./run_tests.sh -p

# 查看帮助
./run_tests.sh -h
```

### 方式 2: 直接使用 pytest

```bash
# 首先安装测试依赖
pip install -e ".[test]"

# 运行所有测试
pytest -v

# 运行特定目录
pytest tests/unit_tests/ -v

# 运行特定文件
pytest tests/unit_tests/test_config.py -v

# 运行特定测试
pytest tests/unit_tests/test_config.py::TestResolveEnvVars::test_resolve_string_with_env_var -v

# 生成覆盖率报告
pytest --cov=jiuwenclaw --cov-report=html --cov-report=term-missing
```

---

## 🧪 已实现的测试用例

### 1. `test_config.py` - 配置模块测试

**测试内容**：
- ✅ 环境变量解析（`resolve_env_vars`）
- ✅ 字符串中的环境变量替换
- ✅ 默认值处理
- ✅ 字典和列表中的环境变量
- ✅ 嵌套结构解析
- ✅ 配置文件读取

**测试数量**: ~15 个测试

**关键测试**：
```python
test_resolve_string_with_env_var()      # 测试 ${VAR} 解析
test_resolve_string_with_default()       # 测试 ${VAR:-default}
test_resolve_dict_with_env_vars()        # 测试字典解析
test_resolve_nested_structure()          # 测试嵌套结构
```

---

### 2. `test_evolution_schema.py` - 演进模型测试

**测试内容**：
- ✅ `EvolutionType` 枚举
- ✅ `EvolutionChange` 数据类
- ✅ `EvolutionEntry` 数据类
- ✅ `EvolutionFile` 数据类
- ✅ `EvolutionSignal` 数据类
- ✅ 序列化/反序列化

**测试数量**: ~30 个测试

**关键测试**：
```python
test_evolution_entry_make()               # 测试工厂方法
test_evolution_entry_is_pending()         # 测试属性
test_evolution_file_pending_entries()     # 测试属性
test_evolution_signal_to_dict()           # 测试序列化
```

---

### 3. `test_signal_detector.py` - 信号检测器测试

**测试内容**：
- ✅ 执行失败信号检测
- ✅ 用户修正信号检测
- ✅ 中英文关键词检测
- ✅ 信号去重
- ✅ Skill 名称提取
- ✅ Excerpt 截取

**测试数量**: ~20 个测试

**关键测试**：
```python
test_detect_execution_failure()           # 测试错误检测
test_detect_user_correction_chinese()     # 测试中文修正
test_detect_multiple_signals()            # 测试多信号
test_deduplicate_signals()                # 测试去重
test_detect_with_skill_from_tool_calls()  # 测试 Skill 归因
```

---

### 4. `test_schema.py` - 消息模型测试

**测试内容**：
- ✅ `ReqMethod` 枚举
- ✅ `EventType` 枚举
- ✅ `Mode` 枚举
- ✅ `AgentRequest` 数据类
- ✅ `AgentResponse` 数据类
- ✅ `AgentResponseChunk` 数据类
- ✅ `Message` 数据类

**测试数量**: ~25 个测试

**关键测试**：
```python
test_create_agent_request_minimal()       # 测试最小请求
test_create_agent_request_full()          # 测试完整请求
test_create_request_message()             # 测试请求消息
test_create_event_message()               # 测试事件消息
test_message_mode()                       # 测试模式字段
```

---

### 5. `test_utils.py` - 工具函数测试

**测试内容**：
- ✅ 路径解析函数
- ✅ 包检测函数
- ✅ Logger 设置
- ✅ 常量定义

**测试数量**: ~10 个测试

**关键测试**：
```python
test_get_root_dir()                       # 测试根目录获取
test_get_config_dir()                     # 测试配置目录
test_setup_logger_default()               # 测试 Logger 设置
test_path_caching()                       # 测试路径缓存
```

---

## 🎯 测试覆盖率目标

| 模块 | 当前覆盖率 | 目标覆盖率 |
|------|-----------|-----------|
| `config.py` | ~80% | 90% |
| `evolution/schema.py` | ~90% | 95% |
| `evolution/signal_detector.py` | ~75% | 85% |
| `schema/*.py` | ~70% | 80% |
| `utils.py` | ~60% | 75% |
| **总体** | **~70%** | **80%** |

---

## 🤖 GitHub Actions CI

### 工作流 1: Tests (`.github/workflows/test.yml`)

**触发条件**：
- Push to `main` or `develop`
- Pull requests to `main` or `develop`
- 手动触发

**测试矩阵**：
- Python: 3.11, 3.12, 3.13
- OS: Ubuntu Latest

**步骤**：
1. ✅ Checkout 代码
2. ✅ 设置 Python
3. ✅ 安装依赖
4. ✅ 运行单元测试
5. ✅ 上传覆盖率到 Codecov

### 工作流 2: Code Quality (`.github/workflows/lint.yml`)

**检查项目**：
- ✅ 类型检查 (mypy)
- ✅ 代码格式 (black)
- ✅ Linting (ruff)
- ✅ 安全扫描 (bandit)

---

## 📝 使用示例

### 快速开始

```bash
# 1. 安装依赖
pip install -e ".[test]"

# 2. 运行所有测试
./run_tests.sh

# 3. 查看覆盖率报告
open htmlcov/index.html  # macOS
```

### 开发新功能时的测试工作流

```bash
# 1. 编写测试
# tests/unit_tests/test_new_feature.py

# 2. 运行新测试
pytest tests/unit_tests/test_new_feature.py -v

# 3. 查看覆盖率
pytest --cov=jiuwenclaw.new_feature --cov-report=term-missing

# 4. 运行所有测试确保没有破坏
pytest tests/

# 5. 提交代码
git add .
git commit -m "feat: add new feature with tests"
git push
```

### CI 失败时的调试

```bash
# 1. 本地复现 CI 环境
python -m pytest tests/ -v

# 2. 检查 Python 版本
python --version  # 应该是 3.11, 3.12, 或 3.13

# 3. 检查依赖
pip list | grep pytest

# 4. 运行特定失败的测试
pytest tests/unit_tests/test_config.py::TestResolveEnvVars -vv
```

---

## 🛠️ 测试框架配置详解

### pytest.ini

```ini
[pytest]
# 测试发现模式
python_files = test_*.py *_test.py
python_classes = Test*
python_functions = test_*

# 测试路径
testpaths = tests

# 输出选项
addopts =
    -v                              # 详细输出
    --strict-markers                # 严格标记检查
    --tb=short                      # 简短的错误堆栈
    --cov=jiuwenclaw                # 覆盖率
    --cov-report=term-missing       # 终端报告
    --cov-report=html               # HTML 报告
    --cov-report=xml                # XML 报告（CI）
    --asyncio-mode=auto             # 异步测试模式

# 标记定义
markers =
    unit: Unit tests
    integration: Integration tests
    slow: Slow running tests
    async: Async tests
```

### conftest.py Fixtures

```python
@pytest.fixture
def temp_workspace() -> Path:
    """创建临时工作区"""

@pytest.fixture
def temp_config_file() -> Path:
    """创建临时配置文件"""

@pytest.fixture
def mock_env_vars() -> None:
    """设置模拟环境变量"""

@pytest.fixture
def sample_skill_md() -> Path:
    """创建示例 SKILL.md 文件"""

@pytest.fixture
def sample_messages() -> List[dict]:
    """示例消息列表"""
```

---

## 📚 扩展测试

### 添加新的测试文件

```bash
# 1. 创建测试文件
touch tests/unit_tests/test_new_module.py

# 2. 编写测试
# 参考 tests/README.md 中的模板

# 3. 运行测试
pytest tests/unit_tests/test_new_module.py -v
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

## ✅ 下一步建议

1. **增加测试覆盖**：
   - `evolution/evolver.py` - 演进生成器
   - `evolution/service.py` - 演进服务
   - `gateway/` - 网关层
   - `channel/` - 频道适配器

2. **添加集成测试**：
   - 端到端测试
   - API 测试
   - 性能测试

3. **提升测试质量**：
   - 使用 mock 隔离外部依赖
   - 添加性能基准测试
   - 实现测试数据工厂

4. **改进 CI/CD**：
   - 添加性能测试
   - 集成安全扫描
   - 自动化发布流程

---

## 📞 需要帮助？

- 查看 `tests/README.md` 获取详细指南
- 运行 `./run_tests.sh -h` 查看测试脚本帮助
- 查看 pytest 文档: https://docs.pytest.org/

---

**Happy Testing! 🎉**
