# 系统插件设置

系统插件通过 `application.settings` 声明设置菜单、分类和配置项。安装并激活插件后，设置页会按插件声明的位置显示相应内容。

设置页不再预置“扩展”菜单和“启动开场”设置页。插件可以提供自己的菜单，也可以向已有设置分类添加配置项。
插件提供的开场播放、预览与主题背景能力继续使用原有入口。

插件菜单未指定排序位置时，追加到已有菜单之后。旧插件声明 `after: "extensions"` 时，按缺失排序锚点的规则追加，菜单及配置项仍可正常显示。

开发声明与位置规则见 [S_18 插件设置 location 规约](../../jiuwenswarm/extensions_v2/docs/specs/S_18_settings-location.md)。

## 系统插件还原

还原会恢复已安装插件的启用状态，并重启 AgentServer、其所属 Worker 和 Gateway。正在处理的任务会中断。
已卸载的包、插件产生的文件或网络副作用不在还原范围内。

- 主页面可用时，从「设置 → 系统插件还原」进入。地址栏保留当前聊天地址。
- 主页面卡住或白屏时，在新标签页打开 Web 地址下的 `/system-plugin-recovery`。独立页直接通过 Web 服务连接恢复进程，无需加载聊天前端或连接 AgentServer、Gateway。
- Web 服务也不可用时，只要恢复进程和启动器仍在运行，管理员就可以在部署机器上读取扩展数据目录中的 `system_plugin_recovery_endpoint.json`，用其中的 `url` 在本机浏览器打开还原页。数据目录按 `extensions.data_dir`、`EXTENSION_DATA_DIR` 的优先级解析，默认是 `~/.jiuwenswarm/extension`。该地址含本机恢复令牌，请保密。远程运维时可用 SSH 将本地同一端口转发到部署机器的 `127.0.0.1` 恢复端口。

个人版 Web 未配置恢复密码时，远程客户端可通过 Web 服务的 IP 地址或域名查看存档、预览并执行还原。能访问恢复入口的客户端均可发起还原和后端重启，请将入口限制在受信网络，或启用远程恢复认证。

如需对远程恢复操作启用认证，为 Web 服务设置 `JIUWENSWARM_REMOTE_RECOVERY_PASSWORD`，将 Web 监听限制在回环地址，并由同机 HTTPS 反向代理接入。反向代理须保留公网 `Host`，将 `X-Forwarded-Proto` 覆盖为 `https`。设置密码后，远程查看和执行还原均须通过此 HTTPS 入口认证。先在浏览器打开 `/system-plugin-recovery`，用 HTTP Basic 用户名 `recovery` 和配置的密码登录。管理员也可通过 SSH 将本地同一端口转发到部署机器的 Web 回环端口，再按本机方式打开还原页。
