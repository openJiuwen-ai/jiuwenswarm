from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.session.session_history import resolve_session_dir


@pytest.fixture
def patched_sessions_root(tmp_path, monkeypatch):
    """把 get_agent_sessions_dir 指向临时目录，隔离真实用户数据。"""
    sessions_root = tmp_path / "agent" / "sessions"
    sessions_root.mkdir(parents=True)
    import jiuwenswarm.server.runtime.session.session_history as sh

    monkeypatch.setattr(sh, "get_agent_sessions_dir", lambda: sessions_root)
    return sessions_root


# ---------------------------------------------------------------------------
# 合法 session_id：必须能解析到 <sessions_root>/<id>
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("session_id", [
    "default",
    "acp_1a2b3c4d",
    "heartbeat_19fa3218917_d32bc8",
    "sess_19fa67ef575_2c1f05a964a8",
    "default_code",
    # 合法字符 . - 在白名单 [A-Za-z0-9_.-] 内
    "a.b.c",
    "good-name",
    "with.dot-and_dash",
    # Gateway internal sessions use a double-underscore namespace.
    "__cron__",
    "__cron__execution_123",
])
def test_legit_id_resolves_to_sessions_dir(patched_sessions_root, session_id):
    p, err = resolve_session_dir(session_id)
    assert err is None, f"legit id rejected: {session_id}"
    assert p == (patched_sessions_root / session_id).resolve(strict=False)


def test_legit_id_create_flag_makes_directory(patched_sessions_root):
    p, err = resolve_session_dir("new_session_123", create=True)
    assert err is None
    assert p is not None
    assert p.is_dir()


def test_legit_id_default_create_does_not_create(patched_sessions_root):
    # create 默认 False，不应创建目录
    p, err = resolve_session_dir("not_yet_existing")
    assert err is None
    assert p is not None
    assert not p.exists()


def test_create_does_not_mkdir_before_rejecting_traversal(patched_sessions_root, monkeypatch, tmp_path):
    """白名单被绕过时，create=True 必须先经 relative_to 越界校验、拒绝，
    不得在拒绝前用 mkdir 越界创建目录。

    锁定 resolve+relative_to 必须在 mkdir 之前执行的顺序契约：曾出现过
    "先 mkdir 再校验" 的顺序，导致白名单被绕过时越界空目录已创建、事后才返回
    (None, err)，残留文件系统副作用。本测试防止顺序回退。
    """
    # 让白名单失效（模拟被绕过），强制走第二道 relative_to 防线
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.is_valid_session_id",
        lambda _s: True,
    )

    # sessions_root 之外的目标，调用前必须不存在
    outside_target = (patched_sessions_root / "../escape_via_create").resolve(strict=False)
    assert not outside_target.exists()

    # create=True 也不得越界创建：必须被 relative_to 拦截、返回 None
    p, err = resolve_session_dir("../escape_via_create", create=True)
    assert p is None, f"traversal payload with create=True should be rejected, got {p}"
    assert err is not None
    # 关键断言：拒绝后磁盘上不得残留越界目录
    assert not outside_target.exists(), (
        f"mkdir created directory outside sessions_root before rejection: {outside_target}"
    )


# ---------------------------------------------------------------------------
# 路径遍历 payload：必须拒绝（返回 None + error），根本不碰路径
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("session_id", [
    "..",
    "../",
    "../config",
    "../..",
    "../../..",
    "/etc",
    "/",
    "a/b",
    "a\\b",          # Windows 反斜杠
    "..\\",
    "..\\..\\tmp",
    " ..\\hidden",  # 前导空格（白名单会把空格替换成 _，前后不等 → 拒绝）
    "trailing/..",
    "dir/../../../etc",
])
def test_traversal_payload_rejected(patched_sessions_root, session_id):
    p, err = resolve_session_dir(session_id)
    assert p is None, f"LEAK: payload resolved to path: {session_id!r} -> {p}"
    assert err is not None


def test_empty_and_none_rejected(patched_sessions_root):
    assert resolve_session_dir("")[0] is None
    assert resolve_session_dir("   ")[0] is None


def test_whitespace_only_id_rejected(patched_sessions_root):
    p, err = resolve_session_dir("    ")
    assert p is None
    assert err is not None


# ---------------------------------------------------------------------------
# 纵深防御：白名单被绕过时，resolve() 越界校验仍拦截。
# ---------------------------------------------------------------------------

def test_second_line_defense_blocks_outside_root(patched_sessions_root, monkeypatch, tmp_path):
    """白名单被绕过时，resolve + relative_to 第二道防线仍拦截越界路径。"""
    # 在 sessions_root 之外制造一个真实目录（攻击目标）
    outside = tmp_path / "outside_target"
    outside.mkdir()

    # 让白名单失效，模拟"白名单被绕过"的假设场景
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.is_valid_session_id",
        lambda _s: True,
    )

    # 确认白名单确实被绕过（否则后续断言无意义）
    # 即便白名单放过，../outside_target 解析后落在 sessions_root 之外，必须被拒
    p, err = resolve_session_dir("../outside_target")
    assert p is None, f"second line of defense failed: {p}"
    assert err is not None
    # 外部目录不受影响
    assert outside.exists()


# ---------------------------------------------------------------------------
# handler 调用链模拟：先 strip 再 resolve，与三个 delete handler 真实顺序一致
# ---------------------------------------------------------------------------

def test_handler_call_chain_strip_then_resolve(patched_sessions_root):
    """三个 delete handler 都是 target = str(...).strip() 后调用 resolve_session_dir。"""
    # 带空格的合法 id strip 后合法
    p, err = resolve_session_dir("  default  ".strip())
    assert err is None
    assert p == (patched_sessions_root / "default").resolve(strict=False)

    # 带空格的恶意 payload strip 后仍恶意
    p, err = resolve_session_dir("   ../config   ".strip())
    assert p is None
    assert err is not None


# ---------------------------------------------------------------------------
# 边界值：. 和 .. 必须被拒（sanitize 会把它们映射成 hash，sanitize(s) != s）
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("session_id", [".", ".."])
def test_dot_and_dotdot_rejected(patched_sessions_root, session_id):
    """单个 . 和 .. 是路径遍历的根载体，必须被白名单拒绝。"""
    p, err = resolve_session_dir(session_id)
    assert p is None, f"LEAK: {session_id!r} resolved to {p}"
    assert err is not None


@pytest.mark.parametrize(
    "session_id",
    [
        ".hidden",
        "-hidden",
        "hidden.",
        "hidden-",
    ],
)
def test_reserved_edge_character_rejected(patched_sessions_root, session_id):
    """Dots and hyphens may occur only inside a session id."""
    p, err = resolve_session_dir(session_id)
    assert p is None
    assert err == "invalid session_id"


@pytest.mark.parametrize("session_id", ["_hidden", "hidden_", "__cron__"])
def test_underscore_edge_character_accepted(patched_sessions_root, session_id):
    """Leading/trailing underscores are safe and used by internal session ids."""
    p, err = resolve_session_dir(session_id)
    assert err is None
    assert p == (patched_sessions_root / session_id).resolve(strict=False)


def test_default_fallback_value_is_accepted(patched_sessions_root):
    """sanitize 的空输入回退值 default 本身是合法 id，应通过。"""
    p, err = resolve_session_dir("default")
    assert err is None
    assert p == (patched_sessions_root / "default").resolve(strict=False)


# ---------------------------------------------------------------------------
# 超长 ID：sanitize 对 >80 字符截断，截断后 != 原值 → 白名单应拒绝
# 这锁定"超长输入被拒"行为，防止有人改白名单让超长 ID 静默通过。
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("length", [81, 200, 1000])
def test_overlong_id_rejected(patched_sessions_root, length):
    """超过 80 字符的纯字母 ID：sanitize 截断后 != 原值，白名单拒绝。"""
    session_id = "a" * length
    p, err = resolve_session_dir(session_id)
    assert p is None, f"LEAK: len={length} resolved to {p}"
    assert err is not None


def test_max_length_id_accepted(patched_sessions_root):
    """刚好 80 字符（sanitize 不截断的边界）应通过，验证阈值准确。"""
    session_id = "a" * 80
    p, err = resolve_session_dir(session_id)
    assert err is None
    assert p is not None
    assert p.name == session_id


# ---------------------------------------------------------------------------
# 真实 symlink 越界：sessions 目录内放 symlink 指向外部目录，
# 验证 resolve() + relative_to 第二道防线拦截符号链接逃逸。
# ---------------------------------------------------------------------------

def test_symlink_escape_blocked(patched_sessions_root, tmp_path, monkeypatch):
    """sessions_root 内建 symlink 指向外部目录，resolve 后必须被拒。"""
    # symlink 的链接名合法（字母数字），白名单会放行；重点测第二道防线。
    # 用 monkeypatch 让白名单放行，强制走 resolve 越界检查
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.is_valid_session_id",
        lambda _s: True,
    )

    # 在 sessions_root 之外造一个真实目录（攻击目标）
    outside = tmp_path / "outside_real_target"
    outside.mkdir()
    (outside / "secret.txt").write_text("do not delete me")

    # 在 sessions_root 内建 symlink 指向外部目录
    link_name = "escape_link"
    link_path = patched_sessions_root / link_name
    try:
        link_path.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("当前环境不支持创建 symlink（Windows 需管理员/开发者模式）")

    # 即便 symlink 名字合法、白名单放行，resolve 跟随链接后落在 sessions_root 之外，必须被拒
    p, err = resolve_session_dir(link_name)
    assert p is None, f"symlink escape LEAK: {link_name} -> {p}"
    assert err is not None
    # 外部目录与文件完好
    assert outside.exists()
    assert (outside / "secret.txt").exists()
