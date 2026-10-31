# S_06 · Git Diff 监控与状态聚合（GitDiffWatcherRegistry / DiffStatusService）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_06 / git-diff-watch |
| 关联模块 | `jiuwenswarm/server/runtime/session/git_diff_watcher.py`（1,310 行）<br>`jiuwenswarm/server/runtime/session/git_diff_status.py`（1,058 行） |
| 最近一次修订日期 | 2026-10-22 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

- `GitDiffWatcherRegistry`（模块级单例）的全部契约：订阅生命周期
  （`add_watch` / `remove_watch` / `update_files` / `update_detail`）、
  首次快照原子提交（`commit_initial_*`）、按 project 聚合的轮询任务
  （`_poll_loop`）、三层 fingerprint 去重、事件推送与孤儿回收。
- `DiffStatusService`（模块级单例）的三个查询契约：
  `get_project_diff_status` / `get_turn_diff_list` / `get_turn_diff_detail`，
  及其错误语义（transient / no-git fallback / 历史回放降级）。
- `get_session_extra_history_roots`：多根历史收集协议（team / member /
  worktree / sub-agent 四类根）。
- 事件 payload 构造 helper 家族（`extract_files_from_status` /
  `build_summary_entry` / `file_map_to_dict_no_hunks` 等）——handler 与
  registry 共用，保证 schema 访问逻辑单点实现。
- 四个推送事件的 payload 契约：`project.git.diff_changed` /
  `diff_files_changed` / `diff_detail_changed` / `project.git.error`。

### 本规约不管什么

| 子主题 | 归属 |
|---|---|
| diff 计算本体（`get_git_diff` / `get_turn_diff_summaries` 的实现、file_ops / change_set 快照格式） | `server/utils/diff_service.py`（server 层，明确不在本模块） |
| `project.git.*` RPC 指令的参数解析、路由与响应包装 | `gateway/channel_manager/web/git_ws_handler.py` |
| repo 状态探测（is_git / branch / transient 判定） | [[S_05]] `ProjectGitService.status` |
| 事件通道 `send_event` 的底层投递 | `gateway/channel_manager`（web channel） |
| `Project` 对象的存在性 / work_mode 校验 | [[S_04]] + handler 层 |

### 关键背景一：按 project 聚合的观察者模式

同一 project 可能同时挂多个 watcher（多个 WebSocket 连接、同一连接的
summary / files / detail 多层订阅）。Registry **每个 project 只跑一个轮询
任务**（`_poll_tasks[project_id]`），一轮计算一次 diff，所有 watcher 共享
结果、各自比对 fingerprint 决定是否推送——昂贵的 Git 子进程不随 watcher
数量线性复制（模块 docstring §2.5 / §2.7）。

### 关键背景二：current 项目级、last_turn session 级

`current`（工作区 diff）与 session 无关，一轮内**只算一次**跨 session
复用；`last_turn`（上一轮对话 diff）是 session 级的，按 `session_id` 分组
独立计算（`_compute_and_push` docstring :876-885）。

### 关键背景三：首次快照由 handler 计算，Registry 只做播种

订阅的首次响应（`diff_watch` / `diff_files_watch` / `diff_detail_watch`
的 RPC 响应）由 handler 直接调 `DiffStatusService` 计算并发送；Registry
随后通过 `commit_initial_*` 把首次快照**种进 fingerprint** 并唤醒轮询。
若不播种，poll 首轮 fingerprint 从空串变为非空，必然推送一条与首次响应
内容相同的冗余事件（`seed_summary_fingerprint` docstring :571-576）。

## 不变量

- **N1 三索引 + 单轮询任务**：Registry 维护
  `_watches`（watch_id → GitDiffWatch）、`_ws_watches`（`id(ws)` →
  watch_id 集合）、`_project_watches`（project_id → watch_id 集合）三个
  索引与 `_poll_tasks`（project_id → Task）。**无 watcher 的 project 不
  启动轮询**（`mark_dirty` :563-565 先查 `_project_watches` 非空）；
  project 最后一个 watcher 被移除时同步取消轮询任务
  （`_remove_watch_internal` :726-732）。
- **N2 三层订阅独立去重**：同一 `watch_id` 下 summary / files / detail
  三套 fingerprint 各自独立比对（`GitDiffWatch` docstring :44-47）：
  summary = 统计信息指纹（含 `is_dirty`，:112-115）；files = 文件列表
  指纹（按 `files_source` 区分 current / last_turn）；detail = 已订阅
  文件的 hunk 内容指纹（按 `detail_source` + `detail_files`）。fingerprint
  = md5 + 稳定序列化（set 一律 `sorted` 后入指纹，:88-184）。**切换
  `files_source` / `detail_source` / `detail_files` 仅更新字段 +
  `mark_dirty`，不取消 / 重建底层 watcher 任务**（§5.2.6）。
- **N3 首次快照播种协议**：`update_files` / `update_detail` 切换 source
  时**清空对应 fingerprint 但不 mark_dirty**（:332-334 / :463-464）——
  handler 在计算首次快照 + 发送响应之后调 `commit_initial_files` /
  `commit_initial_detail`（内部 = seed fingerprint + mark_dirty），
  避免 poll 首轮「空串 → 非空」必然触发冗余事件。`commit_initial_*`
  是封装「seed + mark_dirty」两步的原子提交接口（:605-609 注释：让
  handler 只提交首次快照，fingerprint 算法变更不再影响 handler）。
  watch 已不存在时 no-op（:624-625）。
- **N4 订阅切换的原子回滚**：`add_watch` 提供 `on_initial` 回调——回调
  抛错时自动 `remove_watch` 并向上抛（不吞异常，:260-266），取代旧的
  「add_watch + 兜底 mark_dirty 定时器」方案，watcher 创建与首次快照
  播种是同一原子事务。`update_files_with_restore` /
  `update_detail_with_restore` 对称封装：snapshot 旧状态 → update 切换
  → `on_snapshot` 回调（handler 计算首次快照并发送）→ 回调抛错自动
  `restore_*_state`（:382-431 / :514-554）。裸 `snapshot_*_state` /
  `restore_*_state` 保留为公开接口仅为兼容既有调用方与测试。
- **N5 空 session_id 强制关闭 include_last_turn**（:234-240）：
  `effective_include_last_turn = bool(include_last_turn) and
  bool(session_id)`。原因：`_compute_and_push` 用 `if session_id:` 判定
  是否计算 last_turn，空串会被静默跳过；若 include_last_turn 仍为 True，
  summary fingerprint 会包含 `last_turn.stats=None` 且事件 payload 语义
  不一致（前端误以为有 last_turn）。watch_id 格式
  `gitdiff_{project_id}_{session_id or 'noss'}_{uuid12}`（:241）。
- **N6 轮询唤醒与 debounce**（`_poll_loop` :782-869）：等待阶段用
  `asyncio.wait(wake_tasks, timeout=POLL_INTERVAL_SEC,
  return_when=FIRST_COMPLETED)`——pending 任务**取消并 gather 等待完成**
  （防 "Task was destroyed but it is pending" 泄漏警告，:816-820）；
  被唤醒（非超时）时先 `sleep(DEBOUNCE_SEC)` 合并连续 `mark_dirty`
  （连续 switch_branch / 文件保存场景防密集 Git 子进程），随后统一
  `clear()` 所有 wake_event。`CancelledError` / `RuntimeError` 路径同样
  取消并回收全部 wake_tasks。
- **N7 错误三分类**：
  (a) **结构性错误**（`_STRUCTURAL_ERROR_CODES` =
  {NOT_GIT_REPOSITORY, PROJECT_DIR_MISSING}）：推送 error 事件后
  **轮询任务退出但 watcher 保留**（:844-857），等 `mark_dirty` 重建任务
  （`mark_dirty` 会 `_ensure_poll_task`，使 `project.git.init` 等写操作
  能恢复监控，:556-565）；
  (b) **临时性错误**（GIT_COMMAND_FAILED / TIMEOUT 等）：
  `consecutive_errors` 计数，backoff = `min(5×count, 15)` 秒，推送
  error 事件后重试（:858-869）；
  (c) **推送失败**：`push_failures` 连续达到 `MAX_PUSH_FAILURES=3`
  回收 watcher（`_on_push_failure` :1234-1248）——兜底断连与
  `add_watch` 竞态产生的孤儿（ws 已断开但 `cleanup_ws` 未覆盖），
  避免对已关闭连接无限推送。推送成功即清零计数。
- **N8 分层计算与线程隔离**（`_compute_and_push` :871-1050）：
  (a) 项目已删 / 隐藏 → `cleanup_project` 后返回（:889-898）；
  (b) `current` 用所有 watcher 的需求**并集**计算一次（files 需求 =
  任一 `files_source=="current"` 或 detail current 非空；hunk 需求 =
  detail current 文件并集），Git 命令一律 `asyncio.to_thread` 包裹防阻塞
  事件循环（:924-938）；(c) `last_turn` 按 session 分组独立计算，且
  **传入 `get_session_extra_history_roots(session_id)`**——否则多 member
  在 git worktree 工作时的改动会被漏读，实时推送的 last_turn 数据残缺
  （:980-984 注释）；(d) 同一 project 的所有 session 共享同一 Git 仓库，
  一次失败必然对所有 session 失败，无需逐 session 容错（:929-930）；
  (e) `cache_bust=False` 读 project：轮询周期 2 秒强制读盘代价高，同进程
  内 hide/remove 会经 store 写路径刷新缓存（:887-888）。
- **N9 归属校验**：`remove_watch` / `update_files` / `update_detail` /
  snapshot 族均支持 `expected_ws`（校验 watch 归属连接）与
  `expected_project_id`（校验 watch 归属项目）——不匹配**按不存在处理**
  （返回 None / no-op），防同一连接内交叉项目订阅导致首次快照与后续
  事件错配（:315-317）。
- **N10 清理三入口**：`cleanup_ws`（断连，`app_gateway` 侧调用）、
  `cleanup_project`（项目删 / 隐藏，`_compute_and_push` 自检触发）、
  `_remove_watch_internal`（持锁内部移除）。三者均为**同步方法**，在
  event loop 内序列化执行；dict 遍历用 `list()` 快照规避迭代期间 size
  变更，与持锁的 add / remove / update 可能交错但不破坏结构
  （:671-712）。`shutdown()` 取消全部轮询任务（单例重置 / 进程退出）。
- **N11 DiffStatusService 错误语义**（`get_project_diff_status`
  :714-841）：
  (a) **no-git fallback**：repo 错误码 ∈ {NOT_GIT_REPOSITORY,
  GIT_NOT_FOUND} 时不抛错——`repo_root` 回退 project_dir、branch / head /
  transient 置空、`is_git=False`、`current=None`（Jiuwen file-op 历史
  仍可支撑 diff 视图，`_supports_no_git_fallback` :633-638）；
  (b) 其他 repo 错误 → 抛 `GitOperationError`；
  (c) **transient**：`current=None` 但仍成功返回 `repo.transient=true`
  （供展示，不阻断）；
  (d) `get_git_diff` / `get_turn_diff_summaries` 的异常**向上抛**——
  尤其 last_turn 路径，若静默返回空数据，客户端会误以为订阅成功但
  拿不到内容（:816-824 注释：让 handler 感知并触发订阅状态回滚）。
- **N12 历史回放的 repo 上下文降级**：`_repo_context_for_history`
  （:681-703）**不抛任何错**——transient / timeout / command_failed 与
  file_ops + change_set 历史快照无关，失败时用 project_dir 兜底；
  `_historical_repo_context`（:583-592）让 turn **持久化的**
  repo_root / branch / base_head 优先于当前 fallback。设计原则：历史
  轮次的 repo 上下文已持久化在 change_set entry，当前 git 状态只是
  fallback。对比：`_repo_context_from_status(reject_transient=True)`
  会抛 `GIT_TRANSIENT_STATE`（供需要现场一致性的调用方）。
- **N13 is_dirty 三源合一**（`_convert_current_diff` :510-549）：
  `is_dirty = stats.files_changed > 0 or has_untracked or repo_is_dirty`。
  `repo_is_dirty` 来自 `git status --porcelain`（[[S_05]] GitRepoStatus），
  涵盖 untracked 与已跟踪改动，是**最权威判定源**；`include_files=False`
  时 files 为空、无法检测 untracked，用 `repo_is_dirty` 兜底——summary
  首次订阅正是 `include_files=False`。
- **N14 只转换不改写**：`DiffStatusService` 复用
  `server/utils/diff_service.py` 的 `DiffService` 能力，只做 schema 转换
  （camelCase → snake_case）、空状态与 transient 语义转换、与
  ProjectGitService repo 状态合并，**不修改 DiffService 原始返回**——
  避免破坏 TUI `command.diff` 等既有消费方（类 docstring :706-712）。
  第一版不含 staged / unstaged 分类。

## 接口契约

### 订阅生命周期（handler → Registry）

| 方法 | 契约要点 |
|---|---|
| `add_watch(ws, project_id, session_id, scope="summary", *, include_last_turn=True, on_initial=None)` | 创建 watcher 并入三索引。`on_initial` 为 `async (watch) -> dict`（返回首次快照 status_dict）：成功 → 内部 `commit_initial_summary`；抛错 → 自动 `remove_watch` + 向上抛。`on_initial=None` 保持旧行为（调用方自行 commit）。空 session_id 强制关闭 include_last_turn（N5） |
| `remove_watch(watch_id, *, scope="all", expected_ws=None)` | `scope="all"` 移除整个 watcher；`"files"` 仅置空 files_source + fingerprint；`"detail"` 仅置空 detail_source / detail_files + fingerprint（后两者保留 summary 订阅）。返回被移除的 watch 或 None |
| `update_files(watch_id, source, *, expected_ws, expected_project_id)` | 置 `files_source`、清空 files fingerprint，**不 mark_dirty**（N3）。source ∈ {"current", "last_turn"} |
| `update_files_with_restore(watch_id, source, *, expected_ws, expected_project_id, on_snapshot)` | 原子切换 + 失败自动回滚（N4）。`on_snapshot` 为 `async (watch) -> None` |
| `update_detail(watch_id, source, files, ...)` / `update_detail_with_restore(...)` | 对称的 detail 版本；`detail_files` 存为 set |
| `snapshot_files_state` / `restore_files_state` / `snapshot_detail_state` / `restore_detail_state` | 兼容公开接口；新代码优先用 `*_with_restore` |
| `cleanup_ws(ws)` / `cleanup_project(project_id)` / `shutdown()` | 清理三入口（N10） |

### 提交 / 唤醒（handler → Registry）

| 方法 | 契约要点 |
|---|---|
| `commit_initial_summary(watch_id, status_dict)` | seed summary fingerprint + mark_dirty；watch 不存在 no-op |
| `commit_initial_files(watch_id, status_dict, source)` | seed files fingerprint + mark_dirty |
| `commit_initial_detail(watch_id, status_dict, source, detail_files)` | seed detail fingerprint + mark_dirty |
| `seed_*_fingerprint` | 内部接口（被 commit_initial_* 封装），handler 不直接调 |
| `mark_dirty(project_id, *, watch_id=None)` | 只唤醒仍有 watcher 的 project；结构性错误暂停后**重建**轮询任务 |
| `set_channel(channel)` | 绑定事件发送句柄（`app_gateway` 启动时调用，:2128-2135） |
| `get_git_diff_watcher_registry()` / `reset_git_diff_watcher_registry()` | 单例获取 / 测试重置（reset 会 shutdown） |

### 查询（handler / registry → DiffStatusService）

| 方法 | 契约要点 |
|---|---|
| `get_project_diff_status(*, project, session_id=None, include_files=False, include_hunks=False, hunk_paths=None)` | 聚合 current + last_turn。`include_hunks=True` 隐含 `include_files=True`；`hunk_paths` 限定 hunk 计算的文件集。错误语义见 N11。返回 `ProjectGitDiffStatus`（调用方 `to_dict()` 作 payload） |
| `get_turn_diff_list(*, project, session_id, limit=50, cursor=0)` | 历史轮次摘要列表（不含 hunk），cursor / limit 分页，返回 `{turns, cursor, next_cursor, has_more, limit, total}`。repo 上下文走降级路径（N12） |
| `get_turn_diff_detail(*, project, session_id, turn_index=None, change_set_id=None, include_files=True, include_hunks=True)` | 指定轮次详情，**优先按 change_set_id 查询**；无匹配返回 None |

### 多根历史收集

`get_session_extra_history_roots(session_id)`（:293-398）返回该 session
的 file_ops 额外收集根列表，四类来源：

1. **team_file_monitor_roots**：session metadata 中显式登记的监控根；
2. **team workspace 推导**：team-workspace 父目录 + team_name 反查 home
   目录（`openjiuwen.agent_teams.paths` 三函数）；
3. **session worktree**：团队会话的 git worktree 根；
4. **sub-agent 工作区**：`_discover_sub_agent_workspaces`（:401-441）
   扫描 `<agent_workspace>/sub_agents/<session_id>_sub_*` 目录——单 agent
   模式父会话无 team_name 时靠此发现 sub-agent 的 `.agent_history`。

读 metadata 用 `cache_bust=True` + `enable_writeback=False`（:309-312：
读路径不产生写副作用）。member 名单来自历史记录中的
`team.member.spawned` 事件（`_session_team_member_names` :31-70）。

### 事件 payload helper（handler 与 registry 共用）

| 函数 | 用途 |
|---|---|
| `extract_files_from_status(status_dict, source)` | 从 `to_dict()` 结果提取 current / last_turn 的 files 映射；分支不存在返回 None |
| `build_summary_entry(current)` / `build_turn_summary_entry(last_turn)` | summary 事件条目（`files` 固定 `{}`——summary 层只关心统计） |
| `file_entry_to_dict_no_hunks(entry)` / `file_map_to_dict_no_hunks(files_dict)` | files 事件格式（去 hunk，接受已序列化 dict 避免反复对象重建） |

## 数据结构

### GitDiffWatch（watcher 状态）

| 字段 | 类型 / 取值 | 说明 |
|---|---|---|
| `watch_id` | str | `gitdiff_{project_id}_{session_id or 'noss'}_{uuid12}` |
| `project_id` / `session_id` / `ws` | str / str / Any | 归属三元组 |
| `scope` | str = "summary" | 订阅层级 |
| `files_source` / `detail_source` | None / "current" / "last_turn" | files / detail 层数据源 |
| `detail_files` | set[str] | detail 层已订阅文件 |
| `include_last_turn` | bool | 是否监控 last_turn（N5 钳制后） |
| `last_summary_fingerprint` / `last_files_fingerprint` / `last_detail_fingerprint` | str | 三层去重指纹（空串 = 待播种） |
| `wake_event` | asyncio.Event | 唤醒轮询 |
| `push_failures` | int | 连续推送失败计数（N7c） |

`GitDiffFilesState` / `GitDiffDetailState`：frozen snapshot（回滚用）。

### DiffStatusService 返回对象族（git_diff_status.py :73-243）

| dataclass | 关键字段 |
|---|---|
| `DiffStats` | files_changed / lines_added / lines_removed |
| `DiffHunk` | old_start / old_lines / new_start / new_lines / lines |
| `DiffFileEntry` | file_path（相对 repo_root）/ status（modified\|added\|deleted\|renamed\|missing）/ 行数统计 / is_binary / is_new_file / is_deleted_file / is_untracked / is_large_file / is_truncated / hunks |
| `DiffSummary`（current） | is_dirty / stats / files / kind="working_tree" |
| `DiffTurnSummary`（last_turn） | turn_index / timestamp / user_prompt_preview / stats / files / kind="conversation_turn" / change_set_id / request_id / assistant_message_id / user_message_id / status（change_set 族字段仅在 change_set_id 非空时序列化） |
| `DiffRepoInfo` | is_git / repo_root / branch / head / transient |
| `ProjectGitDiffStatus` | project_id / session_id / work_mode / repo / current / last_turn / generated_at |

全部 `@dataclass(slots=True)`，`to_dict()` 输出 snake_case。

### 推送事件 payload

| 事件 | payload 要点 |
|---|---|
| `project.git.diff_changed` | scope="summary"；`current` / `last_turn` 条目经 `build_*_entry`（files 固定 `{}`）；`include_last_turn=False` 时 last_turn 固定 None |
| `project.git.diff_files_changed` | source=watch.files_source；files 经 `file_map_to_dict_no_hunks`；source="last_turn" 时附加 change_set 绑定元数据（change_set_id / turn_index / assistant_message_id / user_message_id / request_id / status） |
| `project.git.diff_detail_changed` | source=watch.detail_source；files 仅含 `detail_files` 中路径（缺失路径值为 None）；source="last_turn" 时同样附加绑定元数据 |
| `project.git.error` | detail 从 `GitOperationError.git_error`（GitError dataclass）提取，兜底 exc 属性，最后回落 `GIT_COMMAND_FAILED`；含 code / message / command / exit_code / stdout / stderr / hint / retryable |

三者共享 `revision` 字段：`_build_revision("gitdiff", ts:fingerprint8)`
（prefix:ts:fingerprint 前 8 位）。

### 常量

| 常量 | 值 | 说明 |
|---|---|---|
| `POLL_INTERVAL_SEC` | 2.0 | 常规轮询周期 |
| `DEBOUNCE_SEC` | 0.3 | 唤醒后合并窗口（N6） |
| `ERROR_BACKOFF_SEC` | 5.0 | 临时错误退避基数（上限 3×） |
| `MAX_PUSH_FAILURES` | 3 | 孤儿 watcher 回收阈值 |
| `_STRUCTURAL_ERROR_CODES` | {NOT_GIT_REPOSITORY, PROJECT_DIR_MISSING} | 暂停轮询保留 watcher 的错误码 |

## 执行路径

- **P1 首次订阅（summary）**：`git_ws_handler` 收到 `diff_watch` →
  `add_watch(..., on_initial=回调)` → 回调内调
  `get_project_diff_status`（session_id 传入以覆盖 last_turn）并发送首次
  响应 → Registry 内部 `commit_initial_summary`（seed + mark_dirty）→
  轮询任务被唤醒（经 DEBOUNCE 合并）进入常规循环。回调抛错 → 自动
  `remove_watch` + handler 发送错误响应。
- **P2 订阅切换（files / detail）**：`diff_files_watch` /
  `diff_detail_watch` → `update_*_with_restore`（snapshot → 切 source →
  `on_snapshot` 回调计算首次快照并发送 → `commit_initial_files` /
  `commit_initial_detail`）→ 回调抛错自动回滚旧订阅状态。
- **P3 常规轮询**：`_poll_loop` 每轮 `_get_watches_for_project`（空则
  退出）→ `_compute_and_push`：project 存活检查（删 / 隐藏 →
  cleanup_project）→ 项目级 current 并集计算（to_thread）→ 按 session
  分组算 last_turn（含 extra_history_roots）→ 逐 watch 三层 fingerprint
  比对 → 变化者推送对应事件 → 等待 wake_event 或 2s 超时。
- **P4 错误路径**：结构性错误 → error 事件 + 轮询退出（watcher 保留，
  等 mark_dirty 复活）；临时错误 → error 事件 + 退避重试；推送失败 →
  计数，连续 3 次回收 watcher。
- **P5 生命周期收尾**：WebSocket 断连 → `cleanup_ws`（gateway 侧）；
  项目删除 / 隐藏 → `cleanup_project`（轮询自检或 store 路径触发）；
  进程退出 / 单例重置 → `shutdown`。

## 修改检查单

改动本两文件前自问：

1. 新增推送事件或 payload 字段时，是否同步更新 fingerprint 覆盖范围
   （N2）——否则字段变化不触发推送？
2. 是否破坏「首次快照由 handler 计算 + commit_initial_* 播种」协议
   （N3）——在 Registry 内直接推送首次数据会造成双份？
3. 订阅切换路径是否走了 `*_with_restore` 原子封装（N4）——裸
   update + 手工 snapshot/restore 的一致性维护会泄漏到 handler？
4. 轮询等待 / 取消路径是否保留 pending 任务回收（N6）——漏 gather 会
   产生 "Task was destroyed" 泄漏？
5. 新错误码是否需要归入 `_STRUCTURAL_ERROR_CODES`（N7）——临时性错误
   误判为结构性会让 watcher 永久静默？
6. last_turn 计算是否传入 `get_session_extra_history_roots`（N8c）——
   漏传会丢 team / worktree / sub-agent 的改动？
7. `DiffStatusService` 是否保持「只转换不改写」（N14）——原地修改
   DiffService 返回会破坏 TUI 消费方？
8. 历史回放路径是否误用了 `reject_transient=True`（N12）——transient
   不应阻断 file_ops 历史预览？

## 与其它 spec 的关系

- [[S_05]]：repo 状态（is_git / branch / transient / is_dirty）来自
  `ProjectGitService.status`（persist=False）；`GitOperationError` /
  GitError 结构化错误定义同属 S_05。
- [[S_04]]：`get_project_by_id` / 项目隐藏（hidden）判定；项目删除 /
  隐藏触发 `cleanup_project` 的联动。
- [[S_03]]：`get_session_extra_history_roots` 读 session metadata
  （team_name / team_file_monitor_roots）。
- [[S_02]]：file_ops / change_set 历史与 `.agent_history` 的底层格式
  （经 `server/utils/diff_service.py` 消费）。
- `server/utils/diff_service.py`：diff 计算本体（明确不在本模块范围）。
- `gateway/channel_manager/web/git_ws_handler.py`：订阅 handler（本
  规约接口的主要消费方）；`gateway/app_gateway.py` :2128-2135 装配
  `set_channel` + `web_channel.git_watcher_registry`（供
  `_mark_git_watcher_dirty` 唤醒）。
