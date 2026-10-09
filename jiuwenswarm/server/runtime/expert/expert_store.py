# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""专家包来源抽象与实现。

上层（WS handler / 适配器）只经 ``get_expert_source()`` 拿 source，不感知仓库存在：
``list()`` 返回列表元数据，``fetch(expert_id)`` 保证包在本地可用并返回包目录路径，
之后仍走 ``DeepAgent.load_agent_template(本地路径)`` —— 正式仓库就绪后只需替换
``HttpRepoExpertPackageSource``，其余代码零改动。
"""

from __future__ import annotations

import io
import json
import os
import shutil
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol

import httpx

from jiuwenswarm.common.np_transport import (
    PipeError,
    is_named_pipe_url,
    named_pipe_transport_for,
)
from jiuwenswarm.common.utils import get_agent_experts_dir, get_expert_cache_dir, logger

DEFAULT_REPO_URL = "http://127.0.0.1:18901"
REPO_URL_ENV = "JIUWEN_EXPERT_REPO_URL"
LOCAL_DIRS_ENV = "JIUWEN_EXPERT_LOCAL_DIRS"
_FETCH_TIMEOUT_SEC = 30.0


@dataclass
class ExpertSummary:
    """专家列表项（experts.list 的数据源）。"""

    id: str
    name: str
    description: str
    source: str  # "repo" | "local" | "cache"
    available: bool
    unavailable_reason: str = ""
    tags: list[str] = field(default_factory=list)
    type: str = "agent"  # "agent" | "team"（专家团）
    metadata: dict[str, Any] = field(default_factory=dict)
    avatar_url: str = ""  # 仓库下发的头像绝对地址（<img> 直连）；空 = 无头像
    # 专家团成员摘要（type="team" 时非空，leader 置顶）：
    # [{"id", "name", "description", "role": "lead"|"member"}]
    members: list[dict[str, str]] = field(default_factory=list)


class ExpertNotFound(Exception):
    """expert_id 不存在（→ WS 错误码 NOT_FOUND）。"""


class ExpertRepoUnavailable(Exception):
    """包仓库不可达 / fetch 失败（→ REPO_UNAVAILABLE）。"""


class InvalidExpertPackage(Exception):
    """包校验失败，message 即不可用原因（→ INVALID_PACKAGE）。"""


class ExpertPackageSource(Protocol):
    """专家包来源。"""

    async def list(self) -> list[ExpertSummary]: ...

    async def fetch(self, expert_id: str) -> Path:
        """确保包在本地可用，返回包目录路径（供 load_agent_template 使用）。"""
        ...


def get_cached_expert_package_dir(expert_id: str) -> Path | None:
    """返回本地缓存的专家包目录（fetch 成功的落盘产物），无缓存返回 None。

    供重放/重挂路径缓存优先：重建时不重新 fetch、不被网络阻塞；
    缓存只由 fetch 刷新（先清空再解压），用户主动 expert.load 即完成版本更新。
    本地目录 override（LocalDirExpertPackageSource）不落缓存，返回 None 时
    调用方回退 fetch（本地 fetch 无网络开销）。
    """
    package_dir = get_expert_cache_dir() / expert_id
    if (package_dir / "manifest.json").is_file():
        return package_dir
    return None


async def resolve_expert_package_dir(expert_id: str) -> Path:
    """缓存优先解析专家包目录；缓存 miss 回退 fetch（本地源 fetch 无网络开销）。

    与 ``_apply_expert``（expert_capability.py）的 ``package_dir=None`` 兜底语义
    对齐，供**同步装配点**的 async 调用方在进入同步段之前预取——专家团装配
    ``assembly._apply_agent_group`` 只接受已就绪的本地路径，不能自持 await 。
    """
    cached = get_cached_expert_package_dir(expert_id)
    if cached is not None:
        return cached
    if os.environ.get(LOCAL_DIRS_ENV, "1") == "1":
        # 本地源启用（默认）：LocalDir 源不落缓存，miss 回退 fetch 是常态
        logger.info(
            "[ExpertStore] expert package cache miss, fallback to source fetch "
            "(local dirs enabled): %s",
            expert_id,
        )
    else:
        # 生产链路：缓存应已由 expert.load 落盘，miss 属异常态（缓存被清/损坏），
        # 自动回退仓库下载可自愈，但必须留 warning 让异常可被发现
        logger.warning(
            "[ExpertStore] expert package cache unexpectedly missing, fallback "
            "to repo fetch: %s（缓存本应已由 expert.load 落盘，请关注缓存目录健康）",
            expert_id,
        )
    return await get_expert_source().fetch(expert_id)


def validate_expert_package(package_dir: Path) -> list[str]:
    """校验专家包，返回 warnings；非法抛 InvalidExpertPackage。

    与仓库侧 list 判定同规但各自独立（双保险）；整包解析在装载时由
    agent-core loader 再做一次。

    按顶层 manifest 键分派两类包：
    - 含 ``package_type`` → 专家团（agent_group）包，走严格加载器全量校验；
    - 否则按单专家（agent_template）包校验。
    """
    manifest_path = package_dir / "manifest.json"
    if not manifest_path.is_file():
        raise InvalidExpertPackage("manifest.json 缺失")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise InvalidExpertPackage(f"manifest.json 无法解析: {exc}") from exc
    if not isinstance(manifest, dict):
        raise InvalidExpertPackage("manifest.json 不是合法 JSON 对象")
    if "package_type" in manifest:
        if manifest.get("package_type") != "agent_group":
            raise InvalidExpertPackage(
                f"package_type 不支持: {manifest.get('package_type')!r}"
            )
        from jiuwenswarm.server.runtime.expert.agent_group import (
            validate_agent_group_package,
        )

        return validate_agent_group_package(package_dir)
    if manifest.get("packageType") != "agent_template":
        raise InvalidExpertPackage("packageType 必须是 agent_template")
    card = manifest.get("agentCard")
    if not isinstance(card, dict) or not card.get("id") or not card.get("name"):
        raise InvalidExpertPackage("agentCard.id / agentCard.name 缺失")
    if card["id"] != package_dir.name:
        raise InvalidExpertPackage(
            f"agentCard.id（{card['id']}）与包名（{package_dir.name}）不一致"
        )
    if "rails" in manifest:
        raise InvalidExpertPackage("专家包不允许声明 rails")
    if "subagents" in manifest:
        raise InvalidExpertPackage("专家团（subagents）本期不支持")
    persona = manifest.get("persona")
    if not isinstance(persona, dict) or not persona.get("dir"):
        raise InvalidExpertPackage("persona.dir 缺失")
    persona_dir = package_dir / str(persona["dir"])
    if not persona_dir.is_dir() or not list(persona_dir.rglob("*.md")):
        raise InvalidExpertPackage("persona 目录不存在或没有 markdown 文件")
    for tool_entry in manifest.get("tools") or []:
        tool_file = tool_entry.get("file") if isinstance(tool_entry, dict) else None
        # tool 条目是 Python 文件引用（loader: {"file": "tools/xxx.py", "class": ...}）
        if not tool_file or not (package_dir / str(tool_file)).is_file():
            raise InvalidExpertPackage(f"tools 条目引用的文件不存在: {tool_entry!r}")
    pkg_root = package_dir.resolve()
    skipped_empty_skills = 0
    for skill_entry in manifest.get("skills") or []:
        # skill 条目是目录引用（loader: {"dir": "skills/xxx", "mode": ..., ...}）；
        # 空值条目（null / 空 dict / 缺 dir 键 / dir 为空串）跳过——agent-core
        # 装载侧 _build_skill_specs 同规放宽；非空残缺（目录不存在/缺 SKILL.md）
        # 与非 dict 非法条目仍报错
        if not skill_entry or (
                isinstance(skill_entry, dict) and not skill_entry.get("dir")
        ):
            skipped_empty_skills += 1
            continue
        skill_dir_raw = (
            skill_entry.get("dir") if isinstance(skill_entry, dict) else None
        )
        if not skill_dir_raw:
            raise InvalidExpertPackage(f"skills 条目缺少 dir: {skill_entry!r}")
        skill_path = (package_dir / str(skill_dir_raw)).resolve()
        # 声明路径必须仍在包目录内，拒绝路径逃逸（zip slip 同规）
        if pkg_root not in skill_path.parents and skill_path != pkg_root:
            raise InvalidExpertPackage(f"skills dir 逃逸包目录: {skill_dir_raw}")
        if not skill_path.is_dir():
            raise InvalidExpertPackage(f"skills dir 不存在或非目录: {skill_dir_raw}")
        # 叶子形态：dir 直接含 SKILL.md（与 agent-core _skill_paths_to_rail_mounts 的
        # is_leaf 分支对齐；父目录形态需扫子目录，本期不支持，留作扩展）
        if not (skill_path / "SKILL.md").is_file():
            raise InvalidExpertPackage(
                f"skills dir 下缺少 SKILL.md: {skill_dir_raw}（叶子形态要求直接含 SKILL.md）"
            )
    avatar = (manifest.get("metadata") or {}).get("avatar")
    if avatar:
        avatar_path = (package_dir / str(avatar)).resolve()
        # 声明路径必须仍在包目录内，且文件存在
        if package_dir.resolve() not in avatar_path.parents or not avatar_path.is_file():
            raise InvalidExpertPackage(f"metadata.avatar 声明的头像文件不存在: {avatar}")
    warnings: list[str] = []
    if skipped_empty_skills:
        warnings.append(f"skills 忽略 {skipped_empty_skills} 个空值条目")
    if "model" in manifest:
        warnings.append("model 字段不生效（根模板 model 不会被使用），请移除")
    return warnings


class HttpRepoExpertPackageSource:
    """调简易包仓库 API 的实现。

    base_url 两种形态：
      - ``http(s)://<host>:<port>``（默认 http://127.0.0.1:18901，loopback TCP）
      - ``np://<管道名>[/path 前缀]``（桌面命名管道迁移形态：HTTP/1.1 字节流经
        ``common.np_transport.NamedPipeTransport`` 过管道，对端是桌面主进程的本机代理）
    """

    def __init__(self, base_url: str | None = None, client: Any = None) -> None:
        self._base_url = (base_url or os.environ.get(REPO_URL_ENV) or DEFAULT_REPO_URL).rstrip("/")
        # client 仅供测试注入（duck-typed httpx.AsyncClient）
        self._client = client

    def _http(self) -> Any:
        if self._client is not None:
            return self._client
        if is_named_pipe_url(self._base_url):
            # 实测结论（httpx 0.28.1）：AsyncClient 直接接受 np:// base_url
            # （httpx.URL 是通用 URI 解析，不做 scheme 白名单），raw_path 拼接语义
            # 与 http 一致（np://claw-expert-repo + /api/v1/packages →
            # raw_path=b"/api/v1/packages"），无需占位 http:// base_url。
            # transport 只取 authority 段（管道名）；trust_env=False 防 HTTP(S)_PROXY
            # 等代理 env 干扰本机管道。
            return httpx.AsyncClient(
                base_url=self._base_url,
                timeout=_FETCH_TIMEOUT_SEC,
                transport=named_pipe_transport_for(self._base_url),
                trust_env=False,
            )
        return httpx.AsyncClient(
            base_url=self._base_url, timeout=_FETCH_TIMEOUT_SEC
        )

    async def _get(self, path: str) -> httpx.Response:
        client = self._http()
        try:
            if self._client is not None:
                return await client.get(path)
            async with client:
                return await client.get(path)
        except (httpx.HTTPError, PipeError) as exc:
            # PipeError：np:// 形态管道不可达（桌面代理未起/超时），与 http 连接失败同语义
            raise ExpertRepoUnavailable(f"专家仓库不可达: {exc}") from exc

    async def list(self) -> list[ExpertSummary]:
        resp = await self._get("/api/v1/packages")
        if resp.status_code != 200:
            raise ExpertRepoUnavailable(f"专家仓库列表接口返回 {resp.status_code}")
        try:
            payload = resp.json()
            items = payload.get("experts", [])
        except ValueError as exc:
            raise ExpertRepoUnavailable(f"专家仓库列表响应无法解析: {exc}") from exc
        return [
            ExpertSummary(
                id=str(item.get("id", "")),
                name=str(item.get("name", "")),
                description=str(item.get("description", "")),
                source="repo",
                available=bool(item.get("available", False)),
                unavailable_reason=str(item.get("unavailable_reason", "")),
                tags=list(item.get("tags") or []),
                type=str(item.get("type", "agent")),
                metadata=dict(item.get("metadata") or {}),
                avatar_url=str(item.get("avatar_url") or ""),
                members=[dict(m) for m in item.get("members") or [] if isinstance(m, dict)],
            )
            for item in items
        ]

    async def fetch(self, expert_id: str) -> Path:
        resp = await self._get(f"/api/v1/packages/{expert_id}")
        if resp.status_code == 404:
            raise ExpertNotFound(f"专家包不存在: {expert_id}")
        if resp.status_code != 200:
            raise ExpertRepoUnavailable(f"专家仓库下载接口返回 {resp.status_code}")
        target_dir = get_expert_cache_dir() / expert_id
        try:
            _extract_zip(resp.content, target_dir)
        except (zipfile.BadZipFile, ValueError) as exc:
            # 包内容损坏/非法，且不降级缓存——损坏的包不应静默回退旧版本（fail-loud）
            raise InvalidExpertPackage(f"专家包内容损坏或非法: {exc}") from exc
        except OSError as exc:
            # 磁盘/权限等本地环境故障：可重试语义，但缓存目录写不进去与仓库无关
            raise ExpertRepoUnavailable(f"专家包写入本地缓存失败: {exc}") from exc
        return target_dir


def _single_top_level_prefix(names: list[str]) -> str:
    """所有条目共享同一个一级目录前缀时返回该前缀（含 ``/``），否则返回 ''。

    上架工具常「连目录一起压缩」，条目全部落在同一个一级目录下
    （如 ``doc-writer/manifest.json``）；契约形态是条目平铺在 zip 根
    （``manifest.json``）。出现任何根级文件或第二个顶层名即判定非包裹形态。
    """
    candidate: str | None = None
    for name in names:
        parts = [p for p in name.split("/") if p]
        if not parts:
            continue
        # 顶层目录条目本身（如 "doc-writer/"）不否决，可作为候选前缀
        if len(parts) == 1 and not name.endswith("/"):
            return ""
        top = parts[0]
        if candidate is None:
            candidate = top
        elif top != candidate:
            return ""
    return f"{candidate}/" if candidate else ""


def _to_long_path(p: Path | str) -> str:
    """Windows 下转 ``\\\\?\\`` 扩展长度路径，规避 MAX_PATH=260 限制；其他平台原样返回。

    带前缀后 Windows 不再做
    规范化（相对路径、``..`` 不解析），调用前必须已是绝对路径。
    """
    s = os.path.abspath(os.fspath(p))
    if os.name != "nt":
        return s
    if s.startswith("\\\\?\\"):
        return s
    if s.startswith("\\\\"):
        return "\\\\?\\UNC\\" + s.lstrip("\\")
    return "\\\\?\\" + s


# 扩展长度路径总上限约 32767 字符，留余量提前拒绝，让失败落在明确文案里
# 而不是底层 WinError；单段 255 字符限制由文件系统自行拒绝。
_LONG_PATH_GUARD = 32700


def _extract_zip(content: bytes, target_dir: Path) -> None:
    """解压到 target_dir，拒绝路径逃逸的条目。

    原子化：先解压到同级暂存目录（``.<name>.staging-<pid>``），全部成功后才与
    target_dir 交换——损坏的 zip / 解压中断不再摧毁原有可用缓存。交换采用
    rename 两步（target → ``.<name>.old-<pid>`` → staging 顶上），崩溃窗口内
    至多残留 ``.old-*`` 目录，旧缓存不丢；``.`` 前缀使暂存/备份目录不进
    LocalDir.list（其跳过点开头的目录）。

    容错：条目全部位于同一个一级目录前缀下时自动剥掉该层，让
    ``manifest.json`` 落到包根目录，与平铺打包形态等价。

    Windows 全程走 ``\\\\?\\`` 前缀路径（见 ``_to_long_path``），规避 MAX_PATH
    导致的 WinError 3 假「路径不存在」。
    """
    target_str = _to_long_path(target_dir)
    staging_str = _to_long_path(
        target_dir.parent / f".{target_dir.name}.staging-{os.getpid()}"
    )
    backup_str = _to_long_path(
        target_dir.parent / f".{target_dir.name}.old-{os.getpid()}"
    )

    def _rmtree_quiet(path_str: str) -> None:
        if os.path.exists(path_str):
            shutil.rmtree(path_str, ignore_errors=True)

    # 清掉上次崩溃残留的同名暂存/备份目录
    _rmtree_quiet(staging_str)
    _rmtree_quiet(backup_str)

    with zipfile.ZipFile(io.BytesIO(content)) as zf:
        names = zf.namelist()
        for name in names:
            normalized = Path(name)
            if normalized.is_absolute() or ".." in normalized.parts:
                raise ValueError(f"zip 条目路径非法: {name}")
        prefix = _single_top_level_prefix(names)
        os.makedirs(staging_str, exist_ok=True)
        try:
            for name in names:
                rel = name[len(prefix):] if prefix else name
                if not rel or rel.endswith("/"):
                    continue
                # 逐段拼接而非 rel 整体 join：前缀模式下 Windows 不做分隔符/`.` 规范化
                parts = [p for p in rel.split("/") if p not in ("", ".")]
                if not parts:
                    continue
                dest = os.path.join(staging_str, *parts)
                if len(dest) > _LONG_PATH_GUARD:
                    raise ValueError(f"zip 条目路径过长: {name[:80]}")
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with zf.open(name) as src, open(dest, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        except BaseException:
            # 解压失败：清暂存，原缓存原样保留
            _rmtree_quiet(staging_str)
            raise

    # 全部解压成功，交换落位（rename 两步，失败回滚）
    if os.path.exists(target_str):
        os.rename(target_str, backup_str)
    try:
        os.rename(staging_str, target_str)
    except BaseException:
        if os.path.exists(backup_str) and not os.path.exists(target_str):
            os.rename(backup_str, target_str)
        raise
    _rmtree_quiet(backup_str)


class LocalDirExpertPackageSource:
    """本地目录 dev override（仅 env JIUWEN_EXPERT_LOCAL_DIRS=1 时启用）。"""

    def __init__(self, experts_dir: Path | None = None) -> None:
        self._experts_dir = experts_dir or get_agent_experts_dir()

    async def list(self) -> list[ExpertSummary]:
        summaries: list[ExpertSummary] = []
        if not self._experts_dir.is_dir():
            return summaries
        for child in sorted(self._experts_dir.iterdir()):
            if not child.is_dir() or child.name.startswith("."):
                continue
            summaries.append(self._summarize(child))
        return summaries

    @staticmethod
    def _summarize(package_dir: Path) -> ExpertSummary:
        reason = ""
        metadata: dict[str, Any] = {}
        card: dict[str, Any] = {}
        group: dict[str, str] = {}
        members: list[dict[str, str]] = []
        pkg_type = "agent"
        try:
            validate_expert_package(package_dir)
            manifest = json.loads(
                (package_dir / "manifest.json").read_text(encoding="utf-8")
            )
            if manifest.get("package_type") == "agent_group":
                # 专家团：无 agentCard，展示名取顶层 name、描述取 leader 子包，
                # 成员摘要供叠放头像/成员数展示
                pkg_type = "team"
                from jiuwenswarm.server.runtime.expert.agent_group import (
                    read_group_display,
                    read_group_members,
                )

                group = read_group_display(package_dir)
                members = read_group_members(package_dir)
            else:
                card = manifest.get("agentCard") or {}
                metadata = manifest.get("metadata") or {}
        except (InvalidExpertPackage, json.JSONDecodeError) as exc:
            reason = str(exc)
        return ExpertSummary(
            id=str(card.get("id") or package_dir.name),
            name=str(card.get("name") or group.get("name") or package_dir.name),
            description=str(card.get("description") or group.get("description") or ""),
            source="local",
            available=not reason,
            unavailable_reason=reason,
            tags=list(metadata.get("tags") or []),
            type=pkg_type,
            metadata=metadata,
            members=members,
        )

    async def fetch(self, expert_id: str) -> Path:
        package_dir = self._experts_dir / expert_id
        if not package_dir.is_dir():
            raise ExpertNotFound(f"专家包不存在: {expert_id}")
        return package_dir


class CachedExpertPackageSource(LocalDirExpertPackageSource):
    """专家包缓存来源（``experts_cache/``）：已下载过的专家/专家团始终可见。

    挂链尾（仓库之后）：
    - list：同名条目以仓库元数据为准（版本更新/下架信息新鲜），缓存只补充仓库没有的；
    - fetch：先走仓库（下载产物本来就落 experts_cache，版本更新语义不变），
      仓库 404（下架）或不可达时回退缓存目录——已下载专家仍可装载/重装；
    - 列表只收校验通过的包：下载中断的残留目录（available=False）不进列表。

    缓存目录按用户动态解析（``get_expert_cache_dir()`` = 工作区下 experts_cache），
    不写死任何 uid 路径。
    """

    # 链式 fetch 兜底命中本源时的告警文案（ChainExpertPackageSource.fetch 读取）：
    # 走到这里意味着仓库说「没有此包」（可能已下架——原因或为安全回收）
    fallback_notice = (
        "本地源与仓库均无此包（可能已下架），由缓存兜底装载；"
        "下架原因可能是安全回收，请关注"
    )

    def __init__(self) -> None:
        super().__init__(experts_dir=get_expert_cache_dir())

    async def list(self) -> list[ExpertSummary]:
        summaries = await super().list()
        return [
            replace(summary, source="cache")
            for summary in summaries
            if summary.available
        ]


class ChainExpertPackageSource:
    """多来源链：靠前来源优先（list 同名覆盖、fetch 先尝试）。

    fetch 降级语义：
    - ``ExpertNotFound``（权威否定：该源确定无此包）→ 落到下一源；
    - ``ExpertRepoUnavailable``（模糊故障：源答不了）→ 同样落到下一源，但兜底
      命中时记 warning（可能非最新版本）；所有源都失败时优先抛它而非
      ExpertNotFound——「仓库不可达」比「不存在」更可行动；
    - 其余异常（如 ``InvalidExpertPackage`` 包损坏）不降级，直接上抛——
      损坏的包不应静默回退旧版本。
    """

    def __init__(self, sources: list[ExpertPackageSource]) -> None:
        self._sources = sources

    async def list(self) -> list[ExpertSummary]:
        merged: dict[str, ExpertSummary] = {}
        first_error: Exception | None = None
        # 低优先级先合并，后面的覆盖同名
        for source in reversed(self._sources):
            try:
                for summary in await source.list():
                    merged[summary.id] = summary
            except ExpertRepoUnavailable as exc:
                first_error = first_error or exc
        if not merged and first_error is not None:
            raise first_error
        return sorted(merged.values(), key=lambda s: s.id)

    async def fetch(self, expert_id: str) -> Path:
        # local 优先；降级规则见类 docstring
        repo_error: ExpertRepoUnavailable | None = None
        saw_miss = False
        for source in self._sources:
            try:
                package_dir = await source.fetch(expert_id)
            except ExpertNotFound:
                saw_miss = True
                continue
            except ExpertRepoUnavailable as exc:
                repo_error = repo_error or exc
                continue
            if repo_error is not None:
                logger.warning(
                    "[ExpertStore] expert %s: 上游源不可达（%s），由 %s 兜底"
                    "——可能不是最新版本",
                    expert_id, repo_error, type(source).__name__,
                )
            elif saw_miss:
                notice = getattr(source, "fallback_notice", None)
                if notice:
                    logger.warning("[ExpertStore] expert %s: %s", expert_id, notice)
            return package_dir
        if repo_error is not None:
            raise repo_error
        raise ExpertNotFound(f"专家包不存在: {expert_id}")


_default_source: ExpertPackageSource | None = None


def get_expert_source() -> ExpertPackageSource:
    """source 工厂：本地专家包目录源（默认启用，env 显式设 "0" 关闭）> 仓库 > 包缓存（experts_cache）。

    本地源默认开启：expert-manager 技能创建、用户导入的专家包落在本地 experts/
    目录，需此源才会进入 experts.list 从而可召唤；桌面端与 CLI 均默认生效。
    """
    global _default_source
    if _default_source is None:
        sources: list[ExpertPackageSource] = []
        if os.environ.get(LOCAL_DIRS_ENV, "1") == "1":
            sources.append(LocalDirExpertPackageSource())
            logger.info(
                "[ExpertStore] expert local dir source enabled (%s)",
                get_agent_experts_dir(),
            )
        sources.append(HttpRepoExpertPackageSource())
        sources.append(CachedExpertPackageSource())
        _default_source = ChainExpertPackageSource(sources)
    return _default_source


def reset_expert_source() -> None:
    """测试用：重置工厂缓存。"""
    global _default_source
    _default_source = None
