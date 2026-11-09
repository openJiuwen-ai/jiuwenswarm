# 同步盘 IO 挂在事件循环上，慢盘时 AgentServer 整体停摆连及 pong

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-27 |
| 严重程度 | 高（生产事故 OA.05000010，2026-10-15 复现日志确认的第二条失效链；慢盘/NFS 场景下影响事件循环上全部请求处理与 keepalive 响应） |
| 范围 | `jiuwenswarm/server/runtime/session/session_metadata.py`、`tests/unit_tests/test_session_metadata_event_loop_stall.py`、`jiuwenswarm/agents/harness/common/rails/runtime_prompt_rail.py`、`tests/unit_tests/agents/harness/common/rails/test_runtime_prompt_rail_event_loop_stall.py`、`jiuwenswarm/server/runtime/prewarm.py`、本文档；同方案的水印钩子病灶（3a）在 relay-claw 仓库 `extensions/image_watermark/extension.py`，随该仓库单独提交 |
| 测试基线 | 全仓 `tests/`（--deselect 排除 1 个存量挂起用例，见已知遗留）：12487 passed / 34 failed / 2870 errors，失败与错误均为环境类存量问题（Windows symlink 特权 WinError 1314、本机环境变量 AHA_* 与技能包污染 pptx-*、npm 构建环境、Windows shell kill 行为、dispatch 表预期集合漂移、全量运行时残留进程串扰——UnicodeDecodeError/WinError 32 均单跑复验通过或 stash 基线复核同样失败），与本改动无关；本改动直接相关面定向回归全绿：3b 171 passed，3c 142 passed |
| 关联 spec | 无（未触及 `S_NN` 契约面：HANDLERS / RequestContext / SERVICE_MEMBERS / sandbox 条件链 / sink 契约均未变） |

## 现象

2026-10-15 复现日志（full.log）中，8 页发布会 PPT 的请求超时，事件循环呈整进程
停摆形态；同一循环上的 keepalive pong 同步停发，网关侧 8 连 miss 主动断链——失效
形态与 10-26 事故相同。差别在路径：10-26 是"出站缓冲积压把 pong 的 drain 扣住"
（B_01 已修），10-15 是"同步盘 IO 把整个事件循环卡死"——B_01 清掉前者后，后者
成为同一故障的第二条独立成因链。

## 根因

AgentServer 事件循环上直接挂着三处同步盘 IO，慢盘叠加时逐次/叠加停摆：

1. **水印钩子（3a，relay-claw 仓库）**：`extensions/image_watermark/extension.py`
   的工具调用钩子在事件循环上循环处理产物（图片读取 / 水印绘制 / 落盘），
   PPT 类请求批量产物时停摆时间随张数线性放大。
2. **session_metadata（3b）**：`preserve_pin_fields=True` 的元数据更新（普通消息
   路径每次必经）原在函数顶部于调用方线程（即 AgentServer 事件循环）同步读
   metadata.json 做 pin 字段合并（`_merge_pin_fields_from_disk`）——高频消息
   场景每次更新都触盘一次。
3. **RuntimePromptRail（3c）**：`before_model_call`（每次模型调用必经）有两处——
   ① 无条件同步读 runtime_state.yaml；② 模型名兜底链
   （`_configured_model_names` → `get_model_names` → `get_merged_config_dict`）
   在合并配置缓存 miss（启动后首调或 `clear_config_cache` 失效）时双 YAML 读 +
   合并（实测 ~150ms）同样跑在事件循环上。

## 决策

三处病灶统一 `asyncio.to_thread` 化 + 启动预热，盘 IO 全部离开事件循环：

- **3a**：循环体提取为模块级同步函数 `_process_artifact_sync(...)`，钩子内
  `await asyncio.to_thread(...)` 逐项调度（改动在 relay-claw 仓库）。
- **3b**：删除调用方线程 merge（`_merge_pin_fields_from_disk` 函数及其在
  `_enqueue_write` 中的两处调用）。落盘正确性由 `_write_metadata_sync` 在
  `_FILE_LOCK` 内 merge 本就保证；落盘完成后（同步分支立即 / worker 分支异步）
  将 merge 结果回写缓存，pin 字段视图仅在落盘窗口内短暂陈旧。
- **3c**：rail 内两处读盘均 `asyncio.to_thread` 化（`_read_runtime_state_sync`
  辅助方法 + `_configured_model_names` 兜底调用）；prewarm 新增阶段 0，启动时
  在线程池里 `await asyncio.to_thread(get_merged_config_dict)` 预热合并配置
  缓存，消除首个真实请求的冷加载。

3c 双层互补而非冗余的依据：to_thread 治所有缓存 miss 场景（含运行中
`clear_config_cache` 失效）；prewarm 治启动后首调——事件循环虽已不停摆，但
首个真实请求的 ~150ms 冷加载时延仍用户可感。阶段 0 放在快速有界序列里
（`warmup_import_and_checkpointer`），使 `ensure_interface_deep_and_checkpointer`
兜底 await 本 task 时即可保证缓存就绪；阶段 3 为慢速 fire-and-forget 且企业版
跳过，不适合承载。

## 拒绝的方案

1. **3b 仅调大队列/调整 worker 并发**：merge 读盘发生在入队之前的调用方线程上，
   队列侧任何调整都无法治理。
2. **3c 仅做 prewarm 不做 to_thread**：运行中 `clear_config_cache` 失效后，
   下一次模型调用的冷加载仍在事件循环上；rail 的 runtime_state.yaml 读取也
   无人治理。
3. **3c 仅做 to_thread 不做 prewarm**：事件循环不再停摆，但启动后首个真实请求
   仍要支付一次 ~150ms 冷加载（线程池调度 + 双 YAML 读），首调时延用户可感。
4. **runtime_state.yaml 结果缓存**：该文件由外部进程（Gateway / CLI 侧）写入，
   AgentServer 侧无失效通知机制，缓存陈旧会导致当前模型 / 模式列表误判，
   正确性风险不可接受。

## 验证

- **3b**：`tests/unit_tests/test_session_metadata_event_loop_stall.py` 2 例——
  1. 慢盘注入（metadata.json `read_text` 延迟 1.5s）下 heartbeat max_gap < 1.5s
     且更新最终落盘（修复前红形态：普通消息更新把事件循环阻塞一整次盘读）；
  2. 跨进程 pin 字段保留——Gateway 写入 pinned=True / pin_order=3 后，
     AgentServer 以陈旧缓存执行普通更新，磁盘 pin 字段不得被覆盖。
  定向回归 171 用例（session_metadata 121 + ops/storage/fork/rewind/route 50）
  全绿。
- **3c rail**：`test_runtime_prompt_rail_event_loop_stall.py` 1 例——合并配置
  缓存强制 miss + 用户 override YAML 读取延迟 1.5s（模板读保持快速，控制变量为
  单次慢读），heartbeat max_gap < 1.5s 且兜底模型名仍注入 runtime.setting
  （修复前红形态：1.55s 停摆）。
- **3c prewarm**：既有 19 个 prewarm 用例全绿；启动日志新增
  `stage=0 name=merged_config` 埋点正常。
  定向回归 142 用例（rails 全目录 + test_prewarm.py）全绿。
- **3a（relay-claw 仓库）**：`extensions/image_watermark/test_extension.py` 新增
  ticker 断言用例，18 passed。
- 全量回归基线见元信息表。

## 已知遗留

- **3b pin 字段视图落盘窗口内短暂陈旧**：merge 结果在落盘完成后才回写缓存，
  同会话读路径在该窗口内可能短暂看不到另一进程刚写入的 pin 变更；窗口为
  worker 队列调度量级，落盘侧始终 merge 磁盘最新值，无正确性影响。
- **to_thread 共享默认线程池**：三处病灶与其他 to_thread 使用者理论上存在极端
  并发下的池竞争（Windows 默认 40 线程），目前未见实例；如未来出现可评估独立
  IO 池。
- **"事件循环上无盘 IO"纪律缺守护**：本次为人工盘点收口，新代码仍可能引入同步
  IO；liveness 指标（事件循环 tick 间隔）上监控/告警可作长期防线，进 backlog。
- **存量挂起用例**：`tests/unit_tests/gateway/test_http_agent_client.py::test_push_loop_stops_when_ephemeral_pod_is_unreachable`
  在本机全量与单跑均挂起（fake SSE server 建立后无进展，`asyncio.timeout(3)`
  保护块之外）；`git stash` 基线复核同样挂起，与本次改动无关（gateway SSE 路径
  与三处病灶零交集）。全量回归以 `--deselect` 绕过。
- 3a 的提交 / 归档在 relay-claw 仓库侧独立处理，本文档仅记录病灶与修复方式。
