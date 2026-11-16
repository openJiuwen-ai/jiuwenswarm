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
  - jiuwenswarm/extensions/video_duplex/backend/task_adapter.py
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
