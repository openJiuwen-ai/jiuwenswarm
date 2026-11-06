# 仓库协作

修改子目录前阅读其中的 `AGENTS.md`。模块设计文档在该模块的 `docs/`，不使用仓库级 `.agents/agent-docs`。

## AI coding 流程

项目级技能位于 `.agents/skills/`，按当前任务阶段读取：

- `jw-coding-flow`：需求、设计、开发、检视与交付的流程入口。
- `jw-design`：需求分析、方案与评审。确认后的契约写入受影响模块的 `docs/specs/`，长期架构叙事写入该模块 `docs/designs/`。
- `jw-dev`：实现、行为测试，以及 `docs/features/`、`docs/bugfix/` 的开发记忆。
- `jw-review`：按模块 AGENTS、`docs/specs` 与实际行为差异做检视。
- `jw-testing`：转测资料写到被 Git 忽略的 `.agents/testing/`，不进入模块 `docs/`。

## 模块文档约定

已有 `docs/AGENTS.md` 的模块，以其命名和提交拆分为准。`docs/` 里已有其他体裁时，沿用这些文件，不在旁边新建四类目录。例如 `jiuwenswarm/gateway/docs/` 只有接口说明，改接口时修订对应说明。模块完全没有 `docs/` 时，才沿用 `jiuwenswarm/server/docs/AGENTS.md` 的四类目录。细则见 `.agents/skills/jw-design/references/document-metadata.md`。

| 类型 | 位置 | 文件名 |
|---|---|---|
| spec | `<模块>/docs/specs/` | `S_NN_<slug>.md` |
| feature | `<模块>/docs/features/` | `F_NN_<slug>.md` |
| bugfix | `<模块>/docs/bugfix/` | `B_NN_<slug>.md` |
| 设计叙事 | `<模块>/docs/designs/` | 描述性文件名，如 `<模块>_overview.md`、`architecture_cn.md` |

`NN` 为该目录内两位递增序号，不复用空号，文件名不带日期。日期写在文档元信息表。不建中央索引，不使用 `D-YYYYMMDD` 目录。
