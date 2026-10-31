# session.end 特性设计

| 项 | 值 |
| --- | --- |
| 编号 | F_24 / session-end |
| 建档日期 | 2026-10-29 |
| 归档日期 | 2026-10-29 |
| 计划与进度 | [EXTV2-19 / Server PR10](../designs/extensions-v2-development-plan.md#extv2-19-主会话结束通知与用户资源清理) |
| 主责规约 | [S_13](../specs/S_13_session-end.md) |

## 1. 范围、状态与依据

在最新扩展目标 rsi_native_agent_os（7795851916dd5ce1d34b70f5a20d6d30eafc25e3）开发，使用现有 Core ead700d7d18a247968d64de54918cf5d201a207a，不依赖未合入 PR04/05/06 或 Core !110。实现、定向回归和真实服务验证已完成，证据与范围见第 6 节。

当前永久删除、归档、连接断开、Runtime 缓存清理相互独立。采用已有 Runtime 删除参与者协议，在明确的主会话永久删除收尾阶段发通知；不把每轮完成或缓存释放当作会话结束。不修改 Team/子 Agent，不添加前端入口或额外用户 Ops。

## 2. 能力说明与 Claude 对照

`session.end` 让用户在主会话永久删除流程中，释放自己为该会话准备的缓存、订阅和连接。Runtime 已停止目标执行、准备释放资源时，Worker EventBus 调用 USER Handler；用户依据平台给出的 session_id 清理自己的资源，再继续默认通知 CORE。平台随后继续原资源释放和文件删除。事件不会替用户执行清理，也不允许否决删除。

Claude 固定提交 bf2323b3021c9d3db7a31e1e7d61068c3f704497（声明文件头 2.1.277）的 session.end 包含引擎提供的结束原因、会话身份和恢复资料，默认返回会话身份。本地保留只读观察与继续默认链的用途，首版只覆盖 session_deleted，不伪造 resume、进程退出、clear 或切换会话信息，不承诺直接复用其类型。

PR06 的 session.start 按包激活实例首次输入前初始化；本事件按主会话删除收尾通知，两者粒度不同。用户的会话资源应按 session_id 管理，不能只保存一份“最后打开会话”的资源。

## 3. 事件契约

全部输入输出字段、默认和权限见 [S_13 第 1 节](../specs/S_13_session-end.md#1-输入输出与填写规则)。输入为 SessionEndInput，所有字段由平台填写；用户不传入目标/原因，也不能改写。accepted 回执只说明默认通知已完成，不说明业务删除已提交。

用户注册 `registrar.register_event_handler("session.end", self.on_end, {}, "user")`；空 matcher 匹配全部目标主会话，也可按 `reason`/`mode` 等字段等值筛选。清理后必须 `return await next_handler(event)`；next 继续当前链，不开启第二次删除。错误、伪造结果和取消见 S_13。

### 3.1 可直接安装的用户包

本例在正常回合后建立自己的会话缓存，结束时按 session_id 移除；只在用户侧提供以下两个文件，经既有管理入口安装。接收通知时所有输入字段都是平台事实，不填回额外目标或结束原因。

`manifest.yaml`：

```yaml
schemaVersion: 1
package:
  id: example.session-end
  version: 1.0.0
  displayName: Session End Example
extensions:
  - id: end-cleanup
    runtime: backend
    module: extension.py
```

`extension.py`：

```python
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Keep user-owned per-Session cache and release it on terminal deletion."""

import logging

logger = logging.getLogger("jiuwenswarm.server.extensions.example.session_end")


class SessionEndExample:
    """A user's Session resources, independent of platform history ownership."""

    def __init__(self):
        self.cache = {}

    def register(self, registrar):
        registrar.register_event_handler("context.usage.updated", self.on_usage, {}, "user")
        registrar.register_event_handler("session.end", self.on_end, {}, "user")

    async def on_usage(self, event, ops, next_handler):
        self.cache[ops.session_context.session_id] = {"project_root": await ops.session.root()}
        return await next_handler(event)

    async def on_end(self, event, ops, next_handler):
        session_id = ops.session_context.session_id
        released = self.cache.pop(session_id, None) is not None
        logger.info("session end example: session=%s end=%s reason=%s released=%s",
                    session_id, event.event_input.end_id, event.event_input.reason, released)
        return await next_handler(event)


def create_extension():
    return SessionEndExample()
```

通知总等待预算为 2 秒；超时取消通知并继续原资源释放，取消请求不能撤销已经发生的外部清理。原删除请求取消则传播取消，由 Runtime 保留原有补偿语义。开始一次通知后，该未完成删除的重试不自动重放；用户清理应幂等，失败后按自己的外部资源规则补偿。

## 4. 方法契约

本特性没有新增用户 Ops。既有只读 cwd/root/model/repo 可在平台绑定的上下文下使用；会话 ID 已知不等于工作目录和运行模型已知。cwd 无可信值时为 None，root 只接受保存的绝对项目目录，model 仍遵循其保存值读取契约。

用户不调用通知函数或手工构造 SessionContext。正式示例包见 [session_end](../../examples/session_end/README.md)，只注册 USER Handler；原会话删除入口负责触发。

## 5. 接入与兼容方案

```text
Worker 装配 → bootstrap 注册 session.end 类型/Validator/默认 CORE
  → 原 ops.build 准备共享能力 → 用户包 register 发布 on_end

Web/Gateway/Front 已有 session.delete 请求
  → Supervisor 路由 Worker → 原 SessionArchiveService / AgentRuntime.delete_session
  → 校验目标、删除锁与既有写入/执行屏障 → quiesce
  → 宿主删除参与者 release_resources
  → 构造平台输入与只读 SessionContext，记录本次通知身份
  → Worker bus.trigger(Event("session.end", input), context)
  → USER on_end 释放自己的会话资源 → next_handler(原事件)
  → 剩余 Handler → 默认 session.end CORE 返回真实通知回执
  → 参与者返回 → 原 KVC/Runtime/Runner 释放与文件删除继续
  → 原删除响应返回；提交后移除通知去重记录
```

类型和注册归 event_types，CORE 归 event_handlers；实际 trigger 及通知任务归 server/runtime/session 的平台参与者。RuntimeParticipantRegistry 增加可选宿主固定删除参与者，与 KVC 的动态发布组合；AgentServer 在构建 Runtime 时注入，Runtime 不直接依赖 EventBus。

选择通知 CORE，不在该 CORE 里第二次释放原资源，避免双重释放和破坏现有删除事务。无需 Core handler_ids 新接口；一次通知沿现有 matcher 执行当前用户链，并为活动包记录在途使用直至任务结束。无装配、无用户包或卸载后保留默认删除，不更改 Front/Supervisor 协议。

## 6. 验收与未决问题

2026-10-31 合并交付验证：本特性与会话查询文档、session.start、session.end、session.attach/session.detach 一起整理到 [Server !99](https://gitcode.com/huenrui_/jiuwenswarm/merge_requests/99)，保留四个提交和各自特性文档。合并 PR 中的 session.start 依赖 [agent-core !110](https://gitcode.com/huenrui_/agent-core/merge_requests/110) 的公开 handler_ids；必须先合入 Core !110，再合入 Server。Core 文档因与目标分支重号改为 EV-07 / F_07，接口语义不变。

本次基于 Server `c6a07f909` 和合并最新目标分支的配套 Core，Windows / Python 3.11.15 下 Server 扩展、Runtime、历史、模型上下文与 Gateway 回归 **1744 项通过、3 项跳过**；Core Events/Extension/callback **587 项通过、1 项跳过**。Gateway 的 `test_web_disconnect_unregisters_physical_subscriptions` 在未修改目标分支和合并分支上均因 SERVICE_UNAVAILABLE 失败，最终合跑显式排除该 1 项，未将其计为通过。完整命令与 XML 在 `D:\work_space\.codex-temp\consolidate-server-prs-20261031`：server-final-command.json、server-final-regression.xml、core-command.json、core-regression.xml；基线对照见 gateway-baseline.xml 与 gateway-candidate.xml。本次未重跑真实模型或浏览器，以下旧日期的真实验证保持历史归属。

原独立交付为 Server !93；合并后同一个 RuntimeParticipantRegistry 同时保留固定删除观察者和固定 foreground 观察者。KVC 启停、挂起与 lease 释放的用例同时核对两者不会互相覆盖；本事件自身不要求 handler_ids，也不因此新增 end/detach 联动。

2026-10-29，Windows / Python 3.11.15，使用上文固定的 Server 基线与 Core ead700d7d18a247968d64de54918cf5d201a207a，当前工作区变更包含本特性。404 项扩展/Runtime/删除与归档回归通过，无跳过；另单独重验本特性的 19 项用例通过（包含在该回归范围中，不重复累计）。仅出现外部 Authlib 弃用提示，未运行格式化或静态检查工具。

```powershell
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2 tests/unit_tests/runtime/test_runtime_session_provisioner.py tests/unit_tests/runtime/test_runtime_service.py tests/unit_tests/runtime/test_session_lifecycle_participants.py tests/unit_tests/runtime/test_runtime_architecture.py tests/unit_tests/server/test_archive_lifecycle.py tests/unit_tests/server/test_agent_lifecycle_request.py --asyncio-mode=auto -o log_cli=false
python -m pytest -o addopts= -q tests/unit_tests/extensions_v2/test_session_end.py --asyncio-mode=auto -o log_cli=false
```

| 场景 | 操作与预期 | 实际证据 |
| --- | --- | --- |
| 真用户包与默认 CORE | 安装用户模块，目标/包身份/Ops 正确，next 得到真实回执 | test_session_end.py；真实服务的两个安装包均执行 |
| 重复/并发与失败重试 | 同一未完成删除至多尝试一次，提交后释放状态 | 定向用例与真实 concurrent-delete/repeated-delete 记录 |
| 防改写/短路 | 五个输入字段、提前伪造结果、后置改写及 bool/int 替换无效，CORE 仍处理原目标 | 注册包参数化用例，检查真实调用作用域中的 CORE 回执 |
| 超时/取消 | 合作取消、忽略取消的任务、原请求取消均有明确边界 | 2 秒生产预算的短预算定向构造；超时后不允许迟到 CORE，包计数最终归零 |
| 原生删除顺序 | USER/通知 CORE 完成时目录仍在，随后原 Runtime 实际删除 | 实际 AgentRuntime.delete_session 测试；真实 Worker 读回同样事实 |
| 不误判结束 | 普通 cleanup、断连/重连、归档/恢复不通知 | Runtime 定向测试；真实连接与归档操作，通知计数保持不变 |
| 参与者共存 | KVC 启停、suspend 和 lease release 保留宿主参与者 | 实际 KVCacheApplicationOwner 与 Registry 回归 |
| 作用域与卸载 | 两会话互不串用，Team 排除；卸载后原删除继续 | 定向 Team/跨会话用例；真实卸载后创建并删除第三个会话 |

真实服务证据：`D:\work_space\.codex-temp\server-session-end-event-20261029\real-191323`。实际 Front → Supervisor → 子 Worker → Qwen3.8-Flash 完成两个模型回合；用户包在已有用量事件创建自己拥有的资源文件，在 session.end 内删除它们并取得默认 CORE 回执。断连、归档/恢复不产生结束通知；两个目标均只通知一次，平台目录随后实际删除。现成示例日志出现两次 released=True。未 mock Ops、模型或 Adapter，未手工构造 SessionContext。

result.json、user-handler-results.jsonl、各删除/归档响应、日志和 source-before/source-after 收据记录来源及一致性；cleanup.json 确认 Worker 已退出、端口关闭。首次联调脚本把归档请求误写为单个 session_id，收到 BAD_REQUEST 后已按既有 session_ids 协议修正；该次不计为验收通过，当前结论以上述成功运行记录为准。

已知边界：没有跨进程崩溃/重启 exactly-once；用户资源在取消或平台随后失败时可能已清理，平台不恢复外部副作用。没有真实服务下的恶意无限任务终止保证、非 Windows 实机、产品浏览器 UI、远端 CI 或大并发性能证据。不同 Harness 的统一结束语义、进程退出和恢复资料仍不在首版范围。

## 7. 参考资料

- [S_13](../specs/S_13_session-end.md)、[内置事件开发指南](../designs/builtin-event-development-guide.md)。
- [Runtime 生命周期协议](../../../runtime/session_lifecycle.py)、[删除编排](../../../runtime/session_provisioner.py)、[AgentServer 装配](../../../server/agent_ws_server.py)。
- [Claude 固定声明](https://github.com/anthropics/claude-code/blob/bf2323b3021c9d3db7a31e1e7d61068c3f704497/mods/types/claude-code.d.ts)。

- [事件定义](../../event_types/session_end.py)、[默认 CORE](../../event_handlers/session_end.py)、[平台通知参与者](../../../server/runtime/session/end_notifications.py)、[用户示例](../../examples/session_end/README.md)。
