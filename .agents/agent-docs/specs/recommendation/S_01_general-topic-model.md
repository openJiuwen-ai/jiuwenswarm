---
title: 常规页话题推荐配置契约
author: Rossoneri
author_email: 863031676@qq.com
id: S_01_general-topic-model
type: S
modules: []
files: []
---

﻿---
title: 常规页话题推荐配置契约
author: Rossoneri
author_email: 863031676@qq.com
id: S_01_general-topic-model
type: S
modules: []
files: []
---

﻿---
title: 常规页话题推荐配置契约
author: Rossoneri
author_email: 863031676@qq.com
id: S_01_general-topic-model
type: S
modules: []
files: []
---

﻿---
author: null
modules:
  - jiuwenswarm/channels/web/frontend/src/features/settings
  - jiuwenswarm/server/runtime/recommendation_rpc.py
code_files:
  - jiuwenswarm/agents/harness/common/recommendation/topic_controls.py
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/general/TopicModelSettings.tsx
---
# 常规页话题推荐配置契约

- 话题推荐默认为关闭。配置入口为设置-常规，不提供独立推荐 Tab。
- 声明式 switch 可用 configure 组件延迟启用；只有配置成功且 onSave 完成才关闭弹窗。取消或失败不启用。
- 配置复用 MediaModelConfigDialog 的提供商、协议、API Key、模型连接表单，不显示添加模型的别名、推理级别或上下文窗口。topic_starters.select_model 的 configuration 对象独立存入 recommendation.model_config，不写入聊天 models 列表。
- topic_starters.status 的 model_config 默认返回独立配置及 has_api_key，不返回 api_key；仅显式 include_api_key=true 时返回真实密钥供编辑回填，编辑框默认 password 隐藏，可通过眼睛切换可见性。旧 configured_model 不用于渲染模型卡片。configured_model 表示显式绑定且仍存在的模型 ID；未绑定为字符串空值。selected_model 同样仅返回显式配置；缺失、空值、旧 follow_main 或已删除的模型均不回退聊天默认模型。
- topic_starters.select_model 的 model 空字符串清除 model_config 和旧绑定，在同次配置写入中将 topic_enabled 设为 false，关闭运行时并广播 topic_starters.control_changed(enabled=false)。不删除共享模型资源。
- 首页只有明确 disabled 状态显示关闭引导；preparing、no_content 等开启但无卡片的状态不显示该引导。


