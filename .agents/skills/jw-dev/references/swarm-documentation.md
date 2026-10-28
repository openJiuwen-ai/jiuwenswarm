# JiuwenSwarm 开发文档约定

应用代码位于 `jiuwenswarm/`，沙箱位于 `jiuwenbox/`，SDK 位于 `sdks/`，独立 TUI 包位于 `packages/jiuwenswarm-tui/`。
按实际修改目录维护 `.agents/agent-docs/specs/<module>/`、`features/<module>/` 和 `bugfix/<module>/`。
读取目标目录 AGENTS.md 与既有中英文文档，使用 [spec-template.md](spec-template.md) 和 [development-memory.md](development-memory.md)。

## 边界与事实

- `jiuwenswarm/agents/` 的应用 Harness 与 agent-core 的 `openjiuwen/harness/`、`openjiuwen/agent_teams/` 分属不同仓库。
  跨仓行为需要核对依赖源码与实际消费方；core 设计/spec 留在 core，swarm 文档链接引用，不复制成当前契约。
- 涉及事件、协议或返回值时，沿服务、gateway、Web/TUI、SDK 的实际调用链核对消费与兼容行为。
- spec 只描述当前契约，模块路径及符号必须真实存在，不变量应可定位；实施决策和偏差归档至 feature，故障证据归档至 bugfix。
- 应用行为变化同步 `docs/` 中对应中英文说明；UI 变更提供实际截图或录屏证据，不伪造验证结果。
- 运行时路径统一使用 `jiuwenswarm/common/utils.py` 的路径函数。定位 session 时使用根 AGENTS.md 列出的数据位置。

## 验证与交付

从仓库根目录激活 `.venv` 并设置 `PYTHONPATH`，选择相关 pytest 行为测试。
前端按其 `package.json` 选择已有 `npm run test:<功能>`；不以定向新用例替代已有消费者回归。
不运行格式化或静态检查。报告实际命令、版本、结果及未验证范围。
提交遵循本仓库规则，先给出具体范围并取得确认，不沿用 core 的 harness / agent_teams 拆提交要求。

## 决策与行为证据

决策续写/替代按 [决策归属](../../jw-design/references/decision-records.md) 维护，不把 core 专属归档与拆提交规则同步到本仓。
按 [行为证据](behavior-evidence.md) 覆盖受影响的 Runtime、Gateway/AgentServer、Web/TUI、Process CLI/SDK 实际消费入口；
不机械要求无关渠道重测。模型输入、工具结果、UI/协议输出和最终文件/数据库/进程状态按各自语义取证，不能以 chat.final、ACK 或成功提示替代实际副作用。
前端共享组件核对真实调用方、主题/窄屏/无障碍与中英文；跨仓回放 fixtures 或生成资源变更同时核对 core 版本与实际读者，保留本地 editable 和锁定依赖证据的区别。
有证据支持的故障类别按 [防错模式](defensive-patterns.md) 写回所属模块规则/协议并链接 bugfix，不因单渠道故障增加全局禁令。

按 [上下文与流程](context-and-flows.md) 追踪渠道输入、Runtime 状态、持久化、Gateway 事件和客户端派生输出的相关因果链，
明确 session/history、缓存、UI 投影及遥测的实际职责；不假定每条路径都经过同一渠道或网络入口。
已有 `.doc_project_maintainer/` 只作为按需导航，核实其版本与相关片段；验证后同步受影响的已有资料，未覆盖部分继续明确保留。
