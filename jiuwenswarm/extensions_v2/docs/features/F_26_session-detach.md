# session.detach 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_26 / session-detach |
| 建档日期 | 2026-10-29 |
| 归档日期 | 2026-10-29 |
| 计划与进度 | [EXTV2-21 / Server PR11](../designs/extensions-v2-development-plan.md#extv2-21-界面与主会话关联解除通知) |
| 主责规约 | [S_15](../specs/S_15_session-detach.md) |

## 1. 范围、状态与依据

PR11 依赖 PR08 的平台关联表，按本次要求共同交付在 PR !95 中；两项特性文档分别为 F_25 和 F_26。目标分支 rsi_native_agent_os 基线为 7795851916dd5ce1d34b70f5a20d6d30eafc25e3，Core 基线为 ead700d7d18a247968d64de54918cf5d201a207a。实现、回归和真实服务验证已完成，命令、证据与范围见第 6 节。

## 2. 能力说明与 Claude 对照

session.detach 在平台已经移除一个具体视图的旧主会话关联后通知用户。用户用 attachment_id 清理自己为这次关联创建的资源；该通知不结束会话、不删除会话历史、不停止 Agent，也不改变其他视图的关联。新的主会话提交替换旧关联时自动通知；主动离开时调用 Server 的 session.detach 请求，平台核对关联身份后移除并通知。

Claude 固定声明 bf2323b3021c9d3db7a31e1e7d61068c3f704497 / 2.1.277 的 session.detach 使用 surface、clientId 和 detach/end 原因，CORE 回显 clientId。本地复用 PR08 的 channel + view_id / attachment_id，reason 区分 switch/detach。PR10 的会话删除不在本次接入范围，暂不声称支持 Claude 的 end 原因；传输断开和页面隐藏不会推断为真实解除。

## 3. 事件契约

完整字段、填写方式和不变量见 [S_15](../specs/S_15_session-detach.md)。输入、输出均为 frozen dataclass，平台填写，用户只能观察并原样 next。重复解除不再通知，用户失败或通知等待超时不恢复已解除关联，也不妨碍已经提交的新目标。

用户安装示例包或自己的 ZIP，在 register 中订阅 session.detach。用户不注册平台类型、CORE 或 Ops，不构造 SessionContext；其清理资源应按 attachment_id 区分，避免旧关联的慢清理破坏同视图的新资源。

### 3.1 用户准备与完整注册示例

以下两文件放在 ZIP 根目录，用现有扩展管理入口安装。空匹配模式接收全部；需要筛选时可使用 channel_id、reason 等字段的等值模式。资源按 attachment_id 保存、清理，用户最后原样返回默认回执。

```yaml
schemaVersion: 1
package:
  id: example.session-detach
  version: 1.0.0
  displayName: Session Detach Example
extensions:
  - id: view-resources
    runtime: backend
    module: extension.py
```

```python
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Own resources by association identity, allowing independent view cleanup."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_detach")


class SessionDetachExample:
    """Keep only this package's per-association presentation resources."""

    def __init__(self):
        self._resources = {}

    def register(self, registrar):
        registrar.register_event_handler("session.attach", self.on_attach, {}, "user")
        registrar.register_event_handler("session.detach", self.on_detach, {}, "user")

    async def on_attach(self, event, ops, next_handler):
        receipt = await next_handler(event)
        self._resources[receipt.attachment_id] = {"session_id": ops.session_context.session_id}
        return receipt

    async def on_detach(self, event, ops, next_handler):
        value = event.event_input
        self._resources.pop(value.attachment_id, None)
        receipt = await next_handler(event)
        logger.info("session detach example: session=%s surface=%s attachment=%s reason=%s accepted=%s",
                    ops.session_context.session_id, value.surface_id, value.attachment_id, value.reason, receipt.accepted)
        return receipt


def create_extension():
    return SessionDetachExample()
```

输入中的 session_id 是离开的旧会话，surface_id 是离开的视图，attachment_id 是这次旧关联的身份；reason= switch 时 next_session_id 才可能指出新目标。CORE 回执的 accepted=True 只确认旧关联已解除，不是全部用户展示已完成。完整字段的默认、来源和填写责任以 S_15 第 1 节为准。

## 4. 方法契约

不新增 Ops。新增 Server 业务请求 session.detach，详细请求字段及回执见 S_15 第 2 节。这是跨进程业务入口，进入 Worker 后才触发同名事件，两者不是同一个注册过程。客户端需要使用对应 attach 通知的 session_id、surface_id 和 attachment_id；平台从关联表取得旧上下文。现有 Web 的创建/切换已自动触发替换通知；主动关闭接入者应显式发送请求，本次不修改前端页面自动关闭行为。用户需要把关联身份送到自己的界面时，可在 on_attach 中使用自己的渲染链携带这些业务参数；这不需要改平台业务方法。

例如接入者发送以下原协议请求。示例 ID 必须替换为这次 attach 的真实值；request_id 每次生成不同 UUID。请求返回 detached=True 表示实际移除，重复请求为 False，和事件回执 accepted 的含义分别见 S_15。

```json
{
  "request_id": "client-generated-unique-uuid",
  "channel_id": "web",
  "req_method": "session.detach",
  "params": {
    "session_id": "demo-session-a",
    "view_id": "view-main",
    "attachment_id": "0123456789abcdef0123456789abcdef"
  }
}
```

这不是 `ops.session.detach()`，本次没有提供这个 Ops。平台自己的通知函数也不作为用户 API 发布。

## 5. 接入与兼容方案

```text
Worker 启动 → 注册 attach / detach 类型和默认 CORE → 共享 Ops Build → 用户注册两个 USER
创建/切换提交 → 确认新目标 → 平台提交替换关联
  → 若存在旧关联：detach(旧 session/context) → USER 清理旧 attachment → CORE 确认已解除
  → attach(新 session/context) → USER 初始化 → CORE 确认当前关联
显式 session.detach 请求 → 原 Front/Supervisor 路由 → Worker 核对 channel/view/session/attachment
  → 移除该关联 → detach USER → 默认 CORE → 返回原请求的实际移除结果
```

关键决策：以平台实际关联表为准，不采用请求自报的 previous_session_id；先提交事实再通知，避免用户异常回滚已生效的切换。单纯 foreground visible=False 也可能表示隐藏，不足以解除；转入未跟踪范围时只清理旧主会话关联，不对该范围新增扩展。

替代方案：不把 Gateway WebSocket 断开当作视图关闭，因为同连接可能承载多个视图，稳定 view_id 重连也不应反复 attach/detach。不让默认 CORE 再次移除关联，避免重复执行和用户拦截改变已提交事实。通知失败不自动重试；内存表不跨 Worker 重启持久保存。

## 6. 验收与未决问题

2026-10-31 合并交付验证：本特性与会话查询文档、session.start、session.end、session.attach/session.detach 一起整理到 [Server !99](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/99)，保留四个提交和各自特性文档。合并 PR 中的 session.start 依赖 [agent-core !110](https://gitcode.com/huenrui_/agent-core/merge_requests/110) 的公开 handler_ids；必须先合入 Core !110，再合入 Server。Core 文档因与目标分支重号改为 EV-07 / F_07，接口语义不变。

本次基于 Server `c6a07f909` 和合并最新目标分支的配套 Core，Windows / Python 3.11.15 下 Server 扩展、Runtime、历史、模型上下文与 Gateway 回归 **1744 项通过、3 项跳过**；Core Events/Extension/callback **587 项通过、1 项跳过**。Gateway 的 `test_web_disconnect_unregisters_physical_subscriptions` 在未修改目标分支和合并分支上均因 SERVICE_UNAVAILABLE 失败，最终合跑显式排除该 1 项，未将其计为通过。完整命令与 XML 在 `D:\work_space\.codex-temp\consolidate-server-prs-20261031`：server-final-command.json、server-final-regression.xml、core-command.json、core-regression.xml；基线对照见 gateway-baseline.xml 与 gateway-candidate.xml。本次未重跑真实模型或浏览器，以下旧日期的真实验证保持历史归属。

原 attach/detach 合并交付为 Server !95；本次保留按 channel/view/attachment 身份解除的契约及旧 detach → 新 attach 顺序，增加组合观察者的回归，不把永久删除、断线或隐藏自动解释为显式 detach。

2026-10-29，Windows / Python 3.11.15，Swarm 使用第 1 节固定基线和 PR !95 的 attach/detach 联合变更，Core 为 ead700d7d18a247968d64de54918cf5d201a207a。446 项扩展、原创建/切换、Runtime、生命周期、E2A 编解码、Gateway 代理及 Supervisor 转发回归通过，无失败、无跳过；一项外部 Authlib 弃用提示。此前首轮 48 项定向通过，已包含在回归范围中，不重复累计。未运行格式化或静态检查工具。

```powershell
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 tests/unit_tests/runtime/test_session_attach_provisioner.py tests/unit_tests/runtime/test_session_create_provisioner.py tests/unit_tests/runtime/test_session_switch_provisioner.py tests/unit_tests/runtime/test_session_provision_contract.py tests/unit_tests/runtime/test_session_lifecycle_participants.py tests/unit_tests/runtime/test_runtime_service.py tests/unit_tests/runtime/test_runtime_architecture.py tests/unit_tests/e2a/test_wire_codec.py tests/unit_tests/gateway/test_e2a_proxy.py tests/unit_tests/agentserver/test_supervisor_forwarding.py --asyncio-mode=auto -o log_cli=false
```

| 场景 | 操作与预期 | 已通过的证据 |
| --- | --- | --- |
| 两视图解除一个 | A 的两个视图只移除指定旧 attachment，另一个资源与关联保留 | 真实 USER 包、资源字典、显式请求及单测 |
| 真实切换提交 | prepare/abort 不通知；commit 后 detach 旧会话，再 attach 新目标；重复 commit 不重放 | test_session_attach_provisioner；真实 Front/Supervisor/子 Worker |
| 身份、重复与并发 | 旧 token 不删新关联；同视图重新进入生成新 ID；两个并发解除只一个 True | 真实 STALE_ATTACHMENT/BAD_REQUEST、并发 True/False 与单位测试 |
| 超时、取消和伪造 | 解除事实保留，迟到 CORE 无效，在途计数最终退出，所有输入不可改写 | 真实 Registry 用户包参数化与竞态测试 |
| 原会话不被结束 | 显式解除前后会话文件哈希相同；随后原会话可继续真实聊天 | 真实文件比较与一轮有效用量 |
| 归档与断线 | 归档不隐式解除；已有关联仍可显式清理；稳定 view_id 的重连不发 detach | 真实归档、清理与重连记录 |
| 卸载与来源 | 卸载 USER 后默认解除/切换继续；只用实际平台上下文和 Ops | 真实 ZIP 安装/卸载、源码哈希、子 Worker 进程与包身份 |

真实证据目录：`D:\work_space\.codex-temp\server-session-detach-event-20261029\real-202911`。最终成功运行记录 6 次 attach、5 次 detach；切换通知中旧 session/context/项目根正确，显式解除的旧关联清理不会删除新资源。没有任何用量时已经能通知和读取 root，未手工构造 SessionContext，未 mock 模型/Ops/Adapter。现成用户示例记录 5 次 accepted=True；另有 1 轮显式 Qwen3.8-Flash 业务回合及正用量证明解除不结束主会话。result.json、wire.jsonl、user-handler-results.jsonl、source-before/source-after 保存实际来源与前后一致性；cleanup.json 确认 Worker 退出、端口关闭。

实际接线发现原通用归档执行屏障会先拦截清理请求，现将 session.detach 作为仅清理已有匹配关联的请求交给自身身份核验，归档不会重新执行、恢复或写历史。验收客户端并发请求改用 UUID 避免时间粒度造成的请求编号复用；此前失败运行不作为通过证据，未因该测试客户端问题改动产品路由。

验证边界：真实链从 Front 开始，未验证产品浏览器视觉或真实 Gateway Web 连接；Gateway 此次只补已有 Web 转发表登记并运行代理回归。无非 Windows 实机、远端 CI 或跨重启恢复证据。用户的页面关闭自动请求接入、surfaces Ops、PR10 的结束联动仍未包含；通知异常不恢复已提交事实，旧资源清理不承诺外部副作用回滚。2 秒是每个通知的等待预算，一次切换含 detach 和 attach 两次通知，不代表整个切换只增加至多 2 秒。


## 7. 参考资料

- [F_25 / attach](F_25_session-attach.md)、[S_14](../specs/S_14_session-attach.md)、[S_15](../specs/S_15_session-detach.md)。
- [Claude 固定声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)。

- [事件定义](../../event_types/session_detach.py)、[默认 CORE](../../event_handlers/session_detach.py)、[平台通知](../../../server/runtime/session/detach_notifications.py)、[关联表与实际移除](../../../server/runtime/session/attach_notifications.py)、[用户示例](../../examples/session_detach/README.md)。
