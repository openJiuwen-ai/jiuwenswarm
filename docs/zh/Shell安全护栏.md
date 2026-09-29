# Shell安全护栏

ShellGuard 检查智能体直接调用的 Shell 命令，复用权限引擎和用户确认链路。
黑名单返回 `deny`，白名单返回 `allow`，灰名单返回 `ask`。命令放行后仍需通过文件、网络等适用的护栏。

在「更多 → 配置信息 → 安全配置 → Shell安全护栏」中修改「启用内置命令规则」，点击「保存护栏配置」。后续命令检查使用保存的配置，不需要重启沙箱。

在同一区域的「自定义命令规则」中可新增、编辑和删除规则。添加时选择黑 / 白 / 灰名单及通配符 / 正则表达式，填写命令匹配模式后点击「保存规则」。正则模式不需要填写 `re:` 前缀，系统自动补齐；无效正则由后端拒绝保存。新增规则适用于所有 Shell 工具，编辑已有规则保留其原有工具范围。内置高危规则可展开查看，不支持逐条修改。

```yaml
permissions:
  enabled: true
  package_builtin_rules: true
  shell_guard:
    builtin_rules_enabled: true
  rules:
    - id: deny_demo
      tools: [shell]
      pattern: 'blocked-command *'
      action: deny
    - id: allow_status
      tools: [shell]
      pattern: 'git status *'
      action: allow
    - id: ask_deploy
      tools: [shell]
      pattern: 're:^deploy\s+.*$'
      action: ask
```

命令模式支持 `*`、`?` 通配符，以及 `re:` 前缀的 Python 正则表达式。通配符使用整条匹配，不跨越 Shell 拼接元字符；可解析的复合命令还会逐条检查子命令。正则使用搜索匹配，需要全串匹配时加 `^` 和 `$`。

`?` 不匹配命令中的字面量 `*` 或 `?`，例如 `rm ?` 不会放行 `rm *`；`*` 仍可匹配文件通配参数，例如 `ls *` 可匹配 `ls *.txt`。前端正则输入框只接受表达式，不接受 `re:` 前缀；后端也拒绝重复前缀，防止规则静默失效。

内置高危规则复用 agent-core 的 `harness/resources/builtin_rules.yaml`，包括磁盘破坏、注册表删除等黑名单，以及递归删除、提权、下载后执行等灰名单。命令黑名单优先于“记住允许”；记住的授权可以免除后续匹配的灰名单确认。

对于命令替换等无法完整解析检查的结构，记住的授权不能直接放行，仍需本次确认；即使关闭未知结构检查开关，此限制仍适用于记住的授权。能明确命中黑名单的命令继续拒绝。

`builtin_rules_enabled` 缺省为 `true`。关闭它只停用内置命令规则，用户命令规则、未知结构检查、解释器管道检查以及文件和网络护栏保持原有行为。`permissions.enabled` 是权限管控总开关，`package_builtin_rules` 是命令、文件和网络内置规则的总开关，均保留原有语义。

本能力不监控脚本运行时动态创建的子进程，也不解析外部脚本文件内的全部命令。
