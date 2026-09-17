# 命令发现

## 不要猜命令

当不确定资源组、方法名或参数时，先运行 help：

```bash
gitcode-api --help
gitcode-api <resource> --help
gitcode-api <resource> <method> --help
```

示例：

```bash
gitcode-api pulls --help
gitcode-api pulls create --help
```

资源级 help 会列出该资源组的 subcommands。方法级 help 开头会展示对应 SDK 方法签名，可直接据此补齐参数。

## 命名映射

Python SDK 方法名中的下划线在 CLI 中变成连字符：

```text
client.oauth.build_authorize_url(...) -> gitcode-api oauth build-authorize-url ...
client.contents.get_raw(...) -> gitcode-api contents get-raw ...
```

资源组名称通常保持不变，例如 `repos`、`pulls`、`issues`、`search`。

## 推荐发现流程

1. 运行 `gitcode-api --version` 确认版本。
2. 运行 `gitcode-api --help` 看当前安装版本暴露的资源组。
3. 运行 `gitcode-api <resource> --help` 看方法列表。
4. 运行 `gitcode-api <resource> <method> --help` 看参数签名。
5. 只传 help 中确认存在的参数；不要传空字符串、空数组或猜测字段。
6. 如果接口报错，先读错误信息，再回到方法 help 校正参数。

## 与文档不一致时

以本机 `gitcode-api ... --help` 为准。该 CLI 是从同步 SDK 资源方法动态生成的，不同安装版本可能暴露不同方法。
