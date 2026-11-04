# server.run_command 示例

将 manifest.yaml、extension.py 放在 ZIP 根目录，通过既有扩展安装入口加载。向 Worker 原生文本命令入口发送 `/project_status demo`，USER Handler 查询本次绑定 cwd，返回项目文字；不调用 next，因此不执行原生命令。context 中的备注通过平台 ContextEngine/Session 作为隐藏用户消息保存，供后续模型轮次读取。

该事件不新增前端命令菜单；前端或 Gateway 自己消费的斜杠命令不会因此自动转发到 Worker。原生文本入口只覆盖既有 DeepAdapter 路径，详情、完整调用链和字段见 [F_30](../../docs/features/F_30_server-run-command.md)。示例不依赖 ui.render。
