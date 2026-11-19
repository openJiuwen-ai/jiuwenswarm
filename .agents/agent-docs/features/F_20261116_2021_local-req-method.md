---
id: F_20261116_2021_local-req-method
title: ReqMethod 本地维护与 E2A 字符串边界
type: F
author: mingo007
author_email: maoqiming1@huawei.com
modules:
  - jiuwenswarm/common/schema
  - jiuwenswarm/common/e2a
  - jiuwenswarm/extensions/video_duplex
files:
  - jiuwenswarm/common/schema/message.py
  - jiuwenswarm/common/schema/agent.py
  - jiuwenswarm/common/e2a/gateway_normalize.py
  - jiuwenswarm/server/dispatch.py
  - jiuwenswarm/extensions/video_duplex/backend/task_adapter.py
  - jiuwenswarm/extensions/video_duplex/backend/video_live.py
  - jiuwenswarm/extensions/video_duplex/backend/video_search.py
  - jiuwenswarm/extensions/video_duplex/extension.py
  - tests/unit_tests/common/test_local_req_method.py
  - tests/unit_tests/common/test_req_method_contract.py
designs: []
design_note: 按用户明确要求迁移现有枚举并独立提交 PR，保持线协议兼容，未单独建立设计文档。
---

# ReqMethod 本地维护与 E2A 字符串边界

## 依据与范围

2026-11-16，基于 JiuwenSwarm develop `d12e51ce1` 实施。协议依据为
`docs/zh/E2A-protocol.md`、`docs/en/E2A-protocol.md`，以及锁定的
agent-protocol `06a75d66eb011762077e8924d0d1e41791a93034` 和
agent-gateway `2c40ac150276477a8079ef7937674b600b6a1420`。
未修改协议仓、Gateway 仓或依赖版本。

## 实现与决策

- 将协议现有 434 个成员和本仓原运行时补齐的 98 个独有成员合为本地 532 个成员，保留名称与值。
- `schema.message` 定义普通 `Enum`，`schema.agent` 复用同一枚举；移除请求枚举的运行时补齐，不再修改 E2A ReqMethod。
- 保留普通 Enum 的比较语义，未改为字符串枚举，以免隐式改变分派行为。
- `Message`、`AgentRequest` 继承共享 dataclass，仅覆盖请求方法类型，保留字段顺序和协议基类兼容性。
- 本地 normalization 包装层在调用协议转换前转成字符串，不修改调用方消息；复用协议转换逻辑，不复制序列化实现。
- Front 和 IPC 原有本地 import 路径自然解析为本地枚举。视频扩展改用本地 normalization，并移除本地枚举成员的运行时探测。
- 测试按边界区分 Gateway 与 Host 枚举；同进程测试替身在跨边界处按字符串值转换，避免依赖两仓对象身份。
- EventType、Mode、响应模型及 EventType 兼容补齐不属本次迁移范围，维持原状。

本记录局部替代原 schema 注释中“复用协议 ReqMethod 对象”的实现约定；共享消息字段、响应与事件契约继续有效。无独立设计偏差。

## 验证证据

本次工作区候选使用 Windows、Python 3.12/3.14。协议源码来自本地 checkout，
已核对 message.py 与锁定版本一致；Gateway 使用锁定版本源码。

| 范围 | 实际结果 | 限制 |
| --- | --- | --- |
| 基线枚举与补齐调用逐项比对 | 434 + 98 = 532，名称和值完全一致 | 一次性基线核对，不要求未来本地枚举与上游持续同步 |
| 新增本地枚举测试，Python 3.12 | 547 passed | 使用 --noconftest |
| 本地枚举、企业身份、normalization、Front 方法校验、拆分 Gateway、视频兼容、诊断 HTTP、飞书回调与部分方法契约，Python 3.14 | 661 passed, 1 deselected | 使用 --noconftest；被排除转发名单测试依赖缺失的 openjiuwen |
| cron runtime、zombie stream、trajectory HTTP 扩展验证 | 采集失败 | 环境缺少 openjiuwen，未形成这些测试的通过证据 |

组合验证命令（设置 PYTHONPATH 指向本仓及上述协议/Gateway 源码）：

```powershell
python -m pytest tests/unit_tests/common/test_local_req_method.py tests/unit_tests/common/test_enterprise_protocol_identity.py tests/unit_tests/e2a/test_gateway_normalize.py tests/unit_tests/common/test_req_method_contract.py tests/unit_tests/agentserver/test_front_method_validation.py tests/unit_tests/server/jiuwenmate/test_split_gateway_contract.py jiuwenswarm/extensions/video_duplex/tests/backend/test_extension_compatibility.py tests/unit_tests/diagnosis/test_diagnosis_http.py tests/unit_tests/gateway/test_feishu_card_callback_routing.py --noconftest -o addopts= -o log_cli=false -q --tb=short -k 'not test_forwarded_methods_exist_in_local_req_method'
```

新增测试逐一将全部 532 个成员通过实际 normalization、JSON、Front parse 和
Worker IPC dump/load，验证字符串值与本地枚举身份；同时验证导入不修改协议
ReqMethod、旧版 fallback、未知方法终态错误、非法参数拒绝及调用方消息不变。
预期来自固定协议成员和原基线补齐调用，而非由新实现生成的快照。

## 结论与遗留

已验证枚举归属独立、现有成员值不变及真实协议转换入口的兼容性，已同步中英文协议说明。
生产引用和混合 Gateway/Host 测试边界已盘点并检视；不是全量运行时验收。
由于当前环境未安装 openjiuwen，全局 conftest 和部分运行时测试不可用；
完整 UT/ST、应用启动及端到端回归仍需完整依赖环境和 CI 验证。
按仓库约束未运行格式化或静态检查工具。
修改枚举、协议版本、normalization 或 Front/IPC 装配时应重跑上述验证。

## 2026-11-16 评审后清理

按用户确认，删除视频扩展启动时对 `VOICE_TASK_CHECKPOINT_ACK` 的动态检查，
以及对应的模块 mock 和缺失成员测试。该成员已与扩展同包维护；检查本地枚举
既无独立版本兼容意义，也不能证明远端支持此方法。其他依赖能力检查保留。
此前 661 项通过属于初版记录；本次候选重跑上述相同组合命令，结果为
`660 passed, 1 deselected`，减少的一项是已删除的过时测试。
正常插件注册测试在没有 schema 模块 mock 时仍通过；环境与未覆盖限制不变。

## 2026-11-17 UT 门禁修复

门禁报告对应提交 `3d2fd38c9ff44e9ad7ebe2ce64e090f7d281907c`，
Linux / Python 3.11.5，生成于 2026-11-16 22:03:06，结果为
25165 passed、2 failed、224 skipped。
报告：[PR !10318 UT](https://openjiuwen-ci.obs.cn-north-4.myhuaweicloud.com/jiuwenswarm/ut/10318/unit_test_report.html)。

两项失败均来自此前遗漏的同进程测试边界：

- `test_session_upload_read.py::test_enterprise_raw_file_reuses_tenant_rpc_and_streaming`
  将 Gateway 枚举直接装进 Host AgentRequest，Host 分派判定为不支持方法，HTTP 返回 400。
- `test_trajectory_usage_http.py::test_http_exposes_session_scoped_request_usage`
  将 `TrajectoryHost.call` 直接注入 Gateway HTTP 服务，未经过字符串转换，同样返回 400。

修复限定在测试替身：按 `.value` 转为 Host 本地枚举，并将跨仓调用断言改为比较线协议字符串。
保留原 HTTP、租户隔离、流式读取与用量数据断言，不放宽生产分派，不重新绑定上游枚举。
新增两项回归用真实 `fetch_agent_unary`、JSON、Front 与 IPC 路径验证这两个方法
转换为本地枚举，同时保留 session、user、group 和 bot 身份。

本地验证：上述组合命令为 `662 passed, 1 deselected`。
另用真实 Gateway HTTP 服务及 TrajectoryHost、注入 settings/reader 复现修复前 400、修复后 200；
用真实 WorkspaceFileAdapter 分派复现修复前 BAD_REQUEST、修复后成功，后者仅 stub 文件 I/O、
未经过租户包装层。两项原始完整用例仍受本地 openjiuwen 缺失限制，文件读取还依赖 Linux
文件描述符接口；不能将局部复现视为完整 UT 已通过，需新一轮 CI 确认。

## 2026-11-17 develop 冲突处理

合并 develop `7cc474c95`，保留其测试文件整合与团队控制功能，不重写已有提交。
本节续写合并后候选的实施与验证；上述 532 个成员及历史测试结果仍对应当时版本。

- 将上游新增的 `TEAM_CREATE`、`TEAM_GET`、`TEAM_STOP`、`TEAM_MEMBER_JOIN`、
  `TEAM_MEMBER_LEAVE`、`TEAM_MEMBERS_LIST`、`TEAM_QUOTA_GET` 纳入静态本地枚举，
  当前共 539 个成员，名称和字符串值保持不变。
- 不保留上游新增的请求枚举动态补齐函数，删除 dispatch 和相关测试中的补齐调用；
  企业版 handler 注册仍由 `is_enterprise()` 控制，枚举存在不代表个人版开放此能力。
- 保留上游删除旧测试文件的结果，将 trajectory HTTP 测试替身的跨仓枚举转换、
  企业协议身份与拆分 Gateway 测试断言迁移至 `test_channel_regressions.py`、
  `test_common_regressions.py` 和 `test_server_jiuwenmate_regressions.py`。
- 使用上游更新后的文档索引工具重新生成索引，不手工拼接冲突内容。

验证环境仍为 Windows / Python 3.14，协议及 Gateway 使用前述锁定版本源码，
测试使用 `--noconftest -o addopts= -o log_cli=false`：

| 范围 | 实际结果 | 限制 |
| --- | --- | --- |
| develop 枚举目录逐项比对 | 539 个成员的名称和值完全一致 | 依据锁定 E2A 目录、develop 补齐调用与团队控制清单，一次性比对 |
| 本地枚举、normalization、方法契约、Front、视频兼容、诊断 HTTP、飞书回调 | 660 passed, 1 deselected | 转发名单测试仍因缺少 openjiuwen 排除 |
| 团队路由固定测试 | 13 passed | 覆盖路由与本地枚举边界，不代表完整企业运行时验收 |
| 上述三个整合模块中的迁移用例 | 采集失败 | 整合模块的顶层导入依赖缺失的 openjiuwen，需 CI 验证 |

全部本地成员的 Front / IPC 往返测试自动覆盖新增的 7 个团队方法；导入不修改
协议枚举的回归继续通过。完整 UT 门禁仍需本次合并提交触发的新一轮 CI 确认。

## 2026-11-17 按要求 rebase

按用户要求，将原始三个提交重放到 develop `24fdb1bf7`，不保留 merge 提交。
上节记录保留为历史证据，其冲突处理已纳入重放后的提交：新增团队成员和企业门控、
整合测试中的本地枚举转换及此前 UT 修复全部保留。逐文件对象比对确认，本 PR
涉及的生产代码和测试与变基前 `7666a953f` 内容一致，文档索引保留最新上游记录。

变基后在相同环境重跑上述定向组合并加入团队路由测试，结果为
`673 passed, 1 deselected`。openjiuwen 缺失和完整门禁待 CI 的限制不变，
不得将变基前 CI 状态视为新提交的验证结论。

推送期间 develop 又前进到 `598a0ca1b`（清理已迁往 Gateway 的重复测试），
已再次 rebase：保留上游对 `test_issue_5448_zombie_stream_terminal_frame.py`
的删除及 `gateway_server_fakes` 导入调整，不恢复重复用例；保留仍在本仓的
trajectory HTTP 与 A4P 测试边界修复。最终基线为 `598a0ca1b`，仍为三个
线性提交，最终候选再次运行同一组合得到 `673 passed, 1 deselected`。

## 2026-11-17 补齐 Gateway 动态注册的视频方法

`fe428f551` 的本地枚举遗漏 `VIDEO_CONVERSATION_APPEND`，导致 dispatch 构建
handler 表时发生 AttributeError。该成员不在锁定 E2A 原始目录中，而由锁定
Gateway 的 `common/protocol_methods.py` 动态注册；此前共享枚举时可以间接获取，
改为本地独立枚举后不再随 Gateway 导入补齐。此前目录比对只覆盖 E2A 和本仓
补齐，未覆盖 Gateway 补齐，因此不能作为运行时完整目录的证据。

- 在本地静态声明 `VIDEO_CONVERSATION_APPEND = "video.conversation.append"`，
  当前共 540 个成员，不恢复生产代码对 E2A/Gateway 枚举的依赖或动态修改。
- 新增独立子进程回归，将锁定 Gateway 实际注册后的完整目录作为外部预期，
  检查本地成员是否缺失、同名线协议值是否一致，避免只遍历本地已有成员。
- 独立 schema 导入测试确认无需先导入 Gateway 即可使用此成员；真实
  `fetch_agent_unary -> JSON -> Front -> IPC` 回归增加该方法，验证本地身份与租户路由。

在补齐生产代码前，两项新增回归均失败，明确报告缺失的视频方法；补齐后，
Windows / Python 3.14 下同一定向组合得到 `676 passed, 1 deselected`。
所排除转发名单测试及完整 dispatch 导入仍受缺少 openjiuwen 限制，完整 UT 待 CI。
额外尝试视频会话追加用例时，采集在既有 `task_adapter.py` 导入不存在的
`jiuwenswarm.common.e2a.constants` 处失败；该文件的导入不属于本次改动，
因此不将该用例记为通过，也不扩大本次枚举修复范围。

## 2026-11-19 最新基线遗漏核对

按用户要求拉取最新 develop `3c3556f61` 并 rebase，保留线性提交历史。
验证依赖随上游更新为 agent-protocol `defa6d8d8904eb3272a58fa0ad13f94f7adb4a51`
与 agent-gateway `d68e80bf7abc5a6f28e7178ae79c2ea644308259`，通过独立临时
checkout 固定源码版本，未修改其他任务的依赖环境或本仓依赖 pin。

发现并处理以下遗漏：

- 最新 develop 新增 `SKILLS_REBUILD_STATUS = "skills.rebuild_status"`，
  已在 rebase 冲突处理中纳入静态本地枚举，保留新 dispatch 与 adapter 路由。
- 最新锁定 Gateway 另有 `MODE_SET`、`MODE_GET`、`SKILLS_UPLOAD_STAGE`、
  `SKILLS_UPLOAD_FINALIZE` 四项动态补齐，本地原先缺失。目录对照和完整转发
  名单测试均实际失败后补齐，线协议值保持一致，当前共 545 个本地成员。
- `video_live.py` 仍经 Gateway 间接引用 E2A 请求枚举，现统一改用本地枚举；
  视频追加测试改为断言本地成员身份，跨仓 fetch 回归加入上述四个方法。
- 为实际执行视频回归，修复两处生产代码及一处测试中已不存在的
  `jiuwenswarm.common.e2a.constants` 导入，改用与其他调用方一致的
  `e2a.constants`。这不改变常量归属，也不重新引入 E2A ReqMethod 引用。

核对范围与实际证据：

| 范围 | 结果 |
| --- | --- |
| 锁定 E2A 原始目录、Gateway 实际注册后目录、develop 本仓补齐及团队清单的并集 | 545 项与本地名称和值完全一致，无缺失、无值差异、无额外项 |
| 1574 个生产 Python 文件中的 ReqMethod 直接成员与字面量 getattr/hasattr 引用 | 共 463 个不同成员，无未定义引用；无 E2A/Gateway ReqMethod 直接导入残留 |
| 枚举、normalization、Front、IPC、方法契约、视频兼容、诊断、飞书回调及团队路由 | 686 passed，含此前未执行的完整转发名单用例，无 deselected |
| 视频 live 和 task queue 两个完整模块 | 300 passed，使用 --asyncio-mode=auto |

测试使用 Windows / Python 3.14、`--noconftest -o addopts= -o log_cli=false`。
本次共 986 项定向测试通过；完整 dispatch 装配、全量 UT/ST 和端到端行为仍需
完整 openjiuwen 依赖环境与 CI 验证，不将枚举可解析等同于业务 handler 已实现。
上述并集对照与引用盘点只针对本次固定版本，依赖或业务方法变化时必须重验。
