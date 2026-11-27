<div align="center">

# JiuwenClaw

> Your On-Call AI Butler — Bringing Intelligence to Your Fingertips

[![Python Version](https://img.shields.io/badge/python-3.11%2C3.12%2C3.13-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0-green)](LICENSE)
[![Huawei Cloud MaaS](https://img.shields.io/badge/华为云-MaaS-red)](https://www.huaweicloud.com/)

</div>

## 🌟 Overview

**JiuwenClaw** is an intelligent AI Agent built in Python. True to its name — "Claw" symbolizes precise reach and connection — it extends the power of large language models directly to your fingertips through the communication apps you already use every day.

### ✨ Key Features

- **Ecosystem Compatible**: Full support for **Huawei Cloud MaaS** and other mainstream model platforms
- **Seamless Integration**: Native integration with the **Xiaoyi Open Platform**, enabling Huawei phone users to invoke JiuwenClaw directly through the Xiaoyi assistant
- **Flexible Deployment**: Self-hosted deployment with full data sovereignty
- **Multi-Platform Access**: Interact via web interface, messaging apps, and more

## 🎯 Design Philosophy

> **Understands You. Evolves With You.**

### 🤝 Your Personal Task Butler

Whether dealing with task additions, mid-flow interruptions, or shifting requirements, JiuwenClaw understands your intent precisely — intelligently scheduling and executing tasks in an orderly, stress-free manner.

### 🔄 Autonomous Evolution

When you express dissatisfaction or an error occurs, JiuwenClaw automatically refines the relevant skills based on your feedback — continuously improving, always working in your best interest.


<p align="center">
  <strong>⚡ Your always-on, data-sovereign personal AI assistant ⚡</strong>
</p>

## ⚠️ Version Upgrade Notice

**If you're upgrading from an earlier version to v0.1.7:**

Due to breaking changes in this release, you **must** reinitialize JiuwenClaw after upgrading. The service will fail to start without reinitialization.

### Backup Before Upgrading

| Data Type | Source Path | Description |
|-----------|-------------|-------------|
| Memory Data | `.jiuwenclaw/workspace/agent/memory` | All your conversation memories |
| Custom Skills | `.jiuwenclaw/workspace/agent/skills` | Your custom agent skills |
| Configuration | `.jiuwenclaw/config` | Your app settings |

### Migration Steps

After upgrading and running `jiuwenclaw-init`, manually migrate your data:

1. **Copy Memory:**
   ```bash
   cp -r .jiuwenclaw/workspace/agent/memory .jiuwenclaw/agent/memory
   ```

2. **Copy Skills:**
   ```bash
   cp -r .jiuwenclaw/workspace/agent/skills .jiuwenclaw/agent/skills
   ```

## 🚀 Getting Started

### 📦 Installation

```bash
# Install JiuwenClaw
pip install jiuwenclaw

# Initialize JiuwenClaw (first-time setup or after upgrading)
# ⚠️ Remember to backup your data before running this command
jiuwenclaw-init

# Start JiuwenClaw
jiuwenclaw-start
```

### 💬 How to Use

#### 1️⃣ Conversation Mode

| Method             | Description                                                  |
| ------------------ | ------------------------------------------------------------ |
| **Web Frontend**   | After starting the service, visit `http://localhost:5173` to chat directly in your browser |
| **Xiaoyi Channel** | Huawei phone users can invoke Xiaoyi to talk with JiuwenClaw directly |
| **Lark Channel** | Once configured, chat with JiuwenClaw seamlessly inside Lark |

#### 2️⃣ Scheduled Tasks

Set up heartbeat tasks with your to-do items, and JiuwenClaw will wake up on schedule to execute them automatically — making your time management smarter and more effortless.

## 📚 Documentation

| Document                                             | Description                                              |
| :--------------------------------------------------- | :------------------------------------------------------- |
| [📖 Quick Start](docs/en/Quickstart.md)              | Get up and running with JiuwenClaw in 5 minutes          |
| [⚙️ Configuration & Workspace](docs/en/Configuration.md) | Environment setup and workspace management               |
| [📁 Workspace Structure](docs/en/Agent.md)           | workspace directory layout, presets, and dynamic content |
| [🛠️ Skill System](docs/en/Skills.md)                 | Guide to developing custom skills                        |
| [📱 Channel Configuration](docs/en/Channels.md)      | Integrating Feishu, Xiaoyi, and other channels           |
| [⌨️ CLI Commands](docs/en/CLI.md)                    | Command-line tool usage guide                            |
| [⏰ Scheduled Tasks](docs/en/ScheduledTasks.md)      | Scheduled task management                                |
| [🧠 Memory](docs/en/Memory.md)                       | Intelligent memory and learning capabilities             |
| [🌐 Browser Automation](docs/en/Browser.md)          | Web browsing and automation features                     |
| [📋 Task Planning](docs/en/TaskPlanning.md)          | Chat behavior and task flow                              |
| [🔄 Skill Self-Evolution](docs/en/SkillSelfEvolution.md) | Mechanism for automatic skill evolution                  |
| [📦 Context Compression](docs/en/ContextCompression.md) | Context compression and unloading                        |
| [🚀 Development Practices](docs/en/development-practices/README.md) | Development practices and experience sharing             |

## 🤝 Contributing

We warmly welcome community contributions — whether it's filing bug reports, suggesting new features, or improving documentation, every bit of support means the world to us.

1. Fork the repository
2. Create your feature branch (`git checkout -b feature/AmazingFeature`)
3. Commit your changes (`git commit -m 'Add some AmazingFeature'`)
4. Push to the branch (`git push origin feature/AmazingFeature`)
5. Open a Pull Request

## 📄 License

This project is licensed under the **Apache License 2.0**. See the [LICENSE](LICENSE) file for details.


---

<p align="center">
  <strong>Making intelligence accessible. Making life simpler.</strong><br>
  <sub>✨ JiuwenClaw — Your Personal AI Assistant ✨</sub>
</p>
