# Content Creation Assistant Plugin / 内容创作辅助插件

Version: `1.0.2`

内容创作辅助插件把零散的提纲、访谈记录、笔记、旧稿或成稿推进为可编辑的高质量内容。它优先在当前回复中交付可用结构与正文，不把计划、空模板或能力介绍当作首值。

核心能力覆盖三大阶段九个场景：**起草**（材料启动 + 文稿撰写）、**增强**（资料调研与引用补充 + 开篇优化 + 标题优化 + 文本润色）、**审阅**（结构诊断 + 逐段修改反馈 + 成稿收口）。

## Supported scenes

| Stage | Scene ID | 适用请求 | 当前回复中的最小成果 |
| --- | --- | --- | --- |
| 起草 | `material_activation` | 从零散材料启动新内容 | 文档判断、结构与章节任务、实质性开篇正文、风险、单一下一步 |
| 起草 | `manuscript_writing` | 基于已定结构撰写完整章节 | 承接位置、连续正文、章节完成说明、更新续写口令 |
| 增强 | `research_and_citation` | 为已有内容补充调研支撑和引用 | 调研缺口清单、可插入引用段落、引用标注、待核验标记 |
| 增强 | `opening_optimization` | 优化已有开篇或重写开篇 | 开篇诊断、优化后开篇正文、变更说明 |
| 增强 | `title_optimization` | 诊断和优化文章标题 | 标题诊断、2-3个优化方案、变更说明 |
| 增强 | `text_polishing` | 不改结构和事实，提升语言质量 | 润色诊断、逐段润色文本、变更汇总 |
| 审阅 | `paragraph_revision` | 对文稿逐段给出修改反馈 | 逐段诊断、修改建议、可直接替换的修订文本 |
| 审阅 | `structure_diagnosis` | 评估章节安排、逻辑流向和篇幅平衡 | 结构地图、逐维度诊断、结构建议 |
| 审阅 | `finished_draft_closure` | 审阅或收口已有成稿 | 总体判断、最高价值修复、证据与交付风险、单一下一步 |

只要材料足以产生可逆草稿，助手就先写出可编辑成果；只有一个缺失事实会实质改变结果时，才提出一个阻塞问题。它不虚构研究、引文、事实核验、版权授权或人工终审。

## Capability and compatibility

| State | 范围 | 行为边界 |
| --- | --- | --- |
| `supported` | 上述九类场景的对话内写作、调研、引用、开篇优化、标题优化、润色、结构诊断、逐段反馈和收口 | 不依赖连接器、服务、网络或文件工具即可提供首值 |
| `degraded` | 外部事实查证、文件写入或导出等可选增强不可用 | 说明缺失能力或失败，继续提供 chat-level artifact，不报告假成功 |
| `out_of_scope` | 宿主升级、自动发布、隐藏持久化、原子回滚、无回执的机器质量通过 | 不执行也不作成功承诺 |

## Package contents

运行时核心由三个聚焦 Skill、两个 Tool 和各自的 Skill 内引用组成：

| Skill | 阶段 | 参考文件 |
| --- | --- | --- |
| `content-drafting` | 起草 | first-value-writing.md, manuscript-writing.md, visualization-guidelines.md |
| `content-enhancement` | 增强 | research-and-citation.md, opening-optimization.md, title-optimization.md, text-polishing.md |
| `content-review` | 审阅 | paragraph-revision.md, structure-diagnosis.md, quality-and-safety.md |

| Tool | 功能 | 触发场景 |
| --- | --- | --- |
| `document_exporter` | 将结构化文档导出为带 TOC、引用列表和元数据的 Markdown 文件 | 用户要求导出、保存或交付成品文稿时 |
| `citation_formatter` | 按 GB/T 7714、APA、MLA 或行内格式标准化引用条目 | 用户需要格式化引用时，或导出前生成参考文献列表 |

## 使用方式

在 JiuwenSwarm 插件中心安装并启用本插件，对话输入区勾选插件 chip（可配合任意专家），然后发送推荐问法即可触发对应能力。