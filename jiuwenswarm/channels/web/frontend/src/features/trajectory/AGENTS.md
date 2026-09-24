# 轨迹查看器开发规则

本文件的约束范围是 `jiuwenswarm/channels/web/frontend/src/features/trajectory/`（轨迹查看器 feature）。前端根的 `AGENTS.md`（Chrome 107 基线、Prettier、语义色 token、SVG `currentColor`）同时生效。

## 常用命令

所有前端命令在 `jiuwenswarm/channels/web/frontend/` 下执行。

```bash
# 单个测试套件（每个 test:trajectory-* 先用 esbuild 把被测模块打包进
# node_modules/.cache/<suite>/，再用 node --test 跑 tests/*.test.mjs）
npm run test:trajectory-projector    # projector + v2 replay vectors
npm run test:trajectory-window       # window/subjects/team grouping
npm run test:trajectory-retention    # checkpoint 续跑
npm run test:trajectory-sequences    # 内容寻址重建
npm run test:trajectory-turn-gaps    # 未记录 turn 区间
npm run test:trajectory-frames       # 流式增量帧
npm run test:trajectory-host         # SingleAgentSurface / featureConfig / tool schema
npm run test:trajectory-timeline
npm run test:trajectory-compaction
npm run test:trajectory-preview
npm run test:trajectory-lanes
npm run test:trajectory-layout

npm run lint          # eslint --max-warnings 0
npm run build         # tsc && vite build
npm run dev
```

没有聚合的 `npm test`，改动后按受影响模块逐个跑对应套件。

语义约定常量由 agent-core 生成，**不要手改** `semconv/*.generated.ts`：

```bash
# 仓库根目录，AGENT_CORE_DIR 默认 ../agent-core
make openjiuwen-semconv          # 重新生成 openjiuwen-semconv.generated.ts
make genai-semconv               # 重新生成 gen-ai-semconv.generated.ts
make check-openjiuwen-semconv    # 只校验不写入
make check-genai-semconv
```

## 架构

### 数据流水线

后端 `/api/trajectory/sessions/{id}/...`（`jiuwenswarm/gateway/channel_manager/web/trajectory_http.py`）→ 一串纯函数模块 → React 展示层。理解这个链路需要按顺序读：

1. **`trajectoryClient.ts`** — 同源 HTTP 客户端与所有 wire 类型（`TrajectoryDetailRecord` / `TrajectoryStreamFrame` / 各 response）。端点：`/subjects`（chain 清单）、`/{subject}`（分页记录）、`/stream-frames`、`/checkpoints`、`/sequences`、`/usage`、`/archive`。
2. **`trajectoryWindow.ts`** — 增量窗口事务。核心不变式：按 `revision` 水位翻页、`store_epoch` 轮转即全量重来、每条记录 latest-wins 且**终态吸收**（已 completed/error 的记录不会被后到的 running 覆盖）、stream frame 走自己独立的 `frame_seq` 水位。
3. **`trajectorySequences.ts`** — 记录里的长属性以 `@oj-seq:...` 引用形式存储，这里按 hash 把链重建成完整内容；因为内容寻址，缓存条目永不过期。
4. **`trajectoryCheckpoints.ts`** — 保留策略（`jiuwenswarm/observability/retention.py`）整页删除旧 turn 后留下 checkpoint，这里把它拆成三份 seed（subject 分组 / projector / v2 reducer），使剩余 turn 的渲染结果与旧 turn 仍在时完全一致。
5. **`trajectorySubjects.ts`** — 按执行主体（main_agent / team_leader / team_member / subagent / unassigned）分组，决定 tab 顺序与重名 subagent 的序号；`createTrajectorySubjectViewCache` 负责跨次发布保持未变分组与其投影的引用身份，避免整表重渲染。
6. **`projector/`** — 投影成读模型：
   - `attribute-resolver.ts` 把 OTLP 属性归一成 `NormalizedTrajectoryAttributes`。
   - `trajectory-v2-reducer.ts` 是 schema-v2 事件的幂等 reducer（context window commit/delta、compaction、tool result 回放），按 subject + sequence epoch 隔离。
   - `otel-trajectory-projector.ts` 是入口 `projectOtelTrajectory()`，合并 v1 span 投影与 v2 reduction，产出 `TrajectorySnapshot`。失败的呈现按记录归属：原生 run 把错误记在撞上它的那条记录上（inference / tool 的 span），对应行自然渲染成 error；外部 CLI 被网关限流时那次调用没有响应体、压根没产生记录，错误只落在 turn span 上，于是由 `failedTurnCells()` + `withTurnFailure()` 在该 turn 末尾补一行错误态 ASSISTANT（`startedAt` 取 span 结束时间，否则会排到整轮最前）。若这一轮连第一次模型调用都没成功（没有任何 group），再用 span 的 `openjiuwen.span.input` 补一行 USER —— 否则它没有任何行、整个 turn 不会被画出来，只留下看起来跳号的 Turn 编号。
7. **`trajectory/`** — 读模型与展示投影：`model.ts`（`TrajectorySnapshot` / `TrajectoryTurnModel` / `TrajectoryRequest`）、`record.ts`（`TrajectoryCell`）、`timeline.ts`、`search-index.ts`、`virtual-rows.ts`、`preview.ts`、`compaction.ts`。
8. **`client/`** — 展示组件 `TrajectoryExplorer` / `TrajectoryTable` / `TrajectoryTimeline` / `TrajectoryToolbar`，纯 props，不依赖任何应用级 context。

`trajectoryTurnGaps.ts` 从主 Agent 的 turn 编号推出未留下记录的 turn 区间：后端 turn 计数器不论是否录制都会逐轮递增（`jiuwenswarm/observability/turn.py`），而录制开启时每轮根 span 都会落库，所以编号缺口就是轨迹开关关闭期间跑过的 turn；retention 删掉的 turn 由 checkpoint 的 `turns.maxNumber` 兜底，不算缺口。只对单 Agent 模式的主 Agent 生效，子 Agent 和 Team 泳道的编号不跟这个计数器走。

`trajectoryArchive.ts` 是同一条流水线的离线入口：把 zip 内 `trajectory.jsonl`（v3，content-addressed JSONL）流式解开后喂给 `applyTrajectoryDetailRecords`，供回放已归档 session。

### 三个宿主

- **`SingleAgentSurface.tsx`** — chat / trajectory 的 tab 边界。chat 子树常驻挂载，trajectory 子树在首次显式请求前不挂载；只有 `agent` 和 `team` 模式暴露它。
- **`TrajectoryPanel.tsx`** — session 级 transport 宿主，唯一有副作用的大文件。它持有 window state、sequence cache、v2 reducer、subject view cache，订阅 `webClient` 的 `trace.updated` 提示与终态事件（`chat.final` / `chat.error` / `harness.session_finished` 等）触发补拉，管理归档回放模式与 raw OTel inspector。
- **`TeamTrajectoryWorkspace.tsx`** — team 模式下的成员泳道，复用同一套原生 overview/ledger，`teamTrajectoryLanes.ts` 提供泳道读模型。

挂载点在 `src/App.tsx`（`lazy(() => import('./features/trajectory/TrajectoryPanel'))`）；`featureConfig.ts` 是通过 `useSyncExternalStore` 暴露的开关，由实验设置写入。

### 输入框停靠

轨迹视图下 `.chat-panel-shell` 并非 `display:none`，而是绝对定位铺满后由 `App.css` 的 `.single-agent-surface--trajectory .chat-panel-shell > :not(.chat-panel-header)` 隐藏。`.chat-compose`（输入框、审批卡片、目标栏）对这条规则开了例外，浮在轨迹之上，使读者不必切回对话即可回应审批或发消息。

让位靠的是压缩轨迹区域，不是给内容加内边距：ChatPanel 用 ResizeObserver 实测 compose 高度上报，经 `trajectoryComposerClearance()` 归一后由 App 写成 `--trajectory-composer-clearance`，作为轨迹视图的 `bottom`。之所以不能用 `TrajectoryExplorer` 现成的 `bottomInset`（它只给 `TrajectoryTable` 加 `padding-bottom`），是因为面板底部还有 raw inspector 和 footer 在表格之外，padding 管不到它们，会从输入框两侧露出来。收起状态按 sessionId 存在 App，收起时 clearance 归零、轨迹立刻吃满高度。

面板自身也为此让出了竖直空间：raw inspector 默认折叠成摘要行（`rawExpanded` 初值 `false`），MIT 归属 footer 默认不渲染，改由 header 的 `trajectory-attribution-toggle` 按钮开合——归属仍须在 UI 可达，不要直接删掉。

## 本目录的硬约束

**纯函数模块与 React 隔离。** 每个 `test:trajectory-*` 脚本都用 esbuild 把单个模块打成独立 `.mjs` 再跑 `node --test`。给 `trajectoryWindow.ts`、`trajectorySubjects.ts`、`teamTrajectoryLanes.ts`、`trajectory/*.ts` 这类模块引入 React、CSS module 或浏览器 API，会直接打断对应测试脚本。新增可测纯模块时，要在 `package.json` 里补一条同形状的 `test:trajectory-*`。

**DSH 上游拷贝。** `client/`、`primitives/`、`theme/`、`trajectory/` 下多数文件是 DeepSeek Harness commit `99f6f02` 的 MIT 拷贝，映射记录在 `NOTICE.md` 与各目录的 `PROVENANCE.md`。改这些文件时同步更新对应 PROVENANCE 的适配说明；不要新增对上游 workspace 包的构建期或运行期依赖。

**主题作用域。** `theme/*.css` 把上游的全局 `:root` / `body` / 滚动条选择器改写成 `.jiuwenTrajectoryTheme` 与 `.jiuwenTrajectoryPortal`。不要在这里写全局选择器，否则会污染宿主应用。

**v2 replay vectors 是双份的。** `tests/fixtures/trajectory-v2/*.json` 与 agent-core 的 `tests/unit_tests/agent_evolving/trajectory/fixtures/v2` 是同一组向量的两份拷贝（两个仓库没有共同根）。改动 v2 payload 形状或回放语义时两边都要改。`VIEW_ONLY_CODES`（`v2.checkpoint_recovery` / `v2.partial_window`）是查看器独有、agent-core 不产出的诊断码。

**投影 fixture。** `fixtures/*.json` 是 `tests/trajectoryProjector.test.mjs` 的输入，其中 `core-contract-records.json` 覆盖核心契约，改动投影行为时优先扩这份而不是新建。
