---
id: B_20261120_2339_singleton-model-is-default
title: models.upsert 对单条 model_name 强制 is_default=true，请求 false 被静默改写
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules:
  - jiuwenswarm/common
files:
  - jiuwenswarm/common/config.py
  - tests/unit_tests/test_model_resource_fixes.py
designs: []
design_note: 仅调整 is_default 的读取推断规则，不改变写入契约、模型分组结构与选择兜底；无独立设计。
---

# models.upsert 对单条 model_name 强制 is_default=true，请求 false 被静默改写

## 现象与根因

#6853：`models.upsert` 请求体中 `model` 携带 `is_default: false`（新增一个
独立 `model_name` 的模型），响应里的 `is_default` 却是 `true`，与请求不一致。

链路排查：

1. `upsert_model_resource` 正常保存了 `is_default: false`，写入本身没有改写。
2. 响应由 `ModelCatalog().get_public_model_detail(saved["model_id"])` 构造，
   该读取路径经过 `_decrypt_model_entries` → `_infer_is_default`。
3. `_infer_is_default` 对「同 `model_name` 组内只有一个条目」的分支无条件写入
   `is_default = True`，显式 `false` 被覆盖，于是响应、`models.list` 与
   `models.detail` 全部回显 `true`。

即写入与读取对同一字段的口径不一致：调用方无法表达「新增模型但不是该组默认」。

## 修复决策

- `_infer_is_default` 的单条目分支改为「仅当条目缺省 `is_default` 时推断为
  `True`」，显式 `false` 原样保留。
- 多条目分支保持原语义：有显式 `true` 则该条独占，否则首条为默认。
- 无默认条目的组不会导致模型选择失败：`select_default_model_entry` 在找不到
  `is_default=True` 时回退到首条可用条目，`_create_model` 也有
  `next(iter(self._model_cache.values()))` 兜底。
- 不改变 `models.upsert` 写入契约、`models.replace_all` 与分组/亲和逻辑。

## 验证

`tests/unit_tests/test_model_resource_fixes.py` 新增
`test_upsert_honours_explicit_non_default_on_singleton_group`：

- 新增独立 `model_name` 且显式 `is_default: false`：`upsert_model_resource`
  返回 `false`，`ModelCatalog.get_public_model_detail` 回显 `false`；
- 同样场景省略 `is_default`：仍推断为 `true`，确认不破坏「单条目即默认」。

定向执行该用例 1 passed。

## 遗留

- `models.replace_all` 同样走读取推断，本次修复一并生效。
- 文档由 Gateway 仓维护；修复后「请求 false → 响应 false」与文档契约一致，
  未改动文档文本。
