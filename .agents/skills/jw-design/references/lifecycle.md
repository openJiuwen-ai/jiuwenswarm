# 方案与归档怎么流转

文档在受影响模块的 `docs/`。命名和提交拆分以该模块 `docs/AGENTS.md` 为准；没有该文件时见 [模块文档约定](document-metadata.md)。

## 谁维护什么

- `jw-design`：读现有 specs 和 designs。确认后的契约变更写入 `docs/specs/`，长期架构叙事写入 `docs/designs/`。
  一次特性的取舍写入 `docs/features/F_NN_<slug>.md`。确认前在正文标明尚未确认。
- `jw-dev`：按已确认方案改代码和测试，补全同一份 feature 的实际做法、偏差、验证和遗留。
  契约变了就改对应 spec。故障写入 `docs/bugfix/B_NN_<slug>.md`。
- 不把开发结果回填成已确认方案。目标方案变了，回到 `jw-design`，经确认后再改 spec 或 designs。

## 状态

不使用 `proposals` / `active` / `archive` 目录，也不使用 `D-YYYYMMDD-HHMM` 标识。

| 文档 | 状态怎么表达 |
|---|---|
| spec | 正文就是当前契约。修订日期写在元信息 |
| designs | 长期叙事。过时结论在文内标明被哪份 feature 替代 |
| feature / bugfix | 只增不删。未确认的方案在文内写明；确认人与日期只能在真实确认后写入 |
| 转测 | 本地 `.agents/testing/`，状态见 jw-testing 的 lifecycle |

用户或实际评审者明确确认后，才把方案写成已确认。开工、测试通过、合并分别记录，不互相代替。

## 引用

用仓库相对路径引用，例如 `jiuwenswarm/server/docs/specs/S_02_request-pipeline-dispatch.md`。
跨模块各自维护自己的 `docs/`，不把别的模块文档复制过来。
