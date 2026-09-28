# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Config panel 下沉 handler 命名空间（gateway 拆分）。

Web / TUI 两侧 config 域 handler 按通道分模块保留（两侧同名 RPC 契约
不一致，禁止合并实现）：
- ``models_handlers``：Web 侧 models/config 域
- ``config_set_handlers``：Web 侧 config.set / config.save_all 域
- ``tui_models_handlers``：TUI 侧 config 域（Auto-Harness 等专属字段）
"""
