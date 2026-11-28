---
id: B_20261126_1055_file-size-unit-rounding
title: 文件大小在单位边界显示为 1024K/1024M
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules:
  - jiuwenswarm/channels/web/frontend/src/features/groupChat
  - jiuwenswarm/channels/web/frontend/src/components/AgentManagementPanel
  - jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket
files:
  - jiuwenswarm/channels/web/frontend/src/features/groupChat/state.ts
  - jiuwenswarm/channels/web/frontend/src/components/AgentManagementPanel/AgentUploadDialog.tsx
  - jiuwenswarm/channels/web/frontend/src/components/AgentManagementPanel/AgentGroupUploadDialog.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/UploadFileCreateModal.tsx
  - jiuwenswarm/channels/web/frontend/tests/groupChat.test.mjs
designs: []
design_note: 前端展示层单位进位缺陷，无独立设计方案，按 Issue #7060 处理。
---

# 文件大小在单位边界显示为 1024K/1024M

## 现象与影响

文件大小展示在单位边界给出不可能出现的读数：

- `1048575` 字节显示为 `1024K`（应显示 `1M`）
- `1073741823` 字节显示为 `1024M`（应显示 `1G`）
- 上传弹窗类实现显示 `1024.0 KB`，`UploadFileCreateModal` 在 1TB 附近还会取到 `undefined` 单位

用户看到的文件大小自相矛盾，也会让「1024K」这类读数被误读为超过 1M。

## 根因

四处实现共同的判断顺序错误：先按两位小数 `toFixed` 舍入，再判断是否 `>= 1024` 进位。
`1048575 / 1024 = 1023.999…` 未达 1024，不做进位，但舍入后显示为 `1024K`。

`UploadFileCreateModal` 另有下标越界：单位表只有 `B/KB/MB/GB`，`Math.floor(log(bytes)/log(1024))`
对 ≥1TB 返回 4，`units[4]` 为 `undefined`。

## 修复决策

- 所有实现统一改为「先取整、再判断进位」：舍入结果达到 1024 才进入下一单位
- `UploadFileCreateModal` 补单位下标上限，超出最大单位时停在 GB，不再输出 `undefined`
- 保留四处各自的精度与单位风格（`K/M/G` 两位小数 vs `KB/MB/GB` 一位小数），不在本 PR 内做实现收敛，
  避免把展示格式调整混进缺陷修复
- 顺带给出各实现共用的边界断言：`1024*1024-1 → 1M`、`1024*1024*1024-1 → 1G`

## 回归与验证

- `npm run test:group-chat`：43 passed，新增两条边界断言
- 构建后实测：`1048575 → 1M`、`1073741823 → 1G`；上传弹窗类 `1048575 → 1.0 MB`

## 遗留与限制

- 四处 `formatFileSize` 仍是各自私有实现，后续单位展示调整需四处同步，建议后续单独提 PR 收敛到共享 util
- 单位表最大到 GB，1TB 及以上仍显示 `1024.0 GB`；如需精确到 TB，扩展 `units` 即可，属展示精度问题而非进位错误
