"""Download current Feishu trigger images with the bound bot's identity.

Image references are stable across execution retries; physical files and source
checks belong to a single execution and are never accepted from model input.
"""
from __future__ import annotations

import asyncio
import hashlib
import io
import json
import re
import shutil
import tempfile
import time
from pathlib import Path
from urllib.parse import quote

import httpx
from PIL import Image, UnidentifiedImageError

BASE = "https://open.feishu.cn/open-apis"
SCOPE_KEY = "jiuwenmate.feishu_inbound_images"


class MediaUnavailable(ValueError):
    """A source or image cannot safely be handed to the vision model."""


def image_keys(message: dict, message_id: str, chat_id: str, refs: tuple[str, ...]) -> tuple[str, ...]:
    """Select only registered structured image nodes from the exact live message."""
    if message.get("message_id") != message_id or message.get("chat_id") != chat_id or message.get("deleted"):
        raise MediaUnavailable("图片原消息已撤回或不属于当前群，无法读取。")
    content = json.loads(message.get("body", {}).get("content", "{}"))
    nodes = []
    if message.get("msg_type") == "image":
        nodes = [{"tag": "img", "image_key": content.get("image_key")}]
    elif message.get("msg_type") == "post":
        if not ("content" in content or "elements" in content):
            content = content.get("zh_cn") or content.get("en_us") or next(iter(content.values()), {})
        rows = content.get("content", content.get("elements", []))
        nodes = [node for row in rows if isinstance(row, list) for node in row if isinstance(node, dict)]
    keys = []
    for node in nodes:
        if node.get("tag") != "img":
            continue
        key = node.get("image_key")
        if not isinstance(key, str) or not re.fullmatch(r"img_[A-Za-z0-9_-]+", key):
            raise MediaUnavailable("图片引用格式无效，请重新发送图片。")
        if f"feishu:attachment:{message_id}:{key}" not in refs:
            raise MediaUnavailable("图片不在本次消息的授权附件中，请重新发送。")
        keys.append(key)
    return tuple(keys)


class FeishuInboundImages:
    """Own image files and fresh source checks for one already-authorized trigger."""

    def __init__(self, client: httpx.AsyncClient, message_id: str, chat_id: str,
                 refs: tuple[str, ...], root: Path, *, max_bytes: int = 20 * 1024 * 1024,
                 max_pixels: int = 40_000_000, max_images: int = 8, timeout: float = 45,
                 owns_client: bool = False, manifest_path: Path | None = None) -> None:
        self.client, self.message_id, self.chat_id = client, message_id, chat_id
        self.refs, self.root = refs, root
        self.max_bytes, self.max_pixels, self.max_images, self.timeout = max_bytes, max_pixels, max_images, timeout
        self.owns_client = owns_client
        self.manifest_path = manifest_path
        self.references: tuple[str, ...] = ()
        self.notice = ""
        self._paths: dict[str, Path] = {}
        self._revision = None
        self._keys: tuple[str, ...] = ()
        self._directory: Path | None = None
        self._closed = False
        self.source_guard = None

    async def _message(self) -> dict:
        response = await self.client.get(f"{BASE}/im/v1/messages/{quote(self.message_id, safe='')}")
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0:
            raise MediaUnavailable("机器人无法读取图片原消息，请检查机器人消息读取权限。")
        items = body.get("data", {}).get("items", [])
        if len(items) != 1:
            raise MediaUnavailable("图片原消息不可读，请重新发送。")
        return items[0]

    async def prepare(self) -> None:
        """Download bounded, validated image files; retain partial failure notices."""
        try:
            async with asyncio.timeout(self.timeout):
                message = await self._message()
                self._keys = image_keys(message, self.message_id, self.chat_id, self.refs)
                self._revision = message.get("update_time")
                if self.manifest_path is not None:
                    source = {"message_id": self.message_id, "chat_id": self.chat_id,
                              "revision": self._revision, "keys": list(self._keys)}
                    if self.manifest_path.exists():
                        if json.loads(self.manifest_path.read_text(encoding="utf-8")) != source:
                            raise MediaUnavailable("图片原消息已修改，请重新发送问题。")
                    else:
                        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
                        pending = self.manifest_path.with_suffix(".part")
                        pending.write_text(json.dumps(source), encoding="utf-8")
                        pending.chmod(0o600)
                        pending.replace(self.manifest_path)
                if len(self._keys) > self.max_images:
                    raise MediaUnavailable(f"本条消息图片超过 {self.max_images} 张，请分批发送。")
                if not self._keys:
                    return
                self.root.mkdir(parents=True, exist_ok=True)
                # Clean only expired host-generated directories, never arbitrary session files.
                for old in self.root.glob("feishu-media-*"):
                    if old.is_dir() and not old.is_symlink() and time.time() - old.stat().st_mtime > 86400:
                        shutil.rmtree(old)
                self._directory = Path(tempfile.mkdtemp(prefix="feishu-media-", dir=self.root))
                downloaded: dict[str, Path] = {}
                for index, key in enumerate(self._keys, 1):
                    ref = f"feishu-image:{index}"
                    try:
                        if key not in downloaded:
                            downloaded[key] = await self._download(key, index)
                        self._paths[ref] = downloaded[key]
                    except (MediaUnavailable, httpx.HTTPError, ValueError) as exc:
                        self.notice += f"图 {index} 读取失败：{self._safe_error(exc)}\n"
                self.references = tuple(self._paths)
        except (MediaUnavailable, httpx.HTTPError, ValueError, TimeoutError) as exc:
            self.notice = self._safe_error(exc)
            self._paths.clear()
            self.references = ()
            if self._directory is not None:
                shutil.rmtree(self._directory, ignore_errors=True)
                self._directory = None
        except BaseException:
            await self.aclose()
            raise

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, MediaUnavailable):
            return str(exc)
        if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
            return "图片读取超时，请稍后重试。"
        return "图片读取失败，请检查机器人资源权限或重新发送图片。"

    async def _download(self, key: str, index: int) -> Path:
        url = f"{BASE}/im/v1/messages/{quote(self.message_id, safe='')}/resources/{quote(key, safe='')}"
        data = bytearray()
        async with self.client.stream("GET", url, params={"type": "image"}) as response:
            response.raise_for_status()
            async for chunk in response.aiter_bytes():
                data.extend(chunk)
                if len(data) > self.max_bytes:
                    raise MediaUnavailable("图片文件过大，请压缩后重发。")
        try:
            with Image.open(io.BytesIO(data)) as image:
                if image.width * image.height > self.max_pixels:
                    raise MediaUnavailable("图片分辨率过大，请缩小后重发。")
                suffix = {"PNG": ".png", "JPEG": ".jpg", "WEBP": ".webp", "GIF": ".gif", "BMP": ".bmp"}.get(image.format)
                if suffix is None:
                    raise MediaUnavailable("不支持此图片格式，请使用 PNG、JPEG 或 WebP。")
                image.verify()
        except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
            raise MediaUnavailable("图片损坏或无法解码，请重新发送。") from exc
        path = self._directory / f"image-{index}{suffix}"
        pending = path.with_suffix(".part")
        pending.write_bytes(data)
        pending.chmod(0o600)
        pending.replace(path)
        return path

    async def resolve(self, reference: str) -> str:
        """Recheck the live message before releasing this invocation's image file."""
        reference = reference.replace("media:image:", "feishu-image:", 1)
        if self._closed or reference not in self._paths:
            raise MediaUnavailable("只能读取本次消息提供的图片编号。")
        if self.source_guard is not None:
            await self.source_guard()
        async with asyncio.timeout(self.timeout):
            current = await self._message()
        keys = image_keys(current, self.message_id, self.chat_id, self.refs)
        if keys != self._keys or current.get("update_time") != self._revision:
            raise MediaUnavailable("图片原消息已修改，请重新发送问题。")
        path = self._paths[reference]
        if not path.is_file() or path.is_symlink():
            raise MediaUnavailable("本次图片已清理，请重新发送。")
        return str(path)

    def guidance(self) -> str:
        """Return model-visible image references and honest failure descriptions."""
        lines = ["本次飞书消息图片："]
        lines.extend(f"图 {ref.split(':')[-1]}：{ref}" for ref in self.references)
        if self.references:
            lines.append("调用 visual_question_answering（或 image_ocr），image_path_or_url 使用上述编号；question 使用用户原问题。成功读取后才能描述画面，多图按编号回答。")
        if self.notice:
            lines.append(self.notice)
        return "\n".join(lines)

    async def aclose(self) -> None:
        """Remove execution-owned files and release its HTTP connection pool."""
        self._closed = True
        try:
            if self._directory is not None:
                shutil.rmtree(self._directory, ignore_errors=True)
        finally:
            if self.owns_client:
                await self.client.aclose()


async def prepare_feishu_images(record, session_workspace: Path) -> FeishuInboundImages | None:
    """Resolve the configured bot only for a current Feishu group attachment."""
    route, inputs = record.route_snapshot, record.request_input
    if route is None or route.channel != "feishu" or inputs.kind != "group_message":
        return None
    if not any(ref.startswith(f"feishu:attachment:{inputs.trigger_message_id}:img_") for ref in inputs.attachment_refs):
        return None
    from jiuwenswarm.common.config import get_config
    from jiuwenswarm.server.jiuwenmate.feishu_group_fanout import configured_feishu_bindings
    from jiuwenswarm.server.jiuwenmate.media.vision_runtime import resolve_vision_config

    config = get_config()
    matches = [b for b in configured_feishu_bindings(config) if
               (b.app_id, b.namespace_id, b.agent_id) == (route.app_id, record.namespace_id, record.agent_id)]
    vision_available = resolve_vision_config() is not None
    identity = json.dumps([record.namespace_id, record.agent_id, record.session_id, record.request_id, route.app_id])
    private_root = session_workspace.parents[3].parent / "media-cache"
    manifest = private_root / "feishu-sources" / (hashlib.sha256(identity.encode()).hexdigest() + ".json")
    client = httpx.AsyncClient(timeout=15, follow_redirects=False)
    scope = FeishuInboundImages(client, inputs.trigger_message_id, route.target_id,
                               inputs.attachment_refs, private_root / "executions", owns_client=True,
                               manifest_path=manifest)
    if not vision_available:
        scope.notice = "视觉模型尚未配置，当前无法理解图片；请联系服务管理员配置视觉模型。"
        return scope
    if len(matches) != 1:
        scope.notice = "当前同事的飞书机器人绑定不可用，请联系服务管理员。"
        return scope
    channels = config.get("channels", {})
    apps = list((channels.get("feishu") or {}).get("apps") or [])
    apps.extend((channels.get("feishu_enterprise") or {}).values())
    bound = [a for a in apps if isinstance(a, dict) and a.get("app_id") == route.app_id and a.get("enabled", True)]
    if len(bound) != 1 or not bound[0].get("app_secret"):
        scope.notice = "飞书机器人凭据不可用，请联系服务管理员。"
        return scope
    try:
        response = await client.post(f"{BASE}/auth/v3/tenant_access_token/internal",
                                     json={"app_id": route.app_id, "app_secret": bound[0]["app_secret"]})
        response.raise_for_status()
        body = response.json()
        if body.get("code") != 0 or not body.get("tenant_access_token"):
            raise MediaUnavailable("飞书机器人认证失败，请联系服务管理员。")
        client.headers["Authorization"] = "Bearer " + body["tenant_access_token"]
        await scope.prepare()
    except (MediaUnavailable, httpx.HTTPError, ValueError) as exc:
        scope.notice = scope._safe_error(exc)
    except BaseException:
        await scope.aclose()
        raise
    return scope
