# Channels

## Concept Overview

### What is a Channel?

A **Channel** is JiuwenSwarm's message ingress layer. As a unified messaging hub, it connects various external chat platforms (such as Feishu, DingTalk, WeCom, Telegram, etc.) and provides the following core capabilities:

| Capability | Description |
|------------|-------------|
| **Message Ingestion** | Receives messages from different platforms, normalizes them, and forwards them to JiuwenSwarm for processing |
| **Multi-platform Sync** | A single JiuwenSwarm service can connect to multiple platforms simultaneously, with messages interoperable across them |
| **Identity Mapping** | Establishes unique identifiers for users on each platform and maintains independent conversation contexts |
| **Security Isolation** | Message flows across channels are isolated from each other; supports whitelists, permission controls, etc. |

### Relationship between Channels, Web Chat, and Agents

These three components form JiuwenSwarm's complete conversation system:

- **Channel**: The message ingress layer that receives and normalizes messages from different platforms. Besides external channels like Feishu and Xiaoyi, **the Web UI itself is also a built-in channel**.
- **Agent**: The message processing engine. All messages from any channel are ultimately processed by an Agent for understanding, reasoning, and response.
- **Web Chat**: A built-in web channel that provides a visual interface for users to interact directly with Agents.

**Data Flow**: User message → Any channel (Feishu/Xiaoyi/Web, etc.) → JiuwenSwarm Gateway → Agent processing → Response → Original channel → User

### Introduction

JiuwenSwarm's **Channels** are the **gateways** through which you converse with different chat platforms. JiuwenSwarm has already achieved seamless integration with **HarmonyOS Xiaoyi**, **Feishu**, and more, with continuous expansion to additional platforms. You can talk to JiuwenSwarm directly through **Feishu**, the **Xiaoyi app on HarmonyOS devices**, and others.

### Digital Avatar

JiuwenSwarm supports **Group Digital Avatar** on **Feishu** and **WeCom** channels. When enabled, the bot acts as a designated user's "digital avatar" in group chats — it automatically identifies messages relevant to that user and replies on their behalf in first person. For personal action items such as to-dos and reminders, the avatar sends the reply as a private message to the user while posting a brief confirmation in the group. Irrelevant messages are filtered out automatically, saving Agent resources.

This feature is disabled by default. See the configuration instructions under each channel below.

### Configuration Methods

This tutorial focuses on **Web UI configuration**. Subsequent channel setup instructions are all based on web interface operations.

For manual configuration, edit the `config.yaml` file (default path: `~/.jiuwenswarm/config/config.yaml`), set the corresponding channel to `enabled: true` and fill in the credentials. Changes take effect automatically upon saving.

- **Edit `config.yaml` manually** — Default location is `~/.jiuwenswarm/config/config.yaml` (created automatically on first `jiuwenswarm-start`). Set the desired channel to `enabled: true` and fill in the credentials; saving triggers an automatic reload without restarting.

### Channel Feature Differences

Different channels have varying capabilities regarding private/group chat support, trigger methods, and permission requirements:

| Feature | Description |
|---------|-------------|
| **Private/Group Chat** | Some channels only support private chat (e.g., Xiaoyi), while others support both private and group chats (e.g., Feishu, WeCom) |
| **Trigger Methods** | Private chats typically trigger directly via conversation; group chats may require @mentioning the bot or using specific command prefixes |
| **Permission Requirements** | Different platforms have varying permission requirements for bots, such as message read permissions, group management permissions, etc. |

See the detailed setup instructions for each channel for specific differences.

---

## Channel Setup

JiuwenSwarm supports integration with multiple chat platforms, divided into the following categories based on region:

### China Channels

| Channel | Description |
|---------|-------------|
| [Xiaoyi](ChinaChannels.md#xiaoyi) | Huawei HarmonyOS intelligent assistant, private chat only |
| [Feishu](ChinaChannels.md#feishu-lark) | Enterprise collaboration platform, supports private and group chat, with Digital Avatar feature |
| [DingTalk](ChinaChannels.md#dingtalk) | Enterprise collaboration platform, supports private and group chat |
| [WeCom](ChinaChannels.md#wecom-wechat-work) | Enterprise messaging tool, supports private and group chat, with Digital Avatar feature |
| [Personal WeChat](ChinaChannels.md#personal-wechat) | Personal messaging tool, private chat only |

For detailed configuration instructions, see: [China Channels](ChinaChannels.md)

### International Channels

| Channel | Description |
|---------|-------------|
| [Telegram](InternationalChannels.md#telegram) | International messaging tool, supports private and group chat |
| [Discord](InternationalChannels.md#discord) | Gaming community platform, supports private and group chat |
| [Slack](InternationalChannels.md#slack) | Enterprise collaboration platform, supports DMs and channel threads |
| [WhatsApp](InternationalChannels.md#whatsapp) | Personal messaging tool, supports private chat |

For detailed configuration instructions, see: [International Channels](InternationalChannels.md)

---

## Developer Channels

In addition to the end-user chat platforms above, JiuwenSwarm also provides integration methods for developers:

| Integration | Description |
|-------------|-------------|
| [ACP Plugin Usage](ACP_Client_Config.md) | Integrate with JiuwenSwarm via the ACP protocol, suitable for custom integrations |
