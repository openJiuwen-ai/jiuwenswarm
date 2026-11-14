---
id: B_20261108_1042_auto-memory-readonly-bypass
title: 自动记忆抽取子代理只读命令判定可被串联命令绕过
type: B
author: yanhai
designs: []
design_note: 对现有只读命令判定的局部替换，未引入新方案文档，故无关联 D。
modules:
  - jiuwenswarm/agents/harness/common/auto_memory
files:
  - jiuwenswarm/agents/harness/common/auto_memory/tool_restriction_rail.py
  - tests/unit_tests/agentserver/memory/test_auto_memory_tool_restriction_rail.py
author_email: yanhai010@gmail.com
---

# 自动记忆抽取子代理只读命令判定可被串联命令绕过

## 现象与根因

`AutoMemoryToolRestrictionRail._is_read_only_bash_command`（`jiuwenswarm/agents/harness/common/auto_memory/tool_restriction_rail.py`）用**子串包含**判断 bash 命令是否只读：只要命令行里出现 `READ_ONLY_BASH_COMMANDS` 中任一字符串（或 `--status/--list/--show/--print/--read` 之一）就放行。于是 `ls && touch x`、`ls\ntouch x`、`ls |& touch x` 这类命令被判为只读，随后整条命令交给 shell 执行。

基线提交为 `3c008abb662c70facf9be298313e6b99a6fef88a`；openjiuwen 为 `pyproject.toml` 锁定的 `f3f8f576164cc405c046e43079c8ad00ee17f0c4`（`openjiuwen-0.1.20`）。

## 复现

用 `/tmp/b8_repro.py` 真实调用 rail（`AgentCallbackContext` + `ToolCallInputs(tool_name="bash")`），判定以 `ctx.extra["_skip_tool"]` 为准。修复前：

| 命令 | 判定 |
|---|---|
| `git status` | 放行（对照，正确） |
| `ls && touch /tmp/b8-marker` | **放行** |
| `ls\ntouch /tmp/b8-marker` | **放行** |
| `ls \|& touch /tmp/b8-marker` | **放行** |

再用真实 `BashTool`（`SysOperationCard(mode=LOCAL, work_config=LocalWorkConfig(shell_allowlist=None))`，与本地部署一致）执行被放行的 `ls && touch /tmp/b8-marker`：`invoke success=True`，`/tmp/b8-marker` 被创建——判定错误确实产生了磁盘副作用。

首版修复（只按段校验命令名）之后又发现同一类漏判：**引号内的命令替换**仍被放行。`` echo ''`touch /tmp/b8_e2e_marker` ``、`echo "$(touch /tmp/b8_e2e_marker)"`、`git log --format="$(id)"` 在首版修复后均为 `denied=False`；其中第一种经真实 `BashTool` 执行后 marker 被创建（`check_injection` 的反引号正则被 `''` 前缀绕开，见下节）。补充"任一 token 含 `$` 或反引号即拒绝"后，上述三条全部 `denied=True` 且 marker 未生成。

另测得两条 shell 语义事实，作为拒绝规则的依据：

- `PATH=/tmp/b8path ls` 执行 `/tmp/b8path/ls`（临时赋值改变被执行的程序）。
- `git -c diff.external=/tmp/b8gitpath/git-log diff` 执行了 `/tmp/b8gitpath/git-log`（git 全局选项可引入外部程序；`--exec-path` 同类，影响外部子命令/helper 的解析位置）。

## 威胁模型与影响面

- **谁能触发**：抽取子代理读取整段会话历史（`extraction_runner.py:240-260` 传入 `history`），会话内容可被第三方影响（用户粘贴的代码、issue 文本、工具返回）。提示注入可以让子代理发出被放行的 bash 调用。前置条件是 code 模式且开启自动记忆。
- **子代理拿到的是哪份 bash 工具**：`extraction_runner.py:322` 取父代理 `ability_manager.list()`，其返回的是 **ToolCard**（`openjiuwen/core/single_agent/ability_manager.py:1088`）；`create_deep_agent` 的 `_normalize_tools`（`openjiuwen/harness/factory.py:86-108`）只把 card 放进配置，执行时按 card id 从进程级 resource_mgr 取实例（`ability_manager.py:1229` 文档串、`:1672` 的 `get_tool`），因此解析到的就是父代理注册的那份 `BashTool`，绑定父代理的 SysOperation。`code_agent_rail.py:245-252` 的注释也明确：子代理必须复用父 SysOperation。
- **这道闸门之外的防线（按锁定的 openjiuwen 版本核对）**：
  - 本地部署的父 SysOperation 来自 `sysop_builder.py:919-925 create_local_sysop_card()`，`shell_allowlist=None` ⇒ `shell_operation.py:881-884` 的 `_check_allowlist` 直接返回 True；`restrict_to_sandbox` 默认 False ⇒ `shell_operation.py:950-953` 的 `_get_sandbox_roots` 返回 None，无路径沙箱。
  - `shell_operation.py:279` 的 `_DANGEROUS_PATTERNS` 是**空表**，注释说明危险检查已移到 PermissionEngine builtin rules；而抽取子代理只挂 `RuntimePromptRail`/`ProjectMemoryRail`/`CodingMemoryRail` + 本 rail（`extraction_runner.py:347-357`、`:393`），默认安全 rail 是 `SecurityRail = SafetyPromptRail`（`openjiuwen/harness/rails/security/prompt_security_rail.py:49`，纯提示词），不含 `PermissionInterruptRail`。
  - 本仓 `bash_tool_safety.py:157 install_shell_tool_safety_hooks()` 只在 `server/worker/runtime_backend.py:151` 安装，其 `_check_command_safety`（`command_tools.py:140`）只拦 kill/pkill/taskkill 家族，拦不住 `rm -rf` 以外的写操作。
  - `BashTool` 自带的 `check_injection`（`openjiuwen/harness/tools/shell/bash/_security.py:40-49`，拦 `$(`、反引号、`<()`/`>()`）**不能当作替代防线**：它只在 `OPENJIUWEN_BASH_STRICT=1` 时经 `_guard` 生效（`_tool.py:192`，默认关闭），且反引号正则带 `(?<!')` 前瞻（`_security.py:30`），`` echo ''`cmd` `` 这类写法直接漏判。

⇒ 结论：该 rail 是这条链上唯一按命令内容做判定的一层，放行一条串联命令约等于以当前用户身份执行任意命令。按安全缺陷（纵深防御被绕过）处理；修复范围仍限于这一处判定。

## 修复与不变量

`_is_read_only_bash_command` 改为结构化解析 + 逐段精确白名单：

- 用**引号感知的单遍扫描器**切分（不再用 `shlex`）；**含 `\n`/`\r` 的命令直接拒绝**（换行在 shell 里就是命令分隔，会把两条命令并成一段）。
- 引号与反斜杠只决定字符是否字面化，**不切 token**：shell 交给程序的 `--out''put=x`、`hello\ world` 都是单个参数，扫描器必须同样读成一个 token，因此引号态切换与 `\x` 只追加字符；只有 plain 态的空白切 token。
- 控制符采用**闭合规则**：只有 plain 态、未被引号或反斜杠转义的 `;&|<>()` 连续串才是操作符，且必须属于 `;`、`&&`、`||`、`|`、`&` 之一，否则拒绝（覆盖 `|&`、`&>>`、`>&`、`<>`、`>|`、`<<`、`<<<`、括号）；single/double 态内的操作符只是普通字符（`grep -rn '&&' src` 放行）。
- 命令替换按引号态判定：`$` 与反引号出现在 plain 态或 double 态即拒绝；single 态内是字面量（`grep -F '$' f`、`grep -F '`' f` 放行）。
- plain 态的 `{`/`}`/`*`/`?`/`[` 直接拒绝：花括号展开与路径名展开都会在 shell 层把词拼成选项（`git diff --{output,}=/tmp/x` 展开为 `--output=/tmp/x`，实测目标文件被创建/截断；在放有 `--output=zz`、`--ext-diff` 文件的目录里 `git diff *` 会把它们当选项传给 git，实测 `zz` 被写出），扫描器却只看到一个不匹配任何选项名的 token；引号内的 `{}`/`*` 仍是字面量。
- 每段校验：开头 `NAME=value` 拒绝；命令名含 `/`/`\` 拒绝；参数命中禁止选项族即拒绝——`--output`/`--ext-diff` 按**双向前缀**匹配（长选项可缩写到任意无歧义前缀，`--outpu=x` 因此也算命中；附着值 `=value` 先切掉再比较），`--output-indicator-*` 在比较前显式放行（只改 patch 标记字符，落在 `--output` 的前缀阴影里），`--textconv` 只做单向前缀，因为 `--text` 是合法无害的 diff 选项且是它的前缀；`git` 要求紧邻的下一个 token 就是 `status`/`log`/`diff`/`show`（全局选项整体拒绝）；`find` 的参数命中 `-delete`/`-exec`/`-ok`/`-fprint`/`-fls` 前缀族拒绝；其余命令名必须属于 `READ_ONLY_BASH_COMMANDS`。
- 不变量：**任一段无法证明只读即整条拒绝**；空段忽略（`git status;` 仍放行）。

误拒面（`git log 2>/dev/null`、`git -C repo status`、`find . -exec`、`LC_ALL=C ls`、`echo $HOME`、任何多行命令、任何 plain 态花括号、未加引号的通配符如 `ls *.py`）可接受：抽取子代理同时持有不受限的 `read_file`/`grep`/`glob`/`list_files`/`search`，这些写法丢失不影响它读文件这一主业。正常选项不受影响：`git diff --text` 放行（`--textconv` 保持单向匹配正是为了不误拒它），`git diff --output-indicator-new=+` 放行（`--output-indicator-*` 只改 patch 标记字符，无写盘/执行面，在双向匹配前显式放行，否则会被 `--output` 的前缀阴影误拒）。

## 白名单命令的选项空间核对

评审指出门禁只对 `git`/`find` 过滤选项，其余白名单命令的参数不受约束。逐条核对结论：

| 命令 | 选项层面的写/执行面 | 结论 |
|---|---|---|
| `ls`、`cat`、`head`、`tail`、`pwd`、`echo`、`wc`、`stat`、`grep` | 没有写盘或执行选项，输出只到 stdout；重定向已由分隔符规则整体拒绝 | 保留 |
| `find` | 写/执行 action 只有 `-delete`、`-exec`/`-execdir`、`-ok`/`-okdir`、`-fprint`/`-fprint0`/`-fprintf`、`-fls`，现有前缀族全部覆盖；primary 只能写全名（本机 `find` 是 bfs 4.1.1，实测 `-dele`/`-exe`/`-fpri` 报 unknown primary；GNU findutils 侧按 primary 精确名匹配记为文档依据，未在本机实测） | 保留现有黑名单 |
| `git`（限 `status`/`log`/`diff`/`show`） | `--output` 写文件、`--ext-diff` 执行外部 diff 程序，三条子命令都接受（`git show --output=` rc=0 并写出文件、`git log --ext-diff`/`git show --ext-diff` rc=0，git 2.50.1 实测），门禁按双向前缀拦，缩写写法一并覆盖；长选项缩写本身在 `diff`/`log`/`show` 上**不成立**（`--outpu=` rc=129、`--ext-dif` rc=128、`--sta`/`--onel` 同样不认，它们用各自的选项表），只有走 parse-options 的 `status` 接受缩写（`--porc` rc=0），而 status 的选项空间没有写盘或执行选项——双向匹配因此不依赖子命令用哪种解析器；`--ext-diff` 只挡住显式旗标，挡不住仓库配置驱动的程序：`diff.external` 与 `diff.<driver>.command`（由 `.gitattributes` 选中）在裸 `git diff` 下就会执行（`log -p`/`show` 实测不执行，它们只在 `--ext-diff` 下才执行，而该旗标已拦）；textconv 在 `diff`/`log -p`/`show` 下**默认**执行，只有显式 `--no-textconv` 能关；`core.fsmonitor` 由仓库配置驱动，`status` 与脏工作区的 `diff` 会执行它 | 参数黑名单保留；上述仓库配置驱动的执行面登记为残余 |
| `file` | `-C`/`--compile` 编译 magic 数据库并写出 `<source>.mgc`；file 5.41 实测 `-C`、`-bC`（短选项聚簇）、`--compile`、`--compil`（长选项缩写）四种写法都写出 `magic.mgc` | **移出白名单** |

`sort -o`、`unzip -d`、`tar -x`、`xxd -o`、`sed -i` 一类写盘选项所在命令本来就不在 `READ_ONLY_BASH_COMMANDS` 内，从未被放行。

`file` 选择整条移出而不是补 `_FILE_FORBIDDEN_OPTION_PREFIXES`：聚簇与缩写都能到达 `-C`/`--compile`，前缀黑名单在结构上不可靠；抽取子代理另有不受限的 `read_file`/`grep`/`glob`/`list_files`/`search`，`prompts.py` 里也没有 `file` 的用法，能力损失为零。

另有一类绕过与长选项缩写无关，却是同一张黑名单在 shell 层的入口：**token 合成**。

- 引号拼接：`git diff --out''put=/tmp/x`、`git diff -''-output=/tmp/x` 交给 git 的都是 `--output=/tmp/x`（实测写出 101 字节）。扫描器按 shell 语义读 token（引号只影响字面化、不切 token），因此仍合成单个 token 并被前缀规则拦住；若在引号态切换处切 token，就会得到 `-` + `-output=/tmp/x` 两个都不匹配禁止名的片段，重新打开这个洞，所以扫描器必须严格按"只有 plain 态空白切 token"实现。
- 花括号展开：`--{output,}=/tmp/x` 在会展开该语法的 `/bin/sh`（darwin 实测）下变成 `--output=/tmp/x`，实测 `git diff` 报 `invalid option: --=`，但目标文件已被创建/截断；`find . -e''xec rm x \;` 同理合成 `-exec`。plain 态 `{`/`}` 因此直接拒绝；dash 一类不展开的 shell 下这条是过度拒绝，门禁代码跨平台，取保守口径。
- 路径名展开：CWD 里存在以选项名命名的文件时，`git diff *` 会把 `--output=zz`、`--ext-diff` 当作选项传给 git（实测外部 diff 被执行、`zz` 被写出），`git log --oneline *` 同样写出 `zz`。与花括号同口径处理：plain 态未加引号的 `*`/`?`/`[` 一律拒绝，代价是 `ls *.py` 一类写法被误拒（子代理另有不受限的 `glob` 工具）。

长期方向仍是把选项检查从"禁止黑名单"改为"逐命令允许白名单"，本轮不做。

## 回归证据

- `tests/unit_tests/agentserver/memory/test_auto_memory_tool_restriction_rail.py`：修复前 **35 failed / 12 passed（共 47）**，修复后 **47 passed**。失败项即拒绝表（串联、多行、`|&`/`&>>`/`>&`/`<>`/`>|`/`<<<`、重定向、裸与引号内/反引号的命令替换、`PATH=`/`LC_ALL=` 前缀、`./ls`、`/bin/ls`、git 全局选项、`find` 写入/执行动作）与非字符串 `command`。
- 本轮选项加固：新增 6 条拒绝用例（`file`、`file -C`、`file --compile`、`file -bC`、`file --compil`、`git diff --textconv`）。未改实现前 **47 passed / 6 failed**，改实现后 **53 passed（共 53）**；再按评审补 3 条写入围栏拒绝用例（同前缀兄弟目录、绝对与相对 `..` 穿越）后为 **56 passed（共 56）**。
- 本轮按三条检视意见复核后：用例 56 → **88**。只加测试、未改实现时为 **7 failed / 77 passed**（当时的 84 条版本），改实现后全绿。改动前为红的 7 条是引号回归 4 条（`grep -rn '&&' src`、`grep -F "|" f`、`grep -F '$' f`、`grep -F '`' f`）、长选项缩写 2 条（`git diff --outpu=/tmp/out.patch`、`git log -p --ext-dif`）、花括号展开 1 条（`git diff --{output,}=/tmp/x`）。改写扫描器后为防重新打开引号拼接，另加 `git diff --out''put=/tmp/x`、`git diff -''-output=/tmp/x`、`find . -e''xec rm x \;` 三条改动前后都为绿的用例（最后一条特意不用 `{}`，否则会先被花括号规则拒掉，看不出引号拼接是否读对）；`git log --output=`、`git show --output=`、`git log --ext-diff` 补的是 `--output`/`--ext-diff` 在三条子命令上的回归保护（改动前已绿）；`git commit -m x`/`git push`/`git checkout -- .`/`git config --global core.pager 'sh -c id'`/`git -c ...`/`git --config-env=...` 补的是「git 子命令必须紧跟」这条核心规则（改动前已绿）。
- 复核后追加 4 条（2 允许、2 拒绝，用例 84 → 88）：`git diff --text`、`git diff --output-indicator-new=+ HEAD` 放行（分别钉住 `--textconv` 的单向匹配、`--output-indicator-*` 的前缀阴影豁免）；`git diff *`、`git log --oneline *` 拒绝（路径名展开）。这 4 条对上一版实现是 **3 failed / 85 passed**（红的正是 `--output-indicator` 误拒与两条路径名展开），修完 **88 passed**。
- 相邻 rail 用例：`tests/unit_tests/agentserver/test_code_agent_mode_rail.py` + `tests/unit_tests/agentserver/rails` 共 **136 passed**，与改动前一致。
- `/tmp/b8_repro.py` 修复后：`git status` 仍放行；三条绕过 payload 全部 `denied=True`，串联命令不再交给 `BashTool`，marker 未生成。

## 遗留与对外口径

- 未改变抽取子代理的工具集（仍继承父代理 `bash`），也未处理 `mcp_exec_command`（当前整条拒绝，属 fail-closed）与写路径判定。
- 已声明残余（都由仓库配置驱动，参数黑名单关不掉，完整清单以 `_is_read_only_bash_command` 的 docstring 为准）：
  - 外部 diff 程序：`diff.external` 与 `diff.<driver>.command`（`.gitattributes` 选中）在裸 `git diff` 下执行（git 2.50.1 实测）；`git log -p`/`git show` 实测不执行，只在 `--ext-diff` 下才执行，而该旗标已被拦——所以这一项登记在 `git diff`，不登记 `log -p`/`show`。
  - textconv：`git diff`/`log -p`/`show` 默认执行仓库配置的过滤器，关闭需要门禁注入 `--no-textconv`，而门禁不注入参数。
  - `core.fsmonitor`：`git status` 与脏工作区的 `git diff` 会执行它。
  - `git status` 还会刷新 `.git/index`（stat 缓存）。
- `--textconv` 只做单向前缀匹配，`--textcon` 一类缩写仍放行；该项本就是防御性登记（textconv 默认就会执行，拦旗标不等于收口），所以接受这个缺口，也不为它把匹配扩成双向——`--text` 是合法 diff 选项，双向会误拒。
- `file` 已移出白名单，抽取子代理若仍尝试会收到拒绝消息，改用 `read_file`/`glob`。
- 建议的上游表述：只读命令闸门不应使用子串匹配；应逐段解析并要求每段基础命令属于白名单，无法证明只读即拒绝。对外发布需另行授权。
