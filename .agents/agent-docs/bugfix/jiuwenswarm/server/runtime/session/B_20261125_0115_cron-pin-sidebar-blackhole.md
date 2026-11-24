---
id: B_20261125_0115_cron-pin-sidebar-blackhole
title: 缺 cron_id 的 cron 执行会话置顶后从侧边栏所有区域消失且无法取消置顶
type: B
author: CharlesWang2030
author_email: 93426525@qq.com
designs: []
design_note: 由缺陷分析归档 JW-78 逆向定位，无独立设计文档；方案取舍（入口拒绝 vs 准入放宽 vs 元数据自愈）记录在本文档决策节。
modules:
  - jiuwenswarm/server/runtime/session
  - jiuwenswarm/server/control/store
  - jiuwenswarm/channels/web/frontend
files:
  - jiuwenswarm/common/cron_session.py
  - jiuwenswarm/server/runtime/session/session_metadata.py
  - jiuwenswarm/server/control/store/project_queries.py
  - jiuwenswarm/server/control/repositories/session_repository.py
  - jiuwenswarm/server/runtime/gateway_adapter/session_adapter.py
  - jiuwenswarm/channels/web/frontend/src/multi-session/sidebar/useSidebarMenu.tsx
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
  - jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
  - tests/unit_tests/common/test_cron_session.py
  - tests/unit_tests/server/test_session_metadata.py
  - tests/unit_tests/server/test_project_handlers.py
  - tests/unit_tests/server/test_gateway_adapter.py
---

# 缺 cron_id 的 cron 执行会话置顶后从侧边栏所有区域消失且无法取消置顶

缺陷编号：JW-78。基线：develop `cbe751704f86fcd157f2ef2d251d5b060d168a6c`。

## 现象

存在**缺 `cron_id` 元数据**的 cron 执行会话（存量旧版 team 定时任务执行会话，
`channel_id="__cron__"`、session_id 形如 `cron_<ts>_<job_id>`，job id 只嵌在
目录名里）时，用户在侧边栏定时任务分组的会话列表里 hover 会话行点击置顶：

1. 置顶"成功"（无任何报错）；
2. 该会话从 cron 分组列表消失（列表排除 `pinned`）；
3. 侧边栏顶部置顶区不显示它（`__cron__` 渠道但缺 `cron_id`，准入被拒）；
4. 项目普通会话列表也没有它（`channel_id != "web"` 一律排除）。

会话在侧边栏所有区域不可见：无法点击进入、无法取消置顶（取消的前提是能看到
它）、无法归档/删除。聊天记录仍在磁盘（`~/.jiuwenswarm/agent/sessions/<sid>/`），
用户视角"会话蒸发"。只能靠直接 RPC `session.pin {pinned:false}` 恢复。

对照：有 `cron_id` 的现代单 agent 执行会话置顶后正常出现在置顶区。

## 根因

三个列表对"pinned 的 `__cron__` 会话"的过滤条件没有对齐同一份可见性契约，
交叉出盲区：

| 列表 | 入口 | 对该会话的处理 |
|---|---|---|
| 置顶区 | `load_pinned_sessions`（project_queries.py） | 准入要求 `channel=="web"` 或 `"__cron__"+cron_id 非空` → **不收** |
| cron 分组 | `load_project_cron_sessions` | 排除 `pinned` → **排除** |
| 项目普通列表 | `load_project_sessions` | 排除 `pinned` 且排除非 web 渠道 → **排除** |

而 `set_session_pinned`（session_metadata.py）对会话渠道/类型**零校验**，
任何会话都可置顶成功——入口放行 + 出口三处全拒，构成完整黑洞。

## 修复决策

采用"入口拒绝 + 存量准入放宽"组合（社区 issue 建议的方案 a+b）：

1. **入口拒绝（方案 a）**：`set_session_pinned` 在置顶前检查
   `pinned_session_admitted`（置顶区准入谓词的取反）——非 `web` 渠道且不带
   job 身份（`cron_id` 为空、目录名又不嵌 job id（`cron_<job_id>` /
   `cron_<ts>_<job_id>` 约定））的会话抛 `SessionPinRejection`
   （code=FORBIDDEN），任何写入发生前拒绝。取消置顶
   不走该分支（存量恢复入口保持畅通）。
   - 判定不以 `channel_id == "__cron__"` 为前置：`channel_id` 可经
     `channel_id or ""` 写入路径落空（`update_session_metadata` /
     `sync_session_request_metadata` / 兜底元数据），落空的 cron 名字形态
     会话在 cron 分组（按名字兜底、不按渠道过滤）可见可点置顶，若只认
     `__cron__` 则置顶后同样蒸发（社区检视 Medium 意见场景）。准入谓词
     单一化后渠道缺失不再旁路守卫。
   - 目录名嵌 job id 或有 `cron_id` 的会话不拒：job 身份是 cron 分组兜底匹配
     （`cron_session_matches_job`）与置顶区准入共用的身份信号，置顶后仍可见。
2. **存量准入放宽（方案 b）**：`load_pinned_sessions` 对 `__cron__` 会话的
   准入放宽为"`cron_id` 非空 **或** 目录名嵌 job id"，与 `get_cron_sessions`
   的准入语义对齐——修复前已被置顶"蒸发"的存量 `cron_<ts>_<job>` 会话自动
   回到置顶区，用户可在 UI 中取消置顶恢复。
   准入与拒绝共用 `session_metadata.pinned_session_admitted` 单一谓词
   （内部经 `common.cron_session.cron_session_has_job_identity`），
   任何一侧调整都会同步另一侧，避免两处判断再次漂移出新的可见性盲区。
3. **错误可达**：`session_repository.handle_pin` 与 `SessionAdapter._handle_pin`
   把 `SessionPinRejection` 映射为 FORBIDDEN 错误响应（不再落入
   INTERNAL_ERROR）；前端 `toggleSessionPin` 对 FORBIDDEN 展示专门文案
   （`multiSession.project.errors.pinCronNoIdentity`，中英双语）。
4. **方案 c（置顶时回写 cron_id 自愈）放弃**：pin 时无法从会话自身推出它属于
   哪个 job（随机后缀不含 job id），回写需要 job 上下文，入口拿不到。

### 边界与并发

- 拒绝检查在 `_SESSION_PIN_LOCK` 内、读 metadata 后、幂等短路前：并发重复
  点击同样被拒绝，不会出现部分写入。
- `pinned=False` 一律放行：包括缺身份的会话（存量已置顶数据的唯一恢复路径）。
- `pinned_session_admitted` 与 `load_pinned_sessions` 准入逐字同一谓词：
  `web` 渠道放行；其余渠道一律走 job 身份判定（`cron_id` 非空或目录名嵌
  job id 放行）。`channel_id` 落空的会话不再被默认放行——带身份的 cron
  形态会话置顶后仍可见（cron 分组按名字/cron_id 列出），无身份的拒绝置顶。
- 无身份且目录名非 job 形态的 `__cron___<ts>_<rand>` 会话从来不会出现在任何
  侧边栏列表（cron 分组要求 `cron_id` 或名字匹配），因此不存在"UI 曾可置顶"
  的存量实例；入口拒绝只为封死未来新增的同形会话。

## 验证

- 单元（`tests/unit_tests/server/test_session_metadata.py` 新增
  `TestSessionPinCronIdentityGuard`，10 例参数化）：
  - `__cron___<ts>_<rand>` 无 cron_id → 拒绝（FORBIDDEN）且不落盘；
  - 有 cron_id / `cron_<ts>_<job>` / `cron_<job>` / web 渠道 → 放行；
  - `channel_id` 落空：目录名嵌 job id 或有 cron_id → 放行；无身份 → 拒绝；
    tui 渠道无身份 → 拒绝；
  - 缺身份会话的 unpin 不受拦截。
- 公共谓词（`tests/unit_tests/common/test_cron_session.py`）：
  `cron_session_name_has_job` / `cron_session_has_job_identity` 的名字形态、
  空白 cron_id 与非 cron 会话边界。
- 错误映射（`test_gateway_adapter.py::TestSessionAdapter::
  test_pin_rejection_maps_to_forbidden`）：真实 `SessionAdapter.handle`
  分发（委托 `session_repository.handle_pin`）下 `SessionPinRejection`
  返回 `code=FORBIDDEN`，不退化为 INTERNAL_ERROR。
- 集成路径（`tests/unit_tests/server/test_project_handlers.py` `TestSessionPin`
  扩至 8 例）：
  - 缺身份会话 RPC 置顶返回 `ok=false, code=FORBIDDEN`，metadata 未写
    `pinned=True`；unpin 放行；
  - `channel_id` 落空且无身份的会话 RPC 置顶同样被拒（Medium 意见场景回归）；
  - 存量已置顶的 `cron_<ts>_<job>` 会话出现在 `project.pinned_sessions`
    置顶区，可 unpin 后回到 cron 分组；
  - 目录名嵌 job id 的会话（含 `channel_id` 落空形态）正常置顶并出现在置顶区。
- 前端：`npm run test:sidebar-model`（13 pass）、`npm run test:i18n-locales`
  （6 pass，中英键同步校验）、`npx tsc --noEmit` 通过。
- 回归：`test_project_handlers.py`（73）、`test_session_metadata.py` +
  `test_gateway_adapter.py::TestSessionAdapter` + `test_cron_session.py`
  （234）、`test_control_query_execution.py`、`test_archive_lifecycle.py`、
  `test_app_web_handlers.py`、`test_cron_session_runtime_status.py`
  （372）全绿。
- 已知无关 flaky：`test_gateway_adapter.py::test_upload_chunk_concurrent_
  same_name_does_not_clobber` 在干净基线同样间歇失败（Windows 并发文件创建
  竞态，8 次 2 败），与本次改动无代码交集。

## 遗留

- 置顶区放宽准入后，置顶的 `cron_<ts>_<job>` 会话在置顶区**不显示所属任务名**
  （to_session_info 投影无 job 维度）；与有 cron_id 的置顶会话行为一致，未
  单独处理。
- 前端会话行的 pin 按钮对这类会话仍渲染（点击后被拒并提示）；未做置灰预判
  （需要前端知道 cron_id 缺失与否，Session 投影含 cron_id 字段，可后续增强）。
- `SessionAdapter._handle_pin` 当前不参与 `handle()` 分发（`SESSION_PIN` 委托
  `session_repository.handle_pin`）；其中保留的同源 FORBIDDEN 映射是防御性的，
  避免未来路由回切时退化为 INTERNAL_ERROR。真实生效入口由上述 handler 测试覆盖。
