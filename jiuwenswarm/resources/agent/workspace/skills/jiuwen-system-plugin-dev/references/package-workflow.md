# 系统插件开发流程

按用户希望的效果确定已有能力，再开发独立的系统插件，使用最终 ZIP 完成加载测试；是否运行安装按用户意愿决定。
本文补充环境核对与系统插件实现，只在当前步骤需要时读取对应小节；源码搜索方法见 [局部阅读](source-reading.md#2-先搜索再读命中片段)，安装阶段再查 [安装与状态核对](package-installation.md)。

## 1. 确认源码和阅读位置

Swarm 和 Core 分别按以下顺序确定源码：用户指定的源码、固定远端分支、远端获取失败后的处理。
只为尚未取得源码的仓库继续下一步。

### 1.1 优先使用用户指定的源码

- **两个仓库都已指定：** 分别从指定目录阅读 Swarm 和 Core，不另行下载替代。
- **只指定一个仓库：** 保留该仓库的阅读位置，另一个仓库按下一节获取。
- **指定仓库：** 核对目录和模块是否存在，不自行切换分支或改动仓库。路径不可读时，向用户确认正确位置。

### 1.2 获取固定分支的源码

用户未指定源码的仓库，直接使用下表中的分支。分支选择不依赖 APP 版本号，不先查找运行包对应的提交或发布 Tag。

| 仓库 | 固定分支 | GitCode | GitHub |
| --- | --- | --- | --- |
| Swarm | `releases/0.2.9.beta3` | [openJiuwen/jiuwenswarm](https://gitcode.com/openJiuwen/jiuwenswarm) | [openJiuwen-ai/jiuwenswarm](https://github.com/openJiuwen-ai/jiuwenswarm) |
| Core | `release/v0.1.20.post3` | [openJiuwen/agent-core](https://gitcode.com/openJiuwen/agent-core) | [openJiuwen-ai/agent-core](https://github.com/openJiuwen-ai/agent-core) |

获取时注意：

- **平台顺序：** 优先从 GitCode 获取指定分支；无法获取时，再从 GitHub 获取同一分支。
- **分支选择：** 获取命令或下载地址必须明确指向表中分支，不使用仓库默认分支，也不换成其他分支或 Tag。
- **稀疏检出：** 需要 clone 时，按 [源码获取说明](source-reading.md#1-按需稀疏检出) 使用浅克隆、文件内容过滤和 sparse-checkout，只取本次所需模块；后续按需补充目录。
- **保存位置：** 使用现有终端或下载工具，把源码保存到工作区的独立目录。源码只用于阅读，不安装其中的依赖，不替换 APP 内的运行代码。
- **适用范围：** 固定分支是本 Skill 的默认阅读来源，不据此宣称它与当前运行包来自同一次提交；实际能力仍以目标宿主为准。

### 1.3 远端获取失败时

只有某个仓库在 GitCode、GitHub 都无法获取指定分支的源码时，才按当前运行方式处理：

- **Wheel：** 可以读取当前环境中已安装的代码。使用目标 Swarm 的 Python 解释器定位 Swarm 或 Core 的实际模块目录，不使用其他 Python 环境，也不安装任意最新版代替。没有可读代码时，再向用户确认源码位置。
- **EXE、DMG：** 调用当前环境提供的提问工具，说明哪个仓库获取失败，请用户提供本地源码目录或可访问的源码下载地址。等待用户答复后继续，不在安装目录或 `_internal` 中寻找源码，也不搜索二进制内容。
- **运行方式不明确：** 向用户确认当前环境和源码位置，不自行猜测。

尚未取得所需源码时，不根据示例或猜测补写平台接口。

### 1.4 确认阅读目录

用户指定或从固定分支获取的源码仓根分别记作 `CORE_ROOT` 和 `SWARM_ROOT`。
读取适用的仓库根、模块规则后，从以下入口按文件名和关键词搜索，先读命中片段，必要时再展开相关函数、类或规约小节。表中目录按当前问题选择，不预先通读文档和源码：

| 阅读目的 | 确定位置 |
| --- | --- |
| Core 事件、Matcher、上下文和 Ops | `<CORE_ROOT>/openjiuwen/core/events/` 下的规则、`docs/` 和公开实现 |
| manifest、Registrar、加载和生命周期 | `<CORE_ROOT>/openjiuwen/core/extension/` 下的规则、`docs/` 和公开实现 |
| Swarm 事件、Ops 和 WebUI | `<SWARM_ROOT>/jiuwenswarm/extensions_v2/` 下的规则、`docs/` 和相关业务实现 |
| 可用参考系统插件 | 从上述模块的索引或测试进入，确认示例适用的实际宿主版本 |

查阅时注意：

- **Swarm 路径：** 仓库内部还有同名 `jiuwenswarm/` 包目录，不能省略；从名为 `jiuwenswarm` 的仓库父目录看，路径为 `jiuwenswarm/jiuwenswarm/extensions_v2/`。
- **模块根与仓库根：** `openjiuwen.core` 的模块目录对应 `openjiuwen/core/`，`jiuwenswarm` 的模块目录对应 Python 包；它们不一定是源码仓根。Wheel 回退时，按实际模块位置阅读。
- **资料缺失：** 找不到附带文档时，结合公开接口、源码和测试判断；缺少关键契约时说明具体缺口。
- **其他执行域：** 系统插件涉及 Gateway 等宿主能力时，沿对应模块规则核对当前实现和管理入口，不套用 AgentServer 专属契约。

### 1.5 复用宿主的执行环境

阅读源码和执行脚本分别选择位置。第 1.1～1.3 节决定从哪里读代码；测试、调试和打包都使用当前宿主已有的环境。
下载仓库不等于需要安装仓库，不对下载的源码执行 `uv sync`、`pip install -e` 或新建 `.venv`。

| 运行方式 | 使用的执行入口 |
| --- | --- |
| 源码运行 | 启动当前 Swarm 的现有 Python；通常位于该运行仓库已有的 `.venv/Scripts/python.exe`（Windows）或 `.venv/bin/python`（macOS、Linux） |
| Wheel | 安装并启动当前 Swarm 的那个 Python 环境；Wheel 本身不提供另一套解释器 |
| EXE | `JIUWENSWARM_EXECUTABLE` 指向的后端程序。当前桌面包位于安装目录的 `resources/backend/workswarm.exe`，Python 和依赖随包提供 |
| DMG | 同样使用 `JIUWENSWARM_EXECUTABLE`。当前 Electron 包的后端位于 `.app/Contents/Resources/backend/workswarm`，Python 和依赖位于 APP 内 |

选择和使用时注意：

- **源码、Wheel：** 从已有启动环境、`VIRTUAL_ENV` 或已提供的解释器路径确认入口。用户指定或下载的源码目录不一定是运行环境，不使用其中尚未准备的解释器。
- **桌面包：** 直接使用环境变量提供的后端路径，不在安装目录中寻找 `python.exe`，也不另找系统 Python。安装目录布局可能随发行版本变化，以实际提供的路径为准。
- **入口含义：** 下文的 `<PYTHON_ENTRY>` 表示选中的 Python 或后端可执行文件的绝对路径，不要求用户新增环境变量。当前桌面后端支持执行 `.py` 脚本，但不能当作支持全部 Python 命令行参数的通用解释器。
- **目录和路径：** 从系统插件自己的目录执行，脚本、测试和产物使用绝对路径。下载的 Swarm、Core 源码不加入 `PYTHONPATH`，避免覆盖运行环境中实际使用的模块。
- **Windows 临时目录：** 安装测试会生成较深的目录，临时路径宜简短；遇到路径长度限制时，可给测试脚本传 `--basetemp "<较短的独立临时目录>"`。
- **环境缺失：** 入口不可用或缺少必要模块时，说明具体缺口，并向用户确认可用的已有环境；不自动安装依赖或重建完整环境。

PowerShell 调用示例：

```powershell
& "<PYTHON_ENTRY>" "<SKILL_ROOT>/scripts/run_tests.py" "<系统插件测试目录或文件>"
& "<PYTHON_ENTRY>" "<SKILL_ROOT>/scripts/build_package.py" "<系统插件源码目录>" "<输出 ZIP 路径>"
```

Bash 调用时去掉开头的 `&`，其余路径仍保留引号；EXE、DMG 的执行入口直接使用现有的 `JIUWENSWARM_EXECUTABLE`。

[测试脚本](../scripts/run_tests.py) 使用所选环境中的 pytest 和 pytest-asyncio，支持测试目录或单个文件，不下载依赖。
测试失败、没有发现测试或缺少测试工具时，保留实际退出码，不能只因脚本已执行就判断验证通过。
桌面包中的测试若要启动子进程，使用当前 `sys.executable` 执行脚本；标准输入不需要时传 `stdin=subprocess.DEVNULL`，不要照搬普通 Python 的 `-B` 等启动参数。

## 2. 把诉求对应到已有事件

围绕以下问题搜索，只读相关命中片段和必要的接口定义；信息已足够时就继续开发，不为了解整套机制而展开其他模块：

1. 哪个事件允许修改目标行为，目标宿主是否已经登记并在真实业务中触发？
2. 一次事件处理什么对象，Matcher 支持哪些字段？
3. Handler 在 next 前处理、next 后加工，还是直接短路？各条路径返回什么？
4. 是否需要 Ops，其完整方法名、参数、上下文和返回值是什么？
5. 用户从哪项已有操作看到变化，界面需要哪个已支持的槽位或推送？
6. 系统插件激活后怎样验证，是否需要重新进入会话或让现有页面重新挂载？

判断能力时注意：

- **真实入口：** 不从普通 Python 函数名猜事件，也不从 Ops 名称推断会触发同名事件。
- **多个事件：** 需要多个已有事件时说明各自分工，不将一次业务动作重复执行。
- **平台缺口：** 没有合适的扩展能力时先报告，不通过修改平台或新增触发点补齐。

## 3. 声明与实现独立的系统插件

当前示例采用以下布局，实际字段仍以目标 Core 解析器为准：

```text
package-root/
├── manifest.json
├── README.md
└── package_module/
    ├── __init__.py
    └── extension.py
```

系统插件声明按以下要求核对：

- **归档布局：** ZIP 根目录直接包含唯一的 `manifest.json`，`extensions[].module` 是系统插件内真实的 `.py` 路径。
- **基础字段：** `schemaVersion` 固定为 `1`，`package_type` 固定为 `extension`，`package.domain` 必须为 `agentserver` 或 `gateway`；运行域决定由哪个宿主安装和加载。
- **入口路径：** 当前入口不能是 `__init__.py`，去掉 `.py` 后的路径段不能含点，例如 `greeting/extension.py`。
- **系统插件身份：** 当前系统插件 ID 长度 1～128，首位为英文字母或数字，其余可含英文字母、数字、点、下划线和连字符，不含空格或连续两个点。
- **展示信息：** 使用顶层 `display` 声明双语 `name`、`description`、可选 `tags`、`details`、`avatar` 和 `events`；不要继续使用旧的 `package.displayName`、`package.description` 写法。
- **版本与依赖：** `package.version` 为字符串。`requires.host` 比较宿主版本，`requires.packages` 声明其他系统插件依赖，不是 Core 分发版本或 pip 依赖清单。
- **加载入口：** 当前后端示例使用 `runtime: backend`、`create_extension()` 和 `register(registrar)`；其他执行域按其实际支持范围实现。
- **Handler 登记：** 使用真实 Registrar 接口，默认 USER，系统插件身份由平台注入。
- **UI 声明：** 只使用目标宿主支持的槽位、组件和 props；Core manifest 校验不代表 UI 内容也已全部通过校验。

实现时注意：

- **依赖范围：** 只使用必要依赖，新库说明运行要求，不将虚拟环境、平台源码或凭据放入 ZIP。
- **状态归属：** 系统插件对象的状态按会话或业务实例隔离；重载会创建新对象，持久状态和恢复方式按实际业务说明。
- **默认动作：** 原动作与 CORE 按契约执行一次，错误、取消和清理遵循既有机制。

## 4. 验证最终文件

先测试真实加载、可信系统插件身份、Handler 发布和事件结果，再生成最终 ZIP。
测试可模拟网络或存储，但不能绕过本次要验证的系统插件加载和处理链。

- **最终 ZIP：** 使用正式打包脚本生成交付文件，将同一文件安装到临时宿主中验证；测试后保持文件不变，按用户意愿交付或安装。
- **带界面的系统插件：** 验证 contribution、上行事件、Handler、Ops 渲染和下行数据；真实页面显示单独验证。
- **重载：** 修改临时安装副本，再通过公开重载入口核对新结果和失败行为。
- **卸载：** 核对 Handler、界面和资源按契约清理。
- **异常场景：** 按实际业务覆盖主要失败、取消、会话隔离和重复调用，不假定框架自动重试。

## 5. 安装和交付

完成最终 ZIP 验证后，先按 [主流程第 5.1 节](../SKILL.md#51-确认是否安装) 判断是否安装；不安装时交付开发结果并结束，不进入界面效果确认。
需要安装时，再按 [安装与状态核对](package-installation.md) 通过 `bash` 调用宿主 CLI：

1. 确认当前运行实例、命令入口和宿主可读取的 ZIP 路径；连接与授权由宿主提供。
2. 安装一次，并核对原始 Report 与当前激活状态。
3. 执行原有业务操作，确认实际效果；带界面的系统插件按其生命周期验证页面。
4. 更新 README 和交付说明，分别记录源码与 ZIP、安装目录、测试结果和未完成项。

来源目录与安装副本分别说明：

- **维护源码：** 在源码目录继续开发，修改后重新打包和验证。
- **热重载：** 读取安装目录中的副本，修改原 ZIP 或来源目录不会自动更新它。先把已测试的修改同步到安装命令返回的安装目录，再 reload 对应系统插件；来源目录、最终 ZIP 与安装副本保持同一版本。
- **远程部署：** ZIP 必须位于服务可读取的位置，CLI 不上传文件。
- **验证记录：** 隔离加载、CLI 通信、运行安装和真实业务验收分别记录，不把其中一步扩大解释为全部完成。
