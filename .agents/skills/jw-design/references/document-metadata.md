# 文档作者、设计关联与自动索引

本规则管理 `.agents/agent-docs/` 下的 D/F/B/S、说明与生成索引，不批量给产品/API 文档或历史代码署名。
它记录文档写作者，不记录长期 code owner；任务分工、真实评审人和已有检视/合并配置仍保持各自含义。

## Frontmatter

所有受管 Markdown 文档都以 YAML frontmatter 开头；历史 owner 就是 author，迁移时将原值保留为 author，再移除旧字段。

| 字段 | 含义 |
|---|---|
| author | 创建本文时当前仓库有效的 `git config user.name`；保留原作者，不被后续普通修改覆盖。 |
| author_email | 同一 Git 身份的 `git config user.email`，辅助区分重名账号。 |
| id、title、type | 稳定标识、标题与文档类型 D/F/B/S；说明文档可不编号。 |
| modules | 涉及模块的实际目录列表；用元数据检索，不再用 F 子目录分类。 |
| files | 实际涉及的代码文件路径列表，可空；不把计划路径伪装成已经存在的实现。 |
| designs | F/B 必须声明的关联 D 标识列表，支持已有 SDD 标识；跨仓标识注明仓库，并在正文给出定位。 |
| design_note | 无独立 D 时解释原因；历史关系未核实时说明缺口。 |

Git 身份缺失时明确报告，不能猜测身份、使用模型名或替用户修改 Git 配置。
模板中的 author: null 是待工具填写的占位。新文档执行同步时使用 --document 指定路径，
工具根据当前仓库身份填入 author 与 author_email；已存在的有效作者不会被覆盖。
历史文档优先将原 owner 的值直接迁移为 author，不因 Git 新增记录不同而覆盖它，也不丢失 @handle 等原始署名。
只有原来没有 owner 或 author 时，才从可证实的 Git 文档新增记录补充；明显迁入/集成记录不作为原始创作证据。
两者都无法取得时才使用 author: null 和 author_note，不用本次迁入者覆盖历史署名。
只有用户明确要求按本次写作身份迁移时，才能使用 --adopt-legacy-author 为尚无作者的旧文档署当前身份。

## 文档分工与位置

- D：开发前方案、取舍、评审与确认；继续使用 designs/proposals、active、archive 生命周期目录。
- F：实际实施、决策、设计偏差、验证与遗留。新建文件直接位于 features/F_YYYYMMDD_HHMM_<slug>.md，不再按模块建子目录。
- B：故障现象、根因、修复决策与回归；保留 bugfix 的既有模块布局。
- S：当前有效契约、不变量、协议和接口；保留 specs 的模块布局与既有编号。
- 转测资料：作者同样遵循 frontmatter 规则，由 jw-testing 独立管理，仍为本地忽略资料，不进入 D/F/B/S 正式索引。

历史文件名和标识不因整理重编号。旧 F 子目录可由索引递归读取；迁移历史路径须保留正文并修复引用，不能让新布局造成旧链接失效。
作者字段不替代真实评审/批准记录，也不意味着文档作者就是代码开发者。

## F/B 与设计的明确关系

- 有设计：designs 填已核实的完整 D/SDD 标识，正文解释本次实施/修复与方案的关系。
- 无独立设计：designs: []，并填写 design_note，如对现有契约的小范围修复；不虚构 D 编号。
- 历史待核实：designs: null，附 design_note；索引显示“待核实”，不能解释为没有设计。
- 多份设计使用列表；部分替代按决策关系记录仍有效的范围，不把正文里碰巧提及的 D 自动当成关联设计。

## 写作后自动更新索引

创建/修改/迁移受管文档后，在本任务内执行同步作为写作收尾，不等待用户额外要求更新索引：

```bash
source .venv/bin/activate
export PYTHONPATH=.:$PYTHONPATH
python .agents/skills/jw-design/scripts/sync_documents.py --document .agents/agent-docs/features/F_YYYYMMDD_HHMM_topic.md
```

示例路径需要换成实际文件。仅重新生成时省略 --document；工具可以从其他 cwd 启动，默认根目录按脚本所在仓库解析。
自动维护 `.agents/agent-docs/INDEX.md`、`designs/INDEX.md`、`features/INDEX.md`、`bugfix/INDEX.md`、`specs/INDEX.md`。
总索引与 F/B/S 索引按 modules 分组，提供模块导航和文档数量；跨模块文档在各相关分组展示，总数按源文件去重，未声明模块的放入未分类。
组内保留类型、作者、模块、代码文件和关联 D，目录不再承担唯一检索功能；已有 D 索引继续按模块和生命周期展示。
模块名称展示沿用各仓 D 索引的规则：core 省略固定 openjiuwen/ 前缀，swarm 保留跨仓完整路径；不改写源文档的声明。
索引是生成视图，事实只在源 frontmatter 维护。不启动常驻 watcher、不安装 Git hook，也不增加提交或外部操作权限。
检视期间保持候选只读，只报告缺失元数据或索引，交由开发/文档修改阶段同步。
