# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Photo tools - 相册工具.

包含：
- search_photo_gallery: 搜索图库
- upload_photo: 上传照片获取公网 URL
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Union

from openjiuwen.core.foundation.tool import tool

from jiuwenclaw.utils import logger
from .utils import (
    execute_device_command,
    format_success_response,
    raise_if_device_error,
    ToolInputError,
)


@tool(
    name="search_photo_gallery",
    description="""插件功能描述：搜索用户手机图库中的照片

  工具使用约束：如果用户说从手机图库中或者从相册中查询xx图片时调用此工具,注意此工具仅支持从本地图库检索，不支持云空间相册检索。

  工具输入输出简介：
  a. 根据图像描述语料检索匹配的照片,返回照片在手机本地的 mediaUri以及thumbnailUri。
  b. 返回的 mediaUri以及thumbnailUri 是本地路径,无法直接下载或访问。
  如需下载、查看、使用或展示照片,请使用 upload_photo 工具将 mediaUri或者thumbnailUri 转换为可访问的公网 URL。
  c. mediaUri代表手机相册中的图片原图路径，图片大小比较大，清晰度比较高
  d. thumbnailUri代表手机相册中的图片缩略图路径，图片大小比较小，清晰度适中，建议在upload_photo 工具的入参中优先使用此路径，不容易引起上传超时等问题

  搜索能力边界：
  a. 支持口语化输入：改写模型会自动提取姓名、种类、地点等实体，可以使用自然语言描述（如"小狗的照片"、"南京拍的风景"）
  b. 支持相册搜索：可以在query中包含相册名称（如"西安之行相册的照片"）
  c. 支持人像搜索：前提是照片有人像tag，且需要口语化描述（如"张三的照片"）
  d. 不支持时间相对词：不支持"最新"、"最旧"、"最早"等表述，需要使用具体时间（如"2024年的照片"而非"去年的照片"）
  e. 不支持多实体查询：不支持"或"逻辑和时间范围（如"南京或上海的照片"、"近三年的照片"），需要拆分成多次独立查询
  f. 不支持POI逆地理映射：照片的location是门牌号，用真实场地名称可能搜不到
  g. 不支持收藏感知：无法感知照片是否被收藏
  h. 不支持细粒度品种：对于动物、植物等的具体品种识别能力有限
  i. 注意：POI提取可能不准确：地名可能作为语义搜索条件，可能导致"xx湖"搜到"yy江"或"zz湾"的照片

  查询优化建议：
  a. 时间查询：将"最新"、"去年"、"近三年"等转换为具体年份（如"2024年"、"2023年到2025年"需拆分成"2023年"、"2024年"、"2025年"三次查询）
  b. 多条件查询：将"或"逻辑拆分成多次查询（如"南京或上海的照片"→先查"南京的照片"，再查"上海的照片"）
  c. 实体原子化：确保每个query只包含一个原子实体（地点、人名、物品等）
  d. 相册名称：如果知道相册名，直接在query中包含相册名可以提高准确度

  注意事项：
  a. 只有当用户明确表达从手机相册搜索或者从图库搜索时才执行此工具，如果用户仅表达要搜索xxx图片，并没有说明搜索数据源，则不要贸然调用此插件，可以优先尝试websearch或者询问用户是否要从手机图库中搜索。
  b. 操作超时时间为60秒,请勿重复调用此工具,如果超时或失败,最多重试一次。
  c. 如果用户请求包含多个实体或时间范围，需要主动拆分成多次查询并告知用户。
  """,
)
async def search_photo_gallery(
    query: str,
) -> Dict[str, Any]:
    """搜索照片.

    Args:
        query: 图像描述语料

    Returns:
        设备返回的完整 outputs，经 format_success_response 包装
    """
    try:
        logger.info(f"[SEARCH_PHOTO_GALLERY_TOOL] Searching photos - query: {query}")

        if not query or not isinstance(query, str):
            raise ToolInputError("缺少必填参数 query（搜索关键词）")

        query = query.strip()
        if not query:
            raise ToolInputError("query 不能为空")

        command = {
            "header": {
                "namespace": "Common",
                "name": "Action",
            },
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "SearchPhotoVideo",
                    "bundleName": "com.huawei.hmos.aidispatchservice",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "intentParam": {
                        "query": query,
                    },
                    "permissionId": [],
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("SearchPhotoVideo", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        raise_if_device_error(outputs, "搜索照片失败")

        result = outputs.get("result")
        if not isinstance(result, dict):
            result = {}
        n = len(result.get("items", []))
        logger.info(f"[SEARCH_PHOTO_GALLERY_TOOL] Search completed, items={n}")

        return format_success_response(dict(outputs), f"搜索到 {n} 张照片")

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[SEARCH_PHOTO_GALLERY_TOOL] Failed to search photos: {e}")
        raise RuntimeError(f"搜索照片失败: {str(e)}") from e


def _normalize_media_uris(param: Any) -> List[str]:
    """将 media_uris 规范为字符串列表（支持数组或 JSON 数组字符串）。"""
    if param is None:
        raise ToolInputError("缺少必填参数 media_uris")
    if isinstance(param, list):
        return param
    if isinstance(param, str):
        try:
            parsed = json.loads(param)
        except json.JSONDecodeError as e:
            raise ToolInputError(
                f"media_uris 必须是合法 JSON 数组字符串。解析错误: {e}"
            ) from e
        if not isinstance(parsed, list):
            raise ToolInputError("media_uris 解析后必须是数组")
        return parsed
    raise ToolInputError(
        f"media_uris 必须是数组或 JSON 数组字符串，当前类型: {type(param).__name__}"
    )


def _decode_image_url_escapes(url: str) -> str:
    """与 upload-photo-tool.ts getPhotoUrls 一致：替换 URL 中的 \\u003d、\\u0026。"""
    return url.replace("\\u003d", "=").replace("\\u0026", "&")


@tool(
    name="upload_photo",
    description="""工具能力描述：将手机本地文件回传并获取可公网访问的 URL。

  前置工具调用：此工具使用前必须先调用 search_photo_gallery 工具获取照片的 mediaUri或者thumbnailUri
  工具参数说明：
  a. 入参中的mediaUris中的mediaUri必须与search_photo_gallery结果中对应的mediaUri或者thumbnailUri完全保持一致，不要自行修改，必须是file://开头的路径。
  b. 优先使用search_photo_gallery结果中的thumbnailUri作为入参，thumbnailUri是缩略图，清晰度与文件大小都非常合适展示给用户，如果thumbnailUri不存在或者用户要求使用原图，则使用search_photo_gallery结果中对应的mediaUri
  c. media_uris 是照片在手机本地的 URI 数组（从 search_photo_gallery 工具响应中获取）。限制：每次最多支持传入 5 条 mediaUri

  注意事项：
  a. 操作超时时间为60秒,请勿重复调用此工具,如果超时或失败,最多重试一次。
  b. 此工具返回的图片链接为用户公网可访问的链接，如果需要后续操作需要下载到本地，如果需要返回给用户查看则直接以图片markdown的形式返回给用户""",
)
async def upload_photo(media_uris: Union[str, List[str]]) -> Dict[str, Any]:
    """上传照片

    Args:
        media_uris:本地 URI 列表，或 JSON 数组字符串

    Returns:
        imageUrls、count、message；单次最多 5 条 URI
    """
    try:
        normalized = _normalize_media_uris(media_uris)
        logger.info(
            "[UPLOAD_PHOTO_TOOL] Normalized mediaUris count=%s",
            len(normalized),
        )

        if len(normalized) == 0:
            raise ToolInputError("mediaUris 数组不能为空")

        if len(normalized) > 5:
            raise ToolInputError(
                f"最多支持 5 条 mediaUri，当前提供了 {len(normalized)} 条。请分批处理。"
            )

        for uri in normalized:
            if not isinstance(uri, str) or not uri.strip():
                raise ToolInputError("media_uris 中每项必须为非空字符串")

        image_infos = [{"mediaUri": u.strip()} for u in normalized]

        command = {
            "header": {
                "namespace": "Common",
                "name": "Action",
            },
            "payload": {
                "cardParam": {},
                "executeParam": {
                    "executeMode": "background",
                    "intentName": "ImageUploadForClaw",
                    "bundleName": "com.huawei.hmos.vassistant",
                    "needUnlock": True,
                    "actionResponse": True,
                    "appType": "OHOS_APP",
                    "timeOut": 5,
                    "intentParam": {"imageInfos": image_infos},
                    "permissionId": [],
                    "achieveType": "INTENT",
                },
                "responses": [{"resultCode": "", "displayText": "", "ttsText": ""}],
                "needUploadResult": True,
                "noHalfPage": False,
                "pageControlRelated": False,
            },
        }

        outputs = await execute_device_command("ImageUploadForClaw", command)

        if not isinstance(outputs, dict):
            outputs = {"outputs": outputs}

        result = outputs.get("result") if isinstance(outputs, dict) else None
        if not isinstance(result, dict):
            result = {}
        image_urls = result.get("imageUrls", [])
        if not isinstance(image_urls, list):
            image_urls = []

        decoded_urls: List[str] = []
        for url in image_urls:
            if not isinstance(url, str):
                logger.warning(
                    "[UPLOAD_PHOTO_TOOL] imageUrl 非字符串: %s",
                    type(url),
                )
                continue
            decoded = _decode_image_url_escapes(url)
            if decoded != url:
                logger.info(
                    "[UPLOAD_PHOTO_TOOL] Decoded URL: %s -> %s",
                    url[:120] + ("..." if len(url) > 120 else ""),
                    decoded[:120] + ("..." if len(decoded) > 120 else ""),
                )
            decoded_urls.append(decoded)

        logger.info(
            "[UPLOAD_PHOTO_TOOL] Retrieved %s image URLs",
            len(decoded_urls),
        )

        payload = {
            "imageUrls": decoded_urls,
            "count": len(decoded_urls),
            "message": f"成功获取 {len(decoded_urls)} 张照片的公网访问 URL",
        }
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(payload, ensure_ascii=False),
                }
            ]
        }

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[UPLOAD_PHOTO_TOOL] Failed to upload photos: {e}")
        raise RuntimeError(f"上传照片失败: {str(e)}") from e
