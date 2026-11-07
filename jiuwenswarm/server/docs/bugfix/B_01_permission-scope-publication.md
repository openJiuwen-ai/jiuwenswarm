# 会话和 Skill 调用现场拼配置，个人版 Agent 共用 default 相互覆盖

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-10-29 |
| 严重程度 | 高：权限配置隔离与撤销 |
| 范围 | AgentServer 权限配置、Skill/子 Agent/Flash rail、团队 rail 装配 |
| 测试基线 | W3 发布撤销、文件状态、并发隔离与既有权限回归 |
| 关联 spec | S_03_runtime-agent-management.md |

## 现象与根因

Skill/子 Agent/Flash 在判定时使用一次性 `config=`；子 Agent 未知 key 会补登记
空配置。企业版会话 overlay 未保留 NetGuard 差分。个人版多个 Agent 的配置均发布到
default，团队 rail 也未提供独立 key，可能覆盖别的实例配置。

## 决策

复用现有 PermissionManager、配置合成器、Grant Store 和生命周期入口。
新增的 `published_scopes.py` 只管理宿主所拥有的配置 key，不做规则判定。

| 配置 | 发布/清理入口 |
|---|---|
| Agent 基线 | rail 构造/update_config；uninit 清理 |
| 会话 | adapter 请求准备与 overlay 写入；会话删除清理 |
| ACTIVE Skill | Grant Store 成功提交后通知；激活发布，失效/撤销移除 |
| 团队成员 | 已有 narrow_permissions 合成后，以独立 key 构造 rail |

个人版 Agent 显式生成生命周期内的实例 key，避免覆盖 SDK 的共享 default；企业版
继续使用现有业务 key。会话/Skill key 基于该基线和已有作用域身份生成，对 SDK 仍是
不透明字符串。普通无会话策略的调用使用已发布 Agent 基线；显式未知 key、未发布的
ACTIVE Skill 配置直接失败，禁止补登记空配置。Skill 加载门禁使用会话基线，不用
当前 Skill overlay 自授权。文件状态读取失败不能当作“无 grant”放宽权限。

Flash 保留现有工具投影，仅改为 key 判定，并修复 warnings/external_paths 字段聚合。
owner_scopes 只保留发布时的路由元数据，不能短路 NetGuard 等组件的拒绝。

## 拒绝的方案

不改 SDK Manager，不在工具调用时合并配置或重建引擎，不用共享 default 承载团队或
会话配置；不改 W1/W2 规则、沙箱或 Gateway A2A manager。

## 验证与已知遗留

回归覆盖 Skill 激活/撤销/会话删除、文件存储损坏和审批期间变化、多 Agent 并发、
工具异常/取消后的 key 清理、URL 数组、缓存和 Jina。验证使用当前工作区 core 和
agent-security develop `bcb51893bc80b40513605afa0cd9d641766fa559` 源码快照；部署时
需安装匹配版本。本地测试环境缺少 agent-runtime，相关既有测试使用 conftest 的兼容
stub，不代表完整部署验收。出站传输边界见 common 对应 B_01；模型/MCP 另行适配。
