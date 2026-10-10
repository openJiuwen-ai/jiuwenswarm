# JiuwenMate UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在 JiuwenSwarm Web 前端中实现与 JiuwenMate 设计稿一致、使用静态演示数据且不连接后端的完整页面集合。

**Architecture:** 在 `src/features/jiuwenmate/` 中建立独立页面外壳、页面状态和演示数据；现有 `App.tsx` 只负责进入/退出 JiuwenMate 以及把公共导航请求交给现有页面。办公室、创建、工作台、频道记录、轨迹和活跃数据均由聚焦的小组件组成，不复制现有市场、技能、扩展和设置实现。

**Tech Stack:** React 18、TypeScript、CSS、i18next、lucide-react、Vite

**Spec:** `docs/designs/jiuwenmate-ui-desigin.md`

## Global Constraints

- 特性、目录、组件和测试命名统一使用 `JiuwenMate` / `jiuwenmate`，不得使用 `AIColleague` / `aiColleague`。
- 第一阶段仅使用静态演示数据和本地 React 状态，不新增或调用后端接口。
- 不改变个人版单 Agent、现有 Team 模式、WebSocket、会话恢复和执行逻辑。
- 公共入口复用现有市场、资产、技能、扩展和设置页面。
- 所有颜色使用现有语义主题变量；多色设计素材可以保留原色。
- 所有新增交互元素和业务区域使用 `jiuwenmate-*` 格式的 `data-testid`，并在前端 `AGENTS.md` 登记前缀。
- 所有新增用户可见文案同时加入中文和英文 locale。
- Chrome 107 及以上可用；内容在窄窗口下可滚动且不互相遮挡。

## Review Focus

- 个人版切换到 JiuwenMate 再切回时，原有导航和会话状态必须保留；由 Task 1 的模式切换测试覆盖。
- 创建流程重复进入或取消时，不能产生重复演示同事或遗留错误页面状态；由 Task 3 的创建状态测试覆盖。
- 同事、频道和轨迹切换必须只改变 JiuwenMate 本地选中项；由 Task 4 的工作台测试覆盖。
- 空列表、长名称和窄窗口不能导致侧栏遮挡主内容；由 Task 4、Task 5 的布局断言和截图验证覆盖。
- 公共导航入口必须返回现有页面而不是渲染复制页面；由 Task 6 的集成测试覆盖。

---

### Task 1: JiuwenMate 模式入口与隔离外壳

**Files:**
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/JiuwenMateApp.tsx`
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/types.ts`
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/jiuwenmate.css`
- Modify: `jiuwenswarm/channels/web/frontend/src/App.tsx`
- Modify: `jiuwenswarm/channels/web/frontend/src/components/SessionSidebar/index.tsx`
- Modify: `jiuwenswarm/channels/web/frontend/src/components/SessionSidebar/SessionSidebar.css`
- Modify: `jiuwenswarm/channels/web/frontend/AGENTS.md`
- Modify: `jiuwenswarm/channels/web/frontend/package.json`
- Test: `jiuwenswarm/channels/web/frontend/tests/jiuwenmateMode.test.mjs`

**Interfaces:**
- Produces: `JiuwenMateExistingPage = 'agents' | 'personalContext' | 'skills' | 'connectorMarket' | 'settings'`，只允许 JiuwenMate 请求打开这五类既有页面。
- Produces: `JiuwenMateAppProps { onExit(): void; onOpenExistingPage(nav: JiuwenMateExistingPage): void }`。
- Produces: `JiuwenMateView` 页面枚举和只存在于 JiuwenMate 外壳中的导航状态。

- [ ] **Step 1: 写模式切换失败测试**

断言左下角切换按钮存在、进入 JiuwenMate 后原 `SessionSidebar` 主内容不渲染、退出后保留进入前的 `activeNav`。

- [ ] **Step 2: 登记测试脚本并运行，确认失败**

在 `package.json` 中登记 `test:jiuwenmate-mode`，用 esbuild 编译被测的模式状态模型后运行 Node test。

Run: `npm run test:jiuwenmate-mode`

Expected: FAIL，原因是 JiuwenMate 模式和入口尚不存在。

- [ ] **Step 3: 实现最小模式外壳**

在 `App.tsx` 中增加独立布尔状态，不复用 `AgentMode`；给 `SessionSidebar` 增加明确的模式切换回调和底部入口；JiuwenMate 模式只挂载 `JiuwenMateApp`。

- [ ] **Step 4: 登记模块测试前缀并补齐中英文文案**

登记 `features/jiuwenmate | jiuwenmate | JiuwenMate 页面`，新增切换入口和基础导航文案。

- [ ] **Step 5: 运行测试与类型检查**

Run: `npm run test:jiuwenmate-mode && npx tsc --noEmit`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add jiuwenswarm/channels/web/frontend/src/App.tsx \
  jiuwenswarm/channels/web/frontend/src/components/SessionSidebar \
  jiuwenswarm/channels/web/frontend/src/features/jiuwenmate \
  jiuwenswarm/channels/web/frontend/tests/jiuwenmateMode.test.mjs \
  jiuwenswarm/channels/web/frontend/package.json \
  jiuwenswarm/channels/web/frontend/AGENTS.md
git commit -s -m "feat(jiuwenmate): add isolated UI mode"
```

### Task 2: 设计素材、演示数据与共享组件

**Files:**
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/mockData.ts`
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/components/JiuwenMateRail.tsx`
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/components/ColleagueCard.tsx`
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/components/ColleagueAvatar.tsx`
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/components/SegmentedTabs.tsx`
- Create: `jiuwenswarm/channels/web/frontend/src/assets/jiuwenmate/*`
- Modify: `jiuwenswarm/channels/web/frontend/package.json`
- Test: `jiuwenswarm/channels/web/frontend/tests/jiuwenmatePresentation.test.mjs`

**Interfaces:**
- Produces: `JIUWENMATE_COLLEAGUES`, `JIUWENMATE_CHANNELS`, `JIUWENMATE_TRACES` 和 `JIUWENMATE_ACTIVITY` 只读演示数据。
- Produces: 可复用的导航、头像、同事卡片和页签组件。

- [ ] **Step 1: 写演示数据与组件契约失败测试**

断言稳定 ID 唯一、同事卡片包含名称/描述/标签/头像、列表项以稳定 ID 作为 `data-variant`。

- [ ] **Step 2: 登记测试脚本并运行，确认失败**

在 `package.json` 中登记 `test:jiuwenmate-presentation`，编译演示数据与共享展示模型后运行 Node test。

Run: `npm run test:jiuwenmate-presentation`

Expected: FAIL，原因是演示数据和共享组件尚不存在。

- [ ] **Step 3: 导入设计稿所需素材并实现共享组件**

仅复制页面实际使用的角色头像和插画；单色图标优先使用现有资产或 lucide，不导入 Pixso 编译产物。

- [ ] **Step 4: 运行测试**

Run: `npm run test:jiuwenmate-presentation`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add jiuwenswarm/channels/web/frontend/src/features/jiuwenmate \
  jiuwenswarm/channels/web/frontend/src/assets/jiuwenmate \
  jiuwenswarm/channels/web/frontend/package.json \
  jiuwenswarm/channels/web/frontend/tests/jiuwenmatePresentation.test.mjs
git commit -s -m "feat(jiuwenmate): add design assets and presentation models"
```

### Task 3: 办公室介绍与创建同事页面

**Files:**
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/pages/OfficeIntroPage.tsx`
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/pages/ColleagueCreatePage.tsx`
- Modify: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/JiuwenMateApp.tsx`
- Modify: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/jiuwenmate.css`
- Modify: `jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json`
- Modify: `jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json`
- Modify: `jiuwenswarm/channels/web/frontend/package.json`
- Test: `jiuwenswarm/channels/web/frontend/tests/jiuwenmateCreateFlow.test.mjs`

**Interfaces:**
- Consumes: Task 2 的同事卡片、头像和演示同事。
- Produces: `OfficeIntroPageProps { onCreate(): void }` 和 `ColleagueCreatePageProps { onCancel(): void; onComplete(colleague): void }`。

- [ ] **Step 1: 写创建流程失败测试**

覆盖介绍页进入创建、专家卡片选择、自定义表单、取消返回、完成后只加入一个演示同事并进入工作台。

- [ ] **Step 2: 登记测试脚本并运行，确认失败**

在 `package.json` 中登记 `test:jiuwenmate-create-flow`，编译创建流程状态模型后运行 Node test。

Run: `npm run test:jiuwenmate-create-flow`

Expected: FAIL。

- [ ] **Step 3: 实现介绍页和创建页**

按照设计稿实现三步介绍、专家卡片网格、自定义创建表单、能力配置折叠区和底部操作栏；创建仅更新内存状态。

- [ ] **Step 4: 补齐中英文文案并运行测试**

Run: `npm run test:jiuwenmate-create-flow && npm run test:i18n-locales`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add jiuwenswarm/channels/web/frontend/src/features/jiuwenmate \
  jiuwenswarm/channels/web/frontend/src/i18n/locales \
  jiuwenswarm/channels/web/frontend/package.json \
  jiuwenswarm/channels/web/frontend/tests/jiuwenmateCreateFlow.test.mjs
git commit -s -m "feat(jiuwenmate): add colleague creation pages"
```

### Task 4: 同事工作台、频道记录与轨迹页面

**Files:**
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/pages/ColleagueWorkspacePage.tsx`
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/pages/ChannelTracePage.tsx`
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/components/ColleagueList.tsx`
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/components/ConversationComposer.tsx`
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/components/TraceTimeline.tsx`
- Modify: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/JiuwenMateApp.tsx`
- Modify: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/jiuwenmate.css`
- Modify: `jiuwenswarm/channels/web/frontend/package.json`
- Test: `jiuwenswarm/channels/web/frontend/tests/jiuwenmateWorkspace.test.mjs`

**Interfaces:**
- Consumes: Task 2 的同事、频道和轨迹演示数据。
- Produces: 同事、会话、频道、定时任务和轨迹的本地选择状态；不向外暴露业务数据。

- [ ] **Step 1: 写工作台状态失败测试**

覆盖同事选择、三个记录页签、频道切换、查看轨迹、时长/Token 切换、空定时任务和长名称展示。

- [ ] **Step 2: 登记测试脚本并运行，确认失败**

在 `package.json` 中登记 `test:jiuwenmate-workspace`，编译工作台状态模型后运行 Node test。

Run: `npm run test:jiuwenmate-workspace`

Expected: FAIL。

- [ ] **Step 3: 实现三栏工作台和频道记录**

按设计稿建立 72px 导航栏、同事栏、记录栏和主内容区；会话输入框仅呈现，不发送消息。

- [ ] **Step 4: 实现轨迹页面与空态**

轨迹时间条、轮次、工具调用和原始记录使用演示数据；保证内容区独立滚动。

- [ ] **Step 5: 运行测试和类型检查**

Run: `npm run test:jiuwenmate-workspace && npx tsc --noEmit`

Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add jiuwenswarm/channels/web/frontend/src/features/jiuwenmate \
  jiuwenswarm/channels/web/frontend/package.json \
  jiuwenswarm/channels/web/frontend/tests/jiuwenmateWorkspace.test.mjs
git commit -s -m "feat(jiuwenmate): add colleague workspace and traces"
```

### Task 5: 办公室成员、活跃数据与任务中心

**Files:**
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/pages/OfficeDashboardPage.tsx`
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/components/ActivityHeatmap.tsx`
- Create: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/components/ActivityTable.tsx`
- Modify: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/JiuwenMateApp.tsx`
- Modify: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/jiuwenmate.css`
- Modify: `jiuwenswarm/channels/web/frontend/package.json`
- Test: `jiuwenswarm/channels/web/frontend/tests/jiuwenmateOffice.test.mjs`

**Interfaces:**
- Consumes: Task 2 的同事和活跃数据。
- Produces: 办公室成员、活跃数据和任务中心三个可切换页面状态。

- [ ] **Step 1: 写办公室页面失败测试**

覆盖三个页签、成员卡片、全员/个体切换、年度/时段切换、热力图提示内容和任务中心静态状态。

- [ ] **Step 2: 登记测试脚本并运行，确认失败**

在 `package.json` 中登记 `test:jiuwenmate-office`，编译办公室状态模型后运行 Node test。

Run: `npm run test:jiuwenmate-office`

Expected: FAIL。

- [ ] **Step 3: 实现办公室页面**

成员卡片复用 `ColleagueCard`；热力图使用 CSS Grid 和语义主题变量，不新增图表依赖。

- [ ] **Step 4: 运行测试**

Run: `npm run test:jiuwenmate-office`

Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add jiuwenswarm/channels/web/frontend/src/features/jiuwenmate \
  jiuwenswarm/channels/web/frontend/package.json \
  jiuwenswarm/channels/web/frontend/tests/jiuwenmateOffice.test.mjs
git commit -s -m "feat(jiuwenmate): add office dashboard pages"
```

### Task 6: 公共导航复用、视觉验收与全量验证

**Files:**
- Modify: `jiuwenswarm/channels/web/frontend/src/features/jiuwenmate/JiuwenMateApp.tsx`
- Modify: `jiuwenswarm/channels/web/frontend/src/App.tsx`
- Modify: `jiuwenswarm/channels/web/frontend/package.json`
- Test: `jiuwenswarm/channels/web/frontend/tests/jiuwenmateIntegration.test.mjs`

**Interfaces:**
- Consumes: Tasks 1～5 的完整 JiuwenMate 页面。
- Produces: 公共入口到现有 `activeNav` 的单向导航，以及可构建的完整静态页面集合。映射固定为：广场→`agents`、资产和知识→`personalContext`、技能→`skills`、扩展→`connectorMarket`、设置→`settings`。

- [ ] **Step 1: 写公共导航失败测试**

断言市场、资产、技能、扩展和设置入口调用既有导航回调，不在 JiuwenMate 中生成复制页面；从公共页面返回后仍可重新进入 JiuwenMate。

- [ ] **Step 2: 登记测试脚本并运行，确认失败**

在 `package.json` 中登记 `test:jiuwenmate-integration`，编译公共导航映射模型后运行 Node test。

Run: `npm run test:jiuwenmate-integration`

Expected: FAIL。

- [ ] **Step 3: 接通公共导航**

JiuwenMate 内只传递 `JiuwenMateExistingPage`，由 `App.tsx` 将其赋给既有 `activeNav`；不得触发会话创建、WebSocket 请求或后端 API。

- [ ] **Step 4: 执行浏览器视觉核对**

逐页对比办公室介绍、创建、工作台、频道记录、轨迹、定时任务空态、办公室成员和活跃数据设计稿；检查 1920×1080、1440×900 和窄窗口，修正布局差异。

- [ ] **Step 5: 运行全量验证**

Run: `npm run test:jiuwenmate-mode && npm run test:jiuwenmate-presentation && npm run test:jiuwenmate-create-flow && npm run test:jiuwenmate-workspace && npm run test:jiuwenmate-office && npm run test:jiuwenmate-integration && npm run test:i18n-locales && npm run build`

Expected: 所有测试和构建通过。

- [ ] **Step 6: 检查补丁质量**

Run: `git diff --check && git status --short`

Expected: 无空白错误；只包含 JiuwenMate 页面、测试、素材、文案和设计文档，原有 `main.py` 不纳入提交。

- [ ] **Step 7: 提交**

```bash
git add jiuwenswarm/channels/web/frontend
git commit -s -m "feat(jiuwenmate): complete static UI experience"
```
