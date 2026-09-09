<h1 align="center">JiuwenSwarm Docs</h1>

<p align="center">
  <strong>This page collects common JiuwenSwarm usage instructions and feature documentation.</strong>
</p>

<p align="center">
  <a href="README_EN.md">English</a>
  ·
  <a href="README.md">中文</a>
</p>

## Documentation Overview

This page collects common JiuwenSwarm usage instructions, feature documentation, and development practices. The content is organized into five sections: **Installation**, **Basic Usage**, **Advanced Operations**, **Appendix**, and **Development Practices**.

* **Installation**: For first-time JiuwenSwarm users, covering basic installation, environment preparation, TUI mode installation, and quick start guidance.
* **Basic Usage**: Introduces common daily-use entry points, including page overview, conversation, agents, sessions, scheduled tasks, skills, channels, configuration, browser service, logs, and MCP service settings.
* **Advanced Operations**: Covers advanced capabilities and extension mechanisms, including context compression, Skill self-evolution, tool permissions and security, E2A / A2A protocols, multi-agent collaboration, memory systems, and TUI mode.
* **Appendix**: Provides supplementary materials for project usage and maintenance, including EXE packaging, Windows auto-update design, and developer documentation.
* **Development Practices**: Collects real Agent application cases built with JiuwenSwarm, helping developers reference existing practices for secondary development and capability extension.

---

<table width="100%" style="display: table; width: 100%; table-layout: fixed;">
  <colgroup>
    <col width="22%">
    <col width="28%">
    <col width="50%">
  </colgroup>
  <tbody>
    <tr>
      <th colspan="3" align="left" bgcolor="#f3f4f6"><h3>📦 Installation</h3></th>
    </tr>
    <tr>
      <th width="22%">Feature</th>
      <th width="28%">Documentation</th>
      <th width="50%">Description</th>
    </tr>
    <tr>
      <td width="22%"><strong>Installation</strong></td>
      <td width="28%"><a href="en/InstallGuide.md">Install Guide</a> / <a href="en/Quickstart_tui.md">TUI Mode Install Guide</a></td>
      <td width="50%">Introduces JiuwenSwarm basic installation, environment preparation, startup methods, and TUI mode installation and runtime configuration.</td>
    </tr>
    <tr>
      <th colspan="3" align="left" bgcolor="#f3f4f6"><h3>📘 Basic Usage</h3></th>
    </tr>
    <tr>
      <th width="22%">Feature</th>
      <th width="28%">Documentation</th>
      <th width="50%">Description</th>
    </tr>
    <tr>
      <td width="22%"><strong>Quick Start</strong></td>
      <td width="28%"><a href="en/Quickstart.md">Quick Start</a></td>
      <td width="50%">Beginner-friendly startup configuration, basic conversation flow, and common operations.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Page Overview</strong></td>
      <td width="28%"><a href="en/Page-Overview.md">Page Overview</a></td>
      <td width="50%">Web UI layout, core areas, and feature entry points.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Conversation</strong></td>
      <td width="28%"><a href="en/Conversation.md">Conversation</a></td>
      <td width="50%">Web conversation entry point, supporting message sending, new sessions, and planning / performance / cluster mode switching.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Agent</strong></td>
      <td width="28%"><a href="en/Agent.md">Agent</a></td>
      <td width="50%">Agents with different roles, workspace creation, and workspace management flows.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Session</strong></td>
      <td width="28%"><a href="en/Session.md">Session</a></td>
      <td width="50%">Session information management, viewing and restoring historical chats, and deleting session history.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Scheduled Tasks</strong></td>
      <td width="28%"><a href="en/ScheduledTasks.md">Scheduled Tasks</a></td>
      <td width="50%">Configuration, execution, and management of scheduled tasks.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Skills</strong></td>
      <td width="28%"><a href="en/Skills.md">Skills</a></td>
      <td width="50%">Agent skill mounting, invocation, and extension mechanisms.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Skill Symphony</strong></td>
      <td width="28%"><a href="en/Symphony.md">Skill Symphony</a></td>
      <td width="50%">Introduces skill orchestration and dispatch system.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Channels</strong></td>
      <td width="28%"><a href="en/Channels.md">Channels</a></td>
      <td width="50%">JiuwenSwarm multi-channel access and interaction.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Configuration</strong></td>
      <td width="28%"><a href="en/Configuration.md">Configuration</a></td>
      <td width="50%">System parameters, LLM APIs, and runtime environment configuration.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Browser Service</strong></td>
      <td width="28%"><a href="en/Browser.md">Browser</a></td>
      <td width="50%">Web access, information retrieval, and browser tool invocation capabilities.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Browser Extension</strong></td>
      <td width="28%"><a href="en/browser-extension/BrowserExtension.md">Browser Extension</a> / <a href="en/browser-extension/BrowserExtensionGuide.md">Guide</a> / <a href="en/browser-extension/BrowserExtensionInstall.md">Install</a></td>
      <td width="50%">A Chromium extension that puts the JiuwenSwarm agent beside any page you read.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Logs</strong></td>
      <td width="28%"><a href="en/Logs.md">Logs</a></td>
      <td width="50%">System log paths, runtime records, and common troubleshooting entry points.</td>
    </tr>
    <tr>
      <td width="22%"><strong>MCP Service Settings</strong></td>
      <td width="28%"><a href="en/MCPConfiguration.md">MCP Configuration</a></td>
      <td width="50%">External tool integration and Model Context Protocol configuration.</td>
    </tr>
    <tr>
      <th colspan="3" align="left" bgcolor="#f3f4f6"><h3>⚙️ Advanced Operations</h3></th>
    </tr>
    <tr>
      <th width="22%">Feature</th>
      <th width="28%">Documentation</th>
      <th width="50%">Description</th>
    </tr>
    <tr>
      <td width="22%"><strong>Context Compression</strong></td>
      <td width="28%"><a href="en/ContextCompression.md">Context Compression and Offload</a></td>
      <td width="50%">Long-context handling, conversation compression, and context offload mechanisms.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Skill Self-Evolution</strong></td>
      <td width="28%"><a href="en/SkillSelfEvolution.md">Skill Self-Evolution</a></td>
      <td width="50%">Skill iteration, self-optimization, and capability accumulation mechanisms.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Tool Permissions and Security</strong></td>
      <td width="28%"><a href="en/ToolPermissionsSecurity.md">Tool Permissions and Security</a></td>
      <td width="50%">Security interception and permission control for system commands, file operations, and tool calls.</td>
    </tr>
    <tr>
      <td width="22%"><strong>E2A</strong></td>
      <td width="28%"><a href="en/E2A-protocol.md">E2A Protocol</a></td>
      <td width="50%">Unified request envelope protocol between Gateway and AgentServer.</td>
    </tr>
    <tr>
      <td width="22%"><strong>A2A</strong></td>
      <td width="28%"><a href="en/A2A.md">A2A</a></td>
      <td width="50%">Agent-to-Agent communication protocol and integration flow.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Agent Team</strong></td>
      <td width="28%"><a href="en/AgentTeam.md">Agent Teams</a> / <a href="en/SwarmSkills.md">Swarm Skills</a> / <a href="en/DistributedTeam.md">Distributed Team</a></td>
      <td width="50%">Supports multi-agent team collaboration, team-level skill orchestration and reuse, and multi-process distributed Team runtime mode.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Memory</strong></td>
      <td width="28%"><a href="en/Memory.md">Memory</a> / <a href="en/AutoMemory.md">Auto Memory</a> / <a href="en/CodingMemory.md">Coding Memory</a> / <a href="en/TaskMemory.md">Task Memory</a></td>
      <td width="50%">Supports short-term and long-term memory management, automatic post-conversation memory extraction, code-specific memory accumulation, and task experience retrieval, reuse, and continuous accumulation.</td>
    </tr>
    <tr>
      <td width="22%"><strong>TUI Mode</strong></td>
      <td width="28%"><a href="en/SlashCommandArchitecture.md">Slash Command Architecture</a> / <a href="en/SlashCommands.md">Slash Command Reference</a> / <a href="en/Modes.md">Mode System</a> / <a href="en/TUISwarmFlowGuide.md">SwarmFlow (TUI)</a></td>
      <td width="50%">TUI slash commands, PLAN / AGENT / CODE / TEAM mode switching, and SwarmFlow toggle, run-tree viewer, and HITL replies.</td>
    </tr>
    <tr>
      <th colspan="3" align="left" bgcolor="#f3f4f6"><h3>📄 Appendix</h3></th>
    </tr>
    <tr>
      <th width="22%">Category</th>
      <th width="28%">Documentation</th>
      <th width="50%">Description</th>
    </tr>
    <tr>
      <td width="22%"><strong>Package EXE</strong></td>
      <td width="28%"><a href="en/PackExeGuide.md">Package EXE Guide</a></td>
      <td width="50%">Windows standalone executable packaging flow.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Auto Update</strong></td>
      <td width="28%"><a href="en/WindowsAutoUpdateDesign.md">Desktop Auto-Update Design</a></td>
      <td width="50%">Windows and macOS desktop auto-update design, flow, and key modules (including pre-release pushes).</td>
    </tr>
    <tr>
      <td width="22%"><strong>Developer Documentation</strong></td>
      <td width="28%"><a href="en/developer_guide.md">Developer Guide</a></td>
      <td width="50%">Source setup, debugging flow, and secondary development materials.</td>
    </tr>
    <tr>
      <th colspan="3" align="left" bgcolor="#f3f4f6"><h3>🛠️ Development Practices</h3></th>
    </tr>
    <tr>
      <th width="22%">Practice</th>
      <th width="28%">Documentation</th>
      <th width="50%">Description</th>
    </tr>
    <tr>
      <td width="22%"><strong>Code Review Assistant</strong></td>
      <td width="28%"><a href="en/development-practices/JiuwenSwarm-Code-Review-Assistant.md">Code Review Assistant</a></td>
      <td width="50%">Building a code review workflow.</td>
    </tr>
    <tr>
      <td width="22%"><strong>Daily Report Generator</strong></td>
      <td width="28%"><a href="en/development-practices/JiuwenSwarm-Daily-Report-Generator.md">Daily Report Generator</a></td>
      <td width="50%">Agent development case for automatically summarizing daily work reports.</td>
    </tr>
  </tbody>
</table>
