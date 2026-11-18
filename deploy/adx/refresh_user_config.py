#!/usr/bin/env python3.11
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""刷新单个逻辑用户的 jiuwenswarm 配置。

流程：
  1. 备份 <user>/.jiuwenswarm/config/config.yaml
  2. 用 root 模板覆盖
  3. 保留备份中的 models.agentos
  4. 属主改为 agentos、权限 0600

--all 模式：对 home-base 下所有已初始化用户重复 1-4。

运行要求：root、python3.11（已安装 jiuwenswarm）、目标用户已由
`agentos.sh create-user` 初始化。

退出码：
  0  成功
  1  参数/前置校验/配置合并失败（目标配置未改动；--all 时表示有用户失败）

测试方式：Runtime 只保留 6 个外部 I/O，测试用 dataclasses.replace 替换即可，
无需 root、网络和 docker。
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import pwd
except ImportError:  # pragma: no cover - Windows 本地测试
    pwd = None  # type: ignore[assignment]

DEFAULT_HOME_BASE = "/home/agentos/users"
DEFAULT_TEMPLATE = "/root/.jiuwenswarm/config/config.yaml"
DEFAULT_OS_USER = "agentos"
USERNAME_RE = re.compile(r"^[a-z0-9_-]{3,32}$")

ABSENT = "absent"
MERGED = "merged"
EMPTY = "empty"
INVALID = "invalid"


class FatalError(Exception):
    """参数、前置校验或配置合并失败，退出码 1。"""


class _StreamHandler(logging.Handler):
    """按级别写入当前的 sys.stdout / sys.stderr，兼容输出重定向与测试捕获。"""

    def __init__(self) -> None:
        super().__init__()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        stream = sys.stderr if record.levelno >= logging.WARNING else sys.stdout
        stream.write(self.format(record) + "\n")
        stream.flush()


_LOG = logging.getLogger("refresh_user_config")
_LOG.setLevel(logging.INFO)
_LOG.propagate = False
_LOG.addHandler(_StreamHandler())


def _paint(stream, code: str, text: str) -> str:
    if not stream.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def info(message: str) -> None:
    _LOG.info(_paint(sys.stdout, "36", f"=== {message} ==="))


def success(message: str) -> None:
    _LOG.info(_paint(sys.stdout, "32", f"✅ {message}"))


def warning(message: str) -> None:
    _LOG.warning(_paint(sys.stderr, "33", f"⚠️  {message}"))


def error(message: str) -> None:
    _LOG.error(_paint(sys.stderr, "31", f"❌ {message}"))


# ---------------------------------------------------------------------------
# 纯函数
# ---------------------------------------------------------------------------


def validate_username(username: str) -> bool:
    return bool(USERNAME_RE.fullmatch(username or ""))


def backup_candidate(user_cfg: Path, stamp: str, exists: Callable[[Path], bool]) -> Path:
    candidate = user_cfg.with_name(f"{user_cfg.name}.bak.{stamp}")
    seq = 1
    while exists(candidate):
        candidate = user_cfg.with_name(f"{user_cfg.name}.bak.{stamp}-{seq}")
        seq += 1
    return candidate


def extract_agentos(backup_data: Any) -> tuple[str, list[Any] | None]:
    """返回 (status, entries)，status 为 absent / merged / empty。"""
    if not isinstance(backup_data, dict):
        raise FatalError("备份配置顶层不是映射")
    models = backup_data.get("models")
    if models is not None and not isinstance(models, dict):
        raise FatalError("备份配置 models 不是映射")
    if not isinstance(models, dict) or "agentos" not in models:
        return ABSENT, None
    source = models.get("agentos")
    if source is None:
        return ABSENT, None
    if not isinstance(source, list):
        raise FatalError("备份配置 models.agentos 不是列表")
    for index, entry in enumerate(source):
        if not isinstance(entry, dict):
            raise FatalError(f"models.agentos[{index}] 不是映射")
    return (MERGED if source else EMPTY), source


def normalize_agentos(entries: list[Any]) -> None:
    """agentos 条目不允许 is_default=true；运行时本就会强制 false。"""
    for entry in entries:
        if entry.get("is_default") is True:
            entry["is_default"] = False


def apply_agentos(target_data: Any, entries: list[Any]) -> dict[str, Any]:
    if target_data is None:
        target_data = {}
    if not isinstance(target_data, dict):
        raise FatalError("模板配置顶层不是映射")
    models = target_data.get("models")
    if models is None:
        models = {}
        target_data["models"] = models
    elif not isinstance(models, dict):
        raise FatalError("模板配置 models 不是映射")
    models["agentos"] = entries
    return target_data


# ---------------------------------------------------------------------------
# 可注入 I/O
# ---------------------------------------------------------------------------


def _default_chown(path: Path, uid: int, gid: int) -> None:
    if not hasattr(os, "chown"):
        raise FatalError("当前平台不支持 chown（需在 Linux 上运行）")
    os.chown(path, uid, gid)


def _default_geteuid() -> int:
    getter = getattr(os, "geteuid", None)
    return getter() if callable(getter) else 1


def _default_is_tty() -> bool:
    return sys.stdin.isatty()


def _default_get_user_ids(name: str) -> tuple[int, int]:
    if pwd is None:
        raise FatalError("当前平台不支持 pwd，无法解析 agentos 用户")
    try:
        entry = pwd.getpwnam(name)
    except KeyError:
        raise FatalError(
            f"系统用户 {name} 不存在（请先执行 agentos.sh up 或 create-user）"
        ) from None
    return entry.pw_uid, entry.pw_gid


def _default_load_round(path: Path) -> Any:
    from jiuwenswarm.common.config import load_yaml_round_trip

    return load_yaml_round_trip(path)


def _default_dump_round(path: Path, data: Any) -> None:
    from jiuwenswarm.common.config import dump_yaml_round_trip

    dump_yaml_round_trip(path, data)


@dataclass
class Runtime:
    """脚本外部 I/O 的最小集合；测试按需 replace。"""

    chown: Callable[[Path, int, int], None] = _default_chown
    geteuid: Callable[[], int] = _default_geteuid
    get_user_ids: Callable[[str], tuple[int, int]] = _default_get_user_ids
    load_round: Callable[[Path], Any] = _default_load_round
    dump_round: Callable[[Path, Any], None] = _default_dump_round
    is_tty: Callable[[], bool] = _default_is_tty


@dataclass(frozen=True)
class Options:
    username: str
    home_base: Path
    template: Path
    dry_run: bool = False
    yes: bool = False
    all_users: bool = False
    os_user: str = DEFAULT_OS_USER


@dataclass(frozen=True)
class Context:
    username: str
    cfg_dir: Path
    user_cfg: Path
    template: Path
    uid: int
    gid: int


# ---------------------------------------------------------------------------
# 步骤
# ---------------------------------------------------------------------------


def _load_plain(path: Path) -> Any:
    import yaml

    with open(path, "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def preflight(options: Options, rt: Runtime) -> Context:
    if not options.username:
        raise FatalError("未指定用户。用法: ./refresh-user-config.sh <username> [options]")
    if rt.geteuid() != 0:
        raise FatalError("必须以 root 运行（需要修改文件属主）")
    if not validate_username(options.username):
        raise FatalError(
            f"用户名格式无效: '{options.username}'"
            "（要求 3-32 位小写字母/数字/下划线/连字符）"
        )

    template = Path(options.template).expanduser()
    if not template.is_file():
        raise FatalError(f"模板配置不存在: {template}")

    user_home = Path(options.home_base).expanduser() / options.username
    cfg_dir = user_home / ".jiuwenswarm" / "config"
    user_cfg = cfg_dir / "config.yaml"
    if not user_home.is_dir():
        raise FatalError(
            f"用户家目录不存在: {user_home}"
            f"（请先执行 agentos.sh create-user {options.username}）"
        )
    if not cfg_dir.is_dir():
        raise FatalError(
            f"配置目录不存在: {cfg_dir}"
            f"（请先执行 agentos.sh create-user {options.username}）"
        )
    if user_cfg.is_symlink():
        raise FatalError(f"目标配置是符号链接，请先人工确认: {user_cfg}")
    if user_cfg.is_file() and user_cfg.resolve() == template.resolve():
        raise FatalError(f"模板与目标配置是同一个文件，拒绝执行: {user_cfg}")

    uid, gid = rt.get_user_ids(options.os_user)
    return Context(options.username, cfg_dir, user_cfg, template, uid, gid)


def confirm_targets(header: str, lines: list[str], rt: Runtime) -> bool:
    """打印摘要并等待确认；非交互式环境要求显式 -y。"""
    if not rt.is_tty():
        raise FatalError("非交互式环境请显式指定 -y/--yes")
    info(header)
    for line in lines:
        _LOG.info(line)
    try:
        answer = input("确认执行? [y/N] ").strip().lower()
    except EOFError as exc:
        raise FatalError("未读到确认输入，请显式指定 -y/--yes") from exc
    if answer not in {"y", "yes"}:
        warning("已取消")
        return False
    return True


def confirm(ctx: Context, options: Options, rt: Runtime) -> bool:
    if options.dry_run or options.yes:
        return True
    return confirm_targets(
        "即将刷新用户配置",
        [
            f"  用户        : {ctx.username}",
            f"  目标配置    : {ctx.user_cfg}",
            f"  模板        : {ctx.template}",
        ],
        rt,
    )


def backup(ctx: Context, rt: Runtime) -> Path | None:
    if not ctx.user_cfg.is_file():
        warning(f"目标配置不存在，将直接按模板新建: {ctx.user_cfg}")
        return None
    stamp = datetime.now(timezone.utc).astimezone().strftime("%Y%m%d-%H%M%S")
    target = backup_candidate(ctx.user_cfg, stamp, Path.exists)
    shutil.copy2(ctx.user_cfg, target)
    rt.chown(target, ctx.uid, ctx.gid)
    os.chmod(target, 0o600)
    success(f"已备份: {target}")
    return target


def merge(backup_path: Path | None, target: Path, rt: Runtime) -> tuple[str, int]:
    backup_data = _load_plain(backup_path) if backup_path is not None else {}
    status, entries = extract_agentos(backup_data)
    if status not in (MERGED, EMPTY):
        return status, 0

    normalize_agentos(entries)
    rt.dump_round(target, apply_agentos(rt.load_round(target), entries))

    written_status, written_entries = extract_agentos(_load_plain(target))
    if written_status not in (MERGED, EMPTY) or written_entries != entries:
        raise FatalError("合并结果与备份 models.agentos 不一致")
    return status, len(entries)


def install(ctx: Context, tmp_path: Path, rt: Runtime) -> None:
    rt.chown(tmp_path, ctx.uid, ctx.gid)
    os.chmod(tmp_path, 0o600)
    os.replace(tmp_path, ctx.user_cfg)

    stat = ctx.cfg_dir.stat()
    if (stat.st_uid, stat.st_gid) != (ctx.uid, ctx.gid):
        rt.chown(ctx.cfg_dir, ctx.uid, ctx.gid)
    success(f"配置已更新: {ctx.user_cfg}（agentos:agentos 0600）")


def print_dry_run(ctx: Context) -> None:
    status, count = ABSENT, 0
    if ctx.user_cfg.is_file():
        try:
            status, entries = extract_agentos(_load_plain(ctx.user_cfg))
            count = len(entries) if entries else 0
        except FatalError:
            status = INVALID

    info(f"[dry-run] 用户: {ctx.username}")
    info(f"[dry-run] 目标配置: {ctx.user_cfg}")
    info(f"[dry-run] 模板: {ctx.template}")
    if ctx.user_cfg.is_file():
        info(f"[dry-run] 将备份到: {ctx.user_cfg}.bak.<YYYYmmdd-HHMMSS>")
    else:
        warning("目标配置不存在，将按模板新建")
    if status == MERGED:
        info(f"[dry-run] 将恢复 models.agentos（{count} 条）")
    elif status == EMPTY:
        info("[dry-run] 备份中 models.agentos 为空列表，将保留空列表")
    elif status == INVALID:
        warning("备份中的 models.agentos 结构异常，实际执行会失败")
    else:
        info("[dry-run] 备份中无 models.agentos，将保留模板原值")
    info("[dry-run] 将写入 owner agentos、mode 0600")


def refresh_config(ctx: Context, rt: Runtime) -> tuple[str, int, Path | None]:
    """备份 + 模板覆盖 + 回填 models.agentos + 落盘。"""
    backup_path = backup(ctx, rt)
    tmp_path = ctx.cfg_dir / f"config.yaml.new.{os.getpid()}"
    try:
        shutil.copy2(ctx.template, tmp_path)
        info("拷贝模板并合并 models.agentos")
        status, count = merge(backup_path, tmp_path, rt)
        install(ctx, tmp_path, rt)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise

    if status == MERGED:
        info(f"已从备份恢复 models.agentos（{count} 条）")
    elif status == EMPTY:
        info("备份中的 models.agentos 为空列表，已保留空列表")
    else:
        info("备份中无 models.agentos，保留模板原值")
    return status, count, backup_path


def discover_users(options: Options) -> list[str]:
    """列出 home_base 下已初始化（存在 .jiuwenswarm/config）的逻辑用户。"""
    home_base = Path(options.home_base).expanduser()
    if not home_base.is_dir():
        raise FatalError(f"用户根目录不存在: {home_base}")
    users: list[str] = []
    for entry in sorted(home_base.iterdir()):
        if not entry.is_dir() or not USERNAME_RE.match(entry.name):
            continue
        if not (entry / ".jiuwenswarm" / "config").is_dir():
            continue
        users.append(entry.name)
    return users


def print_all_dry_run(users: list[str], options: Options) -> None:
    home_base = Path(options.home_base).expanduser()
    info(f"[dry-run] 用户根目录: {home_base}")
    info(f"[dry-run] 模板: {options.template}")
    info(f"[dry-run] 将刷新 {len(users)} 个用户:")
    for username in users:
        _LOG.info(f"  - {username}: {home_base / username / '.jiuwenswarm' / 'config' / 'config.yaml'}")


def confirm_all(users: list[str], options: Options, rt: Runtime) -> bool:
    if options.dry_run or options.yes:
        return True
    return confirm_targets(
        f"即将刷新 {len(users)} 个用户的配置",
        [f"  - {username}" for username in users]
        + [f"  模板        : {options.template}"],
        rt,
    )


def run_all(options: Options, rt: Runtime) -> int:
    """刷新所有已初始化用户的配置。"""
    if options.username:
        raise FatalError("--all 不接受用户名参数")
    if rt.geteuid() != 0:
        raise FatalError("必须以 root 运行（需要修改文件属主）")

    users = discover_users(options)
    if not users:
        info(f"{Path(options.home_base).expanduser()} 下没有已初始化的用户，跳过")
        return 0
    info(f"发现 {len(users)} 个用户: {', '.join(users)}")

    if options.dry_run:
        print_all_dry_run(users, options)
        return 0
    if not confirm_all(users, options, rt):
        return 0

    updated: list[Context] = []
    failures: list[str] = []
    for username in users:
        info(f"刷新用户 {username}")
        try:
            ctx = preflight(replace(options, username=username), rt)
            refresh_config(ctx, rt)
        except Exception as exc:  # noqa: BLE001
            # 单个用户的任何异常都不能中断批量刷新（YAML 解析错误、ruamel 写入异常、
            # 意外类型错误等）。KeyboardInterrupt 属于 BaseException，仍会正常中断。
            if isinstance(exc, FatalError):
                message = str(exc)
            elif isinstance(exc, OSError):
                message = f"文件操作失败: {exc}"
            else:
                message = f"{type(exc).__name__}: {exc}"
            error(f"{username}: {message}")
            failures.append(f"{username}: {message}")
            continue
        updated.append(ctx)

    _LOG.info("")
    info("配置刷新结果")
    _LOG.info(f"  成功: {len(updated)} / {len(users)}")
    for line in failures:
        _LOG.info(f"  失败: {line}")

    return 1 if failures else 0


def run(options: Options, rt: Runtime) -> int:
    if options.all_users:
        return run_all(options, rt)
    ctx = preflight(options, rt)
    if options.dry_run:
        print_dry_run(ctx)
        return 0
    if not confirm(ctx, options, rt):
        return 0

    status, count, backup_path = refresh_config(ctx, rt)

    _LOG.info("")
    info("完成")
    if backup_path is not None:
        _LOG.info(f"  备份          : {backup_path}")
    _LOG.info(f"  目标配置      : {ctx.user_cfg}")
    _LOG.info(f"  models.agentos: {status}（{count} 条）")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str):
        self.print_usage(sys.stderr)
        raise FatalError(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="refresh-user-config.sh",
        description="刷新指定用户的 jiuwenswarm 配置。",
        epilog=(
            "退出码: 0 成功；1 参数/校验/合并失败（配置未改动；--all 时表示有用户失败）\n"
            "环境变量: AGENTOS_HOME_BASE / AGENTOS_SWARM_TEMPLATE 可覆盖默认路径"
        ),
    )
    parser.add_argument("username", nargs="?", help="逻辑用户名（3-32 位小写字母/数字/下划线/连字符）")
    parser.add_argument(
        "--home-base",
        default=os.environ.get("AGENTOS_HOME_BASE", DEFAULT_HOME_BASE),
        metavar="DIR",
        help=f"用户根目录，默认 {DEFAULT_HOME_BASE}",
    )
    parser.add_argument(
        "--template",
        default=os.environ.get("AGENTOS_SWARM_TEMPLATE", DEFAULT_TEMPLATE),
        metavar="FILE",
        help=f"模板 config.yaml，默认 {DEFAULT_TEMPLATE}",
    )
    parser.add_argument(
        "--all",
        dest="all_users",
        action="store_true",
        help="刷新所有已初始化用户",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印动作，不写文件、不发请求")
    parser.add_argument("-y", "--yes", action="store_true", help="跳过确认（非交互式必须指定）")
    return parser


def options_from_args(args: argparse.Namespace) -> Options:
    return Options(
        username=args.username or "",
        home_base=Path(args.home_base),
        template=Path(args.template),
        dry_run=args.dry_run,
        yes=args.yes,
        all_users=args.all_users,
    )


def main(argv: list[str] | None = None, runtime: Runtime | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
        return run(options_from_args(args), runtime or Runtime())
    except FatalError as exc:
        error(str(exc))
        return 1
    except KeyboardInterrupt:
        error("已中断")
        return 1
    except OSError as exc:
        error(f"文件操作失败: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
