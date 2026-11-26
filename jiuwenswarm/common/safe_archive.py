# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""安全归档公共层：发布 / 导出 / 导入三场景共用的打包与解包原语。

打包与解包逻辑禁止裸调用通用压缩/解压方法，本模块是资产链路安全红线的
唯一实现（当前覆盖 .zip 标准包）：

1. **冗余目录剔除**：``.git`` / ``.archive`` / ``__pycache__`` / ``.pytest_cache``
   / ``node_modules`` / ``out`` / ``.venv`` / ``.DS_Store``；
2. **敏感信息零泄露**：剔除私钥、credentials/token 文件、``*.key`` / ``*.p12``
   / ``*.pfx`` 与真实 ``.env``；仅放行 ``.env.example`` / ``.env.template``，
   并扫描其中不得含明文密钥或带账密的 URL；
3. **路径与体量防护**：拒绝 symlink 与路径穿越（Zip Slip），强制文件数、
   单文件、解压总量、深度与压缩包体积上限。

收集与过滤逻辑原先位于 ``marketplace/asset_package_builder.py``（发布场景
专用），为避免导出/导入重复建设抽到此处做布局无关化；
``asset_package_builder`` 保留发布布局薄层并 re-export 旧名
（``PackageBuildError`` / ``PackageLimits`` / ``collect_publish_files``），
既有调用方行为不变。导出场景用 :func:`pack_directory_single_root`
（单根目录、不注入发布外壳），导入场景用 :func:`extract_archive_safely`。
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import zipfile
from collections.abc import Iterator, Mapping
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import parse_qsl, urlsplit


class SafeArchiveError(ValueError):
    """安全归档失败。``code`` 为内部码，入口层负责收敛为契约错误码。

    内部码：``UNSAFE_PATH`` / ``LIMIT_EXCEEDED`` / ``CREDENTIAL_VALUE`` /
    ``OUTPUT_INSIDE_SOURCE`` / ``SOURCE_CHANGED`` / ``EMPTY_SOURCE`` /
    ``INVALID_PACKAGE``（损坏、加密或不支持的压缩包）。
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ArchiveLimits:
    """体量上限 —— 默认即强制上限：压缩包 ≤50MB、单文件 ≤64MB、文件数 ≤10000。"""

    zip_bytes: int = 50 * 1024 * 1024
    file_bytes: int = 64 * 1024 * 1024
    total_bytes: int = 512 * 1024 * 1024
    max_files: int = 10000
    max_depth: int = 32


EXCLUDED_DIRS = {
    ".git",
    ".archive",
    "__pycache__",
    ".pytest_cache",
    "node_modules",
    "out",
    ".venv",
}
EXCLUDED_FILES = {
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "token.json",
    "tokens.json",
    ".DS_Store",
}
# 仅允许随包携带的环境模板文件（内容仍需过凭证扫描）。
ALLOWED_ENV_TEMPLATES = {".env.example", ".env.template"}

# app 载荷（app_payload=True）场景：私有根目录与生成的启动脚本一律剔除，
# 大小写折叠比较（app 包发布到大小写不敏感的目标环境）。
_APP_PRIVATE_ROOTS = {"data", ".git", ".jiuwenswarm", ".ssh", ".workswarm-app-release"}
_APP_GENERATED_SCRIPTS = {
    "scripts/start.ps1",
    "scripts/start.sh",
    "scripts/stop.ps1",
    "scripts/stop.sh",
}

# Windows 目录联接（junction）reparse tag；非 Windows 平台恒为 None。
_MOUNT_POINT_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", None)


def _is_link(path: Path) -> bool:
    """symlink 或 Windows 目录联接（junction）都视为链接并拒绝。"""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return True
    return _MOUNT_POINT_TAG is not None and getattr(
        info, "st_reparse_tag", None
    ) == _MOUNT_POINT_TAG


def _app_private_path(path: Path) -> bool:
    return (
        path.parts[0].casefold() in _APP_PRIVATE_ROOTS
        or path.as_posix().casefold() in _APP_GENERATED_SCRIPTS
        or path.name.casefold().endswith(".log")
    )


def _should_exclude_rel_path(name: str, path: Path, excluded_absolute: set[Path]) -> bool:
    """冗余/敏感文件与被显式排除路径的判定。"""
    if path.absolute() in excluded_absolute or name in EXCLUDED_FILES:
        return True
    if name.endswith((".key", ".p12", ".pfx")):
        return True
    return name.startswith(".env") and name not in ALLOWED_ENV_TEMPLATES


def _check_file_limits(
    files: list[Path], info: os.stat_result, rel: str, total: int, limits: ArchiveLimits
) -> None:
    """收集时的体量与深度护栏，超限抛 ``LIMIT_EXCEEDED``。"""
    if len(files) > limits.max_files:
        raise SafeArchiveError("LIMIT_EXCEEDED")
    if info.st_size > limits.file_bytes or total > limits.total_bytes:
        raise SafeArchiveError("LIMIT_EXCEEDED")
    if len(PurePosixPath(rel).parts) > limits.max_depth:
        raise SafeArchiveError("LIMIT_EXCEEDED")


def collect_archive_files(
    root: Path,
    *,
    limits: ArchiveLimits = ArchiveLimits(),
    exclude_paths: tuple[Path, ...] = (),
    app_payload: bool = False,
) -> tuple[list[Path], list[str]]:
    """安全收集 root 下允许归档的常规文件，返回 (文件列表, 被剔除相对路径)。

    拒绝根目录或任何子路径上的 symlink / Windows 目录联接；命中冗余
    目录/敏感文件/.env 的条目记入 excluded 供调用方展示；超出文件数/
    单文件/总量/深度上限抛 ``LIMIT_EXCEEDED``。

    ``app_payload=True``（app 发布载荷）追加语义：文件与目录名大小写
    折叠比较、剔除 app 私有根（data/.git/.jiuwenswarm/.ssh/
    .workswarm-app-release）与生成的启动脚本、``out`` 目录保留
    （app 构建产物目录）。
    """
    root = Path(root)
    if not root.is_dir() or _is_link(root):
        raise SafeArchiveError("UNSAFE_PATH")
    excluded_absolute = {Path(path).absolute() for path in exclude_paths}
    files, excluded = [], []
    total = 0
    for directory, dirs, names in os.walk(root, followlinks=False):
        dirs.sort()
        names.sort()
        for name in dirs[:]:
            path = Path(directory) / name
            rel = path.relative_to(root)
            dirname = name.casefold() if app_payload else name
            builtin_excluded = dirname in EXCLUDED_DIRS and not (
                app_payload and dirname == "out"
            )
            private_excluded = app_payload and _app_private_path(rel)
            if builtin_excluded or path.absolute() in excluded_absolute or private_excluded:
                dirs.remove(name)
                excluded.append(rel.as_posix() + "/")
            elif _is_link(path):
                raise SafeArchiveError("UNSAFE_PATH")
        for name in names:
            path = Path(directory) / name
            rel = path.relative_to(root)
            lookup_name = name.casefold() if app_payload else name
            if _should_exclude_rel_path(lookup_name, path, excluded_absolute):
                excluded.append(rel.as_posix())
                continue
            if app_payload and _app_private_path(rel):
                excluded.append(rel.as_posix())
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or "\\" in rel.as_posix():
                raise SafeArchiveError("UNSAFE_PATH")
            total += info.st_size
            files.append(path)
            _check_file_limits(files, info, rel.as_posix(), total, limits)
    return sorted(files), sorted(excluded)


def validate_archive_root_name(name: str) -> str:
    """校验压缩包顶层目录名：非空、无路径分隔符、无控制字符、非 . / .. 。"""
    if not isinstance(name, str) or not name.strip() or name in {".", ".."}:
        raise SafeArchiveError("UNSAFE_PATH")
    has_separator = "/" in name or "\\" in name
    if has_separator or any(ord(c) < 32 for c in name):
        raise SafeArchiveError("UNSAFE_PATH")
    return name


def validate_environment_template(path: Path) -> None:
    """扫描 .env.example / .env.template：不得含明文密钥或带账密的 URL。

    模板不可读或非 UTF-8 属包内容不合规，收敛为 ``INVALID_PACKAGE``；
    扫描规则本身不变：明文密钥、带账密 URL、密钥形键名的未占位化取值
    都抛 ``CREDENTIAL_VALUE``。
    """
    try:
        with path.open(encoding="utf-8") as stream:
            while True:
                line = stream.readline(65537)
                if not line:
                    break
                if len(line) > 65536:
                    raise SafeArchiveError("LIMIT_EXCEEDED")
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, value = stripped.split("=", 1)
                value = value.strip().strip("\"'")
                if "://" in value:
                    try:
                        parsed = urlsplit(value)
                    except ValueError:
                        raise SafeArchiveError("CREDENTIAL_VALUE") from None
                    if parsed.username or parsed.password:
                        raise SafeArchiveError("CREDENTIAL_VALUE")
                    if any(
                        re.search(
                            r"(token|key|password|secret|credential|authorization)",
                            k,
                            re.I,
                        )
                        and v
                        and not re.fullmatch(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}", v)
                        for k, v in parse_qsl(parsed.query)
                    ):
                        raise SafeArchiveError("CREDENTIAL_VALUE")
                if re.search(
                    r"(token|key|password|secret|credential|authorization)", key, re.I
                ):
                    if value and not re.fullmatch(
                        r"(?:Bearer )?\$\{[A-Za-z_][A-Za-z0-9_]*\}", value
                    ):
                        raise SafeArchiveError("CREDENTIAL_VALUE")
    except (OSError, UnicodeDecodeError):
        # 非 UTF-8 内容或读取失败：包内容不合规，按非法包收敛。
        raise SafeArchiveError("INVALID_PACKAGE") from None


@contextmanager
def _open_directory(path: str, flags: int, *, dir_fd: int | None = None):
    """Keep each traversal directory descriptor paired with its close."""
    descriptor = os.open(path, flags, mode=0o600, dir_fd=dir_fd)
    try:
        yield descriptor
    finally:
        os.close(descriptor)


@contextmanager
def _open_source_fd(value: Path, flags: int):
    """以 ``mode=0o600`` 打开源文件并保证描述符成对释放（G.FIO.01 / G.PRM.03）。

    ``fdopen`` 以 ``closefd=False`` 挂接，描述符的唯一定时释放点在本
    ``finally``，静态分析与运行时都满足申请/释放配对。
    """
    fd = os.open(value, flags, mode=0o600)
    try:
        yield fd
    finally:
        os.close(fd)


def _open_source_file(root: Path, relative: Path) -> int:
    if os.open in os.supports_dir_fd and hasattr(os, "O_NOFOLLOW"):
        absolute = root.absolute()
        with ExitStack() as descriptors:
            directory_fd = descriptors.enter_context(
                _open_directory(absolute.anchor, os.O_RDONLY | os.O_DIRECTORY)
            )
            for component in (*absolute.parts[1:], *relative.parts[:-1]):
                directory_fd = descriptors.enter_context(
                    _open_directory(
                        component,
                        os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=directory_fd,
                    )
                )
            return os.open(
                relative.name,
                os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                mode=0o600,
                dir_fd=directory_fd,
            )
    # Platforms without openat still reject static links at every component.
    candidate = root
    for component in relative.parts:
        candidate /= component
        if _is_link(candidate):
            raise SafeArchiveError("UNSAFE_PATH")
    return os.open(candidate, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0), 0o600)


def _iter_bounded_chunks(
    stream,
    file_limit: int,
    *,
    consumed: int = 0,
    total_limit: int | None = None,
) -> Iterator[bytes]:
    """按 64KB 块流式读取，逐块校验单文件与累计总量上限。

    三处消费方（写入 zip 条目、防 TOCTOU 复制、安全解包）共用同一条
    限流读取路径，避免各写一份易漂移的读取循环。
    """
    copied = 0
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        copied += len(chunk)
        if copied > file_limit or (
            total_limit is not None and consumed + copied > total_limit
        ):
            raise SafeArchiveError("LIMIT_EXCEEDED")
        yield chunk


def _stream_copy_into(
    stream,
    target,
    file_hash: "hashlib._Hash",
    limits: ArchiveLimits,
    consumed: int,
) -> int:
    """把源流按块写入复制目标并同步摘要，返回本次复制字节数。"""
    copied = 0
    chunks = _iter_bounded_chunks(
        stream, limits.file_bytes, consumed=consumed, total_limit=limits.total_bytes
    )
    for chunk in chunks:
        copied += len(chunk)
        file_hash.update(chunk)
        if target:
            target.write(chunk)
    return copied


def _check_source_stable(stream, current: os.stat_result) -> None:
    """读完复核 size/mtime/ctime 未变（被并发修改抛 ``SOURCE_CHANGED``）。"""
    after = os.fstat(stream.fileno())
    if (current.st_size, current.st_mtime_ns, current.st_ctime_ns) != (
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    ):
        raise SafeArchiveError("SOURCE_CHANGED")


def _copy_one_file(
    root: Path,
    path: Path,
    limits: ArchiveLimits,
    destination: Path | None,
    *,
    consumed: int,
) -> tuple[int, bytes]:
    """单个文件的防 TOCTOU 复制/摘要，返回 (实际字节数, 含路径前缀的摘要)。

    相对路径名参与摘要（与内容摘要串接），保持与逐文件循环的旧摘要字节
    兼容；复制目标（可选）按同样相对路径落盘并恢复可执行位。
    """
    rel = path.relative_to(root)
    path_digest = hashlib.sha256(rel.as_posix().encode() + b"\0")
    try:
        before = path.lstat()
    except OSError:
        # 收集与复制之间源文件消失：与 symlink/非常规文件同样按不安全处理。
        raise SafeArchiveError("UNSAFE_PATH") from None
    try:
        fd = _open_source_file(root, rel)
    except OSError:
        raise SafeArchiveError("UNSAFE_PATH") from None
    file_hash = hashlib.sha256()
    target = None
    with os.fdopen(fd, "rb") as stream:
        current = os.fstat(stream.fileno())
        if not stat.S_ISREG(current.st_mode) or (before.st_dev, before.st_ino) != (
            current.st_dev,
            current.st_ino,
        ):
            raise SafeArchiveError("UNSAFE_PATH")
        if destination is not None:
            output = destination / rel
            output.parent.mkdir(parents=True, exist_ok=True)
            target = output.open("xb")
        try:
            copied = _stream_copy_into(stream, target, file_hash, limits, consumed)
        finally:
            if target:
                target.close()
                output.chmod(0o755 if current.st_mode & 0o111 else 0o644)
        _check_source_stable(stream, current)
    path_digest.update(file_hash.digest())
    return copied, path_digest.digest()


def copy_files_digest(
    root: Path,
    files: list[Path],
    limits: ArchiveLimits,
    destination: Path | None = None,
) -> str:
    """按相对路径逐文件做防 TOCTOU 复制/摘要，返回内容 sha256（hex）。

    每个文件经 ``O_NOFOLLOW`` 逐段打开，打开后复核 dev/ino 与常规文件类型，
    读完复核 size/mtime/ctime 未变（变了抛 ``SOURCE_CHANGED``）；可选复制到
    ``destination`` 下同样的相对路径。
    """
    digest = hashlib.sha256()
    total = 0
    for path in files:
        copied, file_digest = _copy_one_file(
            root, path, limits, destination, consumed=total
        )
        total += copied
        digest.update(file_digest)
    return digest.hexdigest()


@dataclass(frozen=True)
class ArchiveArtifact:
    """写好的 zip 产物元数据。"""

    sha256: str
    size_bytes: int


def _stat_source(value: Path) -> os.stat_result:
    """写入前的源文件体检：lstat 拒 symlink，仅放行常规文件。"""
    try:
        before = value.lstat()
    except OSError:
        raise SafeArchiveError("UNSAFE_PATH") from None
    if not stat.S_ISREG(before.st_mode):
        raise SafeArchiveError("UNSAFE_PATH")
    return before


def _copy_path_into_entry(value: Path, dest, limits: ArchiveLimits) -> None:
    """把源文件流式写入 zip 条目，全程防 TOCTOU。

    ``O_NOFOLLOW`` 打开最终分量，打开后复核 dev/ino 与常规文件类型，
    读完复核 size/mtime/ctime 未变（变了抛 ``SOURCE_CHANGED``）。
    """
    before = _stat_source(value)
    flags = (
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    )
    try:
        with _open_source_fd(value, flags) as fd, os.fdopen(
            fd, "rb", closefd=False
        ) as src:
            current = os.fstat(src.fileno())
            if not stat.S_ISREG(current.st_mode) or (
                before.st_dev,
                before.st_ino,
            ) != (current.st_dev, current.st_ino):
                raise SafeArchiveError("UNSAFE_PATH")
            for chunk in _iter_bounded_chunks(src, limits.file_bytes):
                dest.write(chunk)
            _check_source_stable(src, current)
    except OSError:
        raise SafeArchiveError("UNSAFE_PATH") from None


def _entry_weight(value: Path | bytes) -> tuple[os.stat_result | None, int]:
    """条目的 (源文件 lstat 或 None, 展开字节数)；bytes 内容无源文件。"""
    if isinstance(value, bytes):
        return None, len(value)
    # lstat 不跟随 symlink：收集与写入之间的窗口内若被换成 symlink 或
    # 非常规文件，在此直接拒绝（防 TOCTOU 入包）。
    before = _stat_source(value)
    return before, before.st_size


def _zip_file_digest(zip_path: Path) -> str:
    """整包 sha256（hex），供产物元数据使用。"""
    digest = hashlib.sha256()
    with zip_path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_zip_entries(
    entries: Mapping[str, Path | bytes],
    zip_path: Path,
    *,
    limits: ArchiveLimits = ArchiveLimits(),
    compression: int = zipfile.ZIP_DEFLATED,
) -> ArchiveArtifact:
    """把 ``{包内路径: 源文件或字节内容}`` 确定性写成 zip，返回摘要与体积。

    确定性：条目排序、时间戳固定 1980-01-01、权限只保留可执行位 ——
    同源内容重复打包产物字节级一致。写入中任何超限都抛 ``LIMIT_EXCEEDED``
    并清掉半成品 zip。``compression`` 允许调用方选择压缩算法（如 app 载荷
    使用 ``ZIP_STORED`` 以满足 Hub 压缩比限制）。
    """
    zip_path = Path(zip_path)
    if len(entries) > limits.max_files or any(
        len(PurePosixPath(k).parts) > limits.max_depth for k in entries
    ):
        raise SafeArchiveError("LIMIT_EXCEEDED")
    expanded = 0
    completed = False
    try:
        with zipfile.ZipFile(zip_path, "w", compression=compression) as archive:
            for arcname, value in sorted(entries.items()):
                before, size = _entry_weight(value)
                expanded += size
                if size > limits.file_bytes or expanded > limits.total_bytes:
                    raise SafeArchiveError("LIMIT_EXCEEDED")
                info = zipfile.ZipInfo(arcname, date_time=(1980, 1, 1, 0, 0, 0))
                info.compress_type = compression
                mode = 0o755 if before is not None and before.st_mode & 0o111 else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                with archive.open(info, "w") as dest:
                    if isinstance(value, bytes):
                        dest.write(value)
                    else:
                        _copy_path_into_entry(value, dest, limits)
                if zip_path.stat().st_size > limits.zip_bytes:
                    raise SafeArchiveError("LIMIT_EXCEEDED")
        if zip_path.stat().st_size > limits.zip_bytes:
            raise SafeArchiveError("LIMIT_EXCEEDED")
        completed = True
    finally:
        if not completed:
            zip_path.unlink(missing_ok=True)
    return ArchiveArtifact(_zip_file_digest(zip_path), zip_path.stat().st_size)


@dataclass(frozen=True)
class PackedDirectory:
    """:func:`pack_directory_single_root` 的结果。"""

    artifact: ArchiveArtifact
    root_name: str
    files: tuple[str, ...]  # 包内相对路径（含 root_name/ 前缀）
    excluded: tuple[str, ...]  # 被安全过滤剔除的源内相对路径


def pack_directory_single_root(
    source: Path,
    zip_path: Path,
    *,
    root_name: str | None = None,
    limits: ArchiveLimits = ArchiveLimits(),
    exclude_paths: tuple[Path, ...] = (),
) -> PackedDirectory:
    """导出场景：把 source 打成 ``<root_name>/…`` 的纯净单层根目录 zip。

    导出 zip 必须为单层根目录结构，``<id>`` 与顶层目录名严格一致，严禁
    嵌套发布外壳（plugin.yaml 等由发布场景自行追加，导出禁止）。与发布
    场景共用同一套安全收集，并扫描环境模板凭证。过滤后无文件可打包时抛
    ``EMPTY_SOURCE``（资产包内容缺失属上游校验问题，由入口收敛）。
    """
    source = Path(source)
    if source.is_symlink():
        raise SafeArchiveError("UNSAFE_PATH")
    if root_name is None:
        root_name = source.name
    root_name = validate_archive_root_name(root_name)
    files, excluded = collect_archive_files(
        source, limits=limits, exclude_paths=exclude_paths
    )
    for path in files:
        if path.name in ALLOWED_ENV_TEMPLATES:
            validate_environment_template(path)
    entries = {
        f"{root_name}/{path.relative_to(source).as_posix()}": path for path in files
    }
    if not entries:
        raise SafeArchiveError("EMPTY_SOURCE")
    artifact = write_zip_entries(entries, Path(zip_path), limits=limits)
    return PackedDirectory(artifact, root_name, tuple(sorted(entries)), tuple(excluded))


_DRIVE_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class ExtractedArchive:
    """:func:`extract_archive_safely` 的结果。"""

    root_name: str | None  # 唯一顶层目录名；包内非单根时为 None
    files: tuple[str, ...]  # 已解出的文件相对路径（posix 分隔）
    total_bytes: int  # 实际解出的字节总量


def _entry_parts(name: str, limits: ArchiveLimits) -> PurePosixPath:
    """校验单条目名并返回其 posix 分量：拒绝对路径、盘号、``..`` 与超深。"""
    name = name.replace("\\", "/")
    if (
        name.startswith("/")
        or _DRIVE_RE.match(name)
        or ".." in PurePosixPath(name).parts
    ):
        raise SafeArchiveError("UNSAFE_PATH")
    parts = PurePosixPath(name)
    if not parts.parts:
        raise SafeArchiveError("UNSAFE_PATH")
    if len(parts.parts) > limits.max_depth:
        raise SafeArchiveError("LIMIT_EXCEEDED")
    return parts


def _extract_one(
    archive: zipfile.ZipFile,
    info: zipfile.ZipInfo,
    target: Path,
    limits: ArchiveLimits,
) -> int:
    """解出单个文件条目并恢复权限位，返回实际解出的字节数。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    copied = 0
    with archive.open(info, "r") as src, target.open("wb") as out:
        for chunk in _iter_bounded_chunks(src, limits.file_bytes):
            copied += len(chunk)
            out.write(chunk)
    # 压缩包申报的权限位不可信：只保留"可执行/不可执行"语义（0o755/0o644），
    # 丢弃 group/other 写位与 setuid/setgid/sticky，杜绝全局可写落盘。
    declared_mode = (info.external_attr >> 16) & 0o777
    target.chmod(0o755 if declared_mode & 0o111 else 0o644)
    return copied


def _extract_entries(
    archive: zipfile.ZipFile,
    dest_dir: Path,
    dest_resolved: Path,
    limits: ArchiveLimits,
) -> tuple[list[str], int, set[str]]:
    """遍历全部条目完成安全解包，返回 (文件路径, 实际字节数, 顶层目录名)。"""
    files: list[str] = []
    declared_total = 0
    actual_total = 0
    top_level_dirs: set[str] = set()
    infos = archive.infolist()
    if len(infos) > limits.max_files:
        raise SafeArchiveError("LIMIT_EXCEEDED")
    for info in infos:
        parts = _entry_parts(info.filename, limits)
        if stat.S_IFMT(info.external_attr >> 16) == stat.S_IFLNK:
            raise SafeArchiveError("UNSAFE_PATH")
        if info.is_dir() or len(parts.parts) > 1:
            # 只有被证明是目录的顶层名才参与单根判定，
            # 纯顶层文件包（如单个 readme.txt）不算单根。
            top_level_dirs.add(parts.parts[0])
        target = dest_dir.joinpath(*parts.parts)
        if not target.resolve().is_relative_to(dest_resolved):
            raise SafeArchiveError("UNSAFE_PATH")
        if info.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        declared_total += info.file_size
        if info.file_size > limits.file_bytes or declared_total > limits.total_bytes:
            raise SafeArchiveError("LIMIT_EXCEEDED")
        copied = _extract_one(archive, info, target, limits)
        actual_total += copied
        if actual_total > limits.total_bytes:
            raise SafeArchiveError("LIMIT_EXCEEDED")
        files.append(parts.as_posix())
    return files, actual_total, top_level_dirs


def extract_archive_safely(
    zip_path: Path,
    dest_dir: Path,
    *,
    limits: ArchiveLimits = ArchiveLimits(),
) -> ExtractedArchive:
    """导入场景：Zip Slip / symlink / 超限安全解包。

    - 拒绝绝对路径、盘号路径与含 ``..`` 分量的条目；
    - 拒绝 symlink 条目 —— 解压过程不产生任何 symlink，后续条目不会经由
      已解出的链接被重定向；
    - 压缩包体积、条目数、单文件与总解压量、深度全部受限；解压流按实际
      读出字节计数，不信任 zip 头部申报值（防 zip bomb）；
    - 每个落点 resolve 后复核仍在 ``dest_dir`` 内；
    - 文件按 zip 记录恢复权限位（保留可执行位，保证导出→导入往返后脚本
      仍可执行）；掩码 ``0o777`` 排除 setuid / setgid / sticky 位。

    ``dest_dir`` 应传入全新临时目录：超限中途失败时已解出的部分文件不做
    回收，由调用方整体丢弃临时目录。
    """
    zip_path = Path(zip_path)
    dest_dir = Path(dest_dir)
    try:
        zip_size = zip_path.stat().st_size
    except OSError:
        raise SafeArchiveError("INVALID_PACKAGE") from None
    if zip_size > limits.zip_bytes:
        raise SafeArchiveError("LIMIT_EXCEEDED")
    dest_dir.mkdir(parents=True, exist_ok=True)
    files: list[str]
    actual_total: int
    top_level_dirs: set[str]
    try:
        with zipfile.ZipFile(zip_path, "r") as archive:
            files, actual_total, top_level_dirs = _extract_entries(
                archive, dest_dir, dest_dir.resolve(), limits
            )
    except zipfile.BadZipFile:
        raise SafeArchiveError("INVALID_PACKAGE") from None
    except NotImplementedError:
        # 未编译支持的压缩方法（bz2/lzma 等）或加密条目平台不支持。
        raise SafeArchiveError("INVALID_PACKAGE") from None
    except RuntimeError:
        # 加密条目（archive.open 抛 "File ... is encrypted"）。
        raise SafeArchiveError("INVALID_PACKAGE") from None
    except OSError:
        raise SafeArchiveError("UNSAFE_PATH") from None
    root_name = next(iter(top_level_dirs)) if len(top_level_dirs) == 1 else None
    return ExtractedArchive(root_name, tuple(sorted(files)), actual_total)
