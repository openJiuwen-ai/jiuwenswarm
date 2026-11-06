---
id: B_20261105_1957_agentserver-reconnect
title: 接入 AgentServer 重启后的 Gateway 重连修复
type: B
author: yandong1109
author_email: yandong@huawei.com
modules: [pyproject.toml, tests/unit_tests/gateway]
files:
  - pyproject.toml
  - uv.lock
  - tests/unit_tests/gateway/test_e2a_proxy_reconnect.py
designs: []
design_note: 修复既有传输恢复路径，通过独立 Gateway 仓交付，无独立架构设计。
---

# 接入 AgentServer 重启后的 Gateway 重连修复

## 现象、根因与依赖边界

关联 [#6176](https://gitcode.com/openJiuwen/jiuwenswarm/issues/6176)。
Gateway / AgentServer 分开部署，通过稳定 Service DNS 连接。
AgentServer 重启并恢复后，原 Gateway 仍持续拒绝创建会话，重启 Gateway 才恢复。
原因是异常断线令 server_ready=false，统一 unary 代理在客户端尝试按需重连前即返回
SERVICE_UNAVAILABLE，阻断已有恢复路径。fetch_agent_unary 同样受影响。

本仓 develop 已迁出内置 Gateway，实际执行 agent_gateway 包。
实现修复位于 [agent-gateway !31](https://gitcode.com/openJiuwen/agent-gateway/merge_requests/31)，
本仓将官方依赖固定到 a4e1c9c3a9061804610f7644b2f38a552bc3d102，并同步 uv.lock。
该提交将重连修复与 Gateway develop ab7349af2bf82ba946b46da773ffd06f47d50670 合并。
企业版仍使用内置 gateway，独立交付于
[jiuwenclaw !150](https://gitcode.com/ZYQ5333/jiuwenclaw/merge_requests/150)。

## 决策与当前契约

已完成过一次连接建立的 WebSocket 客户端即使传输未就绪，也允许进入已有 send_request 重连路径。
并发请求等待同一次握手；握手超时或取消会清理半连接，保留 URI / 鉴权头供后续请求重试。
显式 disconnect 清除配置。未连接客户端、其他显式不可用客户端保留既有错误语义。
不伪造 server_ready，不增加后台轮询，不自动重放已发出的业务请求。
当前传输契约同步到 [中文协议说明](../../../../docs/zh/E2A-protocol.md#websocket-断线恢复)
与英文版本；E2A 线协议、身份路由及 Runtime 就绪语义不变。

主仓沿正式依赖边界升级，不把已迁出的 Gateway 代码拷回母仓或运行时打补丁。
相对旧依赖 a9e4d447033f0eadf5e8583c06cfb8993e01e539，此次也接入独立仓已合入的
ASR、日志、协议兼容和测试迁移；依赖新增 openai、imageio-ffmpeg，并要求 uvicorn>=0.52.1。
uv lock 实际解析 330 个包，更新 Gateway 提交、uvicorn 0.46.0 → 0.54.0、
httptools 0.7.1 → 0.9.0；其余包版本及 runtime-foundation 提交保持不变。
随后 uv lock --check --offline 通过（使用开发阶段已获取的官方 PR 对象缓存）。

## 2026-11-05 验证证据与边界

Python 3.11.16 / websockets 15.0.1。独立 Gateway 最新基线
e9239e51286df5e34facb5648bbf0d54df53c6a0 上，新增 15 项回归与既有客户端、代理、
鉴权重连、超时测试共 86 passed。最初 9 项新增用例在未修复企业版全部失败。
真实 WebSocket 用例停止服务后在同端口重新绑定，保持原客户端实例，验证请求恢复、
鉴权头保留与服务端请求次数；另外覆盖并发、连接被拒绝、握手超时与在途业务不重放。

已使用全新 bare 仓从官方 agent-gateway URL 按修复 SHA fetch 成功，避免依赖个人 Fork 地址。
但 uv 0.12.5 冷缓存按 SHA 安装时只拉普通分支，未合入的提交无法找到；
开发验证先从官方 refs/merge-requests/31/head 获取同一 SHA，母仓保持固定 SHA。
因此母仓 PR 为草稿，必须等待依赖 PR 合入并做冷缓存复验，不把本地缓存通过等同于可立即合并。
主仓使用从官方 PR 引用构建并安装的 agent-gateway 包（direct_url.json 确认 commit_id 为 b53e49d99119c622b8d1884c2b151f646a78af00），
而非 PYTHONPATH 指向独立源码；同样五个测试文件共 86 passed。runtime-foundation 使用
本仓锁定的 d4a8761c8a561b1ee1b8a4333def0077b308e709 源码提供企业路由依赖。
测试文件为 test_e2a_proxy_reconnect.py、test_agent_client.py、
test_agent_client_reconnect_headers.py、test_e2a_proxy.py、test_agent_request_timeout_policy.py；
pytest 参数为 --asyncio-mode=auto -q --tb=short，并关闭 log_cli。未将候选部署到 Kubernetes，也未执行浏览器登录/模型对话验收。
依赖 PR 应先合入，再合入母仓升级 PR；若 squash/rebase 改变修复 SHA，需同步更新 pin/lock 并重新验证。

## 检视后补充

PR 自动审查指出首次建连与代理请求可能重复建连、清理被二次取消后配置可能丢失。
新增测试在首版修复失败，修正为完成一次建连后才启用代理重连，并在 finally 恢复配置。
同时覆盖首次建连失败后的启动重试窗口，避免代理抢占启动流程。


## 2026-11-06 同步目标分支与合并检查

母仓目标更新到 438c7ebee3eac08392de8b20ee6f7509cc3cf58d 后，pyproject.toml 和
uv.lock 同时升级 Gateway，产生冲突。目标固定的 cff3b1b9 与原修复 b53e49d9
并非祖先关系，不能直接选择任意一侧，否则会遗漏渠道功能或重连修复。
核对 Gateway develop ab7349af 与 cff3b1b9 的 agent_gateway/、pyproject.toml、uv.lock
内容完全相同。因此先将修复分支合并 Gateway develop，得到 a4e1c9c3，
再合并母仓 develop 并更新依赖。Gateway 相对当前 develop 仍只有原修复的五个文件。

锁文件从母仓最新 develop 重新解析，共 330 个包；相对目标分支仅 Gateway SHA 变化，
保留 agent-core f3f8f576、runtime-foundation d4a8761c 和全部 registry 包版本。
uv lock --check --offline 通过，使用导入本地候选对象的开发缓存，不代表冷缓存可安装。

验证使用 Python 3.11.16、websockets 15.0.1、uvicorn 0.54.0：

- Gateway 合并候选的原五个回归文件：86 passed。
- 母仓使用从 Gateway a4e1c9c3 干净源码构建的安装包，在隔离环境执行原五个文件，
  并加入 test_transport_stall_guard.py 和 test_worker_automatic_recovery.py：103 passed。
  runtime-foundation 使用锁定的 d4a8761c 源码；这不是全量 uv sync 环境验证。
- 旧 CI 报告记录 21938 passed、129 skipped、2 failed。其中 Uvicorn 弃用警告已有
  develop 的定向兼容修正，此次同步后通过；Worker 恢复在 CI 的 1.5 秒启动预算内持续超时，
  本地未复现，未据单次通过声称平台稳定性问题已解决，仍需新流水线复验。
- 修正英文协议说明的段落换行。未改变运行时重连实现、健康探针或 Worker 超时策略。

### 合入前必做检查

- [ ] agent-gateway !31 合入官方 develop，确认最终 SHA 在普通分支可达。
- [ ] 若 squash/rebase 改变 SHA，同步 pyproject.toml 两处 pin 与 uv.lock，并重新执行相关回归。
- [ ] 使用全新空缓存执行 uv lock --check 或 uv sync，禁止预先注入 PR Git 对象。
- [ ] 新候选平台 CI 通过，确认 Worker 恢复用例结果；检视讨论由有权限的检视人解决。
- [ ] 完成以上检查后才将母仓 PR 转为可合入状态，并由有权限的维护者合并。

未完成的检查不能用本地缓存校验或定向测试代替；本轮没有部署、完整测试集或模型对话验收。
