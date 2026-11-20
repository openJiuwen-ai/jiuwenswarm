---
title: 全局默认装备（SDD-0030）：新会话自动挂载默认 MCP
author: '@StoneChan_'
id: F_20261120_1534_session-default-equipment
type: F
modules:
- jiuwenswarm
files:
- jiuwenswarm/server/runtime/mcp/state_store.py
- jiuwenswarm/common/config.py
- jiuwenswarm/common/schema/message.py
- jiuwenswarm/server/dispatch.py
- jiuwenswarm/server/handlers/session.py
- jiuwenswarm/server/runtime/agent_adapter/interface.py
- jiuwenswarm/channels/web/frontend/src/services/equipmentDefaultsApi.ts
- jiuwenswarm/channels/web/frontend/src/stores/sessionStore.ts
- jiuwenswarm/channels/web/frontend/src/components/ChatPanel/ExtensionPickerPanel.tsx
- jiuwenswarm/channels/web/frontend/src/components/ConnectorMarket/McpDetailPage.tsx
- jiuwenswarm/channels/web/frontend/src/i18n/locales/zh.json
- jiuwenswarm/channels/web/frontend/src/i18n/locales/en.json
- tests/unit_tests/agentserver/mcp/test_mcp_state_store.py
- tests/unit_tests/server/extensions/test_session_equipment_defaults.py
- docs/zh/MCP配置.md
- docs/en/MCPConfiguration.md
designs:
- SDD-0030
author_email: chensitong2@huawei.com
---

# 全局默认装备（SDD-0030）：新会话自动挂载默认 MCP

对应 Issue：GitCode #6383（GitHub #11108）。设计全文与场景矩阵见
`designs/proposals/SDD-0030-session-default-equipment/design.md`。

## 元信息

| 项 | 值 |
|---|---|
| 范围 | 后端阶段一（state `is_default` + config 合并层 + restore 第三级回填 + defaults.get/set 双通道路由 + WS 聊天线接线）+ 前端阶段二（详情页开关 + 新会话面板预勾选 + i18n）+ 中英文档 |
| 可靠性 | 新增 UT 46 个（state_store 5 + defaults 24+17 参数化）；回归：extension mount/dispatch 108、message/warmup/cron 202 全绿；WS 直连冒烟（get/PUT/非法项拒绝）通过 |

## 实施要点

| 决策 | 实施落地 |
|---|---|
| 两级默认来源 | `config.yaml` `session.equipment.defaults.mcp`（部署预置，无活性过滤）∪ `state.json` `is_default` 标记（UI 写入，读取时过滤 connected/connecting），合并去重排序；`inherit: false` 短路为空集（特性总闸）；provider 读取异常降级为仅 yaml，绝不阻塞 chat.send |
| 第三级回填 | `restore_chat_send_equipment_params()`：`not saved and "mcp" not in params` 时播种一次。`{}`（无快照）与 `{"mcp": []}`（显式清空）靠 dict 真值区分，逃生门契约原样保留；首轮回合成功后 `save_session_equipment` 落快照，默认清单变更不追溯 |
| 存储原子性 | `replace_default_mcps()` 单锁内全量替换 `is_default` 集（PUT 语义基础）；`set_mcp_default()` 单项翻转不动连接状态；`list_default_mcps()` 只回活记录（断连默认项回填时静默跳过）；`list_default_flagged_mcps()` 供 UI 显示"默认但断连" |
| RPC 双通道 | 共享核心 `read/apply_session_equipment_defaults_payload`（handlers/session.py）：dispatch.py HANDLERS（HTTP REST 路径）与 `JiuWenSwarm.process_message` 的 `_handle_session_equipment_defaults_request`（WS 聊天线，`_SESSION_ASSET_ROUTES` 同款模式，adapter 创建前应答）共用，两种传输不可能漂移 |
| set 校验链 | 类型 → strip/去重 → 上限 10（`_EQUIPMENT_DEFAULTS_MAX`，防 tool schema 挤占新会话上下文）→ 每名必须有活记录（断连项在回填时本就静默失效，写入时直接 BAD_REQUEST 让用户当场修正）；KeyError → MCP_NOT_FOUND |
| UI（Cursor 模式） | MCP 详情页"新会话默认启用"Switch（读 `defaults.get.runtime`，写 PUT 全量清单；断连/达上限/读取失败置灰，set 错误红字透出）；无快照新会话打开扩展面板即预勾选默认集（`prefillDefaultMcps` 整体初始化 + hydrate，所见即所发）；已 hydrate 会话以快照为准不被覆盖；team 模式跳过；空默认集不预填（保持省略字段 → 后端回填活读取） |

## 已知边界

- **跨仓依赖（发布前置）**：web WS 转发链上 agent-gateway 有三处硬编码门（`_FORWARD_REQ_METHODS` / `_FORWARD_NO_LOCAL_HANDLER_METHODS` / `protocol_methods.py` 枚举清单），需 agent-gateway 正式加入两个方法并升级 pin，前端 RPC 才能对外生效；后端回填（SDK/cron/无面板场景）不依赖网关，先行生效。
- WS 路由必须显式接线：合法 ReqMethod 不接 `process_message` 路由会落入聊天路径按空 query 触发 LLM 调用（阶段一初版踩过，冒烟发现后修复，见 SDD §5 第 6 行）。
- 默认范围第一版仅 `mcp`；`plugin_names` / 团队级默认 / TUI `/mcp default` 列入 SDD §8 开放问题。
- 失败回合不落快照（既有行为）：回合失败的会话刷新后重新预勾选默认，回合中手动取消的选择会丢——边缘场景，接受，不做"面板操作即落盘"。
