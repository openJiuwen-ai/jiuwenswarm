---
id: B_20261112_1716_affinity-status-read
title: 控制面遗漏亲和状态导致开关与说明不一致
type: B
author: MengHan18
author_email: hanmeng22@huawei.com
modules:
  - jiuwenswarm/server/control
  - jiuwenswarm/common
  - jiuwenswarm/channels/web/frontend/src/features/settings
files:
  - jiuwenswarm/server/control/config_service.py
  - jiuwenswarm/common/kv_cache_affinity_config.py
  - jiuwenswarm/channels/web/frontend/src/features/settings/services/useSettingsConfig.ts
designs: []
design_note: 修复 F_20261110_2026_affinity-settings 的读入口遗漏及用户反馈的开启交互。
created: '2026-11-12'
---

# 控制面遗漏亲和状态导致开关与说明不一致

现象：开关蓝色，同时显示“控制模型尚未全部声明支持亲和”和“无法读取检查结果”。

根因：`CONFIG_GET` 在现代 Front 经 `server/control/config_service.get_panel()` 读取，而不是旧 Runtime ConfigAdapter。前一版只在 Runtime 读取路径返回能力状态；控制面仍按原始开关值返回 true，并缺少结构化状态。此前注册 Runtime RPC 测试没有覆盖这条真实读分支。

修复：控制面复用纯配置函数 `get_affinity_status()`。core 类型导入推迟到 Runtime 构建策略时，防止修复突破 Front 的轻量导入边界。前端缺失有效状态时不展示开启，移除常驻检查区。初版曾在点击时读取状态并弹窗；最终交互已在 F_20261110_2026_affinity-settings 中替代为直接提交非乐观保存，后端锁内校验，没有支持项时保持关闭并在顶部 Toast 提示，不额外发送预检查请求。

初版证据：`test_front_control_reader_exposes_effective_affinity_status` 从实际 `ReqMethod.CONFIG_GET` 入口覆盖支持与不支持配置；Front 导入隔离等相关后端测试 83 项通过，前端交互 8 项及设置回归 67 项通过，生产构建通过。组件截图在工作区 `build_artifacts/affinity-preview/affinity-simple.png` 与 `affinity-blocked.png`，使用模拟数据而非在线实例。

防错规则：涉及配置读写契约时，分别核对 Front 控制面读取和 Runtime 写入路由；用真实方法分派入口验证字段及错误语义，不能用旧适配器入口测试替代控制面读回验证。当前契约见 S_01_affinity-settings，实施归档见 F_20261110_2026_affinity-settings。
