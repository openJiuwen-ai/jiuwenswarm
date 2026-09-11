# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Push result tool - 查看推送记录工具.

包含：
- view_push_result: 查看定时任务或推送消息的执行结果
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.utils import logger

from .pushdata_manager import search_push_data, get_all_push_data


@tool(
    name="view_push_result",
    description="""Views the execution results of scheduled tasks or push messages. Call this tool when the user says things like "check the execution result of my xxx scheduled task", "check my xxxx push messages" or similar.

Features:
- Supports keyword search: if the user mentions a specific task name or content, filter by keyword
- Without a keyword: returns the most recent push records (10 by default)
- Returned content includes: push ID, time, and content summary

Usage scenarios:
- "Check the execution results of my scheduled tasks from yesterday"
- "Show me the weather push messages"
- "Show the most recent push records"
- "Did my reminder task run?" """,
)
def view_push_result(
    keywords: Optional[str] = None,
    limit: Optional[int] = None,
) -> Dict[str, Any]:
    """查看推送记录（与 xy_channel view-push-result-tool.ts 对齐）.

    Args:
        keywords: 可选的搜索关键词，用于筛选推送记录
        limit: 返回的最大记录数，默认10条，最多50条

    Returns:
        content[0].text: JSON 字符串（success, count, items, message）
    """
    try:
        effective_limit = min(limit or 10, 50)
        kw = keywords.strip() if keywords and isinstance(keywords, str) else None
        logger.info(
            "[VIEW_PUSH_RESULT_TOOL] 开始查询 keywords=%s limit=%s",
            kw, effective_limit,
        )

        # 根据是否有关键词决定调用哪个方法
        results = search_push_data(kw) if kw else get_all_push_data()
        logger.info(
            "[VIEW_PUSH_RESULT_TOOL] 数据源返回 %d 条记录, 查询方式=%s",
            len(results), "关键词搜索" if kw else "全量查询",
        )

        # 按时间倒序排序（最新的在前）
        results.sort(key=lambda x: x.get("time", ""), reverse=True)

        # 限制返回条数
        results = results[:effective_limit]
        logger.info("[VIEW_PUSH_RESULT_TOOL] 截取后返回 %d 条记录", len(results))

        if not results:
            logger.info("[VIEW_PUSH_RESULT_TOOL] 无匹配记录, keywords=%s", kw)
            return {
                "content": [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "success": True,
                                "count": 0,
                                "items": [],
                                "message": (
                                    f'No push records found containing the keyword "{kw}"'
                                    if kw
                                    else "No push records yet"
                                ),
                            },
                            ensure_ascii=False,
                        ),
                    }
                ]
            }

        # 格式化返回结果
        formatted_items = []
        for item in results:
            detail = item.get("dataDetail", "")
            formatted_items.append(
                {
                    "pushDataId": item.get("pushDataId", "")[:8],
                    "fullPushDataId": item.get("pushDataId", ""),
                    "time": item.get("time", ""),
                    "dataDetail": (
                        detail[:200] + "..." if len(detail) > 200 else detail
                    ),
                    "fullLength": len(detail),
                }
            )

        logger.info(
            "[VIEW_PUSH_RESULT_TOOL] 查询完成, 返回 %d 条记录",
            len(formatted_items),
        )
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "success": True,
                            "count": len(formatted_items),
                            "totalMatched": len(results),
                            "items": formatted_items,
                            "message": (
                                f'Found {len(formatted_items)} push records containing "{kw}"'
                                if kw
                                else f"Returned the {len(formatted_items)} most recent push records"
                            ),
                        },
                        ensure_ascii=False,
                    ),
                }
            ]
        }

    except Exception as e:
        logger.error("[VIEW_PUSH_RESULT_TOOL] Failed: %s", e)
        return {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {
                            "success": False,
                            "error": str(e),
                            "message": "Failed to query push records",
                        },
                        ensure_ascii=False,
                    ),
                }
            ]
        }
