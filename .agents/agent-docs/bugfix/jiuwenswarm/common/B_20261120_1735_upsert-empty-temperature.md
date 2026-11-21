---
id: B_20261120_1735_upsert-empty-temperature
title: models.upsert 将空串 temperature 原样落库，与文档约定的空串转 null 不符
type: B
author: openjiuwen_ai
author_email: openjiuwen_ai@163.com
modules:
  - jiuwenswarm/common
files:
  - jiuwenswarm/common/config.py
  - tests/unit_tests/test_model_resource_fixes.py
designs: []
design_note: 仅统一 upsert 与 replace_all 对空串 temperature 的归一化口径，不改模型配置结构与合并语义；无独立设计。
---

# models.upsert 将空串 temperature 原样落库，与文档约定的空串转 null 不符

## 现象与根因

#6856：设置面板保存模型时提交 `model_config_obj.temperature = ""`，
`models.upsert` 返回体中的 `temperature` 仍是 `""`，而
`docs/zh/reference/gateway-web-api.md` 描述的契约是空串转 null。

链路排查：

1. `upsert_model_resource` 先调 `_normalize_model_entry`。当载荷已带
   `model_client_config`（设置面板的完整条目形态）时该函数直接返回，
   `model_config_obj.temperature` 的空串不会被归一化。
2. 随后 `_merge_model_update` 把空串写入持久化条目，`models.list` 再通过
   `mco.get("temperature")` 原样回显，于是请求与响应的 `""` 一致。
3. 同一时刻 `models.replace_all` 的合并逻辑先把空串转成 `None` 并移除该键，
   两条写入路径口径不一致。

扁平形态（无 `model_client_config`）的条目还会在 `float("")` 处直接抛
`ValueError`，属于同一缺口的另一表现。

## 修复决策

- `_normalize_model_entry` 的扁平分支把空白字符串视为「未设置」，跳过
  `float()`，避免 `ValueError`。
- 新增 `_clears_temperature`：识别嵌套 `model_config_obj.temperature` 与
  扁平 `temperature` 两处空串，在合并完成后从 `model_config_obj` 移除该键，
  与既有 `clear_max_tokens` 的处理位置和写法保持一致。
- 语义与 `models.replace_all` 对齐：空串等价于 null（未设置），响应与
  YAML 中保持「数值或缺失」，不再出现字面空串；显式数值仍原样落库。
- 未改动 `models.replace_all`、模型配置结构与 affinity 合并逻辑。

## 验证

`tests/unit_tests/test_model_resource_fixes.py` 新增
`test_upsert_treats_blank_temperature_as_unset`，覆盖：

- 嵌套载荷空串：已存的 `0.7` 被清除，返回体不含 `temperature`；
- 扁平载荷空串：不抛异常，同样被清除；
- `models.list` 回显 `temperature` 为 `null`；
- 随后提交数值 `0.3` 正常落库，确认修复不吞掉合法值。

定向执行 `-k "blank_temperature or upsert_preserves"` 2 passed。

## 遗留

- 文档由 Gateway 仓维护，本次让实现对齐文档契约，未改动文档文本。
- 其余同文件用例的失败均来自本地 `openjiuwen` core 提交落后于
  `uv.lock`（f3f8f57 vs 55756c5）导致的 `SymphonyConfig` 导入错误，
  与本次改动无关。
