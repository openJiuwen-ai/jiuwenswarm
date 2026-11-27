# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""PushData 持久化管理器.

与 xy_channel pushdata-manager.ts 对齐，使用 JSON 文件存储推送记录。
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from jiuwenclaw.utils import logger

PUSHDATA_FILE = os.path.expanduser("~/.openclaw/pushData.json")
MAX_PUSHDATA_ITEMS = 1000


def _format_beijing_time() -> str:
    """格式化当前时间为 YYYYMMDD HHmmss（北京时间）."""
    utc8 = timezone(timedelta(hours=8))
    return datetime.now(utc8).strftime("%Y%m%d %H%M%S")


def _ensure_directory_exists(file_path: str) -> None:
    """确保目录存在."""
    directory = os.path.dirname(file_path)
    os.makedirs(directory, exist_ok=True)


def _read_pushdata_list() -> List[Dict[str, str]]:
    """读取 pushData 列表."""
    try:
        _ensure_directory_exists(PUSHDATA_FILE)
        if not os.path.exists(PUSHDATA_FILE):
            return []
        with open(PUSHDATA_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        if not content.strip():
            return []
        data = json.loads(content)
        if not isinstance(data, list):
            logger.warning("[PushDataManager] pushData.json is not an array")
            return []
        return data
    except (json.JSONDecodeError, OSError) as e:
        logger.error("[PushDataManager] Failed to read pushData: %s", e)
        return []


def _write_pushdata_list(items: List[Dict[str, str]]) -> None:
    """写入 pushData 列表（限制最多 MAX_PUSHDATA_ITEMS 条）."""
    try:
        _ensure_directory_exists(PUSHDATA_FILE)
        limited = items[-MAX_PUSHDATA_ITEMS:]
        with open(PUSHDATA_FILE, "w", encoding="utf-8") as f:
            json.dump(limited, f, ensure_ascii=False, indent=2)
        if len(items) > MAX_PUSHDATA_ITEMS:
            logger.info(
                "[PushDataManager] Trimmed pushData list from %s to %s items",
                len(items),
                len(limited),
            )
    except OSError as e:
        logger.error("[PushDataManager] Failed to write pushData: %s", e)
        raise


def save_push_data(data_detail: str) -> str:
    """保存推送数据，返回 pushDataId.

    Args:
        data_detail: 推送内容详情

    Returns:
        pushDataId (UUID)
    """
    push_data_id = str(uuid.uuid4())
    time_str = _format_beijing_time()

    item = {
        "pushDataId": push_data_id,
        "dataDetail": data_detail,
        "time": time_str,
    }

    items = _read_pushdata_list()
    items.append(item)
    _write_pushdata_list(items)

    logger.info(
        "[PushDataManager] Saved pushData: id=%s, time=%s, detail_len=%s",
        push_data_id[:8],
        time_str,
        len(data_detail),
    )
    return push_data_id


def _match_push_item(item: Dict[str, str], keyword: str) -> bool:
    """判断推送条目是否匹配关键词（忽略大小写）."""
    for field in ("dataDetail", "pushDataId"):
        value = item.get(field, "") or ""
        if keyword in value.lower():
            return True
    return False


def search_push_data(keywords: Optional[str] = None) -> List[Dict[str, str]]:
    """搜索推送数据（支持关键词模糊匹配）.

    Args:
        keywords: 搜索关键词，None 或空字符串时返回全部

    Returns:
        匹配的推送数据列表
    """
    items = _read_pushdata_list()

    if not keywords or not keywords.strip():
        return items

    lower = keywords.lower().strip()
    results = [item for item in items if _match_push_item(item, lower)]

    logger.info(
        "[PushDataManager] Search with keywords %r: found %s items",
        keywords,
        len(results),
    )
    return results


def get_all_push_data() -> List[Dict[str, str]]:
    """获取所有推送数据."""
    items = _read_pushdata_list()
    logger.info("[PushDataManager] Retrieved %s pushData items", len(items))
    return items


def clear_all_push_data() -> None:
    """清空所有推送数据."""
    _write_pushdata_list([])
    logger.info("[PushDataManager] Cleared all pushData")
