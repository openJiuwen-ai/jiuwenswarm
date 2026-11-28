---
id: S_05_ag-ui-host-execution
title: AG-UI 宿主指令与取消契约
type: S
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
---

# AG-UI 宿主指令与取消契约

- AG-UI 的默认执行 profile 接受可选的 `role_instructions`。新建会话去掉首尾空白后最多接受 12,000 字符，超限返回 `BAD_REQUEST`。重试同一 `create_token` 时，角色指令按去掉首尾空白后的文本比较，兼容已有会话中保存的空白。
- 定向取消清理原生工具等待点后，须提交检查点并写入 `application_execution.state=cancelled` 才能确认持久化成功。若提交或写入失败，完成权限切换收尾并返回 `success: false`，Gateway 可以据此保留未确认状态。
- 断开执行流触发宿主清理。停止等待排除当前任务和已结束的任务，等待仍在取消的任务。等待超时须记录未确认停止，保留原始取消或失败结果。

接入方式见 [AG-UI 接入指南](../../../../../../../docs/zh/AG-UI.md)。
