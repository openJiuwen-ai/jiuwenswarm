# S_05 · 项目 Git 服务（ProjectGitService）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_05 / project-git |
| 关联模块 | `jiuwenswarm/server/runtime/session/project_git.py`（2,209 行） |
| 最近一次修订日期 | 2026-10-22 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

- `ProjectGitService`（模块级单例）的全部契约：探测
  （`ensure_on_project_create` / `probe` / `status`）、初始化（`init`）、
  分支操作（`switch_branch` / `create_branch`）、提交（`commit`）、
  推送（`push`）。
- Git 子进程执行的安全边界（禁 shell、分支名校验、超时、编码、选项注入）。
- 探测结果 → `Project.git` 快照字段的持久化协议。
- merge / rebase / cherry-pick / revert 中间态（transient）的识别与写操作
  拒绝协议。
- 分支被 worktree 占用时的 stale / live 二分处置。

### 本规约不管什么

| 子主题 | 归属 |
|---|---|
| `Project.git` 快照字段的存储与落库（本模块只写值） | [[S_04]] |
| 调用本服务的创建时机（新建 auto init / 恢复轻量 probe） | [[S_04]] N12 |
| diff 内容生成与工作区变更监听 | [[S_06]] |
| dubious ownership 判定与提示文案（common 层共享工具） | `common/git_safe_directory.py` |
| `project.git.*` RPC 指令的参数解析层 | gateway Web 本地 handler |

### 关键背景：快照而非实时

`Project.git` 是**探测时刻的快照**（detected_at / branch / status /
is_dirty...），由本服务在探测 / 操作后写回（经 [[S_04]] `save_project`
落库）。它不是实时状态——「恢复 = 重新 probe」，持久化快照可能过期
（项目隐藏期间 .git 被删 / 分支被外部切换）。消费方拿快照做展示，
做决策前须 `status()` 现场核实。

## 不变量

- **N1 子进程执行安全边界**（模块 docstring :6-10 + `_run_git` :296-336）：
  (a) 禁止 `shell=True`，参数以 list 传递；(b) git 可执行文件用
  `shutil.which` 取**完整路径**，不依赖 PATH 中的 `"git"` 字符串；
  (c) 分支名一律先过 `git check-ref-format --branch`（:779，同时完成
  规范化——去除 `refs/heads/` 前缀）；(d) 路径必须来自**已登记 project**
  的 `project_dir`，不接受调用方任意路径；(e) push 的 `remote` 参数以
  `-` 开头拒绝（选项注入，:1969-1970）；(f) 显式 `encoding="utf-8",
  errors="replace"`——Windows locale（cp1252/cp936）会解码失败拖垮整个
  状态探测（:326-331）；(g) `check=False`，returncode 由调用方判断。
- **N2 超时分层与环境变量钳制**：写操作默认 10s（`GIT_COMMAND_TIMEOUT_SEC`
  = `JIUWEN_GIT_COMMAND_TIMEOUT_SEC`）、diff 10s、**push 60s**（涉网络）
  （:58-61）。`_env_float` 非法回退默认、有效但低于 0.1 **钳到下限**——
  防 `timeout=0` 立即 TimeoutExpired（:31-54）。超时统一错误码
  `GIT_COMMAND_TIMEOUT`（retryable），**不阻塞调用方主循环**。
- **N3 GitError 输出截断**：stdout / stderr 一律截断 4000 字符
  （`_GIT_OUTPUT_TRUNCATE`，:64，`to_dict` :87-88）——防巨型输出进
  日志 / RPC 响应。所有方法返回**结构化错误**（GitError：code / message /
  command / exit_code / stdout / stderr / hint / retryable / repo），
  不向上抛裸异常（FileNotFoundError / TimeoutExpired 在边界内消化）。
- **N4 transient 中间态二分**：`_is_transient_state`（:260-293）查
  `gitdir` 下 5 类 marker——`MERGE_HEAD`（merge）、`rebase-merge` /
  `rebase-apply`（rebase）、`CHERRY_PICK_HEAD`（cherry-pick）、
  `REVERT_HEAD`（revert）。**读操作（status / probe）返回
  `transient=true` 供展示**；**写操作（init / switch / create / commit /
  push）前置检查并拒绝**（`GIT_TRANSIENT_STATE`，retryable=False）。
  worktree 场景 `.git` 是文件（`gitdir:` 指针）——用 `absolute()` 而
  **非 resolve()**（不解析 symlink，防 worktree gitdir 与 git 内部路径
  不一致漏检，:271-278）。
- **N5 ensure_on_project_create 分支表**（:852-951）：`work_mode≠code` →
  `disabled`（**不探测**，work 项目无 git 语义）；code 下依次：git 缺失 →
  `git_missing`（retryable）；目录缺失 → `error(PROJECT_DIR_MISSING)`；
  已是仓库 → `ready`（**不重复 init**）；非空但非 git → `not_git`
  （**不自动 init**——尊重用户既有目录）；空目录（或仅 .git）→ 主动
  `git init`，`initialized_by_jiuwenswarm=True`。除 `disabled` 外全部经
  `_persist_probe_result` 写回快照。
- **N6 probe / status 持久化二分**：`probe`（persist=True）刷新快照
  （「恢复」的语义就是它）；`status`（persist=False）纯查询——高频调用
  不产生写放大。
- **N7 init 的新版 / 旧版 git 兼容**：`git init -b <branch>`；stderr 含
  `unknown switch` / `invalid option`（git < 2.28 不支持 `-b`）→ 回退
  `init` + `symbolic-ref HEAD refs/heads/<branch>`（**不能用
  `checkout -b`**——无 commit 的全新仓库上会失败，:1013-1016）。init
  成功后 `initialized_by_jiuwenswarm=True` + 全量状态持久化。
- **N8 快照写回协议**：`_persist_git_snapshot`（status 路径，:692）与
  `_persist_probe_result`（探测路径，:734）经 [[S_04]] `save_project`
  落库；**保存失败仅 warning 不抛**（git 操作本身已成功，快照丢失可
  再 probe 恢复）。probe 路径保留旧值的字段：`initialized_by_jiuwenswarm`
  / `detected_at` / `is_dirty`（非 init 路径不探测 dirty，:750-751）。
  status 字符串映射：ready / transient / not_git / git_missing /
  dubious_ownership / error（`_map_status_string` :763-776）。
- **N9 require_clean 只管已跟踪文件**：`switch_branch` 的
  `require_clean=True` 仅在 `staged / unstaged / conflicted` 非零时拒绝
  （`WORKTREE_DIRTY`），**untracked 不阻止**（:1113-1118）——与 git
  checkout 的实际行为一致（未跟踪文件不会被 switch 改写）。默认
  `require_clean=False`。
- **N10 worktree 占用的 stale / live 二分**（:364-408，:1212-1293）：
  stderr 含 `already used by worktree` / `already checked out` 时——
  **stale**（worktree 目录已删但 `.git/worktrees` 管理条目残留，如已
  解散团队）：执行 `git -c safe.directory=* worktree prune` 后**重试
  一次**；**live**（团队仍在运行）：**不自动 detach 占用者**——自动改
  其他 worktree 的 HEAD 会破坏运行中的团队 / agent 上下文，报错列出
  占用方，交用户显式解散或手动处理。主仓库本身（worktree list 第一项）
  被显式排除（:382-383）。
- **N11 commit 的注入与互斥**：message 经 stdin `git commit -F -` 注入
  （:309-310 docstring）——绕开命令行长度限制与转义问题；多行以 `\n`
  分隔。`stage_all` 与 `paths` **互斥**（BAD_REQUEST）；`stage_all`
  的**默认值推导归 handler 层**（未传 paths 时默认 True），service 层
  只接收明确 bool（:1576-1579，分层纪律）。错误码含
  `NOTHING_TO_COMMIT`（非 amend 且无改动）。
- **N12 push 安全默认**：`force=True` 使用 **`--force-with-lease`** 而
  非 `--force`（:1937，更安全的强推）；`delete` 与 `set_upstream` /
  `force` 互斥（BAD_REQUEST）；detached HEAD 未传 branch →
  `DETACHED_HEAD`；未设 upstream 未传 branch → `PUSH_NO_UPSTREAM`；
  远程拒绝 → `PUSH_REJECTED`。
- **N13 单例**：`get_project_git_service()`（:2196-2201）模块级单例
  （无状态类，单例仅为避免重复构造）；`reset_project_git_service`
  仅供测试。

## 接口契约

| 接口 | 契约要点 | 代表调用方 |
|---|---|---|
| `ensure_on_project_create`（:852） | N5 分支表；返回 `GitProbeResult`（status: disabled / ready / not_git / git_missing / transient / error） | project_store.py 新建项目联动（[[S_04]] N12）；app_web_handlers.py |
| `probe`（:954） | 重新探测 + 刷新快照（persist=True）；不 init | project_store.py 恢复项目联动；[[S_06]] |
| `status`（:960） | 纯查询（persist=False） | git_diff_status.py（[[S_06]]） |
| `init`（:965） | N7 兼容回退；`initial_branch` 过校验 | ensure / 显式 init 指令 |
| `switch_branch`（:1069） | N4 拒绝 / N9 / N10；目标分支 `show-ref --verify` 确认存在 | app_web_handlers.py（project.git.switch） |
| `create_branch`（:1328） | 基于 check-ref-format 校验创建 | app_web_handlers.py |
| `commit`（:1562） | N11；`commit_hash` 为短 hash | app_web_handlers.py（project.git.commit） |
| `push`（:1921） | N12；60s 超时 | app_web_handlers.py（project.git.push） |

返回值类型二分：探测 / 状态族返回 `GitRepoStatus`（含 error 字段，
**不抛**）；分支 / 提交 / 推送族返回 `GitOperationResult`（success +
repo_status + error）。`GitOperationError(RuntimeError)` 仅在 service
**内部**作为控制流使用（如 `_validate_branch_name`），对外边界全部
转换为结构化错误。

## 数据结构

### GitRepoStatus（:95-133）

is_git / repo_root / branch / head / detached / **transient** / upstream /
is_dirty / staged / unstaged / untracked / conflicted / local_branches /
remote_branches / error。

### GitError（:67-92）

code / message / command / exit_code / stdout（截断 4000）/ stderr（截断
4000）/ hint / retryable / repo。错误码全集（本 spec 各条目核实）：
`PROJECT_DIR_MISSING` / `GIT_NOT_FOUND` / `NOT_GIT_REPOSITORY` /
`GIT_DUBIOUS_OWNERSHIP` / `GIT_COMMAND_TIMEOUT` / `GIT_TRANSIENT_STATE` /
`BRANCH_INVALID` / `WORKTREE_DIRTY` / `BRANCH_HELD_BY_WORKTREE` /
`NOTHING_TO_COMMIT` / `DETACHED_HEAD` / `REMOTE_NOT_FOUND` /
`PUSH_REJECTED` / `PUSH_NO_UPSTREAM` / `GIT_COMMAND_FAILED` /
`BAD_REQUEST`。

### 环境变量

`JIUWEN_GIT_COMMAND_TIMEOUT_SEC`（10s）/ `JIUWEN_GIT_DIFF_TIMEOUT_SEC`
（10s）/ `JIUWEN_GIT_PUSH_TIMEOUT_SEC`（60s），均经 `_env_float` 钳制
（≥0.1，非法回退默认）。

## 执行路径

- **P1 探测**：`ensure_on_project_create` / `probe` → `_git_to_repo_status`
  （多次 git 调用聚合：rev-parse / symbolic-ref / status --porcelain /
  branch 列表）→ 持久化（N8）→ 返回。
- **P2 写操作统一前置**：目录存在 → git 存在 → 分支名校验（如涉）→
  `pre_status`（persist=False）→ transient 拒绝（N4）→ 特定前置
  （require_clean / 互斥 / remote 校验）→ 执行 → 后置状态聚合 →
  快照刷新（操作成功时）。
- **P3 错误消化**：FileNotFoundError / TimeoutExpired / 非零 returncode
  （含 dubious ownership 识别）→ 结构化 GitError → 塞进返回值，**不上抛**。

## 修改检查单

修改本模块前自查：

1. 新增 git 子命令：必须走 `_run_git`（N1 全部边界）；分支参数过
   `_validate_branch_name`；新自由字符串参数（remote / ref / path）评估
   选项注入面。
2. 新增写操作：前置 transient 检查不可省（N4）；成功路径评估是否刷新
   快照；失败路径快照不回写旧值。
3. 触碰超时：核对 `_env_float` 钳制语义；push 类网络命令不得用 10s 默认。
4. 触碰 worktree 处置：live 占用**不得**引入自动 detach（N10 rationale）。
5. 触碰快照字段：与 [[S_04]] `Project.git` 字段表同步；保留旧值字段
   （initialized_by_jiuwenswarm / detected_at / is_dirty）语义不可破坏（N8）。
6. 新增返回值：读族 → GitRepoStatus；写族 → GitOperationResult；错误
   消化在边界内完成（不新增裸异常外抛路径）。
7. 修改 ensure 分支表（N5）：对照 project_store 的调用方
   （新建 auto init / 恢复仅 probe）联动测试。

## 与其它 spec 的关系

- [[S_04]]：`Project.git` 快照的存储落库走 `save_project`；创建 / 恢复的
  git 联动时机由 project_store 决定。
- [[S_06]]：`git_diff_status` 消费 `status()`（persist=False）做 diff 上下文
  判定；diff 命令超时用独立的 `GIT_DIFF_TIMEOUT_SEC`。
- [[S_03]]：会话 metadata 的 `mode` 决定项目是否走 git 语义（work 模式
  disabled 分支表）。
