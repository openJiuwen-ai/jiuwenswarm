# A2UI Web-only PR 记录

## 背景

A2UI 的完整能力依赖 Web 前端 renderer、浏览器状态管理和 `a2ui.client_event` 回传。微信、飞书等 IM channel 当前不是 JiuwenSwarm 可控的 Web runtime，因此不应感知 A2UI，也不应承担 A2UI fallback 逻辑。

## 本次变更

- 保持 Web channel 的 A2UI 支持、特性和配置入口不变。
- 将 A2UI channel 策略集中到 `jiuwenswarm.server.runtime.a2ui.integration.is_a2ui_channel`，当前只有 `web` 支持。
- 非 Web channel 直接 bypass A2UI：
  - 不注入 A2UI prompt。
  - 不把 A2UI client event 转成模型 prompt。
  - 不执行 A2UI response finalizer。
  - 不做 A2UI 文本 fallback。
- 保留 gateway 侧兼容 hook 和 `non_web_fallback_enabled` 旧配置字段，但默认值改为 `false`，当前实现不再使用非 Web fallback。
- 更新中英文 A2UI 文档和 IM channel 可行性分析，明确 Web-only 边界。

## 测试记录

已覆盖的关键行为：

- `is_a2ui_channel` 只允许 `web`。
- 非 Web A2UI client event 返回 `None`，进入普通 prompt 流程。
- 非 Web assistant response 不触发 config lookup、validation 或 repair。
- 非 Web 出站 payload 保持原样，不执行 fallback。
- response prompt rail 仅在 Web channel 注入 A2UI prompt。

验证命令：

```powershell
uv run pytest tests/unit_tests/a2ui/test_integration_bridge.py tests/unit_tests/a2ui/test_web_only_prompt_rail.py tests/unit_tests/a2ui/test_feature_config.py -q
```

结果：`18 passed`。

## 后续建议

- 如果未来需要飞书卡片或微信结构化文本，应按平台 adapter 独立设计，不复用 Web A2UI renderer，也不把它定义为完整 A2UI。
- 如果 IM 需要完整 A2UI 体验，应通过 Web companion 页面承载交互，IM 只作为通知和入口。
