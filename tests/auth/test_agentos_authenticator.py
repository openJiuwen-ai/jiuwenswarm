"""测试 agentos_authenticator.py"""
import pytest
import httpx
# 然后再导入测试目标
from jiuwenswarm.extensions.agentos.agentos_router.agentos_authenticator import (
    AgentOSAuthenticator,
)
from jiuwenswarm.extensions.agentos.auth.credential_authenticator import (
    AuthContext,
    AuthResult,
    CredentialAuthenticator,
)


class TestAgentOSAuthenticatorInit:

    def test_init_with_required_params(self):
        """验证必填参数初始化"""
        auth = AgentOSAuthenticator(auth_service_url="http://localhost:8000")
        assert auth._auth_service_url == "http://localhost:8000"
        assert auth._timeout == 10.0

    def test_init_strips_trailing_slash(self):
        """验证初始化时去除尾部斜杠"""
        auth = AgentOSAuthenticator(auth_service_url="http://localhost:8000/")
        assert auth._auth_service_url == "http://localhost:8000"

    def test_init_with_custom_timeout(self):
        """验证自定义 timeout"""
        auth = AgentOSAuthenticator(auth_service_url="http://localhost:8000", timeout=30.0)
        assert auth._timeout == 30.0

    def test_init_creates_async_client(self):
        """验证初始化时创建了 AsyncClient"""
        auth = AgentOSAuthenticator(auth_service_url="http://localhost:8000")
        assert isinstance(auth._auth_client, httpx.AsyncClient)

    def test_is_credential_authenticator(self):
        """验证 AgentOSAuthenticator 是 CredentialAuthenticator 的子类"""
        auth = AgentOSAuthenticator(auth_service_url="http://localhost:8000")
        assert isinstance(auth, CredentialAuthenticator)


class TestAuthenticateToken:

    @pytest.fixture
    def auth(self):
        return AgentOSAuthenticator(auth_service_url="http://test-auth:8000")

    # ── 空 token 场景 ──

    @pytest.mark.asyncio
    async def test_empty_token(self, auth):
        """验证空 token 返回 MISSING_TOKEN"""
        result = await auth._authenticate_token("")
        assert result.success is False
        assert result.error == "缺少 token"
        assert result.extensions.get("error_code") == "MISSING_TOKEN"

    @pytest.mark.asyncio
    async def test_none_token(self, auth):
        """验证 None token 返回 MISSING_TOKEN"""
        result = await auth._authenticate_token(None)  # type: ignore
        assert result.success is False
        assert result.error == "缺少 token"

    @pytest.mark.asyncio
    async def test_whitespace_token(self, auth):
        """验证空白 token 返回 MISSING_TOKEN"""
        result = await auth._authenticate_token("   ")
        assert result.success is False
        assert result.error == "缺少 token"

class TestAuthenticate:

    @pytest.fixture
    def auth(self):
        return AgentOSAuthenticator(auth_service_url="http://test-auth:8000")

    @pytest.mark.asyncio
    async def test_no_credentials(self, auth):
        """验证无凭证时返回 UNSUPPORTED_CREDENTIAL"""
        context = AuthContext(channel_type="web", credentials={})
        result = await auth.authenticate(context)
        assert result.success is False
        assert result.error == "No valid credentials"
        assert result.extensions.get("error_code") == "UNSUPPORTED_CREDENTIAL"

    @pytest.mark.asyncio
    async def test_none_credentials(self, auth):
        """验证 credentials 为 None 时返回 UNSUPPORTED_CREDENTIAL"""
        context = AuthContext(channel_type="web", credentials=None)  # type: ignore
        result = await auth.authenticate(context)
        assert result.success is False
        assert result.error == "No valid credentials"