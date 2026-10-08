# GitCode Review Comments

本文件定义 `dev-reviewer` 与 `gitcode-repo/scripts/pr_commenter.py` 的评论发布契约。

## 评论生成

所有 Must Fix / Should Fix 默认生成独立 Markdown 长行评：

```powershell
python scripts/code_review_runner.py validate-comments --module "<MODULE>" --repo-root "<LOCAL_REPO>"
python scripts/code_review_runner.py render-comments --module "<MODULE>" --repo-root "<LOCAL_REPO>"
```

产物：

- `doc/<module>/review/comments/CR-001.md`
- `doc/<module>/review/comments/manifest.json`

每条评论底部带隐藏签名：`<!-- dev-reviewer:CR-001 -->`，用于去重和失败恢复。

## 评论正文

推荐在 finding 中填写 `comment`：

```json
{
  "title": "`error` 字段对 falsy 值存在误判",
  "scenario": "当 error 为 0、[]、{}、False 等 falsy 但非 None 的值时会误判。",
  "examples": ["error=0 -> 误判为有错误", "error=[] -> 误判为有错误"],
  "impact": "可能导致误触发熔断器中断 Agent 执行。",
  "fix": "先排除 None，仅对非空字符串或真实异常对象判定为错误。",
  "verification": "补充 falsy error 参数化测试。",
  "code": "err = payload.get(\"error\")\nif err is not None:\n    ..."
}
```

缺失 `comment` 时，runner 会从 `issue` / `risk` / `recommendation` / `minimal_patch_example` 派生正文。Must Fix 仍须具备实质字段，避免空洞长评。

## 发布

默认 dry-run，不调用写 API：

```powershell
python scripts/code_review_runner.py post-comments --number <N> --module "<MODULE>" --repo-root "<LOCAL_REPO>" --config gitcode-repo.json --workspace <WS> --target-project upstream
```

真实逐条发布必须显式加 `--execute`：

```powershell
python scripts/code_review_runner.py post-comments --number <N> --execute --module "<MODULE>" --repo-root "<LOCAL_REPO>" --config gitcode-repo.json --workspace <WS> --target-project upstream
```

规则：

- inline finding 必须带 `--comment-file --path --position --need-to-resolve`。
- `(architecture)` / `(documentation)` finding 可发讨论区，必须带 `--allow-review-discussion-comment`。
- 每个 finding 单独文件、单独 API 调用；禁止把多条 findings 合并成一条评论。
- 真实发布时逐条先刷新远端评论状态（每次发布前重新拉取，防止两次检视并发触发导致重复发评）：同一 CR-id 已存在且未解决时跳过（`skipped_existing`）。
- 内容指纹（路径 + 正文，忽略 CR-id 与行号）与既有未解决评论重复时跳过（`skipped_duplicate`），覆盖两次检视对同一问题编号漂移的场景。
- 已解决（resolved）的旧评论不参与去重；复检再次发现同一问题时可重新发布，并配合 `--reopen` 闭环。
- 单条失败或远端刷新失败后停止继续发布，manifest 写入失败原因；重跑时跳过已发布项。

## 复检闭环

复检时用 `gitcode-repo/scripts/pr_creator.py --number <N>` 拉取评论与 `discussion_id`。

- 已修复：`pr_commenter.py --number <N> --resolve <discussion_id>`
- 未修复：保持 unresolved，必要时 `--reopen <discussion_id>`
- 仅当 `unresolved_discussions_count == 0` 且无遗留 Must/Should Fix，才可 `--approve`
