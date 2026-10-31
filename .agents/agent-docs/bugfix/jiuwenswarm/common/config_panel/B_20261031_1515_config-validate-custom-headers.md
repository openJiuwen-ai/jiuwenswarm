---
author: linghb
title: 配置了 custom_headers 的模型「测试连接」误报 401
type: B
modules:
  - jiuwenswarm/common/config_panel
files:
  - jiuwenswarm/common/config_panel/models_handlers.py
  - jiuwenswarm/common/config_panel/tui_models_handlers.py
  - tests/unit_tests/common/test_config_validate_model_mcc.py
designs: []
design_note: 对现有 validate 探测行为的缺陷修复，方案在前置检视分析中与用户确认（持久化基线 + 表单覆盖）。
author_email: 957733934@qq.com
id: B_20261031_1515_config-validate-custom-headers
---

# 配置了 custom_headers 的模型「测试连接」误报 401

| 项 | 值 |
| --- | --- |
| 日期 | 2026-10-31（Asia/Singapore） |
| 范围 | `jiuwenswarm/common/config_panel`（Web / TUI 两条 `config.validate_model` 链路） |
| 测试基线 | 24 项新增回归全绿；相关既有测试 162 通过（详见验证节） |

## 现象

`models.defaults` 条目在 `model_client_config.custom_headers` 配置了请求头鉴权（如
`Authorization: Bearer ...`）的网关上，对话链路正常，配置页「测试连接」却返回 401。
用户在配置页修改 custom_headers（需直接改 YAML）后再点「测试」结论不变——测试
结果对该字段完全不敏感。

## 根因

`custom_headers` 在 UI 契约中是 write-only 字段：`models.list` 响应不返回它
（前端测试声明 `write_only_fields: ['model_client_config.custom_headers']`），
因此前端 payload 物理上不可能携带它。而 `config_validate_model_handler`
（Web：`models_handlers.py`；TUI：`tui_models_handlers.py`）只从表单 params 拼造
7-8 个字段的 `ModelClientConfig`，从不读取持久化条目——依赖请求头鉴权的网关
探测请求缺少鉴权头。

对照链路证据：

- 对话路径 `interface_deep.build_model_from_entry` 把持久化 mcc **整包**传给
  `ModelClientConfig(**mcc_fields)`，`custom_headers` / `ssl_cert` 等字段自然生效；
- core pin 版本 `ModelClientConfig.custom_headers` 是正式字段
  （`schema/config.py:83-85`，`extra="allow"`），`headers_helper` 每次调用合并进请求；
- 同文件 `resolve_model_config_obj_for_validate` **已经**按 `model_name`/`alias`
  查持久化条目取 `model_config_obj` 做 mco 侧基线——mcc 侧的模式另一半缺失。

即：探测与对话对同一份持久化配置不同构，mco 侧对齐了，mcc 侧没有。

## 决策

1. 新增 `resolve_persisted_mcc_for_validate(model_name)`：与
   `resolve_model_config_obj_for_validate` 完全同一查找口径（`model_name` 或
   `alias` 匹配、首个命中、深拷贝返回），取条目 mcc 段；找不到或读取失败
   返回空 dict，探测退化为仅表单字段（历史行为，未知模型不受影响）。
2. 新增 `build_probe_model_client_config(...)`：**持久化条目为基线、表单草稿值
   权威覆盖、探测预算最后固定**。表单字段（用户正在编辑的草稿：api_key /
   api_base / model_provider / endpoint_profile / verify_ssl）始终覆盖持久化值；
   `client_id="config-validate"` / `timeout=25.0` / `max_retries=0` 最后写入，
   不被条目的 `timeout: 1800` 等运行值污染；`model_name` 从基线剔除（client
   config 不含该字段，与对话路径一致）。无草稿 `endpoint_profile` 时剔除基线值
   ——调用方（Web 侧 `effective_endpoint_profile`）按 api_base host 推断的语义
   保持权威。
3. Web 与 TUI 两个 handler 都改为经这两个函数构造探测配置。实现只在 Web 侧
   `models_handlers.py`，TUI 侧 import 复用——符合模块契约（两侧 handler
   契约不一致禁止合并，但纯函数可复用，`normalize_provider_value` 同款范式）。
   TUI 侧方言选择链与 Web 侧同口径（草稿 → host 推断 → 持久化兜底，见
   二次检视修正）。
   gateway/common 剥离副本（105 行，不含 validate handler）无同步义务。
4. `ssl_cert` 等其他 write-only 字段随整包基线自然进入探测，与对话路径
   逐字段一致，无需逐字段枚举。

复查修正（2026-10-31，首版实现后自查发现的三处问题）：

- 移除 `build_probe_model_client_config` 未使用的 `model` 参数（死参数）。
- `verify_ssl` 改三态：表单**显式提交**才覆盖（前端 payload 实际不传该键，
  首版 `bool(params.get("verify_ssl", False))` 恒 False，会静默翻转用户
  YAML 里 `verify_ssl: true` 的探测结论）；未提交时保留基线值，基线也
  没有时回落 False（探测历史默认，而非 core 的 True）。
- 基线字段类型非法（手改 YAML）时 `ModelClientConfig` 构造抛
  ValidationError——修复前探测字段全来自代码字面量不可能炸，修复后用户
  数据可触发未处理异常。构造点加 fallback：退化为仅表单字段并告警，
  脏配置不打断探测。未知 extra 字段（`base_url` 等）经 core
  `extra="allow"` 正常透传，不触发 fallback。

二次检视修正（2026-10-31，外部检视确认的三处问题）：

- **TUI 探测方言丢失**：TUI 侧 validate handler 原先不传
  `endpoint_profile`，builder 按「无草稿剔除基线值」契约剔除持久化方言
  ——条目显式配置方言（host 不在 override 表、无法重新推断）时，TUI
  探测与保存后对话路由不一致。修正为 TUI 调用方与 Web 侧同口径：
  草稿方言优先 → `effective_endpoint_profile(api_base)` 按 host 推断 →
  持久化方言兜底（方言与 api_base 绑定，推断命中时旧值不残留）。
  builder 契约不动——三态语义（None=保留基线）会把绑定旧 api_base 的
  过期方言带进探测（Web 编辑改连接时前端删除该键，后端无法区分
  「清空」与「未提供」），比现状更糟。
- `resolve_persisted_mcc_for_validate` 的 `isinstance(mcc, dict)` 判断
  原在取值之后：truthy 非 dict（手改 YAML）在 `mcc.get` 即抛
  AttributeError，中断整条扫描提前返回空 dict，且该分支永不可达。
  判断提前为跳过该条目继续扫描（同函数既有 `entry` 判断同款）。
- 回归用例 `test_model_name_takes_precedence_over_alias` 名不副实：
  实现是首条命中即返回，不存在 name/alias 优先级机制，用例实际锁定
  的是列表顺序。改名为
  `test_first_matching_entry_wins_over_later_alias_match` 并在 docstring
  声明顺序语义，另补 2 个方言选择链用例（持久化兜底 / 推断覆盖）。

## 拒绝的方案

- **新建 `model_client_factory` 公共模块统一模型构造入口**：两处消费
  （validate 探测）撑不起一个新公共抽象，且与 `interface_deep.build_model_from_entry`
  形成双权威——reviewer 必拒的架构争议点。宁可局部复用同文件查找口径。
- **反向让 common 引用 `interface_deep.build_model_from_entry`**：破坏
  common ← server 依赖方向。
- **只补 `custom_headers` 单字段**：diff 更小，但 `ssl_cert` 等同病字段遗留，
  且需逐字段论证与对话路径的等价性；整包基线一句话可说清
  （探测 Model = 保存后对话 Model − 表单草稿差异 − 探测预算）。
- **改前端把 custom_headers 放进 validate payload**：write-only 契约是有意的
  安全设计（headers 含 token，不下发到前端），不能为修 bug 反向破坏。

## 验证

新增回归 `tests/unit_tests/common/test_config_validate_model_mcc.py`（24 用例）：

- 查找口径：model_name / alias 匹配、无匹配返回空 dict、config 读取异常返回
  空 dict、非 dict 条目跳过、深拷贝隔离、首条命中优先（顺序语义）；
- 构造语义：持久化 custom_headers 进入探测、表单草稿覆盖持久化 draft 字段、
  探测预算不被 `timeout: 1800` 污染、endpoint_profile 草稿优先 / 无草稿剔除
  基线值 / 调用方回传持久化方言生效 / host 推断覆盖过期方言、verify_ssl
  显式覆盖 / 未传保留基线 / 无基线回落 False、无基线等价历史行为、
  `model_name` 不进 client config、非法字段类型 fallback、未知 extra 字段
  透传。

端到端冒烟（monkeypatch config + probe 捕获 Model 实例）：Web 与 TUI 两个
handler 各验证一次，持久化 `custom_headers` 出现在探测 `ModelClientConfig`，
表单值正确覆盖，探测预算正确固定；verify_ssl 未提交时保留基线 True；非法
字段类型时探测仍成功执行（fallback 生效）。

定向回归（相关既有测试）：
`test_resolve_model_config_obj.py` + `test_available_models_merge.py` +
`test_model_config_validation.py` + `test_cron_model_validation.py` +
`test_config_concurrent_models.py` + `test_moa_config_panel.py` +
`test_runtime_model_config.py` + `test_auth_model_catalog.py` +
`test_cli_channel_handlers.py`——复查修正后合计 **139 passed, 1 skipped**
（skip 为既有用例的跳过标记，与本次无关）。

环境说明：执行前 `uv sync` 对齐了本地 venv 与 uv.lock 的 openjiuwen pin
（旧 `5ee4e38d` → pin `e0891988`，uv.lock 本身未修改，`git checkout` 还原了
sync 产生的 lock 文件扰动）。`test_jev_model_clear.py` 3 例失败经 stash
基线对照确认为依赖对齐后暴露的既有测试债，与本次修改无关（修改前后
失败集完全一致）。

## 限制与边界

- **core `sanitize_headers` 会剥掉 `authorization` 键**（受保护头集合
  `PROTECTED_HEADERS`）。条目以 `custom_headers.Authorization` 传递 Bearer
  token 的场景，实际生效路径是 api_key 字段（SDK 注入 Bearer 头）或
  `auth_mode: custom_headers`（空 api_key 时跳过 Authorization 注入、由
  非 Authorization 名的自定义头鉴权）。本修复把 `custom_headers` 原样
  带入探测——与对话路径逐字节一致，core 侧行为对称，不引入差异；
  `auth_mode` 等字段也随整包基线自然进入探测。
- 未保存的新模型查不到条目 → 无基线 → 依赖头鉴权的网关仍可能 401。这是
  预期语义：「测试」验证的是保存后的配置，新模型先保存再测试。
- 同名/同 alias 多条目取首条，与 `resolve_model_config_obj_for_validate`
  既有口径一致。
- agentos 备份条目可被命中——与对话切换到 agentos 模型的行为一致。
- 登录模型（is_login_model）凭据回填不在本次范围：api_key 校验逻辑未动。
- 未验证真实外部网关的 401 复现（需要真实鉴权网关环境）；核心行为经
  monkeypatch 端到端冒烟覆盖。
