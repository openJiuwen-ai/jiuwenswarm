# 用户态 IM Connector 结构

本文描述当前已实现的 `jiuwenswarm/server/im/im_connector`（含 agent-core 侧只读学习接口）。不含托管轮询、门控、落库、Web。

分层：

- **openjiuwen**：`ImLearningSource` 只读协议，不感知 CLI。
- **AgentServer Connector**：`ChannelPlugin` + 三家 CLI 实现；学习侧用 `ConnectorLearningSource` 适配。
- **平台 CLI**：`welink-cli` / `lark-cli --as user` / `dws`。

```
openjiuwen.harness.personal_context.im
        ▲ 只读 fetch
ConnectorLearningSource
        ▲
ChannelPlugin  ←  ConnectorRegistry
        ▲
CliBackedConnector
        ▲
WeLinkConnector / FeishuConnector / DingTalkConnector
        │
    ChannelCli  →  CliRunner  →  平台 CLI 进程
```

## 类图

### 契约与注册

```mermaid
classDiagram
    class ChannelPlugin {
        <<abstract>>
        +meta ChannelMeta
        +fetch_messages(target, options) MessagePage
        +send_message(target, content, options) SendResult
        +resolve_identity() Identity
        +test_connection() TestResult
        +search_persons(text) list~ChannelPerson~
        +discover_conversations(query_count) list~ChannelTarget~
        +format_outgoing_reply(content, context) str
    }

    class ConnectorRegistry {
        +register(plugin)
        +get(channel_id) ChannelPlugin
        +require(channel_id) ChannelPlugin
    }

    class ConnectorLearningSource {
        -_registry ConnectorRegistry
        +fetch_messages(target, cursor) ImMessageBatch
    }

    class ImLearningSource {
        <<Protocol>>
        +fetch_messages(target, cursor) ImMessageBatch
    }

    ChannelPlugin <|-- CliBackedConnector
    ConnectorRegistry o--> ChannelPlugin : 按 channel_id
    ConnectorLearningSource --> ConnectorRegistry
    ConnectorLearningSource ..|> ImLearningSource
```

### CLI 运行时与通道实现

```mermaid
classDiagram
    class CliRunner {
        <<Protocol>>
        +run(args, timeout_ms) CliResult
    }
    class SubprocessCliRunner {
        +run(args, timeout_ms) CliResult
    }
    class MockCliRunner {
        +enqueue(command, result)
        +run(args, timeout_ms) CliResult
    }
    class ChannelCli {
        +query_history_message(...)
        +send_to_group(...)
        +send_to_user(...)
        +auth_status()
        +search_persons(text)
        +lookup_self_person()
        +query_recent_conversations(...)
        +help()
        +history_args()*
        +send_group_args()*
        +send_user_args()*
    }
    class WeLinkCli
    class FeishuCli
    class DingTalkCli
    class CliBackedConnector {
        -_cli ChannelCli
        -_self_account
        -_self_accounts
        -_self_display_name
        +fetch_messages()
        +send_message()
        +test_connection()
        +search_persons()
        +discover_conversations()
        +apply_self_ids()
        +reply_label()
        +parse_history_page()*
        +mention_token()*
        +resolve_identity()*
    }
    class WeLinkConnector
    class FeishuConnector
    class DingTalkConnector

    CliRunner <|.. SubprocessCliRunner
    CliRunner <|.. MockCliRunner
    ChannelCli --> CliRunner
    ChannelCli <|-- WeLinkCli
    ChannelCli <|-- FeishuCli
    ChannelCli <|-- DingTalkCli
    CliBackedConnector --> ChannelCli
    CliBackedConnector <|-- WeLinkConnector
    CliBackedConnector <|-- FeishuConnector
    CliBackedConnector <|-- DingTalkConnector
    WeLinkConnector --> WeLinkCli
    FeishuConnector --> FeishuCli
    DingTalkConnector --> DingTalkCli
```

平台子类只做三件事：拼 argv、解析 JSON、补身份 / @ 规则。拉取、发送、发现会话的主流程在 `CliBackedConnector`。

| 通道 | CLI | 身份 | @ 原发送人 |
|---|---|---|---|
| WeLink | `welink-cli` | `auth status` → 搜 uid | 默认 `@name` |
| 飞书 | `lark-cli --as user` | `auth status` → `+search-user --user-ids me` 取 `localized_name` | `<at user_id="ou_…">`，有 @ 时走 `--markdown` |
| 钉钉 | `dws` | `get-self` → 搜人拿 `openDingTalkId` | `<@DGU…>` |

### 主要数据对象

```mermaid
classDiagram
    class ChannelTarget {
        +kind group|user
        +external_id
        +title
    }
    class FetchOptions {
        +count
        +page_token
        +message_id
        +query_direction
    }
    class MessagePage {
        +messages list~ImMessage~
        +next_page_token
        +has_more
    }
    class ImMessage {
        +msg_id
        +sender_account
        +sender_name
        +content_text
        +is_self
    }
    class SendOptions {
        +reply_prefix
        +mention_sender
        +extra
    }
    class SendResult {
        +ok
        +external_message_id
    }
    class Identity {
        +account
        +display_name
        +extra
    }
    class ImLearningTarget {
        +channel_id
        +kind
        +external_id
    }
    class ImLearningCursor {
        +message_id
        +extra.page_token
    }
    class ImMessageBatch {
        +messages
        +next_cursor
    }

    ChannelTarget --> MessagePage : fetch
    FetchOptions --> MessagePage
    SendOptions --> SendResult
    ImLearningTarget --> ImMessageBatch
    ImLearningCursor --> ImMessageBatch
```

`FetchOptions.page_token` 给钉钉 / 飞书分页；`message_id` + `query_direction` 给 WeLink。学习侧把飞书/钉钉 token 放在 `ImLearningCursor.extra["page_token"]`。

## 时序图

### 拉历史 `fetch_messages`

```mermaid
sequenceDiagram
    autonumber
    participant Caller as 调用方
    participant C as CliBackedConnector
    participant Cli as ChannelCli
    participant Runner as CliRunner
    participant Bin as 平台 CLI

    Caller->>C: fetch_messages(target, FetchOptions)
    C->>C: _ensure_self_account()
    alt 尚未解析本人
        C->>C: resolve_identity()
    end
    C->>Cli: query_history_message(kind, id, count, page_token)
    Cli->>Cli: history_args(...)
    Cli->>Runner: run(args)
    Runner->>Bin: 子进程
    Bin-->>Runner: stdout JSON
    Runner-->>Cli: CliResult
    Cli-->>C: CliResult
    C->>C: parse_history_page(stdout)
    C->>C: to_im_message(...) 填 is_self
    C-->>Caller: MessagePage
```

飞书群：`im +chat-messages-list --chat-id`；飞书单聊：`--user-id`。有下一页时 `has_more` + `next_page_token` 来自平台字段，不用 `msg_id` 冒充。

### 发消息 `send_message`（飞书群 @）

```mermaid
sequenceDiagram
    autonumber
    participant Caller as 调用方
    participant C as FeishuConnector
    participant Reply as format_outgoing_reply_text
    participant Cli as FeishuCli
    participant Bin as lark-cli

    Caller->>C: send_message(group, 正文, SendOptions)
    Note over Caller,C: extra.source_message.sender_account = 群成员 ou_
    C->>C: _ensure_self_account / resolve_identity
    C->>C: mention_open_ids / mention_token
    C->>Reply: 前缀 + at 标签 + 正文
    Reply-->>C: 来自 昵称 的数字分身：...
    alt 群
        C->>Cli: send_to_group(chat_id, text, at_open_ids)
        Cli->>Cli: --markdown 若有 at
    else 单聊
        C->>Cli: send_to_user(open_id, text, at_open_ids)
    end
    Cli->>Bin: im +messages-send --as user
    Bin-->>Cli: CliResult
    Cli-->>C: CliResult
    C-->>Caller: SendResult(ok)
```

`resolve_identity`（飞书）多一步通讯录昵称：

```mermaid
sequenceDiagram
    autonumber
    participant C as FeishuConnector
    participant Cli as FeishuCli
    participant Bin as lark-cli

    C->>Cli: auth_status()
    Cli->>Bin: auth status --json --verify
    Bin-->>C: open_id + userName(用户XXXX)
    C->>Cli: lookup_self_person()
    Cli->>Bin: contact +search-user --user-ids me --lang zh_cn
    Bin-->>C: localized_name
    C->>C: apply_self_ids(..., display_name=昵称)
    C-->>C: Identity
```

钉钉 @ 用 `<@DGU>` + `--at-open-dingtalk-ids`；飞书必须用群成员自己的 `ou_`，搜通讯录同名外部账号会导致客户端灰色 @。

### 学习拉取 `ConnectorLearningSource`

```mermaid
sequenceDiagram
    autonumber
    participant OJ as openjiuwen
    participant LS as ConnectorLearningSource
    participant Reg as ConnectorRegistry
    participant P as ChannelPlugin

    OJ->>LS: fetch_messages(ImLearningTarget, cursor)
    LS->>Reg: require(channel_id)
    Reg-->>LS: ChannelPlugin
    LS->>LS: cursor.extra.page_token → FetchOptions
    LS->>P: fetch_messages(ChannelTarget, FetchOptions)
    P-->>LS: MessagePage
    LS->>LS: ImMessage → ImLearningMessage
    LS->>LS: next_cursor = page_token 或最旧 msg_id
    LS-->>OJ: ImMessageBatch
```

学习路径 **不会** 调用 `send_message`。

### 发现会话 / 搜人 / 探测

```mermaid
sequenceDiagram
    autonumber
    participant Caller as 调用方
    participant C as CliBackedConnector
    participant Cli as ChannelCli

    alt discover_conversations
        Caller->>C: discover_conversations(query_count)
        C->>Cli: query_recent_conversations
        Note over Cli: 飞书 im +chat-list --types p2p,group
        C->>C: parse_recent_conversations
        C-->>Caller: list ChannelTarget
    else search_persons
        Caller->>C: search_persons(关键字)
        C->>Cli: search_persons
        Note over Cli: 飞书 contact +search-user --query
        C-->>Caller: list ChannelPerson
    else test_connection
        Caller->>C: test_connection()
        C->>Cli: help()
        C-->>Caller: TestResult
    end
```

## 目录对应

| 路径 | 职责 |
|---|---|
| `plugin.py` / `types.py` | ChannelPlugin 与 DTO |
| `registry.py` | 按 `feishu` / `dingtalk` / `welink` 注册 |
| `backed.py` | 共用 fetch/send/discover |
| `cli_runtime.py` | 进程、节流、429 重试；发送只重试 1 次 |
| `reply.py` / `messages.py` | 出站前缀；入站归一化 |
| `learning_source.py` | Plugin → `ImLearningSource` |
| `connectors/*/cli.py` | argv |
| `connectors/*/parser.py` | JSON → 中间 dict |
| `connectors/*/plugin.py` | 身份、@、分页解析覆盖 |
| `openjiuwen/.../im/` | 学习 Protocol 与 DTO |

当前 **没有** `im_hosting`：没有轮询、watermark、回合状态机、出站门禁 RPC。
