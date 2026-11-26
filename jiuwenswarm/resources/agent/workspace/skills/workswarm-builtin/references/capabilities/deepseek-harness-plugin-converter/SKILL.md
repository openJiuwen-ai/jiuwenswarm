---
name: deepseek-harness-plugin-converter
description: >-
  将 DeepSeek Harness 插件转换为 JiuwenSwarm 系统插件。优先使用用户提供的源码；没有源码时，从最新官方资料、
  插件元数据和搜索结果定位目标版本，只获取目标插件及直接依赖，理解真实行为后映射到现有事件、Ops 与 UI 槽位，
  完成开发、测试、打包，并按用户意愿安装和验证。不修改平台源码，也不虚构宿主能力。
---

# DeepSeek Harness 插件转换

先找到用户所指插件的源码和确定版本，再分析它在 DeepSeek Harness 中怎样加载、触发和呈现。
转换的是插件能力和用户效果，不是逐行翻译 TypeScript；实现必须使用 JiuwenSwarm 已有的系统插件接口。

用户要求修改已经转换好的系统插件时，先处理现有实现：

- **定位文件：** 按 [已有系统插件定位流程](../jiuwen-system-plugin-dev/SKILL.md#1-确认目标环境和目录) 查询当前安装状态，直接使用返回的安装副本；用户明确提供源码时优先使用。不搜索历史工作区或记忆来确定源码和版本。
- **缺少记录：** 不以历史对话或记忆记录为前提；文件也找不到时，说明已查的位置，请用户提供源码、ZIP 或准确 ID。
- **原插件源码：** 只有本次修改需要核对原插件行为时，才重新读取相关 DSH 源码。
- **修改流程：** 按 [已有系统插件修改流程](../jiuwen-system-plugin-dev/SKILL.md#1-确认目标环境和目录) 操作；已有实现和能力映射足够时，不重新做整套转换分析。

## 按需读取资料

先按本文推进，需要具体细节时再读对应资料，不要一次加载全部参考文件。

| 当前需要 | 按需查阅 |
| --- | --- |
| 查最新官方资料、定位源码、检查转换所需结构 | [DeepSeek Harness 源码定位与结构检查](references/deepseek-harness-plugin.md) |
| 映射原插件行为与目标能力 | [转换与开发流程](references/conversion-workflow.md) |
| 选择 domain、开发、测试、打包和安装 | [系统插件开发与安装](../jiuwen-system-plugin-dev/SKILL.md)，只读取当前步骤 |
| 参考相近场景的关键写法 | [系统插件示例索引](../jiuwen-system-plugin-dev/references/example.md) |
| 嵌入网页、来源隔离和权限 | [前端开发说明](../jiuwen-system-plugin-dev/references/frontend-development.md) |
| 同时需要两个 domain | [组合包开发](../jiuwen-system-plugin-dev/references/bundle-development.md) |

## 职责边界

- **先有源码：** 没有找到插件源码或可核对的发布包时，不根据名称和宣传文案猜实现。请用户提供源码地址或文件后再继续。
- **转换目标：** 保留原插件的核心能力、触发条件和用户可见效果；平台名称、配置格式和实现语言按 JiuwenSwarm 实际接口调整。
- **只用已有能力：** 只复用目标版本已经实现的事件、Ops、UI 槽位和 CLI，不修改 Swarm、Core、Gateway 或 DeepSeek Harness 源码来补能力。
- **来源与许可：** 核对源码版本、仓库、许可证和依赖。许可证不允许复制，或没有可确认的复制授权时，只能根据公开行为独立实现，不复制受限代码和素材。
- **交付标准：** 默认交付源码、README、测试和最终 ZIP；是否安装由用户的明确意愿决定。安装后还要核对激活状态和实际效果。

## 1. 定位 DeepSeek Harness 插件源码

### 1.1 确认插件身份

先从用户输入中提取插件名称、npm 包名、仓库地址、版本、提交或插件索引地址。
用户给了源码目录、仓库或准确发布地址时，直接使用该来源并核对身份，不再搜索平台，也不下载完整 DSH 仓库。

用户只给插件名称时，按以下顺序搜索：

1. 打开 [DeepSeek Harness 官网](https://deepseek.com/harness) 指向的最新开发文档和插件入口；根据目标 DSH 版本核对相应发布说明、Tag 或 commit。
2. 查看当前 DSH 插件页面、[官方插件索引文档](https://deepseek-harness.github.io/deepseek-harness/en/guide/plugin-market)及用户提供或当前环境已配置的索引，从条目中提取 npm 包、Git 仓库、commit 和子目录。索引只用于定位，不能代替源码核对。
3. 仍未找到时，必须使用网络搜索工具搜索插件名称、别名、npm 包名、Git 仓库和 `dsh-plugin`；不能只凭第一次搜索无结果就结束。
4. 对多个同名候选，比较作者、README、包名、界面截图、版本和源码行为；不能确定时，把候选和差异告诉用户，请用户确认目标。

[官方开发文档](https://deepseek-harness.github.io/deepseek-harness/en/develop/basic/)和[官方仓库](https://github.com/deepseek-ai/deepseek-harness)用于理解目标版本的插件接口。官网链接的 GitHub `dsh-plugin` topic、第三方市场和 awesome 列表都只是发现入口，不得称为官方插件平台；最终仍要回到插件自己的 npm 或 Git 源码核对。

找到源码后记录：

- 插件名称、包名、仓库和源码子目录。
- 分析所用版本、Tag 或 commit；有发布包时核对发布包与源码版本。
- 许可证、外部服务、运行依赖和需要的凭据。
- 源码是否完整，是否包含 `package.json`、bundle patch、Host、Client、配置、测试和资源文件。

### 1.2 只获取目标源码

按 [DeepSeek Harness 源码定位与结构检查](references/deepseek-harness-plugin.md)执行：

- 普通第三方插件不 clone 完整 DSH 仓库。先读取发布元数据、manifest、目标目录树和入口；只有实现确实引用的直接依赖才继续获取。
- Git 源按“规范化仓库地址 + commit + 插件子目录”缓存，npm 源按“包名 + 精确版本 + registry/integrity”缓存；命中缓存后直接复用，不重复 clone 或下载。
- 第一次读取时批量取得 manifest、README/许可证、目录树、入口和组合配置，先形成完整轮廓，再按入口引用补充源码，避免用许多命令逐个试读文件。
- 只有目标插件属于 DSH 内置源码，或为确认某个当前接口必须读 DSH 实现时，才按目标 Tag/commit 稀疏获取相应目录；不下载与转换无关的工作区。

## 2. 理解原插件

按 [DeepSeek Harness 源码定位与结构检查](references/deepseek-harness-plugin.md) 核对目标版本的结构，再从真实入口还原调用链。至少回答：

| 项目 | 需要确认的内容 |
| --- | --- |
| 加载 | 插件入口、bundle、patch 顺序、依赖和启停方式 |
| 触发 | 哪个事件、工具调用、命令、页面操作或生命周期触发 |
| 输入输出 | 输入字段、返回结果、错误、取消和继续执行规则 |
| 状态 | 会话状态、持久化、缓存、定时任务和清理方式 |
| 权限 | 文件、进程、网络、凭据、审批和沙箱要求 |
| 界面 | Host 与 Client 分工、页面、槽位、组件、上行操作和下行数据 |
| 外部依赖 | npm 包、MCP、服务端、模型或第三方 API |

先写出简短的行为说明和数据流，再开始映射。不要把 Cordis 服务名、Context API 或前端 slot 名直接当作 JiuwenSwarm 已有能力。

## 3. 判断能否转换

先从 [示例索引](../jiuwen-system-plugin-dev/references/example.md) 选择相近场景的参考片段，再按 [转换与开发流程](references/conversion-workflow.md) 建立映射表；只搜索当前映射尚未确认的事件、Ops、槽位和管理入口。

判断能力缺口前，按 [能力缺口核对](../jiuwen-system-plugin-dev/references/source-reading.md#3-报告能力缺失前的核对) 检查相关域、真实名称、已检出目录和直接测试；涉及网页时，再核对来源隔离和权限。

- **能力齐全：** 说明采用的事件、Ops、Handler 行为、UI 槽位和验收入口，继续开发。
- **缺少必要能力：** 如果确认缺口会让核心功能无法运行、无法被真实业务触发，或只能做成与原插件不同的演示，立即停止开发，向用户说明缺少的能力、代码位置和影响。
- **只有非关键差异：** 如果核心功能可以完整运行，只是动画、布局细节、性能优化或次要体验不能完全一致，使用现有能力完成最佳实现，不需要单独向用户强调这一类差异。

不得新造事件名、伪造 Ops、增加演示按钮或修改平台源码来绕过缺口。

## 4. 开发 JiuwenSwarm 系统插件

完成来源分析和能力映射后，按内置的 [系统插件开发 skill](../jiuwen-system-plugin-dev/SKILL.md) 继续开发。
源码获取、现有执行环境、版本管理、Handler、测试、打包和安装统一沿用该流程，不再维护另一套脚本或示例。
先读取主流程，再按本次需要打开对应资料，不一次加载全部参考文件。

转换时额外保留：

- **来源与映射：** 在 `CAPABILITY_CHOICES.md` 中记录原插件来源、版本、许可证、核心行为及目标事件、Ops、槽位和验收入口。
- **运行域：** 按实际事件与 Ops 归属选择 `agentserver` 或 `gateway`，不能因为原插件带页面就默认选 Gateway。两个域都需要时，交付一个组合包；成员各自保留 domain，不把根 domain 写成数组或随意选择一个。
- **前端效果：** 使用 [前端开发说明](../jiuwen-system-plugin-dev/references/frontend-development.md) 核对 Iframe、独立来源、permissions 和内层 iframe；不沿用原平台的原生 webview 限制结论。
- **交付内容：** 只打入本系统插件需要的实现、说明、测试和资源，不打入 DSH 运行时、Node 环境、虚拟环境或凭据。

## 5. 测试、打包和安装

使用 [系统插件开发流程第 4 节](../jiuwen-system-plugin-dev/SKILL.md#4-测试并生成最终-zip) 的执行环境和最终验证脚本。
命令中的 `SKILL_ROOT` 指向 `jiuwen-system-plugin-dev` 的实际目录；读取或执行它的脚本，不复制到本 Skill 中。

测试从 `JIUWEN_SYSTEM_PLUGIN_ZIP` 加载同一份最终 ZIP，验证原插件核心行为映射、真实 Handler 链、主要失败和隔离；组合包还要覆盖两个域及协作效果。
通过后按 [第 5 节](../jiuwen-system-plugin-dev/SKILL.md#5-确认是否安装并核对运行状态) 判断用户安装意愿、选择支持的入口并核对状态。
组合包的当前 CLI 限制以 [安装说明](../jiuwen-system-plugin-dev/references/bundle-development.md#3-安装与验收) 为准，不拆开安装，也不宣称尚未完成的安装已经成功。

## 6. 验证实际效果

沿用 [业务效果与交付流程](../jiuwen-system-plugin-dev/SKILL.md#6-验证业务效果并交付)。涉及页面时，完成当前激活状态和页面更新的技术核对后，显示用户确认卡片。

- **效果确认中的调整：** 用户尚未确认本轮效果，继续按意见修改时，版本不变，更新本轮安装副本并 reload。
- **交付后的再次修改：** 无论在同一会话还是新会话，都复制源码开发新版本，按共享流程安装并核对实际生效版本，不把新版本写入旧版本安装目录。
- **最终说明：** 补充原插件来源和版本、转换映射，以及核心行为是否已经验证。用户维护方式沿用 [文档写法](../jiuwen-system-plugin-dev/references/package-writing.md#2-写清安装和验证步骤)：本次已安装成功时不输出 `/extension install`，未安装时提供该命令。
- **交付收尾：** 发送最终 ZIP 并简要总结一次；文件工具不支持附带文字时，分别发送文件和这一次说明。除用户明确要求外，不为收尾检索或更新记忆，也不重复总结。
