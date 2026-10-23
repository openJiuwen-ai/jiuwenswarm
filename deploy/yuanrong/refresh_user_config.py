#!/usr/bin/env python3.11
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""刷新单个逻辑用户的 jiuwenswarm 配置。

流程：
  1. 备份 <user>/.jiuwenswarm/config/config.yaml
  2. 用 root 模板覆盖
  3. 保留备份中的 models.agentos
  4. 属主改为 agentos、权限 0600
  5. 删除该用户的 YuanRong agent 实例（supervisor + docker 工具沙箱），
     用户下次访问时由 Gateway 自动重建

--all 模式：对 home-base 下所有已初始化用户重复 1-4，全部完成后只扫描一次
/api/agent，统一删除这些用户的运行实例。

运行要求：root、python3.11（已安装 jiuwenswarm）、目标用户已由
`agentos.sh create-user` 初始化。

退出码：
  0  成功（含“无运行实例，无需重启”）
  1  参数/前置校验/配置合并失败（目标配置未改动）
  2  配置已更新，但重启未完成（需人工确认）

测试方式：Runtime 只保留 8 个外部 I/O，测试用 dataclasses.replace 替换即可，
无需 root、网络和 docker。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

try:
    import pwd
except ImportError:  # pragma: no cover - Windows 本地测试
    pwd = None  # type: ignore[assignment]

DEFAULT_HOME_BASE = "/home/agentos/users"
DEFAULT_TEMPLATE = "/root/.jiuwenswarm/config/config.yaml"
DEFAULT_OS_USER = "agentos"
USERNAME_RE = re.compile(r"^[a-z0-9_-]{3,32}$")
HTTP_TIMEOUT = 10.0

ABSENT = "absent"
MERGED = "merged"
EMPTY = "empty"
INVALID = "invalid"


class FatalError(Exception):
    """参数、前置校验或配置合并失败，退出码 1。"""


def _paint(stream, code: str, text: str) -> str:
    if not stream.isatty() or os.environ.get("NO_COLOR"):
        return text
    return f"\033[{code}m{text}\033[0m"


def info(message: str) -> None:
    print(_paint(sys.stdout, "36", f"=== {message} ==="))


def success(message: str) -> None:
    print(_paint(sys.stdout, "32", f"✅ {message}"))


def warning(message: str) -> None:
    print(_paint(sys.stderr, "33", f"⚠️  {message}"), file=sys.stderr)


def error(message: str) -> None:
    print(_paint(sys.stderr, "31", f"❌ {message}"), file=sys.stderr)


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


def config_value(data: Any, dotted: str) -> str:
    node = data
    for part in dotted.split("."):
        if not isinstance(node, dict):
            return ""
        node = node.get(part)
        if node is None:
            return ""
    return str(node)


def path_within(path: Any, user_home: Path) -> bool:
    """path 等于 user_home 或位于其下。"""
    text = str(path or "").strip().rstrip("/")
    home = user_home.as_posix().rstrip("/")
    return bool(text) and (text == home or text.startswith(home + "/"))


def instance_detail(detail: Any) -> dict[str, Any]:
    """GET /api/agent/{id} 的响应可能把实例放在 instance 字段里。"""
    if isinstance(detail, dict) and isinstance(detail.get("instance"), dict):
        return detail["instance"]
    return detail if isinstance(detail, dict) else {}


def instance_matches_user(detail: Any, user_home: Path) -> bool:
    """按 workspace / 挂载来源 / JIUWENSWARM_USER_DIRECTORY 判断实例归属。

    YuanRong 的 GET /api/agent 列表只返回 instance_id / node_ip / sandbox_ip /
    sandbox_type，没有实例名，必须再查一次详情才能定位用户。
    """
    inst = instance_detail(detail)
    rootfs = inst.get("rootfs") if isinstance(inst.get("rootfs"), dict) else {}
    if path_within(rootfs.get("workspace"), user_home):
        return True
    if mounts_match_user(rootfs.get("mounts"), user_home):
        return True
    env = inst.get("env_vars") if isinstance(inst.get("env_vars"), dict) else {}
    return path_within(env.get("JIUWENSWARM_USER_DIRECTORY"), user_home)


def describe_instance(instance_id: str, detail: Any) -> str:
    """诊断用：实例 id | 沙箱类型 | workspace / 挂载来源。"""
    inst = instance_detail(detail)
    rootfs = inst.get("rootfs") if isinstance(inst.get("rootfs"), dict) else {}
    parts = [instance_id, str(inst.get("sandbox_type") or "-")]
    workspace = str(rootfs.get("workspace") or "")
    if workspace:
        parts.append("workspace=" + workspace)
    mounts = rootfs.get("mounts")
    if isinstance(mounts, list):
        sources = [
            str(mount.get("source") or mount.get("Source") or "")
            for mount in mounts
            if isinstance(mount, dict)
        ]
        sources = [source for source in sources if source]
        if sources:
            parts.append("mounts=" + ",".join(sources[:3]))
    return " | ".join(parts)


def mounts_match_user(mounts: Any, user_home: Path) -> bool:
    """挂载来源在用户家目录下。

    同时兼容 docker inspect 的 ``Source`` 和 YuanRong /api/agent 的 ``source``。
    """
    if not isinstance(mounts, list):
        return False
    prefix = user_home.as_posix().rstrip("/") + "/"
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        source = str(mount.get("Source") or mount.get("source") or "")
        if source.startswith(prefix):
            return True
    return False


# ---------------------------------------------------------------------------
# 可注入 I/O
# ---------------------------------------------------------------------------


def _default_chown(path: Path, uid: int, gid: int) -> None:
    if not hasattr(os, "chown"):
        raise FatalError("当前平台不支持 chown（需在 Linux 上运行）")
    os.chown(path, uid, gid)


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


def _default_http(url: str, method: str) -> tuple[int, str]:
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8", "replace")


def _default_run_cmd(argv: Sequence[str]) -> tuple[int, str, str]:
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True)
    except FileNotFoundError:
        return 127, "", ""
    return result.returncode, result.stdout, result.stderr


@dataclass
class Runtime:
    """脚本外部 I/O 的最小集合；测试按需 replace。"""

    chown: Callable[[Path, int, int], None] = _default_chown
    geteuid: Callable[[], int] = lambda: getattr(os, "geteuid", lambda: 1)()
    get_user_ids: Callable[[str], tuple[int, int]] = _default_get_user_ids
    load_round: Callable[[Path], Any] = _default_load_round
    dump_round: Callable[[Path, Any], None] = _default_dump_round
    http: Callable[[str, str], tuple[int, str]] = _default_http
    run_cmd: Callable[[Sequence[str]], tuple[int, str, str]] = _default_run_cmd
    is_tty: Callable[[], bool] = lambda: sys.stdin.isatty()


@dataclass(frozen=True)
class Options:
    username: str
    home_base: Path
    template: Path
    frontend: str = ""
    no_restart: bool = False
    dry_run: bool = False
    yes: bool = False
    all_users: bool = False
    os_user: str = DEFAULT_OS_USER


@dataclass(frozen=True)
class Context:
    username: str
    user_home: Path
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
    return Context(options.username, user_home, cfg_dir, user_cfg, template, uid, gid)


def confirm_targets(header: str, lines: list[str], rt: Runtime) -> bool:
    """打印摘要并等待确认；非交互式环境要求显式 -y。"""
    if not rt.is_tty():
        raise FatalError("非交互式环境请显式指定 -y/--yes")
    info(header)
    for line in lines:
        print(line)
    try:
        answer = input("确认执行? [y/N] ").strip().lower()
    except EOFError as exc:
        raise FatalError("未读到确认输入，请显式指定 -y/--yes") from exc
    if answer not in {"y", "yes"}:
        warning("已取消")
        return False
    return True


def restart_prompt(options: Options) -> str:
    if options.no_restart:
        return "  运行实例    : 不重启（--no-restart）"
    return "  运行实例    : 将删除 YuanRong agent 实例（用户下次访问自动重建）"


def confirm(ctx: Context, options: Options, rt: Runtime) -> bool:
    if options.dry_run or options.yes:
        return True
    return confirm_targets(
        "即将刷新用户配置",
        [
            f"  用户        : {ctx.username}",
            f"  目标配置    : {ctx.user_cfg}",
            f"  模板        : {ctx.template}",
            restart_prompt(options),
        ],
        rt,
    )


def backup(ctx: Context, rt: Runtime) -> Path | None:
    if not ctx.user_cfg.is_file():
        warning(f"目标配置不存在，将直接按模板新建: {ctx.user_cfg}")
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
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


def _read_value(path: Path, dotted: str) -> str:
    try:
        return config_value(_load_plain(path), dotted)
    except Exception:  # noqa: BLE001
        return ""


def _get_json(rt: Runtime, url: str) -> Any:
    status, body = rt.http(url, "GET")
    if status != 200:
        raise RuntimeError(f"HTTP {status}")
    return json.loads(body)


def restart_instances(ctxs: list[Context], options: Options, rt: Runtime) -> int:
    """扫描 /api/agent，删除属于 ctxs 中用户的运行实例。"""
    if not ctxs:
        info("没有可重启的用户")
        return 0

    frontend = options.frontend or _read_value(
        ctxs[0].user_cfg, "gateway.agent_client.frontend_endpoint"
    )
    if not frontend:
        warning(
            "无法确定 YuanRong frontend 地址"
            "（gateway.agent_client.frontend_endpoint 为空），跳过重启"
        )
        return 2

    base = frontend.rstrip("/")
    homes = {ctx.username: ctx.user_home for ctx in ctxs}
    scope = (
        f"用户 {ctxs[0].username} 的" if len(ctxs) == 1 else f"这 {len(ctxs)} 个用户的"
    )
    info(f"重启{scope}运行实例（frontend: {frontend}）")
    try:
        payload = _get_json(rt, base + "/api/agent")
    except Exception as exc:  # noqa: BLE001
        warning(f"读取 /api/agent 失败（{exc}），无法重启运行实例")
        return 2

    items = payload.get("instances") if isinstance(payload, dict) else payload
    items = items if isinstance(items, list) else []
    targets: list[str] = []
    owner_of: dict[str, str] = {}
    skipped: list[str] = []    # 确认不属于目标用户，或实例已不存在
    uncertain: list[str] = []  # 无法判定归属，重启结果不可确认
    for item in items:
        if not isinstance(item, dict):
            continue
        instance_id = str(
            item.get("instanceId") or item.get("instance_id") or item.get("id") or ""
        )
        if not instance_id:
            uncertain.append("  - <无实例 id> | 无法判定归属")
            continue
        url = base + "/api/agent/" + urllib.parse.quote(instance_id, safe="")
        try:
            status, body = rt.http(url, "GET")
        except Exception as exc:  # noqa: BLE001
            uncertain.append(f"  - {instance_id} | 详情读取失败（{exc}）")
            continue
        if status == 404:
            skipped.append(f"  - {instance_id} | 实例已不存在（404）")
            continue
        if status != 200:
            uncertain.append(f"  - {instance_id} | 详情 HTTP {status}")
            continue
        try:
            detail = json.loads(body)
        except ValueError:
            uncertain.append(f"  - {instance_id} | 详情响应不是合法 JSON")
            continue
        owner = ""
        for username, home in homes.items():
            if instance_matches_user(detail, home):
                owner = username
                break
        if not owner:
            skipped.append(f"  - {describe_instance(instance_id, detail)} | 不匹配")
            continue
        targets.append(instance_id)
        owner_of[instance_id] = owner

    targets = list(dict.fromkeys(targets))
    if not targets:
        if uncertain:
            warning(
                f"/api/agent 返回 {len(items)} 个实例，其中 {len(uncertain)} 个无法判定归属，"
                "重启未完成（需人工确认）"
            )
            for line in uncertain[:20]:
                info(line)
            if skipped:
                info(f"另有 {len(skipped)} 个确认不属于{scope}实例")
            return 2
        if items:
            info(f"/api/agent 返回 {len(items)} 个实例，但没有匹配{scope}实例：")
            for line in skipped[:20]:
                info(line)
        else:
            info(f"未发现{scope}运行实例")
        return 0

    if skipped:
        info(f"跳过 {len(skipped)} 个实例：")
        for line in skipped[:20]:
            info(line)

    failed: list[str] = []
    deleted: list[str] = []
    for instance_id in targets:
        url = base + "/api/agent/" + urllib.parse.quote(instance_id, safe="")
        try:
            status, body = rt.http(url, "DELETE")
        except Exception as exc:  # noqa: BLE001
            failed.append(f"{instance_id}: {exc}")
            continue
        if status in (200, 202, 204, 404):
            success(f"已删除实例: {instance_id}（用户 {owner_of[instance_id]}）")
            deleted.append(instance_id)
        else:
            failed.append(f"{instance_id}: HTTP {status} {body[:160]}")

    if failed:
        warning("运行实例重启未完成: " + "; ".join(failed))
        return 2
    success(f"已删除 {len(deleted)} 个运行实例，用户下次访问自动重建 supervisor 与 docker 沙箱")
    warning("Gateway 内存中的旧实例缓存会在用户下次请求时自愈，可能表现为一次请求重试")
    if uncertain:
        warning(
            f"另有 {len(uncertain)} 个实例无法判定归属，重启未完全确认（需人工确认）："
        )
        for line in uncertain[:20]:
            info(line)
        return 2
    return 0


def restart_phase(
    ctxs: list[Context], options: Options, rt: Runtime, *, docker_cleanup: bool
) -> int:
    """按配置决定是否重启；单用户模式额外做 docker 兜底清理。"""
    if options.no_restart:
        info("--no-restart：跳过运行实例重启")
        return 0
    try:
        rc = restart_instances(ctxs, options, rt)
    except Exception as exc:  # noqa: BLE001
        warning(f"重启运行实例时发生意外异常（{type(exc).__name__}: {exc}）")
        return 2
    if not docker_cleanup or not ctxs:
        return rc
    if rc != 0:
        warning("运行实例重启未确认，跳过 docker 工具沙箱清理")
        return rc
    try:
        cleanup_docker(ctxs[0], rt)
    except Exception as exc:  # noqa: BLE001
        warning(f"docker 工具沙箱清理异常（{type(exc).__name__}: {exc}），已忽略")
    return rc


def cleanup_docker(ctx: Context, rt: Runtime) -> None:
    rc, _, _ = rt.run_cmd(["docker", "info"])
    if rc != 0:
        warning("docker 不可用或守护进程不可达，跳过工具沙箱容器清理")
        return
    sandbox_image = _read_value(ctx.user_cfg, "sandbox.image")
    if not sandbox_image:
        warning("配置中没有 sandbox.image，跳过工具沙箱容器清理")
        return
    rc, stdout, _ = rt.run_cmd(["docker", "ps", "-q"])
    if rc != 0:
        warning("docker ps 执行失败，跳过工具沙箱容器清理")
        return

    removed = 0
    for container_id in stdout.split():
        rc, name, _ = rt.run_cmd(["docker", "inspect", "-f", "{{.Name}}", container_id])
        container_name = name.strip().lstrip("/") if rc == 0 else ""
        if container_name.startswith("agentos-manager-"):
            continue
        rc, image, _ = rt.run_cmd(["docker", "inspect", "-f", "{{.Config.Image}}", container_id])
        if rc != 0 or image.strip() != sandbox_image:
            continue
        rc, mounts_json, _ = rt.run_cmd(
            ["docker", "inspect", "-f", "{{json .Mounts}}", container_id]
        )
        try:
            mounts = json.loads(mounts_json) if rc == 0 else None
        except Exception:  # noqa: BLE001
            mounts = None
        if not mounts_match_user(mounts, ctx.user_home):
            continue
        rc, _, _ = rt.run_cmd(["docker", "rm", "-f", container_id])
        if rc == 0:
            success(f"已删除工具沙箱容器: {container_name} ({container_id})")
            removed += 1
        else:
            warning(f"删除工具沙箱容器失败: {container_name} ({container_id})")
    if removed == 0:
        info("未发现匹配的工具沙箱容器（可能已随 supervisor 释放）")


def print_dry_run(ctx: Context, options: Options) -> None:
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
    if options.no_restart:
        info("[dry-run] --no-restart：跳过运行实例重启")
    else:
        info(f"[dry-run] 将删除用户 {ctx.username} 的 YuanRong agent 实例（未发请求）")
        info("[dry-run] 将按 user_home + sandbox.image 精确匹配并删除 docker 工具沙箱容器")


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
        print(f"  - {username}: {home_base / username / '.jiuwenswarm' / 'config' / 'config.yaml'}")
    if options.no_restart:
        info("[dry-run] --no-restart：跳过运行实例重启")
    else:
        info("[dry-run] 将删除上述用户的全部 YuanRong agent 实例（未发请求）")


def confirm_all(users: list[str], options: Options, rt: Runtime) -> bool:
    if options.dry_run or options.yes:
        return True
    return confirm_targets(
        f"即将刷新 {len(users)} 个用户的配置",
        [f"  - {username}" for username in users]
        + [f"  模板        : {options.template}", restart_prompt(options)],
        rt,
    )


def run_all(options: Options, rt: Runtime) -> int:
    """刷新所有已初始化用户的配置，然后统一重启这些用户的运行实例。"""
    if options.username:
        raise FatalError("--all 不接受用户名参数")
    if rt.geteuid() != 0:
        raise FatalError("必须以 root 运行（需要修改文件属主）")

    users = discover_users(options)
    if not users:
        raise FatalError(f"{Path(options.home_base).expanduser()} 下没有已初始化的用户")
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

    print("")
    info("配置刷新结果")
    print(f"  成功: {len(updated)} / {len(users)}")
    for line in failures:
        print(f"  失败: {line}")

    rc = restart_phase(updated, options, rt, docker_cleanup=False)

    if rc == 0 and failures:
        rc = 1
    return rc


def run(options: Options, rt: Runtime) -> int:
    if options.all_users:
        return run_all(options, rt)
    ctx = preflight(options, rt)
    if options.dry_run:
        print_dry_run(ctx, options)
        return 0
    if not confirm(ctx, options, rt):
        return 0

    status, count, backup_path = refresh_config(ctx, rt)

    rc = restart_phase([ctx], options, rt, docker_cleanup=True)

    print("")
    if rc == 0:
        info("完成")
    else:
        warning("配置已更新，但运行实例重启未完成（退出码 2）")
    if backup_path is not None:
        print(f"  备份          : {backup_path}")
    print(f"  目标配置      : {ctx.user_cfg}")
    print(f"  models.agentos: {status}（{count} 条）")
    return rc


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
        description="刷新指定用户的 jiuwenswarm 配置，并重启该用户的运行实例。",
        epilog=(
            "退出码: 0 成功；1 参数/校验/合并失败（配置未改动；--all 时表示有用户失败）；"
            "2 配置已更新但重启未完成\n"
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
    parser.add_argument("--frontend", default="", metavar="URL",
                        help="覆盖 YuanRong frontend 地址（默认从用户配置读取）")
    parser.add_argument("--no-restart", action="store_true", help="只刷新配置，不重启运行实例")
    parser.add_argument(
        "--all",
        dest="all_users",
        action="store_true",
        help="刷新所有已初始化用户，然后重启全部实例",
    )
    parser.add_argument("--dry-run", action="store_true", help="只打印动作，不写文件、不发请求")
    parser.add_argument("-y", "--yes", action="store_true", help="跳过确认（非交互式必须指定）")
    return parser


def options_from_args(args: argparse.Namespace) -> Options:
    return Options(
        username=args.username or "",
        home_base=Path(args.home_base),
        template=Path(args.template),
        frontend=args.frontend,
        no_restart=args.no_restart,
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
