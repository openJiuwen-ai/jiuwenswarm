---
id: B_20261128_1430_webui-filetree-stale-receipt
title: WebUI FileTree 帧先于回执到达导致 stale 误报与状态机加固
type: B
author: kusanagima
author_email: kusanagima@foxmail.com
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/features/webui/contracts/interaction.ts
  - jiuwenswarm/channels/web/frontend/src/features/webui/runtime/createWebUIRuntime.ts
  - jiuwenswarm/channels/web/frontend/src/features/webui/renderer/adapters/webuiFileTree/FileTree.tsx
  - jiuwenswarm/channels/web/frontend/src/features/webui/renderer/adapters/webuiFileImport/WebUIFileImport.tsx
  - jiuwenswarm/channels/web/frontend/src/features/webui/renderer/adapters/webuiButton/WebUIButton.tsx
  - jiuwenswarm/channels/web/frontend/src/features/webui/docs/specs/S_01_components.md
  - jiuwenswarm/channels/web/frontend/tests/webuiEvents.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/webuiComponentsReact.test.mjs
designs: []
design_note: 基于现有 WebUI 组件交互与渲染帧契约的缺陷修复，无独立设计文档。
---

# WebUI FileTree 帧先于回执到达导致 stale 误报与状态机加固修复记录

## 现象与影响

Issue #7185 反馈：使用包含 `FileTree` 的系统插件（如 `probe.session-files`）点击文件预览或展开目录时，功能本身正常更新，但 `FileTree` 顶部持续出现红框错误提示 `webuiFiles.tree.error`（“目录操作失败，请重新展开目录或选择文件”）。

## 关联设计与当前契约

- 依据 `S_01_components.md` C-13/C-14/C-15：节点交互绑定关联 `localFrameId`，新渲染帧到达会使旧节点绑定失效；等待回执期间绑定失效时返回 `stale`。
- 本次同步更新 `S_01_components.md` C-15：明确返回 `stale` 时保留已收到的服务端回执 `receipt`，并将“本地帧代次推进”与“服务端是否接受”解耦。

## 根因与修复决策

1. **根因**：
   - 插件在事件回调中先调用 `ops.ui.render(...)` 推新帧再返回动作回执时，新帧常比回执早 1–2ms 到达前端。`createWebUIRuntime` 收到新帧后释放旧节点绑定并递增 `localFrameId`，导致随后返回的 `dispatch` 回执被丢弃并返回 `{ status: 'stale' }`。
   - `FileTree.tsx` 的 `request()` 对 `stale` 仅跳过了 `onRejected()` 状态回滚，却在外层 `if` 漏掉了对 `stale` 的豁免，无条件执行 `setError('error')`；在 `keep-mounted` 槽位下旧组件不卸载，导致报错持续存在。
   - 并发与异常边缘问题：`catch` 分支缺少 `onRejected()`；全局 `pending` 禁用整棵树；快速请求闪烁；新帧到达未清除旧瞬态 `error`；收起目录前缀匹配未归一化 Windows `\`。
2. **修复决策**：
   - **Runtime 保留回执 + 统一判定函数**：`createWebUIRuntime.ts` 在 `stale` 分支保留 `receipt`（`{ status: 'stale', receipt }`）；`contracts/interaction.ts` 新增 `isWebUIInteractionFailed()`，规定 `sent` 与 `stale` 均仅在明确收到 `receipt.accepted === true` 时视为成功，未知回执（`null`/缺失）或 `accepted === false` 一律视为失败。`FileTree`、`WebUIFileImport` 与 `WebUIButton` 统一复用该判定。
   - **FileTree 状态机优化**：移除全局 `disabled` 锁，目录展开按 `key` 去重并在新帧到达时保留仍在 `pendingExpandRef` 中的展开键；文件选择采用 `selectSeqRef` 实现 Last-Write-Wins；`SPINNER_DELAY_MS` 取 `150`ms 延迟展示，避免 20–30ms 快速文件预览操作产生加载图标闪烁；新 `treeData` 帧到达时自动清理瞬态 `error`；`catch` 分支补齐 `onRejected()`；路径前缀通过 `normalizePathPrefix` 统一处理 `\` 与尾部 `/`。

## 回归证据与适用范围

在 `jiuwenswarm/channels/web/frontend/` 执行以下校验并全部通过：
- `npm run test:webui-components`：覆盖 `stale` + `accepted: true` 不报错不回滚、`stale` + `null` 未知回执判失败、Last-Write-Wins 文件选择抢占、抛错回滚、新帧清除瞬态错误、并发展开增量回滚以及并发期间注入中间帧保留待定展开键。
- `npm run test:webui-events`：校验 `stale` 结果携带完整 `receipt`。
- `npm run test:webui-runtime`、`npm run test:webui-renderer`、`npm run test:webui-contracts`、`npx tsc --noEmit`：全部通过。

## 防错模式与资料同步

- 对 SDUI 异步交互组件，`status === 'stale'` 与服务端是否接受动作属于正交维度，统一通过 `isWebUIInteractionFailed` 判定，避免各适配器各自手写条件造成漂移。
- 已同步 `S_01_components.md` C-15 与本 bugfix 归档文档。

## 遗留与后续

无。
