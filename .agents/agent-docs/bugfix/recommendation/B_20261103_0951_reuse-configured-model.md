---
id: B_20261103_0951_reuse-configured-model
title: 重新开启话题推荐时复用已配置模型
type: B
author: Rossoneri
author_email: 863031676@qq.com
modules:
  - jiuwenswarm/channels/web/frontend/src/features/settings
files:
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/general/TopicModelSettings.tsx
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/agent/MediaModelConfigDialog.tsx
  - jiuwenswarm/channels/web/frontend/tests/recommendation.test.mjs
designs: []
design_note: 根据用户明确要求修正已保存模型的重新启用行为，无独立设计。
---

# 重新开启话题推荐时复用已配置模型

## 现象与修复

开关声明的 configure 入口每次开启都会渲染连接表单，即使已有独立推荐模型配置。
现在该业务组件读取 topic_starters.status 后，有 model_config.model 时直接保存开关并关闭配置流程；不调用 select_model，不重写连接信息。
没有独立模型时仍展示原配置表单；修改入口 enableOnSave=false，始终展示编辑表单。
直接启用失败时保持开关关闭，显示错误并支持重试。共享设置渲染器不增加话题字段分支。

后续编辑回显修复：status 仅返回 has_api_key，不返回密钥正文；连接未变且有已保存密钥时，API Key 输入框显示掩码占位和“已保存，留空保留原密钥”。输入值保持为空，避免将掩码当作新密钥提交；切换提供商或 API 地址后不再显示保留提示。

用户随后明确要求真实回显，替代上述占位方案：status 默认仍不返回密钥，仅参数 include_api_key 为布尔 true 时返回已保存 api_key。编辑入口请求该参数并直接填入 password 输入值，摘要及直接开启流程不请求密钥。保留眼睛切换可见性，移除占位提示语言键。该变更未涉及推送、日志中的明文输出。

后续样式对齐：话题推荐声明式开关通过 rowClassName 复用图像生成的 settings-agent-media__row，去掉子区域分隔线，采用同样的左右及底部留白；摘要卡片、名称和动作区直接复用图像生成已有 CSS，包含背景、圆角、字体、长名称省略和窄屏间距。新增通用行类名透传，不在共享渲染器判断具体业务字段。

## 验证

- npm.cmd run test:recommendation：18 项通过，覆盖未配置取消、已配置直接启用、启用失败重试、关闭重开、手动编辑和删除。
- 回显修复后重新运行上述 18 项测试通过，增加已保存密钥的占位回显、编辑直接保存不提交掩码、未配置模型不显示已保存提示的断言。
- 真实回显替代方案：前端 18 项通过，验证真实测试密钥进入 password 输入值并原样保存；后端 70 项通过，验证默认不返回密钥、字符串 true 不触发、布尔 true 显式返回。
- 真实回显版本 npm.cmd run build 通过；重载本项目 Runtime Worker 后，经 localhost:5173/ws 验证普通 status 无 api_key，编辑 status 返回非空 api_key，诊断输出仅记录布尔结果，未打印密钥。
- 样式对齐版本推荐回归 18 项通过；实际浏览器截图受既有工具 URL 识别限制未验证。
- npm.cmd run build：通过。
- 浏览器工具无法可靠识别本机页面 URL，未完成实际页面截图核验；本次不改变布局、主题与表单样式。
- 中英文使用说明同步启用条件。后续修改模型配置判据或开关保存契约时需重验。
