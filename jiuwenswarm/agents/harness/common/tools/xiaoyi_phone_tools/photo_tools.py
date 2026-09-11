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

from jiuwenswarm.common.utils import logger
from .utils import (
    execute_device_command,
    format_success_response,
    raise_if_device_error,
    ToolInputError,
)


@tool(
    name="search_photo_gallery",
    description="""Plugin description: Search for photos in the user's phone gallery

  Usage constraints: Call this tool when the user asks to find images from the phone gallery or album. Note this tool only supports searching the local gallery; it does not support searching cloud-space albums.

  Input/output overview:
  a. Retrieves matching photos based on the image description text, returning the photo's local mediaUri and thumbnailUri on the phone.
  b. The returned mediaUri and thumbnailUri are local paths that cannot be downloaded or accessed directly.
  To download, view, use, or display photos, use the upload_photo tool to convert the mediaUri or thumbnailUri into a publicly accessible URL.
  c. mediaUri is the path of the original image in the phone album; the image is larger in size and higher in resolution.
  d. thumbnailUri is the path of the image thumbnail in the phone album; the image is smaller in size with moderate resolution. Prefer this path as the input to the upload_photo tool, as it is less likely to cause upload timeouts.

  Search capability boundaries:
  a. Supports colloquial input: the rewriting model automatically extracts entities such as names, categories, and places; natural-language descriptions are supported (e.g. "photos of the puppy", "scenery shot in Nanjing")
  b. Supports album search: the album name can be included in query (e.g. "photos from the Xi'an trip album")
  c. Supports people search: requires that photos carry a person tag and a colloquial description (e.g. "photos of Zhang San")
  d. Relative time words not supported: expressions like "newest", "oldest", "earliest" are not supported; use a concrete time instead (e.g. "photos from 2024" rather than "photos from last year")
  e. Multi-entity queries not supported: "or" logic and time ranges are not supported (e.g. "photos of Nanjing or Shanghai", "photos from the last three years"); split them into multiple independent queries
  f. POI reverse geocoding not supported: a photo's location is a street address; searching by real venue names may find nothing
  g. Favorite awareness not supported: cannot detect whether a photo is marked as favorite
  h. Fine-grained varieties not supported: limited ability to identify specific breeds of animals, plants, etc.
  i. Note: POI extraction may be inaccurate: place names may act as semantic search conditions, so searching "xx Lake" may return photos of "yy River" or "zz Bay"

  Query optimization tips:
  a. Time queries: convert "newest", "last year", "the last three years" etc. into concrete years (e.g. "2024"; "2023 to 2025" must be split into three queries: "2023", "2024", "2025")
  b. Multi-condition queries: split "or" logic into multiple queries (e.g. "photos of Nanjing or Shanghai" -> first query "photos of Nanjing", then "photos of Shanghai")
  c. Atomic entities: make sure each query contains only one atomic entity (place, person name, item, etc.)
  d. Album names: if the album name is known, including it directly in query improves accuracy

  Notes:
  a. Only run this tool when the user explicitly asks to search the phone album or gallery. If the user only wants to find some image without specifying the data source, do not rashly call this plugin; prefer trying websearch or asking the user whether to search the phone gallery.
  b. The operation timeout is 60 seconds. Do not call this tool repeatedly; if it times out or fails, retry at most once.
  c. If the user's request contains multiple entities or a time range, proactively split it into multiple queries and inform the user.
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
            raise ToolInputError("Missing required parameter: query (search keyword)")

        query = query.strip()
        if not query:
            raise ToolInputError("query must not be empty")

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

        raise_if_device_error(outputs, "Failed to search photos")

        result = outputs.get("result")
        if not isinstance(result, dict):
            result = {}
        n = len(result.get("items", []))
        logger.info(f"[SEARCH_PHOTO_GALLERY_TOOL] Search completed, items={n}")

        return format_success_response(dict(outputs), f"Found {n} photos")

    except ToolInputError:
        raise
    except Exception as e:
        logger.error(f"[SEARCH_PHOTO_GALLERY_TOOL] Failed to search photos: {e}")
        raise RuntimeError(f"Failed to search photos: {str(e)}") from e


def _normalize_media_uris(param: Any) -> List[str]:
    """将 media_uris 规范为字符串列表（支持数组或 JSON 数组字符串）。"""
    if param is None:
        raise ToolInputError("Missing required parameter: media_uris")
    if isinstance(param, list):
        return param
    if isinstance(param, str):
        try:
            parsed = json.loads(param)
        except json.JSONDecodeError as e:
            raise ToolInputError(
                f"media_uris must be a valid JSON array string. Parse error: {e}"
            ) from e
        if not isinstance(parsed, list):
            raise ToolInputError("media_uris must parse into an array")
        return parsed
    raise ToolInputError(
        f"media_uris must be an array or a JSON array string; got type: {type(param).__name__}"
    )


def _decode_image_url_escapes(url: str) -> str:
    """与 upload-photo-tool.ts getPhotoUrls 一致：替换 URL 中的 \\u003d、\\u0026。"""
    return url.replace("\\u003d", "=").replace("\\u0026", "&")


@tool(
    name="upload_photo",
    description="""Tool capability: Upload local phone files and obtain publicly accessible URLs.

  Prerequisite tool call: before using this tool, you must first call the search_photo_gallery tool to obtain the photo's mediaUri or thumbnailUri
  Parameter description:
  a. The mediaUri values in the mediaUris input must exactly match the corresponding mediaUri or thumbnailUri from the search_photo_gallery results; do not modify them yourself. They must be file:// paths.
  b. Prefer the thumbnailUri from the search_photo_gallery results as input; thumbnailUri is a thumbnail whose resolution and file size are both well suited for displaying to the user. If thumbnailUri does not exist or the user asks for the original image, use the corresponding mediaUri from the search_photo_gallery results.
  c. media_uris is an array of the photos' local URIs on the phone (obtained from the search_photo_gallery tool response). Limit: at most 5 mediaUri values per call.

  Notes:
  a. The operation timeout is 60 seconds. Do not call this tool repeatedly; if it times out or fails, retry at most once.
  b. The image links returned by this tool are publicly accessible to the user. Download them locally if needed for subsequent operations; if they are to be shown to the user, return them directly in image markdown form.""",
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
            raise ToolInputError("The mediaUris array must not be empty")

        if len(normalized) > 5:
            raise ToolInputError(
                f"At most 5 mediaUri values are supported; got {len(normalized)}. Please split into batches."
            )

        for uri in normalized:
            if not isinstance(uri, str) or not uri.strip():
                raise ToolInputError("Each item in media_uris must be a non-empty string")

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
            "message": f"Successfully obtained public URLs for {len(decoded_urls)} photos",
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
        raise RuntimeError(f"Failed to upload photos: {str(e)}") from e
