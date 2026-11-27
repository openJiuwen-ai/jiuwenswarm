# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Push result tool - 查看推送记录工具.

包含：
- view_push_result: 查看定时任务或推送消息的执行结果
"""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from openjiuwen.core.foundation.tool import tool

from jiuwenclaw.utils import logger

from .pushdata_manager import search_push_data, get_all_push_data


@tool(
    name="view_push_result",
    description="""查看定时任务或推送消息的执行结果。当用户说"查看我xxx的定时任务执行结果"、"查看我的xxxx的推送消息"或类似语料时调用此工具。

功能说明：
- 支持关键词搜索：如果用户提到具体任务名称或内容，可以按关键词筛选
- 无关键词时：返回最近的推送记录（默认10条）
- 返回内容包括：推送ID、时间、内容摘要

使用场景：
- "查看我昨天的定时任务执行结果"
- "帮我看看天气推送消息"
- "查看最近的推送记录"
- "我的提醒任务执行了吗" """,
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
                                    f'未找到包含关键词"{kw}"的推送记录'
                                    if kw
                                    else "暂无推送记录"
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
                                f'找到 {len(formatted_items)} 条包含"{kw}"的推送记录'
                                if kw
                                else f"返回最近 {len(formatted_items)} 条推送记录"
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
                            "message": "查询推送记录失败",
                        },
                        ensure_ascii=False,
                    ),
                }
            ]
        }
