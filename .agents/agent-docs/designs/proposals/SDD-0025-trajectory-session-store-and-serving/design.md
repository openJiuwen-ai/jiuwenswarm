---
id: SDD-0025
title: 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API
slug: trajectory-session-store-and-serving
status: draft
stage: proposals
reviewers: []
created: 2026-09-15
updated: 2026-09-15
version: 1
domain: observability
modules: [jiuwenswarm/observability, jiuwenswarm/gateway/channel_manager/web]
tags: [trajectory, sqlite, sink, content-addressing, stream-frames, live-hints, http-api, execution-subject, jiuwenswarm, gateway, agentserver]
related: [SDD-0022, SDD-0023, SDD-0026]
implementation:
  status: in-progress
  repos: [jiuwenswarm, agent-core]
  prs: []
  commits:
    - jiuwenswarm@e2b76a3ec
    - jiuwenswarm@efe23c8d3
    - jiuwenswarm@c4dbc8b93
    - jiuwenswarm@c5b25bd0d
    - jiuwenswarm@d8df43b07
    - jiuwenswarm@a2dae6083
    - jiuwenswarm@862d04ccc
    - jiuwenswarm@7d2b23473
    - jiuwenswarm@f959dc03f
    - jiuwenswarm@07c17949a
    - jiuwenswarm@d8cddd4fd
    - jiuwenswarm@6d00e76c6
    - jiuwenswarm@444d19b2a
    - jiuwenswarm@e314482c5
    - jiuwenswarm@a0c29cb21
    - jiuwenswarm@dcd2bd5f6
    - jiuwenswarm@ce95e3bfd
    - jiuwenswarm@c94364d3b
    - agent-core@b84617fee
    - agent-core@7e7871199
  tracking: "jiuwenswarm: jiuwenswarm/observability/ · jiuwenswarm/gateway/channel_manager/web/trajectory_http.py"
author: '@alan_cheng'
author_note: 由历史 owner 字段原值迁移
type: D
files: []
author_email: null
---

# 轨迹数据面：会话级 SQLite 存储、实时变更提示与读取 API

> 本文是 SDD-0022 处理器层的宿主侧消费者，也是 SDD-0026 前端的数据来源。
> 它定义 JiuwenSwarm 如何接收 `SpanRecordProcessor` 的三通道记录，如何按会话持久化，如何把"有变更"推送给浏览器，以及浏览器通过哪些 API 拉取数据。
> 本文基于 jiuwenswarm `dev/swarm/room@43e796e1d` 的代码逆向整理。

## 1. 背景与问题 (Why)

### 1.1 场景

用户在 JiuwenSwarm Web 里打开一个会话的"轨迹"页。Agent 此时可能还在执行第 37 个 ReAct Step，一个 sub-agent 正在流式输出，Team 的三个成员也在并行干活。用户期望看到这些内容：

- 已结束的每一步：模型当时看到的上下文、说了什么、调了哪些工具、结果如何；
- 正在进行的一步：逐字出现的回答；
- sub-agent 和每个成员各自的轨迹；
- 刷新页面、断线重连、AgentServer 重启之后，轨迹依然完整；
- 删除会话时，轨迹一起删除。

### 1.2 现有手段为什么不够

| 手段 | 不足 |
|---|---|
| 文件导出器 jsonl（SDD-0023） | 所有会话按天交错写在一起；只有结束的 span；没有流式帧；带 Langfuse 投影；按会话查询需要全文件扫描 |
| Langfuse / Collector | 依赖外部服务；批处理有 5 秒以上延迟；看不到进行中的 span；无法与 JiuwenSwarm 的会话权限、删除生命周期绑定 |
| `debug_trace` 文本 dump | 人读的文本，不是结构化数据，无法驱动 UI |

### 1.3 约束

1. **进程拓扑**。`jiuwenswarm/app.py` 把 AgentServer（执行 agent、产出 span）和 Gateway（提供 Web HTTP/WS）作为**两个子进程**启动。span 在 AgentServer 里产生，浏览器请求打到 Gateway。
2. **不能拖慢执行**。`SpanProcessor` 回调运行在事件循环线程上（SDD-0022 §5.4），入口只能做 O(1) 的非阻塞入队。
3. **数据量大**。一个长会话的 chat span 每次都复述整个上下文（SDD-0022 §1.2），流式回答每个回合产生数百帧。
4. **读者多样**。有初次加载的，有断线后追赶的，有只看某个 sub-agent 的，也有导出归档的。

## 2. 目标与非目标

**目标**

- G1 **非阻塞摄入**：处理器回调里只做身份校验加 `put_nowait`；序列化、合并、写库都在后台线程完成；队列满时计数丢弃，不阻塞。
- G2 **会话隔离**：一个会话一个 SQLite 文件。删除会话等于删除文件；文件名不含用户可控路径。
- G3 **实时与最终一致**：
  - 进行中的 span 以快照（`running`）形式可见，同一 span 按修订号只保留最新；
  - span 结束后以 `final` 形式不可变归档，此后迟到的快照一律拒绝；
  - 流式帧只追加、不合并。
- G4 **变更推送 + 按游标拉取**：写入提交后通过 AgentServer→Gateway→浏览器推送 `trace.updated` 提示（只含游标，不含数据），浏览器按主体链修订号、帧序号增量拉取。
- G5 **按执行主体读取**：一个会话内的主 agent、每个 sub-agent 调用、leader、每个成员各成一条"主体链"，按 `change_seq` 有序，可以单独分页。
- G6 **存储去重**：复述型 GenAI 属性按内容寻址，同一内容（同一前缀）只存一次；读取时只下发读者还没有的 blob。
- G7 **可恢复**：进程重启后，未结束的 span 标记为 `abandoned`；任何使读者缓存失效的变化（迁移、保留期删除、可见性变化）都会轮换 `store_epoch`，迫使读者从头重建。
- G8 **归档导出**：一次读快照导出整个会话，支持"还原后的纯 OTLP"与"寻址引用加 blobs"两种格式。

**非目标**

- 不做 UI 投影（回合、行、上下文窗口重建），全部在浏览器完成，见 SDD-0026 §5.1。
- 不做分布式 Team 的跨进程汇聚：teammate 进程的 span 不回流 leader 的存储，见 §9 R6。
- 不做多租户或用户级授权。HTTP 读取只有会话模式校验和可选的 Origin 校验。
- 不做服务端归档导入：导入是浏览器端纯回放。
- 不提供独立于 OTel 导出器的"仅存储"模式：开启 `trajectory_ui` 会复用对应运行时的完整导出配置，见 §9 R5。

## 3. 术语与上下文

| 术语 | 含义 |
|---|---|
| **Router** | `TrajectorySessionSinkRouter`：进程级单一入口，按会话把记录分发给各会话 writer |
| **Session writer** | `TrajectoryRecordSink`：每个会话一个后台线程，独占写该会话的 SQLite |
| **Current record** | `trajectory_current_records` 表中一个 span 的当前状态（`running` / `final` / `abandoned`） |
| **Archive record** | `otlp_span_records` 表中结束 span 的不可变原始 OTLP |
| **`change_seq`** | 每次被接受的 upsert 在变更日志中的自增序号；同时是 current record 的修订游标 |
| **Subject chain（主体链）** | 一个会话内同一 `execution_subject_id` 的 current records，按 `change_seq` 排序 |
| **`watermark`** | 会话内最大 `change_seq`，主体列表增量查询的游标 |
| **`frame_seq`** | 帧表的自增 rowid，帧增量查询的游标 |
| **`store_epoch`** | 存储代次（uuid4 hex）。变化意味着读者持有的所有游标和缓存都失效 |
| **Eligible trace（可见 trace）** | 所有记录的 `agent_mode` 都属于规范单 agent 模式或 Team 模式、且至少有一条有效模式的 trace；只有它们对读者可见 |
| **Hint（提示）** | `trace.updated` 推送，只携带 `{session_id, trace_id, revision, frame_seq, store_epoch, lifecycle}` |

## 4. 方案设计 (What / How)

### 4.1 总体架构

```
 AgentServer 进程
 ┌───────────────────────────────────────────────────────────────────────────────────────┐
 │ interface_deep / team_helpers 每次请求：                                               │
 │   sync_agent_observability() / sync_team_observability()                              │
 │     ├ acquire_observability(ObservabilityConfig)   ← SDK 需求协调（SDD-0022 §4.4.1）   │
 │     └ sync_trajectory_runtime(settings, demand)    ← 启停 Router 并注册为消费者         │
 │                                                                                       │
 │ agent-core SpanRecordProcessor（进程级单例）                                           │
 │   consume / consume_snapshot / consume_stream_frame   （事件循环线程，O(1)）           │
 │        │                                                                              │
 │        ▼                                                                              │
 │ TrajectorySessionSinkRouter ── 有界队列(queue_size) ── 线程 "trajectory-session-router"│
 │   ├ 身份校验；拒绝已墓碑的会话；session_id 为空 → 孤儿缓冲（等同 trace 的有主记录）     │
 │   └ 按 session 懒创建 writer；空闲 300 s 退役                                          │
 │        │                                                                              │
 │        ▼                                                                              │
 │ TrajectoryRecordSink ×N（每会话一个线程 "trajectory-record-writer"）                   │
 │   finals 队列 │ snapshot_pending(OrderedDict, 按 span 取最新) │ frames 队列              │
 │   快照去抖 flush_interval_ms（final 与帧到达时提前触发）；batch_size                    │
 │        │                                                                              │
 │        ▼                                                                              │
 │ TrajectoryStore.write_records(records, frames)  BEGIN IMMEDIATE … COMMIT               │
 │   <workspace>/.trace/sessions/<sha[:2]>/<sha256(session_id)>.sqlite3  (WAL)            │
 │        │ WriteBatchResult.updates: CommittedTraceUpdate[]                              │
 │        ▼                                                                              │
 │ on_commit → TrajectoryGatewayHintBridge.publish ── call_soon_threadsafe ── 合并 ──      │
 │            server.send_push({event_type:"trace.updated", …})                           │
 └──────────────┬────────────────────────────────────────────────────────────────────────┘
                │ 既有 AgentServer→Gateway WebSocket（E2A server_push）
 Gateway 进程   ▼
 ┌───────────────────────────────────────────────────────────────────────────────────────┐
 │ MessageHandler._handle_trajectory_update_push → WebChannel.schedule_trajectory_updates │
 │   按 (session, trace) 合并 16 ms → send_event(ws, "trace.updated")（匹配会话的连接）    │
 │ TrajectoryHttpService（FastAPI，dual_protocol 模式）                                   │
 │   AsyncTrajectoryReader(aiosqlite, ?mode=ro, query_only=ON) ── 读同一个会话 DB 文件      │
 └──────────────┬────────────────────────────────────────────────────────────────────────┘
                ▼
            浏览器 TrajectoryPanel（SDD-0026）：收到 hint → GET /api/trajectory/...
```

**模块职责**（jiuwenswarm）

| 文件 | 职责 |
|---|---|
| `observability/config.py` | `TrajectoryStoreSettings`；读取 `trajectory_ui` 段；`session_database_path(root, session_id)` |
| `observability/models.py` | 与 agent-core 的**鸭子类型**契约（`OtlpSpanRecordLike` 等 Protocol）；writer 线程内构造的冻结副本 `TraceRecordData` / `StreamFrameData` / `AddressedSequenceData`；`CommittedTraceUpdate`、`WriteBatchResult`、`TraceSinkStats` |
| `observability/sink.py` | `TrajectorySessionSinkRouter`、`TrajectoryRecordSink`：摄入、路由、合并、批写、重试、保留期调度 |
| `observability/store.py` | `TrajectoryStore`（写）：schema、迁移、写事务、保留期；`AsyncTrajectoryReader`（读）：全部读取查询 |
| `observability/runtime.py` | 进程单例：`sync_trajectory_runtime` / `start_trajectory_runtime` / `shutdown_trajectory_runtime` / `get_trajectory_runtime_sink` |
| `observability/gateway_hints.py` | `TrajectoryGatewayHintBridge`：合并提交提示并推送给 Gateway |
| `observability/updates.py` | `TrajectoryUpdateBroker`：进程内扇出（生产环境无监听者，见 §9 R11） |
| `observability/session_delete.py` | 会话删除墓碑协议 |
| `observability/turn.py` | `SessionTurnTracker`：turn id 与序号，跨 trace 串起一个 ReAct 回合 |
| `observability/projection.py` | `project_trajectory_scope`：只提取 `agentteam.team.id/name`（**不是** UI 投影） |
| `gateway/channel_manager/web/trajectory_http.py` | `TrajectoryHttpService`、`attach_trajectory_routes` |
| `gateway/channel_manager/web/web_connect.py` · `gateway/message_handler/message_handler.py` | 提示转发与 WS 事件 |
| `runtime/session_provisioner.py` | 会话删除时的 begin / abort / commit |

### 4.2 数据流与时序

#### 4.2.1 实时写入与推送

```mermaid
sequenceDiagram
    autonumber
    participant Loop as AgentServer 事件循环
    participant SRP as agent-core SpanRecordProcessor
    participant R as Router 线程
    participant W as 会话 writer 线程
    participant DB as session.sqlite3 (WAL)
    participant B as HintBridge (AgentServer loop)
    participant GW as Gateway MessageHandler
    participant WC as WebChannel
    participant UI as 浏览器

    Loop->>Loop: to_thread(sync_agent_observability) → sync_trajectory_runtime(demand="agent")
    Loop->>SRP: open_agent_run_span(...) → on_start → publish_snapshot("started")
    SRP->>R: consume_snapshot(record)（put_nowait，不读载荷）
    loop 流式
        Loop->>SRP: publish_stream_frame(frame)
        SRP->>R: consume_stream_frame(frame)
    end
    R->>W: 路由到会话 writer（懒创建）
    W->>W: 快照按 (trace, span) 保留最新；去抖 ≤ flush_interval_ms（final/帧提前触发）
    W->>W: 在 writer 线程构造 TraceRecordData（此时才读取载荷、编码）
    W->>DB: BEGIN IMMEDIATE：upsert current + changes；blobs/sequences；frames；孤儿归并；COMMIT
    W->>B: on_commit(updates) → publish → call_soon_threadsafe(_ensure_drain)
    B->>GW: send_push({event_type:"trace.updated", session_id, trace_id, revision, frame_seq, store_epoch, lifecycle})
    GW->>WC: schedule_trajectory_updates((CommittedTraceUpdate,))
    WC->>UI: WS event "trace.updated"（每 16 ms 合并，只发给匹配会话的连接）
    UI->>GW: GET /api/trajectory/sessions/{sid}/subjects?after_revision=W
    UI->>GW: GET …/subjects/{subject}/records?since_revision=R&limit=1000
    UI->>GW: GET …/stream-frames?since_frame_seq=F
    Note over UI: 若 store_epoch 变化 → 从头重建
    SRP->>R: on_end → consume(final)
    R->>W: final 移除同 span 的待写快照并入队
    W->>DB: archive 插入 + current 置 final（raw_json 置空，读取时回落到 archive）
    W->>B: hint(lifecycle=final)
```

**提示桥**：按 `(session, trace)` 合并，保留更大的 `(revision, frame_seq)`。`send_push` 返回 False（Gateway 未连接）时，把提示放回队列，每 0.1 秒重试一次。

#### 4.2.2 sub-agent

- 进程内 sub-agent 的 span 与父 agent 在**同一 trace**，带自己的 `execution_subject_id`；`gen_ai.conversation.id` 是 owner 会话（SDD-0022 §4.2.2）。
- 记录写入 **owner 会话的 DB**，在 `list_subjects` 中表现为独立主体。
- 归属校验（`sink.py`）：`execution_subject_session_id` 必须等于 owner 会话，或以 `{owner}_sub_` 开头，否则拒绝，防止外来记录混入。
- 没有 session 的子 span 进入 Router 孤儿缓冲，等同一 trace 出现有主记录时一并写入。写库事务中的 `_reconcile_orphans` 还会用同 trace 内唯一的会话提示回填 NULL 的 session/request/run/mode 列。

#### 4.2.3 Team

- **进程内 Team**（默认 `spawn_mode: inprocess`）：
  - `sync_team_observability` 以 `demand="team"` 获取 provider 需求，并同步同一个进程单例 runtime；
  - Team span 带 `gen_ai.conversation.id=session_id`，只有 `agentteam.team.*` 时 `agent_mode` 被推断为 `team`；
  - leader 与各成员表现为同一会话 DB 中的不同执行主体。
- **HTTP 门禁**：会话元数据必须是 Team 模式，并且 `team_name` 非空。
- **分布式 Team**（leader 与 teammate 分进程或分主机）：**不支持**。见 §9 R6。
  - leader 的存储只包含 leader 进程产生的 span；
  - teammate 引导路径上没有观测同步调用；即使有，它的 writer 也只会写本机的 `<workspace>/.trace/sessions`；
  - `config.team.distributed.{leader,teammate}.yaml` 不含 `trajectory_ui` 段，只有一个无人读取的旧 `telemetry:` 段。

#### 4.2.4 读取：初次加载与增量

```
初次加载                                     增量（收到 hint 或重连后）
GET /subjects?after_revision=0               GET /subjects?after_revision=<watermark>
  → items[{subject_id, revision,…}],           → 只返回 MAX(change_seq) > watermark 的主体
    watermark, store_epoch                    对每个 revision 前进的主体：
对每个主体（前端并发 6）：                     GET /subjects/{s}/records?since_revision=<已加载 revision>
  GET /subjects/{s}/records?since_revision=0    → 合并后的 current 状态增量；reset=true 时从 0 重来
    ↻ has_more → next_since_revision        GET /stream-frames?since_frame_seq=<frame_seq>
  缺 blob → GET /sequences?hashes=…         GET /usage
GET /usage
```

### 4.3 接口与数据模型

#### 4.3.1 与 agent-core 的契约

`models.py` 以 Protocol 描述 agent-core 的三类记录（`OtlpSpanRecordLike`、`OtlpSpanSnapshotRecordLike`、`StreamFrameRecordLike`）。jiuwenswarm **不 import** agent-core 的具体类，字段契约见 SDD-0022 §4.3.6。

writer 线程内生成的冻结副本：

```python
@dataclass(frozen=True)
class TraceRecordData:
    raw_json: bytes                  # 单 span OTLP JSON，复述型属性为 @oj-seq 引用
    raw_sha256: str                  # 上述字节的哈希
    trace_id: str; span_id: str      # 小写，必填
    parent_span_id: str | None
    start_time_unix_nano: int; end_time_unix_nano: int   # 快照为 0
    session_id: str | None           # gen_ai.conversation.id
    request_id: str | None; run_id: str | None
    agent_mode: str | None           # openjiuwen.agent.mode，或推断为 "team"
    schema_version: str; source: str # "processor" | "processor_snapshot"
    created_at: int                  # 摄入时的 Unix 秒
    execution_subject_id: str = "main"
    execution_subject_display_name / _kind / _parent_id: str | None
    lifecycle: str                   # final | running（provisional 归一为 running）
    record_revision: int; observed_time_unix_nano: int
    update_kind: str                 # final 为 completed；快照为 started / attributes / output …
    logical_size_bytes: int          # 引用还原后的大小
    sequences: tuple[AddressedSequenceData, ...]

@dataclass(frozen=True)
class StreamFrameData:
    session_id: str                  # 必填
    execution_subject_id: str; trace_id: str; span_id: str
    sequence: int                    # ≥ 0
    kind: str                        # text-delta | reasoning-delta | tool-call-delta | usage
    timestamp_unix_nano: int
    text / tool_call_id / tool_name / arguments_delta: str | None   # text 原样保留，不 strip

@dataclass(frozen=True)
class CommittedTraceUpdate:
    session_id: str; trace_id: str
    revision: int                    # 本批该 trace 在会话内的最大 change_seq
    store_epoch: str; lifecycle: str
    frame_seq: int                   # 本批该 trace 的最大帧 rowid
```

#### 4.3.2 磁盘布局

```
<workspace>/.trace/                           # workspace = JIUWENSWARM_DATA_DIR 或 ~/.jiuwenswarm
  traces-YYYY-MM-DD.jsonl                     # SDD-0023 文件导出器（与本文无关，并存）
  sessions/                                   # trajectory_ui.db_path 默认值
    ab/abcdef…<sha256(session_id)>.sqlite3    # 一个会话一个 DB，另有 -wal、-shm
```

- **写连接**：`journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`、`foreign_keys=ON`。
- **读连接**：`file:…?mode=ro` URI 加 `PRAGMA query_only=ON`。
- **schema 版本**：`PRAGMA user_version=3`。
- 首尾带空白的 session_id 直接拒绝。

#### 4.3.3 表结构

| 表 | 主键 / 唯一键 / 索引 | 作用 |
|---|---|---|
| `otlp_span_records` | `ingest_seq` 自增主键；`UNIQUE(trace_id, span_id)`；索引 (session, start DESC, ingest DESC)、(session, ingest)、(session, request, ingest)、(trace, ingest) | **不可变**的 final span 归档。`raw_json` 用 zlib level 3 压缩；`has_error` 由状态码预扫描得出。同一身份以首条 final 为准（`ON CONFLICT DO NOTHING`） |
| `otlp_record_conflicts` | 自增；索引 (trace, span, created_at) | 同一身份再次到达且 sha 不同：记录两个哈希，不覆盖 |
| `trajectory_store_state` | 单行 | `store_epoch`、`max_ingest_seq`、`max_change_seq` |
| `trajectory_current_records` | 主键 (trace, span)；索引 **(session, execution_subject_id, change_seq)**（主体链读取依赖它）、(session, change_seq)、(trace, change_seq) | 每个 span 一行当前状态：`lifecycle`、`record_revision`、`change_seq`、`raw_size_bytes`（逻辑大小）、`update_kind`。**final 行存空 BLOB**，读取时用 `COALESCE(NULLIF(current.raw_json, X''), archive.raw_json)` 回落到归档；running 行存自己的压缩载荷 |
| `trajectory_changes` | `change_seq` 自增；索引 (session, change_seq)、(trace, change_seq) | 修订日志；`raw_json` 恒为空；**目前没有任何读取 API 使用**（§9 R10） |
| `trajectory_frame_spans` | `span_ref` 自增；`UNIQUE(trace_id, span_id)` | 帧所属的 span 只记一次（每帧省去 48 字符的 ID） |
| `trajectory_stream_frames` | `frame_seq` 自增主键（rowid）；索引 (span_ref, sequence) | 只追加的流式帧；`kind` 存整数代码（text-delta=1、reasoning-delta=2、tool-call-delta=3、usage=4）；**没有 session 列**，文件本身就是会话 |
| `trajectory_blobs` | `blob_hash` 主键 | 寻址元素内容（压缩）。`byte_size`；`first_change_seq` 为首次引用时的修订号，**永不刷新**；`created_at` 每次被引用时刷新，用于保留期 |
| `trajectory_sequences` | `seq_hash` 主键；索引 `prev_hash` | 前缀链节点 `seq_hash = H(prev_hash ‖ blob_hash)`，共享前缀共享节点 |

#### 4.3.4 写入状态机

```
                 ┌────────────── running 快照（revision 必须严格递增）─────────────┐
                 ▼                                                                │
   (无) ──快照──► running ──────────────────────────────────────────────────────┘
     │              │ final 到达：archive INSERT（首条为准；sha 不同 → conflicts）
     │              ▼
     └──final──►  final ──► 之后的快照一律拒绝（late running rejected）；final 行 raw_json 置空
                    ▲
   进程重启迁移：running ──► abandoned（update_kind='recovered'）
```

每次被接受的 upsert 都向 `trajectory_changes` 追加一行，其 rowid 写回 current 行的 `change_seq`。

#### 4.3.5 可见性过滤

所有读取都 join 同一个 CTE（`_ELIGIBLE_TRACES_CTE`）。一个 trace 可见，当且仅当同时满足：

- 至少有一条 current record 的 `agent_mode` 属于 `SINGLE_AGENT_CANONICAL_MODES ∪ TEAM_CANONICAL_MODES`（`common/mode_matrix.py`：`agent.work.normal|plan`、`agent.code.normal|plan`、`team`、`code.team`、`team.plan.*`、`team.work.*`、`team.code.*`）；
- 没有任何记录带有集合**之外**的非空模式。

因此 `auto_harness` 等模式的轨迹不会暴露给 Web。

#### 4.3.6 HTTP API

所有路由挂在 Gateway 的 WebChannel 端口，仅在 dual_protocol 模式下存在（环境变量 `WEB_DUAL_PROTOCOL`，默认 `1`）。

**公共行为**：
- **配置实时生效**：每个请求都现读 `trajectory_ui` 设置。
- **访问门禁**（`_validate_access`）：

  | 条件 | 响应 |
  |---|---|
  | 功能关闭 | 503 `TRAJECTORY_DISABLED` |
  | session id 非法 | 400 |
  | 会话元数据查询出错 | 500 `SESSION_LOOKUP_FAILED` |
  | 无元数据 | 404 |
  | 模式未知，或 Team 模式缺 `team_name` | 403 `UNSUPPORTED_SESSION_MODE` |

- **Origin 门禁**：仅当 `JIUWENSWARM_ENABLE_ORIGIN_CHECK=1` 时生效。拒绝 `Sec-Fetch-Site: cross-site`；放行白名单 Origin 与同源请求；否则 403 `FORBIDDEN_ORIGIN`。
- **响应约定**：所有响应带 `Cache-Control: no-store`；错误体为 `{"error","code"}`；未匹配路由返回 JSON 404/405；FastAPI 422 改写为 400；未处理异常返回 500 `TRAJECTORY_REQUEST_FAILED`。整数参数为不超过 2⁶³−1 的十进制 ASCII。

| 方法与路径 | 参数 | 200 响应 | 游标与分页 |
|---|---|---|---|
| `GET /api/trajectory/sessions/{sid}/subjects` | `after_revision`（默认 0） | `{schema_version:1, session_id, items:[{subject_id, display_name, kind, parent_id, record_count, trace_count, first_start_time_unix_nano(str), last_observed_time_unix_nano(str), first_revision, revision, has_error, running}], watermark, store_epoch}` | 只返回 `MAX(change_seq) > after_revision` 的主体；无 DB 时 `store_epoch="absent"` |
| `GET …/subjects/{subject_id}/records` | `since_revision`（默认 0）、`limit` 1–1000（默认 1000）；subject_id 不超过 256 字符 | `{schema_version, session_id, subject_id, revision, reset, records:[{ingest_seq, change_seq, record_id:"trace:span", trace_id, span_id, record_revision, lifecycle, operation:"upsert", observed_time_unix_nano(str), raw_size_bytes, otlp, raw_valid, sequences?:{attr_key:{hash,depth}}}], has_more, next_since_revision, projected_raw_bytes, max_projected_raw_bytes, sequences:{head:[blob…]}, blobs:{hash:content}}` | 按 `change_seq` 有序的**合并后当前状态**增量。`since > current` 或 `0 < since < first` 时返回 `reset=true`。每页字节预算为 `detail_max_bytes`，按逻辑大小计。首条就超预算时返回 `{otlp:null, raw_valid:null, projection_omitted:"record_too_large"}`。`first_change_seq ≤ since_revision` 的 blob 不再下发。主体无记录时 404 |
| `GET …/stream-frames` | `since_frame_seq`（默认 0）、`limit` 1–2000（默认 500） | `{schema_version, session_id, frame_seq, reset, frames:[{frame_seq, trace_id, span_id, subject_id, sequence, kind, timestamp_unix_nano, text?, tool_call_id?, tool_name?, arguments_delta?}], has_more, next_since_frame_seq}` | 会话级，覆盖所有主体，经可见性过滤。`since > max` 或 `0 < since < first−1` 时 `reset`。仅 DB 不存在时 404。**不含** `store_epoch` |
| `GET …/sequences` | `hashes`（逗号分隔 sha256 hex，1–200 个）、`since_revision` | `{schema_version, session_id, sequences:{head:[blob_hash…]}, blobs:{hash:text}}` | 递归 CTE 沿 `prev_hash` 回溯；blob 按 `first_change_seq > since_revision` 过滤（读者按哈希补缺时传 0） |
| `GET …/archive` | `format=otlp`（默认）\| `addressed` | 附件 `trajectory-{sid}[.addressed].archive.json`：`{format:"openjiuwen.trajectory.archive", archive_version:1, session_id, exported_at, store_epoch, revision(str), records:[{record_id, trace_id, span_id, parent_span_id, record_revision, lifecycle, operation, change_seq(str), start/observed/end_time(str), session_id, request_id, run_id, agent_mode, schema_version, source, created_at, update_kind, raw_sha256, raw_json_base64, otlp, raw_valid, sequences?}], [content_addressed:true, sequences, blobs]}` | 单个读快照，不分页。`otlp` 格式把引用还原为完整值；`raw_sha256` 保持存储时（引用形态）的哈希。**只导出** |
| `GET …/usage` | — | `{schema_version, session_id, store_epoch, scope:"session", items:[{trace_id, inference_id, subject_id, start_time_unix_nano(str), usage:{input, cacheRead, cacheWrite, output, reasoning, total}, cumulative_usage}]}` | 取 `gen_ai.operation.name ∈ {chat, generate_content, text_completion}` 且带 `openjiuwen.inference.id` 的 span；进程内缓存以 (epoch, watermark) 为键 |
| `GET …/traces/{trace_id}/spans/{span_id}/raw` | 32 hex trace、16 hex span | 原始字节，`application/json` | 只读 `otlp_span_records`（final），返回**引用形态**字节，不还原 |

#### 4.3.7 推送消息

AgentServer → Gateway（E2A `server_push`）：

```json
{"request_id":"trajectory:{sid}:{trace}:{rev}:{frame_seq}","channel_id":"web","session_id":"…",
 "payload":{"event_type":"trace.updated","session_id":"…","trace_id":"…","revision":42,"frame_seq":1880,
            "store_epoch":"9f…","lifecycle":"running"},"is_complete":false}
```

Gateway 丢弃缺 trace 或 session、`revision < 1` 的推送；`frame_seq` 非法时按 0 处理。

Gateway → 浏览器：

```json
{"type":"event","event":"trace.updated","payload":{"session_id":"…","trace_id":"…","revision":42,"frame_seq":1880,"store_epoch":"9f…","lifecycle":"running"}}
```

只发给 `RoutingKey.session_id` 匹配的连接。没有订阅消息：路由在连接注册时就已确定。

> **Requirement:** 推送 SHALL 只携带游标与代次，不携带记录内容；读者 SHALL 以 HTTP 拉取为准。
> **Scenario:** WHEN 浏览器错过若干 hint（断线）THEN 重连后以本地 `watermark` / `frame_seq` 拉取即可完整追平，无需重放 hint。

> **Requirement:** 读者发现 `store_epoch` 与本地不一致时 SHALL 丢弃全部本地状态，从 `after_revision=0` 重建。
> **Scenario:** WHEN 保留期删除了部分记录 THEN epoch 轮换，读者重建，不会显示已删除的数据或在游标上跳洞。

### 4.4 关键流程

#### 4.4.1 摄入与背压

| 环节 | 行为 |
|---|---|
| Router `_consume` | 只校验身份，**不访问** `raw_json`，保证 agent-core 的惰性编码不落到事件循环上（`a2dae6083`）；拒绝已墓碑的会话；`put_nowait`，队列满时丢弃并计入 `dropped` / `dropped_final` |
| Router 线程 | 按会话懒创建 writer，启动失败的 writer 仍然注册，由它自己的计数器拒收；同 trace 的孤儿记录并入对应 writer；writer 空闲 300 s 后退役 |
| writer `consume`（final） | 删除同 span 的待写快照后入队；队列满时丢弃 |
| writer `consume_snapshot` | 按 `(trace, span)` 保留最新，更旧的修订直接忽略；`qsize + pending ≥ queue_size` 时淘汰最老的待写快照（计入 `evicted_provisional`）；**快照永远不会挤掉 final** |
| writer `consume_stream_frame` | 独立有界队列，**不合并**；满时丢弃并记录缺口 warning |
| writer 循环 | 先取至多 `batch_size` 条 final，再取待写快照；帧每批至多 `max(batch_size, 512)` 条；快照去抖至多 `flush_interval_ms`，final 或帧到达时提前结束；在本线程构造 `TraceRecordData`，坏记录计入 `failed` 但不影响整批 |
| 写失败 | SQLite `locked` / `busy` 时 50 ms 后重试一次；提交失败则该批对 Web 存储丢失，不影响其他导出器（处理器之间由 `SafeSpanProcessor` 隔离） |

#### 4.4.2 写事务（`write_records`）

在一个 `BEGIN IMMEDIATE` 事务内依次执行：

1. 计算本批涉及 trace 的可见性（写前）。
2. 逐条处理记录：
   - 快照：upsert current，追加 change。
   - final：INSERT archive（设置 `has_error`），再 upsert current；sha 冲突时写入 conflicts。
3. `_store_addressed_sequences`：blob 的 `first_change_seq` 取本批最大 `change_seq`；节点写入幂等。
4. `_append_stream_frames`：必要时插入 `trajectory_frame_spans`，再追加帧。
5. `_reconcile_orphans`：用同 trace 内唯一的会话提示回填 NULL 的 session/request/run/mode。
6. 再次计算可见性（写后）。若有原本可见的 trace 变为不可见，轮换 epoch；否则只同步水位。
7. 构建 `_committed_updates`：每个 `(session, trace)` 取最大 `change_seq`、最新 lifecycle、`frame_seq`。

#### 4.4.3 初始化迁移与崩溃恢复

`TrajectoryStore.initialize` 依次执行：

1. 删除旧版（逐行带 `trace_id` 的）帧表。
2. 补齐缺失列。
3. 删除归档已不存在的 final current 行及其帧。
4. 从归档回填 current。
5. **把所有 `running` 行标记为 `abandoned`，`update_kind='recovered'`**，完成崩溃恢复。
6. 对没有模式但带 Team 作用域的 trace 设置 `agent_mode='team'`，这是 `projection.py` 唯一的生产用途。
7. 若有删除、迁移、回填，或水位回退、状态非法，轮换 `store_epoch`。

#### 4.4.4 保留期

`delete_expired` 的截止时间为 `now − retention_days × 86400`，各表口径不同：

| 表 | 判定依据 |
|---|---|
| archive / current / changes / conflicts | `created_at` |
| 帧 | `timestamp_unix_nano` |
| sequences / blobs | 最近一次被引用的 `created_at`（持续被复述的内容不会过期） |

只要删除了数据就轮换 epoch。执行时机：writer 启动时一次，之后在 writer 线程存活期间每 3600 s 一次。

#### 4.4.5 内容寻址存储与增量下发

agent-core 已把 `gen_ai.{input,output}.messages`、`system_instructions`、`tool.definitions` 替换为 `@oj-seq:1:<hash>:<depth>` 引用，并附带 `AddressedSequence`（SDD-0022 §4.3.7）。本文负责：

- **写**：节点表与 blob 表幂等插入。blob 首次出现时记录 `first_change_seq`；每次再被引用时刷新 `created_at`。
- **读 records 页**：
  - 每条记录附带 `sequences:{attr_key:{hash,depth}}`；
  - 页级附带本页涉及链的 `sequences:{head:[blob_hash…]}`，按元素顺序；
  - 页级附带 `blobs`，只包含 `first_change_seq > since_revision` 的内容，即读者"应当还没有"的部分。
- **补缺**：`since_revision` 是 DB 级 `change_seq`。读者切换主体时，可能漏掉其他主体先见过的 blob。读者用 `GET /sequences?hashes=…&since_revision=0` 按链头补齐（§9 R13）。
- **归档**：`otlp` 格式还原为完整值，`addressed` 格式自包含 `sequences` 与 `blobs`。

#### 4.4.6 会话删除（墓碑）

`session_provisioner` 删除非 Team 会话时：

1. `begin(session)`：登记墓碑，Router 此后拒收该会话的记录，并关闭其 writer。
2. 删除会话数据。失败时调用 `abort`，释放墓碑。
3. `commit`：删除 `.sqlite3`、`-wal`、`-shm`。已提交的墓碑在进程生命周期内保留在内存中，用于拒收迟到的记录。

局限：
- Team 会话**跳过**轨迹删除。
- 删除后端只在 runtime 运行期间注册。AgentServer 重启后、首个请求之前就删除会话，会留下孤儿文件（§9 R3）。

#### 4.4.7 回合身份

`SessionTurnTracker.resolve(session, *, continues_turn)` 决定是沿用当前回合还是开启新回合：

- HITL 恢复、steer 进入进行中的回合时，`continues_turn=True`，沿用 `turn.id` / `turn.number`；
- 否则开启新回合，序号加一。

回合身份持久化在 `session.state["trajectory_turn"]`，并随 checkpoint 保存（`d8df43b07`），重启后序号不回退。`/compact` 刻意打开一个不带 turn 的 run span。

#### 4.4.8 启停同步

- `sync_agent_observability()` / `sync_team_observability()` 在**每次请求时**执行。
  - 单 agent 通过 `asyncio.to_thread` 离开事件循环执行（`862d04ccc`）；
  - Team 在事件循环上同步执行，见 §9 R4。
- provider 需求：
  - 单 agent：`agent_observability.enabled`、`trajectory_ui.enabled`、skill 演进、`/debug` 的 `otel_enabled` 强制开启（进程内粘滞），任一为真即需要；
  - Team：`team_observability.enabled`、`trajectory_ui.enabled`、演进，任一为真即需要。
- `sync_trajectory_runtime(settings, demand)` 按 `trajectory_ui.enabled` 启动或停止 Router，并以 `demand` 记账。只要 agent 与 team 中有一方仍需要，Router 就保持运行。
- 关闭顺序：**先从 `SpanRecordProcessor` 注销消费者**（依赖处理器的租约屏障），再排空并关闭 writer，最后释放 provider 需求。

### 4.5 配置与开关

**`trajectory_ui`**（`resources/config.yaml` 与 `observability/config.py`）

| 键 | 代码默认 | 打包值 | 说明 |
|---|---|---|---|
| `enabled` | `False` | `false` | 同时是 agent 与 team provider 的额外需求；启停 Router；Gateway 关闭时返回 503 |
| `db_path` | `<workspace>/.trace/sessions`（相对路径按 workspace 解析） | `""` | 会话 DB 根目录 |
| `retention_days` | 7 | 7 | SQLite 保留天数 |
| `queue_size` | 4096 | 4096 | 同时约束 Router 队列、每会话 final 队列、帧队列、待写快照数、孤儿缓冲 |
| `batch_size` | 64 | 64 | 单事务最大记录数；帧为 `max(64, 512)` |
| `flush_interval_ms` | **500** | **200** | **仅**快照去抖，final 与帧会提前触发 |
| `detail_max_bytes` | 4 MiB（下限 64 KiB） | 4194304 | records 页的逻辑字节预算 |

**硬编码常量**：writer 空闲退役 300 s；保留期扫描间隔 3600 s；SQLite 忙重试 50 ms 一次；关闭排空超时 15 s。

**环境变量**

| 变量 | 默认 | 作用 |
|---|---|---|
| `JIUWENSWARM_DATA_DIR` | `~/.jiuwenswarm` | workspace 根 |
| `WEB_DUAL_PROTOCOL` | `"1"` | 必须开启，HTTP API 才会挂载 |
| `JIUWENSWARM_ENABLE_ORIGIN_CHECK` / `JIUWENSWARM_WS_ALLOWED_ORIGIN_HOSTS` | 关 / 空 | HTTP Origin 门禁 |

**前端开关**：`config.get` / `config.set` 的 `trajectory_ui_enabled` 参数（`app_web_handlers.py`），通过 `update_trajectory_ui_in_config` 写回 yaml。设置页入口为 Settings → Experimental → Trajectory UI（SDD-0026 §4.5）。

**灰度与回退**：
- 关闭 `trajectory_ui.enabled` 后，Gateway 立即返回 503；AgentServer 在**下一次** agent 或 team 请求时停止 Router（没有配置重载钩子，§9 R4）。
- 已写入的 DB 文件保留，按保留期清理（仅限活跃会话，§9 R2）。

## 5. 关键决策与权衡 (ADR)

### 5.1 每个会话一个 SQLite 文件

- **决策**：`<root>/<sha[:2]>/<sha256(session_id)>.sqlite3`，每个会话一个 writer 线程独占写入，Gateway 以只读连接读取。
- **理由**：
  - **隔离**：一个会话写入阻塞或损坏不影响其他会话（测试 `a blocked session doesn't delay others`）。
  - **删除**：删除会话等于删除文件，不需要在大表上做 `DELETE`、也不需要 `VACUUM`。
  - **路径安全**：文件名是哈希，没有路径注入面。
  - **读写分离**：WAL 模式下 Gateway 进程可以直接读取已提交数据，无需额外的服务间 RPC。
  - **单文件内游标简单**：`change_seq`、`frame_seq` 都是本文件的自增序号，不需要 session 列和索引（帧表注释：3.3 万行的会话属性属于文件而非行）。
- **否决的备选**：
  - *全局一个 SQLite*：单写锁串行化所有会话，删除会话代价高，每行都要 session 列和索引。
  - *全局一个 DB 但每会话一张表*：迁移与保留期管理复杂度高，收益有限。
  - *存到外部数据库（Postgres/ClickHouse）*：JiuwenSwarm 是本地优先的桌面或单机服务，不应依赖外部组件。
- **代价**：
  - 保留期只能由活跃 writer 执行，休眠会话的 DB 永不清理（§9 R2）；
  - 跨会话查询（全局搜索）需要遍历文件，本文不支持。

### 5.2 写在 AgentServer，读在 Gateway

- **决策**：span 产生在哪个进程，就在哪个进程写库；Gateway 只读同一文件。
- **否决的备选**：
  - *AgentServer 通过 WS 把记录推给 Gateway，由 Gateway 写库*：大载荷（每条 span 可达 MB 级）要跨进程传输并二次序列化，还会与聊天流量抢同一条 WS。
  - *AgentServer 自己提供 HTTP 读取*：浏览器只连 Gateway；AgentServer 不暴露 HTTP，鉴权和 Origin 校验都在 Gateway 侧。
- **代价**：两个进程必须看到同一个 workspace 路径，分布式部署时不成立（§9 R6）。

### 5.3 推送提示、拉取数据

- **决策**：WS 只推 `trace.updated` 游标提示，数据一律经 HTTP 按游标拉取。
- **理由**：
  - 推送消息很小，可以按 16 ms 和 `(session, trace)` 合并，不会被流式帧淹没；
  - 拉取天然支持断线追赶与幂等重试；
  - 大页可以做字节预算、HTTP 缓存控制和 Origin 校验；
  - 浏览器可以只拉当前关心的主体。
- **否决的备选**：
  - *WS 直接推送完整记录或帧*：连接背压难控，重连后无法得知缺了哪些，需要额外的 ack 或重放机制。
  - *浏览器定时轮询*：空转成本高，延迟取决于轮询间隔。已移除无人读取的 poll interval 配置（`ac365fe52`）。

### 5.4 Current 表与 Archive 表分离

- **决策**：`otlp_span_records` 只保存 final 且不可变；`trajectory_current_records` 保存每个 span 的最新状态。final 行不重复存载荷，读取时回落到 archive（`f959dc03f`）。
- **理由**：
  - archive 是"原始 OTLP 字节"的权威来源，用于 raw 接口、冲突检测、归档导出；
  - current 是按 `change_seq` 的合并增量，读者一次拿到"span 当前是什么"，不需要自己在快照历史中挑选；
  - final 行不重复存载荷，存储减半。
- **否决的备选**：
  - *只存 changes 日志、读取时合并*：读者追赶需要回放历史修订，页面大小不可控。
  - *final 覆盖 current 载荷*：archive 与 current 各存一份，存储翻倍。

### 5.5 快照可合并，final 与帧不可合并

- **决策**：快照按 span 保留最新，允许淘汰；final 永不被快照挤掉；帧独立队列、只追加、不合并。
- **理由**：
  - 快照的语义是"当前状态"，中间版本丢失无害；
  - final 是唯一权威；
  - 帧是增量，丢一帧就永久丢一段文本（SDD-0022 §4.2.6）。
- **代价**：帧队列满时出现缺口，前端表现为 `sequence` 跳号。

### 5.6 以 `store_epoch` 作为缓存失效信号

- **决策**：任何可能让读者已持有的游标或内容失效的服务端变化，都轮换 epoch：迁移、恢复、保留期删除、可见性回退、水位回退。
- **否决的备选**：*对每类变化设计专门的增量通知*（如"删除了哪些记录"）。它需要维护删除日志，读者逻辑复杂，而这些事件本身很罕见。
- **代价**：轮换后读者全量重建，长会话的重建成本较高。

### 5.7 服务端不做 UI 投影

- **决策**：服务端只负责存储、主体链、帧、寻址内容、用量、归档，不输出回合或行的读模型。回合、Step、上下文窗口、工具配对、错误原因全部在浏览器投影（SDD-0026 §5.1）。`projection.py` 只提取 Team 作用域用于迁移修复。
- **理由**（代码与提交记录所体现的取舍；本文评估）：
  - 投影逻辑迭代非常快（09-03 至 09-11 期间前端投影器几乎每天都有语义修正），放在前端不需要迁移服务端存储；
  - 归档导入回放可以复用同一个投影器，保证在线和离线一致；
  - 服务端保持"OTLP 原样加游标"的通用形态，将来其他读者（CLI、分析脚本）也能使用。
- **代价**：
  - 浏览器主线程 CPU 与内存压力（SDD-0026 §9）；
  - v2 事件 schema 只在前端 reducer 中执行校验，服务端无法据此做完整性诊断。

### 5.8 读取预算按逻辑大小计算

- **决策**：`detail_max_bytes` 按 `raw_size_bytes`（引用还原后的逻辑大小）计算，超预算的首条记录只返回索引。
- **理由**：读者收到并展开的是还原后的内容。如果按引用字节计预算，一页"承诺 4 MB"实际可能展开到数十 MB（`span_record_processor.py` `logical_size_bytes` 注释）。

## 6. 影响面与兼容性

### 6.1 BREAKING

| commit | 变更 | 迁移 |
|---|---|---|
| `d8cddd4fd` refactor! | 读取从"按 trace"改为"按执行主体" | 前端同步切换到 subjects 路由（`5156bc395`、`ba7ecd153`） |
| schema `user_version` 3 | 帧表改为 `span_ref`（旧帧表删除）；final 载荷不再复制到 current；载荷压缩 | `initialize` 自动迁移并轮换 epoch；旧的未压缩行仍可读取 |

### 6.2 受影响模块

- **jiuwenswarm**：
  - `observability/*`、`gateway/channel_manager/web/*`、`gateway/message_handler/message_handler.py`；
  - `server/app_agentserver.py`：启动时绑定提示桥，停止时解绑；
  - `agents/harness/agent_observability.py`、`team/team_manager.py`；
  - `runtime/session_provisioner.py`；
  - `common/config.py`、`common/mode_matrix.py`、`resources/config.yaml`。
- **agent-core**：依赖 `SpanRecordProcessor` 的三通道契约、`AddressedSequence`、执行主体属性（SDD-0022）。

### 6.3 兼容性

- **旧会话**：`initialize` 迁移后可读。首次打开会轮换 epoch，前端从头重建。
- **遗留模式字符串**：`/compact` 写入未经 `deprecate_mode` 处理的原始 `params.mode`。若客户端传入旧的单 agent 值（如 `"agent"`），该 trace 会被可见性过滤隐藏（§9 R9）。
- **文件导出器**（SDD-0023）与本文存储相互独立，可以同时开启。
- **`debug_trace`**（`server/runtime/debug_trace/`）：独立的人读文本 dump，写入 `<workspace>/.agent/traces/dump-agent-<sid>.txt`，与本文无关。它的 `otel_enabled` 会强制拉起 provider。

## 7. 测试与验证

jiuwenswarm：`tests/unit_tests/observability/*`、`tests/unit_tests/channel/test_trajectory_http.py`、`tests/unit_tests/agentserver/test_trajectory_runtime_wiring.py`。

| 领域 | 固化的不变量 |
|---|---|
| Sink | 先提交后通知，关闭时排空；队列满时丢弃而不阻塞；帧洪泛不挤掉 final；快照合并与去抖；忙重试在 15 s 关闭期限内完成；外来主体会话被拒绝 |
| Router | 一个会话一个 DB；孤儿记录保留到根记录到达；一个会话阻塞不延迟其他会话；关闭超时的 writer 在空闲退役时保留 |
| Store | 原始字节精确保留，冲突有记录；实时修订能正确 final，迟到的 running 被拒；主体详情增量翻页不跳；重启后 running 标记为 abandoned；Gateway 读者可见已提交的 WAL；Team 与 agent 模式接受、未知模式拒绝；无模式 Team 修复；超预算省略；严格 JSON（拒绝非有限数与非 OTLP）；DB 替换、序列回退、保留期删除时轮换 epoch；载荷压缩且兼容旧的未压缩行；帧只记一次 span；删除旧帧表 |
| 内容寻址 | 增量存储；保留期刷新；恢复中的读者不重复下发已有 blob；OTLP 导出不残留引用；addressed 导出自包含 |
| HTTP | 7 条路由都已注册；迟到读者能追上帧；外来会话与跨会话 raw 访问被拒；`auto_harness` 禁止；遗留单 agent 模式名被拒；Team 需要 `team_name`；未知模式默认拒绝；运行时开关实时生效；归档只导出；所有错误都带 `no-store`；500 返回通用信息；Origin 门禁（含经 web proxy） |
| 运行时接线 | trajectory UI 是额外的 provider 需求；关闭 trajectory UI 保留导出器；关闭时先注销再释放 provider；处理器传入 agent 与 Team 初始化 |
| 提示 | 发送途中能追赶；断线重试；帧前进的提示在合并中保留 |
| 回合 | HITL 恢复沿用回合；重建后序号保持 |
| 删除 | sidecar 文件删除；abort 释放墓碑；迟到记录被拒 |

**验收**：
1. 单 agent 长回合进行中打开轨迹页，≤ 1 s 看到新 Step，逐字看到输出；
2. 杀掉 AgentServer 再重启，未完成的 span 显示为 abandoned，已完成的数据完整；
3. 删除会话后 DB 文件及 sidecar 消失；
4. 一次含 200 次调用的会话，DB 大小相比"无寻址"基线显著下降（可用 `trajectory_blobs` 的 `byte_size` 与逻辑大小对比度量）。

## 8. 可观测性

| 信号 | 位置 | 说明 |
|---|---|---|
| `TraceSinkStats`：accepted / dropped / dropped_final / evicted_provisional / failed / … | `get_trajectory_runtime_sink().stats()` | **没有 HTTP 端点**，只能在进程内诊断；`dropped_frames` 未纳入统计（§9 R8） |
| 帧队列满的缺口 warning | `sink.py` | 帧丢失 |
| SQLite busy 重试与提交失败日志 | writer | 写入失败 |
| `send_push` 失败的 WARNING（每 0.1 s 重试一次）、每次成功推送的 INFO | `agent_ws_server.py` | Gateway 未连接时刷屏；流式时 INFO 较吵（§9 R12） |
| Gateway HTTP 错误码分布（503/403/404/500） | FastAPI 访问日志 | 门禁与读取失败 |
| `otlp_record_conflicts` 行数 | DB | 同一 span 多次 final 且内容不同，属于上游 bug |

**SLI 建议**：`dropped_final == 0`；hint 从提交到浏览器的延迟 P95；records 页响应时间 P95；DB 总占用与 `.trace/sessions` 文件数。

## 9. 风险与回滚

| # | 风险 | 性质 | 缓解与建议 |
|---|---|---|---|
| R1 | Team 导出器误选（SDD-0023 R1）：开启 `trajectory_ui` 会拉起 Team provider，并按误选的 Langfuse 导出器持续报错 | 已确认 | 见 SDD-0023 |
| R2 | **保留期只对活跃会话生效**：`delete_expired` 只在活跃 writer 中运行，休眠会话的 DB 永不清理，删空的文件也不删除；没有任何其他进程清扫 `.trace/sessions` | 设计缺口 | §10 Q2：启动时或定时全目录扫描 |
| R3 | **会话删除不完整**：Team 会话跳过；重启后首个请求前删除会话找不到删除后端，留下孤儿文件；已提交的墓碑常驻内存 | 已知缺陷 | §10 Q3 |
| R4 | **开关延迟与不一致生效**：Gateway 每个请求都现读配置，AgentServer 只在下一次请求时同步，没有重载钩子；两个需求共存时关闭一方，Router 继续写；Team 同步在事件循环上执行，停止 writer 可能阻塞最多 15 s | 已知缺陷 | Team 同步改为 `to_thread`；加入配置重载钩子 |
| R5 | **没有"仅存储"模式**：开启 `trajectory_ui` 会拉起完整的 provider 与导出器（例如打包的 `agent_observability.exporter: file` 会同时写 jsonl）；`sample_rate < 1` 会静默丢失 Web 轨迹 | 设计缺口 | §10 Q4 |
| R6 | **分布式 Team 无跨进程汇聚**；分布式配置模板里残留无人读取的 `telemetry:` 段 | 范围外 | §10 Q5 |
| R7 | `/raw` 返回引用形态字节，不是"精确 OTLP"；进行中的 span 返回 404 | 契约不一致 | 文档化；或还原后返回 |
| R8 | **帧边界问题**：无 session 的孤儿帧会被 Router 缓冲，但 writer 必然拒收（`StreamFrameData` 要求 session）；`dropped_frames` 未进 `TraceSinkStats`；trace 刚开始时，帧早于任何可见记录提交会被过滤，读者游标前进后可能跳过它们（窄竞态）；running 孤儿快照在 final 前不会归并 | 已知缺陷 | |
| R9 | `/compact` 写入原始 mode，遗留值会让 trace 被隐藏 | 条件性缺陷 | 统一经 `deprecate_mode` |
| R10 | **死代码**：cursor 编解码函数、`_trace_summary_from_row`、`TrajectoryCursorError` 导入、`scope_matches`（仅测试用）、`trajectory_changes` 只写不读、`"trajectory."` 方法前缀豁免没有对应方法 | 可维护性 | 清理，或明确保留意图 |
| R11 | Gateway 在 `WebChannel.start` 中注册的 `trajectory_update_broker` 监听在生产中无效（提交发生在 AgentServer 进程） | 死路径 | 删除 |
| R12 | **安全与运维**：HTTP 读取没有用户级授权，Origin 门禁默认关闭；Gateway 断开时推送重试刷 WARNING；每次推送打 INFO；`WEB_DUAL_PROTOCOL=0` 时 API 不挂载 | 风险 | 生产部署前补授权；日志降级 |
| R13 | blob 重发假设：`since_revision` 是 DB 级序号，切换主体时可能省略其他主体先见过的 blob，正确性依赖读者按哈希补缺 | 设计依赖 | 前端已实现补缺（SDD-0026 §4.4.2） |

**回滚**：
- 关闭 `trajectory_ui.enabled`：Gateway 503，AgentServer 下次请求时停止写入，导出器不受影响。
- 删除 `<workspace>/.trace/sessions/` 可清空全部 Web 轨迹，不影响会话本身。
- schema 迁移只向前。回退到旧版本代码前需要删除 DB 目录。

## 10. 开放问题 (Open Questions)

- [ ] Q1 是否为 `TraceSinkStats` 提供诊断端点（含 `dropped_frames`），并在 UI 上提示"轨迹数据有丢失"？
- [ ] Q2 保留期的全局清扫：AgentServer 启动时，或 Gateway 定时按 mtime 删除过期 DB 文件？
- [ ] Q3 Team 会话删除是否纳入墓碑协议？删除后端能否与 runtime 启停解耦（启动即注册）？
- [ ] Q4 是否提供"仅 Web 存储、无导出器"模式（provider 用 no-op 导出器）？是否让 sink 忽略采样（需要 SDK 支持采样前挂载）？
- [ ] Q5 分布式 Team：teammate 进程是否通过 OTLP（复用 SDD-0022 的 `SharedOtlpReceiver` 思路）或专用通道把记录回传 leader 的 sink？
- [ ] Q6 `trajectory_changes` 日志是否保留？可用于"修订历史回看"，否则删除以节省写放大。
- [ ] Q7 是否在服务端对 v2 事件做轻量完整性校验（序号缺口、缺基线），把诊断下沉，减轻前端负担？
- [ ] Q8 `flush_interval_ms` 的代码默认值（500）与打包值（200）是否统一？注释需改为"快照去抖"。

## 11. 评审记录 (Iteration Log)

| 轮次 | 日期 | 版本 | 评审人 | 结论 | 主要修改/讨论 |
|---|---|---|---|---|---|
| 1 | 2026-09-15 | 1 | @alan_cheng | draft | 初稿：由 jiuwenswarm `43e796e1d` 逆向整理，记录存储、推送、读取契约与 13 项风险 |

## 12. 实现记录与偏差

### 实际实现

| 时间 | commit | 内容 |
|---|---|---|
| 2026-08-21 | `e2b76a3ec` | 实时轨迹归档与回放（数据面首版） |
| 2026-08-25 至 31 | `efe23c8d3`、`c4dbc8b93`、`6b81c8b49` | sub-agent 实时稳定；Team 模式存储路径与成员泳道；消费标准 usage |
| 2026-09-03/04 | `a7cc2978d`、`cdb88ad34`、`c5b25bd0d` | 对齐 GenAI semconv；收敛键；回合锚定到 ReAct 循环 |
| 2026-09-08 | `a2dae6083`、`862d04ccc`、`7d2b23473`、`0b8b7ba5a`、`f959dc03f`、`4528407e0`、`07c17949a`、`d8df43b07` | 性能：入口不读载荷、join 离开事件循环、无转发线程路由、状态码预扫描、final 载荷不复制、500 ms 合并窗口、载荷压缩；回合序号持久化 |
| 2026-09-09 | `d8cddd4fd`、`4e96603bc`、`6d00e76c6`、`444d19b2a`、`3e269b58f`、`7e0a67820` | refactor!：按执行主体读取；帧持久化为只追加链并提供服务；帧表去 session 列与冗余索引 |
| 2026-09-11 | `e314482c5`、`a0c29cb21`、`dcd2bd5f6`、`ce95e3bfd`、`c94364d3b`、`ac365fe52` | 寻址序列按前缀存一次；读时增量还原；导出 otlp/addressed 两种格式；帧 span_ref；链还原为数组；删除无人读取的 poll interval |

### 与设计的偏差

代码与文档或注释不一致之处（以代码为准）：

1. `observability/config.py` 与 `test_trajectory_config.py` 称"打包配置显式开启"，但 `resources/config.yaml` 打包值是 `enabled: false`。
2. `flush_interval_ms`：打包值 200，注释写"writer 最大合批等待时间"；代码默认 500，而且它只是快照去抖，final 与帧会提前触发。
3. **"单 Agent"命名已过时**：`config.yaml` 段标题"单 Agent 轨迹观测 Web 数据面"、`trajectory_http.py` 模块说明、`store.py` 与 `mode_matrix.py` 中的注释都这样写，但 Team 会话同样提供服务。
4. `channels/web/AGENTS.md` 称轨迹面板支持"归档导入/导出"，但服务端**只支持导出**，导入是浏览器端回放（测试 `test_archive_route_is_export_only_and_cannot_import_into_sqlite`）。
5. `docs/zh/调试追踪.md`、`docs/en/DebugTrace.md`：
   - 没有提及 `trajectory_ui`、SQLite 存储、`/api/trajectory`；
   - 描述了全局 `debug_trace.enabled`，代码只读 `debug_trace.<agent|code>.enabled`；
   - 称 debug_trace 与 OTel 独立、"OTel 接入在后续阶段"，但 `otel_enabled` 已经会粘滞地强制开启 provider；
   - 中文文档里 `[日志系统](日志系统)` 链接缺少 `.md`。
6. `swarm/DESIGN.md` 列的是 `trajectory_registry`，代码使用 `trajectory_span_processor`；`team_runtime_inheritance.py` 中的 docstring 残留 `trajectory_registry` / `team_trajectory_sink` 旧名。

### 链接

- jiuwenswarm：`jiuwenswarm/observability/`、`jiuwenswarm/gateway/channel_manager/web/trajectory_http.py`、`web_connect.py`、`gateway/message_handler/message_handler.py`、`runtime/session_provisioner.py`、`resources/config.yaml`
- agent-core：`openjiuwen/extensions/observability/span_record_processor.py`、`content_addressing.py`（契约来源）

### 遗留与后续

- §9 R2、R3、R4、R8 优先修复。
- §10 Q1、Q4、Q5。
- 修正 §12 偏差中的文档与注释。

## 13. 参考与关联设计

- **SDD-0022**：轨迹观测框架。`SpanRecordProcessor` 三通道、执行主体、寻址序列、v2 事件。
- **SDD-0023**：轨迹导出。与本文并列的导出器路径；`trajectory_ui` 复用其导出配置。
- **SDD-0026**：轨迹观测前端 UI。本文 API 的唯一生产消费方，负责全部 UI 投影。
