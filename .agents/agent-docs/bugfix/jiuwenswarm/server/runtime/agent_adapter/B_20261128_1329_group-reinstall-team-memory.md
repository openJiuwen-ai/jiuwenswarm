---
id: B_20261128_1329_group-reinstall-team-memory
title: 专家团重装后恢复团队记忆访问
type: B
author: zhengpeng
author_email: zhengpeng47@huawei.com
modules: [jiuwenswarm/agents/harness/common/memory, jiuwenswarm/server/runtime]
files: [jiuwenswarm/server/runtime/extension_package_manager.py, jiuwenswarm/server/runtime/managed_memory_http.py, jiuwenswarm/agents/harness/common/memory/team_memory_members.py, tests/unit_tests/server/extensions/test_extension_package_manager.py]
designs: []
design_note: 沿用稳定团队身份和显式成员管理契约。
---

# 专家团重装后恢复团队记忆访问

#7244 的团队记忆仍在，卸载已将团队成员标记为离队。重新安装后仅执行普通会话初始化，因而保留了离队状态。

Swarm 在包安装成功时恢复已验证名单内的团队资格，沿用已有成员管理队列处理离线重装。普通会话重建保留显式移出结果。见 [Team 成员管理契约](../../../../../specs/jiuwenswarm/server/runtime/agent_adapter/S_04_coding-document-memory.md)。

回归用例 `test_reinstalled_group_recalls_previous_team_memory` 在基线复现空名册。用例从真实内置 HTTP 工具写入评审要求，通过卸载及重装模拟新会话，并验证找回原条目 ID、旧临时成员保持离队。
安装后的记忆异常单独记录。配置读取或队列写入失败时，修复故障后可卸载再安装；事件入队后调用 Memory API 失败时沿用队列重放。故障注入覆盖内置专家团安装标记和监听器重启后的历史记忆检索。
