# 转换结果示例面板

本示例只演示 JiuwenSwarm 系统插件的基本文件关系：manifest 声明已有槽位，入口通过 Registrar 订阅已有 `ui.ready`，再调用已有 `ops.ui.render` 显示内容。

实际转换时必须先分析目标 DeepSeek Harness 插件，再从三个平台仓库的 `develop` 分支核对目标事件、Ops、UI 槽位和真实业务触发入口。不要直接把本示例的事件或界面当作目标插件已经具备的能力。

`test_package.py` 从 `JIUWEN_SYSTEM_PLUGIN_ZIP` 指向的候选 ZIP 安装系统插件，验证真实加载、Handler 发布和渲染结果。用本 Skill 的 `scripts/finalize_package.py` 运行测试后，测试通过的同一 ZIP 才能交付。
