---
id: B_20261127_1021_rea-offline-cancel-recovery
title: REA 断网取消后卡死且重装不可恢复
type: B
author: zhongxiaotian
author_email: zhongxiaotian@huawei.com
modules:
  - jiuwenswarm/channels/web/frontend/src/features/reaSetup
  - jiuwenswarm/common
  - jiuwenswarm/gateway/channel_manager/web
files:
  - jiuwenswarm/channels/web/frontend/src/features/reaSetup/ReaSetupPage.tsx
  - jiuwenswarm/channels/web/frontend/src/features/reaSetup/reaSetupProgress.ts
  - jiuwenswarm/common/rea_setup.py
  - jiuwenswarm/gateway/channel_manager/web/rea_setup_handlers.py
  - tests/unit_tests/common/test_rea_setup.py
  - tests/unit_tests/gateway/test_rea_setup_handlers.py
  - tests/reaSetupProgress.test.mjs
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
designs: []
design_note: 修复现有 REA 安装作业的前端恢复语义，不改变后端安装协议或状态机。
---

# REA 断网取消后卡死且重装不可恢复

## 现象与影响

Issue #7180 中，用户在断网时取消一键安装或增强能力安装，页面停在“正在取消安装”。恢复网络后继续操作会显示 `REA install job not found`；卸载应用、删除 `.jiuwenswarm` 并重装后仍无法恢复。

取消增强安装还会把已验证的 REA MCP 误显示为“未安装”：组件环境写入状态文件时无条件把 `connected` 改成 `registered`，取消路径不会执行最终复验恢复状态。

影响会跨越应用重装，因为活动作业 ID 保存在 Web 前端 `localStorage`，不位于 `.jiuwenswarm` 数据目录。用户当前安装的 `0.2.10` Windows EXE 可通过新 EXE 修复，无需手工清理浏览器存储。

## 关联设计与当前契约

后端 REA 作业由 Gateway 进程内 `_JOBS` 管理，不承诺跨进程恢复。前端 `reaSetup.activeJob.v2` 只用于页面导航恢复，不能把它当作可跨后端重启永久有效的任务事实。

当前安装作业只有在 `queued` 或 `running` 时持久化；后端明确返回 `REA_SETUP_JOB_NOT_FOUND` 后，该前端记录已经失效，必须清除并重新扫描环境。

## 根因与修复决策

原前端轮询只允许连续 3 次请求失败，断网时间稍长便永久退出。用户点击取消后，代码只保存后端返回的“正在取消安装”快照，没有继续轮询到 `cancelled`；如果轮询线程已经退出，页面便会永久停在中间态。

卸载或重装不会清理 Web 前端存储。新后端进程没有旧 `job_id`，重启后的页面仍从 `localStorage` 恢复旧作业并调用 progress，最终只显示 job not found，且旧记录没有被删除。

增强组件在下载或复用成功后会调用 `_persist_rea_env()`。该函数原来在合并组件环境时无条件把 MCP 状态写成 `registered`，而扫描只把 `connected` 当成可用；取消发生在组件环境已经写入、最终验证尚未执行之间，就会把基础环境错误判成未安装。

继续安装后立即取消时，增强组件的环境写入与状态页 MCP 复验会并发更新同一个
`agent/workspace/mcp/state.json`。原子写入原先把固定路径 `state.tmp` 作为临时文件，
Windows 上其中一个写入先完成替换后，另一个写入可能因临时文件被占用或已不存在而失败，
最终把 `[WinError 5] 拒绝访问` 呈现为安装错误。现场可从残留的 `state.tmp` 看到尚未合并的
`JAVA_HOME`，而 `state.json` 仍是旧状态。

修复采用三条规则：

- 对断网、连接未就绪、超时等瞬时错误保持轮询，网络恢复后继续收敛真实后端状态。
- 取消按钮提交一次后显示“正在取消”，取消请求遇到瞬时错误会继续重试，直到后端返回终态。
- 收到 `REA_SETUP_JOB_NOT_FOUND` 时删除陈旧活动作业和扫描缓存，重新执行环境检测。
- 取消终态使用独立的中性提示，不再写入通用错误状态，也不会显示红色“安装遇到问题”告警。
- 组件环境或辅助配置写入不再隐式改变 MCP 生命周期状态。
- MCP 实际复验返回空工具或异常时显式降级为 `registered`，状态页随后重新扫描，保留失败闭锁。
- MCP 状态的读改写使用跨线程、跨进程文件锁，避免增强安装与 MCP 复验互相覆盖。
- 每次写入使用唯一同目录临时文件，并对 Windows 短暂拒绝访问进行有限退避重试；最终失败时清理临时文件。
- 基础安装期间增强卡片展示缓存；基础安装完成后执行增强扫描并整体更新增强区，不再在安装步骤中强制显示“安装中”。
- 增强安装按组件结果增量更新：某个组件完成后立即更新该项，其他组件保持原状态。
- 显式检测只更新被检测的范围；配置保存后的后端校验与重扫会更新能力状态；显式检测或配置完成后清除旧作业的临时结果，避免覆盖新快照。
- “需配置”和“当前不适用”继续完全由现有组件 manifest 决定；扫描发现文件或缓存但 REA 不能直接调用时都归为“未安装”。
- 初始扫描和无缓存基础扫描可以返回 manifest 计划，但不会提前执行增强探针；基础完成后才扫描增强能力。
- 初始化扫描改为单次稳定触发，检测按钮进入单一 busy 状态，等待请求完成后结束，避免 job 轮询导致反复刷新提示。
- “未安装”和“当前不适用”复用同一灰色状态图标，文字语义仍分别保留。

## 拒绝的方案

- 不把作业恢复为 Gateway 持久任务：用户删除 `.jiuwenswarm` 后安装产物和运行上下文已不存在，强行恢复旧作业会产生伪成功。
- 不在断网时只在前端伪造 `cancelled`：后端下载线程可能仍继续运行，会制造状态不一致。
- 不对所有请求错误都清空作业：普通服务异常仍应保留可重试记录，只有后端明确报告作业不存在时才清理。
- 不要求用户执行开发者工具清理 `localStorage`：应用重装后必须自行恢复。
- 不把 `registered` 放宽为可用：状态降级必须只由真实 MCP 验证失败触发。

## 回归证据与适用范围

在 Windows、Node.js 18.19.0 环境执行：

```text
esbuild src/features/reaSetup/reaSetupProgress.ts --bundle --platform=node --format=esm --outfile=node_modules/.cache/rea-setup-progress/reaSetupProgress.mjs
node --test ../../../../tests/reaSetupProgress.test.mjs
17 passed

pytest tests/unit_tests/common/test_rea_setup.py tests/unit_tests/gateway/test_rea_setup_handlers.py tests/unit_tests/extensions/test_rea_setup_extension.py -q --no-cov -k "persists_env_before_later_cancellation or concurrent_env_updates or state_write or configure_component_writes_validated_json_and_env or verify_rea_mcp_downgrades or base_status_rescans or base_status_scope or cancel_handler or scan_accepts_connected_npx_record_without_npx_cli or scan_requires_base_rea_mcp or install_writes_pinned_mcp_record or upgrade_backup_preserves_existing_provider_env"
14 passed, 63 deselected
```

测试覆盖瞬时断网后继续轮询、作业不存在时清理、取消请求断网重试、取消时作业不存在、基础检测保留增强缓存快照、检测到但不可直接调用时归为未安装，以及原有进度估算和持久化行为；Python 用例覆盖取消后保持 `connected`、配置写入保持状态、复验失败降级、状态页重扫、基础未就绪时只返回 manifest 计划而不执行增强探针、并发环境写入不丢键、Windows 拒绝访问重试和原子写临时文件清理。另使用 esbuild 成功编译 `ReaSetupPage.tsx`，并校验中英文语言 JSON。

在隔离的 `clean-v2` 实例上通过真实 Web RPC 启动增强能力作业，3 秒后取消：作业最终为 `cancelled`、`error` 为空，`state.json` 保持 REA MCP `connected` / 229 tools，状态目录没有残留固定 `state.tmp`，日志未再出现 `WinError 5`。

未在真实 Windows EXE 上执行断网、重启后端及卸载重装端到端流程；该场景依赖同一 `localStorage` 和 RPC 分支，需在新构建上复验。

## 防错模式与资料同步

前端持久化的后台作业不是跨进程事实源。后端明确表示作业已不存在时，消费方必须丢弃恢复记录并从实际环境重新建立状态；网络类错误则保留作业并继续等待。配置写入不得隐式重写表示“已验证连接”的生命周期状态，连接状态只应由实际验证结果变更。多个安装或验证路径共享 JSON 状态时，读取、修改、原子替换必须处于同一锁内，临时文件名不得在所有写入者之间复用。

## 遗留与后续

当前后端作业本身不跨 Gateway 重启恢复，这是既有边界。若未来需要跨进程任务恢复，应另行设计持久任务与幂等执行，不能复用本故障的前端清理逻辑。
