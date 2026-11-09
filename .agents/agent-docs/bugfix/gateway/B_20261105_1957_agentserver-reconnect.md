---
id: B_20261105_1957_agentserver-reconnect
title: 接入 AgentServer 重启后的 Gateway 重连修复
type: B
author: yandong1109
author_email: yandong@huawei.com
modules: [pyproject.toml, tests/unit_tests/gateway, tests/unit_tests/agentserver, tests/unit_tests/runtime, tests/integration]
files:
  - pyproject.toml
  - uv.lock
  - tests/unit_tests/gateway/test_e2a_proxy_reconnect.py
  - tests/unit_tests/gateway/test_cron_scheduler.py
  - tests/unit_tests/gateway/test_cron_snapshot_retirement.py
  - tests/integration/test_session_steering_sdk.py
  - tests/unit_tests/agentserver/test_a4p_split_process.py
  - tests/unit_tests/runtime/test_runtime_service.py
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
本仓将官方依赖固定到已合入的 66df2594fe7bd05e97133db4a71a0bc8e7801f76，并同步 uv.lock。
该提交是 !31 的官方合并结果，保留此前已合入的群接入、定时任务广播与快照退役调整。
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
首版提交相对当时旧依赖 a9e4d447033f0eadf5e8583c06cfb8993e01e539，也接入独立仓已合入的
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

### 合入前必做检查（当前状态）

- [x] agent-gateway !31 合入官方 develop，最终合并提交 66df2594 在普通分支可达。
- [x] squash 后已将 pyproject.toml 两处 pin 与 uv.lock 更新为官方合并提交；相关回归见下文。
- [x] 全新空缓存 uv lock --check 通过；另从空 Git 缓存实际安装 Gateway 成功，未注入 PR 对象。
- [ ] 新候选平台 CI 通过，确认 Worker 恢复用例结果；检视讨论由有权限的检视人解决。
- [ ] 平台检查通过并解决检视讨论后，由有权限的维护者合并。

未完成的检查不能用本地缓存校验或定向测试代替；本轮没有部署、完整测试集或模型对话验收。


## 2026-11-06 接入官方合并提交

!31 已以 squash 方式合入，原 b53e49d9 不在官方 develop 祖先链上。此前本地
a4e1c9c3 候选未推送，现由正式合并提交 66df2594 替代，不再需要更新已合并的依赖 PR。
主仓基线同步到 acf2b35b263d5813517d8b9fac187d1df0a9c017；目标仍固定 cff3b1b9，
升级到 66df2594 保留现有功能并接入重连修复。后续 Gateway !39 仅新增 ST 辅助脚本，
本次固定在 !31 的合并结果，不盲目追随分支最新提交。

相对本次主仓基线，pyproject.toml 只修改两处 Gateway SHA，uv.lock 只修改 Gateway
及 workswarm requires-dist 的来源元数据，330 个包的版本及其他 Git 提交保持不变。

升级同时接入 Gateway !36 和 !13 的定时任务广播、快照退役、ack session_id 与
proactive.tick 控制保护。母仓残留的旧快照测试和 mock 随契约退役，同步上游
test_cron_snapshot_retirement.py 与广播断言，保留原有母仓测试结构及其他用例。
这些是依赖升级的消费端适配，未在母仓复制 Gateway 运行时代码。

冷缓存验证：使用空缓存从官方 URL 安装固定 SHA（uv pip install --no-deps），
成功获取、构建并安装 agent-gateway；direct_url.json 对应 66df2594，未 fetch PR ref。
空缓存 uv lock --check 同时通过；此命令本身可只校验已有锁的元数据，实际 Git 可获取性
以安装结果为证据，不将毫秒级锁检查等同于完整依赖安装。

随后在全新隔离环境执行锁定安装：

```bash
UV_PROJECT_ENVIRONMENT=/tmp/9628-final-venv \
UV_CACHE_DIR=/tmp/9628-cold-lock-cache \
uv --no-config sync --locked --python 3.11 \
  --no-install-project --no-install-package workswarm-tui
```

初始缓存没有 Git 对象；公开 wheel 下载曾超时，重试后 288 个依赖安装完成。
Gateway、agent-core、runtime-foundation 的安装元数据分别确认
66df2594、f3f8f576、d4a8761c，未注入 PR ref 或使用开发源码替代依赖包。
主仓从当前目录源码导入，未安装主项目 wheel 与 TUI，不代表完整产品构建。

同一环境运行 18 个相关文件：498 项单元测试全部通过（含重连原五文件 86 项、
旧 CI 涉及的 transport stall / Worker recovery 17 项，以及定时任务和消息处理消费端）。
扩展集成测试 test_session_steering_sdk.py 为 20 passed、5 failed；失败集中在
ask_user 恢复后 steering 注入和 goal clear 后 final 内容断言。
总计 518 passed、5 failed，不能表述为整组测试全通过。

基线对照：将未修改的 develop acf2b35b 导出到隔离目录，配合从官方旧提交
cff3b1b9 导出的 Gateway 源码（其余依赖保持同一锁定环境），重跑完整 25 项集成用例，
同样为 20 passed、5 failed，失败用例和断言一致。该基线对照使用原提交源码，
不是旧 SHA 的冷缓存安装证明；旧 SHA 的普通分支 uv 获取仍失败。
因此这 5 项不是本次依赖升级新增的失败，保留原断言，不以放宽断言绕过问题。

本次未修改 Worker 启动预算或运行时恢复策略；平台全量 CI、AI 检视及讨论解决
仍以新提交的实际结果为准，不用定向回归或基线失败归属替代门禁。


## 2026-11-09 处理全量 CI 的测试适配与隔离失败

11 月 6 日 15:52 的 CI 对应 cd12d25b6，UT 为 24214 passed、139 skipped、39 failed。
原重连五文件 86 项全部通过；失败与上文扩展集成测试的 5 项不同，不能混用归因。

- 1 项位于 test_a4p_split_process.py：跨进程测试替代了 _push_cron_command_ack，
  但 ack 替身未接收新依赖新增的 session_id，子进程 TypeError 后父进程报 EOFError / BrokenPipeError。
  这是此前消费端适配遗漏，现补齐关键字参数并将 session_id 传入 E2A 信封。
- 其余 38 项位于 test_gateway_adapter.py（2 项）和 test_prompt_suggestion.py（36 项）。
  Runtime 的 test_dispose_error_resets_checkpointer_state_and_reinitializes 仅隔离了
  _PERSISTENT_CHECKPOINTER_READY，却将 object() 留在 _shared_checkpointer。
  若此前已完成真实初始化，测试退出后 READY 恢复为 true，后续 session runtime 初始化
  会将此占位对象重新装入 SDK factory，导致缺少 release / pre_agent_execute 等方法。
  修正污染源测试，使用 monkeypatch 同时隔离并恢复共享检查点及共享引擎状态。

最小因果复现：先完成真实检查点初始化，再按「故障注入单测 → 会话删除 → 推荐提示」
顺序运行，修正前为 1 passed、2 failed，报错与 CI 一致。
ACK 单测在原候选也复现了同一 session_id TypeError。
修正后在相同锁定依赖环境（Python 3.11.16；Gateway 66df2594）先初始化真实检查点，
按以下顺序运行四个完整文件，337 passed in 29.29s：

1. tests/unit_tests/agentserver/test_a4p_split_process.py
2. tests/unit_tests/runtime/test_runtime_service.py
3. tests/unit_tests/server/test_gateway_adapter.py
4. tests/unit_tests/test_prompt_suggestion.py

测试使用 pytest --asyncio-mode=auto -q --tb=short，关闭 log_cli。
其中跨进程用例以 spawn 启动实际子进程；推荐提示用例保持原有可控模型替身和页面输出断言。
未放宽业务断言、修改运行时代码或变更依赖；未执行本地全量 2 万余项测试，平台需对新提交复验。

依赖决策：本 PR 继续使用官方已合入且冷安装验证通过的完整 SHA。
当前官方 develop 也使用固定 Gateway SHA，uv/pip 均支持这种 VCS 依赖。
未来正式发布可改为版本约束与锁文件配合；branch=develop 配合受控更新 uv.lock 也能固定
uv 安装，但 pip/已构建 wheel 不读取 tool.uv.sources 或本项目 uv.lock，不能只改第 247 行
就认为所有安装路径都保留同一版本。不在本次 UT 修复中改变整个项目的依赖发布策略。
