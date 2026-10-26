import httpx

from jiuwenswarm.extensions.agentos.auth.credential_authenticator import (
    CredentialAuthenticator,
    AuthContext,
    AuthResult,
)


class AgentOSAuthenticator(CredentialAuthenticator):
    """通过 agent-os IAM ``POST /api/v1/auth/verify`` 校验 token。
    门禁只依据响应中的 ``valid``（身份是否合法），不消费 ``authorized``
    （资源级授权留给后续能力）。
    成功时 ``AuthResult`` 字段约定（供后续会话路由 / 注册中心 / 实例创建贯通）：
    - ``user_id``: IAM 返回的用户 ID（权威身份）
    - ``extensions.username``: 用户名（可选）
    - ``extensions.role``: 角色（可选）
    - ``extensions.auth_method``: 固定为 ``"token"``
    """

    def __init__(self, auth_service_url: str,  # agent-os 后端地址，例如 "http://localhost:8000"
                 timeout: float = 10.0):  # HTTP 请求超时秒数，默认 10
        self._auth_service_url = auth_service_url.rstrip("/")
        self._timeout = timeout
        self._auth_client = httpx.AsyncClient(timeout=timeout)

    async def _authenticate_token(self, token: str,
                                  extra_headers: dict | None = None
                                  ) -> AuthResult:

        # 1. 如果 extra_headers 中有 Authorization header，优先从中提取 token, 如果token为空直接返回失败
        if extra_headers:
            lower_headers = {k.lower(): v for k, v in extra_headers.items()}
            auth_header = lower_headers.get("authorization", "")
            # header拿不到，降级使用传入的token参数
            if not auth_header:
                auth_header = token
            if auth_header.startswith("Bearer "):
                token = auth_header[7:]  # 覆盖 token 参数

        token = (token or "").strip()
        if not token:
            return AuthResult(
                success=False,
                user_id="",
                error="缺少 token",
                extensions={"error_code": "MISSING_TOKEN"},
            )

        # 2. 构建 HTTP 验证：合并自定义 header，只验证身份是否合法，不验证资源权限
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {token}",
        }
        # 不要把 WS 握手头（Host/Upgrade/authorization 等）原样并入 IAM 请求，
        # 否则可能产生重复 Authorization 或错误 Host，导致 verify 失败。
        try:
            resp = await self._auth_client.post(
                f"{self._auth_service_url}/api/v1/auth/verify",
                json={"token": token, "resource_id": "apps", "action_id": "read"},
                headers=headers,
                timeout=self._timeout,
            )

        # 3. 异常处理
        except httpx.RequestError as e:
            return AuthResult(success=False, user_id="", error=str(e))

        # 4. 解析业务响应
        try:
            body = resp.json()
        except ValueError:
            return AuthResult(success=False, extensions={"error_code": "INVALID_RESPONSE"})
        data = body.get("data", {}) if isinstance(body, dict) else {}

        # 5. 仅校验身份合法性（valid）；authorized 不参与门禁决策
        if data.get("valid"):
            return AuthResult(
                success=True,
                user_id=str(data.get("user_id") or ""),
                extensions={
                    "username": data.get("username"),
                    "role": data.get("role"),
                    "auth_method": "token",
                },
            )
        return AuthResult(
            success=False,
            error=str(data.get("error") or "Token 无效或已过期"),
        )

    async def authenticate(self, context: AuthContext) -> AuthResult:
        """支持Token、API-KEY、SSH证书等多种认证方式"""
        credentials = context.credentials or {}
        extra_headers = getattr(context, 'headers', None) or {}  # 从 context 取自定义 header

        # 1. Token认证（Web/TUI Channel）
        if "token" in credentials:
            return await self._authenticate_token(
                str(credentials.get("token") or ""),
                extra_headers,
            )

        return AuthResult(
            success=False,
            user_id="",
            error="No valid credentials",
            extensions={"error_code": "UNSUPPORTED_CREDENTIAL"},
        )