# data-testid 生成规则

生成/改动前端代码(`src/**/*.tsx|ts|jsx|vue`)时,**直接在代码里加上符合规范的 `data-testid`**,让 Playwright 等自动化测试用 `getByTestId` 稳定定位元素。不列清单、不产出 manifest 文件。命名有拿不准的地方(多个候选名、无稳定 ID 等)直接向用户提问。

## 命名结构

```
{module-prefix}-{element-semantic}[-{qualifier}]
```

- **module-prefix**:模块前缀,见下方"前缀映射表"(目录名转 kebab)。**所有 testid 必带,无例外**。
- **element-semantic**:元素语义,kebab-case 全小写,见名知意(可稍长,够区分即可)。
- **qualifier**(可选):限定片段,两种形态之一,见下。

## 三条铁律

1. **连字符命名法**:主体(`module-prefix` + `element-semantic`)全小写、`-` 分隔,禁下划线/驼峰/大写。
2. **见名知意**:名字能让人不看代码就猜到元素用途;可机动按业务语义取名,长一点没关系。
3. **以源码名为准,不受测试用例命名影响**:取组件名/变量名/文案/aria-label/className 里最能代表语义的部分。测试用例名字只作"哪些元素该被测"的参考,不采用其命名。

## qualifier 两种形态

- **人为限定词**:kebab-case 小写,如 `turn`、`add-file`。遵守主体规则。例:`chat-panel-completed-work-chip-turn`。
- **源码特定设定**:直接取自源码的标识值(枚举值、稳定 ID),**原样保留,可含 `.` `_`**——这是"值"不是"命名"。例:`agent.plan`、`auto_harness`、`${node.id}`。

判定:片段来自源码枚举/ID → 源码特定设定,原样保留;来自人为补充语义 → 人为限定词,kebab-case。主体和人为限定词不要主动引入点号/下划线,仅 qualifier 取源码特定设定时才原样保留。

## 多形态元素命名(Playwright strict mode 兼容)

**核心判据:DOM 是否同时并存。**

- **并存(同刻 DOM 多个)** → 各自命名(按语义加限定词)。如 tab 组、选项组、菜单多项。
- **互斥(同刻 DOM 只一个)** → 归一单 testid + `data-variant` 区分。如 list/grid 双形态、success/error toast 条件渲染、loading/ok/error 阶段。

为什么看并存:strict mode 下同 testid 命中多个元素时 `.click()` 会抛 `strict mode violation`;并存的必须各自 testid,互斥的归一后同刻只一个仍唯一命中。

**列表项**(`items.map(...)` 动态生成、数量不定):归一单 testid + `data-variant`,每项带稳定 ID 收窄。`data-variant` 取值兜底优先级:

1. **优先** 可读稳定业务 ID:`member.member_id` / `agent.name` / `item.title` / `node.id`。
2. **次选** 固定枚举值/限定词。
3. **兜底** 顺序 index(数量不定且无稳定 ID 才用)。
4. **最后手段** 文本/role 收窄(尽量避免)。

禁止用 `compact/expanded`、随机 hash、会变的 index 作 variant——这些不区分"哪一项"。

**三条不该归一的反例:**

1. **被多处复用、无 testId 透传 prop 的通用子组件**(SimpleSelect/TimePicker/DatePicker/Switch 等)→ 按**编号 qualifier** 拆 testid(如 `cron-simple-select-1`、`skill-panel-switch-1`),不为它专项加透传 prop。子组件**内部元素也要加**:触发按钮归一 `xxx-trigger`,选项按钮归一 `xxx-option` + `data-variant={opt.value}`(同刻通常只渲染一组,故不编号)。
2. **语义不同的独立元素**(安装/卸载/内置兜底)→ 按语义各自命名(`-install-btn`/`-uninstall-btn`/`-builtin-btn`),判据是文案/onClick/业务用途不同而非状态切换。
3. **无法挂载的场景**(React Fragment `<>` 不产生 DOM)→ 先不加,不为此改标签结构或增删元素。

**scope 收窄**:同 testid 在页面上多次出现(如多个表单各有"提交按钮")时,不必让 testid 全局唯一——给外层结构加 scope 容器 testid,靠容器区分。稳定性比全局唯一重要。

## 同一 DOM 元素状态切换:单 testid + 区分属性

元素文案/图标/class 随状态切换但 DOM 节点同一(没卸载重建),只给一个 testid,状态用区分属性表达:

- **toggle/选中态** → `aria-pressed="true|false"`(源码已有 `aria-pressed`/`aria-selected` 则直接复用)。
- **加载/形态/类型态**(`idle|saving|loading`、`list|grid`、`success|error`)→ `data-variant`(默认口径)。
- `data-state` 一般不用,仅源码既有约定已用时沿用,不新建。

拿不准时优先 `data-variant`。

## 哪些元素该加 testid

**总则:元素有稳定语义身份就加,能加则加。** 不论当前是否可见、是否被 feature flag 关闭——不渲染时 testid 零成本,开关打开后自动可用。子组件内部的元素同样适用。

优先级:

1. 交互元素:button、input、textarea、select、a、可点击的 div/span。
2. 面板/区域根容器(如 `data-testid="chat-panel"`)。
3. 列表容器 + 列表项:`<ul data-testid="xxx-list">` + `<li data-testid="xxx-list-item">`。
4. 对话框/弹窗根。
5. 动态渲染区(内容会变、测试要断言,如 AI 回复气泡)。
6. 表单:form 根 + 各字段(如 `data-testid="session-name-input"`)。
7. 承载业务语义的静态文本:badge/状态标签、空态/提示文案、计数/摘要、菜单项/选项标签、标题/tab 标签。

### 静态文本(默认加,只排除装饰)

默认给所有"承载业务语义的静态文本"加,只排除纯装饰/状态符号。判断口径:文案变了会不会有测试因此崩?会崩就加。纯装饰(svg 图标、emoji、→/✓ 符号)和动态绑定文本(纯 `{msg}` 展示)不加。

## 已有 testid 的处理

- 已有合规(含缺前缀)和已有违规(非 kebab、重名等,且非源码特定设定)的,一律改写为**带前缀、合规**的新命名。
- 测试侧引用了源码不存在的 testid:当前不需要管,不为此在源码补 testid。

## 改动红线

只加/改 `data-testid`。其他一律不动:不改标签、不改属性(className/onClick/aria-*/style/ref)、不改逻辑/文案/样式、不增删元素。

## 前缀映射表

前缀 = 模块目录名转 kebab-case。给某模块生成 testid 先查此表取前缀。

> **必须登记**:新增模块/新增前缀时,必须在本表追加一行(组件目录 | 前缀 | 功能特性),登记后才能使用该前缀。目录名转 kebab-case 若与既有前缀冲突或过长,取一个不冲突的短前缀并在此说明。

| 组件目录(相对 `src/`) | 模块前缀 | 功能特性 |
|---|---|---|
| `components/ChatPanel` | `chat-panel` | 主聊天面板;输入区;消息渲染;欢迎页/历史加载 |
| `components/CronPanel` | `cron` | 定时任务面板;任务抽屉;表达式编辑器;时间选择器;模型/模式选择;状态徽标;确认对话框 |
| `components/ArtifactsPanel` | `artifact` | 产物列表面板;文件预览;产物集合与解析模型 |
| `components/ToolPanel` | `tool-panel` | 工具调用展示;harness 扩展树;只读文件模态框 |
| `components/TodoList` | `todo-list` | 待办列表;待办项 |
| `components/MarkdownRenderer` | `markdown` | Markdown 渲染器;代码块/图表/数学;隔离预览 |
| `features/a2ui` | `a2ui` | A2UI 消息内容渲染;表单控件;错误边界;渲染器注册 |
| `components/SkillPanel` | `skill-panel` | 技能管理主面板;tabs;卡片列表/网格;索引检索树;安装/卸载/启用 |
| `components/SkillGraphPanel` | `skill-graph-panel` | 技能总谱图谱;画布渲染;技能列表;节点详情 |
| `features/OnlineSkillSearchPanel` | `online-skill-search-panel` | 在线技能搜索面板;来源筛选;结果列表/网格;安装 |
| `features/SkillNetSearchModal` | `skill-net-search-modal` | SkillNet 在线搜索/安装弹窗;结果列表;评估对话框 |
| `features/SkillEvolutionModal` | `skill-evolution-modal` | 技能演进记录弹窗;条目列表/编辑/删除/保存 |
| `features/ClawHubSearchModal` | `claw-hub-search-modal` | ClawHub 搜索/安装弹窗;Token 配置;结果列表 |
| `features/SourceManagerModal` | `source-manager-modal` | 技能来源管理弹窗;来源切换;Token 配置 |
| `components/HeartbeatPanel` | `heartbeat-panel` | 心跳任务面板;抽屉;状态徽章;调度编辑器 |
| `components/ConnectorMarket` | `connector-market` | 连接器市场;详情/创建/注册页;各类弹窗;卡片 |
| `features/settings` | `settings` | 设置页;模块导航;各设置模块;渠道/模型列表/对话框 |
| `features/auth` | `auth` | 登录页;登出按钮 |
| `components/subagent` | `subagent` | 子代理面板(紧凑/展开);列表+详情;活动列表 |
| `components/AgentManagementPanel` | `agent-management` | Agent 管理面板;tabs+搜索+创建菜单 |
| `components/SessionsPanel` | `sessions-panel` | 会话列表面板 |
| `components/SessionSidebar` | `session-sidebar` | 会话侧边栏(新建/切换/重命名) |
| `multi-session` | `multi-session` | 多会话路由;聊天路由 |
| `components/TeamPanel` | `team-panel` | 团队面板 |
| `components/teamArea` | `team-area` | 团队区域容器 |
| `components/MemberTaskDrawer` | `member-task-drawer` | 成员任务抽屉 |
| `components/TeamMemberAvatar` | `team-member-avatar` | 团队成员头像 |
| `components/AgentPanel` | `agent-panel` | Agent 面板;文件查看器 |
| `components/GoalBar` | `goal-bar` | 目标进度条 |
| `components/InteractionSlot` | `interaction-slot` | 交互插槽;prompt 路由 |
| `components/LogsPanel` | `logs-panel` | 日志面板 |
| `components/ConfigPanel` | `config-panel` | 配置面板 |
| `components/ExtensionsPanel` | `extensions-panel` | 扩展面板 |
| `components/ExtensionsHubPanel` | `extensions-hub-panel` | 扩展中心面板 |
| `components/HarnessPackagePanel` | `harness-package-panel` | harness 包面板 |
| `components/UpdatePanel` | `update-panel` | 更新面板 |
| `components/BrowserPanel` | `browser-panel` | 浏览器面板 |
| `components/ChannelsPanel` | `channels-panel` | 频道面板 |
| `features/modelSetupGuide` | `model-setup-guide` | 模型配置引导 |
| `features/code-mode` | `code-mode` | 代码模式;代码审查面板 |
| `features/UserQuestionModal` | `user-question-modal` | 用户提问弹窗 |
| `features/TeamSkillsHubModal` | `team-skills-hub-modal` | 团队技能中心弹窗 |
| `components/FileIcon` | `file-icon` | 文件类型图标 |
| `components/ModelProviderIcon` | `model-provider-icon` | 模型供应商图标 |
| `components/Switch` | `switch` | 通用开关(无透传,调用处编号兜底,自身不加) |
| `features/trajectory` | `trajectory` | 轨迹面板(归档导入/导出;原始数据检视器) |
| `features/trajectory` | `single-agent` | 单 Agent 工作台(chat/trajectory 双 tab 切换) |
| `features/trajectory` | `team-trajectory` | 团队轨迹工作台(泳道视图) |
| `App.tsx` | `app` | 应用外壳;全局布局与 toast |