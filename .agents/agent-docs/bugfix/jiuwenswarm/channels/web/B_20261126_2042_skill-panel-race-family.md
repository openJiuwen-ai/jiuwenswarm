---
id: B_20261126_2042_skill-panel-race-family
title: 技能面板过期回写与专家编辑入口断链修复
type: B
author: gunner_beat
author_email: gunner_heart@163.com
modules:
  - jiuwenswarm/channels/web
files:
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel/useHubMarketplace.ts
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel/skillPanelGuards.ts
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel/skillRebuildWait.ts
  - jiuwenswarm/channels/web/frontend/src/components/AgentManagementPanel/DefinitionDetailPage.tsx
  - jiuwenswarm/channels/web/frontend/scripts/test-agent-definition-detail.mjs
  - jiuwenswarm/channels/web/frontend/tests/skillPanelGuards.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/skillRebuildWait.test.mjs
  - jiuwenswarm/channels/web/frontend/tests/agentDefinitionDetailPage.test.mjs
designs: []
design_note: 对技能/专家面板既有交互契约的局部修复（请求-回写时序与 props 传递），无跨模块契约变化，无独立设计。
---

# 技能面板过期回写与专家编辑入口断链修复

## 现象与根因

基线为 develop 的 6c0c84b91。四处相互独立的前端缺陷：

1. **技能详情晚到覆盖（B-M3）**：`fetchSkillDetail`（SkillPanel/index.tsx）与
   `fetchHubSkillDetail`（useHubMarketplace.ts）均为"发请求 → await → 无条件 setState"。
   先打开慢技能 A 再打开快技能 B，A 的响应晚到时把详情页覆盖回 A；
   `hubFetchSeqRef` 的序号守卫只覆盖列表请求，不覆盖详情。关闭详情页也不作废在途请求。
2. **启停开关过期闭包回写（B-M4）**：`toggleOneSkill` 的两段回写使用点击时的旧
   `selectedSkill` 闭包——await 期间切换到技能 B 后，完成回写把详情拉回 A；
   技能包详情刷新分支同样依赖旧闭包决定"当前在看哪个包"。
3. **重建等待循环不可取消（B-M5）**：`waitForSkillRebuild` 的 while 循环最长 30 分钟，
   无卸载/取代检查；完成后无条件 `fetchSkillDetail`，把用户正在看的其他技能详情
   覆盖回重建的旧技能。
4. **专家编辑入口断链（B-M2）**：`DefinitionDetailPage` 的 props 声明了 `onEdit`
   且父级传入（index.tsx:1607），但解构止于 `onDelete`、全文无 JSX 引用——
   唯一渲染编辑按钮的 `DefinitionCard` 是死代码，本地专家详情页无编辑入口，
   只能删除重建。

## 当前契约与修复决策

守卫逻辑抽为纯模块供组件与测试共用：

- `skillPanelGuards.ts`：`SequenceGuard`（begin/isCurrent/invalidate 单调序号）与
  `applyToggleToSelection`（按"当前选中"决定是否回写）。
- `skillRebuildWait.ts`：等待循环注入 sleep/requestStatus/isCancelled/now，
  返回 done/timeout/cancelled，失败语义与原实现一致。

接线：详情请求以序号守卫晚到响应（列表、详情、退出详情三处作废路径齐备）；
启停回写改函数式 setState + `selectedSkillRef` 镜像（useLayoutEffect 同步）判断
"此刻的选中"；重建等待以代次计数 + 卸载标记作取消谓词，取消后不再提示与回写，
`rebuildLoading` 仅由最新代次解锁；专家详情页对 `source === 'local'` 渲染编辑按钮
（复用既有 `agentManagement.actions.edit` 文案与 `detail-action` 样式）。

## 拒绝的方案

- 全部守卫内联在组件里：`fetchSkillVersions` 已有同款内联序号先例，但四处共用
  逻辑抽纯模块才能被 node --test 直接覆盖，也避免四处实现漂移。
- 按 `sys.platform`/浏览器判断跳过详情守卫：竞态与时延相关，与平台无关。
- 重建等待改 AbortSignal：循环内部检查点（sleep 前后）才是取消语义所在，
  注入谓词比传播 signal 到 `webRequest` 更小且可测。

## 回归证据与适用范围

环境为 Windows、Node 24、前端目录 npm ci 后执行（未运行格式化或静态检查）：

```text
npm run test:skill-panel-races        # 12 passed（守卫纯逻辑 + 重建等待六路径）
npm run test:agent-definition-detail  # 3 passed（本地=有编辑键/市场=无/busy 禁用）
npm run test:skill-marketplace        # 回归通过
npm run test:skillpack-install        # 回归通过
npm run test:skill-install-toast      # 回归通过
npm run build                         # tsc + vite 通过
```

红绿双向：`agent-definition-detail` 用例在基线代码（upstream/develop 6c0c84b91）上
重打包运行即红（编辑按钮不存在），恢复修复后绿；`skill-race` 场景在基线构建上
复现"晚到响应把详情覆盖回 A"，修复版保持 B（CDP 驱动 mock 环境，截图与断言在案，
复现环境维护在仓库外）。B-M4/B-M5 的新行为由注入假时钟的纯逻辑用例覆盖
（取消后停轮询、晚到 done 丢弃）；未做真实 30 分钟重建的端到端等待验证。

适用范围：`SkillPanel` 详情/启停/重建链路、专家详情页编辑入口；不改变
`skills.get`/`skills.toggle`/`skills.rebuild` 协议与后端行为。

## 资料同步与遗留

无用户文档触及（交互修复，不改变用法说明）。遗留：技能广场详情
（`skills.swarmskillshub.detail`）的守卫依赖组件各自持有序号，若后续把详情请求
收敛到统一 hook，守卫可合并；`waitForSkillRebuild` 的 `now` 注入默认
`Date.now`，无行为差异。
