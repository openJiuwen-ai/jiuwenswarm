"""测试 credential_authenticator.py 的数据模型和抽象基类"""
import pytest

from jiuwenswarm.extensions.agentos.auth.credential_authenticator import (
    AuthContext,
    AuthResult,
    CredentialAuthenticator,
)


class TestAuthContext:

    def test_create_with_all_fields(self):
        """构造完整字段的 AuthContext"""
        context = AuthContext(
            channel_type="web",
            credentials={"token": "test-token"},
            headers={"User-Agent": "test"},
            remote_addr="127.0.0.1",
        )
        assert context.channel_type == "web"
        assert context.credentials == {"token": "test-token"}
        assert context.headers == {"User-Agent": "test"}
        assert context.remote_addr == "127.0.0.1"

    def test_default_values(self):
        """验证默认值均为空"""
        context = AuthContext()
        assert context.channel_type == ""
        assert context.credentials == {}
        assert context.headers == {}
        assert context.remote_addr == ""

    def test_mutable_defaults_are_independent(self):
        """验证 field(default_factory=dict) 每个实例独立"""
        ctx1 = AuthContext()
        ctx2 = AuthContext()
        ctx1.credentials["key"] = "value"
        assert "key" not in ctx2.credentials

    def test_channel_type_enum_values(self):
        """验证 channel_type 的合法值"""
        for ct in ("web", "tui", "ssh", ""):
            ctx = AuthContext(channel_type=ct)
            assert ctx.channel_type == ct


class TestAuthResult:

    def test_success_result(self):
        """成功场景，含 user_id 和 extensions"""
        result = AuthResult(
            success=True,
            user_id="user-123",
            extensions={"role": "admin"},
        )
        assert result.success is True
        assert result.user_id == "user-123"
        assert result.error == ""
        assert result.extensions == {"role": "admin"}

    def test_failure_result(self):
        """失败场景，含 error 信息"""
        result = AuthResult(success=False, error="Token 无效")
        assert result.success is False
        assert result.error == "Token 无效"
        assert result.user_id == ""
        assert result.extensions == {}

    def test_default_values(self):
        """验证默认值均为空"""
        result = AuthResult(success=True)
        assert result.user_id == ""
        assert result.error == ""
        assert result.extensions == {}

    def test_extensions_mutable_default_independence(self):
        """验证 extensions 实例独立"""
        r1 = AuthResult(success=True)
        r2 = AuthResult(success=True)
        r1.extensions["key"] = "val"
        assert "key" not in r2.extensions

    def test_success_without_user_id(self):
        """success=True 但未提供 user_id 的场景"""
        result = AuthResult(success=True)
        assert result.success is True
        assert result.user_id == ""

    def test_failure_with_extensions(self):
        """失败时仍可携带扩展信息"""
        result = AuthResult(
            success=False,
            error="rate limit",
            extensions={"retry_after": 30},
        )
        assert result.success is False
        assert result.extensions["retry_after"] == 30


class TestCredentialAuthenticator:

    def test_cannot_instantiate_abstract(self):
        """抽象类不能直接实例化"""
        with pytest.raises(TypeError):
            CredentialAuthenticator()  # type: ignore[abstract]

    def test_concrete_subclass_can_instantiate(self):
        """子类实现 authenticate 后可实例化"""
        class SimpleAuthenticator(CredentialAuthenticator):
            async def authenticate(self, context):
                return AuthResult(success=True, user_id="test")

        auth = SimpleAuthenticator()
        assert isinstance(auth, CredentialAuthenticator)

    @pytest.mark.asyncio
    async def test_authenticate_returns_auth_result(self):
        """验证 authenticate 返回 AuthResult"""
        class TestAuth(CredentialAuthenticator):
            async def authenticate(self, context):
                return AuthResult(
                    success=True,
                    user_id="user-abc",
                    extensions={"method": "test"},
                )

        auth = TestAuth()
        ctx = AuthContext(
            channel_type="web",
            credentials={"token": "xxx"},
        )
        result = await auth.authenticate(ctx)
        assert isinstance(result, AuthResult)
        assert result.success is True
        assert result.user_id == "user-abc"
        assert result.extensions["method"] == "test"

    @pytest.mark.asyncio
    async def test_authenticate_failure_path(self):
        """验证鉴权失败路径"""
        class FailingAuth(CredentialAuthenticator):
            async def authenticate(self, context):
                return AuthResult(success=False, error="invalid token")

        auth = FailingAuth()
        ctx = AuthContext(credentials={"token": "bad"})
        result = await auth.authenticate(ctx)
        assert result.success is False
        assert result.error == "invalid token"

    @pytest.mark.asyncio
    async def test_authenticate_receives_context_correctly(self):
        """验证 authenticate 正确接收 AuthContext"""
        class EchoAuth(CredentialAuthenticator):
            async def authenticate(self, context):
                return AuthResult(
                    success=True,
                    user_id=context.credentials.get("token", "none"),
                    extensions={"channel": context.channel_type},
                )

        auth = EchoAuth()
        ctx = AuthContext(
            channel_type="ssh",
            credentials={"token": "ssh-user"},
            remote_addr="10.0.0.1",
        )
        result = await auth.authenticate(ctx)
        assert result.user_id == "ssh-user"
        assert result.extensions["channel"] == "ssh"