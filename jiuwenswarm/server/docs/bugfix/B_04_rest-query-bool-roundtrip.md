# 企业版 REST 南向 bool 字符串化，skills.list 显式 false 被当 true

## 元信息

| 项 | 值 |
|---|---|
| 日期 | 2026-11-12 |
| 严重程度 | 中（#5441 接口测试报障；影响企业版所有 GET 类 REST 南向的 bool query 参数语义，`with_installed=false/0/False` 均被当 true，`skills.list` 意外附带 `plugins` 字段） |
| 范围 | `jiuwenswarm/gateway/routing/agent_rest_map.py`、`jiuwenswarm/server/agent_http_routes.py`、`tests/unit_tests/gateway/test_http_agent_client.py`、`tests/unit_tests/server/test_agent_http_routes_params.py` |
| 测试基线 | `tests/unit_tests/gateway/` + 新增 server 侧参数测试：新增 6 例先红后绿；gateway 域回归通过（收集期 ERROR 均为本地 `.venv` 的 `openjiuwen` 包与最新 dev-stable 不同步的基线环境问题，与本次改动无关） |
| 关联 spec | 无（未触及 `S_NN` 契约面：REST 路由表 / `HANDLERS` / RPC 方法签名均未变，仅 query 值类型还原语义与 Gateway 北向 `_coerce_query_value` 对齐） |

## 现象

#5441（dev-stable 企业级）：接口测试 `GET /api/v1/skills?with_installed=false`
返回意外附带 `"plugins": []`；显式传 `false` / `0` / `False` 均按 true 处理，
仅省略参数行为正确。个人版（WS 链路，JSON 直传 bool）无此问题。

## 根因

REST query 是字符串空间，bool 语义需要两端约定往返。失效链条：

1. Gateway 北向 `_coerce_query_value` 正确把 `"false"` 还原为 `False`；
2. 企业版南向组装 `agent_rest_map._query_values` 用 `str(value)` 序列化，
   `False` → `"False"`；
3. AgentServer `agent_http_routes.collect_params` 用 `dict(request.query_params)`
   原样透传字符串，不做类型还原；
4. handler 侧 `bool(params.get("with_installed", False))` 对非空字符串
   `"False"` 恒为真 → 显式 false 被当 true，`skills.list` 意外附带 `plugins`。

数字参数（`limit=0`）同样被字符串化，仅因现有消费方统一走 `int()` / 
`_coerce_int()` 容忍了字符串形态而未暴露。

## 决策

两端镜像修复，语义与 Gateway 北向 `web_http_app._coerce_query_value` 同表：

- 南向 `_query_values`：bool 用小写字面量 `"true"` / `"false"` 序列化
  （`str(False)` 的 `"False"` 大写形态不再出网）；
- AgentServer `collect_params`：query 值经新增 `_coerce_query_value(key, raw)`
  按参数键的类型约定还原，不再按值形状无差别推断：
  - 布尔字面量 `true/yes/on` → `True`，`false/no/off` → `False`（对所有键
    生效——文本/数值参数不会使用这些字面量，无歧义）；
  - `1`/`0` 仅在 `_BOOLEAN_QUERY_KEYS` 登记的布尔参数上归布尔。`is True` /
    `is False` 形态的消费方（`interface.py` 的 `log_as_user`/`attach_goal`、
    `team_helpers` 的 `enable_routing`、`extension_package_manager` 的
    `include_team_compatibility` 等）只认真正的 bool，int 1/0 会被判错，
    与 `=true`/`=false` 语义分叉；同时不能全局归布尔——`limit=1` 会变
    `True`（`handlers/session._coerce_int` 视 bool 为非法并回落默认值），
    `q=1` 这类文本参数也会被吞成 `True`；
  - `int` 还原只作用于 `_NUMERIC_QUERY_KEYS`（`limit`/`offset`/`page`/
    `page_size`/`max_bytes`）。
  - 其余键一律保持字符串：标识类参数（`session_id`/`name`/`cursor`/`*_id`）
    可能有前导零，且必须与 path/header 通道（不做类型还原）保持同一类型。
  - body 是 JSON 已带类型、path 占位符约定为字符串，均不做转换。
- `wants_stream` 同时接受 bool 与 int/float：`enable_streaming` 已登记为
  布尔键，`?enable_streaming=1` 还原为 `True`；WebSocket 链路与历史调用方
  仍可能直接传 int 1/0，只比对字符串字面量会判 False，SSE 静默退化为单帧
  JSON（调用方收不到任何 chunk 且不报错）。分支顺序与 `_as_bool` 一致：
  bool → int/float → str → 默认 False。

影响面审查：AgentServer `/api/v1` GET handler 的数字消费方统一走
`int()` / `_coerce_int()`（接受 int 与 str），数字还原收敛到显式数值键后
不影响标识类参数；布尔消费方**并非**全部是 `bool(...)`——存在上述
`is True` / `is False` 严格判定点，这是 `1`/`0` 只对登记键归布尔的原因。
Gateway 北向 `/api/v1` 已用同语义 coerce 跑全线，本次是把企业版 REST 链路
向其收敛而非引入新语义。

## 拒绝的方案

1. **只修 handler 侧防御解析**（`skill_manager` 把 `bool(...)` 改为
   字符串布尔解析）：治标——南向字符串化影响所有 GET 类 bool 参数
   （`refresh_marketplaces`、cron `enabled` 等），逐 handler 防御会持续
   漏，且 WS 与 REST 两条链路对同一 handler 的参数形态割裂。
2. **南向 bool 改走 JSON body**：需要把 `skills.list` 从 GET 改 POST，
   破坏既有 REST 契约与缓存语义，变更面远超缺陷本身。

## 验证

先红后绿（未修复代码上相关用例全部失败，准确复现 `str(False) → "False"`
→ `bool("False")` 判真的故障链；修复后全部通过）：

- `tests/unit_tests/gateway/test_http_agent_client.py::
  test_assemble_skills_list_serializes_bool_query_as_lower_literals`：
  南向 `skills.list` 组装，bool 以小写字面量出网、数字保持原样；
- `tests/unit_tests/server/test_agent_http_routes_params.py`：
  1. bool query 还原（含大写 `"False"`）；
  2. `on`/`off`（含大写 `"OFF"`）按 `_as_bool` 同集合还原，不残留字符串；
  3. 显式数值键还原为 int（`limit`/`offset`/`page`）；
  4. 标识类参数保持字符串（`session_id="0012"`、`name="1"`、`cursor="-3"`、
     `agent_id="007"`），不丢前导零；
  5. 未登记的文本参数 `q=1`/`search_keyword=0` 保持字符串，不被吞成 bool；
  6. 登记的布尔参数 `=1`/`=0` 还原为 bool，与 `=true`/`=false` 同义；
  7. 文本 query strip 后原样；
  8. path > query 优先级保持，query 值仍做类型还原；
  9. Gateway 南向组装 + AgentServer 解析全链路往返：`with_installed=False`
     穿过南向后必须仍是 `False`（原故障的端到端复现）；
  10. `?enable_streaming=1` 经 `collect_params` 还原为 `True` 后
      `wants_stream` 仍判 True（`=0` 判 False），SSE 不静默退化；
  11. `wants_stream` 直接接收 int/float（`1`/`1.0` → True，`0` → False），
      字符串分支 `on`/`off` 语义不变。

回归：`test_agent_http_routes_params.py` 与 `test_http_agent_client.py` 共
65 passed。

## 已知遗留

- 新增布尔型 GET 参数时需在 `_BOOLEAN_QUERY_KEYS` 登记，否则 `=1`/`=0` 会
  保持字符串（`=true`/`=false` 不受影响）。未登记属保守退化，不会误判为真。
- 多值 query（`?a=1&a=2`）维持既有行为：取每键最后一个值，与修复前
  `dict()` 构造一致，未引入多值语义。
- 非 GET 的 REST 入口参数走 JSON body，本就不受字符串化影响；
  若未来新增 GET 路由，其 bool 参数自动获得与本次一致的往返语义，
  无需逐路由配置。
