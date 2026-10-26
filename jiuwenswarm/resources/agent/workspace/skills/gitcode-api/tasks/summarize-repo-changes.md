# 总结仓库最近改动

## 目标

围绕 GitCode 仓库最近的 commit / PR / issue 生成高可信摘要，重点回答以下问题：

- 最近 x 条内容主要做了什么。
- 责任人是谁，是否限定到某个 GitCode 用户。
- 改到了哪些代码模块。
- 可能引入了什么风险。
- 改到了哪些对外接口。

默认使用 `gitcode-api` CLI 获取结构化结果。

## 需确认的输入

优先收集这些输入；缺失时补问：

- `owner`: GitCode 仓库所属空间 path。
- `repo`: GitCode 仓库 path。
- `kind`: `commit` / `pull request` / `issue`。
- `limit`: 最近多少条，默认 5，建议不超过 20。
- `filter_user`: 可选，按 GitCode 用户过滤。
- `focus`: 用户更关心哪类输出：
  - 内容摘要 + 责任人
  - 模块 + 风险 + 责任人
  - 对外接口 + 责任人
- `repo_dir`: 可选；当用户要求“模块”或“对外接口”分析时，检查本地仓库目录能显著提高准确率。

## 核心规则

- 先确认 `gitcode-api --version` 是 `1.2.16` 或更高；命令不可用时按 `references/install-and-auth.md` 处理。
- 不要只复述标题；要尽量基于详情、文件路径、patch、issue 正文或评论来总结。
- 不要把“作者 / 创建者 / assignee / reviewer / tester”混成一个字段；必须说明你把谁视为“主要责任人”。
- 责任人可以有多个，次要责任人也要报告，均需输出用户名。
- 不要编造风险或接口变更；证据不足时明确写“未发现明确证据”或“仅为推测”。
- 当用户要“最近 x 条”，默认按服务端最近更新时间倒序获取；若 CLI 方法不直接支持排序参数，以接口默认顺序为准并说明限制。
- 单次 CLI 调用返回内容不全时，多次调用补全，不要漏掉必要信息。
- 哪怕用户只说明总结 Issue 或 PR，实际执行时也要检查 Issue 关联的 PR、PR 关联的 Issue 或正文中出现的交叉引用线索。
- 最后报告时，先给总览，再挑最重要的几条展开。

## CLI 用法确认

不同版本可能有细微差异。执行前先用 help 确认参数：

```bash
gitcode-api commits list --help
gitcode-api pulls list --help
gitcode-api issues list --help
```

需要补详情时继续确认：

```bash
gitcode-api commits get --help
gitcode-api pulls get --help
gitcode-api pulls list-files --help
gitcode-api pulls list-commits --help
gitcode-api issues get --help
gitcode-api issues list-comments --help
```

所有示例都可改用脚本入口（不建议）：

```bash
python scripts/gitcode_api_cli.py <resource> <method> [options]
```

## 如何搜集必要信息

### 1. 提交（Commit）总结

获取最近 commits：

```bash
gitcode-api commits list \
  --owner <owner> \
  --repo <repo> \
  --per-page <limit> \
  --compact
```

可选限定分支或路径：

```bash
gitcode-api commits list \
  --owner <owner> \
  --repo <repo> \
  --sha <branch-or-sha> \
  --path <path> \
  --per-page <limit>
```

用户指定 GitCode 用户时，先拉取足够数量的最近 commits，再在返回结果中按 author / committer 字段过滤；`commits list` 不直接暴露 `author` / `since` / `until` 时不要伪造筛选。

对选中的每条 commit 补详情：

```bash
gitcode-api commits get \
  --owner <owner> \
  --repo <repo> \
  --sha <sha> \
  --compact
```

需要比较两个 ref 时：

```bash
gitcode-api commits compare \
  --owner <owner> \
  --repo <repo> \
  --base <base> \
  --head <head>
```

### 2. 拉取请求（Pull request）总结

获取最近 PR：

```bash
gitcode-api pulls list \
  --owner <owner> \
  --repo <repo> \
  --per-page <limit> \
  --compact
```

如果该版本 help 支持排序参数，可按 help 添加 `--sort updated`、`--direction desc` 或等价参数；不支持时不要传不存在的 flag。

用户指定 GitCode 用户时，先拉取最近 PR，再在返回结果中按 creator / assignee / reviewer / tester 等字段过滤。

对每条 PR 补详情：

```bash
gitcode-api pulls get \
  --owner <owner> \
  --repo <repo> \
  --number <number> \
  --compact
```

分析 PR 改动范围时，默认优先拉文件和提交列表：

```bash
gitcode-api pulls list-files \
  --owner <owner> \
  --repo <repo> \
  --number <number> \
  --compact

gitcode-api pulls list-commits \
  --owner <owner> \
  --repo <repo> \
  --number <number> \
  --compact
```

如果接口失败或数据不足，再从 PR 详情中取 `base.ref` / `base.sha` 与 `head.ref` / `head.sha`，调用 `commits compare` 补全。优先使用 SHA；缺失时再退到分支名。

### 3. 工单（Issue）总结

获取最近 issues：

```bash
gitcode-api issues list \
  --owner <owner> \
  --repo <repo> \
  --per-page <limit> \
  --compact
```

如果该版本 help 支持排序参数，可按 help 添加 `--sort updated`、`--direction desc` 或等价参数；不支持时不要传不存在的 flag。

用户指定 GitCode 用户时，先拉取最近 issues，再在返回结果中按 creator / assignee 字段过滤。

对每条 issue 补详情和评论：

```bash
gitcode-api issues get \
  --owner <owner> \
  --repo <repo> \
  --number <number> \
  --compact

gitcode-api issues list-comments \
  --owner <owner> \
  --repo <repo> \
  --number <number> \
  --per-page 100 \
  --compact
```

issue 通常没有代码 diff；因此模块、风险、接口分析应基于：

- issue 标题与正文。
- 评论中的设计、验收、联动信息。
- 是否提及 PR、commit、分支、服务名、接口名。
- 若有本地仓库和对应 PR / commit 线索，再继续下钻。

### 4. 总结任务中的交叉引用

Issue 与 Pull Request 天然互相连接。总结 issue 时，需要同时检查关联的 pull requests；总结 PR 时，也要检查标题、正文、评论或提交信息里关联的 issue。

CLI 没有直接返回关联对象时，按证据逐步处理：

1. 在 PR / issue 标题、正文、评论、commit message 中寻找 `#123`、`!123`、URL、分支名或 commit sha。
2. 对明确编号调用 `issues get` 或 `pulls get`。
3. 对只有 commit sha 的线索调用 `commits get`。
4. 证据不足时，在报告中写明“未发现明确关联”。

## 责任人的定义

不同对象的“责任人”定义不同，输出时必须写清楚：

- `commit`: 默认责任人是 commit author；committer / merger 视为协作角色。
- `pull request`: 默认责任人是 PR creator；若有 assignees，可写“主责任人候选”；reviewer / tester 视为协作角色。
- `issue`: 默认责任人优先取 assignee；没有 assignee 时退回 creator。

建议统一输出：

- `primary_owner`
- `collaborators`
- `evidence`

## 模块改动分析

优先根据改动文件路径归纳模块，而不是根据 commit message 猜。

推荐顺序：

1. 先聚合改动文件路径。
2. 归纳 top-level 目录、子系统目录、包名或服务名。
3. 若用户给了 `repo_dir`，再到本地仓库确认这些路径属于什么模块。

常用判断方法：

- 顶层目录：如 `frontend/`、`backend/`、`docs/`、`sdk/`
- 服务目录：如 `services/xxx/`、`apps/xxx/`、`modules/xxx/`
- 协议与接口目录：如 `api/`、`routes/`、`controllers/`、`handlers/`、`openapi/`、`proto/`
- 基础设施目录：如 `migrations/`、`deploy/`、`helm/`、`terraform/`

若只能看到零散文件名，就诚实输出“可能涉及某模块”，不要过度定性。

## 风险分析

风险必须绑定证据。优先给“为什么可能有风险”。

高关注信号：

- 数据库迁移、存储结构调整、索引变更。
- 权限、认证、鉴权、租户隔离。
- 缓存键、消息协议、异步任务。
- 公共 SDK、公共库、共享组件。
- 配置项、环境变量、网关、路由。
- 大量删除、重命名、跨模块联动。
- 测试缺失，或只改生产代码未见验证信息。

输出建议分为：

- `Low`: 影响面局部，改动集中。
- `Medium`: 跨模块或配置 / 协议存在联动。
- `High`: 数据、鉴权、兼容性、对外接口或大范围重构。

如果证据不足，不要强行分级，可以写：

- `Risk: unknown`
- `Reason: 缺少 diff / 缺少本地仓库上下文`

## 对外接口分析

这里的“对外接口”优先指 **库 / SDK / 框架 / 服务 对外暴露给调用方的 interface**，而不是网络端口或基础设施端口。

典型例子：

- SDK 可调用入口，如 `client.chat.completions.create(...)`。
- 公共导出类、函数、方法、命名空间。
- 函数或方法的参数、默认值、可选项、返回结构。
- OpenAPI / API schema / DTO / JSON schema / proto 中定义的请求与响应契约。
- CLI 对外命令、子命令、flag、参数语义。

只有在仓库本身明确提供 HTTP / RPC / GraphQL 等服务接口时，才把这些请求接口也视为“对外接口”的一部分。

当用户明确要求“改了哪些对外接口”时，优先走下面流程：

1. 获取变更对应的文件列表与 patch。
2. 优先定位这些路径或代码区域：
   - `sdk/`
   - `client/`
   - `clients/`
   - `api/`
   - `public/`
   - `exports/`
   - `interfaces/`
   - `types/`
   - `schemas/`
   - `openapi/`
   - `swagger/`
   - `proto/`
   - `cli/`
3. 若用户给了 `repo_dir`，继续在本地仓库核实：
   - 对外暴露的 API 接口 / 参数是否变化。
   - 类 / 方法 / 函数签名是否变化。
   - 参数名、参数类型、默认值、必填项是否变化。
   - 返回值、响应 schema、异常或错误码是否变化。
   - 调用入口链路是否变化，例如 `client.chat.completions.create`。
4. 对每条候选接口，区分以下几类：
   - 明确新增的对外接口。
   - 明确修改的对外接口。
   - 明确废弃或删除的对外接口。
   - 仅内部实现变化、无证据表明 interface 变化。
5. 最终输出时，优先写“接口名 + 变化点”。

如果只有 issue 没有代码 diff，除非 issue 正文明确写了接口名、调用方式或参数变更，否则不要把需求描述当成“已发生接口改动”。

## 推荐工作流程

1. 先确认 `owner`、`repo`、`kind`、`limit`、`filter_user`、`focus`。
2. 运行 `gitcode-api --version`；版本不满足时提示安装或升级。
3. 用方法级 help 确认当前版本的 CLI 参数。
4. 拉取最近记录列表，先做一轮粗筛，保留最相关的 x 条。
5. 对每条记录补详情：
   - commit: `gitcode-api commits get`，必要时 `gitcode-api commits compare`。
   - PR: `gitcode-api pulls get`，必要时 `gitcode-api pulls list-files` / `gitcode-api pulls list-commits` / `gitcode-api commits compare`。
   - issue: `gitcode-api issues get`，必要时 `gitcode-api issues list-comments`。
6. 归一化责任人。
7. 根据文件路径和 patch 归纳模块。
8. 根据变更类型判断风险。
9. 当 focus 包含“对外接口”时，优先结合本地仓库做二次核实。
10. 以“结论先行、证据在后”的方式输出。

## 输出模板

### A. 最近 x 条内容，仅摘要（用户明确要求不要过于详细）

```markdown
## 摘要
- [类型 #编号/sha] 一句话说明做了什么
- [类型 #编号/sha] 一句话说明做了什么

## 要点
- 归纳 2-4 个主要主题
```

### B. 责任人 + 模块 + 风险

```markdown
## 摘要
- 最近 <x> 条 <kind> 主要集中在：<themes>

## 条目分解
1. <identifier>
   - Owner: <primary_owner>
   - Modules: <module list>
   - Risk: <Low/Medium/High/unknown>
   - Reason: <why>

## 跨条目风险
- <shared risk>
```

### C. 责任人 + 对外接口

```markdown
## 摘要
- 最近 <x> 条 <kind> 中，明确涉及的对外接口主要有：<interfaces>

## 条目分解
1. <identifier>
   - Owner: <primary_owner>
   - External interface: <endpoint / command / schema / exported API>
   - Evidence: <path / patch / title / body>
   - Confidence: <high / medium / low>
```

## 解读指引

- “模块”尽量写成用户能理解的业务 / 技术模块，不要只贴裸文件名。
- “风险”要讲原因，不要只给标签。
- “对外接口”优先写用户可调用或可依赖的公开 interface 名称，以及具体变化点，例如方法名、参数、返回结构、导出符号或命令参数。
- 当多条记录明显属于同一主题时，先做主题聚合，再展开逐条说明。

## 安全规则

- 不要把 issue 的创建人误写成代码变更责任人。
- 不要把 reviewer / tester 自动认定为 owner。
- 当 CLI 能力不够时，要明确说明你的回退方法或证据缺口。
- 当没有本地仓库上下文时，对“模块”和“对外接口”的判断要降低置信度。
