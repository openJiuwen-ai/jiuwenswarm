---
id: S_01_affinity-settings
title: 算力亲和设置与模型能力契约
type: S
author: MengHan18
author_email: hanmeng22@huawei.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/channels/web/frontend/src/features/settings
files:
  - jiuwenswarm/common/kv_cache_affinity_config.py
  - jiuwenswarm/common/config.py
updated: '2026-11-13'
---

# 算力亲和设置与模型能力契约

全局策略使用 `kv_cache_affinity_config.enable_kv_cache_affinity`，模型能力使用 `model_client_config.extensions.kv_cache.mode=affinity`。旧 `AscendAffinity` Provider 继续作为能力兼容别名。实验开关不自动更改 Provider 或服务地址。

## 准入和状态

全局准入读取有效的已配置推理模型：`models.defaults` 全部条目与 `models.agentos` 并列；没有 defaults 时兼容旧 default/react/环境变量入口。没有实际 model_name 的占位条目不计入；`is_default=false` 是备用模型而非禁用。至少一个模型声明亲和能力即可开启，默认模型和 AgentOS 中的普通云端模型都不否决。能力准入不等于远程协议探测成功，也不保证多端点控制目标唯一。

`config.get` 输出只读 `kv_cache_affinity_can_enable` 和 `kv_cache_affinity_status`。状态含 `requested_enabled`、`enabled`、`can_enable`、`scope`（当前为 `configured`）及 `models[{name,capable}]`，不包含凭据。`enabled` 表示通过本地准入的有效配置策略，不表示运行中任务已经完成重载。

界面只显示标准亲和开关，通过声明式 `optimistic:false` 在后端成功保存后才点亮。点击直接提交 `config.save_all`，不另发 config.get 预检查；无支持项时拒绝持久化，开关保持关闭，使用现有全局 Toast 在屏幕顶部提示“目前没有支持算力亲和的模型，请先在‘模型’中配置”。连接错误保留其错误原因，不能冒充无支持项。不常驻展示检查区，也不弹检查模态框。关闭直接保存 false，保留能力声明。

页面初次读取缺少有效状态时不显示开启。控制面 config_service.get_panel 与 Runtime 配置接口复用无 core 运行时依赖的 get_affinity_status。页面加载/连接恢复及模型保存事件重新读取配置，开关保存响应更新当前状态；不定时轮询、不向推理服务发送探针。

## 保存边界

显式开启在 `update_config()` 的写锁内检查完整候选；失败抛出请求错误，不写入该候选。旧批量模型保存与显式开关合并校验、一起提交。模型保存/删除导致最后一个支持项消失时，在同一次配置写入中关闭策略；仍有支持项时，改默认模型、修改普通模型或删除其他支持项都不关闭。配置重载与持久化使用相同的存在性规则。

原子性限定于亲和与模型配置，不覆盖通用设置中 `.env`、权限等其他存储。不得在持锁 mutator 中再次进入不可重入的 `update_config()`。

`config.set` 的亲和写入位于其他字段校验完成之后；`config.save_all` 先处理其余配置参数，再提交模型与亲和的联合候选。非法 Provider、技能检索参数或 MoA 参数不得导致模型或亲和提前持久化。此顺序不把通用设置的多存储写入扩展为整体事务。

亲和相关的成功保存响应携带最终状态和 `kv_cache_affinity_auto_disabled`；无关的 `config.save_all` 操作保持原响应形状。自动关闭触发 Agent runtime 范围重载。

## 模型编辑

新声明限于无厂商预设身份的自定义 OpenAI-compatible 服务；界面使用现有模型 Form switch。后端校验新增显式声明；旧部署配置仍兼容读取。

旧部署中已带亲和能力的厂商预设模型，界面没有亲和控件时，只改别名、推理参数等无关字段须保留其 extensions 与原 Provider。后端允许原服务身份、配置来源和既有能力不变时回传该声明，但不允许给新的不合格服务添加能力；换服务、模型名或协议仍撤销旧声明。

取消原声明显式写 `mode=none`。递归合并保留无关扩展；原有非 affinity 模式不会因复选框未选中而被清除。连接地址、模型标识、协议或服务来源改变后不继承原服务能力；前端要求重新确认，后端未收到显式新声明时清除旧亲和。撤销/更换旧别名连接同时移除其亲和授权语义。

## 会话与请求

Agent/Team 应用级策略按全局开关和支持项存在性装配，不能用一个云端控制模型关闭整个应用策略。会话选择不更改应用总开关。最终调用模型不支持亲和时不注入亲和身份参数；core 的普通 OpenAI 客户端也按自身扩展模式决定是否生成 `agent_hint`。开启策略、最终模型能力及有效身份同时满足时才生成亲和提示。

关闭策略不新建 Runtime、不对新调用注入亲和字段；已有资源沿原生命周期收尾。热更新按现有配置重载机制生效，不承诺中断在途请求。服务端错误不触发去字段后自动重试生成。


缓存管理保留会话实际端点绑定；冷启动兜底在 defaults 与 AgentOS 的并集中寻找唯一物理亲和目标。模型列表变化时刷新进程内兜底缓存，已绑定会话仍沿用其原服务。多个不同目标时跳过兜底，不任意挑第一个；不能让 AgentOS 云端条目遮蔽 defaults 中的亲和目标。登录模型和多模态专用模型不参与自定义推理模型的全局准入。
