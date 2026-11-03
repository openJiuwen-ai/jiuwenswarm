---
id: S_02_prompt-suggestion-recovery
title: 下一步提问展示与补读契约
type: S
author: Rossoneri
author_email: 863031676@qq.com
modules:
  - jiuwenswarm/agents/harness/common/recommendation
  - jiuwenswarm/server/runtime
  - gateway/channel_manager/web
  - jiuwenswarm/channels/web/frontend/src/hooks
files:
  - jiuwenswarm/agents/harness/common/recommendation/prompt_suggestion.py
  - jiuwenswarm/server/runtime/recommendation_rpc.py
  - gateway/channel_manager/web/app_web_handlers.py
  - jiuwenswarm/channels/web/frontend/src/hooks/usePromptSuggestionBox.ts
---

# 下一步提问展示与补读契约

- prompt_suggestion.ready 推送 session_id、request_id、suggestion_id 和 text，text 为建议正文，不含界面前缀。
- prompt_suggestion.get 接受 session_id，返回 enabled 和 suggestions；通过 Gateway recommendation.job 的 prompt_suggestion_get 动作在 Runtime 执行。
- 返回候选属于当前会话最新成功完成的请求，且生成于当前删除边界之后；已 accepted、ignored 或 suppressed 的候选不返回。开关关闭时 enabled=false、suggestions=[]。
- 前端读取服务端当前开关以恢复外部设置变更，并在会话进入、回答完成、重连以及旧关闭状态收到推送时补读；新输入、会话变化和配置修订使旧查询失效。
- 仅当前会话、回答结束、输入框为空时显示第一条建议正文及 Tab 提示；不在消息区重复展示，不添加“下一步：”。
- Tab/右方向键填入草稿，显式发送后提交，保留建议来源和编辑状态归因。只对实际显示的第一条记录 shown。
- Tab 填入后删空恢复默认占位提示，不复活已接受建议。编辑器需清理浏览器删空留下的空换行结构，同时保留有效 token、图片和输入法组合状态。
