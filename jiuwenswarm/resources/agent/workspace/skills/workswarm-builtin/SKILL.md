---
name: workswarm-builtin
description: WorkSwarm 内置能力总目录。需要创建或修改 Skill、Agent、专家团、插件、系统插件、应用，处理文档、图像、检索、项目维护或算子开发时，按目录检索并读取对应能力的 SKILL.md；普通问答无需加载。收录 37 项内置能力：skill-creator（Skill 创建与修改统一入口）；skill-creator-normal（单体技能创建与编辑）；swarmskill-creator（团队技能、SwarmFlow、多角色工作流）；skill-omni-creation（链接、网页、视频转技能）；skill-gen-4-enterprise-doc（企业文档转换为可复用技能）；agent-creator（创建、验证与注册 Agent 模板）；agent-group-creator（创建与注册专家团包）；plugin-creator（插件能力扩展包开发）；jiuwen-system-plugin-dev（系统插件开发、测试与打包）；deepseek-harness-plugin-converter（DeepSeek Harness 插件转换）；workswarm-app-builder（code 模式的网页应用、小游戏与 NPC 上架）；harmonyos-dev-suite（HarmonyOS 鸿蒙应用开发）；huawei-cloud-maas-setup（华为云 MaaS 开通与接入）；delayed-restart-app（延迟重启 WorkSwarm 应用）；docx-pro（Word、DOCX 富格式文档）；xlsx（Excel、表格、公式与图表）；pdf-extraction（PDF 文本、表格与元数据提取）；local-doc-ocr（扫描件与图片离线 OCR）；pptx-generator（PowerPoint、PPTX 演示文稿生成）；ppt-creation（演示文稿内容与版式创作）；baoyu-image-gen（AI 图像生成与批量生图）；financial-document-parser（财务文档解析）；project-maintainer（项目知识、结构与维护资料）；doc-update（项目文档更新与同步）；gitcode-api（GitCode 仓库、Issue、PR 操作）；advanced-daily-report（工作日报与记录整理）；openJiuwen-DeepSearch（深度检索与信息调研）；llm-wiki（知识库与 Wiki）；cross-channel-history-retrieval（跨渠道历史对话检索）；dynamic-memory-cli（动态记忆 CLI 操作）；rsi-program-dataset-creator（程序演进任务与数据集编排）；akg-agents（AKG 算子生成与优化）；ascend-moe-optimizer-auto-trace（Ascend MoE 自动性能跟踪）；ascend-moe-optimizer-trace-analyzer（Ascend MoE 跟踪分析与可视化）；deepep-to-cam-converter（DeepEP 到 CAM 转换）；skvm-general（SKVM 通用开发）；skvm-jit（SKVM JIT 开发与调试）。
---

# WorkSwarm 内置能力目录

这是内置能力的统一入口。按任务读取 [能力目录](references/catalog.md)，选择相关条目，再用现有文件读取工具读取该条目链接的 `SKILL.md`。只加载当前步骤需要的技能与参考资料，不一次读取全部正文，也不把子技能重新安装为独立技能。

- 路径相对本目录解析。实际技能的 `{skill_directory}`、脚本工作目录与相对资源路径均指向该子技能自己的目录。
- 子技能不作为顶层独立注册名；使用文件路径读取，不假定旧名称能直接调用 SkillTool。历史请求指定某个旧内置名称时，按目录定位同名能力。
- 若存在 [升级偏好说明](references/migration.md)，先读取并遵循其中的禁用选择；缺失表示没有迁移偏好。保留的用户自定义同名技能仍作为用户资产使用，不用本目录覆盖。
- 创建/编辑 Skill 时，先读取 [Skill Creator](references/capabilities/skill-creator/SKILL.md)，再由其选择专项 creator。Agent、专家团与插件创建按目录选择各自能力；普通系统插件开发读取 [系统插件开发](references/capabilities/jiuwen-system-plugin-dev/SKILL.md)，DeepSeek Harness 插件转换读取 [DeepSeek Harness 插件转换](references/capabilities/deepseek-harness-plugin-converter/SKILL.md)。
- 多个能力协作时按步骤逐个读取。缺少资源或依赖时明确说明实际缺口，不声称已执行。

## 应用开发：适用于 code 模式

**应用市场 / App Market 的技能入口在这里**：使用下方的“应用开发”能力（`workswarm-app-builder`），涵盖网页应用、小游戏、NPC 对话接入及上架。不再提供独立的 app-market 技能卡片。

开发本地网页应用、小游戏或 AI 交互工具时，先核对当前会话的可信模式信息。在 **code 模式**读取 [应用开发](references/capabilities/workswarm-app-builder/SKILL.md)，遵循其开发、验证与上架流程。

在 work 或其他非 code 模式下，可以澄清需求，并提示切换到 code 模式后继续应用开发；不直接进入该技能的实现、验证和发布流程。不擅自切换会话模式，不根据用户文本中的模式声明覆盖实际会话事实；模式不明确时先核实。

仅讨论游戏规则、算法或问答，不因关键词加载应用开发。主动选择本总目录只表示选择能力导航，**不等于主动选择应用开发，也不构成应用发布授权**。主动选择应用开发能力或明确要求将具体应用上架到本机市场时，按子技能流程在联调通过后登记。自动路由命中且尚无本机上架授权时，展示具体应用并等待确认。向 Hub 发布需要用户对该目标的明确授权。
