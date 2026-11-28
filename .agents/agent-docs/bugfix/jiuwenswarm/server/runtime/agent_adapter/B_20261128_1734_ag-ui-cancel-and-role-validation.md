---
id: B_20261128_1734_ag-ui-cancel-and-role-validation
title: AG-UI 取消确认与会话指令校验
type: B
author: yukuanj
author_email: jiayukuan@huawei.com
modules:
  - jiuwenswarm/runtime
  - jiuwenswarm/server/runtime/agent_adapter
files:
  - jiuwenswarm/runtime/app_role.py
  - jiuwenswarm/runtime/session_provisioner.py
  - jiuwenswarm/server/runtime/agent_adapter/sdk_execution.py
  - jiuwenswarm/server/runtime/agent_adapter/interface_deep.py
designs: []
design_note: 本次为现有 AG-UI 接入的检视修复，按宿主检查点和取消响应契约实施。
---

# AG-UI 取消确认与会话指令校验

## 现象与修复

- 检查点提交异常原先跳过取消响应和权限切换收尾。现在落盘失败返回未确认并释放切换锁，重试可在持久化成功后确认取消。
- 默认 profile 的 AG-UI 角色指令原先未经归一化直接进入会话元数据。新建时清理首尾空白并限制长度，历史会话按等价文本比较创建令牌重试。
- 断流清理超时原先会覆盖正在传播的取消异常。现在清理保留原异常，并记录仍未停止的任务数。

当前规则见 [AG-UI 宿主指令与取消契约](../../../../../specs/jiuwenswarm/server/runtime/agent_adapter/S_05_ag-ui-host-execution.md)。

## 验证

在 macOS、Python 3.13 和锁定的 Gateway `8fc3c877469dcc46bfaa57579340b6bac5595449` 下，JiuwenSwarm 的运行时与会话创建定向测试通过 89 项，Gateway AG-UI 测试通过 80 项，猜拳示例 Node 测试通过 7 项。故障注入覆盖取消检查点失败及重试、断流时任务停止未确认。会话创建测试覆盖超长文本与历史空白兼容。实际浏览器与模型对局未执行。
