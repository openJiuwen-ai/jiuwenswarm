# 团队协作可视化插件（`webui.team-collab-viz`）

在聊天右侧面板提供两个页签：**团队信息**、**任务关系**。页签内容由插件自带的
本机 HTTP 服务承载（Iframe → `http://127.0.0.1:<动态端口>/web/*.html?sid=<会话>`），
数据来自 `team.monitor_event` 事件触发的整份重读（`ops.team.*`），HTTP 侧**只读**缓存。

- 包 id：`webui.team-collab-viz`，版本 `1.0.0`，`domain: agentserver`
- 接口依据：`outputs/contract-snapshot-api.md` v1.6（schema `1.1`）
- 页签（`conversation.panel.right`，顺序 = manifest **声明顺序**）：`tcv.team-info`（团队信息）、`tcv.task-graph`（任务关系）

---

## 1. 结构

```text
team-collab-viz/
├── manifest.yaml                 # 包身份 + 两条 web.slot.contribution（renderer.kind=webui-tree）
├── README.md                     # 本文件（含全部已知限制 L1–L10）
├── docs/
│   ├── lifecycle.md              # 生命周期锚点、端口释放实测与模式 A/B 口径
│   ├── http-api.md               # /snapshot、/ui-state、/health、静态资源
│   └── install-and-verify.md     # 装机、重载、卸载、验收步骤（含禁止硬刷新）
├── team_collab_viz/              # 后端（Python）
│   ├── __init__.py               # 包标记
│   ├── extension.py              # create_extension()/register() + 两个事件 Handler + Iframe 渲染
│   ├── snapshot.py               # 快照组装与全部派生 + SnapshotCache
│   ├── sessionmap.py             # session → team 映射（事件学习 + ops.state 持久化 + 兜底）
│   ├── ui_state.py               # /ui-state 内存存储（9 键白名单 / TTL / LRU / 16KiB）
│   ├── http_server.py            # 本机 HTTP 服务（CORS / 静态 / 快照 / 长轮询）
│   └── lifecycle.py              # weakref 看门狗、端口绑定与释放
├── web/                          # 前端（Iframe 内独立网页，opaque origin）
│   ├── team-info.html
│   ├── task-graph.html
│   └── assets/                   # app.js / team-info.js / graph.js / layout.js / styles.css / theme.js / icons/
├── tests/                        # unit_*.py（后端单测）、integration_*.py（验收）
└── scripts/                      # 打包与验收脚本
```

## 2. 安装 / 重载 / 卸载

在**运行中的 Swarm 会话内**执行（独立终端没有实例信息，CLI 会报「当前终端没有 Swarm 实例信息」）：

```powershell
# 安装（或升级：先 uninstall 再 install，见 L8）
& $env:JIUWENSWARM_EXECUTABLE extension install <zip 或包目录> --json

# 查询状态
& $env:JIUWENSWARM_EXECUTABLE extension query --json

# 卸载
& $env:JIUWENSWARM_EXECUTABLE extension uninstall webui.team-collab-viz --json
```

装好后点任一会话，右侧出现两个页签（**3 秒内**，无需刷新）。页签里的页面通过
`/health` 自检：`ok=true` 且 `port` 与 `src` 里的端口一致。

> 当前宿主重载成功后会通知已挂载页面重新发送 `ui.ready`。启用后页签未出现时，可点击「重新加载」。

## 3. 数据与刷新

| 环节 | 行为 |
|---|---|
| 事件 | `team.monitor_event`（match `{}`）：学 `session→team` 映射 → **整份重读** `info/members/tasks` + 逐任务 `task_dependencies` → `revision+1` → 唤醒长轮询 |
| 渲染 | `ui.ready`：先确保监听已起（R1）→ 刷新本 scope → 为两个 `instance_key` 各渲染一个 `Iframe` 根（`src` 含 `?sid=`）+ 真实端口 |
| 页面拉取 | `GET /snapshot?sid&since&wait=25` 长轮询；`unchanged` 立即重发；网络失败退化为 3s 轮询并保留现有数据 |
| 视图状态 | `GET/PUT/PATCH /ui-state?sid`，仅 9 个视图键，TTL 30 分钟；**任何响应都返回完整 9 键**（`null` = 重置为缺省值、不删键，`PUT` 亦然），`empty=true` 只表示「从未写过」 |
| 健康 | `GET /health`：`port`、`bind`、`snapshot{revision,generated_at,stale,stale_reason}`、`events`、`draining_ports`、`capabilities` |

**降级口径（契约 §5.2.1 D1–D7）**：读取失败时**保留上次成功的全部数据字段**
（`team/members/tasks/graph/limits` 逐字段不变），只翻 `stale=true` + `stale_reason`，
`generated_at` 保持上次成功时刻、`served_at` 每次响应刷新；`revision` 只在
`stale false→true` 首次与 `true→false` 恢复时各 +1（连续失败不再 +1，避免前端狂刷）；
缓存**只由一次成功重读覆盖**，降级结果不写回缓存。从未成功过 → 走 §2.8 冷启动空快照
（`stale_reason=cold_start`）；无 `session→team` 映射 → 200 + `stale_reason=no_session_map`。

## 4. 已知限制（必须与验收口径一起阅读）

| # | 限制 | 影响 / 规避 |
|---|---|---|
| **L1** | 平台没有 `team_completed` / `task_list_drained` 事件，「团队已完成」是**推导**值 | `team.status_source="derived"`，界面显示「推导」提示；不要当作权威状态 |
| **L2** | `TaskInfo` 无 `summary`/`note`/`reviewer`/`created_at` | 详情浮层只有 `content` 全文 + 负责人 + 前置/后置任务 + `updated_at`，**不显示交接摘要** |
| **L3** | 事件 best-effort（队列 1024、满则丢、不重放） | 快照靠「事件触发整份重读」，不保证事件级精确；`generated_at` 可能滞后于真实变化 |
| **L4** | 通过团队事件记录会话绑定。缺少记录时用 `ops.team.info()` 核验默认队名 `jiuwen_team_<session_id>` | 默认命名的历史会话可恢复；自定义队名仍依赖已经记录的事件绑定 |
| **L5** | 无成员分页/搜索的服务端能力 | 快照返回全量，前端本地分页与搜索 |
| **L6** | 快照**不自刷新**（无事件 = 无更新） | 页面显示 `generated_at`；超 10 分钟提示「数据可能已过期」 |
| **L7** | 视图状态仅进程内存 + TTL 30 分钟，进程重启丢失 | 重启后恢复默认视图（页签/展开/画布都会重置） |
| **L8** | 启用成功后，当前宿主可能未通知已挂载页面重发 `ui.ready` | 在扩展详情点击「重新加载」，重载完成后宿主会通知页面恢复页签 |
| **L9** | **禁止硬刷新宿主渲染进程**（F5 / Ctrl+R / `location.reload()`） | `/chat/<id>` 这类非根路径下相对 `./assets/` 会被 SPA 回退成 `text/html` → **白屏**；一律用**应用内导航**（点会话、切页签） |
| **L10** | **视图状态的并发写入正确性依赖前端写纪律（契约 §5.3.2 W1–W4），服务端拦不住 lost update** | 两个页签是两份独立文档、各自持有挂载时读到的状态：若某页把「另一页的旧值」一起 PATCH 回来，请求在服务端 U1–U7 全部合规，却会覆盖另一页刚写入的值。服务端只能拦「非法键 / 非法子键 / 超限」，**无法拦「结构合法但会覆盖他人新值」的写**。写纪律（前端必须遵守）：**W1** 只写本页负责的 `search` 子键（团队页 `team_info`、任务页 `task_graph`）；**W2** `canvas` 只由任务页写；**W3** 自动 fit 结果不回写（否则会与用户手动平移缩放互相覆盖）；**W4** 首屏是否执行 fit 用**并集判据**：`empty === true` **或** `canvas` 恰为缺省（`zoom == 1 && pan == (0,0)`）→ fit-all；否则严格照存值还原、**绝不二次 fit**。理由是 `empty` 属 **sid 级**标记，任意键写入都会翻转它，单用 `empty` 判 `canvas` 语义上就是错的（实测：团队页写过搜索后再进任务页会在 100% 缩放上停住，8 个节点只看到 5 个）。该判据的前提是「没有任何入口能让视角精确停在缺省值」——**将来新增「重置视角 / 100%」这类入口必须同步改 W4 判据，或由后端在 `GET /ui-state` 提供 `written_keys`**，否则会静默回归。**因此不要假设「`/ui-state` 全绿 = 并发写一定正确」**——它只证明接口契约合规。 |

> **已知副作用（平台行为，非本插件）**：`openjiuwen` 在 import 期会按**相对当前工作目录**的默认目标写
> `./logs/{interface,performance,run}/*.log`。本插件只调用 `logging.getLogger(__name__)`，
> **不配置任何 handler、不写文件、不创建 `./logs`**；若在包根 cwd 跑测试或隔离服务，
> 这些平台日志会落在包目录下（`logs/logs/...`），打包前清理即可。


## 5. 实现要点（排障必读）

- **生命周期锚点 = Handler 对象**。宿主不保留 `create_extension()` 返回值
  （`openjiuwen/core/extension/runtime.py:836` 的 `extension` 只是局部变量，`register()` 返回即出作用域），
  真正的强引用来自 `register_event_handler()` 存下的 bound method（`:97`、`:126`、`:129`，经 `_tracked_handler()` 挂到 EventBus）。
  所以本包以 **Handler 实例**为锚：HTTP 服务由 Handler 拥有，`weakref.finalize(handler)` + **0.2s 看门狗**释放端口，
  端口生命周期 = 一次 activation。以 extension 实例或 `atexit` 作锚点会导致 register 后约 2ms 就关端口 → 页签指向死端口（白屏）。
- 服务注册表放在**非包前缀**模块 `_tcv_local_services`（`sys.modules` 顶层），这样 `reload` 的 `_release_modules()` 摘模块时不会丢注册表。
- `reload`/`uninstall` 后 handler 无人持有 → finalize + 看门狗关端口，实测 **21–106ms** 释放（R8 红线 5s）。
- agent 域包**没有**静态资源路由（只有 gateway 域有 `/extension-assets/`），因此页面必须由插件自己起本机服务。
- Iframe 是 **opaque origin**（宿主 sandbox 无 `allow-same-origin`，无 localStorage），所以：所有响应带
  `Access-Control-Allow-Origin: *`、不带 credentials；页签切换会销毁文档 → 视图状态必须存在服务端（`/ui-state`）。
- 快照**不进入渲染树**（渲染帧预算 256KiB/512 节点），`Iframe` 根只放 `src` + `title`。

## 6. 测试

```powershell
# 后端单测（快照派生 / 视图状态 / HTTP / 扩展装配与降级）
& '<PYTHON_ENTRY>' -m pytest tests/unit_snapshot_test.py tests/unit_ui_state_test.py `
    tests/unit_http_test.py tests/unit_extension_test.py -q

# 隔离加载与验收（QA 维护）
& '<PYTHON_ENTRY>' tests/integration_isolated_load.py
```

单测覆盖：派生（进度/依赖反转/depth/layers/flags/status 判定/中文标签）、9 键白名单与 PATCH 合并、
CORS/OPTIONS/静态内容类型/目录穿越拒绝/`no-store`、长轮询 `unchanged`、降级 D1–D7（数据保留、
`generated_at` 冻结、`served_at` 刷新、`revision` 只 +1 一次、恢复 +1）、
监听先于渲染、`src` 端口 = `getsockname()` 端口、渲染幂等、事件从不抛出。
