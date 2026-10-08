# Standalone PR Review Loop

独立 / 自主模式适用于用户直接要求 reviewer 检视 GitCode PR，或 cron / trigger 定时检视 PR。

## 工作区选择

`gitcode-repo.json` 出厂无默认仓库。开工前必须确定：

- `--workspace <WS>`
- `repo-root`
- `target-project` 是 `upstream` 还是 `fork`

来源优先级：

1. 用户指定 PR URL、owner/repo、workspace 名。
2. cron/webhook prompt 指定目标仓。
3. `workspaces[]` 中按 owner/repo 或 `local_repo.path` 匹配。
4. 无匹配则先补配置，不能猜 `workspaces[0]`。

## 作者过滤

当任务限定检视某人的 PR：

1. 必须确认 GitCode login，不能用中文姓名或未经验证的 upstream owner。
2. 用 `pr_creator.py --list --author <login> --target-project upstream --workspace <WS> --state open`。
3. 对每个候选 PR 再核对 `user == login`，不匹配一律剔除。

## 仓内检视标准

每个 PR collect 之后、检视之前，读取 `context.json` 的 `project_context.repo_review_standards` 并对照执行（规则见 [workflow.md §仓内检视标准](workflow.md)）：清单非空必须逐个读取；被本 PR 修改的标准按 PR base（目标分支）版本读取，并在行评中提示维护者确认。

## 去重

周期性检视“未检视过的 PR”时，远端评论是唯一事实来源。禁止用会话记忆判断是否已检视。

判定已检视的信号：

- 评论正文含 `[Must Fix]` / `[Should Fix]` / `[Nice to Have]`
- 评论正文含“审查结论”“检视意见”“PASS”
- 评论正文含 `<!-- dev-reviewer:CR-xxx -->`
- 存在本分身对应 GitCode 账号发出的行评/讨论区评论

已检视则跳过并在最终摘要记录 PR 号和原因。只有 PR head 有新提交且任务明确要求复检，才重新检视。

并发触发（push webhook 与 cron 撞车、PR 快速连续更新）时，最终以 `post-comments` 的逐条远端刷新 + 内容指纹去重为准，不会重复发布检视意见（规则见 [gitcode-comments.md §发布](gitcode-comments.md)）。

## 汇报格式

最终回复分两块：

- 本次新检视的 PR：列出 PR 号、评论数量、Must/Should/Nice 统计。
- 已跳过的 PR：列出 PR 号和“已有检视意见，跳过”等原因。
