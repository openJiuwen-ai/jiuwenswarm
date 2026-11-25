---
id: B_20261121_1529_skill-install-toast-display-name-width
title: 技能安装成功提示显示英文 slug 与提示条固定过宽修复
type: B
author: yuhaiquan
author_email: 384127717@qq.com
designs: []
design_note: 由 issue #6894 测试反馈直接驱动的展示层修复，无独立 D；沿用前端 i18n 与主题变量既有契约。
modules:
  - jiuwenswarm/channels/web/frontend
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel
files:
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/SkillPanel/SkillToasts.tsx
  - jiuwenswarm/channels/web/frontend/src/features/ClawHubSearchModal/index.tsx
  - jiuwenswarm/channels/web/frontend/src/features/TeamSkillsHubModal/index.tsx
  - jiuwenswarm/channels/web/frontend/src/features/OnlineSkillSearchPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/features/EnterpriseSkillSourcePanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/features/SkillNetSearchModal/index.tsx
  - jiuwenswarm/channels/web/frontend/tests/skillInstallToast.test.mjs
  - jiuwenswarm/channels/web/frontend/package.json
---

# 技能安装成功提示显示英文 slug 与提示条固定过宽修复

## 现象与影响

GitCode issue openJiuwen/jiuwenswarm#6894（0.2.9 Windows 转测版本，单 Agent 模式）：在技能面板"技能广场"安装技能后，右上角绿色成功提示条显示"已安装：primary-school-tutoring-team"、"已安装：dev-coder"等英文 slug，而市场卡片与"我的技能"展示的是中文名（如"小学课程辅导团队"、"AI辅助研发-编码开发"）；同时提示条无论消息长短固定 564px 宽，短消息下视觉上"过长"。issue 截图存于本地 `D:\swarm-dev\tmp_issue6894\`（s1/s3 为提示条现象，s2 为详情页中文展示名对照）。所有技能安装入口（技能广场、ClawHub 搜索、TeamSkillsHub、在线搜索、企业技能源、SkillNet）的成功提示均受影响。

## 关联设计与当前契约

无独立 D。依据的当前契约：前端安装成功提示统一走 i18n 词条（zh：`已安装：{{name}}`），后端各安装处理器（`handle_skills_team_skills_hub_install` / `handle_skills_clawhub_download` / `handle_skills_source_install`）在成功返回的 `skill` 对象中携带 `name`（安装目录名/slug）与 `display_name`（市场展示名，`skill_manager.py` 各返回点）。前端 AGENTS.md 要求用户可见文案中英文双语完整、颜色使用语义变量——本次未新增词条与颜色。

## 根因与修复决策

**英文根因（取错字段，非 i18n 缺翻译）**：`SkillPanel/index.tsx` 的 `handleInstallHubSkill` 成功提示取 `data.skill?.name`（即 SKILL.md frontmatter name / hub slug，英文），未使用 `display_name`。中文展示名在链路上全程存在：市场卡片渲染 `skill.display_name || skill.name`、安装请求把 `display_name` 传给后端、后端成功返回也回传 `display_name`——仅提示条没有取用。语言包、fallback 语言（zh）与语言检测均正常，已排除整体语言回退。

**过宽根因**：成功提示条样式写死 `width: 564px`（与同文件知识库构建进度条对齐的遗留），对"已安装：xxx"这类短消息不成比例。

修复决策：提示名统一改为"入口可见展示名（条目 display_name / 卡片名）→ 后端回传 display_name → 实际安装名"的回退链。条目名优先而非后端回传优先，原因是：同版本重装短路返回不含 display_name；TeamSkillsHubModal 安装调用不传 display_name，后端回传会退化为 slug；企业源后端还会把 display_name 截断到 200 字符——三种场景下条目名都是用户实际看到的更准确名称。宽度改为自适应内容 + `maxWidth: 564px` 上限，文本加 `truncate` 防溢出，关闭按钮加 `flex-shrink-0`。备选方案"统一抽公共 Toast 组件"被拒绝：8 处内联提示条分属 6 个特性文件，收敛组件属重构，超出本 bugfix 边界，列入遗留。

SkillNet 条目无 display_name 数据来源（安装名即包内 name），仅修宽度不改名。SkillToasts 中知识库构建进度条与 loading 条保持固定 564px（进度语义需要稳定宽度），不属于本缺陷现象。

## 回归证据与适用范围

候选：当前工作区 `dev_1121`（未提交）。环境：Windows，Git Bash，Node 前端测试。

- 新增 `tests/skillInstallToast.test.mjs`（`npm run test:skill-install-toast`）：3/3 通过——SkillToasts 成功条挂载断言无固定 width、maxWidth=564px、文本 truncate、关闭按钮 flex-shrink-0；5 个安装入口提示名回退链源码断言；3 个弹窗文件无固定宽度残留。
- `npm run test:skill-marketplace` 4/4、`npm run test:skillpack-install` 11/11 通过；`npx tsc --noEmit` 全工程零错误。
- 未验证：浏览器实际渲染的自适应宽度效果（jsdom 无布局引擎），需转测后在 0.2.10 界面上复核短/长消息两态；agent 对话内 `install_skill` 工具返回的英文 detail（`agents/harness/common/tools/skill_toolkits.py:665`）属对话文本，不在本次前端修复范围。

## 防错模式与资料同步

可复用模式：展示层文案取名字段时，"用户在入口看到的名称"（display_name）与"系统实际登记名"（name/slug）是两个字段，提示类 UI 必须显式声明取用顺序，且要覆盖后端幂等短路返回缺字段的场景（本次靠条目名兜底）。该模式已由 `tests/skillInstallToast.test.mjs` 固化为回归断言；无需新增 spec（无契约变化，后端返回结构未动）。

## 遗留与后续

- 8 处内联成功提示条收敛为共享组件（顺带统一 loading/error 变体），属重构，后续单独立项。
- TeamSkillsHubModal 卡片直接渲染 `item.name` 且安装调用不传 `display_name`，经此入口安装的技能在"我的技能"持久化名仍是包内名（本次仅保证提示条与卡片一致）；如需对齐 SkillPanel 的展示名落盘行为，另行调整该弹窗的安装参数与卡片渲染。
- agent 侧 `install_skill` 工具返回文本为英文（含使用指引），如需对齐界面语言由 agent-harness 侧另立任务。
- 市场卡片描述中的英文触发短语（"Use when…/Do NOT use for…"）为 Hub 内容质量，建议随 issue 回复说明，非前端可修。
