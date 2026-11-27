<div align="center">

# JiuwenClaw

> 随叫随到的智能管家，让AI触手可及

[![Python Version](https://img.shields.io/badge/python-3.11%2C3.12%2C3.13-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![华为云MaaS](https://img.shields.io/badge/华为云-MaaS-red)](https://www.huaweicloud.com/)

</div>

## 🌟 项目简介

**JiuwenClaw** 是一款基于Python开发的智能AI Agent，正如其名——"Claw"象征着精准的抓取与连接。它能够将大语言模型的强大能力，通过你日常使用的各类通讯应用，直接延伸至你的指尖。

### ✨ 核心特色

- **生态兼容**：完美支持**华为云MaaS**等主流模型平台
- **无缝对接**：与**小艺开放平台**无缝接入，华为手机用户可通过小艺直接唤醒
- **灵活部署**：支持自托管部署，数据完全自主可控
- **多端接入**：支持Web端、聊天软件等多种交互方式

## 🎯 核心理念

> **懂你所想，自主演进**

### 🤝 贴身任务管家
面对复杂的输入场景——任务追加、指令打断、需求修改，JiuwenClaw都能精准理解，为你智能排期，有条不紊地完成任务。

### 🔄 自主演进
当你表达不满或运行出错时，它会根据你的反馈自动调整相应技能，持续演进，全心全意为你服务。

<p align="center">
  <strong>⚡ 一个始终在线、数据自主的专属AI助理 ⚡</strong>
</p>

## ⚠️ 版本升级提醒

**如果您从旧版本升级到 v0.1.7：**

由于本版本包含重大变更，升级后**必须**重新初始化 JiuwenClaw，否则服务将无法启动。

### 升级前备份数据

| 数据类型 | 原路径 | 说明 |
|---------|--------|------|
| 记忆数据 | `.jiuwenclaw/workspace/agent/memory` | 所有对话记忆 |
| 自定义技能 | `.jiuwenclaw/workspace/agent/skills` | 您的自定义技能 |
| 配置文件 | `.jiuwenclaw/config` | 应用设置 |

### 数据迁移步骤

升级并运行 `jiuwenclaw-init` 后，请手动迁移数据：

1. **迁移记忆数据**：将原目录下的 `.jiuwenclaw/workspace/agent/memory` 复制到 `.jiuwenclaw/agent/memory`

2. **迁移技能数据**：将原目录下的 `.jiuwenclaw/workspace/agent/skills` 复制到 `.jiuwenclaw/agent/skills`

## 🚀 快速上手

### 📦 安装

```bash
# 安装 JiuwenClaw
pip install jiuwenclaw

# 初始化 JiuwenClaw (首次启动)
jiuwenclaw-init

# 启动 JiuwenClaw
jiuwenclaw-start
```

### 💬 使用方式

#### 1️⃣ 对话模式

| 方式 | 说明                                        |
|------|-------------------------------------------|
| **Web前端** | 启动服务后访问 `http://localhost:5173`，通过浏览器直接对话 |
| **小艺频道** | 华为手机用户可直接唤醒小艺，与JiuwenClaw对话               |
| **飞书频道** | 完成渠道配置后，在飞书中与JiuwenClaw畅聊                 |

#### 2️⃣ 配置模型

在 Web 页面左侧找到「配置信息」，进入配置页面：

![](docs/assets/images/jiuwenclaw_configuration_Info.png)

完善以下四项基本配置，完成后点击右上角「保存」：

![](docs/assets/images/jiuwenclaw_config_api.png)

#### 3️⃣ 开始对话

在 Web 页面左侧找到「对话」，输入问题即可开始：

![](docs/assets/images/jiuwenclaw_example.png)

#### 4️⃣ 会话管理

点击下方的「+」号，可清空当前会话并开启新会话：

![](docs/assets/images/jiuwenclaw_new_session.png)

清理后页面显示：

![](docs/assets/images/jiuwenclaw_clear_session.png)

#### 5️⃣ 定时任务

设置心跳任务，填写待办事项，JiuwenClaw即可定时被唤醒，自动执行预设任务。让你的日程管理更加智能高效！

#### 6️⃣ 清空记忆

当你需要让 JiuwenClaw 忘记之前的所有对话历史和用户信息时，可以清空记忆文件。

**适用场景：**
- **隐私保护**：清除包含敏感信息的历史记录
- **全新开始**：开始一个完全不同的项目或话题，避免历史信息干扰
- **调试排错**：记忆文件损坏或内容异常时重置
- **用户切换**：多用户共用环境时，清除上一个用户的信息

**清空记忆操作步骤：**

记忆文件存储在 `{workspace_dir}/memory/` 目录下：

**方式一：通过 Agent 删除**
直接告诉 JiuwenClaw："请删除所有记忆文件" 或 "清空我的记忆"，Agent 会调用文件工具删除 memory 目录下的文件。
![](docs/assets/images/jiuwenclaw_delete_memory.png)

**方式二：手动删除**
停止 JiuwenClaw 服务后，直接删除 `memory/` 目录下的所有 Markdown 文件即可。
![](docs/assets/images/jiuwenclaw_memory.png)

> ⚠️ **注意**：清空记忆后无法恢复，请谨慎操作。建议定期备份重要的记忆文件。

## 📚 文档导航

| 文档                                  | 核心内容                     |
|:------------------------------------|:-------------------------|
| [📖 快速开始](docs/zh/Quickstart.md)             | 5分钟上手JiuwenClaw          |
| [⚙️ 配置与工作空间](docs/zh/配置信息.md)          | 环境配置与工作区管理               |
| [📁 工作区结构](docs/zh/智能体.md)             | workspace 目录说明，预置与动态生成内容 |
| [🛠️ 技能系统](docs/zh/技能.md)              | 自定义技能开发指南                |
| [📱 频道配置](docs/zh/频道.md)               | 飞书、小艺等频道接入               |
| [⌨️ 命令行指令](docs/zh/命令行指令.md)           | 命令行工具使用指南                |
| [⏰ 定时任务](docs/zh/定时任务.md)             | 定时任务管理                    |
| [🧠 记忆功能](docs/zh/记忆.md)               | 智能记忆与学习                  |
| [🌐 浏览器相关](docs/zh/浏览器.md)             | 自动化浏览功能                  |
| [📋 任务规划](docs/zh/任务规划.md)        | 任务规划与待办事项                |
| [🔄 Skill自演进](docs/zh/Skill自演进.md) | Skill自演进机制                 |
| [📦 上下文压缩](docs/zh/上下文压缩卸载.md)  | 上下文压缩与卸载                 |
| [🚀 开发实践](docs/zh/开发实践/) | 开发实践与经验分享                |

## 🤝 参与贡献

我们热烈欢迎社区贡献！无论是提交Bug、提出新功能建议，还是完善文档，都是对项目的宝贵支持。

1. Fork 本仓库
2. 创建您的特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交您的改动 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 打开一个 Pull Request

## 📄 开源协议

本项目采用 **Apache License 2.0** 开源协议，详情请参阅 [LICENSE](LICENSE) 文件。

---

<p align="center">
  <strong>让智能触手可及，让生活更加简单</strong><br>
  <sub>✨ JiuwenClaw —— 您的专属AI助理 ✨</sub>
</p>