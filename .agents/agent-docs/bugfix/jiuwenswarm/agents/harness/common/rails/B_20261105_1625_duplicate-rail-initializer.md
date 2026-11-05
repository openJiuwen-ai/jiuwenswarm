---
id: B_20261105_1625_duplicate-rail-initializer
title: 合并后重复构造函数导致浏览器与 CUA 路由初始化丢失
type: B
author: yzdnh123
author_email: yzdnh123@gmail.com
modules:
  - jiuwenswarm/agents/harness/common/rails
  - tests/unit_tests/agentserver
files:
  - jiuwenswarm/agents/harness/common/rails/browser_task_prompt_rail.py
  - tests/unit_tests/agentserver/test_deep_adapter_web_tool_language.py
designs: []
design_note: 恢复合并前已有初始化契约，无独立设计。
---

# 故障与根因

GitCode !9593 候选 811b58286 的官方 UT 报告记录 21756 passed、13 failed、126 skipped。
BrowserTaskPromptRail 自动合并后含两个 __init__。后一个零参数构造函数覆盖原有构造函数，
导致调用者传入 enable_subagent_runtime / cua_agent_enabled 时抛 TypeError，
默认构造时遗漏 _deep_agent、_cua_agent_enabled 和 task_prompt_extension。
这导致 11 项浏览器/CUA 接线及系统提示词测试失败。
另两项 Web 工具语言测试通过 object.__new__ 构造夹具，但漏设多模态注册路径所需的 _audio_model_config。

# 修复

把 _channel 初始化并入原有参数化构造函数，移除重复定义；保留 set_channel 及同步子代理配置。
测试夹具显式设置 _audio_model_config = None、_instance = None、_is_session_scoped_adapter = False 和 _tool_retrieval_session_enabled = False，保持其无活动实例、不注册可选工具的既有含义。
不改变工具语言断言，不跳过失败用例。

# 验证

运行 test_cua_subagent_wiring.py、test_deep_adapter_web_tool_language.py 和 test_system_prompt_restructure.py。
修复后前两个路由/提示词测试文件 127 passed；语言夹具补齐后单独复验 2 passed，合计 129 passed，覆盖官方全部 13 项失败。使用 Python 3.13 和锁定 Core/Gateway/protocol 源码。
未运行本地格式化或静态检查。官方静态检查日志 j_QHuMp5F9.txt 显示任务持续 running，20 分钟后 task timeout，report_url/result 均为空；没有返回代码违规清单，需重跑检查服务。
截图环境修复另存本地，不纳入本次 CI 补丁。
