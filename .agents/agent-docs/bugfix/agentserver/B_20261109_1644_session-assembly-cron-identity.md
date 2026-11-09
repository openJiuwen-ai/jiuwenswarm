---
id: B_20261109_1644_session-assembly-cron-identity
title: 启动性能采样前修复会话装配的 Cron 归属依赖遗漏
type: B
author: 刘瑜
author_email: liuyu524@huawei.com
modules:
  - jiuwenswarm/server/runtime/agent_adapter
files:
  - jiuwenswarm/server/runtime/agent_adapter/session_assembler.py
  - jiuwenswarm/server/runtime/agent_adapter/session_identity.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
  - tests/unit_tests/agentserver/test_adapter_execution_services.py
  - tests/unit_tests/agentserver/test_agent_reload_scope.py
designs: []
design_note: 无独立新设计；恢复集成 develop 9f4ddcede 的会话归属语义及现有执行宿主拆分边界。
---

# 会话装配 Cron 归属依赖遗漏

## 现象与影响

在 `b640ec3cfc3a3d414a423fd3364e36606ccdddc5` 加原有本地修改上开展 Source/WHL 默认 Agent 体验采样。
Source 优化候选冒烟在真实界面进入首聊和 `command.goal` 时抛出
`NameError: name '_resolve_cron_session_identity' is not defined`，没有主模型完整回复。
正式采样尚未开始；失败样本、原候选源码清单和 WHL 均保留在
`D:/work_space/jiuwenswarm-source-whl-ux-20261109/`，不能用该失败候选宣称提速。

## 关联设计与当前契约

会话装配继续由 SessionAssembler 所有，不反向依赖沉重的 interface_deep 模块。
Cron 路由携带经过认证请求的用户，非输入创建时回退会话元数据；最终归属仍由 Gateway 校验。
本次不修改公共 RPC、SDK、锁定依赖、后台预热预算或用户数据。

## 根因与修复决策

- 装配方法提取后保留了未绑定的接口模块函数名；将轻量身份解析移至 session_identity，并由装配器接收解析依赖。原接口符号保留兼容别名。
- 当前 Cron 上下文类缺少 develop 的 bind_session 和非空回退语义；恢复这些行为，防止空 contextvars 擦除已绑定用户。
- 补齐装配器的 conversation_app 与 permission continuation 依赖，并移除会把后者遮蔽为局部变量的条件导入。
- 关闭部分初始化的适配器时，MoA Runtime 可以尚未存在，保持幂等清理，不提前释放仍被 SDK 使用的模型租约。
- 相关模型授权测试的请求替身补齐 AgentRequest 已有的 metadata 字段，不放宽真实请求校验。
- 后续真实 Source 冒烟发现 _PLUGIN_RUNTIME_PROFILE 丢失，检查并恢复同类 develop 默认字段：Agent/TTSE profile、MoA 未创建时状态和子任务进度批次锁。保留 Code profile 覆盖，不改变插件算法或默认启用策略。该失败及原制品保留于仓库外 r2/revisions。

## 回归证据与适用范围

使用锁定的 Python 3.11.15、OpenJiuwen 0.1.20 隔离环境，设置独立数据根。
以下用例合计 41 项通过，结果为仓库外 `fix-validation-02.xml` / `.log`：

- tests/unit_tests/agentserver/test_adapter_execution_services.py：包含新增的请求用户优先、会话用户回退、channel_metadata 回退三种实际装配路径。
- tests/unit_tests/agentserver/test_cron_route_identity.py：包括线程上下文丢失、固定归属与 Gateway 命令 ACK。
- tests/unit_tests/agentserver/permissions/test_permission_admission_edges.py：包括清理失败后资源归属及重建。

重建后 Source/安装 WHL 两版共四类冒烟全部通过，确认实际界面到真实 Qwen3.8-Flash 的完整回复。
正式计时结果不能仅由上述定向用例替代，见后续记录。
初次失败的回归日志 fix-validation.xml 也保留，不覆盖失败事实。

扩大验证后另有 87 项通过，结果为 fix-regression-02.xml / .log，覆盖 Cron Rail、权限 Rail、Agent 重载范围及接口端口。
其中两项旧重载测试替换真实编码器后仍要求解析真实协议，修正为使用实际编码器；另一个配置替身改为当前 handler 真正读取的 common.config，保留原行为断言。
这些是测试隔离方式随调用边界变化的调整，没有修改重载配置的产品逻辑。

插件/TTSE/MoA 专项 70 项通过，含两个新增真实插件宿主 profile 快照用例，覆盖 Agent 的 work 与 Code 的 code 区分。
结果为 fix-native-profile-02.xml / .log；与前述组存在用例重叠，不直接累计为唯一通过数量。

## 防错模式与资料同步

已有单次装配/关闭用例使用没有 Cron 工具上下文的简化子适配器，未触及真实实例必经的归属绑定。
新增装配回归使用真实 Cron 上下文，并在 SDK 实例创建前观察已绑定的会话和用户，覆盖提取组件的真实依赖。
本次没有新增公共契约或独立框架；原性能结论仍按候选、依赖和真实链路分别重验。

## 遗留与后续

修正采样顺序后完成 Source/WHL × 基线/候选 × 立即发送/输入 5 秒，每组 30 次、共 240 次正式尝试。
结果索引为 `D:/work_space/jiuwenswarm-source-whl-ux-20261109-balanced/acceptance-index.json`，报告同目录。
前一轮 112 条固定版本顺序的探索数据整体保留但不计入主结论；GBK 控制台异常发生前已落盘的记录补回索引，没有重跑替换。

237 条确认完整可见回复，原 NameError 和缺失 profile 未再复现，但不能据此宣布完整验收通过：

- Source 候选第 4 轮立即发送、第 29 轮输入 5 秒各一次 Worker 必要准备未完成；IPC 已监听，日志止于 model_migrations，均无模型请求，缺少当时线程栈，具体根因仍未确定。
- 基线 WHL 一次收到 Markdown 长回复，按原始正文匹配 DOM 的采样器超时；截图证实回复已显示，但完整可见时刻未可靠测得。它属于测量确认缺失，不能计作基线产品启动故障。

Source 两条启动停顿仍需独立定位，不能归入云端模型波动，也不能用成功样本更快的平均值抵消。
已校验正式 240 个唯一尝试与预定顺序一致，各条件 15 AB/15 BA 交替，4,080 条拥有的进程身份校验无残留，制品、源码和 SDK/Gateway 文件哈希校验通过。
冻结候选与当前工作区 3,045 个产品文件无差异。产品代码本轮未提交或推送。
首次 Source 基线冒烟的 Gateway 渠道配置长等待仍保留原始日志，与保留 OS 缓存的正式结果分开呈现。

## PR 提交前复查

提交前定向回归共 228 项通过，覆盖会话装配、Cron 归属、重载、配置优先级、Worker 启动许可与预热、前台竞争、配置缓存和副本隔离。控制面旧用例仍断言会话优先的历史顺序，已同步为配置优先，并保留等待前台控制请求结束才预热的断言。

本地 PyInstaller 配置中残留的 `gateway/channel_manager/__init__.py` 读取已移除；Gateway 仍由既有 `collect_submodules("agent_gateway")` 收集，不恢复已拆出的源码目录。本次尚未重新构建 Frozen/Electron EXE，不能据此宣称其验收通过。

前端使用与 lockfile 一致的已有完整依赖安装重新构建，Vite 生产构建通过；配置状态、模型草稿、连接重试、Runtime ACK 和中英文资源用例通过。模型推理和设置用例中原基线同样存在的 6 项失败继续保留。证据位于仓库外 `D:/work_space/.codex-temp/startup-pr-delivery-20261110/`；完整单测、远端 CodeCheck/CI 和上述两条 Source 停顿仍未获得通过结论。
