# 出站缓冲积压时 pong 被 drain 扣住，网关连续 8 次 miss 主动断链

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-27 |
| 严重程度 | 高（生产事故 OA.05000010，影响所有长会话 / 团队流式输出场景的 Gateway↔AgentServer 连接） |
| 范围 | `jiuwenswarm/server/agent_ws_server.py`、`tests/unit_tests/agentserver/test_agent_ws_fast_pong.py` |
| 测试基线 | `tests/unit_tests/agentserver/` 全量：6058 passed / 20 failed，失败均为环境类存量问题（Windows symlink 特权、本机环境变量与技能包污染），与本改动无关；agent_ws 相关用例全部通过 |
| 关联 spec | 无（未触及 `S_NN` 契约面：HANDLERS / RequestContext / SERVICE_MEMBERS / sandbox 条件链 / sink 契约均未变） |

## 现象

2026-10-26 生产环境 Gateway 与 AgentServer 之间的 WebSocket 连接反复被网关主动关闭：
网关侧 keepalive 配置为 30s ping / 10s 内要求 pong / 连续 8 次 miss 即 close。事故时段
AgentServer 正向外发送大流量流式输出（团队/对话增量帧），出站缓冲持续积压。

## 根因

`websockets` legacy 协议的 `pong()` 走 `write_frame()` = `write_frame_sync()` + `await
drain()`。失效链条：

1. 出站大流量 → transport 写缓冲积压超过高水位 → `drain()` 挂起等缓冲排空；
2. 库读循环收到 ping 时**内联** `await self.pong(frame.data)`（`websockets/legacy/
   protocol.py` 读循环 ping 分支）——pong 字节其实已同步进缓冲（`write_frame_sync`
   先于 `drain` 执行），但读循环停在 pong 的 drain 行上；
3. 后续 ping 无人读取 → 网关侧连续 8 次 pong miss → 主动 close。

即"读循环被 pong 的 drain 扣为人质"：不是 pong 发不出去，而是读循环悬停导致后续
ping 根本没被消费。复现条件：出站缓冲积压（大流量流式输出）+ 网关周期 ping。

## 决策

新增 `_FastPongServerProtocol(WebSocketServerProtocol)` 覆写 `pong()` 为纯同步路径
（`State.OPEN` 检查 + `prepare_ctrl` 校验 + `write_frame_sync` 直写 transport，永无
await），经 `legacy_serve(create_protocol=...)` 注入；协议类定义包在 `try/except
ImportError` 中，legacy API 被移除时退回默认协议不崩进程。

依据：

- RFC 6455 §5.5.2 对 pong 只要求 "as soon as is practical"，无"等数据队列腾位再回"
  语义；控制帧本就是协议里的独立类别（≤125 字节、可穿插于分片消息中间）；
- websockets 库自身对 close 帧（`write_close_frame`）与 `broadcast()` 均用
  `write_frame_sync` 不等 drain——"重要小帧不等数据面反压"是库内既有惯例；
- 浏览器（引擎层自动回 pong）/ gorilla/websocket（读路径立即回）/ netty（pipeline
  层自动回）均为控制面独立于数据面反压的业界形态。

## 拒绝的方案

1. **扩大 `transports/sink.py` 的 `_DELTA_EVENT_TYPES` 帧礼让名单**（原方案 2）：
   该机制的存在理由是"连接上未读字节过多时增量帧先让路，协议层 pong 才能写进去"
   （sink.py 模块注释原文）。pong 不再走 drain 后这条价值链短路；且礼让名单再大也
   覆盖不了全部帧型，属治标。降级为可选卫生项，不作为本次修复依赖。
2. **顶层 `websockets.serve` 兜底分支同样注入**：websockets 15.x 顶层 `serve` 是新版
   asyncio API，`create_protocol` 接受 `ServerConnection` 工厂，与 legacy 协议类不兼
   容。该分支仅在 legacy 被移除的未来版本触发（当前 15.0.1 主路径必走
   `legacy_serve`），届时再评估新版 API 的等价物。
3. **同步 IO 移入线程池**（原方案 3）：针对 2026-10-15 复现的"事件循环同步 IO 停摆"
   病灶（水印钩子 / session_metadata 同步读 / config 冷加载），与本次"出站积压"病灶
   正交，另行实施，不混入本次提交。

## 验证

- 先红后绿：`tests/unit_tests/agentserver/test_agent_ws_fast_pong.py` 三例——
  1. `drain()` 永不返回时 `pong()` 仍须在 1s 内完成且 pong 帧（`0x8A`）已写入
     transport（修复前该用例超时失败，正是 10-26 故障形态）；
  2. 连接非 OPEN 时静默跳过、不写帧；
  3. `start()` 向 `legacy_serve` 注入 `create_protocol=_FastPongServerProtocol`。
- agentserver 目录全量回归通过（`pytest tests/unit_tests/agentserver/ -o addopts=
  "--asyncio-mode=auto"`；注意裸 `-o addopts=""` 会顺带清掉 `--asyncio-mode=auto`
  导致大批与本改动无关的 async 用例报 "not natively supported"）。

## 已知遗留

- **"8 连 miss"语义收窄**：修复后 pong 迟到只剩"事件循环停摆 / TCP 断链"两种成因，
  "出站饱和"不再通过 pong 迟到隐式暴露。饱和检测应走应用层显式上报（liveness 与
  health 本该分离，类比 k8s liveness/readiness）。
- **网关彻底停消费**时连接保持存活但数据面无进展，需要数据面熔断兜底（如
  `_wait_for_delta_room` 累计等待超阈值主动 close）——进 backlog 评估。
- 2026-10-15 的同步 IO 停摆病灶（事件循环卡死同样会停 pong）由方案 3 处理。
- pong 仍可能迟到的物理边界：TCP 字节流保序（前方积压有限——增量帧有 32KB 背压闸
  兜底）；该边界下 pong 延迟为毫秒级，远低于网关 10s 超时。
