---
id: B_20261103_0936_skill-action-param-type-validation
title: 技能安装/更新/卸载入口参数类型校验缺失修复
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules:
  - jiuwenswarm/server/runtime/skill
files:
  - jiuwenswarm/server/runtime/skill/skill_manager.py
  - tests/unit_tests/agentserver/test_skill_manager_enterprise_state.py
designs: []
design_note: 三个入口的参数契约（string 标识、boolean force）已由前端
  webHttpClient 与 EnterpriseSkillSourcePanel 既有调用形态声明，本次为入口
  类型校验层缺失的补接，无独立设计。
---

# 技能安装/更新/卸载入口参数类型校验缺失修复

## 现象与影响

接口测试（sk_072 / sk_074，0.0.48s）报告两个确定性复现的产品缺陷：

- **#5881**：`POST /skills/sources/actions/install` 携带
  `force:"false"`（字符串）时，请求被接受且执行了强制重装——双重违规：
  字符串既未被类型校验拒绝，`bool("false")` 又求值为 truthy 把语义反转为
  force=true。`version_id:1`（数字）虽被拒，但仅因 fixture 404 巧合命中
  `source_install_failed`，并非类型校验。
- **#5882**：`POST /skills/enterprise/actions/uninstall` 携带
  `origin:{}`（对象）时，请求被接受且执行了真实卸载。`str({} or "")`
  把类型非法的 origin 静默改写为空串，落入 name-only 回退匹配，命中同名
  安装直接删除。企业多源重名场景下，客户端传错类型的 origin 会误删另一
  来源的同名技能，属于语义降级而非单纯容错。

## 关联设计与当前契约

前端契约（`webHttpClient.ts` 路由表 + `EnterpriseSkillSourcePanel` 调用）
始终以正确的 `boolean` force / `string` 标识调用；`installMetadata.ts`
声明 `origin?: string | null`。后端 `handle_skills_source_install` /
`handle_skills_update` / `handle_skills_web_uninstall` 三个入口共用
`str(params.get(x) or "")` 软转换模式，契约层类型校验整体缺失。issue 报告
方建议三个入口一并修复，本次按此范围实施。

## 根因与修复决策

根因：入口用 `str(x or "")` / `bool(x)` 做软转换，把类型错误静默归一化：

- `bool("false")` → `True`（#5881 的强制重装来源）；
- 空容器（`{}`、`[]`）、`None` 在 `or` 短路后全部变空串 → 与"未提供"
  无法区分（#5882 的 name-only 回退误删来源）。

修复决策：三个入口在参数解析第一步做严格类型校验，非法类型返回
`error_code:"invalid_params"`，与缺参的 `missing_params` 区分：

1. `handle_skills_source_install`：`source_id` / `skill_id` /
   `version_id` 非 None 时必须是 str；`force` 缺省 False、提供时必须
   是 bool。
2. `handle_skills_update`：`source_id` / `skill_id` / `target_version_id` /
   `version_id` / `expected_current_version_id` 非 None 时必须是 str；
   校验统一在 `_find_installation_by_skill_ref` 之前完成，避免非法类型
   被先行的 `skill_not_found` 掩盖。
3. `handle_skills_web_uninstall`：`name` / `origin` 非 None 时必须是
   str。origin 为 None/缺省时保留 name-only 回退（legacy 记录卸载路径
   有测试与注释明确保留）；类型非法的 origin 直接拒绝，绝不落入回退。

拒绝的备选：在 gateway HTTP 层统一做 schema 校验——issue 已定位根因在
skill_manager 入口的软转换，且 TUI/SDK 等非 HTTP 调用方也经同一入口，
入口处校验覆盖全部通道；display_name/version/author 等展示性可选元数据
维持软转换（无类型契约声明、无危害路径），不扩大范围。

## 回归证据与适用范围

分支 `fix/issue-5881-5882-skill-action-param-type-types`（候选基线：
develop `08515d6e7` 之后），Windows 本机 `.venv`，`uv run pytest`：

- `tests/unit_tests/agentserver/test_skill_manager_enterprise_state.py`
  33/33（新增 6 例）：uninstall `name:[]` / `origin:{}` 拒绝且库存不变；
  install `force:"false"` 在 `_fetch_verified_source_artifact` 前拒绝
  （monkeypatch 断言下载未触发）；install `source_id:[]` /
  `skill_id:7` / `version_id:1` 入口拒绝；update `target_version_id:1` /
  `expected_current_version_id:2` 入口拒绝。
- 相邻回归：`test_skill_manager_online_search.py` +
  `test_skill_manager_team_skills_hub.py` +
  `test_skills_import_local_token.py` +
  `test_enterprise_skill_http_route.py` 68/68，确认既有 force 布尔
  调用方与 HTTP 路由不受影响。

未验证：真实企业栈的 sds-fixture 端到端（本机无受控 Provider）；由
issue 报告方的 sk_072/sk_074 脚本在合入后复跑确认。重验条件：同一
非法变体请求应得到 `success:false` + `error_code:"invalid_params"`，
安装库存快照不变。

## 防错模式与资料同步

- "入口软转换 `str(x or '')` 会把类型非法归一化为未提供"——与
  `handle_skills_online_search` 既有 `int()` 严格校验先例对齐，三个
  action 入口补齐同层校验；后续新增 action 参数时应在入口声明类型
  并就地校验，避免再依赖巧合拒绝。
- name-only 卸载回退仅服务"origin 未提供"这一语义，类型非法必须
  显式拒绝——已用注释锚定在 origin 校验处。

## 遗留与后续

- `handle_skills_web_uninstall` 的 `service_id` / `agent_id` 及
  `handle_skills_web_install` 的 `url` / `signature` 仍是软转换；
  非法类型会被后续必填校验巧合拒绝，无 #5882 式误删路径，暂不扩大
  修复范围。若后续报告同类问题，按本次模式补齐。
