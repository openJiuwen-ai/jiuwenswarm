# S_03 · 会话元数据（metadata.json）

## 元信息

| 项 | 值 |
|---|---|
| 类型 | spec |
| 编号 / slug | S_03 / session-metadata |
| 关联模块 | `jiuwenswarm/server/runtime/session/session_metadata.py`（2,041 行） |
| 最近一次修订日期 | 2026-10-30 |
| 关联 feature | 暂无 |

## 范围 / 边界

### 本规约管什么

- `<sessions_root>/<session_id>/metadata.json` 的完整读写契约：字段集、
  写入语义（first-write-wins / 三语义 / pin 保护）、原子落盘、缓存纪律。
- 异步写队列（`_METADATA_QUEUE`）与队列满退化同步写的路径。
- 多租户（sessions_root）下的缓存隔离与写根捕获。
- 会话目录解析的安全防线（`_safe_session_subdir`）与内部会话目录的识别跳过。
- 置顶（pinned / pin_order）的紧凑重编号协议。
- 批量读取入口（list / collect）的「不写盘」纪律与惰性迁移。
- 请求级参数→会话级元数据的同步入口（`sync_session_request_metadata`）
  的字段覆盖语义。
- Todo origin 标记（`_TODO_INITIALIZED_KEY`）的写路径保护与消费协议。

### 本规约不管什么

| 子主题 | 归属 |
|---|---|
| session_id 合法性校验（`is_valid_session_id`） | `common/utils.py`（正则全匹配，非本模块） |
| rename 指令层语义（截断 200 等） | [[S_01]] |
| history 持久化与其对 metadata 的顺带更新调用 | [[S_02]] |
| project 注册表本体（`get_resolved_project_dir` 只消费其查询接口） | [[S_04]] |
| 读写 metadata 的上层业务编排（handlers / diff_service / db_actor） | server 层与 gateway 层各自文档 |

### 双进程背景（理解本模块全部并发设计的钥匙）

本模块被 **Gateway 进程与 AgentServer 进程同时读写同一磁盘**：Gateway 写
channel_id / title / pinned 等接入侧字段，AgentServer 写 message_count / mode /
round_id 等运行侧字段；remote 模式下 `channels/web/history_store/db_actor.py`
还会代理转发。因此「缓存是进程内的、磁盘是跨进程的」——所有设计都必须回答
「窗口期内另一个进程读到什么」。

## 不变量

- **N1 写入架构：异步队列 + 满退化同步 + 原子落盘 + 跨进程文件锁**。写请求
  默认进 `_METADATA_QUEUE`（`maxsize=5000`，元素四元组
  `(session_id, metadata, sessions_root|None, preserve_pin_fields)`，:37），
  daemon worker 消费。队列满时**退化为调用线程同步直写**（:1018-1028）——
  宁可阻塞调用方也不丢写。同步写 `_write_metadata_sync`（:820-862）在
  `_FILE_LOCK` + **portalocker 跨进程文件锁**（`<metadata.json>.lock`，
  timeout=30，:835）双重临界区内先写临时文件再 `replace`（:850-858），临时
  文件名含 `pid + thread_ident` 防跨线程互踩；`OSError` 时清理临时文件再
  抛出（:859-861）。跨进程锁是磁盘作为双进程真源的第二道防线——进程内
  `_FILE_LOCK` 管不到另一进程的并发写。
- **N2 缓存纪律：缓存仅由 `_enqueue_write` 维护 + 8192 容量帽 FIFO 逐出**。
  `_write_metadata_sync` 明确**不更新** `_METADATA_CACHE`（docstring
  :829-831）——`init_session_metadata` 直接调它落盘（:1116），若同步写也写
  缓存，Gateway 进程的 init 写入会污染本进程缓存，遮蔽 AgentServer 进程此后
  写入磁盘的最新值。完整链路（`_enqueue_write` :970-1029 + worker :946-961）：
  入队/直写前先写**未 merge** 的视图（:998-1000，本进程立即见最新）；
  `preserve_pin_fields=True` 时落盘后把 merge 结果**回写**缓存——sync_write
  分支（:1008-1011）、worker 成功（:946-950）、queue.Full 退化分支
  （:1025-1028）三处；worker 落盘**失败**时主动**失效**该条目（:953-961）——
  入队时缓存的是未 merge 值，不失效会让陈旧视图长期驻留（表现为置顶排序
  长期错位），丢弃后下次读退化为一次磁盘读。`_METADATA_CACHE_MAX = 8192`
  （:51），超帽 FIFO 逐最旧（`_evict_metadata_cache_locked` :54-57，调用方
  必须已持 `_CACHE_LOCK`）。
- **N3 三锁分工**：`_FILE_LOCK`（:42）串行化所有进程内文件写（tmp+replace
  临界区）；`_CACHE_LOCK`（:47）保护缓存 dict 并发读写；`_SESSION_PIN_LOCK`
  （:1540）序列化 pin 的「设置→收集→重编号→写回」四步（见 N9）。三锁
  互不嵌套获取，无顺序死锁面。
- **N4 缓存 key 含 root 防多租户串缓存**：`_metadata_cache_key` 返回
  `f"{root}\n{session_id}"`（:678-680）。同一 session_id 在不同租户根下是
  两条独立缓存——丢 root 进 key 会让租户 A 的读取命中租户 B 的缓存。
- **N5 多租户写根：入队线程捕获 + 显式传根不回退**。`_coalesce_write_sessions_root`
  基于 ContextVar（:664），**必须在入队线程捕获**——worker 线程不继承请求
  线程的 ContextVar，入队时未定根，落盘时将写错租户树。读取侧显式传入
  `sessions_root` 时**只读该根，不回退全局树**（get_session_metadata
  docstring :1513-1514）。
- **N6 会话子目录六道防线**：`_safe_session_subdir`（:438）依次：
  strip → NUL 字节检查 → 正则约束 → symlink 拒绝 → Windows 重解析点
  （`st_file_attributes & 0x400`，:454）→ `resolve()+relative_to` 最终校验。
  任何一道失败返回 None（调用方需处理目录不存在路径）。这是 session_id
  进入文件系统路径的唯一通道。
- **N7 first-write-wins 字段集**：`channel_id` / `project_dir` / `project_id`
  / `cron_id` / `work_mode` 一旦写入磁盘即锁定，后续 update 传新值**不覆盖**
  （:1239-1280；channel_id :1242-1244、project_dir :1269-1270、project_id
  :1272-1273、work_mode :1276-1280）。channel_id 带 `_CHANNEL_ID_LOCK_VERSION`
  版本标记进入字段锁。依据：这些字段标识会话的创建绑定关系，运行期改写会导致
  接入侧（IM 通道）路由与项目上下文漂移；需要变更时走显式迁移
  （`enable_channel_lock_migration`）而非静默覆盖。
- **N8 title 三语义 + 截断只在 auto-title + 读侧标签清理 + 不调 LLM**。
  `update_session_metadata` 的 title：`None`=不修改；`"x"`（非空）=设置——
  **原样存储，不做长度截断**（:1284-1285）；`""`（空串）=忽略（防御）；
  显式清除走 `clear_title=True`（参数 :1125，分支 :1281-1283）。`_TITLE_MAX_LEN=50`
  （:61）截断**仅发生在 `_auto_title`**（:1035-1040：剥小写 XML 注入标签、
  换行折叠后截 50）。读侧 `_apply_metadata_defaults_with_inference` 统一做
  标签清理（调点 :260-264，`_sanitize_title` :407-422——移除注入标签、丢弃
  悬空标签开头，**不改长度**）。**与 [[S_01]] rename 的
  `_RENAME_TITLE_MAX_LEN=200` 是两处独立上限**——本处是 auto-title 生成
  截断，rename 是指令入口防御。自动标题（首条用户消息生成，:1291-1293）
  **不调用 LLM**。
- **N9 pin 字段保护：默认开启 + 落盘临界区内 merge + Gateway 唯一写方**。
  (a) `update_session_metadata` 在 `pinned is None and pin_order is None`
  时自动 `preserve_pin_fields=True`（:1308）。磁盘 merge **不在调用方线程
  做**，而是下沉到 `_write_metadata_sync` 的 `_FILE_LOCK` + portalocker
  临界区内（锁 :835，merge :840；`_merge_pin_fields` :913-917）：读盘现值 →
  把 `pinned` / `pin_order` merge 回待写数据 → 原子落盘。否则读-改-整份
  写回的旧快照会覆盖并发的 pin 状态。历史教训：merge 曾在 `_enqueue_write`
  调用方线程顶部同步读盘，每条普通消息都在 AgentServer 事件循环上读
  metadata.json，慢盘场景停摆事件循环（OA.05000010，2026-10-15；docstring
  :987-993）。
  (b) `increment_session_round_count`（:1617）等整份回写处显式传 True
  （:1628）。
  (c) `set_session_pinned`（:1543-1614）在 `_SESSION_PIN_LOCK` 内四步：
  设置目标（sync_write=True + cache_bust=True——**跨进程敏感，返回前必须
  落盘**，否则只读磁盘的 AgentServer 在窗口期读到旧值且回写覆盖，:1574-1583）→
  收集全部 pinned（**读缓存而非读盘**——pinned 仅由 Gateway 进程写，缓存即
  权威源，:1585-1588）→ 按 pin_order 升序稳定排序（:1605）→ 紧凑重编号
  1..N 逐个写回（消除间隙，:1607-1612）。新置顶 `pin_order=0` 排最前。全部
  写回 `touch_last_message_at=False`——置顶不是消息，刷 last_message_at 会
  腐蚀 session.list 排序语义（:1555-1557）。
- **N10 批量入口不写盘（惰性迁移）**：`get_all_sessions_metadata`（:1891）与
  `collect_all_sessions_metadata`（:1975）对存量会话的默认值补全 / 字段推断
  一律 `enable_writeback=False`（:1948/:1961、:2023/:2036）——首次
  session.list 触发大量异步写会撑爆 `_METADATA_QUEUE` 退化同步写（整页请求
  卡顿）。缺失字段在后续**单条** `get_session_metadata`（enable_writeback
  默认 True）读取时按需写回。collect 强制 `cache_bust=True` 跨进程拿最新值。
- **N11 读路径无 mkdir 副作用**：`_read_metadata` 不存在时返回 None，
  **不创建目录**（:780-782 注释）——查询一个不存在的 session 不得在磁盘上
  留下空目录（会污染 `get_all_sessions_metadata` 的 iterdir 扫描）。无
  metadata.json 的旧会话在批量入口以目录 `ctime/mtime` 构造最小兜底信息
  （get_all :1923-1951，ctime/mtime :1931-1932；collect :1996-2026），归入
  默认项目统计，不推断写盘。
- **N12 内部会话目录前缀四类**：`heartbeat_` / `__prewarm__` / `.prewarm` /
  `diagnosis_`（:62-68，`_is_internal_session_dir` :74）。所有目录扫描
  （iterdir）与 pin 收集必须跳过，否则心跳会话会出现在用户会话列表与置顶区。
  与 [[S_01]] 的一次性 session 判定（`heartbeat`/`cron`/`diagnosis` 前缀）是
  **两组不同清单**——前者是磁盘目录形态，后者是运行时队列语义。
- **N13 时间回退链禁用 `or`**：`last_user_message_at` 缺失回退 `created_at`
  时用显式 `if key in` 判断而非 `metadata.get(k) or default`——时间戳
  `0.0` 是合法 falsy 值（`_apply_metadata_defaults_with_inference` :303
  注释；collect 兜底分支同语义 :2010-2014）。空会话回退 created_at 保证
  排序稳定性，避免全部沉底。
- **N14 Todo origin 标记：一次性消费，异步写不可重置**。内部键
  `_TODO_INITIALIZED_KEY`（`todo_initialized`，:43）标记「首个 Todo 对象
  是否已确认初始化」：`init_session_metadata` 传 `new_todo_object=True`
  （:1061）时写 `False`（:1101-1102）；`check_todo_initialization`（:865）
  在 `_FILE_LOCK` + portalocker 内读-改-写消费标记（`committing=True` 置
  `True` :898，tmp+fsync+replace :899-907），文件丢失且标记已消费则拒绝
  重建（:894-895），结束后失效缓存条目。**写路径保护**：
  `_write_metadata_sync` merge 时异步更新**不得重置已消费标记**——磁盘
  现值有则保留、无则从待写数据剔除（:841-845），盘读失败的降级分支同样
  剔除（:846-849）。**读路径不外泄**：`get_session_metadata`（:1534）与
  两个批量入口（:1965/:2039）返回前 pop 该键。

## 接口契约

### 写入

| 接口 | 契约要点 | 代表调用方 |
|---|---|---|
| `update_session_metadata`（:1119） | 唯一通用写入口。三开关：`touch_last_message_at`（默认 False）、`cache_bust`（绕缓存强读盘）、`sync_write`（跳过队列直写）。first-write-wins 字段集见 N7；title 语义见 N8；pin 保护见 N9。无 metadata 时走兜底新建分支（:1184-1233，注意 work_mode 在该分支会被当次请求覆盖——修改前读注释 :1192-1197） | session_history.py 顺带更新（见 [[S_02]]）；session_rename.py；set_session_pinned 内部 |
| `init_session_metadata`（:1044） | 会话首次落盘：写全量默认字段并应用 first-write-wins；`new_todo_object=True` 写 Todo origin 标记（N14）；写前做 workspace 配额检查（:1104-1115，`WorkspaceQuotaExceeded` 直抛）；**直接调 `_write_metadata_sync` 不走队列**（:1116，也因此不碰缓存——N2） | session 创建流程（handlers/session.py、bootstrap.py） |
| `sync_session_request_metadata`（:1313） | 请求级参数→会话级元数据统一同步入口。字段语义：`project_dir` 首锁不覆盖（与磁盘不一致记 warning）；`model` / `mode` 仅显式携带才覆盖（`explicit_model_provided` / `explicit_mode_provided`，:1325）；`last_message_at` / `last_user_message_at` 仅 chat 轮刷新（`is_chat_turn`，:1323/:1484）；**始终 `cache_bust=True` 强读盘**——由 AgentServer 调用而 pin 由 Gateway 写，防本进程陈旧缓存覆盖跨进程最新值。无 metadata 时兜底新建（:1384-1418） | AgentServer chat 链路 |
| `set_session_pinned`（:1543） | 唯一 pin 写入口（N9）；无 metadata 返回 None；幂等 | Gateway Web 本地 handler（app_web_handlers.py；**不转发 AgentServer**） |
| `increment_session_round_count`（:1617） | round_id 读-改-写，`preserve_pin_fields=True`（:1628） | chat 轮次推进 |
| `check_todo_initialization`（:865） | Todo origin 校验/消费（N14）；`_FILE_LOCK` + portalocker 内 tmp+fsync+replace；结束失效缓存条目 | Todo 发布链路 |
| `remove_session_metadata_cache`（:1632） | 仅清本进程缓存，不动磁盘 | 会话切换 / 跨根重读前 |

### 读取

| 接口 | 契约要点 | 代表调用方 |
|---|---|---|
| `get_session_metadata`（:1497） | 单条读取 + 默认值补全 + 推断（project_id 可从 project_dir 反查）；`enable_writeback=True` 默认（惰性迁移写回点，见 N10）；只读校验场景（如 discard_turn_changes 绑定校验）传 False 避免读路径写盘副作用；返回前 pop Todo 标记（N14） | handlers/session.py；diff_service.py；agents harness session_ops_service.py |
| `get_all_sessions_metadata`（:1891） | 分页批量，`last_message_at` 倒序；不写盘（N10） | session.list |
| `collect_all_sessions_metadata`（:1975） | 不分页不排序全量收集，`cache_bust=True`；批量构建一次 project 映射避免 N+1（`_build_project_lookup` :178） | 项目统计 / 置顶聚合 |
| `get_resolved_project_dir`（:713） | 由 session 元数据解析项目目录（消费 [[S_04]] 查询接口） | 多处路由判定 |
| `resolve_session_subdir`（:466） | 目录解析入口（过 N6 六道防线） | 本模块内外广泛 |

### Remote 模式对齐义务

`channels/web/history_store/db_actor.py` 在 remote 模式代理 metadata 读写。
对 metadata 语义的任何修改（字段集 / first-write-wins / title 语义 / Todo
标记）必须在 db_actor 路径核对对齐——见 [[S_01]] rename 同类义务。

## 数据结构

### metadata.json 字段（核实集）

| 字段 | 写方进程 | 语义 / 锁定 |
|---|---|---|
| `session_id` / `created_at` | 初始 | 创建即定 |
| `channel_id` | Gateway | **first-write-wins**（带 lock version） |
| `project_dir` / `project_id` / `cron_id` / `work_mode` | 创建方 | **first-write-wins** |
| `user_id` | Gateway | 接入侧归属 |
| `title` | Gateway | 三语义 + clear_title（N8） |
| `last_message_at` / `last_user_message_at` | AgentServer | 时间回退链禁 or（N13） |
| `message_count` / `mode` / `round_id` | AgentServer | 运行侧增量 |
| `model` | AgentServer | 显式覆盖式（sync 入口，仅 explicit 携带才写） |
| `pinned` / `pin_order` | **仅 Gateway** | N9 保护协议 |
| `team_name` / `runtime_team_name` / `team_template_id` | AgentServer | 读侧 setdefault 兜底（:1531-1533） |
| `todo_initialized`（内部键） | init / check_todo | N14 一次性标记；读侧不外泄 |

### 进程内结构

- `_METADATA_QUEUE`（:37）：`maxsize=5000`，元素四元组（N1）。
- `_METADATA_CACHE`（:46）：dict，key `f"{root}\n{session_id}"`（N4），仅由
  `_enqueue_write` 维护（N2）；容量帽 `_METADATA_CACHE_MAX=8192`（:51），
  FIFO 逐最旧（:54-57）。
- `_coalesce_write_sessions_root`：ContextVar（N5）。

## 执行路径

- **P1 常规写**：`update_session_metadata` →（cache_bust 决定读基线：读缓存或
  强读盘）→ 字段合并（first-write-wins / title / pin 保护参数）→ `sync_write`
  ? 直接 `_write_metadata_sync` : `_enqueue_write`（入队 + 更新缓存）→
  worker 消费 → `_write_metadata_sync`（pin merge 在其锁临界区内完成，N9a）→
  preserve 时回写 merge 结果 / 失败失效（N2）。
- **P2 队列满退化**：`_enqueue_write` 捕获 `queue.Full` → 当前线程同步写 →
  preserve 时同样回写 merge 结果（:1025-1028）→ 返回与正常路径一致。
- **P3 单条读（惰性迁移）**：`get_session_metadata` → `_read_metadata`
  （缓存 / 磁盘，无 mkdir）→ `_apply_metadata_defaults_with_inference`
  （:212，默认补全 + project_dir→project_id 反查 + title 标签清理）→
  `enable_writeback` 时异步写回。
- **P4 pin 变更**：`set_session_pinned` → `_SESSION_PIN_LOCK` → 四步（N9c）。
- **P5 批量扫描**：`sessions_dir.iterdir()` → 跳过非目录与内部前缀（N12）→
  单会话 `_read_metadata`（collect 强制 bust）→ 兜底构造（无 metadata.json）
  → 不写盘（N10）→ 排序 / 分页返回。
- **P6 Todo origin 消费**：`check_todo_initialization` → `_FILE_LOCK` +
  portalocker 读-改-写 → `committing=True` 置标记 + fsync 落盘 → 失效缓存
  （N14）。

## 修改检查单

修改本模块前自查：

1. 新增字段：进 `_apply_metadata_defaults_with_inference` 的默认值兜底
   （新旧会话读出一致）；批量入口**不得**新增写盘路径（N10）。
2. 触碰写路径：确认 preserve_pin_fields 语义未被破坏（读-改-整份写回场景，
   merge 必须保持在 `_write_metadata_sync` 锁临界区内，N9a）；确认
   `_write_metadata_sync` 仍不直接碰缓存、preserve 场景的回写/失效仍只发生在
   `_enqueue_write` 三分支与 worker（N2）。
3. 触碰缓存：key 必须含 root（N4）；只允许 `_enqueue_write` / worker 回写
   路径写缓存（N2）；新写入点过 `_evict_metadata_cache_locked`。
4. 触碰 sessions_root 处理：入队前必须完成根捕获（N5）；显式根不得回退
   全局树。
5. 触碰 title / first-write-wins 字段：核对 db_actor remote 路径对齐义务；
   核对 `enable_channel_lock_migration` 迁移开关行为不变；显式 title 仍不做
   长度截断（N8）。
6. 触碰目录解析：不得绕过 `_safe_session_subdir` 直接拼路径；新目录形态
   需评估是否加入内部前缀清单（N12）。
7. 触碰时间字段：回退链禁用 `or`（N13）；置顶类写入禁 `touch_last_message_at`。
8. 新增「读触发写」逻辑：先确认不是批量入口（N10），再确认单条入口有
   `enable_writeback=False` 逃生口。
9. 触碰 Todo 标记：异步写不得重置已消费标记（N14 的 :841-845 保护）；读侧
   新出口必须 pop 该键。
10. 改 `sync_session_request_metadata` 字段语义：核对 model/mode 的显式覆盖
    条件与 `cache_bust=True` 前提（AgentServer 侧调用、pin 跨进程敏感）。

## 与其它 spec 的关系

- [[S_01]]：rename 指令最终落到本模块 `update_session_metadata`（title 三语义
  在两层分别实现——rename 层管指令解析与 200 截断，本层管存储语义与
  auto-title 50 截断）。
- [[S_02]]：history 每条 append 顺带更新本模块的 message_count /
  last_message_at 等（history 写失败不回滚 metadata，各自独立）。
- [[S_04]]：本模块 `get_resolved_project_dir` / 推断反查消费 project 注册表
  查询接口；project 删除后 metadata 中残留的 project_id 属预期（归默认项目）。
- [[S_06]]：diff 展示依赖本模块 mode 字段判定 work/code 会话。
