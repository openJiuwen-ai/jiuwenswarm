# 同时涉及两个运行域的系统插件

只有需求同时用到 Gateway 和 AgentServer 的事件或 Ops 时，才按本文开发组合包。
一个域已经能完成需求时，沿用单域结构；不要仅因有前端页面就拆成两个域。

## 1. 按职责拆分成员

- **Gateway 成员：** 处理应用入口、连接、请求路由、应用外观或桌面形态等 Gateway 能力。
- **AgentServer 成员：** 处理会话执行、工具、上下文、团队或 AgentServer 的页面贡献。
- **整体交付：** 对用户仍是一个系统插件，生成一个 ZIP；内部成员各自声明真实 domain，并由所属进程加载。

先看 [Harness 工作台示例](../assets/examples/harness-workbench/README.md) 的根 manifest，再按需要读取 `plugins/` 中对应成员的入口。
它把 Harness 选择、会话界面和 Gateway 请求处理、主题切换放在同一组合包中，不是跨进程共享一个 Ops 对象。

## 2. 声明和打包

```text
system-plugin/
├── manifest.json
├── README.md
└── plugins/
    ├── session-member/
    │   ├── manifest.json
    │   └── plugin/extension.py
    └── gateway-member/
        ├── manifest.json
        └── plugin/extension.py
```

根声明的必要结构如下，展示信息按实际功能填写：

```json
{
  "schemaVersion": 1,
  "package_type": "extension",
  "is_bundle": true,
  "package": {"id": "example.workbench", "version": "0.0.1"},
  "display": {"name": {"zh": "工作台", "en": "Workbench"}},
  "members": [
    {"path": "plugins/session-member"},
    {"path": "plugins/gateway-member"}
  ]
}
```

- **根声明：** 不写 `package.domain`，也不在根声明 Handler 入口。使用 `is_bundle: true` 和非空 `members`。
- **成员声明：** 每个成员保留独立 ID、版本、单个 domain、入口、UI 和实际依赖，沿用单域系统插件契约。成员 ID 互不相同，也不能与根 ID 相同。
- **路径与依赖：** 成员放在根目录内的独立子目录，不嵌套组合包。真实依赖写在成员的 `requires.packages`；不因两个成员在一起就增加无关依赖，不制造环。
- **版本与修改：** 根与成员分别核对版本及身份。正式升级按主流程保留旧源码；本轮验收调试保持各自 ID、domain 和版本不变。整体 README 说明两域分工和协作入口。

对根目录运行同一份 `scripts/finalize_package.py`。打包脚本用 Core 的 `read_bundle_spec` 和 `dependency_levels` 校验成员及依赖，并核对每个成员入口确实包含在 ZIP 中。
测试从 `JIUWEN_SYSTEM_PLUGIN_ZIP` 取得最终文件，先验证根声明，再用两域的真实加载器和临时宿主验证成员；还要验证两域协作的实际数据流。
只验证一个成员通过，不能称为整个系统插件通过。

## 3. 安装与验收

安装的是整个 ZIP，不让用户分别安装成员，也不为绕过校验给根声明补一个假的 domain。

- **先核对入口：** 当前 Swarm 终端 CLI 的 `inspect_zip` 仍要求根 `package.domain`，不能直接安装此类组合包；当前已有的 Web `/extension install <ZIP 绝对路径>` 和市场入口走组合包管理流程。目标版本更新后，以其实际 CLI 实现为准。
- **自动安装受限：** 用户要求自动安装，而当前终端入口不支持组合包时，说明这一具体缺口，保留已验证的 ZIP，并说明已有安装入口；不另写管理客户端、不拆成两次单域安装，也不把交付 ZIP 写成安装完成。
- **核对整体：** 检查根 ID、版本、`overallStatus` 以及每个成员的 domain、状态、实际目录和报告。`pending`、`partial`、`failed` 不能写成全部生效；有意处于 `standby` 的成员要核对同版本活动实例和归属。
- **本轮调整：** 依据安装结果定位根副本及成员副本，再走宿主支持的组合包 reload，检查两个域；不只重载一个成员就宣布全部完成。
- **用户验收：** 全部必要成员就绪后，再验证协作效果；涉及界面时沿用主流程的用户确认循环。
