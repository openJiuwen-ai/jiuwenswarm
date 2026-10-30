# 模块文档约定

本仓不使用 `.agents/agent-docs`。设计、规约、特性与缺陷文档写在受影响模块自己的 `docs/` 下。
已有 `docs/AGENTS.md` 时，命名、骨架和提交拆分以该文件为准。
`docs/` 里已有其他体裁时，沿用这些文件的位置和写法，不在旁边新建 `specs/`、`features/`、`bugfix/`、`designs/`。
`jiuwenswarm/gateway/docs/` 是接口说明，改接口时修订对应文档。
模块完全没有 `docs/` 时，才沿用 `jiuwenswarm/server/docs/AGENTS.md` 的四类结构。

## 位置与命名

| 类型 | 位置 | 文件名 |
|---|---|---|
| spec | `<模块>/docs/specs/` | `S_NN_<slug>.md` |
| feature | `<模块>/docs/features/` | `F_NN_<slug>.md` |
| bugfix | `<模块>/docs/bugfix/` | `B_NN_<slug>.md` |
| 设计叙事 | `<模块>/docs/designs/` | 描述性文件名 |

- `NN` 是该目录内两位递增序号。已有 N 份时新文档为 N+1，不复用空号，不改已有文件名。
- slug 为小写英文短横线。文件名不带日期，日期写在文内元信息表。
- `designs/` 不参与 S/F/B 编号。它只放长期概览和架构叙事，例如 `<模块>_overview.md`、`architecture_cn.md`。
  一次特性的决策写入 `features/`，不另建带日期的设计目录。
- 转测稿由 jw-testing 写到被 Git 忽略的 `.agents/testing/<模块相对路径>/`，不放进模块 `docs/`，也不入库。

## 元信息

用文档正文里的元信息表，不用 YAML frontmatter，不生成仓库级索引。

- spec 至少包含：类型、编号、关联模块、最近一次修订日期、关联 feature。
- feature / bugfix 至少包含：日期、范围、测试基线。bugfix 在契约有变时填写关联 spec。
- 不写 commit hash，不写 owner。需要署名时，在元信息表增加「作者」，取创建时仓库的 Git `user.name`，后续普通修改不覆盖。

## 分工

- specs：当前契约、不变量、修改检查单。
- designs：系统为什么这样设计，给理解用的长期叙事。架构或数据流变了才改。
- features：一次特性的决策、拒绝的方案、验证和遗留。只增不删。一次连贯改动一份，不按提交拆。
- bugfix：现象、根因、修复决策和回归证据。

有对应设计叙事或 spec 时在正文链接它们。没有独立设计叙事时直接说明依据的 spec，不编造设计编号。

写作结束后不运行索引脚本。检视阶段只报告缺了哪份模块文档，不改待审资料。
