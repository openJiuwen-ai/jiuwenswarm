# 单元测试指南

## 📋 目录

- [安装测试依赖](#安装测试依赖)
- [本地运行测试](#本地运行测试)
- [测试覆盖率](#测试覆盖率)

---

## 🔧 安装测试依赖

### 安装测试依赖

```bash
# 方式 1: 使用 pip
pip install -e ".[test]"

# 方式 2: 直接安装测试包
pip install pytest pytest-asyncio pytest-cov pytest-mock coverage freezegun
```

---

## 🏃 本地运行测试

### 运行所有测试

```bash
# 运行所有测试
pytest

# 或者指定目录
pytest tests/
```

### 运行特定测试文件

```bash
# 运行单个测试文件
pytest tests/unit_tests/test_config.py

# 运行特定目录的测试
pytest tests/unit_tests/
```

### 运行特定测试用例

```bash
# 运行特定测试函数
pytest tests/unit_tests/test_config.py::TestResolveEnvVars::test_resolve_string_with_env_var

# 运行特定测试类
pytest tests/unit_tests/test_config.py::TestResolveEnvVars
```

### 常用测试选项

```bash
# 详细输出
pytest -v

# 显示打印输出
pytest -s

# 显示错误堆栈
pytest --tb=long

# 只运行失败的测试
pytest --lf

# 遇到第一个失败就停止
pytest -x

# 并行运行测试（需要安装 pytest-xdist）
pytest -n auto
```

### 运行带标记的测试

```bash
# 只运行单元测试
pytest -m unit

# 只运行集成测试
pytest -m integration

# 只运行慢速测试
pytest -m slow

# 排除慢速测试
pytest -m "not slow"
```

---

## 📊 测试覆盖率

### 生成覆盖率报告

```bash
# 生成终端报告
pytest --cov=jiuwenclaw --cov-report=term-missing

# 生成 HTML 报告
pytest --cov=jiuwenclaw --cov-report=html

# 生成 XML 报告（用于 CI）
pytest --cov=jiuwenclaw --cov-report=xml
```

### 查看覆盖率报告

```bash
# 生成 HTML 报告后在浏览器中打开
pytest --cov=jiuwenclaw --cov-report=html
open htmlcov/index.html  # macOS
xdg-open htmlcov/index.html  # Linux
```
