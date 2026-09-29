# Dev Reviewer Workflow

本文件承接 `SKILL.md` 中的执行细节。`SKILL.md` 是入口和硬门禁，本文件是 runner 操作手册。

## 前置条件

- 已明确审查范围：PR URL、本地 `--base/--head`、commit/diff，或用户给出的文件列表。
- 已确认或可合理推断 `<module>`；多模块且无法推断时先询问。
- 在执行 runner 前，shell 工作目录为 `skills/dev-reviewer/`。
- 所有 runner 子命令都带 `--module <module>` 与 `--repo-root <repo-root>`；`collect` 可用 `--repo` 作为 repo-root 默认值。

## 仓库对齐

在 `collect` 前，先按 `aidlc-common/references/repo-workspace-sync.md` 核对 repo-root：

1. 若 `local_repo.path` 未配置或目录不是 git 仓，仅用 `gitcode-repo/scripts/repo_manager.py --ensure-clone --workspace <name>` 落盘。
2. 对 PR 审查，用 `gitcode-repo/scripts/pr_creator.py --number <N>` 取得 PR 的 base/head sha。
3. 本地 `HEAD` 必须对齐 PR head；不一致时 fetch 并 checkout 到 PR head 分支或 sha。
4. 对齐成功后再 collect；`--base` / `--head` 必须与 PR 元数据一致。

## 标准流程

```powershell
python scripts/code_review_runner.py collect --pr "<PR_URL>" --issue "<ISSUE>" --repo "<LOCAL_REPO>" --module "<MODULE>"
python scripts/code_review_runner.py init-review --module "<MODULE>" --repo-root "<LOCAL_REPO>"
python scripts/code_review_runner.py report --module "<MODULE>" --repo-root "<LOCAL_REPO>"
```

本地 diff 审查：

```powershell
python scripts/code_review_runner.py collect --pr local --repo "<LOCAL_REPO>" --module "<MODULE>" --base "<BASE_REF>" --head "<HEAD_REF>"
```

仅当用户或 Leader 明确允许审查本地脏工作区时，才允许：

```powershell
python scripts/code_review_runner.py collect --pr local --repo "<LOCAL_REPO>" --module "<MODULE>" --allow-working-tree
```

## 仓内检视标准

`collect` 完成后检查 `doc/<module>/review/context.json` 的 `project_context.repo_review_standards`：

- **非空**：逐个读取列出的文件（SKILL.md 优先，`references` 按需），检视时对照仓内标准执行；`kind=repo-instructions`（AGENTS.md / CLAUDE.md）作为补充上下文。`report` 生成的 review.md 会列出本次发现的标准。
- **标准被本次 PR 修改**（条目 `modified_by_pr=true`，或 collect 警告提示）：该标准改按 PR **base（目标分支）** 版本读取（`git -C "<repo-root>" show <base>:<path>`），并在 limitations 与行评中提示维护者确认标准变更；base 无该文件时标准为本 PR **新增**，本次检视按通用标准执行，只在行评中提示维护者确认新增的标准。
- **优先级**：流程、门禁、行评格式、location 规则以 dev-reviewer 为准；技术判断（什么算缺陷、架构边界、规范取舍）以仓内标准为准；冲突记入 limitations。
- **清单为空**：按通用五维标准检视，无需额外动作。
- 目标仓的标准未放在约定槽位时，用 `collect --review-skill <repo 相对路径>` 显式指定（可重复传入）。

## 可选自动化

```powershell
python scripts/code_review_runner.py lint --module "<MODULE>" --repo-root "<LOCAL_REPO>"
python scripts/code_review_runner.py security-scan --module "<MODULE>" --repo-root "<LOCAL_REPO>" --merge-result
python scripts/code_review_runner.py performance-evidence --module "<MODULE>" --repo-root "<LOCAL_REPO>"
```

这些命令只收集证据；最终结论仍由 reviewer 写入 `review/result.json`。

## 交付物

- 临时证据目录：`doc/<module>/review/`
- 审查数据：`doc/<module>/review/result.json`
- 主交付报告：`doc/<module>/review.md`
- GitCode 评论文件：`doc/<module>/review/comments/*.md`
- GitCode 评论清单：`doc/<module>/review/comments/manifest.json`

`review.md` 只能由 `report` 生成，Agent 只手改 `review/result.json`。

## 超时

- `collect` / `init-review` / `resolve-positions` / `validate-comments` / `render-comments` / `report`：外层 60s。
- `lint` / `security-scan` / `performance-evidence`：按命令输出登记超时或跳过，不能把超时误报为人工确认的 PASS。
- 超时后记录输出、退出码和 limitations；不得宣称 Gate 已通过。
