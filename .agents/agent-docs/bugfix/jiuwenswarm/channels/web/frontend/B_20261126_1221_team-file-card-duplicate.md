---
id: B_20261126_1221_team-file-card-duplicate
title: 团队模式 chat.file 二次投递使文件卡片重复渲染为两张
type: B
author: chen hui
author_email: chenhui280@h-partners.com
designs: []
design_note: 事件时序契约缺陷修复，无独立 D；依据前端 team-leader 消息按 teamStream.requestId 关联的既有契约与各事件处理器的去重守卫约定。
modules:
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/channels/web/frontend/src/hooks/useWebSocket.ts
  - jiuwenswarm/channels/web/frontend/src/features/teamLeaderMessages.ts
  - jiuwenswarm/channels/web/frontend/src/utils/wsEventDedup.ts
  - jiuwenswarm/channels/web/frontend/src/utils/fileDownloadDedup.ts
  - jiuwenswarm/channels/web/frontend/tests/teamLeaderMessages.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/wsEventDedup.test.mjs
  - jiuwenswarm/channels/web/frontend/package.json
---

# 团队模式 chat.file 二次投递使文件卡片重复渲染为两张

## 现象与影响

团队（集群）模式下，同一 `chat.file` 事件二次到达时，文件卡片会被渲染为两张：
一张挂在原 team-leader 消息的 `fileItems` 上，另一张由 `addMessage` 新建一条
带 `fileItems` 的 team-leader 消息产生。单 Agent 模式与 F5 刷新路径不受影响，
因为单 Agent 走 `findActiveTeamLeaderMessage` 命中分支合并、F5 走历史重建不走
实时事件。受影响的是 team relay / fanout 延迟投递的二次 `chat.file`，在
chat.final 已经收束该轮之后才到达的场景。外部 issue：
https://gitcode.com/ZYQ5333/jiuwenclaw/issues/105。

## 关联设计与当前契约

无独立 D。依据的当前契约：

- team-leader 消息通过 `teamStream.requestId` 关联到当前用户轮；
  `findActiveTeamLeaderMessage(messages, requestId)` 在当前轮内按 requestId 定位
  活跃的 team-leader 消息，用于把同轮增量（工具调用、文件等）合并到同一条消息。
- `chat.final` 会收束该轮 team-leader 消息：把 `teamStream` 清成 `undefined`，
  标记该轮已终态化，不再接受按 requestId 合并的增量。
- 事件处理器入口有统一去重守卫 `shouldDropDuplicatedEvent(kind, payload)`，
  按 payload 内的 request/event 标识在 1500ms 内挡住快速重复投递；
  `chat.tool_call` / `chat.tool_result` / `chat.final` / `team.*` 等处理器均已接入，
  `chat.file` 是历史遗漏的唯一缺口。

## 根因与修复决策

两个独立根因叠加，单修任一个都不足以覆盖全部二次投递路径：

**根因 1（事件级去重缺口）：** `chat.file` 处理器入口缺少
`shouldDropDuplicatedEvent('chat.file', payload)` 守卫，是各事件处理器中唯一未接入
统一去重的。1500ms 内的快速重复投递（同 requestId 二次到达）因此绕过去重，进入
后续 team-leader 定位逻辑。

**根因 2（终态后定位失效）：** `chat.final` 把 team-leader 消息的 `teamStream`
清成 `undefined` 后，`findActiveTeamLeaderMessage` 因依赖 `teamStream.requestId`
而命中不到这条已终态化的消息。此时若同一 `chat.file` 在 chat.final 之后延迟到达
（team relay / fanout 投递延迟可能超过 1500ms 去重窗口），team 分支会 fallthrough
到 `addMessage` 新建一条带 `fileItems` 的 team-leader 消息——同一文件落到两条
消息上，渲染两张卡片。

**修复决策（双层去重，互为兜底）：**

- 第一层（事件级）：`chat.file` 入口补
  `shouldDropDuplicatedEvent('chat.file', payload)`，与其它处理器对齐，挡住
  1500ms 内的快速重复投递。`makeEventDedupKey` 给 `chat.file` 的去重键追加文件身份
  （`buildChatFileIdentityKey`：`path` 优先，无 `path` 退回 `name+size`，与
  `getFileIdentityKey` 同口径），仅按 `rid:<request_id>` 去重会丢掉同一轮里先后到达
  的两个不同文件（同一 `send_file_to_user` 共用同一 request_id），追加文件身份后
  真重复投递（同 request_id 且同文件集合）仍去重，不同文件各自成键。
- 第二层（文件身份级）：team 分支 `findActiveTeamLeaderMessage` miss 时，新增
  `findTeamLeaderMessageWithFile(messages, incoming)` 兜底——在当前用户轮内按
  文件身份（`getFileIdentityKey`：`path` 优先，回退 `name+size`）查找已带该文件
  的 team-leader 消息并合并 `fileItems`，挡住 chat.final 之后超过去重窗口的延迟
  二次投递。仅在当前轮内查找（`findLatestUserIndex` 划界），不跨轮合并。

> 检视回归（R2 补丁）：初版第一层只按 `rid:<request_id>` 去重，同一轮里模型并行
> 调用两次 `send_file` 推送两个不同文件（共用同一 request_id）时第二个会被当成
> 重复投递丢弃，文件卡片丢失。修复为去重键追加文件身份（path 优先，回退
> name+size），同时补 `wsEventDedup` 单测覆盖「同 request_id 不同文件各自成键」与
> 「真重复投递仍去重」。无 path 时按 name+size 比对，同名同大小但内容不同的文件
> 会被判为同一身份合并成一张卡片，该场景概率极低，接受该取舍。

**备选与拒绝：**

- 「在 chat.final 不清 teamStream」被拒绝：`teamStream=undefined` 是该轮终态化的
  语义标记，被 `findActiveTeamLeaderMessage` 等多处依赖来判定"轮已结束"；保留它
  能避免把下一轮增量错误合并到上一轮已终态消息。
- 「把去重窗口拉长到覆盖 relay 延迟」被拒绝：relay / fanout 延迟无上界，拉长窗口
  会误杀合法的跨轮同名文件（不同轮生成同名 qa-red.txt 应各占一张卡片），且去重
  守卫的本意是挡"快速重复"，不是挡"延迟到达"。
- 「按 requestId 在所有 team-leader 消息里查」被拒绝：会跨轮合并，破坏每轮独立
  的 team-leader 消息语义；文件身份兜底严格限定在当前轮内。

## 回归证据与适用范围

修复提交 `2f9710343`（2026-11-27，分支 `fix/team-file-card-duplicate`）。

- 新增 `tests/teamLeaderMessages.test.mjs`（`npm run test:team-leader-messages`）：
  10 用例覆盖——`findTeamLeaderMessageWithFile` 按 path 命中、按 name+size 回退命中、
  无重叠返回 undefined、忽略无 fileItems 的 leader、仅当前轮内查找（跨轮不命中）、
  返回最新匹配、忽略非 team-leader 消息、空 incoming 返回 undefined；以及
  `findActiveTeamLeaderMessage` 的 requestId 匹配回归与 chat.final 清 teamStream
  后 miss 的回归（正作为兜底场景的入口断言）。10/10 通过。
- 新增 `tests/wsEventDedup.test.mjs` 的 `chat.file` 用例：同 request_id 不同文件
  各自成键（不被事件级去重误杀）、真重复投递仍去重、不同文件集合各自成键。与
  `teamLeaderMessages.test.mjs` 共同覆盖检视回归（同 request_id 不同文件两张卡片都
  渲染）。`wsEventDedup` 全套 16/16 通过。
- 源码锚定：`useWebSocket.ts` 中 `chat.file` 入口 `shouldDropDuplicatedEvent`
  调用、team 分支 `findTeamLeaderMessageWithFile` 兜底分支的源码位置由单测
  `teamLeaderMessages.test.mjs` 经 esbuild 打包后的 `teamLeaderMessages.ts` 锁定。
- 未验证：浏览器内 team relay 实际延迟投递的端到端复现（需集群后端 + team relay
  环境）；转测时应在 0.2.x 集群环境复核"team 模式生成文件，relay 延迟二次投递"
  不再出现双卡片。单 Agent 与 F5 路径不在本次回归范围（修复未改动其分支）。

## 防错模式与资料同步

可复用模式：

1. **事件处理器去重守卫对齐**：新增事件处理器入口必须接入
   `shouldDropDuplicatedEvent`，与同类处理器保持一致；`chat.file` 的遗漏是历史
   缺口，本次补齐。后续新增 `chat.*` handler 应默认带此守卫。
2. **终态化消息的增量合并兜底**：当某类消息在终态事件（如 `chat.final`）后会被
   清掉关联字段（如 `teamStream`）使按关联字段的定位失效时，对该消息的增量合并
   必须提供"身份级兜底"——按业务身份（这里是文件 path/name+size）在当前轮内
   查找，而非依赖终态化前的关联字段。两层去重互为兜底：事件级挡快速重复，身份级
   挡延迟到达。

无需新增 spec：未变更协议或对外契约，`teamStream.requestId` 关联与 `chat.final`
清 teamStream 的既有语义保持不变；本次是在该契约下补齐去重与兜底。模式已由
`teamLeaderMessages.test.mjs` 固化为回归断言。

## 遗留与后续

- team relay / fanout 的延迟投递根因（为何同一 chat.file 会被二次投递）属后端
  relay 层，不在本次前端修复范围；若后端能保证 chat.file 不在 chat.final 之后
  到达，第二层兜底将退化为防御性冗余，但保留不影响正确性。
- `findTeamLeaderMessageWithFile` 的文件身份键复用 `getFileIdentityKey`（path
  优先，回退 name+size）；若后续文件身份定义变化（如加入 hash），同步核对该
  helper 的命中语义。
