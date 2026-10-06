# 评测模型切换：DS v4.1

2026-09-18 补充：整题发现该接口未可靠遵循关闭思考参数；本次重跑使用执行输出上限 8192，结果见[13-1 整题重跑](duplex-13-1-dsv41-rerun-20260918.md)。下文 2048 和 reasoning_tokens=0 仅描述最初连通性测试。

2026-09-17 已更新服务器 `wzr@47.79.124.13:31442` 的默认评测配置。

| 用途 | 模型 |
|---|---|
| 执行、纠错确认 | `deepseek-v4.1-flash` |
| 看护、路由 | `Qwen/Qwen3.5-9B` |

接口 `https://code.viwo50when4.xyz/v1` 的模型列表返回 `deepseek-v4-pro` 和 `deepseek-v4.1-flash`；按用户要求选择 4.1。temperature=0，执行输出上限 2048，配置发送 `enable_thinking=false`。

通过现有 SDK 实测：流式文本、自动工具调用、指定工具调用全部通过，分别耗时 7.2、4.5、3.9 秒；本次返回 reasoning_tokens=0。这是接口连通性测试，未重跑数据集，不能用于判断整题速度或稳定性。

生效配置：`/workspace/wzr/duplex-benchmark-20260908/private/models.json`，权限 0600；原配置已在同一私有目录备份。密钥不进入源码和评测归档。

验证脚本、结果和启用记录归档于 `/workspace/wzr/duplex-benchmark-20260908/office-code-20260915/dsv41-switch-20260917`；本地副本位于 `D:\jiusi_agent\_deps\duplex-review-20260916\dsv41-switch-20260917`。
