---
id: B_20261031_1048_ttse-core-dependent-ut
title: 暂时移除依赖未合入 core 工厂能力的插件 UT
type: B
author: xinyang_bali
author_email: 835397263@qq.com
designs: []
design_note: 用户明确要求暂时删除依赖 core 的相关 UT，不调整依赖或运行实现，无独立 D。
modules:
  - tests/unit_tests/agentserver
files:
  - tests/unit_tests/agentserver/test_ttse_plugin_runtime.py
---

# 报告定位与处理

用户提供 `unit_test_report (3).html`，结果为 33 failed、20018 passed、100 skipped、0 errors。一个失败来自 Front 旧启动链，当前工作区 PR 9317 检视修复已删除该链，见 B_20261031_1042_pr9317-review。

其余 32 个失败对应插件运行测试的 23 个函数。仓库 HEAD 锁定官方 core `e089198835426cd7681761b0cc278670cd83031f`，文件 Rail 直接调用构造器，没有 `from_plugin_context` 分支，导致 TTSE 缺少 llm/model 参数、其他运行插件没有登记 resources。本机安装的是 fork `0c096e204d8e54605697de3b1d43c4be96b45828`，具备该能力；之前本地通过不能证明官方依赖可运行该链。

从本地 core Git 对象导出官方锁定提交到 `.git/ut-ci-core`，通过 PYTHONPATH 隔离加载，未修改 .venv。删除前运行插件测试和报告中的 Front 用例，得到 **32 failed、20 passed，41.58 秒**，与报告插件失败一致。

## 用户决定与范围

用户要求“暂时先把这些依赖core的ut删除”。因此删除上述 23 个函数，覆盖 32 个报告用例；另删除 `test_init_failure_unwinds_consult_prompt_and_candidate_demand`，该用例也依赖工厂，但旧 core 提前构造失败即可使其通过，无法证明执行到目标 init 失败。合计删除 24 个函数、33 个参数化用例，同时清理失去用途的导入和 fixture 返回项。

保留预检、轨迹租约、团队/作用域拒绝、普通零参插件及关闭任务重试测试。生产实现、core 依赖及锁文件不作本次变更，不通过改造 core 或替换依赖消除测试失败。同步 S_02/S_03 的当前验证范围，保留历史测试事实。

## 验证与恢复条件

Windows Python 3.13.12，PYTHONUTF8=1；PYTHONPATH 按顺序为 `.git/ut-ci-core`、本仓、既有 `.git/ttse-ut-verify-deps` 测试依赖。官方 core 隔离回归 **87 passed，16.13 秒**；未使用本地 fork 替代锁定 core。命令如下：

```powershell
.venv/Scripts/python.exe -m pytest -o addopts='' -o log_cli=false -o asyncio_mode=auto -q -n 2 --tb=short --show-capture=no tests/unit_tests/agentserver/test_ttse_plugin_runtime.py tests/unit_tests/test_app_agentserver.py tests/unit_tests/server/extensions/test_ttse_plugin_package.py tests/unit_tests/agentserver/test_front_phase1.py tests/unit_tests/agentserver/test_runtime_worker_phase2.py::test_front_and_supervisor_import_do_not_load_runtime tests/unit_tests/agentserver/test_worker_required_preparation.py tests/unit_tests/agentserver/test_worker_runtime_ownership.py
```

范围包括保留的插件 runtime、Front phase1、app_agentserver、ZIP 打包/导入、Front/Supervisor 冷导入、Worker 准备与职责。未重跑报告中的两万余项全量 UT，Linux Python 3.11 CI 仍需重新执行。

完整插件挂载、模型/span、共享会话、并存、超时关闭和升级回滚暂失 UT 覆盖；删除测试不修复官方 core 的能力缺口。待配套 core 能力进入官方锁定依赖后，从本次变更前 Git 版本恢复删除用例并重新验证完整链路。未运行静态检查或格式化，未提交、推送。
