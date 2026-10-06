# 2026-10-06：develop 与 PR #4 整合

分支：`yzd/duplex-develop-integration-20261006`。

| 来源 | 提交 |
| --- | --- |
| 上游最新 develop（整合时已远程确认） | `f0a69728c96b5961d993449f1a901cbd2f4dac5b` |
| 此前双工分支 | `370cc096d73e0424d1275cc1a258edeff56a1481` |
| [PR #4](https://github.com/zuiho-kai/jiuwenswarm/pull/4) | `b6cdd883f99ced9ea4d1208540752a60aa2dd59f` |
| develop 锁定的 SDK，亦为本轮测试版本 | `9e3390195a9ea15235b2b5f7412cb2aa440622cc` |

先将此前分支的净改动移植到 develop，提交为 `a252b3313`。再选择性移植 PR 的监工接口、配置、日志和回放计时，并适配最新 develop 的配置处理模块。没有直接合并 PR 的整个分支。

PR 作者为 GitHub 用户 `yzdnh123-a11y`，原提交身份为 `yzdnh123 <yzdnh123@gmail.com>`。整合提交使用这个身份的 `Co-authored-by`。

## 当前行为

- 支持 SDK、Jev、MindsHub、Cloudflare Clef；设置入口位于“智能体 → A2A 双工监工”。默认关闭，启用模式后需重启团队。
- 路由层和应用层的状态哈希过期校验均已移除。等待期间消息序号或执行状态变化，不会仅因此将 INTERRUPT 降级为 APPEND。哈希保留作诊断记录。
- 保留运行状态检查、消息去重、用户暂停 / 停止控制。内部打断保留已接纳消息与完整工具结果，作废旧计划后重新规划。
- 外部接口密钥沿用现有配置加密和保存流程，使用 `DUPLEX_ROUTER_API_KEY`；Clef 账号使用 `CLOUDFLARE_ACCOUNT_ID`。API 地址和阈值只更新当前所选后端。
- 不带入机器专用启动器和后台影子任务。回放中的历史 `stale` 统计仍可读取旧记录；运行时不再产生哈希过期拒绝。

## 验证

- 相关后端、配置、日志、路由、回放计时和真实 SDK 端到端测试：212 passed，2 skipped。
- 两项跳过依赖外部 InterruptBench checkout / 独立 WebArena 环境；没有将这些结果当成数据集成绩。
- 覆盖等待期间接纳新消息、哈希变化后仍能打断、消息保留、工具结果保留、用户生命周期控制和重复消息处理。
- 前端生产构建通过；中英文文案测试 4 项通过。构建保留现有的大 bundle 提示。
- 浏览器对照原设置行检查桌面和 900 px 窄窗口，布局一致。页面未连接完整后端，无法验证实际点击保存或展开被禁用的下拉菜单；保存 / 读取由真实配置处理管线测试覆盖。
- 厂商接口使用模拟传输和本地 HTTP 服务验证，未调用付费线上厂商。功能文件 Ruff、diff 空白检查及 PlantUML 渲染通过。`team_helpers.py` 的全文件 Ruff 仍有两个基线问题（E402、F841），已对照整合前提交确认，与本次日志改动无关。

[架构图](diagrams/duplex-architecture.svg) · [消息处理流程](diagrams/duplex-routing-flow.svg) · [详细调用链](diagrams/duplex-code-flow.svg)
