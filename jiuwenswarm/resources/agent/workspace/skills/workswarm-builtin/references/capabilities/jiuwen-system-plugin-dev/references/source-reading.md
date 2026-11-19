# 源码获取与局部阅读

需要克隆源码或扩大检出范围时读第 1 节；查找平台接口、设计说明或示例时读第 2 节。
源码优先级、固定分支和仓库地址沿用 [开发流程](package-workflow.md#12-获取固定分支的源码)，不因此改变来源。

## 1. 按需稀疏检出

只对新建的阅读缓存执行下列命令，不调整用户指定仓库的稀疏配置。
已有同仓库、同分支的缓存时先检查并复用，缺少目录时按需追加，不重复 clone。

从以下入口选择当前需要的目录；不需要的目录不必同时检出：

| 阅读内容 | 稀疏检出目录，相对于仓库根 |
| --- | --- |
| Core 事件、Matcher 和 Ops | `openjiuwen/core/events` |
| Core manifest、Registrar 和系统插件生命周期 | `openjiuwen/core/extension` |
| Swarm 系统插件和 WebUI | `jiuwenswarm/extensions_v2` |

克隆使用浅克隆、部分克隆和稀疏检出：`--depth 1` 只取当前分支的一层历史，
`--filter=blob:none` 延后获取文件内容，`--no-checkout` 避免先检出整个仓库。
先设置目录，再执行 checkout。只做 sparse-checkout 而省略文件内容过滤，仍可能下载无关代码。

Core 示例，按本次需要保留一个或两个模块目录：

```text
git clone --filter=blob:none --depth 1 --single-branch --no-tags --no-checkout --branch release/v0.1.20.post3 "<Core 仓库 URL>" "<CORE_CACHE>"
git -C "<CORE_CACHE>" sparse-checkout set --cone openjiuwen/core/events openjiuwen/core/extension
git -C "<CORE_CACHE>" checkout release/v0.1.20.post3
```

Swarm 示例：

```text
git clone --filter=blob:none --depth 1 --single-branch --no-tags --no-checkout --branch releases/0.2.9.beta3 "<Swarm 仓库 URL>" "<SWARM_CACHE>"
git -C "<SWARM_CACHE>" sparse-checkout set --cone jiuwenswarm/extensions_v2
git -C "<SWARM_CACHE>" checkout releases/0.2.9.beta3
```

示例中的 URL 按 GitCode、GitHub 的既定顺序选择。每条命令成功后再执行下一条。
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

## 2. 先搜索，再读命中片段

读取适用的 `AGENTS.md` 和本地规则后，围绕本次需求查阅设计文档与代码：

1. **找文件：** 先列相关模块的文件名，按用途、事件、Ops 或槽位缩小目录。
2. **找命中：** 搜索具体事件名、方法名、字段或业务关键词；先看命中文件和行号，再读相邻片段。
3. **补必要内容：** 输入输出、调用条件或副作用仍不清楚时，再展开相关函数、类或规约小节；不预先通读整份设计、源码模块或测试目录。
4. **够用即继续：** 所需事件、匹配字段、Ops 参数和返回规则已确认，就进入实现。记录文件路径和符号，后续需要时从该位置继续，不反复读取整篇。

可使用现有文件搜索、代码搜索工具；有 `rg` 时，例如：

```text
rg --files "<相关模块目录>" -g "*usage*" -g "*.md"
rg -l -F -e "<事件名>" -e "<Ops 方法名>" "<相关模块目录>"
rg -n -C 8 -F "<已确认的符号>" "<命中文件>"
```

命中太多时先缩小目录或关键词，不一次输出全部内容。没有 `rg` 时使用环境已有的搜索工具。
参考文档也按同样方式处理：先定位标题或关键词，再按行号读取相关段落；工具只能整篇返回时，可用终端或支持范围读取的工具查看所需片段。
