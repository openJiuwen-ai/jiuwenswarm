---
description: enterprise_config 企业配置模块说明书——给修改/检视本模块代码的 AI 助手与工程师：模板槽位模型、四级加载链、三级进程缓存、MCP fail-closed 与模型首项取值等本地铁律、与 gateway 读库 / config_provider 的接缝、测试指引。
---

# enterprise_config

企业级配置生效模块：从 **Gateway 本地库**按实例 Agent 资源解析生效配置
（`instance_agent_resource` → `agent_template` → `template_ref` 槽位模板实体），
并把结果**覆盖写入** config 快照的模型 / embedding / MCP 段。企业版
（`is_enterprise()`）专用——个人版一切入口直通（loader 返回 `None`、进程缓存
不启用）。5 个实现 + `__init__` 共 1,030 行。

## 数据流

```
AgentRequest（bot_id 即 resource_id）
  → load_effective_enterprise_config（loader.py，slots 过滤）
      instance_agent_resource 行（enabled=True）
        → ref_template_id → agent_template 行
          → template_ref {slot: [template_id, ...]}（normalize + slots 过滤）
            → 按槽位批量加载模板实体（企业版走 TemplateEntityCache，
              个人版直查 db_queries）
  → EffectiveEnterpriseConfig（不可变的路由 + 实体快照）
  → apply_models / apply_mcp（common/config_provider.py 编排）
      深拷贝 config_base → 覆盖 models.* / embed / mcp.servers
```

## 模块地图

| 文件 | 行数 | 职责 |
|---|---|---|
| [schemas.py](schemas.py) | 153 | `TemplateRefSlot` 十一槽位枚举 + `SLOT_ENTITY_TABLE`（槽位→DB 表）；`normalize_template_ref`（空值键省略、同槽去重保序）；`RoutingContext` 路由三元组（不含 gateway_id）；`EffectiveEnterpriseConfig` 快照 |
| [db_queries.py](db_queries.py) | 89 | 读库 facade，委托 `gateway/storage/backends/db/reader`；`fetch_templates_by_slot` 返回顺序与入参一致、未命中 id 不出现 |
| [loader.py](loader.py) | 516 | `load_effective_enterprise_config` 四级加载链；三级进程缓存与失效；A2A 槽位的宽松处理 |
| [apply_models.py](apply_models.py) | 141 | 模型 / embedding 模板行 → `config.yaml` 兼容结构（`models.default` 等五键 + `react.*` + `embed` 段） |
| [apply_mcp.py](apply_mcp.py) | 116 | MCP 模板行 → `mcp.servers[]`；transport 别名归一（`http`/`streamable_http` → `streamable-http`） |

`__init__.py` 只重导出 loader 的四个公开名（`DEFAULT_AGENT_LOAD_SLOTS` /
`load_effective_enterprise_config` / 两个 invalidate）。

## 本地铁律（改代码前必读）

1. **个人版直通，缓存只给企业版**：`load_effective_enterprise_config` 开头
   `is_enterprise()` 不通过返回 `None`（loader.py:391）；`_fetch_slot_entities`
   等三级缓存的取用全部在 `is_enterprise()` 分支内（:309-313/:343-345/:360-362），
   个人版每次直查，保持原路径。
2. **加载链四级降级——缺失即 `None` + warning，不上抛**：resource 行缺失 /
   `ref_template_id` 空 / agent_template 行缺失 / 过滤后槽位为空 / 任何请求槽位
   都没加载到实体，五处全部 warning + `return None`（loader.py:408-498）。调用方
   （config_provider）以 `loaded is None` 走本地 `config.yaml`。**唯一例外**：
   A2A 槽位实体加载异常仅 warning 跳过、不阻断其它槽位（:473-485）；其余槽位
   加载异常**向上抛**。
3. **A2A 策略只接受单个字面 policy_id**（`_literal_slot_template_id_map`
   :365-379）：引用数 ≠ 1、`${...}` 模板表达式、含 ` or ` 多值 → 整槽丢弃
   （不参与加载）。A2A 的 id 字段是 `policy_id` 而非 `template_id`
   （schemas/db_queries 两处一致）。
4. **MCP fail-closed 整表替换**（apply_mcp.py 模块 docstring）：企业版走企业
   配置合并路径后，`mcp.servers` 完全由管理端下发决定——有模板 → 只保留这些；
   无槽位 / 空列表 / `loaded is None`（config_provider 侧 `clear_local_mcp_servers`）
   → `servers=[]`，**禁止本地 config.yaml 的 MCP 继续生效**。写入条目固定
   `enabled=True`（开关由模板行 `enabled` 决定，跳过是正常路径不打告警）。
5. **模型槽位只取列表首项**（apply_models.py:61-65）：`template_ref` 各槽位可
   引用多个 `template_id`，但写入 config 时每个模型槽位仅取**第一个有效模板**
   （`model_name` 非空）；`default` 槽位同时写 `models.default` +
   `models.defaults` + `react.model_name/model_client_config/model_config_obj`
   三处（:114-126）。embedding 段要求三字段全非空才写入（:135）。
6. **缓存回填的 generation 守卫**（loader.py:160-164/:274-282）：`invalidate()`
   递增 generation 并清空条目；并发中已进入 fetch 的协程回填时若 generation
   已变，**不得写回**——否则 reload 仍读到旧行。改缓存结构时必须保留此协议。
7. **TTL 与单飞**：`TemplateEntityCache` 按 `(table, template_id)`、TTL 3600s；
   resource / agent_template 行缓存按 key、TTL 600s；两者都是按 key（或表）
   的 `asyncio.Lock` 单飞 + 惰性清理（每 32 次操作清一次过期项）。
8. **每网关独立 DB，查询不加实例行级隔离**（db_queries.py 模块 docstring）；
   表名经 `is_safe_ident` 校验，非法表名 warning + 空列表（:79-82）。
9. **`slots` 空 = 编程错误**：`load_effective_enterprise_config` 对空 slots 抛
   `ValueError`（:395-396）——调用方必须显式声明要加载哪些槽位；默认全集
   `DEFAULT_AGENT_LOAD_SLOTS` = 全部十一槽位（schemas.py:48-56）。

## 与其它模块的接缝

- **`gateway/storage/backends/db/reader`**：唯一底层读库通道，本模块不直连
  DB。
- **`common/config_provider.py`**：主装配点——`DefaultConfigProvider.
  load_agent_config` 调 `load_effective_enterprise_config` 后经
  `apply_enterprise_models_and_mcp`（models → embedding cache → mcp）+
  memory merge 得最终 config；`refresh()` 调 `invalidate_enterprise_config_caches`
  后重载 memory 配置。
- **`gateway/channel_manager/web/app_web_handlers.py`**：web 侧直接消费
  loader / schemas。
- **`db_queries` 的散点消费方**（均为函数内延迟导入）：`gateway/config/audit/
  access.py`、`agents/harness/common/memory/config.py`、`agents/harness/
  common/tools/cron/cron_tools.py`、`infrastructure/log_masking/engine.py`、
  `gateway/config_poll/db.py`、`server/runtime/workspace/policy_reload.py`、
  `common/utils.py`——改 `db_queries` 签名前先 grep 这些调用点。
- **`jiuwenswarm/edition.is_enterprise`**：版本判定的单一来源，本模块不自行
  读环境变量。

## 测试与提交

- 单测：`tests/unit_tests/agentserver/test_enterprise_policy.py`（加载链 /
  缓存失效 / A2A 宽松处理）、`test_apply_enterprise_mcp.py`（fail-closed /
  transport 归一）、`test_enterprise_config_provider.py`（config_provider
  装配）。
- 提交三连续约定（feat / test / docs）见域级
  [../docs/AGENTS.md](../docs/AGENTS.md)。
- 读到不一致当场修文档：本文件里与代码不符的接口名、枚举值、路径、点名数量的结构表述
  （如"四级加载链"、"三级进程缓存"、"全部十一槽位"），先 grep 代码、以代码为准刷新
  文档，同一次改动落地；不把过时表述当新约束执行。
