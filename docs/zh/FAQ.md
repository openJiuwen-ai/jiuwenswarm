# JiuwenSwarm 常见问题

> **版本同步**：本文档应与英文版 [`docs/en/FAQ.md`](../en/FAQ.md) 保持同步。更新一版时请同时更新另一版。

---

## 安装与环境

### Q: 启动时报错 "Python version not supported"

请确保 Python 版本 ≥3.11 且 <3.14（推荐 3.11 或 3.12）。

```bash
python --version
```

如版本不符，请安装正确版本后重试。

### Q: 启动时报错 "Node.js not found"

JiuwenSwarm 前端依赖 Node.js，请安装 18.x 或更高版本。

```bash
node --version
```

下载地址：[https://nodejs.org](https://nodejs.org)

### Q: pip install 速度很慢或超时

建议使用国内镜像源：

```bash
# 清华源（推荐）
pip install jiuwenswarm -i https://pypi.tuna.tsinghua.edu.cn/simple

# 阿里云源
pip install jiuwenswarm -i https://mirrors.aliyun.com/pypi/simple/
```

### Q: 如何查看当前安装的版本？

```bash
jiuwenswarm --version
```

或：

```bash
pip show jiuwenswarm
```

### Q: 如何卸载 JiuwenSwarm？

```bash
pip uninstall jiuwenswarm
```

---

## 模型配置

### Q: 支持哪些模型提供商？

JiuwenSwarm 支持多种模型平台：华为云 MaaS、OpenAI、DeepSeek、DashScope、SiliconFlow、InferenceAffinity、OpenRouter 等 OpenAI 兼容接口，也支持本地模型部署。

### Q: 模型配置测试失败怎么办？

请逐项检查：

- **API Key**：是否正确且未过期
- **API Base URL**：是否可访问，注意不要包含 `/chat/completions` 后缀
- **模型名称**：是否与提供商一致，如 `gpt-4o`、`deepseek-chat`
- **model_provider**：是否选择了正确的提供商类型

### Q: api_base 应该怎么填写？

填写服务商提供的 API 地址，**无需包含 `/chat/completions` 后缀**，系统会自动补齐。

示例：

| 提供商 | api_base |
|--------|----------|
| OpenAI | `https://api.openai.com/v1` |
| DeepSeek | `https://api.deepseek.com` |
| DashScope | `https://dashscope.aliyuncs.com/compatible-mode/v1` |

### Q: 保存模型配置后需要重启吗？

点击保存后，后端会自动重启以加载新配置，无需手动操作。

---

## 启动与运行

### Q: 启动后无法访问前端页面？

1. 确认服务已正常启动，终端应显示：

```
[INFO] API server running at http://localhost:8000
[INFO] Web server running at http://localhost:5173
```

2. 在浏览器中访问 `http://localhost:5173`
3. 如端口被占用，可指定自定义端口：

```bash
jiuwenswarm-web --host 0.0.0.0 --port <自定义端口>
```

### Q: 如何在远程服务器上使用？

启动时绑定外部可访问地址：

```bash
jiuwenswarm-web --host 0.0.0.0 --port <自定义端口>
jiuwenswarm-app
```

然后通过 `http://<服务器IP>:<端口>` 访问。

### Q: TUI 模式如何启动？

TUI 需要单独安装，在启动 JiuwenSwarm 后另开终端：

```bash
pip install jiuwenswarm-tui
jiuwenswarm-tui
```

---

## 版本升级

### Q: 如何升级 JiuwenSwarm？

**常规升级**（如 0.2.0 → 0.2.1）：

```bash
pip install --upgrade jiuwenswarm
```

**重大版本升级**（跨 0.1.7 版本）：

1. 备份数据：

| 数据类型 | 路径 | 说明 |
|---------|------|------|
| 记忆数据 | `~/.jiuwenswarm/agent/memory` | 对话记忆 |
| 自定义技能 | `~/.jiuwenswarm/agent/skills` | 自定义 Skill |
| 配置文件 | `~/.jiuwenswarm/config` | 应用设置 |

2. 升级并重新初始化：

```bash
pip install --upgrade jiuwenswarm
jiuwenswarm-init
```

3. 迁移数据：将备份的数据复制回对应目录

### Q: 升级后服务无法启动怎么办？

如跨重大版本升级，需重新运行 `jiuwenswarm-init` 初始化配置。初始化后请检查配置文件是否需要更新。

---

## 使用相关

### Q: 三种执行模式怎么选？

| 模式 | 适用场景 |
|------|---------|
| 规划模式 | 复杂任务，需要分步执行并确认每步结果 |
| 性能模式 | 简单任务，快速响应 |
| 集群模式 | 大型复杂任务，需要多 Agent 专业分工协同（默认） |

### Q: 什么时候需要清空会话？

| 场景 | 说明 |
|------|------|
| 话题切换 | 想开始一个完全不同的新话题 |
| 上下文混乱 | 对话内容过多，模型理解出现偏差 |
| 重复/错误回复 | 模型陷入循环或不相关回复 |
| 隐私清理 | 会话中包含临时敏感信息 |

### Q: Skill 自演进是如何工作的？

当执行出错或用户表达不满时，系统会自动检测信号并优化 Skill 定义，让能力越用越强。无需手动干预。

---

## 更多帮助

- **文档导航**：[docs/README.md](../README.md)
- **问题反馈**：[GitCode Issues](https://gitcode.com/openJiuwen/jiuwenswarm/issues)
- **社区交流**：关注 openJiuwen 社区活动