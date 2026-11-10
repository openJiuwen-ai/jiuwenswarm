# tool_management/docs

`jiuwenswarm.agents.harness.common.tool_management` 工具管理模块的设计归档目录。本目录**仅文档**——运行时代码在
`agents/harness/common/{rails,tools}/` 与 `server/runtime/agent_adapter/`。结构规约、命名与提交约定见本文。

## 目录用途

| 目录 | 用途 | 关联触发 |
|---|---|---|
| `specs/` | 模块设计规约：不变量、接口契约、执行路径、修改检查单。**长期有效**。 | 契约 / rail 行为 / meta-tool / 装配变化 → 修订对应 spec |
| `designs/` | 面向理解系统的叙事（总览 / 两模型 / 术语 / 边界）。 | 能力结构性变化时修订 |
| `features/` | 特性变更归档（决策 + 拒绝方案）。**只增不删**。 | 特性提交前 `F_NN_<slug>.md` |
| `bugfix/` | 触及 spec 路径/不变量的缺陷修复归档。 | `B_NN_<slug>.md` |

## 命名规约

```
specs/    S_NN_<slug>.md    例：S_04_visibility-skill-gates-and-prefix-cache.md
features/ F_NN_<slug>.md
bugfix/   B_NN_<slug>.md
designs/  <自由命名>.md     例：tool_management_overview.md
```

- 前缀 `S_` / `F_` / `B_`；`NN` 两位递增；`<slug>` 小写短横线
- 日期写在文档元信息表，不写文件名

## specs 内容约定（本模块骨架）

与 skill 模块相同骨架：元信息、范围/边界、不变量（N1…）、接口契约、数据结构、执行路径（P1…）、修改检查单、与其它 spec 关系。

- 跨 spec 引用：`[[S_NN]]`
- 模块 `AGENTS.md` 架构铁律从各 spec 不变量提炼

## 提交约定

特性改动三连续提交：`feat(tool-mgmt)` → `test(tool-mgmt)` → `docs(tool-mgmt)`。

bugfix 触及 spec 时：**一个** `fix(tool-mgmt)` 提交（含 `bugfix/B_*` + spec 修订 + 测试）。

## 与其它 AGENTS.md 的关系

- `agents/harness/common/tool_management/AGENTS.md`：模块入口 + 铁律 + 归档强制约束
- 本文件：只讲归档结构，**不重复**设计正文

## 反模式

- 把 concrete toolkit（browser/shell/mcp_toolkits）的设计写进本 specs——应指向邻接模块或「不管」表
- 复制 `common/docs/specs/S_09` / `S_13` 全文——只引用
- 在 spec 里写 volatile 计数（工具总数、路由条数）
