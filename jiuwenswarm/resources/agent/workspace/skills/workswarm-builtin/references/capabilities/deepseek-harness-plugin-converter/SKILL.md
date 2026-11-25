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

## 按需读取资料

先按本文推进，需要具体细节时再读对应资料，不要一次加载全部参考文件。

| 当前需要 | 按需查阅 |
| --- | --- |
| 查最新官方资料、定位源码、检查转换所需结构 | [DeepSeek Harness 源码定位与结构检查](references/deepseek-harness-plugin.md) |
| 映射事件、Ops、UI、开发和验证系统插件 | [转换与开发流程](references/conversion-workflow.md) |
| 读取三个仓库、编写系统插件及安排测试 | [系统插件开发说明](references/system-plugin-development.md) |
| 参考可加载的 manifest、入口和真实链路测试 | [系统插件示例](assets/package_example/) |
| 完成实现后一次性测试、打包和校验最终 ZIP | [最终验证脚本](scripts/finalize_package.py) |

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

从目标 Swarm、Core 和 Gateway 的实际源码中搜索对应事件、Ops、UI 槽位和管理入口，按 [转换与开发流程](references/conversion-workflow.md) 建立映射表。

- **能力齐全：** 说明采用的事件、Ops、Handler 行为、UI 槽位和验收入口，继续开发。
- **缺少必要能力：** 如果缺口会让核心功能无法运行、无法被真实业务触发，或只能做成与原插件不同的演示，立即停止开发，向用户说明缺少的能力、代码位置和影响。
- **只有非关键差异：** 如果核心功能可以完整运行，只是动画、布局细节、性能优化或次要体验不能完全一致，使用现有能力完成最佳实现，不需要单独向用户强调这一类差异。

不得新造事件名、伪造 Ops、增加演示按钮或修改平台源码来绕过缺口。

## 4. 开发 JiuwenSwarm 系统插件

### 4.1 源码和版本

优先使用用户指定的 `jiuwenswarm`、`agent-core` 和 `agent-gateway` 源码。缺少对应仓库时，按 [转换与开发流程](references/conversion-workflow.md#1-确认目标源码) 获取 `develop` 分支或读取当前运行环境。具体仓库地址、稀疏检出目录和批量阅读方式见 [系统插件开发说明](references/system-plugin-development.md#1-读取目标平台源码)。

- 新转换的系统插件使用清晰稳定的 `package.id`，未指定版本时从 `0.0.1` 开始。
- 修改已经转换的系统插件，或一轮开发和验收结束后又提出修改时，复制当前源码形成新版本目录；保持 `package.id`、`domain` 和 `display.name` 不变，按变更类型递增三段数字版本。
- 首次安装后的界面确认循环中提出的调整仍属于当前版本调试，不复制目录、不递增版本；修改安装副本并 reload。
- ZIP 使用 `<package_id>-<version>.zip`，manifest、README、源码目录和交付说明中的版本保持一致。

### 4.2 实现要求

在独立目录中开发，不放进平台源码模块。系统插件至少包含：

- 根目录 `manifest.json`，声明真实的 `package.id`、版本、`agentserver` 或 `gateway` 域、入口和依赖。
- `create_extension()` 工厂和 `register(registrar)`；Handler 使用公开 Registrar、事件和 Ops 接口。
- 与原插件核心行为对应的状态、错误、取消、资源清理和会话隔离。
- 需要界面时使用目标版本已有的 UI contribution 和槽位，保持界面美观，并核对目标、会话和活跃实例。
- README、测试和必要资源；不把 DeepSeek Harness 运行时、Node 环境、虚拟环境或凭据打进 ZIP。

开始实现前，在系统插件源码根目录写入简短的 `CAPABILITY_CHOICES.md`，记录原插件来源和版本、目标 domain、采用的事件、Ops、UI 槽位、真实触发入口及验收方式。实现细节按 [系统插件开发说明](references/system-plugin-development.md#2-开发独立的系统插件) 核对；需要具体结构时读取本 Skill 自带的 [系统插件示例](assets/package_example/)，不依赖其他 Skill 的文件。

## 5. 测试、打包和安装

不能只测试转换后的内部函数。使用目标 Swarm 现有执行环境，在隔离的临时安装目录中验证：

- manifest 和入口能被真实加载，Handler 能发布并按预期顺序执行。
- 触发、未触发、成功、主要失败、取消、重复调用和会话隔离。
- UI contribution、上行操作、下行渲染和重载；真实页面效果另行由用户确认。
- 最终 ZIP 根目录直接包含唯一 `manifest.json`，并使用同一份 ZIP 完成最终加载验证。

实现完成后统一运行本 Skill 的最终验证脚本，不在每次小改动后反复启动测试。测试范围只包含本次系统插件自己的测试，不运行 Swarm、Core 或 Gateway 的完整测试集。下文的 `<PYTHON_ENTRY>` 使用当前 Swarm 已有的 Python 或桌面后端执行入口：

```text
"<PYTHON_ENTRY>" "<SKILL_ROOT>/scripts/finalize_package.py" "<系统插件源码目录>" "<输出 ZIP 路径>" "<系统插件测试目录或文件>"
```

脚本先生成临时 ZIP，把该 ZIP 路径通过 `JIUWEN_SYSTEM_PLUGIN_ZIP` 交给测试；测试必须从这个 ZIP 安装到隔离宿主并验证真实加载和行为。全部通过后，同一文件才成为最终 ZIP。单独定位打包或测试问题时，再使用 `scripts/build_package.py` 或 `scripts/run_tests.py`。

用户明确要求安装时，通过现有终端调用：

```text
jiuwenswarm extension install "<最终 ZIP 路径>" --json
jiuwenswarm extension query "<package_id>" --json
```

Gateway 域的后续查询、reload 和 uninstall 增加 `--domain gateway`。EXE、DMG 使用环境提供的 `JIUWENSWARM_EXECUTABLE`，不另找系统 Python。

用户明确不安装时，交付源码、README、测试结果和 ZIP。用户没有说明时，完成开发和验证后使用当前提问工具询问是否安装，等待用户答复，不默认安装。

## 6. 验证实际效果

安装并确认系统插件已激活后，使用原有业务入口验证核心行为。涉及页面效果时，技术验证完成后使用确认卡片询问用户：“你现在看到的实际效果是否符合预期？”

- 用户确认符合预期后，结束验收并交付。
- 用户在本轮确认循环中提出修改意见时，保持 ID、domain 和版本不变，修改系统插件源码和安装副本，补测试并 reload，再次请用户确认。
- 用户已经确认或本轮交付结束后再次要求修改，按第 4.1 节复制源码并开发新版本，不覆盖旧版本。

最终说明包含原插件来源和版本、转换映射、系统插件 ID 和版本、源码与 ZIP、测试结果、安装和激活状态、实际业务验证，以及仍存在的必要阻塞。
