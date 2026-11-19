---
id: B_20261117_1050_adapter-ut-fixtures
title: 图片降级提示与付费搜索 UT 的测试替身和断言漂移
type: B
author: TwZhang
author_email: zhangtangwei@huawei.com
modules:
  - tests/unit_tests/agentserver
files:
  - tests/unit_tests/agentserver/test_image_fallback_notice_nonstreaming.py
  - tests/unit_tests/agentserver/test_paid_search_configuration.py
designs: []
design_note: 根据用户提供的 UT 报告修正测试替身及副作用断言，无独立设计。
---

# 图片降级提示与付费搜索 UT 的测试替身和断言漂移

## 现象与影响

用户提供的 `unit_test_report (1).html` 对应候选
`67755e9d49f9b4c22abc680973bc6313588971ee`，运行环境为 Linux、Python 3.11.5、pytest 9.0.3。
报告统计为 24532 passed、9 failed、224 skipped。

- `test_image_fallback_notice_nonstreaming.py` 的 8 项均在输入派发前抛出 `HISTORY_RESTORE_CHANGED`，
  导致图片提示、正常文本、空回复及错误回复断言未执行。
- `test_paid_search_configuration.py` 的现有 MCP agent 刷新用例观察到全局工具注册调用 5 次，
  与预期总次数 2 不符。

## 当前契约与根因

图片提示用例通过真实 `process_message_impl` 入口收集模拟执行流。
其运行时替身使用无规格的 `MagicMock`，访问 `react_agent.context_engine.get_context()` 时会生成模拟上下文，
后续 `duplex_history_cursor` 同样成为模拟对象。严格历史读取器按真实游标契约校验这些属性，
在正式消息执行前拒绝该游标。

付费搜索用例要验证 Bocha 启用、切换 Serper、禁用后的模型工具列表与付费工具注册。
原断言统计 `Runner.resource_mgr.add_tool` 的全部调用，统计范围包含同一进程内其他工具注册。
报告证实多出三次调用，本地单独执行该用例时原总次数断言通过，无法仅凭报告确定额外调用的工具来源。

## 修复决策

- 将图片运行时替身的 `react_agent` 显式设为 `None`，准确表达仅提供输出流的运行时。
  真实输入派发入口继续执行，其无 ReAct 上下文路径正常返回。
- 从注册调用记录中筛选参数对象为唯一 `mcp_paid_search` 的调用，精确断言两次注册均使用
  `skip_if_exists=True`。继续验证模型看到的 provider 枚举和禁用后的工具移除。
- 搜索用例增加普通及额外工具注册两个参数场景。
  第二个场景在三次工具列表读取时注册无关工具，覆盖原报告中总调用数从 2 增为 5 的形态。

曾考虑捕获历史读取异常，但这会削弱真实会话恢复的游标校验。
曾考虑把工具注册总次数改为 5 或下限断言，但真实付费工具漏注册或重复注册仍需要被精确检测。

## 验证证据与范围

本地使用 Windows、Python 3.12.13、pytest 9.0.3。
初次收集发现 `.venv` 缺少 `e2a`，随后执行 `uv sync --frozen` 按现有锁定依赖同步环境。
安装的 openjiuwen 对应锁定提交 `2f4bedb9c5d85ab70d7c79d36183e6a98799e94e`。

运行前激活 `.venv` 并设置 `PYTHONPATH`。
所有验证使用 `uv run --no-sync pytest`，通过 `-o addopts=` 关闭默认覆盖率产物，
使用 `-o log_cli=false --tb=short -q` 控制输出。

修复前两个报告失败文件本地运行结果为 8 failed、26 passed，图片提示的八种失败全部复现。
修复后两个文件运行结果为 34 passed。补充额外工具注册参数场景后，相关六个文件共 113 passed。

```text
tests/unit_tests/agentserver/test_image_fallback_notice_nonstreaming.py
tests/unit_tests/agentserver/test_image_fallback_notice_language.py
tests/unit_tests/agentserver/test_image_input_status.py
tests/unit_tests/agentserver/test_paid_search_configuration.py
tests/unit_tests/agentserver/permissions/test_trusted_search_tool_adapter.py
tests/unit_tests/test_context_snapshots.py
```

额外扩展到 `test_history_replay.py`、`test_history_review_regressions.py` 时，
第一次合并运行结果为 165 passed、3 failed，单独重验这两个文件为 51 passed、5 failed。
共同错误来自 `history_replay._read_prefix` 的文件身份校验 `HISTORY_RESTORE_CHANGED`，
失败项随运行变化。上述两个文件及其生产实现均为原候选内容，本轮未修改。
因此 113 项通过仅归属于上面六个已完成回归的文件，完整 Linux CI 尚待重跑。

## 2026-11-17 合并 develop 冲突修订

将 upstream/develop `2cce9d32f8847139dc2aac86c77ccc2cf8ec3ef7` 合并到修复分支。
该版本已包含相同 UT 故障的独立修复，付费搜索注册断言出现文本冲突。

合并时保留 upstream 清理免费搜索环境开关的 fixture，并保留本分支的
`mcp_paid_search` 精确调用参数校验和额外工具注册参数场景。
图片提示用例保留 upstream 对上下文同步的 mock，以及运行时替身显式声明 `react_agent=None`。

合并候选执行 `uv sync --frozen`，随后重跑上述六个 Python 文件，结果为 113 passed。
CLI 授权、host OAuth、扩展依赖连接、卸载守卫及装备广场五组前端回归共 33 项通过。
本轮验证覆盖冲突处的测试行为和本分支的授权生命周期修复。

### 再次同步 develop

后续同步 upstream/develop `4771ebb77e88c2a8082595d6bd061eb56e24938a` 时，
图片提示测试的 `react_agent=None` 修复已在 upstream 出现，冲突范围仅为其说明注释。
采用 upstream 对模拟游标触发恢复校验的说明，合并后的该测试文件与 upstream 完全一致。

合并候选的五组前端回归共 33 项通过。
六个 Python 文件首次执行为 112 passed、1 failed，失败用例为
`test_legacy_voice_ids_survive_compaction_and_restart`，错误为历史文件身份校验
`HISTORY_RESTORE_CHANGED`。该用例单独重跑通过，随后六个文件整体重跑为 113 passed。
该历史回放用例仍存在本地间歇性失败证据，完整 CI 结果需要独立确认。

## 防错模式与资料同步

### 同步付费搜索隔离回归

同步 upstream/develop `93fe339c474be35c178a2f9a807a916ed9322f59` 时，
付费搜索用例新增免费搜索开关参数化，并将 Runner 替身限制在 `mcp_toolkits` 模块。
冲突合并采用该模块级替身及免费搜索可见性断言，组合本分支额外工具注册参数场景，
覆盖免费搜索开关和额外注册的四种组合，保留付费搜索对象的精确调用断言。
三个生成索引由文档同步工具重建。

合并候选执行 `uv sync --frozen` 后，六个 Python 回归文件共 115 项通过，
五组前端回归共 33 项通过。

运行时替身显式声明可选子系统是否存在，避免 `MagicMock` 自动属性被当作真实生命周期状态。
全局副作用断言按被测资源身份筛选，同时保留精确调用参数和次数。
本次规则随测试注释及故障记录维护，文档索引由同步工具生成。
