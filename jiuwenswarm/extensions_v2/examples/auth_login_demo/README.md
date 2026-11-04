# 登录与注册示例插件

[English](README_EN.md)

示例源码集中在 `jiuwenswarm/extensions_v2/examples/auth_login_demo`；`manifest.yaml` 中的 `domain: gateway` 决定其运行宿主。

`webui.login-demo` 是 Gateway 扩展包。全屏登录/注册页、表单规则、左右或居中布局、
渐变背景、WorkSwarm 宣传图、左下角头像菜单及退出登录均由本包声明。
使用 `application.entry` 与 `application.sidebar.footer`，没有登录专用宿主组件。
Box.gap 仅使用现有 `lg` 档位；菜单位置直接声明 AntD placement/align。
Form.requiredMark=false 隐藏用户名和密码标签前的星号，必填校验保留。
插件从账号名生成大写首字素并通过 Avatar.props.children 下发；完整账号名确定背景色，插件通过原生 style 声明背景、白字和字号，刷新及侧栏/菜单之间保持一致。
Unicode 字素分割使用 regex 的 \X；当前运行环境通过 Core → tiktoken 依赖链提供 regex。宿主不生成账号首字母或配色。
需要自定义头像时传入 Avatar.url，图片优先于首字母；不设置图片时省略 url。
两个输入框声明 autoComplete=off，表示不使用浏览器自动填充。该原生属性是浏览器偏好提示，
Chrome 密码管理器可以忽略它；保存密码和已保存账号建议仍由浏览器控制，页面不能保证彻底关闭。

## 当前边界

需要工作区中的本地 Gateway 浏览器接入，见 [本地接入说明](../../../gateway/extensions_v2/docs/features/F_07_local-login-integration.md)。
前端、Gateway 接入和本示例分别提交；本示例由安装包显式启用，不作为平台内置认证。
鉴权由本包的 gateway.request USER handler 完整负责，包括 token 校验与未登录请求的 deny；宿主没有内置认证门禁或 auth.status。
前端使用本次通用组件、client.declarations 接入和 localStorage/requestBindings 能力。
仅安装 ZIP 到未补接入的旧 Gateway，不能运行本包。

Mock 支持创建多个账号。没有预置账号，注册成功后直接登录。
用户名 4–64 个字符；密码 8–128 个字符，包含英文字母、数字和特殊字符。
账号及 token 持久化在**安装副本**的 `data/accounts.sqlite3`，不打入 ZIP；密码只保存随机盐和摘要。
token 首版不会自动过期，退出登录撤销该 token；注销不会删除账号。
账号不参与平台数据路由，因此不同账号看到相同的现有项目、会话和数据，不提供账号隔离保证。

## 打包

组件属性遵循 [S_17](../../docs/specs/S_17_webui-component-props.md)：按钮文案使用 value，
文字使用 type / strong，头像地址使用 src；Form.Item.label 与菜单项 label 继续保留。
修改仓库源码后需重新打包并更新安装副本，单独 reload 不会复制新源码；Python 页面模块更新后重启测试 Gateway。
更新时保留安装副本的 data/accounts.sqlite3。

在仓库根目录执行，输出到你指定的本地位置：

```sh
.venv/bin/python jiuwenswarm/extensions_v2/examples/auth_login_demo/pack.py --output /tmp/webui-login-demo.zip
```

ZIP 根包含 manifest.yaml、settings.json、auth_login_demo/、assets/ 和中英文说明。
不包含账号数据库、缓存、测试或运行日志。

## 本地 Gateway 安装

使用已包含本次浏览器接入的 Gateway。安装并激活后，插件可直接保存登录状态、绑定请求凭据和刷新页面，
无需修改 Gateway 配置、声明额外权限或再次授权。Gateway 与前端需同步更新至本次浏览器协议。

通过已有 `/extension install /tmp/webui-login-demo.zip` 安装，或使用 GatewayPackageManager.install。
启用包之后需要登录，安装管理操作请在启动加载之前完成。
Gateway 与 AgentServer 是不同宿主，本包声明 domain: gateway。
渲染调用正式 ops.ui.render(UiRenderInput(...))，使用 Form.Item 节点；头像菜单使用 ui.select，登录按钮使用 ui.click。
本包自行校验每个业务请求；Gateway 内置门禁与异常处理沿用已合入的后端策略。

安装副本 settings.json 支持 language 为 zh/en、layout 为 split/center。
修改设置后通过 reload 重新激活；修改 Python 页面模块后需要重启测试 Gateway，以加载新的模块内容。
窄屏在小于 992px 时隐藏宣传区，表单保留居中。

## 行为

1. 没有有效 token 的业务请求被插件拒绝，Gateway 单独下发 entry 登录树。
2. 点击“没有账号？创建账号”切换注册，Form 在失焦及按钮确认时本地校验。
3. 点击“注册并登录”创建账号和初始会话，二者在同一个 SQLite 事务中写入；字段不合法或用户名已存在时停留注册页，不写入浏览器凭据。
4. 注册或登录成功，插件下发 client.local_storage.set("login", {accessToken, username})，随后显式下发 client.page.reload()，无需重复填写表单。
5. 前端执行后重新读取业务，HTTP header 与 WS request_context.headers 按 manifest 绑定 Authorization。
6. 插件校验 token，clear entry，并为 footer 下发头像菜单。插件用原生 styles.root.width=200 声明菜单宽度，autoFocus=false 避免鼠标展开时自动高亮操作项，destroyOnHidden=true 避免重新展开时保留上次的焦点高亮。菜单依次为 20px 头像与正文色的当前用户名、原生分隔线、“退出登录”；用户信息行使用 disabled=true 仅展示，label 通过 Box、Avatar、Text 组合，原平台正常处理对话请求。
7. 点击“退出登录”撤销当前 token，成功后下发 delete 和显式刷新；由新页面的业务请求触发登录树，不在刷新前重复渲染登录页。
8. 可使用已创建的任意账号再次登录，或注册其他账号；浏览器刷新可恢复有效 token。

没有 ui.submit、浏览器存储查询 ops、回执、条目版本或宿主写死的 auth 字段。
agent-core 当前只支持两层 Ops，本包通过 with_client_ops(ops) 组合出三层 local_storage 调用。

## 验证

验证状态由 [F_07](../../../gateway/extensions_v2/docs/features/F_07_local-login-integration.md) 维护。
需要实际启动工程并加载安装副本，检查注册、登录、聊天 token、刷新与退出后再次登录。
Mock 验证不等同于真实身份服务、生产级认证或账号数据隔离验收。
