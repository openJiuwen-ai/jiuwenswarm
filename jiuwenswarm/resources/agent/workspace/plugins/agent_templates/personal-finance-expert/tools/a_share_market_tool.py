import json
import urllib.request

from openjiuwen.core.foundation.tool import Tool, ToolCard


class AShareMarketTool(Tool):
    """A股市场行情数据工具：获取实时股票行情、大盘指数、涨跌排行、板块表现及基金估值数据。

    数据源策略：
    - 个股行情 (quote) 和大盘指数 (market_index)：优先新浪财经获取基础行情，
      再从东方财富补充扩展字段（PE/PB/市值/换手率等）；新浪失败时回退东方财富。
    - 涨跌排行、板块表现、基金估值：使用东方财富。
    """

    SINA_BASE_URL = "https://hq.sinajs.cn"
    EAST_BASE_URL = "https://push2.eastmoney.com"
    FUND_URL = "https://fundgz.1234567.com.cn"

    STOCK_FIELDS = (
        "f43,f44,f45,f46,f47,f48,f50,f51,f52,"
        "f57,f58,f60,f116,f117,f162,f167,f168,f169,f170,f171"
    )
    LIST_FIELDS = "f2,f3,f4,f5,f6,f7,f8,f12,f14,f15,f16,f17,f18"
    SECTOR_FIELDS = "f2,f3,f4,f8,f12,f14,f128,f136,f140"

    # 新浪大盘指数代码映射: (sina_code, display_code, display_name)
    SINA_INDEX_CODES = [
        ("sh000001", "000001", "上证指数"),
        ("sz399001", "399001", "深证成指"),
        ("sz399006", "399006", "创业板指"),
        ("sh000300", "000300", "沪深300"),
        ("sh000688", "000688", "科创50"),
        ("sh000016", "000016", "上证50"),
    ]

    def __init__(self) -> None:
        super().__init__(
            ToolCard(
                id="a_share_market",
                name="a_share_market",
                description=(
                    "A股市场行情数据工具：获取实时股票行情、大盘指数、涨跌排行、"
                    "板块表现及基金估值数据。个股行情和大盘指数优先从新浪财经获取，"
                    "再从东方财富补充扩展字段（PE/PB/市值等）；"
                    "涨跌排行、板块表现和基金估值使用东方财富。"
                    "当用户需要查询股票行情、分析个股、了解大盘走势、"
                    "识别热点板块或查看基金估值时调用。"
                    "需配合 a-share-analysis 和 fund-analysis skill 使用。"
                ),
                input_params={
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "description": (
                                "操作类型：quote(个股实时行情)、market_index(大盘指数)、"
                                "stock_ranking(涨跌排行)、sector_performance(板块表现)、"
                                "fund_estimate(基金实时估值)"
                            ),
                            "enum": [
                                "quote",
                                "market_index",
                                "stock_ranking",
                                "sector_performance",
                                "fund_estimate",
                            ],
                        },
                        "stock_codes": {
                            "type": "string",
                            "description": "股票代码，多个用逗号分隔，如 600519,000858。action=quote时必填",
                        },
                        "rank_type": {
                            "type": "string",
                            "description": "排行类型：涨幅(涨幅榜)、跌幅(跌幅榜)、成交额(成交额榜)。action=stock_ranking时必填",
                            "enum": ["涨幅", "跌幅", "成交额"],
                        },
                        "sector_type": {
                            "type": "string",
                            "description": "板块类型：行业(行业板块)、概念(概念板块)。action=sector_performance时必填",
                            "enum": ["行业", "概念"],
                        },
                        "fund_code": {
                            "type": "string",
                            "description": "基金代码，如 005827。action=fund_estimate时必填",
                        },
                        "top_n": {
                            "type": "integer",
                            "description": "返回前N条数据，默认20",
                        },
                    },
                    "required": ["action"],
                },
            )
        )

    async def invoke(self, inputs, **kwargs):
        action = inputs.get("action", "")
        try:
            if action == "quote":
                return self._get_stock_quote(inputs)
            elif action == "market_index":
                return self._get_market_index(inputs)
            elif action == "stock_ranking":
                return self._get_stock_ranking(inputs)
            elif action == "sector_performance":
                return self._get_sector_performance(inputs)
            elif action == "fund_estimate":
                return self._get_fund_estimate(inputs)
            else:
                return {"success": False, "error": f"不支持的操作类型: {action}"}
        except Exception as e:
            return {"success": False, "error": f"工具执行异常: {e!s}"}

    async def stream(self, inputs, **kwargs):
        yield await self.invoke(inputs, **kwargs)

    # ------------------------------------------------------------------ #
    # HTTP 请求
    # ------------------------------------------------------------------ #

    def _sina_http_get(self, url: str) -> str:
        """新浪财经 API HTTP GET，使用 GBK 编码和新浪 Referer。"""
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "https://finance.sina.com.cn",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("gbk")

    def _east_http_get(self, url: str) -> str:
        """东方财富 API HTTP GET，使用 UTF-8 编码和东方财富 Referer。"""
        req = urllib.request.Request(
            url,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/120.0.0.0 Safari/537.36"
                ),
                "Referer": "https://www.eastmoney.com/",
            },
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8")

    # ------------------------------------------------------------------ #
    # 辅助方法
    # ------------------------------------------------------------------ #

    @staticmethod
    def _get_sina_prefix(code: str) -> str:
        """根据股票代码判断新浪市场前缀：sh=上海，sz=深圳。"""
        code = code.strip()
        if code.startswith("6"):
            return "sh"
        return "sz"

    @staticmethod
    def _get_east_prefix(code: str) -> str:
        """根据股票代码判断东方财富市场前缀：1=上海，0=深圳。"""
        code = code.strip()
        if code.startswith("6"):
            return "1"
        return "0"

    @staticmethod
    def _safe_float(value, default=0.0):
        """安全转换为浮点数，处理 '-' 等无效值。"""
        if value is None or value == "-" or value == "":
            return default
        try:
            return float(value)
        except (ValueError, TypeError):
            return default

    @staticmethod
    def _parse_sina_line(line: str, prefix: str):
        """解析新浪行情响应行，返回字段列表或 None。

        Args:
            line: 响应文本中的一行，如 ``var hq_str_sh600519="贵州茅台,...";``
            prefix: 新浪代码前缀，如 ``sh600519``
        """
        full_prefix = f'hq_str_{prefix}="'
        if full_prefix not in line:
            return None
        start = line.index(full_prefix) + len(full_prefix)
        end = line.index('";', start)
        data_str = line[start:end]
        if not data_str:
            return None
        return data_str.split(",")

    # ------------------------------------------------------------------ #
    # 新浪数据获取
    # ------------------------------------------------------------------ #

    def _sina_get_stock_quote(self, codes: list) -> list:
        """从新浪财经获取个股实时行情。

        新浪行情字段顺序：
        [0]名称 [1]开盘 [2]昨收 [3]最新 [4]最高 [5]最低
        [6]买一 [7]卖一 [8]成交量(股) [9]成交额(元)
        [20]日期 [21]时间
        """
        sina_codes = [f"{self._get_sina_prefix(c)}{c}" for c in codes]
        url = f"{self.SINA_BASE_URL}/list={','.join(sina_codes)}"
        raw = self._sina_http_get(url)

        results = []
        lines = raw.strip().splitlines()

        for i, code in enumerate(codes):
            sina_code = sina_codes[i]
            fields = None
            for line in lines:
                fields = self._parse_sina_line(line, sina_code)
                if fields is not None:
                    break

            if fields is None or len(fields) < 10:
                results.append({"code": code, "error": "新浪未返回有效数据"})
                continue

            name = fields[0]
            open_price = self._safe_float(fields[1])
            pre_close = self._safe_float(fields[2])
            price = self._safe_float(fields[3])
            high = self._safe_float(fields[4])
            low = self._safe_float(fields[5])
            volume = self._safe_float(fields[8])
            amount = self._safe_float(fields[9])
            date = fields[20] if len(fields) > 20 else ""
            time_str = fields[21] if len(fields) > 21 else ""

            change = round(price - pre_close, 4) if pre_close else 0.0
            change_pct = round((change / pre_close * 100), 2) if pre_close else 0.0
            amplitude = round(((high - low) / pre_close * 100), 2) if pre_close else 0.0

            results.append(
                {
                    "code": code,
                    "name": name,
                    "price": price,
                    "change": change,
                    "change_pct": change_pct,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "pre_close": pre_close,
                    "volume": volume,
                    "amount": amount,
                    "amplitude": amplitude,
                    "date": date,
                    "time": time_str,
                }
            )

        return results

    def _sina_get_market_index(self) -> list:
        """从新浪财经获取大盘指数数据。"""
        sina_codes = [item[0] for item in self.SINA_INDEX_CODES]
        url = f"{self.SINA_BASE_URL}/list={','.join(sina_codes)}"
        raw = self._sina_http_get(url)

        results = []
        lines = raw.strip().splitlines()

        for sina_code, display_code, display_name in self.SINA_INDEX_CODES:
            fields = None
            for line in lines:
                fields = self._parse_sina_line(line, sina_code)
                if fields is not None:
                    break

            if fields is None or len(fields) < 10:
                continue

            name = fields[0] or display_name
            open_price = self._safe_float(fields[1])
            pre_close = self._safe_float(fields[2])
            price = self._safe_float(fields[3])
            high = self._safe_float(fields[4])
            low = self._safe_float(fields[5])
            volume = self._safe_float(fields[8])
            amount = self._safe_float(fields[9])

            change = round(price - pre_close, 4) if pre_close else 0.0
            change_pct = round((change / pre_close * 100), 2) if pre_close else 0.0
            amplitude = round(((high - low) / pre_close * 100), 2) if pre_close else 0.0

            results.append(
                {
                    "code": display_code,
                    "name": name,
                    "price": price,
                    "change": change,
                    "change_pct": change_pct,
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "pre_close": pre_close,
                    "volume": volume,
                    "amount": amount,
                    "amplitude": amplitude,
                }
            )

        return results

    # ------------------------------------------------------------------ #
    # 东方财富数据获取
    # ------------------------------------------------------------------ #

    def _east_get_stock_quote(self, codes: list) -> list:
        """从东方财富获取个股实时行情（含 PE/PB/市值 等扩展字段）。"""
        results = []
        for code in codes:
            market = self._get_east_prefix(code)
            secid = f"{market}.{code}"
            url = (
                f"{self.EAST_BASE_URL}/api/qt/stock/get?"
                f"secid={secid}&fields={self.STOCK_FIELDS}&fltt=2&invt=2"
            )
            try:
                raw = self._east_http_get(url)
                data = json.loads(raw)
                if data.get("rc") == 0 and data.get("data"):
                    d = data["data"]
                    results.append(
                        {
                            "code": d.get("f57", code),
                            "name": d.get("f58", ""),
                            "price": self._safe_float(d.get("f43")),
                            "change": self._safe_float(d.get("f169")),
                            "change_pct": self._safe_float(d.get("f170")),
                            "open": self._safe_float(d.get("f46")),
                            "high": self._safe_float(d.get("f44")),
                            "low": self._safe_float(d.get("f45")),
                            "pre_close": self._safe_float(d.get("f60")),
                            "volume": self._safe_float(d.get("f47")),
                            "amount": self._safe_float(d.get("f48")),
                            "turnover_rate": self._safe_float(d.get("f168")),
                            "pe_ratio": self._safe_float(d.get("f162")),
                            "pb_ratio": self._safe_float(d.get("f167")),
                            "total_market_cap": self._safe_float(d.get("f116")),
                            "circulating_market_cap": self._safe_float(d.get("f117")),
                            "amplitude": self._safe_float(d.get("f171")),
                            "volume_ratio": self._safe_float(d.get("f50")),
                            "limit_up": self._safe_float(d.get("f51")),
                            "limit_down": self._safe_float(d.get("f52")),
                        }
                    )
                else:
                    results.append({"code": code, "error": "东方财富未返回数据"})
            except Exception as e:
                results.append({"code": code, "error": str(e)})
        return results

    def _east_get_market_index(self) -> dict:
        """从东方财富获取大盘指数数据。"""
        secids = "1.000001,0.399001,0.399006,1.000300,1.000688,1.000016"
        fields = "f2,f3,f4,f5,f6,f7,f8,f12,f14,f15,f16,f17,f18"
        url = (
            f"{self.EAST_BASE_URL}/api/qt/ulist.np/get?"
            f"fields={fields}&secids={secids}&fltt=2&invt=2"
        )
        raw = self._east_http_get(url)
        data = json.loads(raw)
        if data.get("rc") == 0 and data.get("data"):
            results = []
            for item in data["data"].get("diff", []):
                results.append(
                    {
                        "code": item.get("f12", ""),
                        "name": item.get("f14", ""),
                        "price": self._safe_float(item.get("f2")),
                        "change": self._safe_float(item.get("f4")),
                        "change_pct": self._safe_float(item.get("f3")),
                        "volume": self._safe_float(item.get("f5")),
                        "amount": self._safe_float(item.get("f6")),
                        "amplitude": self._safe_float(item.get("f7")),
                        "turnover_rate": self._safe_float(item.get("f8")),
                        "high": self._safe_float(item.get("f15")),
                        "low": self._safe_float(item.get("f16")),
                        "open": self._safe_float(item.get("f17")),
                        "pre_close": self._safe_float(item.get("f18")),
                    }
                )
            return {"success": True, "data": results, "count": len(results)}
        return {"success": False, "error": "东方财富未获取到指数数据"}

    # ------------------------------------------------------------------ #
    # 公开 action 方法
    # ------------------------------------------------------------------ #

    def _get_stock_quote(self, inputs: dict) -> dict:
        """获取个股实时行情：优先新浪，再从东方财富补充扩展字段。

        合并策略：
        - 新浪成功 + 东方财富成功 → 新浪基础字段 + 东方财富扩展字段 (source=sina+eastmoney)
        - 仅新浪成功 → source=sina
        - 仅东方财富成功 → source=eastmoney
        - 均失败 → 返回错误
        """
        stock_codes = inputs.get("stock_codes", "")
        if not stock_codes:
            return {"success": False, "error": "stock_codes 参数必填"}

        codes = [c.strip() for c in stock_codes.split(",") if c.strip()]

        # Step 1: 尝试新浪获取基础行情
        sina_map: dict = {}
        try:
            for item in self._sina_get_stock_quote(codes):
                if "error" not in item:
                    sina_map[item["code"]] = item
        except Exception:
            sina_map = {}

        # Step 2: 从东方财富获取扩展字段（PE/PB/市值等），同时作为回退
        east_map: dict = {}
        try:
            for item in self._east_get_stock_quote(codes):
                if "error" not in item:
                    east_map[item["code"]] = item
        except Exception:
            east_map = {}

        # Step 3: 合并结果
        results = []
        for code in codes:
            sina_item = sina_map.get(code)
            east_item = east_map.get(code)

            if sina_item and east_item:
                merged = dict(sina_item)
                merged["pe_ratio"] = east_item.get("pe_ratio", 0)
                merged["pb_ratio"] = east_item.get("pb_ratio", 0)
                merged["total_market_cap"] = east_item.get("total_market_cap", 0)
                merged["circulating_market_cap"] = east_item.get(
                    "circulating_market_cap", 0
                )
                merged["turnover_rate"] = east_item.get("turnover_rate", 0)
                merged["volume_ratio"] = east_item.get("volume_ratio", 0)
                merged["limit_up"] = east_item.get("limit_up", 0)
                merged["limit_down"] = east_item.get("limit_down", 0)
                merged["source"] = "sina+eastmoney"
                results.append(merged)
            elif sina_item:
                sina_item["source"] = "sina"
                results.append(sina_item)
            elif east_item:
                east_item["source"] = "eastmoney"
                results.append(east_item)
            else:
                results.append(
                    {"code": code, "error": "新浪和东方财富均未获取到数据"}
                )

        return {"success": True, "data": results, "count": len(results)}

    def _get_market_index(self, inputs: dict) -> dict:
        """获取主要大盘指数数据：优先新浪，失败则回退东方财富。"""
        # Step 1: 尝试新浪
        try:
            sina_results = self._sina_get_market_index()
            if sina_results:
                for item in sina_results:
                    item["source"] = "sina"
                return {
                    "success": True,
                    "data": sina_results,
                    "count": len(sina_results),
                }
        except Exception:
            sina_results = None

        # Step 2: 回退东方财富
        try:
            result = self._east_get_market_index()
            if result.get("success"):
                for item in result["data"]:
                    item["source"] = "eastmoney"
            return result
        except Exception as e:
            return {
                "success": False,
                "error": f"新浪和东方财富均获取指数数据失败: {e!s}",
            }

    def _get_stock_ranking(self, inputs: dict) -> dict:
        """获取涨跌排行数据（东方财富）。"""
        rank_type = inputs.get("rank_type", "涨幅")
        top_n = inputs.get("top_n") or 20

        sort_config = {
            "涨幅": {"fid": "f3", "po": "1"},
            "跌幅": {"fid": "f3", "po": "0"},
            "成交额": {"fid": "f6", "po": "1"},
        }

        config = sort_config.get(rank_type)
        if not config:
            return {"success": False, "error": f"不支持的排行类型: {rank_type}"}

        fs = "m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
        fields = self.LIST_FIELDS
        url = (
            f"{self.EAST_BASE_URL}/api/qt/clist/get?"
            f"pn=1&pz={top_n}&po={config['po']}&np=1&fltt=2&invt=2&"
            f"fid={config['fid']}&fs={fs}&fields={fields}"
        )

        try:
            raw = self._east_http_get(url)
            data = json.loads(raw)
            if data.get("rc") == 0 and data.get("data"):
                results = []
                for item in data["data"].get("diff", []):
                    results.append(
                        {
                            "code": item.get("f12", ""),
                            "name": item.get("f14", ""),
                            "price": self._safe_float(item.get("f2")),
                            "change_pct": self._safe_float(item.get("f3")),
                            "change": self._safe_float(item.get("f4")),
                            "volume": self._safe_float(item.get("f5")),
                            "amount": self._safe_float(item.get("f6")),
                            "amplitude": self._safe_float(item.get("f7")),
                            "turnover_rate": self._safe_float(item.get("f8")),
                            "high": self._safe_float(item.get("f15")),
                            "low": self._safe_float(item.get("f16")),
                            "open": self._safe_float(item.get("f17")),
                            "pre_close": self._safe_float(item.get("f18")),
                        }
                    )
                return {
                    "success": True,
                    "data": results,
                    "count": len(results),
                    "rank_type": rank_type,
                }
            return {"success": False, "error": "未获取到排行数据"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _get_sector_performance(self, inputs: dict) -> dict:
        """获取板块表现数据（东方财富）。"""
        sector_type = inputs.get("sector_type", "行业")
        top_n = inputs.get("top_n") or 20

        sector_fs = {
            "行业": "m:90+t:2",
            "概念": "m:90+t:3",
        }

        fs = sector_fs.get(sector_type)
        if not fs:
            return {"success": False, "error": f"不支持的板块类型: {sector_type}"}

        fields = self.SECTOR_FIELDS
        url = (
            f"{self.EAST_BASE_URL}/api/qt/clist/get?"
            f"pn=1&pz={top_n}&po=1&np=1&fltt=2&invt=2&"
            f"fid=f3&fs={fs}&fields={fields}"
        )

        try:
            raw = self._east_http_get(url)
            data = json.loads(raw)
            if data.get("rc") == 0 and data.get("data"):
                results = []
                for item in data["data"].get("diff", []):
                    results.append(
                        {
                            "code": item.get("f12", ""),
                            "name": item.get("f14", ""),
                            "change_pct": self._safe_float(item.get("f3")),
                            "change": self._safe_float(item.get("f4")),
                            "turnover_rate": self._safe_float(item.get("f8")),
                            "leader": item.get("f128", ""),
                            "leader_change_pct": self._safe_float(item.get("f136")),
                            "rising_count": self._safe_float(item.get("f140")),
                        }
                    )
                return {
                    "success": True,
                    "data": results,
                    "count": len(results),
                    "sector_type": sector_type,
                }
            return {"success": False, "error": "未获取到板块数据"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _get_fund_estimate(self, inputs: dict) -> dict:
        """获取基金实时估值数据（天天基金估值接口）。"""
        fund_code = inputs.get("fund_code", "")
        if not fund_code:
            return {"success": False, "error": "fund_code 参数必填"}

        url = f"{self.FUND_URL}/js/{fund_code}.js"

        try:
            raw = self._east_http_get(url)
            if "jsonpgz" in raw:
                start = raw.index("(") + 1
                end = raw.rindex(")")
                json_str = raw[start:end]
                data = json.loads(json_str)
                if data:
                    return {
                        "success": True,
                        "data": {
                            "fund_code": data.get("fundcode", ""),
                            "fund_name": data.get("name", ""),
                            "nav_date": data.get("jzrq", ""),
                            "unit_nav": data.get("dwjz", ""),
                            "estimated_nav": data.get("gsz", ""),
                            "estimated_change_pct": data.get("gszzl", ""),
                            "estimate_time": data.get("gztime", ""),
                        },
                    }
            return {
                "success": False,
                "error": "基金估值数据解析失败，可能基金代码不存在",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
