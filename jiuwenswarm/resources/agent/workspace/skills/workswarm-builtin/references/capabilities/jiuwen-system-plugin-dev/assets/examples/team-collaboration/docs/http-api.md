# 本机 HTTP 接口

基址 `http://127.0.0.1:<动态端口>`（端口由内核分配，见 `docs/lifecycle.md`；验收时以 `/health.port` 为准）。
所有响应：`Access-Control-Allow-Origin: *`、无 credentials、`Cache-Control: no-store`。
**团队数据只读**：团队数据没有任何写端点，`/ui-state` 只接受 9 个视图键。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/snapshot?sid=<session_id>&since=<rev>&wait=<0..25>` | 快照 / 长轮询 |
| GET | `/ui-state?sid=` | 读视图状态 |
| PUT | `/ui-state` | 整体替换（body `{sid, state}`，≤16KiB） |
| PATCH | `/ui-state` | 按 `sid` 合并（数组整体替换；`search`/`canvas` 一层深合并；`null` = 重置为默认） |
| GET | `/health` | 探针（见下） |
| GET | `/web/**` | 静态资源（只读 `web/`，拒绝目录穿越/绝对路径/符号链接） |
| GET | `/` | 等价 `/web/team-info.html` |
| GET | `/favicon.ico`, `/robots.txt` | 204 |
| OPTIONS | 任意 | 204 + CORS 预检（`Allow-Methods: GET, PUT, PATCH, OPTIONS`） |

## `GET /snapshot`

- 首次访问未知 `sid` 时创建 **§2.8 空快照占位**（`revision=1`、`stale=true`、`stale_reason=no_session_map`），
  绝不凭空造团队；`schema_version` 恒为 `"1.1"`。
- `availability.session_map_source` 为 `event`（事件或已保存的绑定）、
  `default_name`（Ops 已核验默认队名）或 `none`（尚无绑定）。
- `since` 省略：直接返回整份快照。
- `since == revision`：长轮询等待（`wait` 秒，`0` 立即返回）。
  超时/无变化 → `{"unchanged": true, "revision", "generated_at", "served_at", "stale"}`
  （v1.6 起带 `stale`/`served_at`，让「过期徽标」在没有新 revision 时也能翻转）。
- `since > revision`（进程重启过）→ 整份快照 + `"revision_reset": true`。
- 每次响应 `served_at` = 当前时刻；`generated_at` = 数据读取时刻（降级时**不变**）。
- `ETag` 仅作信息头，前端**不要**发条件请求。
- 参数非法 → `400 {"code":"BAD_QUERY"}`；「内部异常且从无历史」才 `503`。

### 降级（契约 §5.2.1 D1–D7）

| # | 规则 |
|---|---|
| D1 | 返回上次成功的**全部数据字段**（`team/members/tasks/graph/limits` 原样），绝不置空 |
| D2 | 降级结果**不写回缓存**；缓存只由一次成功重读覆盖 |
| D3 | `generated_at` 保持上次成功时刻 |
| D4 | `served_at` 每次响应刷新 |
| D5 | `revision` 只会在 `false→true` 首次与 `true→false` 恢复时各 `+1`；连续失败不变 |
| D6 | `stale_reason ∈ {ops_error, no_session_map, cold_start}`，必填非空 |
| D7 | `/health.snapshot.stale` 与快照 `stale` 一致 |

从未成功 → §2.8 冷启动空快照（`stale_reason=cold_start`）。

## `GET /ui-state` / `PUT` / `PATCH`

- 白名单 9 键：`active_tab`、`expanded_member_ids`、`member_page`、`selected_task_id`、
  `detail_open`、`search`、`status_filter`、`canvas`、`theme`。
- 顶层出现未知键 → `400 {"code":"STATE_KEY_NOT_ALLOWED"}`，**整体不落盘**（PUT/PATCH 都是全有或全无）。
- `PUT` 整体替换；`PATCH` 按 `sid` 在锁内合并（数组替换；`search`/`canvas` 一层深合并）。
- **`null` = 重置为缺省值，不是删键**（§5.3.1 U1）：`PATCH {"selected_task_id": null}` 后 `GET`
  仍带 `selected_task_id` 且值为 `null`，前端才能区分「用户清空了选择」与「从未设置」。
- **`PUT` 里的 `null` 同义**（U5）：`PUT` 本身是「从缺省出发全量覆盖」，某键传 `null` 即取缺省值，
  不会 400。因此「`GET` 完整 9 键 → 原样 `PUT` 回去」是合法回环（`selected_task_id` 缺省就是 `null`）。
- **任何响应都返回完整 9 键**（U2/U4）：`GET`、`PUT`、`PATCH` 的 `state` 一律是补齐缺省值后的完整对象，
  与 `GET` 同构，前端可整包覆盖；`state` 永不 `null`。
- **`empty=true` 仅表示「该 sid 从未写过」**（U3），此时 `state` 仍是完整缺省对象、`updated_at=null`。
- 缺省值（`GET` 回填与 `null` 重置共用同一份 `ui_state.default_state()`）：
  `active_tab="team-info"`、`expanded_member_ids=[]`、`member_page=1`、`selected_task_id=null`、
  `detail_open=false`、`search={"team_info":"","task_graph":""}`、`status_filter=[]`、`theme="system"`、
  `canvas={"layout_mode":"horizontal","pan":{"x":0.0,"y":0.0},"zoom":1.0}`。
  `canvas` 缺省是**具体对象**：`PATCH {"canvas":{"zoom":2.0}}` 之后 `GET` 得到
  `{"layout_mode":"horizontal","pan":{"x":0,"y":0},"zoom":2.0}`（子键合并 + 其余缺省）。
- 请求体 > 16KiB → `400`；`expanded_member_ids` 上限 200 项；`status_filter` 仅接受 7 个 `TaskStatus` 值。
- TTL 1800s，最多 128 个 scope。

**子键白名单（U7）**：`search` 只允许 `team_info`/`task_graph`（字符串）；`canvas` 只允许
`layout_mode`/`pan`/`zoom`，`pan` 只允许 `x`/`y`（数值）。出现任何未知子键 →
`400 STATE_KEY_NOT_ALLOWED`，且**整条请求不写入**（全成或全拒，与顶层白名单一致）。
必须先做子键白名单校验再做类型校验，这样 `{"search":{"evil":1}}` 报的是白名单错、不会漏成类型错。
`GET` 的 `search`/`canvas` 结构恒定只含上述子键，前端可按固定形状解析。

**子键 `null` 不合法（U6）**：`null` 的重置语义只作用于**顶层 9 键**；`search.team_info: null`、
`canvas.zoom: null`、`canvas.pan: null` 一律 `400 STATE_INVALID`。

**并发写纪律（W1–W4，契约 §5.3.2）**：服务端只保证「每一次写都合法且原子」（per-`sid` 加锁、全成或全拒），
**不能**保证「两页各写自己那一段」。若某页把「上挂载时读到的另一页旧值」一起 `PATCH` 回来，
这个请求在 U1–U7 下完全合规，却会覆盖另一页刚写入的值（lost update）——服务端没有版本向量，
无法区分「用户就是想改回旧值」与「带着旧值误覆盖」。所以并发正确性依赖前端写纪律：
**W1** 只写本页负责的 `search` 子键（团队页 `team_info` / 任务页 `task_graph`）；
**W2** `canvas` 只由任务页写；**W3** 自动 fit 结果不回写；
**W4** 首屏 fit 用**并集判据**：`empty === true` **或** `canvas` 恰为缺省（`zoom == 1 && pan == (0,0)`），
否则严格照存值还原、不二次 fit（`empty` 是 sid 级标记，任意键写入都会翻转它，不能单独用来判 `canvas`）。
**`/ui-state` 全绿 ≠ 并发写一定正确**（见 README L10）。

## `GET /health`

```json
{
  "ok": true, "pid": 1234, "port": 51234, "bind": "127.0.0.1", "started_at": 1699999999999,
  "schema_version": "1.1",
  "snapshot": {"revision": 3, "generated_at": 1699999999000, "stale": false,
               "stale_reason": null, "session_map_source": "event", "scopes": 1},
  "events": {"received": 12, "last_event_at": 1699999999000, "handler_registered": true},
  "ui_state": {"scopes": 1, "ttl_seconds": 1800},
  "draining_ports": [],
  "capabilities": ["snapshot", "long_poll", "ui_state", "ui_state_patch", "cors_star"]
}
```

`bind` 失败时：`ok=false` 且追加 `bind_error`（此时不会渲染任何 `src`）。

## 静态资源

按扩展名给 Content-Type：`.html → text/html; charset=utf-8`、`.js/.mjs → text/javascript; charset=utf-8`、
`.css → text/css; charset=utf-8`、`.svg → image/svg+xml`、`.json/.map → application/json; charset=utf-8`；
未知扩展名 `application/octet-stream`。**不发 `X-Content-Type-Options: nosniff`**（前端依赖浏览器按扩展名执行模块脚本）。

路径解析限制在 `web/` 内：拒绝 `..`、以 `/` 或 `\` 开头、含 NUL、符号链接、非普通文件 → `404 NOT_FOUND`。
