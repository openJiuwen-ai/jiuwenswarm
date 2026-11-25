# 源码获取与局部阅读

需要克隆源码或扩大检出范围时读第 1 节；查找平台接口、设计说明或示例时读第 2 节。
源码优先级、固定分支和仓库地址沿用 [开发流程](package-workflow.md#12-获取固定分支的源码)，不因此改变来源。

## 1. 按域选择并复用稀疏缓存

先按系统插件运行域选择仓库：两域都需要 Core；`agentserver` 域再选择 Swarm，`gateway` 域再选择 Gateway。
只有安装入口或跨域协作确实涉及另一仓库时才补充，不默认同时准备三个仓库。

只对新建的阅读缓存执行下列命令，不调整用户指定仓库的稀疏配置。同一机器上，同一仓库、同一固定分支只保留一份阅读缓存；
开始任务时先检查并复用，缺少目录时用一次 `sparse-checkout add` 集中追加，不重复 clone。缓存记录远端地址、分支和当前提交；
只有用户要求最新代码、缓存损坏，或源码与目标接口明显不一致时，才对该缓存做一次远端核对和浅更新，不在阅读过程中反复 fetch。

从以下入口选择当前需要的目录；不需要的目录不必同时检出：

| 阅读内容 | 稀疏检出目录，相对于仓库根 |
| --- | --- |
| Core 事件、Matcher 和 Ops | `openjiuwen/core/events` |
| Core manifest、Registrar 和系统插件生命周期 | `openjiuwen/core/extension` |
| Swarm 的 AgentServer 事件、Ops 和管理适配 | `jiuwenswarm/extensions_v2` |
| Gateway 系统插件、事件、Ops 和 WebUI | `agent_gateway/extensions_v2` |

克隆使用浅克隆、部分克隆和稀疏检出：`--depth 1` 只取当前分支的一层历史，
`--filter=blob:none` 延后获取文件内容，`--no-checkout` 避免先检出整个仓库。
先设置目录，再执行 checkout。只做 sparse-checkout 而省略文件内容过滤，仍可能下载无关代码。

Core 示例，按本次需要保留一个或两个模块目录：

```text
git clone --filter=blob:none --depth 1 --single-branch --no-tags --no-checkout --branch develop "<Core 仓库 URL>" "<CORE_CACHE>"
git -C "<CORE_CACHE>" sparse-checkout set --cone openjiuwen/core/events openjiuwen/core/extension
git -C "<CORE_CACHE>" checkout develop
```

Swarm 示例：

```text
git clone --filter=blob:none --depth 1 --single-branch --no-tags --no-checkout --branch develop "<Swarm 仓库 URL>" "<SWARM_CACHE>"
git -C "<SWARM_CACHE>" sparse-checkout set --cone jiuwenswarm/extensions_v2
git -C "<SWARM_CACHE>" checkout develop
```

Gateway 示例：

```text
git clone --filter=blob:none --depth 1 --single-branch --no-tags --no-checkout --branch develop "https://gitcode.com/openJiuwen/agent-gateway.git" "<GATEWAY_CACHE>"
git -C "<GATEWAY_CACHE>" sparse-checkout set --cone agent_gateway/extensions_v2
git -C "<GATEWAY_CACHE>" checkout develop
```

Swarm 和 Core 的 URL 按 GitCode、GitHub 的既定顺序选择；Gateway 使用上面的独立 GitCode 仓库。每条命令成功后再执行下一条。
`--cone` 还会保留仓库根和父目录的直接文件，便于读取 `AGENTS.md` 和项目配置，不会展开其他子目录。

遇到引用了尚未检出的文件时，先查目录树，再只补充实际需要的目录：

```text
git -C "<缓存目录>" ls-tree -r --name-only HEAD -- "<候选父目录>"
git -C "<缓存目录>" sparse-checkout add "<确认需要的目录>"
```

- **范围控制：** 下游业务、公开导出、测试或其他规则确有需要时再追加，不将范围设为整个仓库。
- **查找失败：** 未检出的文件不会出现在本地搜索结果中；先核对目录树，不能直接判断平台没有该能力。
- **获取受限：** 服务器不支持文件内容过滤时，尝试另一平台的同一分支，或按文件获取所需内容，不自动退回完整 clone。
- **搜索范围：** 关键词搜索限定在已检出的相关目录；不对整个 `HEAD` 执行内容搜索，避免部分克隆为此补取全仓文件。

## 2. 集中搜索，再批量读取必要上下文

读取适用的 `AGENTS.md` 和本地规则后，围绕本次需求查阅设计文档与代码：

1. **找文件：** 先列相关模块的文件名，按用途、事件、Ops 或槽位缩小目录。
2. **集中找命中：** 把已知事件名、方法名、字段和业务关键词合并到一轮搜索，取得命中文件和行号。
3. **列阅读清单：** 根据调用关系列出实现前必须确认的定义、登记、触发点、Ops 契约和直接相关测试，合并同一文件的相邻范围。
4. **批量读取：** 在一次工具调用中读取清单里的必要范围；内容过长时按文件分成少量有界批次，不按一个符号一次命令来回读取。
5. **补必要内容：** 输入输出、调用条件或副作用仍不清楚时，再对新发现的必要依赖补一批；不预先通读整份设计、源码模块或测试目录。
6. **够用即继续：** 所需事件、匹配字段、Ops 参数和返回规则已确认，就进入实现。记录文件路径和符号，后续从该位置继续，不重复搜索和读取。

可使用现有文件搜索、代码搜索工具；有 `rg` 时，例如：

```text
rg --files "<相关模块目录>" -g "*usage*" -g "*.md"
rg -l -F -e "<事件名>" -e "<Ops 方法名>" "<相关模块目录>"
rg -n -C 8 -F "<已确认的符号>" "<命中文件>"
```

命中太多时先缩小目录或关键词，不一次输出全部内容。没有 `rg` 时使用环境已有的搜索工具。
参考文档也按同样方式处理：先定位标题或关键词，再按行号读取相关段落；工具只能整篇返回时，可用终端或支持范围读取的工具查看所需片段。
