# Shell 网络策略与只拒绝的项目策略

本文介绍对现有权限体系（[工具权限与安全防护](工具权限与安全防护.md)）的两项补充：

* **Shell 网络策略**：shell 命令是否允许访问网络；
* **项目策略文件** `<项目>/.jiuwenswarm/permissions.yaml`：**只能拒绝**，由 Code 模式的守卫 Rail 执行，**从不合并进权限引擎**。

没有项目文件时，默认配置下行为不变：交互会话的 shell 网络策略为 `allow`；无头运行（process CLI 与 implement 驱动器）默认 `deny`。

## 1. 背景

* agent-core 权限引擎有三条流水线：工具/命令规则（Pipeline A）、文件路径（Pipeline B，含从 shell 命令中提取的路径）、**抓取类工具**的 URL（Pipeline C，`net_guard`）。**shell** 中的网络访问（`curl`、`wget`、`pip install`、`git clone|fetch|push`、`npm install` 等）此前不受任何约束。
* 权限引擎默认关闭（`permissions.enabled: false`），无头运行时也没有人能回答 `ask`。
* 仓库无法声明自身的限制。
* 在 SWE-bench 类评测中，Agent 反复尝试从 GitHub 获取上游修复，评测框架只能在容器层屏蔽网络。

## 2. 威胁模型

对手是：(a) 受注入指令驱使的 Agent 本身；(b) 附带恶意 `.jiuwenswarm/permissions.yaml` 的仓库。

* 项目文件**绝不能放宽**用户的任何配置。它由控制仓库的人编写，而不是用户。
* Agent 不能在会话内修改或删除项目文件。
* 无头运行默认不能悄悄访问网络。

这是策略守卫，不是沙箱（见第 6 节）。

## 3. Shell 网络策略（用户/全局配置）

`permissions.shell_network: allow | ask | deny`（默认 `allow`）与 `permissions.headless.shell_network`（默认 `deny`，仅无头运行）。生效级别取配置级别、无头级别（无头时）与项目文件 `shell_network: deny` 中**最严格**者。

检测在**命令位置**匹配可联网的命令，且不区分大小写（macOS 文件系统会把 `CURL` 当作 `curl` 执行）。命令位置包括：文本开头、换行，`;` `&` `|` `(` `{` `!` `` ` `` `$(` 之后，shell 关键字 `if` / `elif` / `then` / `else` / `do` / `while` / `until` 之后，`case` 分支模式之后（`in a)` 或 `;; b)`），以及紧跟在 `-c` 或 `eval` 后的引号内（因此 `bash -c 'curl …'` 会识别，而 `echo 'pip install x'`、`grep -r 'npm install' docs` 不会）。命令前允许 `VAR=value` 赋值、目录前缀，以及**带参数值**的启动器链：`sudo -u a`、`doas -u x`、`env -u X`、`timeout -s KILL 25`、`nice -n 5`、`ionice -c 3`、`stdbuf -oL`、`watch -n 5`、`xargs -I {}`、`exec`（含 `exec -a name`）、`nohup`、`time`、`command -p`、`builtin`、`chronic`、`unbuffer`、`busybox`。`command -v curl` 只是查找工具，不算启动器。`$(which curl)` 与 `` `which wget` `` 会被解析为对应工具。`deny_commands`（第 4 节）使用同一份启动器与关键字列表，两者对「命令词是什么」的判断一致。

**引号与转义。** 命令既按原文匹配，也按规范化后的形式再匹配一次：去掉字符前的反斜杠、去掉空引号对、去掉包住单个无空格词的引号。因此 `\curl`、`c\url`、`p''ip`、`"curl"`、`'c'url` 都会被识别为 `curl` / `pip`。含空格的引号文本仍视为参数。

覆盖：curl、wget、带 URL 或主机参数的 HTTP 客户端（参数前可有选项或 HTTP 方法，如 `http GET https://…`、`lynx -dump https://…`；包括 `http`/`https`/`httpie`、`xh`、`curlie`、`aria2c`、`lynx`、`links`、`elinks`、`w3m`）、netcat/telnet/socat、ssh/sftp/ftp、远程 scp/rsync、`/dev/tcp` 与 `/dev/udp` 重定向、git clone/fetch/pull/push/ls-remote/remote update/submodule update、svn/hg 检出、pip（任意 `pipX.Y`、`pythonX.Y -m pip`）、pipx/uvx、uv add/sync/pip/`run --with`、poetry/pdm add/install、conda/mamba/micromamba install、npm/yarn/pnpm/bun 安装与 npx/dlx、带 URL 的 deno、gem/cpan、系统包管理器、go/cargo 获取、docker/podman pull/push/login、gh CLI。

执行方式：

| 场景 | `deny` | `ask` |
|---|---|---|
| 交互，引擎关闭（`permissions.enabled: false`） | `ShellPolicyGuardRail` 拒绝 | 无审批通道，不执行；记录一次日志 |
| 交互，引擎开启 | 守卫 Rail 拒绝；引擎另有一条 deny 规则 `shell_network_policy` | 引擎通过规则 `shell_network_policy` 询问；若该规则可能放宽用户基线则不注入（见下） |
| 无头（无论引擎是否开启） | 守卫 Rail 拒绝 | 拒绝（无人可答） |

**拒绝在哪里生效。** 上表中引擎关闭的各行（以及守卫 Rail 的所有 `deny` 拒绝）只在挂载了 `ShellPolicyGuardRail` 的地方生效：**Code 模式**（交互与**无头 process CLI**）。通用 Agent 模式与 swarm/team 成员装配路径不挂载它，在那里 `shell_network` 只通过引擎规则执行。因此**引擎关闭（`permissions.enabled: false`）时，通用 Agent 模式完全不执行 `shell_network`，包括 `deny`**。引擎开启时，这些模式同样适用引擎的 deny 或 ask 规则。引擎规则按命令原文匹配；上述规范化形式（`\curl`、`p''ip`）只有守卫 Rail 能识别，因此交互下的引擎 `ask` 不会对它们询问。

**引擎规则绝不放宽。** agent-core 先判定规则命中，再看工具级别与默认值；多条非 deny 命中取最严格者。因此 `deny` 规则只会让结果更严格。`ask` 规则只在一种情况下会放宽：某个 shell 调用没有其他规则命中、其工具没有自己的级别、且 `defaults["*"]` 为 `deny`（工具级 `deny` 会在规则之前直接拒绝）。这种情况下**不**注入 `ask` 规则，由用户自己的 `deny` 基线决定（info 日志）。字典形式的工具级别（`bash: {"*": deny}`）按引擎的方式读取。

引擎规则只由用户/全局配置生成，从不读取项目文件，也从不写入 User/Session overlay（overlay 提取不复制 `rules`）。

模型看到的拒绝结果：

```json
{"success": false, "error": "permission_denied", "policy": "shell_network",
 "command_class": "pip install",
 "detail": "Network access from the shell is disabled (pip install) by permissions.shell_network. Work from the files already in the project; ..."}
```

## 4. 项目策略文件（只拒绝）

`<项目根>/.jiuwenswarm/permissions.yaml`（当前轮次的项目根）。可以是顶层键，也可以放在 `permissions:` 下：

```yaml
deny_paths:                  # 所有访问：读、写、执行
  - secrets                  # 目录：其下所有内容
  - ".env*"                  # glob，相对项目根
  - {path: build, axes: [write]}
deny_read:  ["../**"]        # 项目之外、上一级目录
deny_write: ["/etc/**"]
deny_exec:  [scripts/release.sh]
deny_commands:
  - "rm -rf *"               # 普通模式：* 匹配任意内容，允许后续参数
  - "re:^docker\\s+push\\b"  # re:：正则表达式
shell_network: deny          # 只接受 "deny"
writable_roots: [src, tests] # 按路径写入的文件工具只能写这些目录
```

**只支持上述键。** 用户/全局权限配置中的键在项目文件里只能用于放宽，因此一律忽略并记录日志：工具级别、命令规则、`defaults`、`approval_overrides`、`file_guard`、`net_guard`、以 `allow` 或 `ask` 开头的键，以及 `shell_network: ask|allow`。**项目无法表达 ask 或 allow。**

**格式错误的文件失败即拒绝。** 任何会悄悄丢掉拒绝项的问题，都让整个文件不可用，与非 YAML 文件相同：顶层不是映射（例如是列表）、未知键（如拼写错误的 `deny_path`、`deny-commands`、`shellnetwork`）、类型错误的值（`deny_paths: 42`）、格式错误的条目、错误的 `re:` 模式、不是级别的 `shell_network` 值。此时文件与 shell 工具会被拒绝（`project_policy_invalid`），并给出一行说明，包含文件路径、问题与解决办法：

```text
The project policy file <root>/.jiuwenswarm/permissions.yaml cannot be used (unknown key 'deny_path'); file and shell tools are refused until the user fixes or removes it, or sets permissions.project_policy.enabled: false in config.yaml to ignore project files.
```

**路径。** 相对条目用 `pathlib` 锚定在项目根：`.env*` 仍是 `<根>/.env*`，`../**` 变为 `<根的上一级>/**`。第一个通配符之前的字面部分会被解析（折叠 `..`、跟随符号链接；macOS 上 `/etc` 是 `/private/etc`），因为守卫 Rail 与解析后的调用路径比较。不含通配符的条目是前缀：路径本身及其下所有内容。glob 支持 `**/`、`**`、`*`、`?` 与 `[…]`；匹配到目录的 glob 同样覆盖其下所有内容。在大小写不敏感的文件系统上（macOS 默认）按大小写折叠比较。

**命令。** 对 shell 行中的每个简单命令检查：按 `;` `;;` `&` `|` `&&` `||` `(` `)` `{` `}` `` ` `` `$(` 与换行拆分（因此子 shell、命令组与 `case … ) … ;;` 分支体各自是独立命令）。每行按三种方式拆分：原文、去掉所有引号（因此 `bash -c 'rm -rf x'` 得到 `rm -rf x`）、按第 3 节规范化（因此 `\rm`、`r''m`、`"rm"` 得到 `rm`）。然后去掉 `VAR=value` 前缀、关键字 `if`、`elif`、`then`、`else`、`do`、`while`、`until` 与 `!`、第 3 节的启动器（`sudo`、`env`、`timeout N`、`command -p`、`builtin`、`exec`、`stdbuf`、`busybox`、`xargs` 等）以及 `bash -c`、`eval`，和命令词的目录前缀。普通模式不区分大小写，匹配片段及其后续参数；`re:` 模式在整行及每个片段中搜索。

**在哪里执行。** 只在 Code 模式的守卫 Rail（`ShellPolicyGuardRail`）中执行。它在 agent-core 权限 Rail 之前运行（优先级 95 对 90），只会拒绝：

| 检查 | 拒绝（`policy`） |
|---|---|
| 项目文件不可用（不可读、不是 YAML、超过 64 KB、符号链接解析到项目之外、不是普通文件，或如上所述格式错误） | `project_policy_invalid`：修复前拒绝文件与 shell 工具 |
| 写策略文件（见第 5 节） | `project_policy_file` |
| 调用的任一访问命中路径拒绝：文件工具参数及其读/写/执行动作、`apply_patch` 目标（视为写）、agent-core 提取器从 shell 命令中找到的路径（重定向、路径感知命令、解释器脚本视为执行），以及守卫自身的读取命令提取（见下） | `project_deny_paths` |
| shell 命令匹配 `deny_commands` | `project_deny_commands` |
| 按路径写入的文件工具写到 `writable_roots` 之外 | `writable_roots` |
| 项目设置 `shell_network: deny` 时的联网命令 | `shell_network` |

**Shell 读取命令。** agent-core 的提取器会漏掉很多读文件的命令，因此守卫在规范化后的命令上另做一次尽力而为的提取：

* 已知读取命令的每个非选项参数都视为读：`cat`、`head`、`tail`、`less`、`sed`、`awk`、`base64`、`xxd`、`od`、`hexdump`、`strings`、`grep`、`rg`、`cut`、`sort`、`diff`、校验和工具、`tar`、`zip`、`jq`、`ls`、`find`、`tree`、`du`、`source` 等（`cp`、`rsync` 交给 agent-core 的提取器，它能区分哪个参数是写入目标）。`sed`、`awk`、`grep`、`rg`、`jq` 的脚本/模式参数会跳过，除非脚本是用 `-e` 给出的（`-f FILE` 的脚本文件视为读）；
* `git` 的每个 `REV:path` 参数视为读（`git show HEAD:secret/key`）；
* 含 `/` 的命令词视为对该路径的执行（`./secret/tool`）；
* `cd DIR` 会改变同一行中后续相对路径的基准（`cd secret && cat key`）；
* 这些参数中的通配符会按文件系统展开（最多 64 个匹配）。

**作用范围。** 项目文件在 **Code 模式**（交互与**无头 process CLI** 运行）中执行。**其他 Agent 会忽略它**：通用 Agent 模式与 swarm/team 成员装配路径不挂载该 Rail。

**引擎看不到项目文件。** 引擎快照只由 Global ⊕ User ⊕ Session 配置构建（外加第 3 节的用户/全局 shell 网络规则）。因此最终结果是「用户的引擎判定」与「项目拒绝项」中更严格者：有无项目文件，引擎判定完全相同，守卫 Rail 只会拒绝。

**缓存与开销。** 结果（无论可用与否）按 `(路径, mtime, 大小)`（及符号链接目标的相应值）缓存，未变化的无效文件不会在每次调用时重新读取或重复报告；glob 与命令模式只编译一次，合并为组合正则。在 Apple silicon 笔记本上，200 条路径（100 前缀、100 glob）加 40 条命令模式：每次工具调用约 0.2–0.5 ms（中位数）；文件变化后首次约 12 ms。

在 `config.yaml` 的 `permissions` 段设置 `permissions.project_policy.enabled: false` 可忽略项目文件。

## 5. 策略文件保护

无论何种模式、无论是否存在项目文件，文件工具都不能写 `.jiuwenswarm/permissions.yaml`（比较解析后的目标，符号链接与 `..` 无效；大小写不敏感的文件系统上按大小写折叠比较）。`apply_patch` 的补丁体会被解析出目标：

* codex 风格的 `*** Update File:`、`*** Add File:`、`*** Delete File:`、`*** Move to:` 头（不区分大小写，允许前导空白）；
* unified diff 的 `--- a/…`、`+++ b/…`、`diff --git`、`rename from/to`、`copy from/to` 行；
* CRLF 与仅 CR 的换行；
* git 带引号路径中的 C 转义与 `\ooo` 八进制转义会被解码；
* 嵌套的字符串参数（列表、字典）。

补丁体提到 `.jiuwenswarm` 或 `permissions.yaml`，且解析不出任何目标，或在未能解析的类头部行中提到它时，拒绝（失败即拒绝）。hunk 内容行中的提及不受影响。

以下 shell 命令在任何模式下都会被拒绝：

* 重定向写入该文件（`>`、`>>`、`>|`、`&>`，包括 `cat /dev/null >` 与 `: >`），或对其使用 `tee`、`sed -i`、`perl -i`、`rm`、`unlink`、`mv`、`cp`、`truncate`、`ln`（含 `-sf`）、`chmod`、`chown`、`install`、`dd of=`、`shred`、`rsync`、`git checkout|restore|rm|mv`；
* 删除、移动、链接、复制进或修改权限 `.jiuwenswarm` 目录本身（`rm -rf .jiuwenswarm`、`rm .jiuwenswarm/*`、`mv .jiuwenswarm x`、`cp evil.yaml .jiuwenswarm/`、`chmod 000 .jiuwenswarm`）；
* 用 `find` 查找该目录或文件并带 `-delete`、`-exec`、`-ok`；
* `cd` 进 `.jiuwenswarm`（直接写出目录名）后写 `permissions.yaml`；
* 通过**别名**写入：同一简单命令中含写入动词时，其每个词都会相对项目根解析（跟随符号链接、折叠 `..`），因此先 `ln -s .jiuwenswarm cfg`、之后 `rm cfg/permissions.yaml` 在链接已存在时会被拒绝。

由用户在会话之外编辑该文件。

**可写目录。** `writable_roots`（相对项目根或绝对路径）限制所有按路径写入的文件工具：`write_file`、`edit_file`、`write_text_file`、`write`、`search_replace`、`apply_patch`。该集合由权限能力表推导，新增的按路径写入工具会自动覆盖。目标路径解析（跟随符号链接、折叠 `..`）后必须位于某个根目录内。

## 6. 哪些是强制的，哪些是尽力而为

* **强制：**
  * 项目文件不能改变权限引擎的判定，守卫 Rail 只会拒绝，因此组合结果永远不比用户自己的更宽松；
  * 文件工具参数与 `apply_patch` 目标上的项目路径拒绝；
  * 仓库感知工具（`code_search`、`find_symbol`、`symbol_outline`）的 `path`、`file_path` 与包含型 `glob` 前缀按「读」检查，目录参数按其下所有内容检查；
  * agent-core 的搜索与列目录工具（`grep`、`glob`、`list_files`）：若 `path` 是被拒目录，或其下内容全部被拒的目录（`secret/**` 对应的 `secret`），调用会被预先拒绝。对上级目录的搜索或列目录照常运行，随后守卫从模型看到的文本和结构化结果中删除所有属于读取被拒路径的结果：各种输出模式下的 `grep` 行（包括上下文行和 ripgrep 的 `路径-行号-文本` 形式）、计数和文件列表，`glob` 的匹配项，以及自身路径被拒的 `list_files` 条目（`secret/` 这样的目录名仍会列出，其中的内容不会）。指向被拒目录的符号链接同样会被隐藏。结果末尾会追加一行固定说明：「(Files denied by the read policy in .jiuwenswarm/permissions.yaml are left out of these results.)」。无论是否删除了内容都会追加这一行，因此无法通过搜索猜测的字符串来判断被拒文件是否包含它。过滤失败时，结果会被扣留，而不是原样返回；
  * 指向这类目录的 shell 读取命令（`grep -rn x secret`、`rg x secret`、`ls secret`）同样会被拒绝；
  * 读取整棵目录树的 shell 命令，若树中含有被拒绝读取的路径，则拒绝：`grep -r`/`-R`/`--recursive`/`-d recurse`（含 `-rn` 这类短选项组合）、`rg`、`ag`、`ack`、`ugrep`（默认递归；未给路径时搜索工作目录，被管道输入时除外）、`git grep`、`find … -exec`/`-ok`、`fd -x`/`-X`、把文件列表（`find`、`fd`、`rg --files`、`git ls-files`、`ls`）交给 `xargs CMD`、`tar c`、`zip -r`、`7z a`、`cp -r`/`-a`、`scp -r`、`rsync -r`/`-a`、`ditto` 与 `mv`。会跟踪 `cd`/`pushd`、`git -C` 以及带引号的 `bash -c '…'` 内容。“树中含有”指树根之下存在被拒绝的区域（`secret/**` 对应的 `secret/`），或树中有文件被通配拒绝项匹配（`.env*`、`**/*.pem`；通过有上限的遍历发现，树过大时按拒绝处理）。在其下没有被拒绝路径的目录里做同样的搜索（`grep -rn x calc`）照常执行。拒绝信息会引导使用经过过滤的 `grep` 工具。这堵住了桌面端复测中看到的回退：模型在过滤后的 `grep` 没有结果后，改用 `cd <project> && grep -rn KEY .`；
  * `git_prepare` 的提交（列出的 `paths`，或 `all_tracked` 时所有已跟踪改动，含已暂存）与 `git_publish` 的待推送提交（尚未出现在任何远程分支上的提交）中若含被拒绝读或写的文件，则拒绝；
  * 所有按路径写入的文件工具的可写目录；
  * 文件工具对策略文件的写入；
  * 第 3 节所列命令类别上的用户/全局与项目 `shell_network: deny`。
* **尽力而为，不是沙箱：**
  * 解释器单行命令（`python -c "import urllib…"`）、编译好的二进制、会下载的脚本文件、别名或 shell 函数、保存命令名的变量（`$CMD example.org`）、检测器不认识的工具，仍可能访问网络；
  * shell 命令上的路径拒绝依赖 agent-core 的路径提取器加上守卫的读取命令提取（第 4 节）。看不到的情况：由变量或命令替换拼出的路径（`cat $(echo secret)/key`、`d=secret; cat $d/key`）、经变量的 `cd`、不在列表中的读取命令、由解释器（`python -c "open(…)"`、`node -e`）或脚本打开的文件（字面写出的带引号路径可能被顺带识别，但不可依赖）、上面的递归读取检查识别不了的目录遍历（遍历 `$(find …)` 的循环、遍历目录的脚本或解释器、不认识的读取命令），以及干净目录中指向被拒绝区域的符号链接被跟随链接的命令（`grep -R`、`rg -L`、`find -L`）读取。被拒绝文件的文件名仍可能被列出（`find .`、`ls -R`、`rg --files`），受保护的是文件内容。守卫无法过滤 shell 的输出，因此在有读取拒绝的项目中，经过过滤的 `grep` 工具才是安全的搜索方式；
  * 在被拒路径的上级目录（例如整个仓库）上运行的 `code_search` / `find_symbol` 无法预先拒绝（否则每次搜索都会被拒绝）；从结果中过滤掉被拒文件是这些工具自身的职责（仓库工具 MR 负责）；
  * `run_tests` 不是 shell 工具，因此 shell 网络策略（包括无头模式默认的 `deny`）不作用于它：测试代码仍可访问网络。需要强隔离时请在无网络容器中运行；
  * `deny_commands` 是对命令文本的模式匹配。引号、转义、启动器与关键字位置已规范化（第 4 节），但保存命令名的变量（`g=git; $g push`）、别名、shell 函数、脚本、解释器（`python -c "shutil.rmtree(…)"`）、`find … -exec rm`，以及参数的等价写法（`rm -fr`、`rm -r -f`、`git -C . push`）除非模式覆盖，否则不会匹配（参数变体请用 `re:` 模式）；
  * shell 写入（`echo > file`、`sed -i`）不受 `writable_roots` 检查；
  * 以下对策略文件或其目录的 shell 修改无法识别：通过解释器或脚本（`python -c "open(…,'w')"`、`node -e`、shell 脚本文件）、路径保存在变量中或由命令替换拼出（`f=.jiuwenswarm/permissions.yaml; : > $f`）、通过变量/别名/函数 `cd` 后使用相对名、不直接写出名称的通配符（`rm .jiuwen*/*`、`find . -name '*.yaml' -delete`）、在同一命令中新建的符号链接（别名检查只能看到已存在的链接）、删除上级目录（`rm -rf ../project`），以及文件未被跟踪时的 `git clean` / `git stash -u`；
  * 策略文件也可能被不点名地批量写文件的命令改写：`tar xf`、`unzip`、`git apply`、`patch`、`rsync -a src/ .`、`git reset --hard`、`git checkout -- .`，以及 `vim`、`ex` 等交互式编辑器。它们只能替换项目**自己的**拒绝项（项目文件不能放宽用户配置），用户的设置仍然有效。
* **错误处理：** 守卫 Rail 检查中的任何错误都会以 `"policy": "policy_check_error"` 拒绝该调用并记录异常，从不放行未检查的调用。如果守卫 Rail 本身无法构建，Code 适配器会记录 ERROR 日志并改为挂载一个失败即拒绝的守卫：拒绝 shell 工具与所有文件工具（`"policy": "policy_guard_unavailable"`），并提示检查 `config.yaml` 与日志；会话的其他功能仍可使用。之所以不让整个 Agent 构建失败，是因为那会把用户完全挡在会话之外；之所以不直接去掉守卫，是因为那会悄悄失去保护。生效级别为 `allow`（或交互下为 `ask`）时根本不运行网络启发式检测。
* **检查时间与使用时间：** 路径在检查工具调用时解析。检查与写入之间被替换的符号链接（例如由后台 shell 任务替换）无法发现。
* **大小写不敏感的文件系统：** 项目根位于大小写不敏感的文件系统时，守卫 Rail 按大小写折叠比较。agent-core 引擎按大小写敏感比较路径，因此在这类文件系统上，**用户自己的** file guard 规则仍可能被大小写变体绕过；这是 agent-core 的限制，不在本次改动范围内。
* **不增加操作系统级隔离。** 没有找到不修改 agent-core 就能包装其 shell 工具所执行命令（用于 `sandbox-exec` / `bwrap`）的公开扩展点；JiuwenBox 执行已由 `execution_provider_contract.py` 管理。需要强隔离时，请像评测框架那样在无网络的容器中运行无头任务。

## 7. 配置参考

| 键 | 默认值 | 含义 |
|---|---|---|
| `permissions.shell_network` | `allow` | 交互会话的 shell 网络策略 |
| `permissions.headless.shell_network` | `deny` | 无头运行（process CLI / implement 驱动器）的策略，与上一项取更严格者 |
| `permissions.project_policy.enabled` | `true` | 读取 `<项目>/.jiuwenswarm/permissions.yaml` |

## 8. 如何确认生效

* 启动日志：`[JiuwenSwarmCodeAdapter] ShellPolicyGuardRail create success (headless=True engine=False shell_network=allow)`。
* 每次拒绝记录 `[ShellPolicyGuardRail] refused <tool>: policy=<policy> headless=<bool>`，工具结果是类似第 3 节的 JSON，带对应的 `policy`。
* 被忽略的项目条目记录 `[permissions.project] <path>: ignored (project files are deny-only; supported keys: ...)`。
* 不可用的项目文件记录 `[permissions.project] The project policy file <path> cannot be used (...)`，文件每变化一次只记录一次。
* 守卫无法构建时以 ERROR 级别记录 `[JiuwenSwarmCodeAdapter] ShellPolicyGuardRail create failed; shell and file tools are refused this session`。
* 守卫出错时返回 `"policy": "policy_check_error"`，并记录 `[ShellPolicyGuardRail] policy check error; call refused` 及堆栈。
* 引擎开启时，联网命令由规则 `tiered_policy:rules:deny:rules[shell_network_policy]`（或 `...:ask:...`）判定。该规则以引擎的 `shell` 类别为目标，适用于生效配置 `categories.shell` 中的所有工具。跳过 `ask` 规则时记录 `[permissions.shell_network] 'ask' rule not added: the shell baseline is defaults deny`。

## 9. 局限

* 检测基于模式匹配（见第 6 节），用于拦截常见情况，不是网络沙箱。
* 只有 Code 模式（交互与无头 process CLI）执行项目文件；其他 Agent 与 swarm/team 成员装配路径忽略它。引擎关闭时，这些模式也不执行 `shell_network`（第 3 节）。
* 项目不能请求审批，也不能授予任何权限，只能拒绝。
* 引擎关闭时，交互会话不执行 `shell_network: ask`。引擎的 `ask` 规则会输给用户此前对同一命令授予的「始终允许」，因为 agent-core 先检查 `approval_overrides`，再看非 deny 规则；只有 `deny` 是无条件的。
