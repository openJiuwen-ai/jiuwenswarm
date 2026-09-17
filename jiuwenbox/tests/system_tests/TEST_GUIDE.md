# jiuwenbox 沙箱服务自动化测试执行指导书

## 一、测试框架概述

本自动化测试框架基于 Python + pytest 构建，用于验证 jiuwenbox 沙箱服务的性能与可靠性。测试框架包含以下模块：

| 模块 | 文件 | 功能 |
|------|------|------|
| 配置管理 | `test_config.py` | 测试配置参数和环境检测 |
| 工具类 | `test_utils.py` | 资源劣化管理、混沌注入、性能收集等 |
| 状态机测试 | `test_state_machine.py` | 沙箱生命周期状态转移测试 |
| 资源劣化测试 | `test_resource_degradation.py` | CPU/内存/网络/磁盘劣化测试 |
| 混沌工程测试 | `test_chaos_engineering.py` | 故障注入和系统韧性测试 |
| cgroup 测试 | `test_cgroup_limits.py` | CPU/内存资源限制测试 |
| 错误边界测试 | `test_error_boundaries.py` | 权限错误、磁盘满、网络超时等边界测试 |
| 性能测试 | `test_performance.py` | QPS、时延等性能指标测试 |
| 可靠性测试 | `test_reliability.py` | 自动恢复、fallback、reaper 等可靠性测试 |
| 配置约束测试 | `test_config_constraints.py` | 参数组合约束测试 |
| 文件传输测试 | `test_file_transfer.py` | 文件上传/下载速率、成功率、完整性测试 |
| 并发创建测试 | `test_concurrent_create.py` | 沙箱并发创建性能测试 |
| 长稳测试 | `test_long_stability.py` | 沙箱带负载长时间稳定性测试 |

## 二、环境要求

### 2.1 基础环境

- Python 3.10+
- pytest 7.0+
- httpx 0.24+

### 2.2 可选依赖（Linux 专用）

| 工具 | 用途 | 对应测试模块 |
|------|------|-------------|
| stress-ng | CPU/内存劣化 | 资源劣化测试、混沌工程测试 |
| tc (traffic control) | 网络劣化 | 网络劣化测试 |
| iproute2 | 网络断开/恢复 | 混沌工程测试 |
| Docker | 容器级测试 | 可靠性测试（REL-002、REL-007） |

### 2.3 环境检测

运行以下命令检测环境能力：

```bash
python -c "from tests.system_tests.test_config import EnvironmentDetector; EnvironmentDetector.print_capabilities()"
```

输出示例：

```
==================================================
Environment Capabilities
==================================================
  ✓ linux
  ✗ windows
  ✗ macos
  ✓ stress_ng
  ✓ tc
  ✓ iproute2
  ✓ docker
  ✓ resource_tests
  ✓ network_tests
  ✓ chaos_tests
==================================================
```

## 三、安装步骤

### 3.1 安装 Python 依赖

```bash
cd jiuwenbox
pip install pytest httpx
```

### 3.2 安装 Linux 专用工具（可选）

```bash
# Ubuntu/Debian
sudo apt-get install -y stress-ng iproute2

# CentOS/RHEL
sudo yum install -y stress-ng iproute2

# 确保 tc 模块可用
sudo modprobe sch_netem
```

## 四、配置说明

### 4.1 环境变量配置

所有配置参数均可通过环境变量覆盖：

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| JIUWENBOX_SERVER | https://localhost:8080 | 沙箱服务地址 |
| TEST_TIMEOUT | 120 | 测试默认超时（秒） |
| SANDBOX_READY_TIMEOUT | 30 | 沙箱就绪超时（秒） |
| EXEC_TIMEOUT | 60 | 命令执行超时（秒） |
| QPS_SINGLE_THRESHOLD | 50 | 单实例 QPS 阈值 |
| QPS_MULTI_THRESHOLD | 100 | 多实例 QPS 阈值 |
| LATENCY_COMMAND_THRESHOLD | 0.05 | 命令执行时延阈值（秒） |
| LATENCY_STARTUP_THRESHOLD | 0.1 | 沙箱启动时延阈值（秒） |
| LATENCY_SERVICE_THRESHOLD | 1.0 | 服务启动时延阈值（秒） |
| P999_SPIKE_THRESHOLD | 0.5 | P999 时延毛刺阈值（秒） |
| NETWORK_INTERFACE | eth0 | 网络接口名称 |
| FILE_UPLOAD_SPEED_THRESHOLD | 10 | 文件上传速率阈值（MB/s） |
| FILE_DOWNLOAD_SPEED_THRESHOLD | 20 | 文件下载速率阈值（MB/s） |
| FILE_TRANSFER_TIMEOUT | 300 | 文件传输超时（秒） |
| CONCURRENT_CREATE_TIMEOUT_5 | 10 | 并发创建5个沙箱超时（秒） |
| CONCURRENT_CREATE_TIMEOUT_10 | 20 | 并发创建10个沙箱超时（秒） |
| CONCURRENT_CREATE_TIMEOUT_20 | 40 | 并发创建20个沙箱超时（秒） |
| LONG_STABILITY_DURATION_30MIN | 1800 | 长稳测试30分钟时长（秒） |
| LONG_STABILITY_DURATION_1HOUR | 3600 | 长稳测试1小时时长（秒） |
| LONG_STABILITY_SANDBOX_COUNT | 5 | 长稳测试沙箱数量 |
| LONG_STABILITY_SUCCESS_RATE_THRESHOLD | 0.999 | 长稳测试成功率阈值 |
| LONG_STABILITY_AVG_LATENCY_THRESHOLD | 0.1 | 长稳测试平均时延阈值（秒） |
| LONG_STABILITY_P99_LATENCY_THRESHOLD | 0.3 | 长稳测试P99时延阈值（秒） |

### 4.2 配置示例

```bash
export JIUWENBOX_SERVER=https://192.168.1.100:8080
export QPS_SINGLE_THRESHOLD=150
export QPS_MULTI_THRESHOLD=500
export LATENCY_COMMAND_THRESHOLD=0.05
export LATENCY_STARTUP_THRESHOLD=0.1
export LATENCY_SERVICE_THRESHOLD=1.0
```

## 五、测试执行

### 5.1 运行所有系统测试

```bash
python -m pytest tests/system_tests/ -v
```

### 5.2 运行特定模块测试

```bash
# 状态机测试
python -m pytest tests/system_tests/test_state_machine.py -v

# 性能测试
python -m pytest tests/system_tests/test_performance.py -v

# 可靠性测试
python -m pytest tests/system_tests/test_reliability.py -v

# 资源劣化测试
python -m pytest tests/system_tests/test_resource_degradation.py -v

# 混沌工程测试
python -m pytest tests/system_tests/test_chaos_engineering.py -v

# 文件传输测试
python -m pytest tests/system_tests/test_file_transfer.py -v

# 并发创建测试
python -m pytest tests/system_tests/test_concurrent_create.py -v

# 长稳测试（30分钟）
python -m pytest tests/system_tests/test_long_stability.py::TestLongStability::test_ls_001_long_stability_30min -v

# 长稳测试（1小时）
python -m pytest tests/system_tests/test_long_stability.py::TestLongStability::test_ls_002_long_stability_1hour -v
```

### 5.3 使用标记运行测试

```bash
# 运行所有系统测试
python -m pytest tests/system_tests/ -m system -v

# 运行性能测试
python -m pytest tests/system_tests/ -m performance -v

# 运行慢速测试（资源劣化、混沌工程等）
python -m pytest tests/system_tests/ -m slow -v

# 排除慢速测试
python -m pytest tests/system_tests/ -v --ignore-glob="*slow*"
```

### 5.4 运行特定测试用例

```bash
# 运行单个测试用例
python -m pytest tests/system_tests/test_performance.py::TestPerformanceMetrics::test_perf_001_qps_single_sandbox -v

# 运行多个测试用例
python -m pytest tests/system_tests/test_reliability.py::TestReliability::test_rel_001_sandbox_process_auto_restart \
                  tests/system_tests/test_reliability.py::TestReliability::test_rel_002_container_process_auto_restart -v
```

### 5.5 生成测试报告

```bash
# 生成 HTML 报告
pip install pytest-html
python -m pytest tests/system_tests/ -v --html=test_report.html

# 生成 XML 报告（用于 CI/CD）
pip install pytest-xdist pytest-junitxml
python -m pytest tests/system_tests/ -v --junitxml=test_results.xml

# 并行运行测试（加速）
python -m pytest tests/system_tests/ -v -n auto
```

## 六、测试用例覆盖清单

### 6.1 已实现的测试用例（82/82）

| 测试类别 | 用例数 | 状态 |
|----------|--------|------|
| 状态机模型 | 17 | ✅ 完整 |
| 资源劣化矩阵 | 14 | ✅ 完整 |
| 混沌工程 | 8 | ✅ 完整 |
| cgroup 资源限制 | 6 | ✅ 完整 |
| 错误边界 | 6 | ✅ 完整 |
| 性能指标 | 7 | ✅ 完整 |
| 可靠性 | 10 | ✅ 完整 |
| 配置参数约束 | 6 | ✅ 完整 |
| 文件传输 | 3 | ✅ 完整 |
| 并发创建 | 3 | ✅ 完整 |
| 长稳测试 | 2 | ✅ 完整 |

### 6.2 测试标记说明

| 标记 | 说明 |
|------|------|
| @pytest.mark.system | 系统测试 |
| @pytest.mark.performance | 性能测试 |
| @pytest.mark.slow | 慢速测试（资源劣化、混沌工程等） |

## 七、测试注意事项

### 7.1 权限要求

部分测试需要 root 权限：
- cgroup 资源限制测试
- 网络劣化测试（tc 命令）
- 网络断开/恢复测试（ip 命令）

建议使用 sudo 运行：

```bash
sudo python -m pytest tests/system_tests/ -v
```

### 7.2 环境兼容性

| 测试模块 | Linux | Windows | macOS |
|----------|-------|---------|-------|
| 状态机测试 | ✅ | ✅ | ✅ |
| cgroup 测试 | ✅ | ✗ | ✗ |
| 资源劣化测试 | ✅（需 stress-ng） | ✗ | ✗ |
| 混沌工程测试 | ✅（需 stress-ng + iproute2） | ✗ | ✗ |
| 错误边界测试 | ✅ | ✅ | ✅ |
| 性能测试 | ✅ | ✅ | ✅ |
| 可靠性测试 | ✅ | ✅ | ✅ |
| 配置约束测试 | ✅ | ✅ | ✅ |

非兼容环境下，相关测试会自动跳过，不会报错。

### 7.3 测试顺序

建议按以下顺序执行测试：

1. 状态机测试（基础功能验证）
2. 配置约束测试（参数验证）
3. cgroup 测试（资源限制验证）
4. 错误边界测试（边界条件验证）
5. 可靠性测试（核心可靠性验证）
6. 性能测试（性能指标验证）
7. 资源劣化测试（资源劣化验证）
8. 混沌工程测试（故障注入验证）

### 7.4 测试时间估算

| 测试模块 | 预计时间 |
|----------|---------|
| 状态机测试 | 5-10 分钟 |
| 配置约束测试 | 5-10 分钟 |
| cgroup 测试 | 10-15 分钟 |
| 错误边界测试 | 10-15 分钟 |
| 可靠性测试 | 15-30 分钟 |
| 性能测试 | 15-30 分钟 |
| 资源劣化测试 | 30-60 分钟 |
| 混沌工程测试 | 30-60 分钟 |
| 文件传输测试 | 10-30 分钟 |
| 并发创建测试 | 5-30 分钟 |
| 长稳测试（30分钟） | 30 分钟 |
| 长稳测试（1小时） | 60 分钟 |
| **总计（不含长稳）** | **120-220 分钟** |
| **总计（含长稳）** | **210-280 分钟** |

## 八、故障排查

### 8.1 常见问题

| 问题 | 原因 | 解决方案 |
|------|------|----------|
| 连接拒绝 | 服务未启动或地址错误 | 检查 JIUWENBOX_SERVER 环境变量 |
| 权限错误 | 测试需要 root 权限 | 使用 sudo 运行测试 |
| stress-ng 未找到 | 未安装 stress-ng | 安装 stress-ng |
| tc 未找到 | 未安装 iproute2 | 安装 iproute2 |
| 测试超时 | 服务响应慢或资源不足 | 增加 TEST_TIMEOUT 环境变量 |
| 网络测试失败 | 网络接口名称错误 | 设置 NETWORK_INTERFACE 环境变量 |

### 8.2 调试技巧

```bash
# 启用详细日志
python -m pytest tests/system_tests/ -v -s

# 只运行失败的测试
python -m pytest tests/system_tests/ -v --last-failed

# 运行单个测试并启用调试
python -m pytest tests/system_tests/test_performance.py::TestPerformanceMetrics::test_perf_001_qps_single_sandbox -v -s
```

## 九、CI/CD 集成

### 9.1 GitHub Actions 示例

```yaml
name: System Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.10'
      - name: Install dependencies
        run: |
          pip install pytest httpx
          sudo apt-get install -y stress-ng iproute2
          sudo modprobe sch_netem
      - name: Start jiuwenbox server
        run: |
          # 启动沙箱服务
          nohup python -m jiuwenbox.server --host 0.0.0.0 --port 8080 &
          sleep 10
      - name: Run system tests
        run: |
          export JIUWENBOX_SERVER=https://localhost:8080
          python -m pytest tests/system_tests/ -v --junitxml=test_results.xml
      - name: Upload test results
        uses: actions/upload-artifact@v4
        with:
          name: test-results
          path: test_results.xml
```

### 9.2 GitLab CI 示例

```yaml
system_tests:
  stage: test
  image: python:3.10
  before_script:
    - pip install pytest httpx
    - apt-get update && apt-get install -y stress-ng iproute2
    - modprobe sch_netem
    - # 启动沙箱服务
    - python -m jiuwenbox.server --host 0.0.0.0 --port 8080 &
    - sleep 10
  script:
    - export JIUWENBOX_SERVER=https://localhost:8080
    - python -m pytest tests/system_tests/ -v --junitxml=test_results.xml
  artifacts:
    reports:
      junit: test_results.xml
```

## 十、扩展指南

### 10.1 添加新测试用例

1. 在 `test_cases.md` 中定义测试用例
2. 在对应的测试文件中实现测试函数
3. 使用 pytest 标记（`@pytest.mark.system` 等）

### 10.2 添加新配置参数

1. 在 `test_config.py` 的 `TestConfig` 类中添加新参数
2. 在测试中引用该参数
3. 在指导书中添加环境变量说明

### 10.3 添加新工具类

1. 在 `test_utils.py` 中添加新类或方法
2. 在测试中导入并使用

---

**文档版本**: v1.0  
**创建日期**: 2026-07-01  
**适用框架**: pytest + Python 3.10+
