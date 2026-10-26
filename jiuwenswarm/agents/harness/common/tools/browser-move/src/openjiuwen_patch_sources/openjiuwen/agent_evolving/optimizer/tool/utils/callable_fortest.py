# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

import os
import json
import asyncio
from typing import Any, Dict, Optional, Callable

from fastmcp.client import Client
from fastmcp.client import SSETransport


def make_sync_mcp_caller(
    url: str,
    name: str = "Streamable HTTP Python Server", 
) -> Callable[[Dict[str, Any]], Any]:

    def call(tool_arguments: Dict[str, Any]) -> Any:
        async def _run():
            transport = SSETransport(url=url)
            client = Client(transport)

            async with client:  
                tool_name = tool_arguments["name"]
                arguments = tool_arguments.get("arguments")

                if isinstance(arguments, str):
                    try:
                        arguments = json.loads(arguments)
                    except json.JSONDecodeError as e:
                        raise ValueError(
                            "Failed to parse `arguments` as JSON string. "
                            f"Raw arguments: {arguments}"
                        ) from e

                result = await client.call_tool(tool_name, arguments)
                return result.content[0].text

        return asyncio.run(_run())

    return call


MCP_URL = os.getenv("MCP_URL", "")
MCP_NAME = os.getenv("MCP_NAME", "Streamable HTTP Python Server")


gaode_map_mcp_generic = make_sync_mcp_caller(MCP_URL)

schema = {
    "type": "function",
    "function": {
        "name": "SearchFunds",
        "description": """搜索基金、根据基金名称匹配基金代码。
通过名称（可用于确定基金代码）、代码、拼音、交易状态等信息进行搜索。
同时可以按照收益、限额、费率等进行排序，，在大部分情况都需要此工具。
（注意如果使用了keyword，就不要使用“分类”这个参数，
另外returnYear指的是近一年收益）""".strip(),
        "parameters": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "description": """分类 (可选值: '', '不限', '
偏股型', '指数型', 
'QDII型', '商品型', '债券型', '货币型', 
'国企改革', '工业4.0', '国防军工', 
'城镇化', '消费', '节能环保', '美丽中国',
 '养老', '价值蓝筹', '金融', '一带一路', 
'农林牧渔', '资源', 'TMT', '新能源', 
'文化传媒', '健康中国', '新兴产业', '量化投资', '定增', 
'逆向投资', '沪港深', '量化对冲', '打新', 
'股票型', '偏股混合型', '平衡混合型', '灵活配置型', 
'偏债混合型', '综合指数', '规模指数', '策略指数', 
'风格指数', '行业主题指数', '定制指数', '债券指数', 
'国际股票型', '国际混合型', '国际债券型', 
'国际另类投资', '全球市场', '美国市场', '欧洲市场', 
'香港市场', '亚太市场', '新兴市场', '大中华市场', 
'黄金', '白银', '油气', '纯债', '一级债', 
'二级债', '高杠杆', '利率债', '信用债', 
'可转债', '偏股债')""".strip()
                },
                "keyword": {
                    "type": "string",
                    "description": "基金名称关键字，支持分词搜索"
                },
                "size": {
                    "type": "number",
                    "description": "每页数量"
                },
                "sortOrder": {
                    "type": "string",
                    "description": "选择排序的顺序，如果是查找最大、最多等，可以是\"降序\"，否则为\"升序\" (可选值: '', '升序', '降序')"
                },
                "tradeStatus": {
                    "type": "string",
                    "description": "交易状态 (可选值: '', '不限', '正常开放', '认购期', '暂停申购', '暂停赎回', '暂停交易')"
                },
                "sortColumn": {
                    "type": "string",
                    "description": "选择要排序的列，可选值：成立日期、基金规模、收益率、近一年收益、起购金额、基金限额、选股能力、择时能力、最新股票仓位、综合费率、跟踪误差、七日年化收益率、万份收益"
                },
                "page": {
                    "type": "number",
                    "description": "页码，从0开始"
                }
            }
        }
    }
}
description = json.dumps(schema, ensure_ascii=False)
tool = {'name': 'SearchFunds', 'description': description}
