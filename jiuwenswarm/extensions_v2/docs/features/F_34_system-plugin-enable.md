# 系统插件启用状态

## 元信息

| 项 | 内容 |
| --- | --- |
| 类型 / 编号 | feature / F_34 |
| 最近修订 | 2026-11-08 |
| 关联计划 | [EXTV2-34](../designs/extensions-v2-development-plan.md#extv2-34-系统插件启用状态) |
| 状态 | 已实现 |

## 1. 目标

系统插件安装后可以单独启用或禁用。禁用只改变运行状态，不删除安装副本；重新启用使用原来的安装副本。

## 2. 后端行为

`manifest.json.enable` 是可选布尔字段。未填写时按 `true` 处理，Core 在写入受管安装副本时补写该字段。
安装 `enable: false` 的包会通过 manifest 校验并保留到安装目录，但不会加载模块或登记 Handler。

`ExtensionManager.set_enabled(package_id, enabled)` 复用 Core 的公开管理接口。禁用会撤销 Handler 并保留
目录；启用会在写入 `true` 后登记 Handler。Gateway 使用相同语义，只处理声明为 `package.domain: gateway` 的包。

启动发现只加载 `enable: true` 的受管副本。目录和详情投影读取受管 manifest，因此已安装但禁用的包可继续被查询、展示和再次启用。
`/extension reload <package_id>` 不改变安装副本的 `enable`。已启用包按正常流程热重载；已安装但禁用的包会成功返回，
但不加载模块或登记 Handler，`enable` 仍为 `false`。需要重新生效时，应先执行启用操作。

## 3. 页面行为

扩展页仅对系统插件提供蓝色开关：插件广场中已安装卡片、我的扩展中的系统插件卡片及详情页右上角均使用同一状态。
未安装时保留“安装”按钮；安装后不再显示“已启用”文字按钮。点击卡片其他区域仍进入详情页。
前端调用 `extensions.v2.set_enabled` 后重新获取目录和详情，不在本地推断操作结果。中文和英文文案分别使用
“启用 / 禁用”和 “Enable / Disable”。

## 4. 验证

后端定向测试覆盖 `enable: false` 安装后不加载、禁用后保留安装副本、重新启用后的 Handler 登记，以及请求参数校验。
前端生产构建检查系统插件目录、我的扩展和详情页中的开关类型与翻译资源。未启动业务服务或占用端口。
