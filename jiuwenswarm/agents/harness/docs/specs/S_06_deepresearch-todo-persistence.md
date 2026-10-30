# S_06 — DeepResearch 四阶段快照的 todo 落盘

| | |
|---|---|
| Spec ID | S_06 |
| 状态 | 当前实现的真实描述 |
| 修订日期 | 2026-10-28 |
| 关联模块 | `jiuwenswarm/agents/harness/common/tools/deepresearch/todo_progress.py`（落盘件：路径解析 + 安全校验 + 原子写，313 行）<br>`jiuwenswarm/agents/harness/common/tools/deepresearch/tools.py`（两处 send 通道调用点：~3230 race-completed 通道、~3476 主 push 通道）<br>`jiuwenswarm/agents/harness/common/tools/deepresearch/stream_router.py`（`DEEPRESEARCH_STAGES` 四阶段常量 ~94、节点→阶段映射 `_NODE_STAGE`、task.update 生产侧） |
| 消费点 | 落盘文件即 [[S_01]] 布局的标准 `{workspace}/todo/{session_id}/todo.json`——`TaskExecutionRail._load_todo_from_json` / `_format_tasks_for_update`（[[S_02]]）可直接读，供历史 / 恢复视图展示 |
| 测试锚点 | `tests/unit_tests/agentserver/test_deepresearch_todo_progress.py`（全量）；`test_deepresearch_stream_tool.py`（~1913 路径异常容忍、~4468/~5153 persist mock） |

## 1. 范围与边界

本 spec 管 **"DeepResearch 流式进度事件中的四阶段 task.update 快照，如何以安全方式落盘为标准 harness todo.json"**：

- DeepResearch 是**无 session 内 todo 工具**的编排型能力：进度以网关 push 事件（`task.update`）流给前端，不经过 [[S_02]] 的执行地图
- 落盘的意义：**会话结束后**历史 / 恢复视图仍能看到"这次研究完成了哪四个阶段"——落盘的是**最终成就快照**，不是过程状态
- 落盘发生在 push 通道内：每个 `task.update` payload 推给网关**之前**先尝试持久化

**不管**（指向其它 spec / 域）：

- stream_router 的流协议、事件路由、节点→阶段映射的编排语义 → deepresearch 工具域（`stream_router.py` 其余部分）
- 标准 todo.json 的存储布局、`TodoItem` schema、openjiuwen 工具链 → [[S_01]]
- TaskExecutionRail 读侧的完整行为 → [[S_02]]
- deepresearch 的其它模块（execution / runtime / sdk_bridge / rewrite_*）→ deepresearch 工具域

## 2. 核心不变量

**N1 固定四阶段模式，载荷强校验**
`_deepresearch_tasks(payload)` 只接受这样的快照：`event_type == "task.update"`；`tasks` 列表长度**精确等于** `len(DEEPRESEARCH_STAGES)`（4：研究主题澄清 / 大纲生成 / 并行调研与章节撰写 / 报告交付）；第 i 项 `task_id` 必须是 `deepresearch_stage_{i}` 且 `task_content` 必须精确等于对应阶段标题；`status ∈ {pending, in_progress, completed, cancelled}`。任一不符返回 None（不落盘、不报错）。**上游改阶段标题会静默停止落盘**（旧文件 content 与新标题不一致的历史兼容问题另见修改检查单）。

**N2 只落盘全 completed 快照**
校验通过后仍要求 `all(task["status"] == "completed")`——部分完成 / 进行中 / 有取消的快照一律不落。落盘语义是"这次研究交付了"，中途状态不该出现在历史视图里。

**N3 落盘即标准格式**
写出条目字段：`id` / `content` / `activeForm`（=阶段标题）/ `status` / `createdAt` / `updatedAt`——可被 openjiuwen `TodoItem.from_dict` 往返，可被 `TaskExecutionRail._load_todo_from_json` 直接读。DeepResearch 不发明私有格式。

**N4 createdAt 保留、updatedAt 刷新**
`_existing_created_at` 读旧文件按 id 建 createdAt 映射（旧文件缺失 / JSON 损坏 / 非列表 → 空映射）；写新文件时已有 id 沿用旧 createdAt，其余用 `now`（UTC ISO）。重跑同一 session 不重置"何时开始"，但 updatedAt 恒为本次落盘时间。

**N5 落盘先于 push，同 payload 两处通道**
`tools.py` 的两个 send 闭包（race-completed 通道 ~3230 与主通道 ~3476）都是：`_redact_json_value` 脱敏 → `task.update` 则先 `persist_deepresearch_task_update(safe_payload, todo_path)` → 再 `push.send_push`。两处行为一致，无第三条旁路。

**N6 落盘 fail-open，不牺牲主链路**
路径解析失败 → `todo_path=None`，后续跳过 persist；persist 抛任何异常 → warning（只记异常**类型名**）后照常 push。可见性优化绝不能让研究结果的推送失败。

**N7 路径分量不透明校验**
`deepresearch_todo_path(session_id, workspace_key)`：`session_id` 与非 None 的 `workspace_key` 都过 `_validate_component`——拒绝空串 / `.` / `..` / `/` / `\` / `\0` / UTF-8 超 255 字节，非法即 `ValueError`（哨兵串 `deepresearch_todo_invalid_path`）；`workspace_key` 仅 **None → "default"**，空白串不落 default——strip 后空串同样被 `_validate_component` 以 `ValueError` 拒绝（fail-closed）。最终路径 = `get_tenant_agent_workspace_dir(wk) / "todo" / {session_id} / "todo.json"`——**无法逃出租户 workspace**。

**N8 父目录信任链（fail-closed）**
`_open_trusted_parent` 建立"持有者"身份：
- **POSIX**：从 anchor 起**逐级** `os.open(part, dir_fd=fd, O_NOFOLLOW|O_DIRECTORY)`，中间目录缺失则 `mkdir(0o700, dir_fd=fd)` 后重开；任一级是 symlink 即失败。返回持有 fd + `(st_dev, st_ino)`
- **Windows**：无法 open 目录的 Python 实现降级为 lstat 身份链（best-effort），能 open 的层级做 lstat/fstat 双重身份比对
- 后续所有读 / 写 / replace / cleanup 都相对这个持有 fd（`dir_fd=trusted.fd`），不信任命名路径
- `_verify_named_parent`：replace 前后重新按名解析并比对身份——父目录被换（重命名 / 符号链接重指）即 `OSError`

**N9 原子替换 + TOCTOU 全程防御**
- 进程级 `_TODO_WRITE_LOCK`（threading.Lock）串行化全部落盘
- 读旧文件 `_read_existing_todo`：leaf 必须 regular + `nlink==1` + `size ≤ 1MB`；打开前后 fstat 身份一致；读完后**再**比对一次身份（读期间被换即失败）
- 写 temp：`_allocate_temp` 用 `secrets.token_hex(16)` 随机名 + `O_CREAT|O_EXCL`，最多 32 次重试；temp 身份（dev/ino/nlink）校验后才用
- `os.replace` 在持有 fd 内原子换名（Windows 走路径版 replace 后**再** verify 一次父链）
- finally 里 `_cleanup_owned_temp` 只删**自己创建且身份未漂移**的 temp（name + identity 双条件）——别人的文件不碰

**N10 幂等重入**
同 payload 重复落盘结果一致：全量替换、不增量合并、createdAt 沿旧值。并发落盘被 N9 的锁串行。

## 3. 接口契约

| 公开面 | 位置 | 消费方 |
|---|---|---|
| `deepresearch_todo_path(*, session_id, workspace_key=None) -> Path` | `todo_progress.py` | `tools.py` 两处 send 通道（前置解析） |
| `persist_deepresearch_task_update(payload, *, todo_path) -> bool` | `todo_progress.py` | `tools.py` 两处 send 通道（task.update 分支） |
| `DEEPRESEARCH_STAGES`（四元组） | `stream_router.py` | `_deepresearch_tasks` 校验 + stream_router 阶段推进 |

`persist_deepresearch_task_update` 返回 False 的三种情形：载荷校验不过（N1）/ 非全 completed（N2）/ 其它事件类型。返回 True 仅在完整走完原子替换后。

## 4. 数据结构

- 落盘条目：`{"id": "deepresearch_stage_{1..4}", "content": <阶段标题>, "activeForm": <阶段标题>, "status": "completed", "createdAt": <ISO8601>, "updatedAt": <ISO8601>}`——JSON 数组、`ensure_ascii=False`、indent=2、尾部换行
- `_TrustedParent(fd, identity, ancestors)`：持有父目录 fd（POSIX）或祖先身份链（Windows）的 frozen dataclass
- 常量：`_MAX_TODO_BYTES = 1MiB`（leaf 与读缓冲上限）、`_MAX_COMPONENT_BYTES = 255`、temp 前缀 `.deepresearch-todo-{hex}.json`
- `_TODO_STATUSES` 四值与 openjiuwen TodoStatus 对齐（completed 落盘，其余三值仅校验用）

## 5. 执行路径全景

**P1 生产（事件源）**
deepresearch 编排节点按 `_NODE_STAGE` 映射推进 `state.current_stage` → stream_router 组装 `task.update` payload（含四阶段 tasks 快照与计数）→ 进入 `tools.py` 的 send 通道。

**P2 落盘（persist 全程）**
send 通道收到 payload → 脱敏 → `event_type == "task.update"` → `persist_deepresearch_task_update`：载荷校验（N1）→ 全 completed 判定（N2）→ 加进程锁 → 建信任父链（N8，含逐级 mkdir）→ 读旧 createdAt（N4）→ 分配 temp → `json.dump` + flush + temp 身份复核 → `_verify_named_parent` → `os.replace` 原子换名 → finally 清理自有 temp + 关持有 fd → 返回 True。任何一步 OSError / ValueError 冒给调用方 → E5 warning → push 照常。

**P3 消费（读侧接缝）**
会话结束后：历史 / 恢复路径经 `TaskExecutionRail._load_todo_from_json(session_id)` 读同一文件（workspace TODO 节点）→ `_format_tasks_for_update(retained, source="todo")` 转广播格式 → 前端 todo 视图展示四阶段全 completed。**落盘文件是 DeepResearch 与 [[S_02]] 执行地图体系的唯一交汇点**——但方向是单向的（DeepResearch 只写不读 rail 状态，rail 只读不写 DeepResearch 进度）。

## 6. 异常分支

- **E1** 载荷不符（长度 ≠ 4 / task_id / content / status 任一不匹配）→ 返回 None，非错误，不落盘
- **E2** 非 `task.update` 事件 → 直接跳过 persist
- **E3** `deepresearch_todo_path` 分量非法 → `ValueError`；调用方 warning + `todo_path=None`，本 session 后续跳过落盘
- **E4** 安全防御触发（symlink 中间目录 / leaf 非 regular / nlink≠1 / 身份漂移 / 超 1MB / temp 分配 32 次失败）→ `OSError` fail-closed——**宁可落盘失败也不写外部目录**
- **E5** persist 任何异常 → 调用方 warning（只记类型名，不泄内容）→ push 照常（N6）
- **E6** 旧 todo.json 损坏 / 非列表 / 字段类型不对 → `_existing_created_at` 返回空映射，createdAt 全用 now——旧文件损坏不阻塞新快照落盘

## 7. 边界 case 清单（测试锚点）

| 主题 | 锚点（test_deepresearch_todo_progress.py） |
|---|---|
| 租户 workspace 路径解析 | `test_deepresearch_todo_path_uses_tenant_workspace` |
| 分量校验拒绝 | `test_deepresearch_todo_path_rejects_unsafe_opaque_components` |
| 无法逃出租户目录 | `test_deepresearch_todo_path_cannot_escape_tenant_workspace` |
| 非全 completed 不落盘 | `test_persist_deepresearch_task_update_ignores_incomplete_snapshots`（逐 active_stage 遍历） |
| 四阶段全量落盘 + TodoItem 往返 + rail 读侧 | `test_persist_deepresearch_task_update_retains_completed_four_stage_plan` |
| 原子替换（os.replace 单次、dir_fd 版本） | `test_persist_deepresearch_task_update_publishes_with_atomic_replace` |
| symlink session 目录拒绝 | `test_persist_rejects_symlink_session_directory` |
| 持有期父目录被换 fail-closed | `test_persist_fails_closed_when_named_parent_is_swapped_after_open` |
| 其它快照忽略 | `test_persist_deepresearch_task_update_ignores_other_snapshots` |
| 空 todo 不发 task.update（读侧接缝） | `test_skill_complete_with_empty_todo_emits_no_task_update` |
| push 通道 persist 异常容忍 | `test_deepresearch_stream_tool.py` ~1913（RuntimeError 注入） |

## 8. 修改检查单

1. **改 `DEEPRESEARCH_STAGES` 标题 / 数量** → `_deepresearch_tasks` 校验自动跟随（按 STAGES 枚举），但**已落盘的历史文件 content 与新标题不一致**——历史视图会出现"旧标题条目"，需评估是否迁移；上游改标题时落盘会静默切换，无 warning
2. **改落盘时机（如中途也落）** → 重新评估 N2（历史视图语义从"成就快照"变"过程状态"）与 E1 的读侧兼容
3. **改存储布局** → 必须同步 [[S_01]] 与 `TaskExecutionRail._load_todo_from_json` 读侧——这是双向契约
4. **动安全写代码** → 跑 test_deepresearch_todo_progress.py 全量——symlink / swap / identity 系列是安全回归网，删任何一道校验前先看 N8/N9 对应测试
5. **新增需要落盘的事件类型** → 复用 persist 骨架（信任链 + temp + replace），勿另起写路径
6. **两处 send 通道的行为必须保持一致** → 改一处必须同步另一处（race-completed ~3230 与主通道 ~3476 是复制粘贴式对称）

## 9. 与其它 spec 的关系

- **[[S_01]]**：落盘文件即 S_01 定义的标准 todo.json 布局与条目 schema（同一棵目录树，`TodoItem.from_dict` 兼容）
- **[[S_02]]**：`_load_todo_from_json` / `_format_tasks_for_update` 是落盘数据的读侧消费；DeepResearch 进度**不经过**执行地图（无 session 内 todo 工具），落盘是事后可见性
- **[[S_05]]**：DeepResearch 无 profile todo 工具变体——本 spec 是任务规划域在 deepresearch 的**唯一**接缝
- **[[S_03]]**：无耦合（四阶段全 completed 落盘不参与收口判定——DeepResearch 的完成由流式 final 事件决定，不走 todo 关单）
