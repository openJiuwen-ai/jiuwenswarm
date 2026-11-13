---
id: F_20261110_2026_affinity-settings
title: 实验功能算力亲和开关与自定义模型能力接入
type: F
author: MengHan18
author_email: hanmeng22@huawei.com
modules:
  - jiuwenswarm/common
  - jiuwenswarm/channels/web/frontend/src/features/settings
files:
  - jiuwenswarm/common/kv_cache_affinity_config.py
  - jiuwenswarm/common/config.py
  - jiuwenswarm/common/config_panel/config_set_handlers.py
  - jiuwenswarm/common/config_panel/model_resource_handlers.py
  - jiuwenswarm/common/config_panel/models_handlers.py
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/models/ModelDialog.tsx
  - jiuwenswarm/channels/web/frontend/src/features/settings/modules/experimental/definition.ts
  - jiuwenswarm/agents/swarm/config_specs.py
  - jiuwenswarm/server/runtime/session/kv_cache/kv_cache_model_provider.py
  - jiuwenswarm/server/runtime/session/kv_cache/kv_cache_application_runtime.py
designs: []
design_note: 外部初始方案为 docs_HM_kvc/E前端开关打开/01 和 02；全局准入和交互按用户确认的 04 小白版文档替代，无仓库内 D 标识。
created: '2026-11-10'
updated: '2026-11-13'
---

# 实验功能算力亲和开关与自定义模型能力接入

## 基线和实际实现

从更新后的 `develop` 提交 `09b3b6b7f` 创建开发分支，随后按用户要求命名为 `agentHint/front_settings`，工作目录仍为原 JiuwenSwarm 仓库。实际发布版本以 Git 提交历史为准。相对原方案的旧基线，设置页增加了 `availableWhen`，核心模型资源与能力契约未变化。

实验功能解除隐藏，使用标准 Settings switch 和已有可用性门控。当前按 2026-11-12 最终确认方案使用单一开关：至少存在一个支持模型即可开启，失败时顶部 Toast 提示，不常驻显示检查区、不额外发送预检查请求。模型 Form 增加自定义 OpenAI-compatible 能力复选框，贯通详情、草稿、提交、列表刷新和撤销。Provider 不由开关自动重写。

复用 `update_config()` 写锁实现开启前校验和模型变更自动关闭；旧 `config.save_all/models.replace_all` 与新版资源接口均接入。启动禁用兜底使用相同的“存在支持项”规则，取消 AgentOS 优先级作为准入条件，保留多端点歧义保护。会话临时换模型不关闭应用总开关。旧批量接口自动关闭亲和时也同时触发 agent_runtime 重载，避免运行态保留旧策略；新增注册 RPC 回归覆盖该边界。原有 `.env` 等跨存储事务不在本次扩展范围。

新增声明由后端验证服务类型，连接变更不继承旧能力；取消亲和明确写 none，保留其他扩展。自定义切换需要清除旧厂商身份，资源提交对此使用明确空值。

当前契约见 [S_01_affinity-settings](../specs/jiuwenswarm/common/kv_cache_affinity_config/S_01_affinity-settings.md)。原“自动切 Provider”逻辑被局部替代；旧别名读取、身份协议和生命周期规则继续有效。

## 实际验证和证据

- 后端模型、能力、生命周期与设置 RPC 定向回归：160 passed、253 deselected，退出码 0。覆盖注册 RPC、写盘失败无副作用、自动关闭及其重载范围。
- 最终请求体测试经过 JiuwenSwarm ModelCatalog/Resolver/Compiler 和实际 core OpenAI 客户端：开启+亲和模型有提示，关闭或云端模型无提示。
- 前端设置回归 67 passed；模型刷新回归 20 passed；新能力/表单/开关/状态行为验证 6 passed。
- `npm run build` 已通过；`git diff --check` 通过。
- 使用真实组件、模拟设置数据进行 Chrome 截图检查，覆盖 1280px 实验区域、模型弹窗及 420px 不兼容 AgentOS 状态。420px `scrollWidth=innerWidth=420`，无水平溢出。不是完整在线后端联调。

日志和截图位于工作区根的 `build_artifacts/affinity-*.txt` 与 `build_artifacts/affinity-preview/`。新增用例在 `tests/unit_tests/test_kv_cache_affinity_settings.py`、前端 `tests/kvCacheAffinitySettings.test.mjs`；旧隐藏断言改为可编辑，旧 Provider 联动断言改为独立能力规则。

## 环境修正与限制

新版 develop 锁定 gateway `435d3323425e1051c6fe2d1feadcba68b4be020d`，本地 `.venv` 原为 `cff3b1b9...`，缺少 `agent_gateway.common.protocol_methods`。仅将该包安装到仓库锁定版本，不修改依赖声明或锁文件，也不更改 core 源码。本次使用本地 core `b0429dce92f31a03b1e421b9fae35a0f23b488c4`。

Windows pytest 原有日志句柄使临时 HOME 删除失败。仓库外 `build_artifacts/run_affinity_pytest.py` 在 pytest_unconfigure 前释放 FileHandler，最终组合测试干净退出。未修改无关测试基础设施。

真实 Ascend 推理服务、完整运行中的多会话切换、深色主题及完整产品页面仍需本地验收。截图仅验证组件布局，能力勾选仅为人工声明，不证明对端协议可用；不增加无条件去字段重试。构建中的既有重复 CUA 翻译键及体积告警未作为本特性修改。


## 2026-11-12 界面修订

按用户反馈，把只读检查结果改为标准亲和 switch 的 always-visible subItems，使用现有 SettingRow 的 subSettings 容器。开关与结果位于同一个卡片，不再作为两个并列设置项；不改通用组件样式或后端检查语义。

检查仍只读取配置声明：scope 为 default 或 agentos；requested_enabled、enabled、can_enable 及 models[].capable 均为本地配置派生值。没有向推理服务发送探针或管理请求。界面更新来自页面加载/连接恢复、模型保存事件及开关保存响应，无定时轮询。

现有前端行为用例改为渲染实际 experimentalModule 的亲和设置项，并确认检查结果位于同一个开关卡片中：6 项通过；npm run build 和 git diff --check 通过。实际组件宽屏截图确认只有一张外部卡片，证据位于 build_artifacts/affinity-preview/experimental-integrated.png。此次只改展示布局，不沿用旧环境测试结论来声称最新 core 已联调通过。


## 2026-11-12 开启校验弹窗与控制面读取修复

用户指出不兼容模型应保持关闭并弹窗，而不是同时显示蓝色开关和未知诊断区域。移除常驻检查组件，使用现有标准 switch 的 configure 机制：点击时读取最新 config.get，通过才调用非乐观保存；不兼容、状态缺失或失败时不先点亮开关，弹窗仅有“知道了”。关闭仍直接保存 false。

根因是现代 Front 将 config.get 分派到 server/control/config_service.get_panel，而前一版只修改了 Runtime common/config_panel 下的读取实现。控制面仍返回原始开关值，不返回能力状态，造成蓝色开关与无法读取结果并存。修复控制面复用同一 get_affinity_status；通过把 KVCacheAffinityConfig 导入推迟到策略构建函数，维持 Front 不加载 core 的边界。没有复制第二套准入规则，也没有修改服务探测行为。

真实控制面 ReqMethod.CONFIG_GET 回归覆盖支持/不支持两种状态；配置与 Front phase1 测试 83 项通过，包含无 Runtime 导入检查。前端新增/更新的 8 项交互用例和原 67 项设置回归通过。Chrome 真实组件模拟数据截图覆盖普通开关和失败弹窗，证据为 affinity-preview/affinity-simple.png 与 affinity-blocked.png。完整在线实例需重启后端以加载控制面修复，不能只靠刷新前端。


## 2026-11-12 最终准入与顶部提示修订

用户确认 `docs_HM_kvc/E前端开关打开/04_两套方案对比与旧限制说明_小白版.md` 后实施。本节替代此前“AgentOS 全部或默认模型支持”的准入，以及 configure 预检查/模态提示交互；上文的历史验证仅对应当时版本。

- 统一配置准入枚举 defaults 全部具名模型及 AgentOS：任意支持项即可开启。保存、加载归一化、控制面读取使用同一规则；只在最后一个支持项消失时自动关闭。
- Agent/Team 装配使用独立的应用级策略构建函数；实际模型调用仍保留能力与身份门控。冷会话兜底移除 AgentOS 排他优先，仍要求唯一物理目标。复查发现进程缓存会在新增第二服务后保留旧目标，因此按当前模型条目失效兜底缓存；不清空 core 已有会话绑定。新增回归证明新冷会话跳过歧义、旧会话继续向原服务清理。
- 删除临时开启预检查对话框，标准 switch 声明 `optimistic:false` 与 `errorDisplay:toast`。共享 renderer 复用全局 Toast，业务错误翻译在设置服务层；不增加业务字段分支。保存成功前始终关闭，请求期间禁用。
- 无支持项返回稳定错误，顶部中文/英文提示；网络错误按自身原因提示。正常开启、关闭均无额外 config.get；关闭不改模型 extensions。
- 本地 core editable 为 `d11a35662793fd047bf09c1060d2b00ecf72d20a`，Swarm 基线仍为 `09b3b6b7f3fa7808625dc2803b1aede5125bdb8c`，分支已按用户要求改名 `agentHint/front_settings`。未修改依赖锁或 core。

最终证据：

- 前端新行为 9 项通过，经过真实 SettingsPageLayout/ConfigSourceProvider/保存队列/Toast；无支持项、保存中不点亮、成功点亮、连接异常分类、关闭均覆盖。
- Settings 原回归 67 项通过；Jev/浏览器/多模型协作反馈回归 71 项通过，确认新增 Toast 选项不改变其他设置的行内/页头错误规则。
- `npm run build` 通过。宽屏 1280px 与窄屏 520px Chrome 截图使用真实共享组件和模拟 RPC，无常驻检查区，失败提示在屏幕顶部；截图为 `build_artifacts/affinity-preview/affinity-final-normal.png`、`affinity-final-blocked.png`、`affinity-final-narrow.png`。
- 回归启动时修正测试替身漂移：Gateway Registry 的 crypto bridge 方法、Jev 图标/厂商导出及 Vite glob 替身，并补充 renderer 新增 Toast 依赖的测试导出。未因此修改产品的 crypto/厂商实现。

后端最终组合回归 **479 passed**，退出码 0：`test_kv_cache_affinity_config_rules.py`、`test_kv_cache_affinity_settings.py`、`test_team_kv_cache_affinity_config.py`、`test_kv_cache_affinity_lifecycle.py`、`agentserver/test_front_phase1.py`、`test_model_resource_fixes.py`、`gateway/test_app_web_handlers.py`。覆盖注册 config.save_all 对当前混合列表的成功/拒绝与写盘副作用，默认云端与 AgentOS 云端不否决、最后支持项删除/撤销、配置重载、Team 装配、真实 core wire body、热变更多端点兜底与旧会话绑定。日志 `build_artifacts/affinity-new-policy-tests.txt`；Windows 外置日志关闭插件沿用此前说明。

`git diff --check` 通过。按 jw-review 自检本轮准入、持久化、共享渲染与调用路径，修正旧兜底缓存后相关回归通过。真实推理服务、完整在线多会话与深色主题未在本轮联调，仍由用户本地启动验收；能力声明不等于对端实现协议。


## 提交前复测

2026-11-12 在当前工作区重新执行组合验证：后端 479 项通过；前端亲和 9 项、Settings 67 项、Jev/浏览器/多模型协作反馈 71 项、模型刷新 20 项通过，总计前端 167 项。`npm run build` 与 `git diff --check` 通过。日志为 `build_artifacts/affinity-commit-*.txt`，真实服务联调限制沿用前述说明。


## 2026-11-13 Rebase upstream/develop

按用户要求将 `agentHint/front_settings` rebase 到 `upstream/develop` 的 `4b7a8d3f9ba1233636a432a47dfcd9c2b8959a11`，保留本特性为单个提交。操作前保留本地备份分支 `backup/agentHint-front-settings-before-rebase-20261113`。

解决 7 处冲突：三个文档索引、前端 package.json、共享 SettingItemRenderer、设置 registry/types，以及 common/config.py。保留上游新增测试脚本、不可用开关及原因提示，同时保留亲和开关非乐观保存与顶部 Toast。模型保存路径同时保留上游初始占位项清理及清理后密钥保护顺序、亲和能力验证和最后支持项消失时关闭总开关。文档索引由同步脚本重建，保留原索引作者。

Settings 的一个源码结构断言同步为上游的 `enabled = checked && !unavailable` 门控，避免继续要求旧 `checked` 表达式；不改变产品行为。

后端在本地现有 Python 环境中验证：亲和配置、设置、生命周期、Team、Front、模型资源与 Gateway 组合回归 479 项通过；上游模型空值/占位配置回归另有 105 项通过，共 584 项。日志为 `build_artifacts/affinity-rebase-backend.txt` 与 `affinity-rebase-placeholder.txt`。默认关闭、仅声明能力不自动开启、只有普通模型时拒绝开启及混合模型准入也通过定向断言。

本地 core editable 为 `d11a35662793fd047bf09c1060d2b00ecf72d20a`，已安装 Gateway 为 `435d3323425e1051c6fe2d1feadcba68b4be020d`；新的 upstream 锁定 Gateway 为 `54e5330d3ea6be972aa38de4c42a898ea73fed37`。本次未同步 Python 依赖或改动 editable core，因此以上不代表完整新锁定环境、后端启动或一体机端到端联调通过。

前端按新锁文件执行 `npm ci --ignore-scripts --no-audit --no-fund`，未改动 package-lock.json；安装后重新运行亲和设置 9 项、Settings 67 项、Jev/浏览器/多模型反馈 72 项及模型刷新 20 项，共 168 项通过。日志为 `build_artifacts/affinity-rebase-final-test-*.txt`。

`npm run build` 与 `git diff --check` 通过；生产构建日志为 `build_artifacts/affinity-rebase-build.txt`。既有 CUA 重复翻译键、Rollup 注释及大包体积提示未纳入此次修改。


## 2026-11-13 流水线检视复核与模板注释

按用户要求补充 resources/config.yaml 的 agentos 注释示例，在 timeout 下展示 extensions.kv_cache.mode=affinity 和 affinity_field=agent_hint。实际 models.agentos 继续为空，总开关默认 false；YAML 解析前后相等。

检视对象为 `943b1cf6c79ce805dacf596d4b7823e8a464fea7` 及用户提供的流水线报告。按 jw-review 只读核实后转入 jw-dev 修复：

- 亲和提前落盘意见成立。新增用例复现 config.set 携带非法 Provider / skill_retrieval 参数时，BAD_REQUEST 仍改变亲和值；save_all 还可提前改变模型。将 config.set 的亲和提交推迟到其余校验之后，并将 save_all 的模型/亲和联合提交放到 apply_config_payload 成功之后。覆盖开启、关闭、带模型与不带模型、非法 MoA 参数；权限仍沿原 pending commit 边界，不扩大 .env 等跨存储事务承诺。
- 隐藏控件导致旧能力丢失意见成立。仅在原服务身份未变、亲和控件不可表达且没有连接切换时保留既有声明与 Provider；后端同步允许未改变来源和服务身份的原有能力回传。新增和改变服务的声明仍执行自定义 OpenAI-compatible 限制。协议切换不套用报告中无条件保留的建议，Anthropic 请求不能继承旧亲和能力。
- 缺少 status 时保留原始开关值的建议不采纳。现有 spec 和用户确认方案要求缺少有效状态时不显示开启，原始请求值不能证明模型仍具备能力；enabled 也不能换成 requested_enabled。本轮保留既有回归来锁定此行为；不承诺新前端与缺少状态契约的旧后端混用，未知状态不构成后端已实际关闭的证据。
- NH-001 随提交顺序修复一并处理；NH-002 删除重复 pop；NH-003 修正同级缩进。NH-004 的“仅 auto_disabled 才返回状态”与实际候选代码不符：save_all 在携带 models 或显式亲和开关时已返回最终状态，与当前 spec 一致，无关请求不扩展返回字段。

复现日志为 `build_artifacts/affinity-review-repro-backend.txt`（14 个新增后端场景中 12 个失败、2 个已有防护通过）及 `affinity-review-repro-ui.txt`（旧预设别名修改失败）。修复后组合验证在原隔离测试环境执行，未改动真实用户配置、数据目录、后台进程或 editable core；本次不处理上游目录布局回退及本地历史数据恢复。

本轮修复后后端 598 项通过；前端亲和 11 项、Settings 67 项、Jev/浏览器/多模型反馈 72 项、模型刷新 20 项，共 170 项通过。后端日志为 `build_artifacts/affinity-review-backend.txt`，前端日志为 `affinity-review-test-*.txt`。未运行格式化或静态检查。

本轮 `npm run build` 与 `git diff --check` 通过，构建日志为 `build_artifacts/affinity-review-build.txt`。本次无布局样式变更，未另做浏览器截图或真实 Ascend 服务联调；已有构建告警保持原状。


## 2026-11-13 16:16 再次 Rebase upstream/develop

按用户要求从 `116168c2ed745397cca433afad6b2c291f07976e` rebase 到新的 `upstream/develop`：`de27acf2569f6aa83bf1bda99a91db1f8c226e38`。操作前保留本地备份 `backup/agentHint-front-settings-before-rebase-20261113-161620`。仅根索引、bugfix 索引、features 索引有冲突，按 55 份源文档重新生成，保留原作者；不选择任意一侧而丢弃另一侧的索引条目。

业务代码自动合并后复核：亲和设置服务、模型适配与配置写入规则和 rebase 前一致；控制面同时保留上游 TTSE 状态读取和亲和状态。前端 lockfile 未变化。上游本次 utils.py 的路径改动仅增加头像缓存路径，未再次切换会话根目录；本次未迁移用户数据，也未修改 Python 环境或 editable core。

亲和、模型、Front、Gateway、空值恢复及 TTSE 配置回归合计 602 项通过，前端相关回归 170 项通过。额外加入上游新增历史恢复回归时，混合进程总结果为 625 passed / 15 failed：其中部分失败来自 Worker callback registry 已关闭的顺序污染。该文件独立进程复测为 35 passed / 3 failed；三个失败是 warmup 的 KeyboardInterrupt/SystemExit 回滚以及删除历史后游标失效场景，均提前出现 HISTORY_RESTORE_CHANGED。

为区分上游问题与 rebase 冲突，在纯 `de27acf25` 的独立 detached worktree、同一 Python 环境中仅运行上述三项，结果同为 3 failed / 35 deselected。相关历史源文件与上游没有本分支差异。此结果仅证明本地环境可在纯上游复现，不把尚未定位的历史恢复问题夹入亲和补丁，也不宣称全部后端测试通过。日志为 `build_artifacts/affinity-rebase2-backend.txt`、`affinity-rebase2-history-isolated.txt`、`affinity-rebase2-upstream-history.txt`；前端日志为 `affinity-rebase2-test-*.txt`。

本轮 `npm run build` 与 `git diff --check` 通过；构建日志为 `build_artifacts/affinity-rebase2-build.txt`。保留既有告警，未执行格式化或静态检查。

用户补充两条静态检视意见后，手工调整 prepare_model_affinity_update：先跳过非亲和声明及保持原能力的情形，再以协议、厂商身份和模型来源三个条件判断新声明是否合法，单个 if 不超过三个布尔表达式；config.save_all 的 auto_disabled 表达式分行至 120 字符以内。两处均保持原语义，不运行静态检查或格式化工具。按用户要求与 rebase 结果保持为一个提交，并使用指定远端旧 SHA 的 force-with-lease 更新同一分支。

上述两处调整后重新运行不混入历史恢复用例的组合回归：602 passed，退出码 0，日志为 `build_artifacts/affinity-rebase2-final-backend.txt`。前端在最后两处纯 Python 调整前已通过 170 项回归与生产构建，此后未改动前端源码。历史恢复补测的三个纯上游失败仍保留前述限制。
