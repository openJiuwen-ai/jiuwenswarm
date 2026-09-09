<h1 align="center">JiuwenSwarm 文档</h1>

<p align="center">
  <strong>汇总 JiuwenSwarm 的常用使用说明与功能文档。</strong>
</p>

<p align="center">
  <a href="README.md">中文</a>
  ·
  <a href="README_EN.md">English</a>
</p>

## 文档简介

本文档汇总了 JiuwenSwarm 的常用使用说明、功能文档与开发实践，内容按照使用流程和功能类型划分为 **安装**、**基础使用**、**高阶操作**、**附录** 和 **开发实践** 五个部分。

* **安装**：面向首次使用 JiuwenSwarm 的用户，包含基础安装、环境准备、TUI 模式安装以及快速启动相关说明。
* **基础使用**：介绍日常使用中常见的功能入口，包括页面概览、对话、智能体、会话、定时任务、技能、频道、配置信息、浏览器服务、日志和 MCP 服务设置等内容。
* **高阶操作**：介绍系统的进阶能力与扩展机制，包括上下文压缩、Skill 自演进、工具权限与安全防护、E2A / A2A 协议、多智能体协作、记忆系统和 TUI 模式等内容。
* **附录**：提供与项目使用和维护相关的补充资料，包括打包 EXE、Windows 自动更新设计和开发者文档。
* **开发实践**：整理基于 JiuwenSwarm 构建的实际 Agent 应用案例，帮助开发者参考已有实践进行二次开发和能力扩展。

---

<table width="100%" style="display: table; width: 100%; table-layout: fixed;">
  <colgroup>
    <col width="22%">
    <col width="28%">
    <col width="50%">
  </colgroup>
  <tbody>
    <tr>
      <th colspan="3" align="left" bgcolor="#f3f4f6"><h3>📦 安装</h3></th>
    </tr>
    <tr>
      <th width="22%">功能模块</th>
      <th width="28%">文档链接</th>
      <th width="50%">内容</th>
    </tr>
    <tr>
      <td width="22%"><strong>安装</strong></td>
      <td width="28%"><a href="zh/安装指南.md">安装指南</a> / <a href="zh/Quickstart_tui.md">TUI 模式安装指南</a></td>
      <td width="50%">介绍 JiuwenSwarm 的基础安装、环境准备、启动方式，以及 TUI 模式的安装与运行配置。</td>
    </tr>
    <tr>
      <th colspan="3" align="left" bgcolor="#f3f4f6"><h3>📘 基础使用</h3></th>
    </tr>
    <tr>
      <th width="22%">功能模块</th>
      <th width="28%">文档链接</th>
      <th width="50%">内容</th>
    </tr>
    <tr>
      <td width="22%"><strong>快速上手</strong></td>
      <td width="28%"><a href="zh/Quickstart.md">快速开始</a></td>
      <td width="50%">新手快速启动配置、基础对话流程与常用操作。</td>
    </tr>
    <tr>
      <td width="22%"><strong>页面概览</strong></td>
      <td width="28%"><a href="zh/页面概览.md">页面概览</a></td>
      <td width="50%">Web 端界面布局、核心区域与功能入口。</td>
    </tr>
    <tr>
      <td width="22%"><strong>对话</strong></td>
      <td width="28%"><a href="zh/对话.md">对话</a></td>
      <td width="50%">Web 对话入口，支持消息发送、新建会话以及规划 / 性能 / 集群模式切换。</td>
    </tr>
    <tr>
      <td width="22%"><strong>任务规划</strong></td>
      <td width="28%"><a href="zh/任务规划.md">任务规划</a></td>
      <td width="50%">任务拆解、规划与执行管理，支持 TodoToolkit 工具进行任务跟踪。</td>
    </tr>
    <tr>
      <td width="22%"><strong>智能体</strong></td>
      <td width="28%"><a href="zh/智能体.md">智能体</a></td>
      <td width="50%">不同角色智能体、工作区创建与管理流程。</td>
    </tr>
    <tr>
      <td width="22%"><strong>会话</strong></td>
      <td width="28%"><a href="zh/会话.md">会话</a></td>
      <td width="50%">Session 信息管理、历史聊天记录查看与恢复、会话历史删除。</td>
    </tr>
    <tr>
      <td width="22%"><strong>命令行指令</strong></td>
      <td width="28%"><a href="zh/命令行指令.md">命令行指令</a></td>
      <td width="50%">受控通道指令与命令行操作指南。</td>
    </tr>
    <tr>
      <td width="22%"><strong>定时任务</strong></td>
      <td width="28%"><a href="zh/定时任务.md">定时任务</a></td>
      <td width="50%">定时触发任务的配置、运行与管理。</td>
    </tr>
    <tr>
      <td width="22%"><strong>技能</strong></td>
      <td width="28%"><a href="zh/技能.md">技能</a></td>
      <td width="50%">智能体技能挂载、调用与扩展机制。</td>
    </tr>
    <tr>
      <td width="22%"><strong>技能交响乐</strong></td>
      <td width="28%"><a href="zh/Symphony-技能编排与分发.md">技能交响乐</a></td>
      <td width="50%">技能编排与分发系统。</td>
    </tr>
    <tr>
      <td width="22%"><strong>频道</strong></td>
      <td width="28%"><a href="zh/频道.md">频道</a></td>
      <td width="50%">JiuwenSwarm 的多渠道接入与交互。</td>
    </tr>
    <tr>
      <td width="22%"><strong>配置信息</strong></td>
      <td width="28%"><a href="zh/配置信息.md">配置信息</a></td>
      <td width="50%">系统参数、大模型 API、运行环境相关配置。</td>
    </tr>
    <tr>
      <td width="22%"><strong>浏览器服务</strong></td>
      <td width="28%"><a href="zh/浏览器.md">浏览器</a></td>
      <td width="50%">网页访问、信息获取与浏览器工具调用能力。</td>
    </tr>
    <tr>
      <td width="22%"><strong>浏览器扩展</strong></td>
      <td width="28%"><a href="zh/browser-extension/浏览器扩展.md">浏览器扩展</a> / <a href="zh/browser-extension/浏览器扩展指南.md">指南</a> / <a href="zh/browser-extension/浏览器扩展安装.md">安装</a></td>
      <td width="50%">一款 Chromium 扩展，将 JiuwenSwarm 智能体带到你阅读的任意页面旁。</td>
    </tr>
    <tr>
      <td width="22%"><strong>日志</strong></td>
      <td width="28%"><a href="zh/日志.md">日志</a></td>
      <td width="50%">系统日志路径、运行记录与常见排错入口。</td>
    </tr>
    <tr>
      <td width="22%"><strong>MCP 服务设置</strong></td>
      <td width="28%"><a href="zh/MCP配置.md">MCP 配置</a></td>
      <td width="50%">外部工具接入，Model Context Protocol 相关配置。</td>
    </tr>
    <tr>
      <th colspan="3" align="left" bgcolor="#f3f4f6"><h3>⚙️ 高阶操作</h3></th>
    </tr>
    <tr>
      <th width="22%">功能模块</th>
      <th width="28%">文档链接</th>
      <th width="50%">内容</th>
    </tr>
    <tr>
      <td width="22%"><strong>上下文压缩</strong></td>
      <td width="28%"><a href="zh/上下文压缩.md">上下文压缩</a></td>
      <td width="50%">超长上下文处理、会话压缩与上下文卸载机制。</td>
    </tr>
    <tr>
      <td width="22%"><strong>单机多实例运行</strong></td>
      <td width="28%"><a href="zh/单机多实例运行.md">单机多实例运行</a></td>
      <td width="50%">在同一台机器上运行多个 JiuwenSwarm 实例的部署场景与配置。</td>
    </tr>
    <tr>
      <td width="22%"><strong>A2UI</strong></td>
      <td width="28%"><a href="zh/A2UI.md">A2UI</a></td>
      <td width="50%">Agent to UI 生成式界面协议，支持动态界面生成与交互。</td>
    </tr>
    <tr>
      <td width="22%"><strong>Skill 自演进</strong></td>
      <td width="28%"><a href="zh/Skill自演进.md">Skill 自演进</a></td>
      <td width="50%">技能迭代、自我优化与能力沉淀机制。</td>
    </tr>
    <tr>
      <td width="22%"><strong>工具权限与安全</strong></td>
      <td width="28%"><a href="zh/工具权限与安全防护.md">工具权限与安全防护</a></td>
      <td width="50%">系统命令、文件操作、工具调用中的安全拦截与权限管控。</td>
    </tr>
    <tr>
      <td width="22%"><strong>E2A</strong></td>
      <td width="28%"><a href="zh/E2A-protocol.md">E2A-protocol</a></td>
      <td width="50%">Gateway 与 AgentServer 之间的统一请求信封协议。</td>
    </tr>
    <tr>
      <td width="22%"><strong>A2A</strong></td>
      <td width="28%"><a href="zh/A2A.md">A2A</a></td>
      <td width="50%">Agent to Agent 通信协议与接入流程。</td>
    </tr>
    <tr>
      <td width="22%"><strong>Agent Team</strong></td>
      <td width="28%"><a href="zh/AgentTeam.md">Agent Teams</a> / <a href="zh/SwarmSkills.md">Swarm Skills</a> / <a href="zh/分布式Team.md">分布式 Team</a></td>
      <td width="50%">支持多智能体团队协作、团队级技能编排与复用，以及多进程分布式 Team 的运行模式。</td>
    </tr>
    <tr>
      <td width="22%"><strong>记忆</strong></td>
      <td width="28%"><a href="zh/记忆.md">记忆</a> / <a href="zh/自动记忆.md">自动记忆</a> / <a href="zh/编码记忆.md">编码记忆</a> / <a href="zh/经验记忆.md">经验记忆</a> / <a href="zh/JiuwenMemory-SDK接入.md">JiuwenMemory SDK</a></td>
      <td width="50%">支持长短期记忆管理、对话后自动提取记忆、编码场景下的专属记忆沉淀，任务经验的检索、复用与持续积累，以及以 SDK 模式接入 agent-memory 记忆引擎。</td>
    </tr>
    <tr>
      <td width="22%"><strong>TUI 模式</strong></td>
      <td width="28%"><a href="zh/SLASH_COMMAND_ARCHITECTURE.md">Slash 命令架构</a> / <a href="zh/Slash命令表.md">Slash 命令速查</a> / <a href="zh/模式系统.md">模式系统</a> / <a href="zh/TUI使用SwarmFlow指南.md">SwarmFlow（TUI）</a> / <a href="zh/自动修复PR.md">自动修复 PR</a></td>
      <td width="50%">支持 TUI 终端中的 Slash 命令体系、常用命令速查、PLAN / AGENT / CODE / TEAM 模式切换，SwarmFlow 开关、运行树查看与 HITL 回复，以及 <code>/autofix-pr</code> 自动修复 PR 直至检查通过。</td>
    </tr>
    <tr>
      <th colspan="3" align="left" bgcolor="#f3f4f6"><h3>📄 附录</h3></th>
    </tr>
    <tr>
      <th width="22%">分类</th>
      <th width="28%">文档链接</th>
      <th width="50%">内容</th>
    </tr>
    <tr>
      <td width="22%"><strong>打包 EXE</strong></td>
      <td width="28%"><a href="zh/打包exe指南.md">打包 exe 指南</a></td>
      <td width="50%">Windows 独立执行程序打包流程。</td>
    </tr>
    <tr>
      <td width="22%"><strong>自动更新</strong></td>
      <td width="28%"><a href="zh/windows自动更新设计.md">桌面端自动更新设计</a></td>
      <td width="50%">Windows 与 macOS 桌面端自动更新方案、流程与关键模块（含预发布版推送）。</td>
    </tr>
    <tr>
      <td width="22%"><strong>开发者文档</strong></td>
      <td width="28%"><a href="zh/developer_guide.md">开发者文档</a></td>
      <td width="50%">源码搭建、调试流程与二次开发资料。</td>
    </tr>
    <tr>
      <th colspan="3" align="left" bgcolor="#f3f4f6"><h3>🛠️ 开发实践</h3></th>
    </tr>
    <tr>
      <th width="22%">实践案例</th>
      <th width="28%">文档链接</th>
      <th width="50%">内容</th>
    </tr>
    <tr>
      <td width="22%"><strong>代码审查助手</strong></td>
      <td width="28%"><a href="zh/开发实践/JiuwenSwarm代码审查助手开发实践.md">代码审查助手</a></td>
      <td width="50%">代码 Review 工作流构建。</td>
    </tr>
    <tr>
      <td width="22%"><strong>日报生成器</strong></td>
      <td width="28%"><a href="zh/开发实践/JiuwenSwarm日报生成器开发实践.md">日报生成器</a></td>
      <td width="50%">自动汇总工作日报的 Agent 开发案例。</td>
    </tr>
  </tbody>
</table>
