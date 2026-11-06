# 认证与登录

在应用入口提供完整的登录与注册页面，并在侧边栏底部提供账号菜单。

## 功能

- **登录与注册**：首次进入先显示登录页，注册成功后直接登录。
- **凭证注入**：登录后自动为 HTTP 与 WebSocket 请求附加凭证。
- **账号菜单**：侧边栏底部展示当前账号、头像和退出入口。
- **退出即失效**：退出登录会清除本地凭证并撤销当前 token。

## 生效机制

1. 插件由 Gateway 加载，激活后在 `application.entry` 渲染登录页。
2. 登录成功后，插件把凭证写入当前浏览器的本地存储。
3. `requestBindings` 在请求发出前把凭证注入请求头，`gateway.request` 校验登录态。
4. 侧边栏 `application.sidebar.footer` 展示账号菜单，退出登录会撤销凭证。

## 说明

- 账号与 token 保存在插件安装副本的 `data/accounts.sqlite3`，不会写入源码包。
- 账号不参与平台数据路由，不同账号看到相同的已有项目、会话与数据。
- 本示例用于演示插件自有认证，不作为平台内置认证。

## 安装

在仓库根目录执行：

```sh
python jiuwenswarm/extensions_v2/examples/auth_login_demo/pack.py --output /tmp/webui-login-demo.zip
```

将 ZIP 放到运行 Gateway 的机器可读取的位置，通过插件详情页安装后重载。
