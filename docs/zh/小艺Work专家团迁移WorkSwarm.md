# 小艺 Work 专家团迁移 WorkSwarm 说明

本文面向研发和测试，说明 10 个小艺 Work 专家团如何转换为当前 WorkSwarm `develop` 分支可读取、可导入的 AgentGroup 资源包，以及验收时可以和不可以宣称的结论。

## 1. 基线与迁移范围

当前实现基于以下源码基线：

- JiuwenSwarm `develop`：`3548d192d8211a2b15480f519752239a6e26478c`
- agent-core：`4e6e53d4f3a97e4f39651be337ba309835de15e0`
- A4P：`1699ea352a08430731c6dddac0fc46a1b1a385f2`

两类源包共包含 10 个专家团：

| 源包 | 专家团 |
| --- | --- |
| `xiaoyi-work-six-expert-teams-persona-aligned-20260928.zip` | `family-weekend-trip`、`healthy-habit-companion`、`rental-move-decision`、`family-learning-companion`、`family-party-planner`、`home-storage-makeover` |
| `专家团(1).zip` | `travel`、`home-cook`、`personal-styling`、`home-design` |

迁移拆成两个可独立审查的阶段：

1. **ZIP 结构与 manifest 契约转换**：由 `scripts/convert_xiaoyi_agent_groups.py` 负责。
2. **Skill 与工具语义适配**：由 `scripts/expert_team_migration/adapt_workswarm_skills.py` 和可审查映射 `scripts/expert_team_migration/workswarm_skill_replacements.json` 负责。

这种拆分使 ZIP 契约问题和 Skill 运行时问题不会被一次大范围重写混在一起。

## 2. ZIP 字段与结构适配

### 2.1 根 manifest

转换后的根 `manifest.json` 按 WorkSwarm AgentGroup 契约规范化：

- 将 `packageType` 改为 `package_type: "agent_group"`，移除旧 `schemaVersion`。
- 显式补齐 `version`。
- 将小艺 `metadata` 中的展示信息转为 `display_name`、`display_description`、`category`、`tags` 和 `quick_inputs`。
- 展示名、描述、标签和快捷提问使用 `zh` / `en` 本地化结构。
- 将共享 `skills` 规范为去重后的 Skill ID 列表。
- 保留有效团队头像引用，移除无效或越界路径。

### 2.2 成员 manifest

每个 `agents/<member-id>/manifest.json` 转为 WorkSwarm `agent_template`：

- 将 `packageType` 改为 `package_type: "agent_template"`，补齐 `version`。
- 将旧 `metadata` 和 `agentCard` 的有效展示信息合并至标准字段。
- 规范化 `name`、`description`、`display_name`、`display_description`、`category`、`tags` 和 `quick_inputs`。
- 将源包 `avatars/<member-id>.<ext>` 迁移到成员目录；manifest 实际引用的栅格头像统一保持原始比例、缩放为最长边不超过 `256px` 并转为 WebP；WorkSwarm 原生支持的 SVG 头像保留为 SVG，不经有损栅格化。
- 原始高清栅格素材可保留在包内作为未被 manifest 引用的素材，但成员 manifest 和团队 manifest 必须引用压缩后的 `.webp`（或原生 SVG），不得直接引用原始大图。这是因为 `agent_groups.list` 会将引用头像编码为 data URL，原图会显著放大 catalog 响应载荷。
- 原有详细能力说明仍保留在 persona 文件，不丢失角色行为约束。

### 2.3 归档结构

最终每个 ZIP 必须满足：

- ZIP 内只有一个与根 manifest `name` 一致的顶层目录。
- 剔除 `__MACOSX`、`.DS_Store`、`._*` 等平台垃圾文件。
- 拒绝路径穿越和符号链接。
- 成员列表、persona、Skill 引用和 leader 约束可被当前 `develop` 生产 loader 解析。
- manifest 引用的团队和成员头像均为最长边不超过 `256px` 的 WebP；原始素材如需保留，不参与运行时引用。
- 归档排序和时间戳固定，使相同输入能产生稳定的 ZIP。

### 2.4 固定成员与共享边界

Skill 适配阶段会对 10 个团的根 `manifest.json` 统一写入：

- `team_mode: "predefined"`：运行时只使用包内预定义成员，不让主理人在当前任务中临时创建不携带预置 Skill 的动态成员。
- `member_instruction`：向所有成员共享最小事实、工具失败、数据隔离和交付边界。不能获得真实工具结果时必须返回 `blocked`、`partial` 或「待核实」，不得用模型记忆、手写 SVG 或截图冒充真实查询/生成产物。

`member_instruction` 和成员 persona 中的 declared-Skill 执行契约都由带起止标记的 managed block 维护。因此再次运行适配器时会更新旧块而不是重复追加；若成员后续新增 Skill，第二次适配会自动将新 Skill 的 `skill_tool` 调用补入同一契约块。

## 3. 小艺专属 Skill 替换

10 个团都会执行 Skill 适配。除小艺专属的搜索、图像理解和媒体生成能力外，
三个生活场景团还会替换不适合 WorkSwarm 的 Clawdbot/OpenClaw 持久化、自治唤醒、
断食默认和地图开发指南，并为明确承诺单 HTML 的成员补全交付契约。

| 能力 | 小艺侧 Skill / 别名 | WorkSwarm portable Skill | WorkSwarm 原生工具 | 直接或间接受影响的专家团 |
| --- | --- | --- | --- | --- |
| 网页研究 | `xiaoyi-web-search-win` | `workswarm-web-research` | `free_search`、`fetch_webpage` | `home-cook`、`home-design`、`personal-styling`、`travel` |
| 图像理解 | `xiaoyi-image-understanding` | `workswarm-image-understanding` | `visual_question_answering` | `home-storage-makeover` |
| 图像生成 | `seedream-image_gen`、`seedream-image-gen`、`seedream-image-gen-win`、`xiaoyi-image-gen` | `workswarm-image-generation` | `generate_visual` | `family-weekend-trip`、`family-party-planner`、`home-storage-makeover`、`home-cook`、`home-design`、`personal-styling` |
| 视频生成 | `seedance-video-gen-win`、`xiaoyi-video-gen` | `workswarm-video-generation` | `generate_video`、`check_video_status` | `home-cook`、`home-design`、`personal-styling` |
| 通勤比较 | `amap-jsapi-skill` | `amap-commute-comparison` | 通过 `tool_search` 发现已连接的 AMap MCP 工具 | `rental-move-decision` |
| 上班族生活方式 | `fitness-coach`、`fasting-tracker` | `workday-health-plan` | 无额外工具 | `healthy-habit-companion` |
| 打卡页面 | `habit-flow-skill`、`autonomous-tasks` | `habit-checkin-page` | `write_file` | `healthy-habit-companion`、`family-learning-companion` |
| 单 HTML 交付 | 新增契约 | `workswarm-single-html-delivery` | `write_file` | 六个小艺生活场景团的指定页面成员 |

适配器不只替换 Skill ID，还会同步：

- 为每个成员的全部声明 Skill 写入“相关才调用、相关则必须先加载”的统一执行契约；
  无关 Skill 不为凑调用数强制执行，相关 Skill 失败则必须返回 `blocked` 或 `partial`，
  不得改为模型直接生成并冒充 Skill 结果。

- 复制 portable Skill 文档到对应的团队或成员目录。
- 移除已被替代的旧 Skill 目录和 NOTICE 条目。
- 替换 persona、Skill 文档和 manifest 中的旧 ID、别名与工具调用语义。
- 将非主理人成员约束为只向主理人交付内部产物，不直接向用户发送附件。
- 将主理人的最终 HTML 交付约束为先写入真实 `.html` 文件，再以绝对路径调用 `send_file_to_user`。

`flyai-travel` 和 `wendao-skill` 不被盲目删除。它们在保留时会执行明确的来源语义和 URL 格式修正。

## 4. OpenClaw 能力边界

`autonomous-tasks` 不再通过文本改写伪装成 WorkSwarm 定时任务，而是在有限期打卡场景中直接替换为
`habit-checkin-page`。新 Skill 只承诺静态优先页面和可选浏览器本地状态，不冒充后台唤醒、
WhatsApp 提醒或长期记忆。如用户明确要求定时提醒，应单独检查当前运行时能力。

`amap-commute-comparison` 也不携带 Key；它要求运行环境预先安装并连接 AMap MCP。
静态扫描会把旧 Skill ID、`send_html_card`、OpenClaw cron 命令和 `.openclaw/skills` 硬编码路径视为残留问题。

## 5. 执行方式

以下命令从 JiuwenSwarm repo 根目录执行。标准环境优先使用 `uv run python`。

### 5.1 一条命令完成最终迁移（推荐）

```bash
uv run python -m scripts.migrate_xiaoyi_agent_groups \
  --input /absolute/path/xiaoyi-work-six-expert-teams-persona-aligned-20260928.zip \
  --input '/absolute/path/专家团(1).zip' \
  --output-dir /absolute/path/workswarm-agent-groups-final \
  --overwrite
```

该命令在临时工作区依次执行“发现 → 结构转换 → Skill 适配 → develop loader 校验 → 确定性 ZIP”，只有 10 个包和 `migration-report.json` 全部准备完成后，才通过同一文件系统上的目录级切换更新整个输出目录；切换失败会恢复旧目录，避免留下新旧混合的交付物。下面的分步命令主要用于定位问题。

### 5.2 仅批量转换两类源包

```bash
uv run python -m scripts.convert_xiaoyi_agent_groups \
  --input /absolute/path/xiaoyi-work-six-expert-teams-persona-aligned-20260928.zip \
  --input '/absolute/path/专家团(1).zip' \
  --output-dir /tmp/workswarm-agent-groups-converted \
  --overwrite
```

转换阶段会生成 10 个 ZIP 和 `conversion-report.json`。默认会调用当前 `develop` 的 AgentGroup loader；只有进行纯结构排查时才应使用 `--skip-runtime-validation`。

### 5.3 适配一个已解压的专家团

Skill 适配器接受“根目录名与 manifest `name` 一致”的未打包目录：

```bash
uv run python scripts/expert_team_migration/adapt_workswarm_skills.py \
  /tmp/workswarm-agent-groups-stage/family-weekend-trip
```

可先执行无写入预演：

```bash
uv run python scripts/expert_team_migration/adapt_workswarm_skills.py \
  --dry-run \
  /tmp/workswarm-agent-groups-stage/family-weekend-trip
```

适配后复检：

```bash
uv run python scripts/expert_team_migration/adapt_workswarm_skills.py \
  --check \
  /tmp/workswarm-agent-groups-stage/family-weekend-trip
```

命令退出码为 `0` 且最后一行 JSON 中 `status="ok"`、`runtimeValidated=true` 才表示包内 Skill 适配和当前 loader 检查通过。对 10 个根目录全部执行适配和检查后，再将每个根目录分别打成单顶层目录 ZIP。

### 5.4 单元和契约回归

```bash
uv run python -m pytest \
  tests/unit_tests/scripts/test_convert_xiaoyi_agent_groups.py \
  tests/unit_tests/scripts/test_migrate_xiaoyi_agent_groups.py \
  tests/unit_tests/expert_team_migration \
  tests/unit_tests/server/extensions/test_agent_group_package_acceptance.py \
  -q
```

### 5.5 最终 ZIP 的隔离导入 smoke

```bash
uv run python scripts/acceptance/agent_group_package_acceptance.py \
  import-local \
  --data-dir /tmp/workswarm-agent-groups-clean-home \
  --expected-count 10 \
  --json-out /tmp/workswarm-agent-groups-import.json \
  /absolute/path/to/family-weekend-trip.zip \
  /absolute/path/to/healthy-habit-companion.zip \
  /absolute/path/to/rental-move-decision.zip \
  /absolute/path/to/family-learning-companion.zip \
  /absolute/path/to/family-party-planner.zip \
  /absolute/path/to/home-storage-makeover.zip \
  /absolute/path/to/travel.zip \
  /absolute/path/to/home-cook.zip \
  /absolute/path/to/personal-styling.zip \
  /absolute/path/to/home-design.zip
```

`--data-dir` 必须是不存在或空的绝对目录。退出码为 `0`、顶层 `passed=true`、`catalog.missingImportedIds=[]` 才算完成这一层验收。

## 6. 三层验证与结论边界

| 层级 | 入口与证据 | 可以得出的结论 | 不能替代的验证 |
| --- | --- | --- | --- |
| 1. 提供方契约 | 转换和适配单测、生产 `load_agent_group_package_bundle` | 包结构、manifest、成员、persona 和 Skill 引用符合当前源码契约 | 不能证明导入 API、前端或任务效果正常 |
| 2. 消费方集成 | `JiuWenSwarm.process_message` 依次调用 `agent_groups.import_local`、`list`、`show`，再对安装目录执行 loader 二次读取 | 最终 ZIP 能经过真实 AgentServer Adapter 导入、安装、持久化并向 catalog 提供成员、Skill 和头像数据 | 不能证明模型会按 Query 正确选人或产物符合预期 |
| 3. 源码 Web 消费方 smoke | 在与第 2 层相同的隔离数据目录启动 develop Web，进入“我的专家 → 专家团” | 页面显示本次导入的 10 个卡片，无缺失或重复，卡片名称、头像和详情可见 | 不能证明模型会按 Query 正确选人或产物符合预期；也不等于 Windows 安装包通过 |
| 4. 交付物与效果 | 从前端选择专家团、发送代表 Query，检查成员调度、工具调用、任务状态、最终文件及打开效果 | 只有此层对应 case 通过后，才能宣称该专家团真实效果通过 | 源码 Web 环境仍不等于 Windows 客户端安装包 |

效果验证至少记录：Query、实际调用成员、Skill / tool 调用、耗时、失败与重试、最终主产物路径及打开结果。如果只完成前两层，结论应写为“包契约和导入链路通过”，不应写为“10 个专家团效果通过”。

## 7. 当前 macOS 环境限制

当前开发机为 macOS 12 x86_64，`uv.lock` 锁定的 `faiss-cpu==1.14.3` 没有该平台可用 wheel，因此在该机器上执行全新 `uv sync` 会因依赖分发限制失败。这不等于 AgentGroup 资源包校验失败。

当前机器可以使用已完整安装的 Python 3.12 环境，并通过 `PYTHONPATH` 精确指向上述 JiuwenSwarm、agent-core 和 A4P commit 进行源码契约与集成验证。但发布前仍需在支持的 Linux / Windows 构建环境使用 lockfile 重建标准 venv，并保留 Windows 客户端的 ZIP 选择、导入、权限、文件保存和成品打开 smoke。

## 8. 相关文档

- [AgentGroup 资源包验收](./AgentGroup资源包验收.md)
- [TeamOrganization 专家团与汇总配置](./TeamOrganization专家团与汇总配置.md)
