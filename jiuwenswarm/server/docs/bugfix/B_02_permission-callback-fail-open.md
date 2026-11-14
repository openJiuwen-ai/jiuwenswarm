# 权限 KeyError 被回调框架吞掉后工具继续执行

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-29 |
| 严重程度 | 高：权限判定失败时可能无裁决执行 |
| 范围 | core/AgentServer 的普通权限、Skill 门禁与 Subagent 权限回调 |
| 关联 spec | S_03_runtime-agent-management.md |
| 测试基线 | 真实 AsyncCallbackFramework、AgentCallbackManager、AbilityManager 与本地 TLS/mTLS |

## 现象与根因

Manager/宿主路由遇到未知或未发布 key 时抛 KeyError。直接调用可以看到异常；
AsyncCallbackFramework.trigger 对普通异常记录日志后继续，因此工具执行体仍可能运行。
只测 resolve_interrupt 或手动调用 before_tool_call 不足以证明运行时拒绝。

## 决策

权限 before_tool_call 使用统一边界将普通异常转换为 AbortError，覆盖完整回调，包含
ALLOW 后绑定出站 key 失败的路径。保留现有 AbortError、HITL 和取消语义；普通异常转换
不设置 cause 字段，防止框架抛回普通异常。配置/存储/路由失败的工具执行次数必须为 0。
场景 hook 失败也中止；普通 core rail 提供默认元数据/基线 selector，支持宿主回退工厂。
子 Agent 的开关读取失败不能当作 disabled；文件执行窗口读取使用会抛出错误的专用入口，
避免将状态文件不可用当成“没有 ACTIVE Skill”而跳过权限检查。

## 拒绝的方案

不改变通用回调框架对非安全回调的容错策略；不把未知 key 补为空配置，不回退 default。
不把企业版 persist 的失败回退为覆盖租户基线，这会破坏会话配置隔离。

## 审查意见核实

| 意见 | 核实结果 |
|---|---|
| 普通异常导致 fail-open | 已复现并修复，真实工具执行计数为 0 |
| requests 丢失自定义 CA/客户端证书 | 当前 requests 的 send 仍调用 cert_verify；本地自签 CA、mTLS、缺客户端证书拒绝均验证，不能据 get_connection_with_tls_context 忽略参数断言丢失 |
| aiohttp 继承调用方证书配置 | 当前独立默认上下文不继承，已明确文档边界 |
| 普通 rail 缺 metadata/baseline selector | 已在 core 基类补默认接口，并验证普通工厂的 owner deny 生效 |
| 企业同业务 key 多实例 publisher 不唯一 | 确认存在，仍需显式所有权方案；本轮异常改为终止后不会因该错误继续执行 |
| 会话 key 累积至 Manager 配额 | 确认需要结束生命周期与活跃引用预算；本轮未引入可淘汰在途 key 的 LRU |
| 企业版 persist 缺 session/publisher | 确认刷新覆盖有缺口；不能采用覆盖租户基线的回退方案 |

## 验证与边界

新增测试经过真实回调链与工具调度器，覆盖普通/Skill/Flash 非投影工具、未发布 ACTIVE
Skill、Subagent 缺 publisher/key/存储、ALLOW 后元数据异常、场景 hook 异常和普通工厂。
自定义 CA 与客户端证书通过真实本地 TLS 服务验证，未访问外部服务。
使用匹配 SDK 源码与已安装 pytest 的 Python 环境；agent-runtime 缺失仍由原 conftest
兼容 stub 处理。未做部署 E2E。沙箱和 Gateway A2A manager 保持排除。
