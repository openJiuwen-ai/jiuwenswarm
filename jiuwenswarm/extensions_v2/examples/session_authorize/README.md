# Session Authorization Probe

把 manifest.yaml 和 extension.py 放在 ZIP 根目录，通过既有扩展安装入口加载。包监听 `context.usage.updated`，在活动 USER Handler 内调用无参 `await ops.session.authorize()`，仅记录是否有结果和 kind，再继续原用量事件；不打印句柄，不获取或保存真实凭据，不进入 ui.render。

当前默认没有宿主凭据绑定，正常结果是 None。`{"handle": "...", "kind": "api-key"}` 或 bearer 只表示宿主已签发受管引用，不代表网络请求成功或完成新的 OAuth 授权。方法不接受 service/scopes，也不返回 token。

另一个 `session.authorize` Handler 示例继续 `next_handler(event)`，事件得到 `{"value": ...}`；如需隐藏本次结果可返回 `{"value": None}`，拒绝可返回 `{"deny": "原因"}`。Ops 会解包 value。不要在本事件内再次调用同名 Ops。

**宿主开发者**可通过 `get_authorization_service()` 取得平台服务，再显式调用 `bind(SessionContext(...), provider="openai"或"anthropic", credential=可信凭据, kind="api-key"或"bearer", expires_at=可选UTC时间戳)`。同一会话一次只保留一份绑定，切换提供方会撤销旧句柄。真实平台自动采集尚未接入；宿主须在凭据变化、会话重建/结束时显式重新绑定或 unbind。密钥不放进 SessionContext.metadata，不从用户事件参数接收。

本期没有句柄消费 Ops，不能把 handle 当作密钥传给 requests/httpx。Ops 参数、事件输入/输出和错误契约见 [F_29_session-authorize.md](../../docs/features/F_29_session-authorize.md) 与 [S_16_session-authorize.md](../../docs/specs/S_16_session-authorize.md)。
