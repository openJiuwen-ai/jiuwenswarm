---
id: B_20261112_2025_permission-profile-refresh
title: 刷新后智能审批档位错误回显为默认权限
type: B
author: nypdmax
author_email: chengkunck@gmail.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/server/control
files:
  - jiuwenswarm/common/permission_mode.py
  - jiuwenswarm/common/config_panel/config_set_handlers.py
  - jiuwenswarm/server/control/config_service.py
  - tests/unit_tests/agentserver/test_front_permission_profile.py
designs: []
design_note: 补齐既有 config.get/config.set 权限档位回显契约，无独立设计；审批决策与持久化格式不变。
---

# 刷新后智能审批档位错误回显为默认权限

## 现象与契约根因

选择智能审批后刷新，界面回显默认权限，实际执行仍为智能审批；单独刷新即可触发。
保存响应已有 permissions_profile=automatic，但 Front 的 CONFIG_GET 只返回
permissions_enabled=true；App.tsx 缺少 automatic 字段时回退到 default。
这是配置读写响应契约不一致，审批配置本身未丢失。

develop、releases/0.2.9 的个人版查询经过 Front；releases/0.2.8 的普通本地
Gateway 已返回档位，仅经 AgentServer Front 的查询路径受影响。

## 修复决策与取舍

- 将既有 permission_profile、canonical_permission_facade 移到轻量 permission_mode，
  读写两端共用，旧模块继续导出；避免 Front 为读取配置而加载 Runtime/OpenJiuwen。
- 两个响应字段由同一档位生成。单独补齐 profile 会使字符串 "true"、整数 1 等
  非布尔 enabled 产生 full_access/true 的矛盾响应；保留运行时 enabled is True 判定，
  不通过宽松转换改变审批语义。
- 未读取到权限时沿用 full_access/false 回退；权限已读出后其他字段异常，保留已读值。
- 恢复既有 Web 契约，不增加审批策略、配置项或持久化迁移，不新增独立设计或 spec。
