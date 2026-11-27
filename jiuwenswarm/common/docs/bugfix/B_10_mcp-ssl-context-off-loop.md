# MCP 建连在事件循环上同步加载 CA，周期扫描让 AgentServer 停摆数秒

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-18 |
| 严重程度 | 中：每次远程 MCP 建连都让主事件循环停摆 0.5~3s，所有会话的流式输出、WS 心跳、工具调用同时卡住 |
| 范围 | mcp_call_timeout_patch.py：SDK 工厂包装与 connect 包装；sse / streamable-http |
| 测试基线 | Python 3.11，MCP 相关 9 个测试文件 290 项，除 1 项既有失败外全部通过 |
| 关联 spec | S_09_mcp-integration.md，N3 |
| 外部问题 | OfficeAce 1.7.0 反馈 E-1115-1631-c0e3 的日志中观察到（不是该问题的根因，见下） |

## 现象与根因

E-1115-1631-c0e3 的 AgentServer 日志中，LoopMonitor 报了两次 2.5~2.9s 的停摆（14:31:39、
14:41:31，间隔约等于注册表扫描周期 600s），主线程栈为
`_scan_one → _discover → StreamableHttpClient.connect → streamablehttp_client →
create_mcp_http_client → httpx.AsyncClient.__init__ → ssl.create_default_context`。

mcp SDK 每次建连都新建一个 `httpx.AsyncClient`，httpx 在构造时同步执行
`ssl.create_default_context(cafile=certifi.where())`，在主事件循环上解析整份 CA。
本机实测单次约 0.5~1.3s，用户机器上约 2.5~3s。远程连接器周期扫描、请求级 worker
建连与重建，都会让整个 AgentServer 停摆。

同一份日志里另有一次 79 分钟停摆，那是系统睡眠：桌面端 07:10:07Z 记录 suspend，
08:29:27Z 记录 resume。停摆窗口内的主线程采样都在 `_poll`，采样线程本身也没有运行。
该次停摆与本缺陷无关。

## 决策

- connect 包装（timeout 补丁里既有的 async 包装，扫描和 worker 建连都经过它）先
  `await asyncio.to_thread(httpx.create_ssl_context)`，构建一次后按
  (`SSL_CERT_FILE`, `SSL_CERT_DIR`) 缓存。这与 httpx 默认 `verify=True, trust_env=True`
  选证书源的口径相同。
- SDK 工厂包装（`sse_client` / `streamablehttp_client`）在缓存就绪时注入
  `httpx_client_factory`。工厂与 `create_mcp_http_client` 同口径（默认 timeout、headers、auth），
  只多传 `verify=<共享上下文>`。
- 以下任一情况不注入，完全走 SDK 默认路径：缓存未就绪、调用方已指定工厂、按位置传参、
  SDK 签名不接受 `httpx_client_factory`、构建失败。构建失败只记 warning，原来的错误仍由
  SDK 在原处抛出。
- 共享同一个 SSLContext 是 httpx 推荐用法。所有注入的客户端都是 http1，httpcore 每次连接
  设置的 ALPN 与默认路径相同。

## 拒绝的方案

- 全局替换 `httpx._transports.default.create_ssl_context`：会影响进程内所有 httpx 客户端
  （LLM 等），波及面过大。
- 把 SDK 工厂整体换到线程执行：工厂在 SDK 的 async 生成器里同步调用，无法 await。
- 后台线程预热、首个建连不等待：首个建连仍会在主循环上加载 CA，且后台任务会干扰
  测试的任务收敛检查。

## 验证

- 新增 5 项测试，修复前全部失败：只构建一次且在工作线程；防漂移 2 项（与 SDK 默认工厂逐项
  一致）；构建失败或调用方自带工厂时不注入；证书环境变量变化后不复用旧上下文。
- 两个既有测试的调整：`test_mcp_remote_lifecycle` 的 `transport` fixture 用内存传输，不会
  建 httpx 客户端，预置一份缓存，避免首个建连在 0.05s 预算里真实加载 CA；
  `test_legacy_connect_cannot_swallow_timeout_cancellation` 同样预置缓存，保证 0.01s 预算
  落在被测的旧 connect 上，不会因建上下文而"碰巧"超时。两处都没有放宽断言。
- 实测 5 次远程建连（本机）：修复前，第 2~5 次每次在 `create_default_context` 卡
  1180~1280ms；修复后为 15~20ms。首次建连仍有约 1.5s 卡顿，来自首次模块导入
  （importlib），与本缺陷无关。
- 真实链路复现脚本（`.agents/testing/repro_ppt_transport_cancel.py`）A/B/C 三个场景
  照常通过。
- 既有失败 `test_registry_scan_timeout_and_shutdown[sse]` 与
  `test_mcp_patch_test_isolation` 的逆序两项，在 dev-stable 上同样失败，与本次无关。

## 已知遗留

- timeout 补丁在 DeepAdapter 构造时才装配。早于此的启动期扫描仍走 SDK 默认路径，只影响一次。
- 首次建连的模块导入卡顿不在本次范围内。
