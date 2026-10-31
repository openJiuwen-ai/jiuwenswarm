---
id: B_20261031_0946_ttse-codecheck-boundaries
title: TTSE 相关 CodeCheck 受保护访问与关闭条件复杂度修复
type: B
author: xinyang_bali
author_email: 835397263@qq.com
designs: []
design_note: 按用户提供的 20 项 CodeCheck 清单修复实现边界，沿用当前 TTSE 通用运行契约，无独立 D。
modules:
  - jiuwenswarm
  - jiuwenswarm/server/runtime/agent_adapter
files:
  - jiuwenswarm/openjiuwen_fs_rwlock_patch.py
  - jiuwenswarm/server/runtime/agent_adapter/plugin_runtime.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - tests/unit_tests/test_openjiuwen_fs_rwlock_patch.py
  - tests/unit_tests/agentserver/test_ttse_plugin_runtime.py
  - scripts/package_ttse_plugin.py
  - tests/unit_tests/server/extensions/test_ttse_plugin_package.py
---

# TTSE CodeCheck 边界修复

## 现象与根因

用户附件列出 20 项：文件锁兼容补丁中的 17 项外部受保护成员访问，插件运行时的 1 项过多布尔表达式及 2 项适配器受保护字段访问。原补丁将外部函数绑定到 core 类，却直接访问 core 的内部锁、条件变量与计数；插件槽位直接访问适配器实例和插件登记表，跨越所有权边界。

CodeCheck 看板 URL 通过当前 web 工具不可访问，本次以用户粘贴清单为准。未运行格式化或静态检查，不声明远端报告已关闭。

## 修复与不变量

- 文件锁仅包装 core 的公开 read/write/close，借公开 file_lock 调用 acquire_read/acquire_write/release。新增组合式状态对象拥有自身条件变量、读者计数、写入状态及等待计数，不读写 core 受保护成员，也不通过 getattr 绕过私有字段访问。
- 每个读者在自身 asyncio Task 中获取并释放文件锁；writer 与本地 readers 互斥，等待 writer 时阻止新 reader。活动锁拒绝关闭，关闭后拒绝新请求。WeakKeyDictionary 不强持有原 lock；reset 恢复三个公开方法并清理兼容状态。worker 启动时安装顺序保持原契约。
- 适配器公开 get_plugin_runtime_host/get_plugin_runtime_manager。前者提供当前实例、登记映射、模型二元组、工作区、配置和 fresh-turn 回调；后者由适配器管理单实例。runtime 不直接读取适配器受保护字段。快照不缓存，保留模型热更新；登记映射保留引用，保持装卸同步；repr 不包含敏感配置。
- 槽位操作及事务锁通过显式公开接口协作，不跨类调用受保护方法。
- closer 判定分解为 _needs_close_attempt：不存在时启动；运行中或已取消不重启；仅已结束且异常为 TimeoutError 时重新尝试。其他完成结果和异常继续由 task.result 传播，不取消任务冒充写入者退出。

未选择删除锁补丁：当前宿主仍保留 filelock 4.x 的兼容需求，不能依赖用户未提交的版本限制代替正确实现。未选择子类直接访问父类受保护成员、改用字符串反射或 lint suppression，因为都不能建立明确边界。未修改 core、配置、依赖声明/锁文件，也未更新安装插件或重启服务。

## 行为验证

现有 .venv 的 filelock 为 3.32.7，PYTHONUTF8=1，PYTHONPATH 包含本仓、本地 ../agent-core 和既有隔离协议依赖目录。

- 相关回归 **88 passed**：锁补丁、TTSE runtime、worker 可选补丁、code TTSE 挂载、会话关闭及 ZIP 导入。新增/更新用例覆盖并发 reader、取消 reader 后其他 reader 仍持锁、外部文件争用超时及重试、活动期间 close、关闭后拒绝获取、reset、已完成超时 closer 重试及取消 closer 不重复启动；既有慢 closer 不被取消/重复、失败回滚、模型替换等测试继续通过。
- 首轮有 **12 passed, 47 setup errors**：当前仓库排除了 TTSE 资源目录，manifest.json 不存在，真实 ZIP fixture 无法运行；不是行为断言失败。为验证当前修复，使用已安装 TTSE 2.0.0 的临时副本，临时定位打包器的 source 根后执行同一批行为测试。未恢复、跟踪或改写用户排除的资源文件。上述 88 项结果以此夹具条件为限，不代表缺少资源时原始测试命令可直接成功。
- 另将 filelock 4.0.2 无依赖安装到 .git/codecheck-filelock4，仅通过该次测试 PYTHONPATH 使用；同一文件锁测试 **7 passed**。未改变 .venv 依赖或用户 pyproject/uv.lock。

## 交接限制

需要远端重新执行 CodeCheck 才能确认报告关闭。普通开发环境若要直接执行依赖真实 TTSE ZIP 的测试，须另行提供被排除的插件源码/制品。未验证真实外部模型任务，未提交或推送。

## 后续附件六项修复（2026-10-31）

用户新增附件列出 G.FMT.02 三项、G.CTL.03 一项、G.CLS.08 一项和 G.LOG.02 一项，以当前工作区为准逐项核对：

- plugin_runtime.py 的恢复失败异常、快照 manifest 校验及 PluginBinding 返回表达式改为多行，异常文本、校验与返回参数不变。
- 七操作数条件已由 PR 9317 检视修复拆成四组顺序校验，本次确认沿用。
- interface_deep.py 在适配器构造函数显式初始化 `_plugin_runtime_manager: PluginRuntimeManager | None = None`，保留首次使用时构造及单实例复用。
- package_ttse_plugin.py 的 CLI 使用 logging INFO 输出制品路径；只在直接运行入口配置日志，导入打包函数不改全局日志配置。路径现在输出至日志流 stderr，ZIP 内容与打包函数返回值不变。

本轮相关回归 **28 passed，15.99 秒**：test_ttse_plugin_runtime.py、test_code_adapter_ttse_rail.py、server/extensions/test_ttse_plugin_package.py，均位于 tests/unit_tests。Windows Python 3.13.12，PYTHONUTF8=1，PYTHONPATH 依次为从 Git 导出的官方锁定 core e089198835426cd7681761b0cc278670cd83031f、本仓、既有 .git/ttse-ut-verify-deps；pytest 使用 `-o addopts='' -o log_cli=false -o asyncio_mode=auto -q -n 2 --tb=short --show-capture=no`。

额外通过子进程实际执行打包 CLI，以 tests/fixtures/ttse_plugin 为 source、临时目录为 output，确认退出码为零、stderr 含输出路径，并独立打开 ZIP 检查 manifest.json。该临时制品自动清理。

未运行格式化或静态检查，未提交/推送；六项源码修复不代表远端报告已经关闭。此前关于生产资源缺失的限制已由后续版本化测试夹具解决，当前新版 core 工厂相关集成覆盖缺口见 B_20261031_1048_ttse-core-dependent-ut，不以本轮 28 项通过替代该链验证。
