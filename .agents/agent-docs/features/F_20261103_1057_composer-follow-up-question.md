---
id: F_20261103_1057_composer-follow-up-question
title: 回答后在输入框占位位置显示下一步提问
type: F
author: Rossoneri
author_email: 863031676@qq.com
modules:
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel
files:
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/index.tsx
  - jiuwenswarm/channels/web/frontend/src/components/ChatPanel/InputArea.tsx
  - jiuwenswarm/channels/web/frontend/src/hooks/usePromptSuggestionBox.ts
  - jiuwenswarm/agents/harness/common/recommendation/prompt_suggestion.py
  - jiuwenswarm/server/runtime/recommendation_rpc.py
  - gateway/channel_manager/web/app_web_handlers.py
  - jiuwenswarm/channels/web/frontend/tests/recommendation.test.mjs
designs: []
design_note: 按用户明确要求调整既有提示词推荐交互，无独立设计。
---

# 回答后在输入框占位位置显示下一步提问

复用现有 prompt_suggestion.ready 生成及推送链路；完成的回答通过 forks.turn_ended 调度生成，不伪造推荐文字。
此前消息区建议标签可见时会抑制输入框占位建议；删除该可见性观察条件。输入框在支持的聊天模式中优先展示当前建议，保留不可用、审批、压缩、语音等状态提示。
既有 hook 只在当前会话、页面有效、回答结束且草稿为空时提供建议；新回答或用户输入使旧建议失效。
保留 Tab/右方向键填入草稿、编辑、显式发送及来源归因。根据用户后续要求，移除回答下方建议标签及关闭按钮；输入框不添加“下一步：”前缀，仅显示后端建议正文和 Tab 填入提示。展示归因只计入实际显示的第一条建议，不将未展示的备选标记为已展示。

Tab 填入后清空的回归：contenteditable 删空时浏览器可能残留 br 或 div/br，导致 :empty::before 不生效。输入同步时清除无文字、无不可编辑 token 和无图片的空结构，并将草稿归一为空字符串；输入法组合期间不清理。已接受建议不复活，恢复默认占位提示。

## 推送丢失恢复

实机会话 web_1a19f59c152_020078285faf 于 11:01:16 收到 prompt_suggestion.ready，但未记录 shown。App 推荐开关只在初始配置加载及设置回调同步，外部保存开关可能留下旧关闭状态，使 hook 直接丢弃推送。
新增 prompt_suggestion.get，Gateway 转发 recommendation.job 的 prompt_suggestion_get 动作；Runtime 从最新成功完成请求的生成账本读取尚未接受、忽略或抑制的候选，同时返回实时提示词推荐开关。关闭、旧请求、删除前和其他会话候选不返回。
前端在进入会话、完成回答、连接恢复及旧关闭状态收到新推送时补读；配置修订、页面离开、新请求或输入状态变化后丢弃过期查询响应。
重载本机 Runtime 与 Gateway 后，接口已读回该会话的原建议；11:12:29 浏览器日志记录了对应 suggestion_id 的 shown，确认页面已消费恢复内容。

## 当前环境

排查确认 prompt_suggestion_enabled=false、topic_recommendation_enabled=true；通过 config.save_all 开启当前环境的提示词推荐，不修改其他开关，也不改变安装默认值。

## 验证

npm.cmd run test:recommendation：19 项通过，包含标签可见时占位建议仍显示、Tab 填入不发送、发送携带建议来源、新一轮回答替换旧建议、用户草稿不被覆盖。
补读修复后：前端 21 项、后端 test_prompt_suggestion.py 与 test_topic_recommendation.py 合计 217 项通过。移除标签及前缀后，前端 20 项通过，覆盖正文起始、无标签节点及仅第一条建议计为展示；此前点击标签直接发送用例已被 Tab 填入后显式发送用例替代。
Tab 填入后清空版本：前端 21 项通过，新增 br 和 div/br 两种浏览器删空残留的默认占位恢复用例，并确认不会再次采纳旧建议。
未运行真实用户新问答来触发模型生成；上述推送与交互回归使用可控事件。浏览器 URL 识别限制下未取得当前页面截图。
