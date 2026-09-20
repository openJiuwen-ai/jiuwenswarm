#!/usr/bin/env python3
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""OpenAI 兼容 Mock LLM HTTP 服务，供 Enterprise Runtime 压测与 E2E 联调使用。

实现 ``POST /v1/chat/completions``（流式 SSE / 非流式 JSON）、``GET /health``、``GET /v1/models``，
并提供压测场景常用选项（``--host``、请求统计、毫秒时间戳日志）。

``--profile loadtest`` 时按用户输入关键词自动路由多场景 Mock 流程：

- **travel**（默认）：小说《旅行的意义》多工具 Agent 流程
- **scheduled_task**：用户消息含「定时任务」等关键词
- **cron_delivery**：定时任务到点触发，返回一句喝水提醒
- **skill**：Deep 工具面 todo / write_file / send_file 生成北京 3 日游攻略（不安装 skillnet、不调 skill_step）
- **file**：用户消息含「文件」关键词

典型用法（项目根目录）::

    # E2E / --flow single：固定 mock token（数量、间隔可配）
    uv run python packages/jiuwenclaw-ee/claw_manager/scripts/mock_llm_server.py \\
        --port 19999 --profile e2e --stream-token-count 5 --stream-token-interval 0.05
    # 等价：MOCK_LLM_STREAM_TOKEN_COUNT=5 MOCK_LLM_STREAM_TOKEN_INTERVAL=0.05 \\
    #     python .../mock_llm_server.py --port 19999 --profile e2e

    # 压测：模拟真实 Agent 多工具流程（todo / 长文 / write_file / read_file / send_file）
    uv run python packages/jiuwenclaw-ee/claw_manager/scripts/mock_llm_server.py \\
        --host 0.0.0.0 --port 19999 --profile loadtest --novel-chars 32000

AgentServer / Gateway 侧模型配置示例（经 Runtime ``_agent_env_vars`` 或 model_template）::

    API_BASE=http://127.0.0.1:19999/v1
    API_KEY=mock-key
    MODEL_PROVIDER=OpenAI
    MODEL_NAME=mock-model
    LLM_SSL_VERIFY=false
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import signal
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

DEFAULT_STREAM_TOKEN_COUNT = 20
DEFAULT_STREAM_TOKEN_INTERVAL_S = 2.0
LOADTEST_STREAM_TOKEN_COUNT = 5
LOADTEST_STREAM_TOKEN_INTERVAL_S = 0.05
LOADTEST_NOVEL_CHARS = 32_000
LOADTEST_STREAM_CHUNK_CHARS = 256
LOADTEST_CHAT_EXCERPT_CHARS = 6_000
LOADTEST_WRITE_MAX_CHARS = 32_000

_LOADTEST_NOVEL_MARKERS = ("旅行的意义", "人生的意义", "十万字", "txt")
# 单独「小说」太宽，会误匹配 Agent 回写的 intro 文案；用下方 echo 检测兜底
_TRAVEL_ASSISTANT_ECHO_MARKERS = (
    "我看到你希望我专注于",
    "让我重新规划，专注于创作",
    "而不是立即尝试完成整部",
)
_TRAVEL_COMPLETION_ECHO_MARKERS = (
    "完成！我已经为你创作了小说",
    "开篇章节约6000字",
    "如需继续创作后续章节",
)
_SKILL_ECHO_MARKERS = (
    "全部完成！✅",
    "任务完成总结",
    "北京3日游攻略已生成",
    "有什么需要调整或补充的，随时告诉我",
)
_FILE_ECHO_MARKERS = (
    "已经把扩写好的作文发给你了",
    "纯汉字约 5400 字",
    "字数核对：纯汉字",
)
_CRON_ECHO_MARKERS = (
    "喝水提醒已创建",
    "到点后会通过 web 频道向你推送提醒",
    "执行完成后自动删除",
)
# skill 场景除关键字外，常见用户表述
_SKILL_INTENT_EXTRA = ("skillnet", "openclaw.tours", "旅游攻略技能", "北京3日游", "旅游攻略")
_NOVEL_FILENAME = "旅行的意义_开篇完整版.txt"


# ---------------------------------------------------------------------------
# 多场景路由：根据用户输入关键词选择 Mock 对话流程（loadtest profile）
# ---------------------------------------------------------------------------
class MockScenario:
    """Mock LLM 联调场景标识。"""

    TRAVEL = "travel"
    SCHEDULED_TASK = "scheduled_task"
    CRON_DELIVERY = "cron_delivery"
    SKILL = "skill"
    FILE = "file"


# loadtest 联调脚本的固定场景顺序（travel 完成后用户发 skill 时，Agent 可能尚未把新消息注入 LLM）
_LOADTEST_SCENARIO_SEQUENCE = (
    MockScenario.TRAVEL,
    MockScenario.SKILL,
    MockScenario.FILE,
    MockScenario.SCHEDULED_TASK,
)


# 优先级：cron_delivery > scheduled_task > skill > file > travel(默认)
_SCHEDULED_TASK_KEYWORDS = ("定时任务", "定时提醒")
_SKILL_KEYWORDS = ("skill", "技能")
# 不要用单独的「文件」——用户包装 prompt / 系统提示里几乎总会出现，会把小说场景串到扩写
_FILE_KEYWORDS = ("童趣的春天", "扩写", "作文")

# ---------------------------------------------------------------------------
# 定时任务场景常量
# ---------------------------------------------------------------------------
_CRON_INTRO = ""
_CRON_TZ = "Asia/Shanghai"
_CRON_DELAY_SECONDS = 60


def _oneshot_cron_expr(*, delay_seconds: int = _CRON_DELAY_SECONDS, timezone_name: str = _CRON_TZ) -> str:
    """生成约 delay_seconds 后触发的 7 段一次性 Quartz cron_expr。"""
    dt = datetime.now(ZoneInfo(timezone_name)) + timedelta(seconds=delay_seconds)
    return f"{dt.second} {dt.minute} {dt.hour} {dt.day} {dt.month} ? {dt.year}"


def _cron_job_args(*, delay_seconds: int = _CRON_DELAY_SECONDS) -> dict[str, Any]:
    """当前 cron_create_job schema：必填 name/cron_expr/timezone/description。"""
    return {
        "name": "喝水提醒",
        "cron_expr": _oneshot_cron_expr(delay_seconds=delay_seconds),
        "timezone": _CRON_TZ,
        "description": "🥤 喝水时间到啦！记得喝杯水，保持水分摄入～",
        "targets": "web",
        "enabled": True,
    }
_CRON_DONE_MESSAGE = (
    "✅ 喝水提醒已创建！\n\n"
    "⏰ 执行时间：1 分钟后\n\n"
    "📝 提醒内容：🥤 喝水时间到啦！记得喝杯水，保持水分摄入～\n\n"
    "📡 投递频道：web\n\n"
    "🔔 一次性任务，到点后会通过 web 频道向你推送提醒，执行完成后自动删除\n\n"
    "如果之后想取消，跟我说一声就行～"
)
_CRON_WAKE_MARKERS = ("喝水时间到啦",)
_CRON_DELIVERY_MESSAGE = "🥤 喝水时间到啦！记得喝杯水，保持水分摄入～"
_CRON_CONTEXT_PATH_RE = re.compile(r"/context/cron_[a-z0-9_]+_context")
# ---------------------------------------------------------------------------
# 技能场景常量（Deep 已注册工具：todo / write_file / send_file_to_user）
#
# 不调用 install_skill（会打真实 GitHub）、skill_step（dev-stable 无此工具）、
# free_search / fetch_webpage / skill_complete（当前 resource_mgr 未挂这些名字）。
# ---------------------------------------------------------------------------
# 场景切换后 session 内仍有 travel 等待办，skill 需 force=true 才能覆盖创建新任务列表。
_SKILL_TODO_CONTENTS = (
    "生成北京3日游攻略",
    "交付攻略文档",
)
_SKILL_INTRO = (
    "我来帮你制作北京3日游攻略。先列出待办，再写文档并发给你。"
    "不安装技能、不搜索网页、不提问。\n\n"
    "[当前步骤: 生成北京3日游攻略]\n"
)
_SKILL_BEFORE_WRITE_INTRO = (
    "[当前步骤: 生成北京3日游攻略]\n\n"
    "现在把北京3日游攻略写入 markdown：\n"
)
_SKILL_BEFORE_SEND_INTRO = "攻略已写好，现在把文件发给你：\n"
_BEIJING_GUIDE_FILENAME = "北京3日游旅游攻略.md"
_BEIJING_GUIDE_PATH = Path(__file__).resolve().parent / _BEIJING_GUIDE_FILENAME
_BEIJING_GUIDE_CONTENT = """# 🏯 北京3日游旅游攻略

> 由 OpenClaw Tour Planner 技能生成 | 数据来源：Nominatim · Open-Meteo · Wikivoyage · 北京旅游网

---

## 📋 行程概览

| 项目 | 详情 |
|------|------|
| **目的地** | 北京（39.90°N, 116.41°E，海拔47m，GMT+8） |
| **游玩天数** | 3天 |
| **推荐季节** | 4月-10月（旺季），当前为7月盛夏 |
| **预算（单人）** | 约 ¥1,300 - ¥2,200（不含往返大交通） |
| **行程主题** | 皇城经典 → 长城雄关 → 皇家园林与胡同文化 |

---

## 🌤️ 近期天气预报（2026年7月）

> 数据来源：Open-Meteo 免费天气API

| 日期 | 最高温 | 最低温 | 降水量 | 天气状况 | 风速 |
|------|--------|--------|--------|----------|------|
| 7月20日（周一） | 31.9°C | 22.5°C | 11.1mm | ⛈ 雷暴 | 11.9 km/h |
| 7月21日（周二） | 30.5°C | 21.2°C | 0.5mm | 🌧 小雨 | 11.6 km/h |
| 7月22日（周三） | 29.5°C | 24.7°C | 6.3mm | ⛈ 雷暴 | 11.3 km/h |
| 7月23日（周四） | 28.5°C | 21.8°C | 18.1mm | ⛈ 雷暴 | 9.1 km/h |
| **7月24日（周五）** | **29.7°C** | **22.8°C** | **0.0mm** | **☁ 阴天** | **6.0 km/h** |
| 7月25日（周六） | 32.9°C | 24.3°C | 0.6mm | 🌧 小雨 | 6.1 km/h |
| 7月26日（周日） | 33.3°C | 26.2°C | 1.8mm | ⛈ 雷暴 | 6.5 km/h |

### ☀️ 天气建议
- **最佳出行日**：7月24日（周五），阴天无降水，气温适宜
- 北京7月盛夏高温多雨，**务必携带雨具和防晒用品**
- 建议早出晚归，避开正午高温时段（11:00-14:00）
- 雷暴天气时避免在长城等高处停留

---

## 📅 Day 1：中轴线皇城经典游

> 穿越六百年紫禁城，漫步中轴线上的皇城根下

### 🌅 上午：天安门广场 → 故宫博物院

| 时间 | 活动 | 备注 |
|------|------|------|
| 08:00 | 天安门广场观升旗/游览 | 广场免费开放，建议早起避开人流 |
| 09:00 | 故宫博物院（紫禁城）入馆 | ⚠️ **需提前在官网预约购票** |
| 09:00-12:00 | 游览故宫：午门→三大殿→后三宫→御花园 | 推荐游览3小时 |

**故宫实用信息：**
- 🕐 开放时间（旺季4-10月）：8:30-17:00，16:00停止入馆
- 🎫 门票：旺季60元/人（珍宝馆10元、钟表馆10元另购）
- 📞 咨询电话：400-950-1925
- 🔍 紫禁城南北长961m，东西宽753m，城墙高10m，护城河宽52m
- 💡 提示：故宫实行实名制预约，每日限流，务必提前1-7天预约

### 🌞 下午：景山公园 → 北海公园

| 时间 | 活动 | 备注 |
|------|------|------|
| 12:30 | 午餐：故宫东华门附近餐厅 | 可品尝老北京炸酱面 |
| 14:00 | 景山公园 | 登万春亭俯瞰故宫全景，门票2元 |
| 15:30 | 北海公园 | 皇家御苑，白塔、九龙壁、泛舟太液池 |
| 17:00 | 公园闭园前结束游览 | 北海门票10元 |

### 🌃 晚上：王府井大街

| 时间 | 活动 | 备注 |
|------|------|------|
| 18:00 | 晚餐：王府井小吃街 / 全聚德王府井店 | 北京烤鸭必尝！ |
| 19:30 | 王府井步行街逛街购物 | 北京最繁华商业街之一 |
| 21:00 | 返回酒店休息 | 建议住二环内，方便出行 |

---

## 📅 Day 2：长城雄关 + 奥运风采

> 登万里长城做好汉，赏鸟巢水立方夜景

### 🌅 上午：八达岭长城

| 时间 | 活动 | 备注 |
|------|------|------|
| 07:00 | 从市区出发前往八达岭 | 建议早出发，避开高温和人流高峰 |
| 08:30 | 抵达八达岭长城，开始登城 | 旺季开放，推荐游览3小时 |
| 08:30-11:30 | 攀登长城（南段1176m / 北段2565m） | 北段更壮观，南段人少 |

**八达岭长城实用信息：**
- 📍 地址：北京市延庆区军都山关沟古道北口
- 🕐 开放时间（旺季4-10月）：6:30-16:30（夏季可能延长）
- 🎫 门票：旺季40元/人（缆车另购：单程100元，往返140元）
- 📞 购票咨询：010-69121474（9:00-16:30）
- 🏆 国家5A级景区，1987年列入世界文化遗产
- 🚗 停车场可容纳1500辆机动车，高峰时有免费摆渡车

**交通方式（三选一）：**
1. **S2线市郊铁路**：黄土店站→八达岭站，约1小时，可刷公交卡
2. **877路公交**：德胜门→八达岭，约1.5小时，票价12元
3. **自驾/打车**：京藏高速（G6），约1小时

### 🌞 下午：奥林匹克公园

| 时间 | 活动 | 备注 |
|------|------|------|
| 12:30 | 午餐：八达岭景区或返程途中 | |
| 14:30 | 返回市区，前往奥林匹克公园 | |
| 15:30 | 鸟巢（国家体育场）外景参观 | 可购票入内参观 |
| 16:30 | 水立方（国家游泳中心）外景 | 蓝色立方体建筑，夜景灯光绝美 |

### 🌃 晚上：什刹海 / 后海

| 时间 | 活动 | 备注 |
|------|------|------|
| 18:00 | 晚餐：什刹海周边餐厅 | 涮羊肉推荐东来顺什刹海店 |
| 19:30 | 后海酒吧街漫步 | 霓虹倒映湖面，体验胡同夜生活 |
| 20:30 | 什刹海胡同夜游 | 感受老北京的烟火气 |
| 22:00 | 返回酒店 | |

---

## 📅 Day 3：皇家园林 + 胡同文化

> 颐和园中赏湖光山色，南锣鼓巷品胡同烟火

### 🌅 上午：颐和园

| 时间 | 活动 | 备注 |
|------|------|------|
| 08:30 | 抵达颐和园（东宫门/北宫门入） | 建议从北宫门入，逆时针游览 |
| 09:00-12:00 | 游览颐和园 | 中国现存最大皇家园林，世界文化遗产 |

**颐和园游览路线推荐：**
- 北宫门→苏州街→万寿山→佛香阁→长廊→排云殿→昆明湖→十七孔桥→铜牛→东宫门出
- 🎫 门票：旺季30元/人（联票含佛香阁60元）
- 🕐 开放时间：6:30-18:00（旺季）
- 💡 园内可乘游船游昆明湖（另收费）

### 🌞 下午：天坛公园 → 南锣鼓巷

| 时间 | 活动 | 备注 |
|------|------|------|
| 12:30 | 午餐：天坛附近餐厅 | |
| 14:00 | 天坛公园 | 明清皇帝祭天祈谷之处，世界文化遗产 |
| 14:00-16:00 | 游览天坛：祈年殿→回音壁→圜丘 | 🎫 联票34元/人 |
| 16:30 | 前往南锣鼓巷 | 地铁6号线直达 |
| 17:00 | 南锣鼓巷胡同文化体验 | 700年历史胡同，文艺小店+特色小吃 |

**南锣鼓巷亮点：**
- 北京最古老的街区之一，元大都时期已成巷
- 两侧16条胡同整齐排列，呈"鱼骨状"格局
- 文艺店铺、手作工坊、特色咖啡馆云集
- 可品尝文宇奶酪店、炸酱面、糖葫芦等北京小吃

### 🌃 晚上：告别晚餐

| 时间 | 活动 | 备注 |
|------|------|------|
| 18:30 | 告别晚餐：全聚德/大董烤鸭 | 北京烤鸭是必吃的告别仪式 |
| 20:00 | 簋街夜宵（可选） | 北京最有名的夜宵美食街 |
| 21:00 | 返程准备 / 返回酒店 | |

---

## 💰 预算估算（中档标准，单人）

| 费用类别 | 明细 | 预估金额 |
|----------|------|----------|
| **住宿** | 经济型/中档酒店 × 2晚 | ¥600 - ¥1,000 |
| **餐饮** | 早餐+午餐+晚餐 × 3天 | ¥300 - ¥600 |
| **交通** | 地铁/公交日常 + 长城往返 | ¥100 - ¥200 |
| **门票** | 故宫60+长城40+颐和园30+天坛34+景山2+北海10 | ¥176 |
| **其他** | 纪念品/零食/应急 | ¥100 - ¥225 |
| **合计** | | **¥1,276 - ¥2,201** |

### 💡 省钱小贴士
- 办理北京公交一卡通，地铁公交通用，有折扣
- 故宫、长城等热门景点提前在官网预约，可避免黄牛加价
- 颐和园、天坛等公园可选择不买联票，只购基础门票
- 餐饮选择胡同里的老字号小店，性价比高且地道

---

## 🎒 打包清单

### 必备物品
- [ ] 身份证（景点实名制预约必备）
- [ ] 手机 + 充电宝（导航、电子票、拍照）
- [ ] 雨伞/雨衣（7月雷暴频繁）
- [ ] 防晒霜 SPF50+（盛夏紫外线强）
- [ ] 太阳帽/遮阳伞
- [ ] 舒适运动鞋（长城爬坡需要）
- [ ] 充足饮用水（随身携带，及时补水）

### 建议携带
- [ ] 薄外套（室内空调冷/早晚温差）
- [ ] 驱蚊液（公园水边蚊虫多）
- [ ] 小零食（长城游览时间较长）
- [ ] 相机（故宫、长城出片绝佳）
- [ ] 旅行装洗漱用品
- [ ] 常用药品（创可贴、肠胃药、藿香正气水防中暑）

---

## 📱 实用信息

### 🚇 交通出行
- 北京地铁覆盖主要景点，推荐办公交一卡通
- 故宫：地铁1号线天安门东/西站
- 八达岭：S2线市郊铁路或877路公交
- 颐和园：地铁4号线北宫门站
- 天坛：地铁5号线天坛东门站
- 南锣鼓巷：地铁6号线南锣鼓巷站

### 🎫 门票预约提醒
| 景点 | 预约方式 | 提前预约时间 |
|------|----------|-------------|
| 故宫博物院 | 故宫官网 dpm.org.cn | 提前1-7天 |
| 八达岭长城 | 八达岭官网 badaling.cn | 提前1-7天 |
| 颐和园 | 颐和园官方公众号/官网 | 提前1-7天 |
| 天坛公园 | 天坛官方公众号/官网 | 建议提前1-3天 |

### 🍜 北京必吃美食
| 美食 | 推荐餐厅 | 参考价格 |
|------|----------|----------|
| 北京烤鸭 | 全聚德、大董、便宜坊 | ¥150-300/人 |
| 涮羊肉 | 东来顺、聚宝源 | ¥100-200/人 |
| 炸酱面 | 海碗居、方砖厂69号 | ¥30-50/碗 |
| 卤煮火烧 | 小肠陈、门框胡同百年卤煮 | ¥25-40/碗 |
| 豆汁焦圈 | 护国寺小吃、锦芳小吃 | ¥10-20/份 |
| 驴打滚/艾窝窝 | 稻香村、护国寺小吃 | ¥10-25/份 |

---

## ⚠️ 注意事项

1. **防暑防雨**：7月北京高温多雨，雷暴天气频繁，务必携带雨具，及时补水
2. **提前预约**：故宫等热门景点实行实名制限流，务必提前预约，现场不售票
3. **避开高峰**：周末和节假日人流大，建议工作日出行；长城建议早出发
4. **文明游览**：故宫、长城等均为世界文化遗产，请勿触摸文物、勿乱刻乱画
5. **交通规划**：北京城区面积大，景点间距离较远，合理规划路线可节省大量时间
6. **支付方式**：几乎全场景支持微信/支付宝，建议下载并绑定

---

## 🗺️ 行程路线图

```
Day 1: 天安门广场 → 故宫 → 景山 → 北海 → 王府井
        （中轴线一带，步行+地铁，全天集中在二环内）

Day 2: 八达岭长城（北郊延庆） → 奥林匹克公园（北城） → 什刹海（二环内）
        （向北出发，返程沿途游览，晚间回市区）

Day 3: 颐和园（西北郊） → 天坛（南城） → 南锣鼓巷（二环内）
        （由西北向东南，最后在胡同收尾）
```

---

> 📌 本攻略由 **OpenClaw Tour Planner** 技能自动生成
> 数据来源：Nominatim（地理编码）、Open-Meteo（天气）、Wikivoyage（旅行指南）、北京旅游网（景点信息）
> 生成时间：2026年7月20日 | 技能版本：v1.0.2 | License: MIT

祝你旅途愉快！🎉
"""


def _load_beijing_guide_content() -> str:
    """优先读同目录攻略文件；缺失时回退到内嵌全文。"""
    try:
        return _BEIJING_GUIDE_PATH.read_text(encoding="utf-8")
    except OSError:
        return _BEIJING_GUIDE_CONTENT


_SKILL_DONE_MESSAGE = (
    "全部完成！✅ 以下是任务总结：\n\n"
    "## ✅ 任务完成总结\n\n"
    "### 北京3日游攻略已生成\n"
    f"📄 **攻略文件**：`{_BEIJING_GUIDE_FILENAME}`\n\n"
    "#### 📅 三日行程速览\n\n"
    "| 日期 | 主题 | 核心景点 |\n"
    "|------|------|----------|\n"
    "| Day 1 | 中轴线经典之旅 | 天安门广场 → 故宫 → 景山公园 → 北海公园 → 王府井 |\n"
    "| Day 2 | 长城壮游 + 奥运地标 | 八达岭长城 → 鸟巢/水立方 |\n"
    "| Day 3 | 皇家园林与胡同文化 | 颐和园 → 天坛 → 南锣鼓巷 → 什刹海 |\n\n"
    "#### 💰 预算参考\n\n"
    "- **经济型**：¥1,500-2,200/人\n"
    "- **中等消费**：¥2,500-3,500/人\n\n"
    "#### 📖 攻略亮点\n\n"
    "- 🌤️ 7月北京天气与穿衣指南（防暑防雨必备）\n"
    "- 🍜 北京必吃美食清单（烤鸭、炸酱面、铜锅涮肉等8道）\n"
    "- 🎫 各景点门票价格及开放时间\n"
    "- 📱 景点预约提醒（故宫需提前7天预约！）\n"
    "- 🚇 交通出行方案（地铁/公交/S2线/网约车）\n"
    "- ⚠️ 8条实用注意事项\n\n"
    "💡 **提醒**：故宫等热门景点需提前在官方微信公众号预约，切勿到现场才买票。"
    "7月北京炎热多雨，务必做好防晒和防雨准备！\n\n"
    "有什么需要调整或补充的，随时告诉我！😊"
)
# ---------------------------------------------------------------------------
# 文件扩写场景常量（童趣的春天）
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent
_SPRING_ESSAY_SOURCE = "童趣的春天.md"
_SPRING_ESSAY_OUTPUT = "童趣的春天_扩写版.md"
_SPRING_ESSAY_TARGET_CHARS = 6000
_SPRING_ESSAY_SOURCE_PATH = _SCRIPT_DIR / _SPRING_ESSAY_SOURCE
_HTTP_URL_RE = re.compile(r"https?://[^\s\"'\\<>]+")
_FILE_DONE_MESSAGE = (
    "已经把扩写好的作文发给你了 ✅\n\n"
    "这次扩写保留了原文的五个场景（清晨桃树、午后田野、放风筝、春夜蛙鸣、结尾怀念），"
    "并在此基础上做了这些丰富：\n\n"
    "细节加深：蜜蜂采花粉、花瓣雨、编花环的手法、捉蝴蝶的过程、"
    "做风筝扎骨架糊纸、青团现采现蒸的香气\n\n"
    "新增场景：清明青团、春雨踩水坑、雨后田埂踩脚印、捉蜗牛数蚯蚓，让\"童趣\"更立体\n\n"
    "情感升华：结尾加入\"只要春风还在吹，记忆就会一年年醒来\"的呼应，照应开头\n\n"
    "字数核对：纯汉字约 5400 字，全文约 6200 字（含中文标点与空白；"
    "学校作文一般按汉字+标点计），达到约 6000 字要求。"
    "如果你希望纯汉字也稳过 6000，或想调整某个场景的篇幅、增删某段，告诉我即可。"
)
_SPRING_EXPANSION_SECTIONS = (
    (
        "桃花开得最盛的那几天，风一吹，粉色的花瓣便簌簌落下，像一场温柔的花瓣雨。"
        "我们仰着头在树下转圈，任花瓣落在头发和肩膀上，笑声惊起了花间的蜜蜂。"
        "它们忙着采花粉，翅膀在阳光下闪着金色的光，我们踮起脚，学着大人的样子"
        "轻轻把花瓣拢进掌心，再撒向空中，看它们打着旋儿飘远。"
    ),
    (
        "编花环是午后田野里的必修课。先选几朵颜色最艳的野花，再把细长的草茎一根根理顺，"
        "从中间对折、交叉、缠绕，动作要轻，别把花瓣揉皱。花环戴在头上，我们便觉得自己"
        "成了春天里的小公主和小王子。捉蝴蝶更是热闹，有人负责惊飞，有人负责用网兜兜住，"
        "白色的、黄色的、带斑点的蝴蝶在花丛里翩翩起舞，像极了会飞的花朵。"
    ),
    (
        "做风筝的日子，爸爸会找来旧报纸和竹条，先扎出骨架，再一层层糊上纸。"
        "我在风筝上画上太阳、云朵和小鸟，等浆糊干透，春风正好，我们便跑到空旷的草地上。"
        "线轴在手中转动，风筝一点点升高，最后变成天空中的一个小点，心也随着风筝飞向了远方。"
    ),
    (
        "清明前后，奶奶会在灶台前蒸青团。艾草是清早从田埂边现采的，糯米团子裹进豆沙馅，"
        "锅盖一掀，清香便漫满整个院子。我们围在灶台边等，烫着嘴也要先咬上一口，"
        "软糯里带着草木的气息，那是春天最实在的味道。"
    ),
    (
        "春雨过后，村口的土路积起浅浅的水坑。我们脱掉鞋袜，光着脚踩进去，"
        "水花溅到裤脚上也不管，只听见啪嗒啪嗒的声响，像春天在和我们打招呼。"
        "雨后的田埂松软湿润，我们故意踩出一串歪歪扭扭的脚印，再蹲下来数泥土里探头的蜗牛和蚯蚓，"
        "比谁找到的多，笑声惊飞了停在电线杆上的麻雀。"
    ),
    (
        "春夜的蛙鸣一声接一声，像是大地的呼吸。我们躺在院子里，看星星一颗一颗亮起来，"
        "听大人们讲春天里万物苏醒的故事。那些关于花、关于风、关于远行的传说，"
        "在夜色里变得格外动人，也把好奇悄悄种进了心里。"
    ),
    (
        "如今虽已长大，每到春天，那些记忆仍会如春风般拂过心头。"
        "那些简单而纯粹的快乐，那些对世界的好奇与探索，都深深地刻在了心里。"
        "春天不仅是一个季节，更是童年最美好的开始。"
        "只要春风还在吹，记忆就会一年年醒来——在桃树下，在田野里，在风筝线上，"
        "在每一口青团的清香里，在我们轻轻踩过的每一个水坑中。"
    ),
)
_FORBIDDEN_WRITE_PATH = "/__mock_loadtest_forbidden__/novel.txt"
# 不在 config builtin allow 规则内（echo/ls/pwd 等），可稳定触发 bash ASK
_PERMISSION_PROBE_BASH = "python3 -c \"print('mock_loadtest_permission_probe')\""
_INTRO_PERMISSION_BASH = (
    "接下来我需要执行一条 shell 命令来确认 Agent 工作区环境。"
    "这一步会触发 bash 权限审批，请允许后继续。\n"
)
_INTRO_PERMISSION_TODO_MODIFY = (
    "开篇已写入文件。接下来我需要更新任务清单状态，"
    "这一步会触发 todo_modify 权限审批，请允许后继续。\n"
)
_INTRO_PLAN = (
    "我看到你希望我专注于创作小说的开头部分。让我重新规划，"
    "专注于创作一个引人入胜的开篇，而不是立即尝试完成整部十万字的小说。\n"
)
_TODO_CONTENTS = (
    "《旅行的意义》开篇",
    "人物详细介绍",
    "故事背景设定",
    "故事冲突与悬念设置",
    "整理并发送开篇文件",
)
_TRAVEL_SCENE_BLOCKS = (
    (
        "1.\n\n雨下得很大。\n\n"
        "陈远站在火车站候车大厅的玻璃窗前，看着雨水在玻璃上划出一道道蜿蜒的痕迹。"
        "窗外的城市在雨幕中变得模糊，霓虹灯的光晕在湿漉漉的地面上晕开，像一幅被水洗过的油画。\n\n"
        "他低头看了看手表：晚上十一点四十七分。距离他辞职已经过去了三十六个小时，"
        "距离火车发车还有十三分钟。背包靠在脚边，里面装着他全部的家当。"
    ),
    (
        "2.\n\n火车驶出城市，进入郊野。雨渐渐小了，窗外是一片漆黑，只有偶尔闪过的零星灯光。\n\n"
        "陈远躺回铺位，闭上眼睛，却睡不着。脑海里反复播放着过去三十六小时的画面："
        "递交辞职信时经理惊讶的表情，母亲电话里带着哭腔的声音，朋友们不解的询问。"
        "也许他真的疯了——放弃年薪五十万的工作，只为了去一个遥远的地方，"
        "寻找一个可能根本不存在的答案。"
    ),
    (
        "3.\n\n凌晨三点，陈远被一阵轻微的啜泣声惊醒。声音来自对面铺位。"
        "苏菲蜷缩在铺位上，肩膀微微颤抖。\"你没事吧？\"陈远轻声问。\n\n"
        "\"只是……想家了。\"苏菲说，\"第一次离家这么远，感觉比出国还远。\""
        "陈远理解这种感觉——一路走来，他做了所有\"正确\"的选择，却离真实的自己越来越远。"
    ),
    (
        "4.\n\n清晨六点，火车停靠在一个小站。窗外是连绵的山峦，笼罩在薄雾中，像一幅水墨画。\n\n"
        "车厢连接处，一位老人正看着窗外的风景。\"您经常去西藏？\"陈远问。"
        "\"每年都去，已经十年了。\"老人说，\"同一个地方，不同的时间，就是不同的世界。\"\n\n"
        "\"您觉得旅行的意义是什么？\"陈远忍不住问。老人沉默了很久："
        "\"也许，意义不在于找到答案，而在于寻找的过程。\""
    ),
    (
        "5.\n\n中午时分，火车停靠在一个较大的车站。远方的天际线上，隐约可以看到雪山的轮廓。\n\n"
        "新乘客林晓背着摄影包上了车。\"旅行作家，兼摄影师。\"她说，"
        "\"不过这次……算是告别之旅吧。\"三个人都沉默了，只是看着窗外的风景。"
        "火车沿着湖岸行驶，阳光洒在湖面上，波光粼粼。\n\n"
        "陈远突然想起一句话：旅行不是为了到达目的地，而是为了学会如何到达。"
        "也许，答案就在路上。也许，问题本身就是答案。也许，旅行就是回家。\n\n"
        "（开篇章节完）"
    ),
)
_CHARACTER_INTRO = (
    "现在让我继续完善人物介绍和背景设定。\n\n"
    "**陈远（35岁）**：前互联网公司技术主管，理性、敏感，因轻度抑郁辞职，"
    "试图在旅途中重新定义\"成功\"与\"幸福\"。\n\n"
    "**苏菲（22岁）**：法学专业休学旅行，聪明而理想主义，在迷茫中寻找独立与热爱。\n\n"
    "**林晓（28岁）**：旅行作家兼摄影师，厌倦\"旁观者\"身份，希望从记录者变成参与者。\n\n"
    "**老张（60岁）**：退休货车司机，为完成对妻子的承诺而每年进藏，将在后续章节登场。\n"
)
_NOVEL_FINAL_MESSAGE = (
    f"完成！我已经为你创作了小说《旅行的意义》的完整开篇部分，"
    f"并保存为 `{_NOVEL_FILENAME}` 发送给你。\n\n"
    "开篇章节约6000字，建立了主要人物、场景氛围与核心主题，"
    "并设置了陈远、苏菲、林晓各自的悬念。如需继续创作后续章节，请告诉我。"
)
_SESSION_MEMORY_SYSTEM_MARKERS = ("session memory updater",)
_SESSION_MEMORY_USER_MARKERS = (
    "Use the edit_file",
    "<current_notes_content>",
    "note-taking instruction",
    "session notes file",
)
_SESSION_MEMORY_CURRENT_STATE_DESC = (
    "_What is actively being worked on right now? "
    "Pending tasks not yet completed. Immediate next steps._"
)
_MOCK_SESSION_MEMORY_CURRENT_STATE = (
    "Mock loadtest session memory snapshot: inherited main-agent conversation "
    "summarized for continuity after compaction."
)
_SESSION_MEMORY_DONE_MESSAGE = "Session memory notes updated."


@dataclass
class _RequestStats:
    active: int = 0
    total: int = 0
    stream_total: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def begin(self, *, stream: bool) -> None:
        async with self.lock:
            self.active += 1
            self.total += 1
            if stream:
                self.stream_total += 1

    async def end(self) -> None:
        async with self.lock:
            self.active = max(0, self.active - 1)

    async def snapshot(self) -> tuple[int, int, int]:
        async with self.lock:
            return self.active, self.total, self.stream_total


async def _read_until(reader: asyncio.StreamReader, marker: bytes, *, limit: int = 1024 * 1024) -> bytes:
    buf = bytearray()
    while marker not in buf:
        chunk = await reader.read(4096)
        if not chunk:
            break
        buf.extend(chunk)
        if len(buf) > limit:
            raise ValueError("HTTP header too large")
    return bytes(buf)


async def _read_chunked_body(reader: asyncio.StreamReader) -> bytes:
    body = bytearray()
    while True:
        size_line = await reader.readline()
        if not size_line:
            break
        size_text = size_line.decode("ascii", errors="replace").strip().split(";", 1)[0]
        if not size_text:
            continue
        size = int(size_text, 16)
        if size == 0:
            await reader.readline()
            break
        body.extend(await reader.readexactly(size))
        await reader.readline()
    return bytes(body)


async def _read_http_request(
    reader: asyncio.StreamReader,
) -> tuple[str, str, dict[str, str], dict[str, Any]]:
    """Read a full HTTP/1.1 request (supports Content-Length and chunked body)."""
    header_blob = await _read_until(reader, b"\r\n\r\n")
    header_text, _, rest = header_blob.partition(b"\r\n\r\n")
    lines = header_text.decode("utf-8", errors="replace").split("\r\n")
    request_line = lines[0] if lines else ""
    parts = request_line.split(" ")
    method = parts[0] if parts else "GET"
    path = parts[1] if len(parts) > 1 else "/"

    headers: dict[str, str] = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip().lower()] = value.strip()

    body = bytearray(rest)
    content_length = headers.get("content-length")
    transfer_encoding = headers.get("transfer-encoding", "").lower()
    if content_length:
        need = int(content_length) - len(body)
        while need > 0:
            chunk = await reader.read(need)
            if not chunk:
                break
            body.extend(chunk)
            need -= len(chunk)
    elif "chunked" in transfer_encoding:
        if body:
            temp_reader = asyncio.StreamReader()
            temp_reader.feed_data(bytes(body))
            temp_reader.feed_eof()
            body = bytearray(await _read_chunked_body(temp_reader))
        else:
            body = bytearray(await _read_chunked_body(reader))

    payload: dict[str, Any] = {}
    body_text = bytes(body).decode("utf-8", errors="replace").strip()
    if body_text:
        try:
            parsed = json.loads(body_text)
            if isinstance(parsed, dict):
                payload = parsed
        except json.JSONDecodeError:
            logger.warning("Failed to parse JSON body (len=%s)", len(body_text))

    return method, path, headers, payload


def _wants_stream(headers: dict[str, str], payload: dict[str, Any]) -> bool:
    if payload.get("stream") is True:
        return True
    accept = headers.get("accept", "")
    return "text/event-stream" in accept.lower()


def _http_response(status: int, body: str, *, content_type: str = "application/json") -> bytes:
    encoded = body.encode("utf-8")
    return (
        f"HTTP/1.1 {status}\r\n"
        f"Content-Type: {content_type}\r\n"
        f"Content-Length: {len(encoded)}\r\n"
        "Connection: close\r\n"
        "\r\n"
    ).encode("utf-8") + encoded


def _sse_event(data: dict[str, Any] | str) -> bytes:
    if isinstance(data, str):
        payload = data
    else:
        payload = json.dumps(data, ensure_ascii=False)
    return f"data: {payload}\n\n".encode("utf-8")


def _models_payload() -> str:
    return json.dumps(
        {
            "object": "list",
            "data": [{"id": "mock-model", "object": "model", "owned_by": "mock"}],
        },
        ensure_ascii=False,
    )


def _message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
                continue
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                parts.append(part["text"])
            elif part.get("type") == "text" and isinstance(part.get("text"), str):
                parts.append(part["text"])
        return " ".join(parts)
    if isinstance(content, dict) and isinstance(content.get("text"), str):
        return content["text"]
    for key in ("query", "input", "text"):
        val = message.get(key)
        if isinstance(val, str):
            return val
    return ""


def _latest_user_message_index(messages: list[Any]) -> int | None:
    for idx in range(len(messages) - 1, -1, -1):
        message = messages[idx]
        if isinstance(message, dict) and message.get("role") == "user":
            return idx
    return None


def _normalize_user_intent_text(text: str) -> str:
    """归一化 user 文本，便于识别 Agent 每轮回写的同一条用户请求。"""
    cleaned = (text or "").strip()
    if not cleaned:
        return ""
    for prefix in ("你收到一条消息：\n", "You receive a new message:\n"):
        if cleaned.startswith(prefix):
            parsed = _parse_user_prompt_payload(cleaned)
            if isinstance(parsed, dict):
                content = parsed.get("content")
                if isinstance(content, str) and content.strip():
                    return content.strip()
    # build_user_prompt 包装：尽量抽出 content 字段再比较
    brace = cleaned.find("{")
    if brace >= 0:
        blob = cleaned[brace:]
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            end = blob.rfind("}")
            parsed = None
            while end > 0:
                try:
                    parsed = json.loads(blob[: end + 1])
                    break
                except json.JSONDecodeError:
                    end = blob.rfind("}", 0, end)
        if isinstance(parsed, dict):
            content = parsed.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip()
    return cleaned


def _user_message_intent(message: dict[str, Any]) -> str:
    return _normalize_user_intent_text(_message_text(message)) or _message_text(message).strip()


def _is_user_reappend(messages: list[Any], idx: int) -> bool:
    """判断 messages[idx] 是否为更早一条同 intent user 的 re-append（Agent 每轮回写）。"""
    if idx < 0 or idx >= len(messages):
        return False
    message = messages[idx]
    if not isinstance(message, dict) or message.get("role") != "user":
        return False
    intent = _user_message_intent(message)
    if not intent:
        return False
    for i in range(idx - 1, -1, -1):
        earlier = messages[i]
        if not isinstance(earlier, dict) or earlier.get("role") != "user":
            continue
        if _is_skill_rail_injection(_message_text(earlier)):
            continue
        earlier_intent = _user_message_intent(earlier)
        if not earlier_intent:
            continue
        if earlier_intent == intent:
            return True
    return False


def _is_auto_attached_context_text(text: str) -> bool:
    """Agent 自动注入的 <system-reminder> / 附件上下文，不是用户新意图。"""
    stripped = (text or "").lstrip()
    if not stripped:
        return False
    return (
        stripped.startswith("<system-reminder>")
        or "The following context is automatically attached" in stripped[:240]
        or "以下上下文会自动附加" in stripped[:80]
    )


def _is_skill_rail_injection(text: str) -> bool:
    """SkillCompliance / SkillUse / 进度提醒等注入，不应作为新用户回合起点。"""
    if _is_auto_attached_context_text(text):
        return True
    markers = (
        "[ACTIVE SKILL BODY]",
        "[Skill ",
        "[skill_step",
        "当前 session 已加载 SKILL.md",
        "This session has loaded SKILL.md",
        "skill_step(action=\"create\"",
        "skill_step(action='create'",
        "立即调用 `skill_complete",
        "Call `skill_complete",
        "Progress reminder",
        "进度提醒",
        "Current todo list",
        "当前待办",
        "task_id:",
        "Next step: Immediately execute",
    )
    return any(marker in text for marker in markers)


def _latest_real_user_message_index(messages: list[Any]) -> int | None:
    for idx in range(len(messages) - 1, -1, -1):
        message = messages[idx]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _message_text(message)
        if _is_skill_rail_injection(text):
            continue
        return idx
    return None


def _latest_user_text(messages: list[Any]) -> str:
    idx = _latest_real_user_message_index(messages)
    if idx is None:
        return ""
    message = messages[idx]
    if not isinstance(message, dict):
        return ""
    return _message_text(message).strip()


def _text_matches_keywords(text: str, keywords: tuple[str, ...], *, case_insensitive: bool = False) -> bool:
    haystack = text.lower() if case_insensitive else text
    for keyword in keywords:
        needle = keyword.lower() if case_insensitive else keyword
        if needle in haystack:
            return True
    return False


def _is_travel_assistant_echo_user(text: str) -> bool:
    """Agent 把 assistant intro 回写为 user 时的噪声，不能作为 travel 锚点。"""
    return any(marker in text for marker in _TRAVEL_ASSISTANT_ECHO_MARKERS)


def _is_travel_completion_echo_user(text: str) -> bool:
    """Agent 把小说收尾 assistant 文案回写为 user 时的噪声。"""
    return any(marker in text for marker in _TRAVEL_COMPLETION_ECHO_MARKERS)


def _is_travel_noise_user(text: str) -> bool:
    return _is_travel_assistant_echo_user(text) or _is_travel_completion_echo_user(text)


def _is_travel_todo_echo_user(text: str) -> bool:
    """todo_modify 完成后 Agent 可能把 travel 任务名回写为 user，不能当作新场景意图。"""
    cleaned = _normalize_user_intent_text(text) or text.strip()
    return cleaned in _TODO_CONTENTS


def _is_skill_echo_user(text: str) -> bool:
    return any(marker in text for marker in _SKILL_ECHO_MARKERS)


def _is_file_echo_user(text: str) -> bool:
    return any(marker in text for marker in _FILE_ECHO_MARKERS)


def _is_cron_echo_user(text: str) -> bool:
    return any(marker in text for marker in _CRON_ECHO_MARKERS)


def _is_cron_wake_message(payload: dict[str, Any]) -> bool:
    """定时任务到点唤醒：user 消息含喝水提醒正文。"""
    for message in reversed(_agent_messages(payload)):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _message_text(message)
        if any(marker in text for marker in _CRON_WAKE_MARKERS):
            return True
    return False


def _is_cron_delivery_request(payload: dict[str, Any]) -> bool:
    """严格判定 cron 子会话到点投递（需 payload 含 cron context 路径）。"""
    if not _is_cron_wake_message(payload):
        return False
    try:
        blob = json.dumps(payload, ensure_ascii=False)
    except TypeError:
        blob = str(payload)
    return bool(_CRON_CONTEXT_PATH_RE.search(blob))


def _is_assistant_echo_user(text: str) -> bool:
    """各场景 assistant 收尾/总结/intro 回写为 user 时的噪声，不参与路由与锚点。"""
    return (
        _is_travel_noise_user(text)
        or _is_skill_echo_user(text)
        or _is_file_echo_user(text)
        or _is_cron_echo_user(text)
    )


def _is_routing_noise_user(messages: list[Any], idx: int) -> bool:
    """场景路由时忽略的 user：rail 注入、re-append、assistant 回写噪声。"""
    if idx < 0 or idx >= len(messages):
        return True
    message = messages[idx]
    if not isinstance(message, dict) or message.get("role") != "user":
        return True
    text = _message_text(message)
    if _is_skill_rail_injection(text) or _is_auto_attached_context_text(text):
        return True
    if _is_user_reappend(messages, idx):
        return True
    if _is_assistant_echo_user(text):
        return True
    if _is_travel_todo_echo_user(text):
        return True
    return False


def _message_has_file_scenario_upload(message: dict[str, Any]) -> bool:
    """当前这条 user 是否带了 file 场景附件（忽略 travel 残留交付物）。"""
    text = _message_text(message)
    if _text_matches_keywords(text, _FILE_KEYWORDS):
        return True
    for item in _files_from_user_message_text(text):
        blob = " ".join(
            str(item.get(key) or "") for key in ("name", "path", "url")
        )
        if _SPRING_ESSAY_SOURCE in blob or _text_matches_keywords(blob, _FILE_KEYWORDS):
            return True
    return False


def _intent_matches_scenario(intent: str, scenario: str, *, messages: list[Any] | None = None) -> bool:
    if _is_assistant_echo_user(intent):
        return False
    if scenario == MockScenario.CRON_DELIVERY:
        return any(marker in intent for marker in _CRON_WAKE_MARKERS)
    if scenario == MockScenario.SCHEDULED_TASK:
        return _text_matches_keywords(intent, _SCHEDULED_TASK_KEYWORDS)
    if scenario == MockScenario.SKILL:
        if _text_matches_keywords(intent, _SKILL_KEYWORDS, case_insensitive=True):
            return True
        haystack = intent.lower()
        return any(kw.lower() in haystack for kw in _SKILL_INTENT_EXTRA)
    if scenario == MockScenario.FILE:
        if _text_matches_keywords(intent, _FILE_KEYWORDS):
            return True
        if messages is not None:
            idx = _find_user_message_index_by_intent(messages, intent)
            if idx is not None:
                return _message_has_file_scenario_upload(messages[idx])
        return False
    if scenario == MockScenario.TRAVEL:
        return _text_matches_keywords(intent, _LOADTEST_NOVEL_MARKERS)
    return False


def _find_user_message_index_by_intent(messages: list[Any], intent: str) -> int | None:
    target = intent.strip()
    if not target:
        return None
    for idx in range(len(messages) - 1, -1, -1):
        message = messages[idx]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _message_text(message)
        if _is_skill_rail_injection(text):
            continue
        normalized = _normalize_user_intent_text(text) or text.strip()
        if normalized == target or text.strip() == target:
            return idx
    return None


def _collect_routable_user_intents(
    messages: list[Any], since_index: int = 0
) -> list[str]:
    """从 messages[since_index:] 按从新到旧收集不重复 user 意图（跳过 routing 噪声）。"""
    intents: list[str] = []
    seen: set[str] = set()
    for idx in range(len(messages) - 1, since_index - 1, -1):
        if _is_routing_noise_user(messages, idx):
            continue
        intent = _user_message_intent(messages[idx])
        if not intent or intent in seen:
            continue
        seen.add(intent)
        intents.append(intent)
    return intents


def _collect_user_intents_since(messages: list[Any], since_index: int) -> list[str]:
    return _collect_routable_user_intents(messages, since_index)


_SCENARIO_PRIORITY = (
    MockScenario.CRON_DELIVERY,
    MockScenario.SCHEDULED_TASK,
    MockScenario.SKILL,
    MockScenario.FILE,
    MockScenario.TRAVEL,
)


def _route_intents_to_scenario(
    intents: list[str], messages: list[Any]
) -> str:
    """按场景优先级匹配一组 user 意图（skill 优先于 travel）。"""
    for scenario in _SCENARIO_PRIORITY:
        for intent in intents:
            if _intent_matches_scenario(intent, scenario, messages=messages):
                return scenario
    return MockScenario.TRAVEL


def _detect_loadtest_scenario(payload: dict[str, Any]) -> str:
    """收集全部有效 user 意图后按优先级路由（避免末尾 travel re-append 覆盖 skill）。"""
    if _is_cron_wake_message(payload):
        return MockScenario.CRON_DELIVERY
    messages = _agent_messages(payload)
    intents = _collect_routable_user_intents(messages)
    if not intents:
        return MockScenario.TRAVEL
    return _route_intents_to_scenario(intents, messages)


def _next_loadtest_scenario_in_sequence(completed: str) -> str | None:
    try:
        idx = _LOADTEST_SCENARIO_SEQUENCE.index(completed)
    except ValueError:
        return None
    if idx + 1 >= len(_LOADTEST_SCENARIO_SEQUENCE):
        return None
    return _LOADTEST_SCENARIO_SEQUENCE[idx + 1]


def _prior_scenarios_in_sequence(completed: str) -> frozenset[str]:
    """已完成场景及其在 loadtest 顺序中的前序场景（不应再被 re-route 命中）。"""
    try:
        idx = _LOADTEST_SCENARIO_SEQUENCE.index(completed)
    except ValueError:
        return frozenset({completed})
    return frozenset(_LOADTEST_SCENARIO_SEQUENCE[: idx + 1])


def _intent_matches_prior_scenarios(
    intent: str, completed: str, *, messages: list[Any] | None = None
) -> bool:
    for scenario in _prior_scenarios_in_sequence(completed):
        if _intent_matches_scenario(intent, scenario, messages=messages):
            return True
    return False


def _has_post_done_message_activity(
    payload: dict[str, Any], state: "_LoadtestSessionState"
) -> bool:
    """done 后 messages 变长/变短，说明 Agent 开始了新一轮 LLM 回合（新 user 可能尚未写入 messages）。"""
    msg_len = len(_agent_messages(payload))
    at_done = state.msg_len_at_done
    return msg_len != at_done


def _scan_new_scenario_after_done(
    payload: dict[str, Any],
    state: "_LoadtestSessionState",
    *,
    since: int,
) -> str | None:
    """从 since 起找最新的、不属于已完成场景的 user 意图。"""
    messages = _agent_messages(payload)
    completed = state.scenario
    for idx in range(len(messages) - 1, since - 1, -1):
        if _is_routing_noise_user(messages, idx):
            continue
        intent = _user_message_intent(messages[idx])
        if not intent:
            continue
        if _intent_matches_prior_scenarios(intent, completed, messages=messages):
            continue
        for scenario in _SCENARIO_PRIORITY:
            if scenario in _prior_scenarios_in_sequence(completed):
                continue
            if _intent_matches_scenario(intent, scenario, messages=messages):
                return scenario
    return None


def _scan_payload_for_scenario_after_done(
    payload: dict[str, Any], state: "_LoadtestSessionState"
) -> str | None:
    """从 done 后的 user 消息原文嗅探下一场景（新 user / 附件可能尚未被 intent 解析）。

    注意：不对整包 payload 做 cron 关键词匹配——tools/system 里常有 cron_create_job 等字样。
    """
    prior = _prior_scenarios_in_sequence(state.scenario)
    messages = _agent_messages(payload)
    since = state.msg_len_at_done
    if since > len(messages):
        since = 0

    user_blobs: list[str] = []
    for idx in range(since, len(messages)):
        message = messages[idx]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        user_blobs.append(_message_text(message))

    if not user_blobs:
        return None

    combined = "\n".join(user_blobs)

    for scenario in _SCENARIO_PRIORITY:
        if scenario in prior:
            continue
        if scenario == MockScenario.SKILL:
            if _text_matches_keywords(combined, _SKILL_KEYWORDS, case_insensitive=True):
                return scenario
            haystack = combined.lower()
            if any(kw.lower() in haystack for kw in _SKILL_INTENT_EXTRA):
                return scenario
        elif scenario == MockScenario.FILE:
            if "童趣的春天" in combined:
                return scenario
            if "files_updated_by_user" in combined and "扩写" in combined:
                return scenario
        # SCHEDULED_TASK 仅通过显式 user 意图匹配，不做 payload 嗅探
    return None


def _detect_loadtest_scenario_after_done(
    payload: dict[str, Any], state: "_LoadtestSessionState"
) -> str | None:
    """任务已完成后检测下一场景。

    1. 扫描 done 之后的新 user 意图（跳过已完成及前序场景 intent）
    2. dedup 导致 messages 变短时 since 回退为 0，但仍跳过前序场景 intent
    3. done 后 user 消息嗅探（附件 / skill 文本可能尚未被 intent 解析）
    4. 仍无匹配但 messages 已变化 → 按 loadtest 固定顺序切到下一场景
       （Agent 收到 chat.send 后，首轮 LLM 可能只有上一场景收尾 echo）
    """
    messages = _agent_messages(payload)
    since = state.msg_len_at_done
    if since > len(messages):
        since = 0

    found = _scan_new_scenario_after_done(payload, state, since=since)
    if found is not None:
        return found

    found = _scan_payload_for_scenario_after_done(payload, state)
    if found is not None:
        return found

    if _has_post_done_message_activity(payload, state):
        return _next_loadtest_scenario_in_sequence(state.scenario)

    return None


def _payload_tool_names(payload: dict[str, Any]) -> set[str]:
    tools = payload.get("tools")
    if not isinstance(tools, list):
        return set()
    names: set[str] = set()
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        fn = tool.get("function")
        if isinstance(fn, dict) and isinstance(fn.get("name"), str):
            names.add(fn["name"])
    return names


def _is_session_memory_request(payload: dict[str, Any]) -> bool:
    """Session Memory 后台 ReActAgent：仅注册 edit_file，prompt 含 notes 提取指令。"""
    tool_names = _payload_tool_names(payload)
    if tool_names and tool_names <= {"edit_file"}:
        return True

    messages = payload.get("messages")
    if not isinstance(messages, list):
        return False

    blob_parts: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") in {"system", "user"}:
            blob_parts.append(_message_text(message))
    blob = "\n".join(blob_parts)
    if any(marker in blob for marker in _SESSION_MEMORY_SYSTEM_MARKERS):
        return True
    if "Use the edit_file" in blob and "<current_notes_content>" in blob:
        return True
    return any(marker in blob for marker in _SESSION_MEMORY_USER_MARKERS[2:])


_SESSION_KEY_RE = re.compile(r"sess_[a-z0-9_]+")

_SCENARIO_FINAL_STAGE: dict[str, int] = {
    MockScenario.TRAVEL: 11,
    MockScenario.SCHEDULED_TASK: 1,
    MockScenario.SKILL: 5,
    MockScenario.FILE: 4,
}


@dataclass
class _LoadtestSessionState:
    scenario: str
    stage: int = 0
    done: bool = False
    msg_len_at_done: int = 0


_loadtest_states: dict[str, _LoadtestSessionState] = {}


def _reroute_since_index(state: _LoadtestSessionState, messages: list[Any]) -> int:
    """done 后扫描新意图的起始下标；dedup 导致 messages 变短时回退到 0。"""
    since = state.msg_len_at_done
    if since > len(messages):
        return 0
    return since


def _agent_messages(payload: dict[str, Any]) -> list[Any]:
    messages = payload.get("messages")
    return messages if isinstance(messages, list) else []


def _extract_loadtest_session_key(payload: dict[str, Any]) -> str | None:
    """优先从完整 payload 提取 sess_*，避免早期请求只有 intent 哈希、后期才有路径导致状态丢失。"""
    try:
        blob = json.dumps(payload, ensure_ascii=False)
    except TypeError:
        blob = str(payload)
    match = _SESSION_KEY_RE.search(blob)
    if match:
        return match.group(0)
    for message in reversed(_agent_messages(payload)):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _user_message_intent(message)
        if text and not _is_assistant_echo_user(text):
            return f"intent:{abs(hash(text))}"
    return None


def _is_main_agent_loadtest_request(payload: dict[str, Any]) -> bool:
    """排除 Session Memory 子 Agent、以及消息过短的并行探测请求。"""
    if _is_session_memory_request(payload):
        return False
    if _is_cron_wake_message(payload):
        return True
    messages = _agent_messages(payload)
    tools = payload.get("tools")
    if isinstance(tools, list) and len(tools) <= 1:
        return False
    key = _extract_loadtest_session_key(payload)
    if not key:
        return False
    state = _loadtest_states.get(key)
    if state is not None and state.done:
        return True
    if state is None:
        return len(messages) >= 2
    return len(messages) >= 3


def _prepare_loadtest_state(
    payload: dict[str, Any], scenario: str
) -> tuple[str | None, int, str]:
    """主 Agent 请求：按 session 返回 (key, stage, effective_scenario)。

    - 无状态 → 新建 stage=0
    - done=False → 继续当前任务
    - done=True 且无新 user 意图 → 保持收尾 stage（Agent 同轮尾请求）
    - done=True 且有新 user 意图 → 按优先级重新路由，stage=0
    """
    if not _is_main_agent_loadtest_request(payload):
        return None, 0, scenario
    key = _extract_loadtest_session_key(payload)
    if not key:
        return None, 0, scenario

    state = _loadtest_states.get(key)

    if state is not None and not state.done:
        return key, state.stage, state.scenario

    if state is not None and state.done:
        next_scenario = _detect_loadtest_scenario_after_done(payload, state)
        msg_count = len(_agent_messages(payload))
        if next_scenario is None:
            logger.info(
                "loadtest keep done scenario=%s stage=%d msg_len=%d at_done=%d",
                state.scenario,
                state.stage,
                msg_count,
                state.msg_len_at_done,
            )
            return key, state.stage, state.scenario
        route_reason = "sequence"
        messages = _agent_messages(payload)
        since = _reroute_since_index(state, messages)
        if _scan_new_scenario_after_done(payload, state, since=since) == next_scenario:
            route_reason = "intent"
        elif _scan_payload_for_scenario_after_done(payload, state) == next_scenario:
            route_reason = "payload"
        new_intents = _collect_user_intents_since(messages, since)
        logger.info(
            "loadtest re-route after done (%s): %s -> %s msg_len=%d at_done=%d new_intents=%r",
            route_reason,
            state.scenario,
            next_scenario,
            msg_count,
            state.msg_len_at_done,
            [i[:80] for i in new_intents],
        )
        scenario = next_scenario

    _loadtest_states[key] = _LoadtestSessionState(scenario=scenario, stage=0)
    return key, 0, scenario


def _advance_loadtest_state(
    key: str | None, scenario: str, stage_used: int, payload: dict[str, Any]
) -> None:
    if not key:
        return
    state = _loadtest_states.get(key)
    if state is None or state.scenario != scenario:
        return
    final = _SCENARIO_FINAL_STAGE.get(scenario, 11)
    if stage_used >= final:
        state.stage = final
        state.done = True
        state.msg_len_at_done = len(_agent_messages(payload))
    else:
        state.stage = stage_used + 1


_TOOL_FAILURE_MARKERS = (
    "Ability execution error",
    "Ability not found",
    "Skill not found",
    "SkillNet 安装失败",
    "API rate limit exceeded",
    "GitHub API Error",
    "success=False",
    '"ok": false',
    '"ok": False',
    "'ok': False",
)


def _tool_result_failure_reason(text: str) -> str | None:
    """从单条 tool 返回文本判断是否失败。"""
    blob = (text or "").strip()
    if not blob:
        return None
    lowered = blob.lower()
    for marker in _TOOL_FAILURE_MARKERS:
        if marker.lower() in lowered:
            return marker
    if blob.startswith("{"):
        try:
            parsed = json.loads(blob)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            if parsed.get("ok") is False:
                detail = parsed.get("detail") or parsed.get("error") or "ok=false"
                return str(detail)[:200]
            if parsed.get("success") is False:
                detail = parsed.get("error") or parsed.get("detail") or "success=false"
                return str(detail)[:200]
    return None


def _latest_tool_failure(messages: list[Any]) -> str | None:
    """最近一轮 assistant tool_calls 对应的 tool 结果若失败，返回简短原因。"""
    last_assistant_idx = None
    for idx in range(len(messages) - 1, -1, -1):
        message = messages[idx]
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            last_assistant_idx = idx
            break
    if last_assistant_idx is None:
        return None
    tool_names: list[str] = []
    for call in messages[last_assistant_idx].get("tool_calls") or []:
        if not isinstance(call, dict):
            continue
        fn = call.get("function")
        if isinstance(fn, dict) and fn.get("name"):
            tool_names.append(str(fn["name"]))
    default_name = tool_names[0] if tool_names else "tool"
    for message in messages[last_assistant_idx + 1:]:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        reason = _tool_result_failure_reason(_tool_message_text(message))
        if reason:
            return f"{default_name}: {reason}"
    return None


def _mark_loadtest_scenario_done(key: str | None, payload: dict[str, Any]) -> None:
    """工具失败后结束当前场景，避免继续点后续工具。"""
    if not key:
        return
    state = _loadtest_states.get(key)
    if state is None:
        return
    state.done = True
    state.msg_len_at_done = len(_agent_messages(payload))


def _fail_fast_plan(scenario: str, stage: int, reason: str) -> _AgentPlan:
    return _AgentPlan(
        kind="stream_text",
        text=(
            f"[mock-fail-fast] scenario={scenario} stage={stage} "
            f"上一轮工具失败，停止继续调用后续工具。reason={reason}"
        ),
    )


def _parse_session_memory_notes_path(messages: list[Any]) -> str | None:
    path_patterns = (
        re.compile(r"file_path:\s*(\S+\.md)"),
        re.compile(r"(/\S+session_context(?:\.pending)?\.md)"),
    )
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _message_text(message)
        for pattern in path_patterns:
            match = pattern.search(text)
            if match:
                return match.group(1)
    return None


def _parse_current_notes_content(messages: list[Any]) -> str:
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        text = _message_text(message)
        start = text.find("<current_notes_content>")
        end = text.find("</current_notes_content>")
        if start >= 0 and end > start:
            return text[start + len("<current_notes_content>"):end].strip()
    return ""


def _session_memory_edit_already_done(messages: list[Any]) -> bool:
    seen_session_memory_prompt = False
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "user":
            text = _message_text(message)
            if "Use the edit_file" in text and "<current_notes_content>" in text:
                seen_session_memory_prompt = True
        if not seen_session_memory_prompt:
            continue
        if message.get("role") != "tool":
            continue
        if message.get("name") == "edit_file":
            return True
        if "edit_file" in _tool_message_text(message):
            return True
    return False


def _build_session_memory_edit_args(messages: list[Any]) -> dict[str, Any]:
    notes_path = _parse_session_memory_notes_path(messages) or "session_context.pending.md"
    current_notes = _parse_current_notes_content(messages)
    desc = _SESSION_MEMORY_CURRENT_STATE_DESC
    body = _MOCK_SESSION_MEMORY_CURRENT_STATE

    old_string = ""
    new_string = ""
    if f"# Current State\n{desc}" in current_notes:
        old_string = f"# Current State\n{desc}"
        new_string = f"# Current State\n{body}"
    elif "# Current State\n" in current_notes:
        match = re.search(r"(# Current State\n)(.*?)(?=\n# |\Z)", current_notes, re.S)
        if match:
            old_string = match.group(1) + match.group(2).rstrip("\n")
            new_string = f"# Current State\n{body}"
        else:
            old_string = desc
            new_string = body
    elif desc in current_notes:
        old_string = desc
        new_string = body
    else:
        old_string = ""
        new_string = (
            "# Session Title\nMock loadtest session\n\n"
            f"# Current State\n{body}\n"
        )

    return {
        "file_path": notes_path,
        "old_string": old_string,
        "new_string": new_string,
    }


def _plan_session_memory_response(payload: dict[str, Any]) -> _AgentPlan:
    messages = payload.get("messages")
    if not isinstance(messages, list):
        messages = []
    if _session_memory_edit_already_done(messages):
        return _AgentPlan(kind="stream_text", text=_SESSION_MEMORY_DONE_MESSAGE)
    return _AgentPlan(
        kind="intro_and_tool_call",
        text="Updating session memory notes.\n",
        tool_name="edit_file",
        tool_args=_build_session_memory_edit_args(messages),
    )


def _should_use_novel_scenario(profile: str, payload: dict[str, Any]) -> bool:
    """loadtest 仅处理主 Agent 多工具链与 Session Memory；其余走通用 mock。"""
    if profile != "loadtest":
        return False
    if _is_session_memory_request(payload):
        return True
    if _is_cron_wake_message(payload):
        return True
    return _is_main_agent_loadtest_request(payload)


# travel: stage 0 todo_create → 1 stream → 2 bash → … → 11 收尾
_TRAVEL_FINAL_STAGE = 11


def _build_travel_opening_text(target_chars: int) -> str:
    parts = ["《旅行的意义》开篇\n\n第一章：雨夜的陌生人\n\n"]
    block_idx = 0
    while len("".join(parts)) < target_chars:
        parts.append(_TRAVEL_SCENE_BLOCKS[block_idx % len(_TRAVEL_SCENE_BLOCKS)])
        parts.append("\n\n")
        block_idx += 1
    text = "".join(parts)
    if len(text) > target_chars:
        text = text[:target_chars]
    return text


def _build_travel_novel_file_text(target_chars: int) -> str:
    header = (
        "《旅行的意义》\n"
        "作者：Mock Agent\n"
        "说明：Enterprise Runtime loadtest 自动生成的开篇章节。\n\n"
    )
    body = _build_travel_opening_text(max(500, target_chars - len(header)))
    text = header + body
    if len(text) > target_chars:
        text = text[:target_chars]
    return text


# content 截到行尾；先把 tool 结果里的字面 \\n 还原成换行，避免 JSON 转义导致整段被吞掉
_TODO_ID_RE = re.compile(r"task_id:\s*(\S+)\s*,\s*content:\s*([^\n]+)")
_FILE_PATH_KV_RE = re.compile(r"""['"]file_path['"]\s*:\s*['"]([^'"]+)['"]""")
_ABS_PATH_RE = re.compile(r"([A-Za-z]:\\[^\s\"']+|/[^\s\"']+" + re.escape(_NOVEL_FILENAME) + r")")


def _tool_message_text(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
        return "\n".join(parts)
    return str(content or "")


def _looks_like_tool_result_json(text: str) -> bool:
    """粗判 tool 返回是否为可再展开的 JSON 文本。"""
    if not text.startswith("{"):
        return False
    markers = ("task_id:", "Successfully", "message")
    return any(marker in text for marker in markers)


def _unwrap_tool_result_text(blob: str) -> str:
    """把 tool 返回的 JSON（如 {"message": "..."}）展开成可读文本，并还原转义换行。"""
    text = blob.strip()
    if _looks_like_tool_result_json(text):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            for key in ("message", "result", "content", "output"):
                value = parsed.get(key)
                if isinstance(value, str) and value.strip():
                    text = value
                    break
            else:
                text = json.dumps(parsed, ensure_ascii=False)
        elif isinstance(parsed, str):
            text = parsed
    # JSON dump 后再当普通字符串传来时，换行仍是字面 \\n
    if text.count("\n") < 2 and "\\n" in text:
        text = text.replace("\\n", "\n")
    return text


def _is_absolute_path(path: str) -> bool:
    if path.startswith("/"):
        return True
    return len(path) > 2 and path[1] == ":" and path[0].isalpha()


def _workspace_relname(value: str, fallback: str) -> str:
    """只保留工作区文件名，丢掉宿主机 / 沙箱绝对目录。"""
    text = (value or "").strip()
    if not text:
        return fallback
    name = Path(text.split("?", 1)[0]).name
    if "%" in name:
        name = unquote(name)
    return name or fallback


def _is_http_url(value: str) -> bool:
    text = (value or "").strip()
    return text.startswith("http://") or text.startswith("https://")


def _is_host_fixture_path(path: str) -> bool:
    """压测机仓库路径，沙箱里既读不到也写不进去。"""
    normalized = (path or "").replace("\\", "/").strip()
    if not normalized:
        return False
    return (
        "/claw_manager/scripts/" in normalized
        or normalized.startswith("/root/mz/")
        or "/root/mz/" in normalized
    )


def _is_sandbox_usable_path(path: str) -> bool:
    """可从沙箱内 cp 的绝对路径；宿主机脚本目录不算。"""
    text = (path or "").strip()
    if not text or not _is_absolute_path(text) or _is_http_url(text):
        return False
    return not _is_host_fixture_path(text)


def _parse_file_path_from_tool_blob(blob: str, filename: str) -> str | None:
    if filename not in blob:
        return None
    for match in _FILE_PATH_KV_RE.finditer(blob):
        path = match.group(1).strip()
        if filename in path:
            return path
    pattern = re.compile(r"([A-Za-z]:\\[^\s\"']+|/[^\s\"']+" + re.escape(filename) + r")")
    abs_match = pattern.search(blob)
    if abs_match:
        return abs_match.group(1)
    return None


def _parse_abs_path_from_tool_blob(blob: str) -> str | None:
    return _parse_file_path_from_tool_blob(blob, _NOVEL_FILENAME)


def _last_todo_create_tool_call_id(messages: list[Any]) -> str | None:
    """返回 messages 里最近一次 todo_create 的 tool_call id。"""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call in reversed(tool_calls):
            if not isinstance(call, dict):
                continue
            fn = call.get("function")
            if not isinstance(fn, dict) or fn.get("name") != "todo_create":
                continue
            call_id = call.get("id")
            if isinstance(call_id, str) and call_id:
                return call_id
    return None


def _parse_todo_items_from_tool_content(content: str) -> list[tuple[str, str]]:
    text = _unwrap_tool_result_text(content)
    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    if "task_id:" not in text:
        return items
    for match in _TODO_ID_RE.finditer(text):
        todo_id = match.group(1).rstrip(",").strip()
        todo_content = match.group(2).strip().rstrip('"').rstrip("'")
        if not todo_id or todo_id in seen:
            continue
        seen.add(todo_id)
        items.append((todo_id, todo_content))
    return items


def _parse_todo_items(messages: list[Any]) -> list[tuple[str, str]]:
    """从 tool 消息解析 todo；优先使用最近一次 todo_create 的结果（场景切换后避免 travel id）。"""
    create_call_id = _last_todo_create_tool_call_id(messages)
    if create_call_id:
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "tool":
                continue
            if message.get("tool_call_id") != create_call_id:
                continue
            items = _parse_todo_items_from_tool_content(_tool_message_text(message))
            if items:
                return items
            break

    items: list[tuple[str, str]] = []
    seen: set[str] = set()
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        for todo_id, todo_content in _parse_todo_items_from_tool_content(
            _tool_message_text(message)
        ):
            if todo_id in seen:
                continue
            seen.add(todo_id)
            items.append((todo_id, todo_content))
    return items


def _assistant_tool_call_args(messages: list[Any], tool_name: str) -> list[dict[str, Any]]:
    found: list[dict[str, Any]] = []
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        tool_calls = message.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for call in tool_calls:
            if not isinstance(call, dict):
                continue
            fn = call.get("function")
            if not isinstance(fn, dict) or fn.get("name") != tool_name:
                continue
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else dict(raw_args)
            except json.JSONDecodeError:
                continue
            if isinstance(args, dict):
                found.append(args)
    return found


def _is_valid_write_file_path(path: Any) -> bool:
    if not isinstance(path, str) or not path:
        return False
    if _FORBIDDEN_WRITE_PATH in path:
        return False
    return _is_absolute_path(path)


def _resolve_novel_file_path(messages: list[Any]) -> str:
    """从 write_file / read_file 的 tool 结果中提取绝对路径，供 send_file_to_user 使用。"""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        path = _parse_abs_path_from_tool_blob(_tool_message_text(message))
        if path:
            return path
    for args in reversed(_assistant_tool_call_args(messages, "write_file")):
        path = args.get("file_path") or args.get("path")
        if _is_valid_write_file_path(path):
            return path
    for args in reversed(_assistant_tool_call_args(messages, "read_file")):
        path = args.get("file_path") or args.get("path")
        if isinstance(path, str) and path and _is_absolute_path(path):
            return path
    return _NOVEL_FILENAME


def _shell_single_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def _coerce_upload_file_entries(raw: Any) -> list[dict[str, str]]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []

    entries: list[Any] = []
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        if any(raw.get(k) for k in ("url", "uri", "path", "name", "filename")):
            entries = [raw]
        else:
            for key, value in raw.items():
                if isinstance(value, dict):
                    item = dict(value)
                    item.setdefault("name", str(key))
                    entries.append(item)
                elif isinstance(value, str) and value.strip():
                    text = value.strip()
                    if text.startswith("http://") or text.startswith("https://"):
                        entries.append({"name": str(key), "url": text, "path": ""})
                    else:
                        entries.append({"name": str(key), "url": "", "path": text})
    else:
        return []

    result: list[dict[str, str]] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or item.get("filename") or "").strip()
        url = str(item.get("url") or item.get("uri") or "").strip()
        path = str(item.get("path") or "").strip()
        if not name:
            if path:
                name = Path(path).name
            elif url:
                name = Path(url.split("?", 1)[0]).name
        if not name and not url and not path:
            continue
        if not name:
            name = _SPRING_ESSAY_SOURCE
        result.append({"name": name, "url": url, "path": path})
    return result


def _parse_user_prompt_payload(text: str) -> dict[str, Any] | None:
    """解析 build_user_prompt 包装后的 JSON（可能前缀中文说明）。"""
    brace = text.find("{")
    if brace < 0:
        return None
    blob = text[brace:]
    try:
        parsed = json.loads(blob)
    except json.JSONDecodeError:
        # 容错：从后往前截断到最后一个 }
        end = blob.rfind("}")
        while end > 0:
            try:
                parsed = json.loads(blob[: end + 1])
                break
            except json.JSONDecodeError:
                end = blob.rfind("}", 0, end)
        else:
            return None
    return parsed if isinstance(parsed, dict) else None


def _files_from_user_message_text(text: str) -> list[dict[str, str]]:
    """从单条 user 消息文本解析附件元数据。"""
    if not text:
        return []

    payload = _parse_user_prompt_payload(text)
    if isinstance(payload, dict):
        for key in ("files_updated_by_user", "files"):
            files = _coerce_upload_file_entries(payload.get(key))
            if files:
                return files

    urls = _HTTP_URL_RE.findall(text)
    if urls:
        preferred = [
            u for u in urls
            if _SPRING_ESSAY_SOURCE in u or "sandbox_path" in u or u.lower().endswith(".md")
        ]
        chosen = preferred[0] if preferred else urls[0]
        name = Path(chosen.split("?", 1)[0]).name or _SPRING_ESSAY_SOURCE
        if "%" in name:
            name = unquote(name)
        return [{"name": name, "url": chosen, "path": ""}]

    return []


def _extract_uploaded_files(messages: list[Any]) -> list[dict[str, str]]:
    """从 user 消息提取上传附件（扫描全部 user，不仅最新一条 re-append）。"""
    for idx in range(len(messages) - 1, -1, -1):
        message = messages[idx]
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _message_text(message)
        if _is_skill_rail_injection(text):
            continue
        files = _files_from_user_message_text(text)
        if files:
            return files
    return []


def _pick_upload_file(messages: list[Any]) -> dict[str, str]:
    files = _extract_uploaded_files(messages)
    for item in files:
        if _SPRING_ESSAY_SOURCE in item.get("name", "") or _SPRING_ESSAY_SOURCE in item.get("path", ""):
            return item
    if files:
        return files[0]
    return {"name": _SPRING_ESSAY_SOURCE, "url": "", "path": ""}


def _build_heredoc_write_bash(filename: str, content: str) -> str:
    """在工作区写入 Mock 原文（无 url/path 时兜底，确保 read_file 有文件可读）。

    heredoc 结束行后不能接 &&（bash -c 会报 syntax error），改用换行分隔命令。
    """
    delimiter = "MOCK_ESSAY_EOF"
    while delimiter in content:
        delimiter += "_"
    return (
        f"cat > {_shell_single_quote(filename)} << '{delimiter}'\n"
        f"{content}\n"
        f"{delimiter}\n"
        f"ls -la {_shell_single_quote(filename)} "
        f"&& wc -c {_shell_single_quote(filename)}"
    )


def _build_file_download_bash(upload: dict[str, str]) -> str:
    """把原文落到工作区：有 URL 就 curl，沙箱内路径才 cp，否则 heredoc。"""
    name = _workspace_relname(
        upload.get("name") or upload.get("path") or "",
        _SPRING_ESSAY_SOURCE,
    )
    url = (upload.get("url") or "").strip()
    path = (upload.get("path") or "").strip()
    if _is_http_url(url):
        return (
            f"curl -fsSL -o {_shell_single_quote(name)} {_shell_single_quote(url)} "
            f"&& ls -la {_shell_single_quote(name)} "
            f"&& wc -c {_shell_single_quote(name)}"
        )
    if _is_sandbox_usable_path(path):
        return (
            f"cp {_shell_single_quote(path)} {_shell_single_quote(name)} "
            f"&& ls -la {_shell_single_quote(name)} "
            f"&& wc -c {_shell_single_quote(name)}"
        )
    return _build_heredoc_write_bash(name, _load_spring_essay_source())


def _resolve_spring_local_read_path(messages: list[Any], upload: dict[str, str]) -> str:
    """read_file 只用工作区相对文件名，不用宿主机脚本路径。"""
    resolved = _resolve_spring_source_path(messages)
    if resolved and not _is_host_fixture_path(resolved) and not _is_absolute_path(resolved):
        return _workspace_relname(resolved, _SPRING_ESSAY_SOURCE)
    return _workspace_relname(
        upload.get("name") or upload.get("path") or "",
        _SPRING_ESSAY_SOURCE,
    )


def _load_spring_essay_source() -> str:
    try:
        return _SPRING_ESSAY_SOURCE_PATH.read_text(encoding="utf-8")
    except OSError:
        return (
            "# 童趣的春天\n\n"
            "春天，是童年记忆中最温柔的序曲。每当春风拂过脸颊，"
            "我便仿佛又回到了那个充满好奇与惊喜的童年时光。"
        )


def _build_spring_essay_expanded(*, target_chars: int = _SPRING_ESSAY_TARGET_CHARS) -> str:
    source = _load_spring_essay_source()
    body = source.split("---", 1)[0].strip()
    parts = [body]
    section_idx = 0
    while len("".join(parts)) < target_chars:
        parts.append(_SPRING_EXPANSION_SECTIONS[section_idx % len(_SPRING_EXPANSION_SECTIONS)])
        section_idx += 1
    text = "\n\n".join(parts)
    footer = (
        f"\n\n---\n**字数统计**：约{target_chars}字\n"
        f"**主题**：童趣的春天（扩写版）\n"
        "**说明**：Mock Agent 自动扩写生成"
    )
    return text + footer


def _resolve_spring_source_path(messages: list[Any]) -> str:
    for message in reversed(messages):
        if not isinstance(message, dict):
            continue
        text = _tool_message_text(message) if message.get("role") == "tool" else _message_text(message)
        path = _parse_file_path_from_tool_blob(text, _SPRING_ESSAY_SOURCE)
        if path and not _is_host_fixture_path(path):
            return _workspace_relname(path, _SPRING_ESSAY_SOURCE)
    for args in reversed(_assistant_tool_call_args(messages, "read_file")):
        path = args.get("file_path") or args.get("path")
        if isinstance(path, str) and path and _SPRING_ESSAY_SOURCE in path:
            if not _is_host_fixture_path(path):
                return _workspace_relname(path, _SPRING_ESSAY_SOURCE)
    return _SPRING_ESSAY_SOURCE


def _resolve_spring_output_path(messages: list[Any]) -> str:
    """write_file 只用工作区相对文件名，避免写到宿主机脚本目录。"""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        path = _parse_file_path_from_tool_blob(_tool_message_text(message), _SPRING_ESSAY_OUTPUT)
        if path and not _is_host_fixture_path(path) and not _is_absolute_path(path):
            return _workspace_relname(path, _SPRING_ESSAY_OUTPUT)
    return _SPRING_ESSAY_OUTPUT


def _resolve_spring_send_path(messages: list[Any]) -> str:
    """send_file_to_user 必须用 write_file 返回的工作区绝对路径。

    相对文件名会按 AgentServer 进程 cwd 查找，找不到已写入的扩写版。
    """
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        path = _parse_file_path_from_tool_blob(_tool_message_text(message), _SPRING_ESSAY_OUTPUT)
        if path and _is_sandbox_usable_path(path):
            return path
    for args in reversed(_assistant_tool_call_args(messages, "write_file")):
        path = args.get("file_path") or args.get("path")
        if isinstance(path, str) and _SPRING_ESSAY_OUTPUT in path and _is_sandbox_usable_path(path):
            return path
    return _SPRING_ESSAY_OUTPUT


def _todo_modify_complete_args(
    messages: list[Any],
    task_index: int,
    *,
    fallback_contents: tuple[str, ...] | None = None,
) -> dict[str, Any]:
    """只更新 status=completed，不改写 content，避免错误解析污染任务文案。"""
    todos = _parse_todo_items(messages)
    todo_id: str | None = None
    if fallback_contents and 0 <= task_index < len(fallback_contents):
        want = fallback_contents[task_index]
        for tid, content in todos:
            if content == want or content.startswith(want) or want in content:
                todo_id = tid
                break
    if todo_id is None and 0 <= task_index < len(todos):
        todo_id = todos[task_index][0]
    elif todo_id is None and todos:
        todo_id = todos[min(task_index, len(todos) - 1)][0]
    if not todo_id:
        todo_id = "mock-todo-1"
    return {
        "action": "update",
        "todos": [
            {
                "id": todo_id,
                "status": "completed",
            }
        ],
    }


@dataclass
class _AgentPlan:
    kind: str
    text: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None


def _todo_create_tasks(contents: tuple[str, ...]) -> list[dict[str, str]]:
    """Build todo_create ``tasks`` for current agent-core.

    Runtime requires a JSON array of objects with ``content``; a semicolon-
    separated string is rejected with 182504.
    """
    return [{"content": text, "activeForm": f"正在执行：{text}"} for text in contents]


def _plan_travel_flow_response(
    payload: dict[str, Any],
    *,
    stage: int,
    novel_chars: int,
    excerpt_chars: int,
) -> _AgentPlan:
    """小说《旅行的意义》场景：todo → stream → bash → write → read → todo×5 → send → 收尾。"""
    messages = _agent_messages(payload)
    opening = _build_travel_opening_text(min(excerpt_chars, novel_chars))
    file_body = _build_travel_novel_file_text(min(novel_chars, LOADTEST_WRITE_MAX_CHARS))

    if stage == 0:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text=_INTRO_PLAN,
            tool_name="todo_create",
            tool_args={"tasks": _todo_create_tasks(_TODO_CONTENTS)},
        )
    if stage == 1:
        return _AgentPlan(kind="stream_text", text=opening)
    if stage == 2:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text=_INTRO_PERMISSION_BASH,
            tool_name="bash",
            tool_args={"command": _PERMISSION_PROBE_BASH},
        )
    if stage == 3:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text="权限已确认。现在把开篇写入工作区文件：\n",
            tool_name="write_file",
            tool_args={"file_path": _NOVEL_FILENAME, "content": file_body},
        )
    if stage == 4:
        path = _resolve_novel_file_path(messages)
        return _AgentPlan(
            kind="intro_and_tool_call",
            text="让我检查一下当前文件的内容：\n",
            tool_name="read_file",
            tool_args={"file_path": path},
        )
    if stage == 5:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text=_INTRO_PERMISSION_TODO_MODIFY,
            tool_name="todo_modify",
            tool_args=_todo_modify_complete_args(
                messages, 0, fallback_contents=_TODO_CONTENTS
            ),
        )
    if stage == 6:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text="权限已确认。现在继续更新任务状态并完善人物介绍：\n" + _CHARACTER_INTRO,
            tool_name="todo_modify",
            tool_args=_todo_modify_complete_args(
                messages, 1, fallback_contents=_TODO_CONTENTS
            ),
        )
    if stage == 7:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text="人物介绍已完善，标记「故事背景设定」完成：\n",
            tool_name="todo_modify",
            tool_args=_todo_modify_complete_args(
                messages, 2, fallback_contents=_TODO_CONTENTS
            ),
        )
    if stage == 8:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text="背景设定已就绪，标记「故事冲突与悬念设置」完成：\n",
            tool_name="todo_modify",
            tool_args=_todo_modify_complete_args(
                messages, 3, fallback_contents=_TODO_CONTENTS
            ),
        )
    if stage == 9:
        path = _resolve_novel_file_path(messages)
        return _AgentPlan(
            kind="intro_and_tool_call",
            text="现在让我完成最后一个任务，将文件发送给你：\n",
            tool_name="send_file_to_user",
            tool_args={"abs_file_path_list": [path]},
        )
    if stage == 10:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text="文件已发送，标记「整理并发送开篇文件」完成：\n",
            tool_name="todo_modify",
            tool_args=_todo_modify_complete_args(
                messages, 4, fallback_contents=_TODO_CONTENTS
            ),
        )
    return _AgentPlan(kind="stream_text", text=_NOVEL_FINAL_MESSAGE)


def _resolve_beijing_guide_path(messages: list[Any]) -> str:
    """从 write_file / tool 结果解析攻略绝对路径，供 send_file_to_user 使用。"""
    for message in reversed(messages):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        path = _parse_file_path_from_tool_blob(_tool_message_text(message), _BEIJING_GUIDE_FILENAME)
        if path:
            return path
    for args in reversed(_assistant_tool_call_args(messages, "write_file")):
        path = args.get("file_path") or args.get("path")
        if isinstance(path, str) and path and _BEIJING_GUIDE_FILENAME in path:
            return path
    return _BEIJING_GUIDE_FILENAME


def _plan_scheduled_task_flow_response(
    payload: dict[str, Any],
    *,
    stage: int,
    novel_chars: int,
    excerpt_chars: int,
) -> _AgentPlan:
    """定时任务场景：调用 cron_create_job 创建一次性延迟任务 → 返回确认消息。"""
    _ = payload, novel_chars, excerpt_chars
    if stage == 0:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text=_CRON_INTRO,
            tool_name="cron_create_job",
            tool_args=_cron_job_args(),
        )
    return _AgentPlan(kind="stream_text", text=_CRON_DONE_MESSAGE)


def _plan_cron_delivery_response(
    payload: dict[str, Any],
    *,
    stage: int,
    novel_chars: int,
    excerpt_chars: int,
) -> _AgentPlan:
    """定时任务到点：仅返回一句喝水提醒，不触发 travel / 通用 mock。"""
    _ = payload, stage, novel_chars, excerpt_chars
    return _AgentPlan(kind="stream_text", text=_CRON_DELIVERY_MESSAGE)


def _plan_skill_flow_response(
    payload: dict[str, Any],
    *,
    stage: int,
    novel_chars: int,
    excerpt_chars: int,
) -> _AgentPlan:
    """技能场景：todo → write_file → send_file_to_user（对齐 Deep 已验证工具）。"""
    _ = novel_chars, excerpt_chars
    messages = _agent_messages(payload)

    if stage == 0:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text=_SKILL_INTRO,
            tool_name="todo_create",
            tool_args={"tasks": _todo_create_tasks(_SKILL_TODO_CONTENTS), "force": True},
        )
    if stage == 1:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text=_SKILL_BEFORE_WRITE_INTRO,
            tool_name="write_file",
            tool_args={
                "file_path": _BEIJING_GUIDE_FILENAME,
                "content": _load_beijing_guide_content(),
            },
        )
    if stage == 2:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text="攻略文档已写入，标记「生成北京3日游攻略」完成：\n",
            tool_name="todo_modify",
            tool_args=_todo_modify_complete_args(
                messages, 0, fallback_contents=_SKILL_TODO_CONTENTS
            ),
        )
    if stage == 3:
        guide_path = _resolve_beijing_guide_path(messages)
        return _AgentPlan(
            kind="intro_and_tool_call",
            text=_SKILL_BEFORE_SEND_INTRO,
            tool_name="send_file_to_user",
            tool_args={"abs_file_path_list": [guide_path]},
        )
    if stage == 4:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text="文件已发送，标记「交付攻略文档」完成：\n",
            tool_name="todo_modify",
            tool_args=_todo_modify_complete_args(
                messages, 1, fallback_contents=_SKILL_TODO_CONTENTS
            ),
        )
    return _AgentPlan(kind="stream_text", text=_SKILL_DONE_MESSAGE)


def _plan_file_flow_response(
    payload: dict[str, Any],
    *,
    stage: int,
    novel_chars: int,
    excerpt_chars: int,
) -> _AgentPlan:
    """文件扩写场景：bash(下载/确认) → read_file → write_file → send_file_to_user → 确认消息。"""
    _ = novel_chars, excerpt_chars
    messages = _agent_messages(payload)
    upload = _pick_upload_file(messages)
    source_path = _resolve_spring_local_read_path(messages, upload)
    output_path = _resolve_spring_output_path(messages)
    send_path = _resolve_spring_send_path(messages)
    expanded_body = _build_spring_essay_expanded(target_chars=_SPRING_ESSAY_TARGET_CHARS)

    if stage == 0:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text="我先把上传的作文下载到工作区并确认文件：\n",
            tool_name="bash",
            tool_args={"command": _build_file_download_bash({
                **upload,
                "name": source_path,
            })},
        )
    if stage == 1:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text="接下来读取原文内容：\n",
            tool_name="read_file",
            tool_args={"file_path": source_path},
        )
    if stage == 2:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text="正在把作文扩写到约 6000 字并写入文件：\n",
            tool_name="write_file",
            tool_args={"file_path": output_path, "content": expanded_body},
        )
    if stage == 3:
        return _AgentPlan(
            kind="intro_and_tool_call",
            text="扩写完成，现在把文件发回给你：\n",
            tool_name="send_file_to_user",
            tool_args={"abs_file_path_list": [send_path]},
        )
    return _AgentPlan(kind="stream_text", text=_FILE_DONE_MESSAGE)


_SCENARIO_PLANNERS: dict[str, Any] = {
    MockScenario.TRAVEL: _plan_travel_flow_response,
    MockScenario.SCHEDULED_TASK: _plan_scheduled_task_flow_response,
    MockScenario.CRON_DELIVERY: _plan_cron_delivery_response,
    MockScenario.SKILL: _plan_skill_flow_response,
    MockScenario.FILE: _plan_file_flow_response,
}


def _plan_loadtest_response(
    payload: dict[str, Any],
    *,
    novel_chars: int,
    excerpt_chars: int,
) -> tuple[_AgentPlan, str, int]:
    """按场景分发 Mock 响应计划，返回 (plan, scenario, stage)。"""
    if _is_session_memory_request(payload):
        return _plan_session_memory_response(payload), MockScenario.TRAVEL, 0

    if _is_cron_wake_message(payload):
        plan = _plan_cron_delivery_response(
            payload,
            stage=0,
            novel_chars=novel_chars,
            excerpt_chars=excerpt_chars,
        )
        return plan, MockScenario.CRON_DELIVERY, 0

    routed_scenario = _detect_loadtest_scenario(payload)
    key, stage, scenario = _prepare_loadtest_state(payload, routed_scenario)
    if stage > 0:
        failure = _latest_tool_failure(_agent_messages(payload))
        if failure:
            logger.warning(
                "loadtest fail-fast scenario=%s stage=%d reason=%s",
                scenario,
                stage,
                failure,
            )
            _mark_loadtest_scenario_done(key, payload)
            return _fail_fast_plan(scenario, stage, failure), scenario, stage
    planner = _SCENARIO_PLANNERS.get(scenario, _plan_travel_flow_response)
    plan = planner(
        payload,
        stage=stage,
        novel_chars=novel_chars,
        excerpt_chars=excerpt_chars,
    )
    _advance_loadtest_state(key, scenario, stage, payload)
    return plan, scenario, stage


def _mock_usage(*, completion_chars: int, prompt_tokens: int = 10) -> dict[str, int]:
    """与 OpenAI usage 字段对齐，供 AgentServer 汇总为 chat.usage_summary。"""
    prompt = max(1, prompt_tokens)
    completion = max(20, completion_chars // 4)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }


async def _write_sse_headers(writer: asyncio.StreamWriter) -> None:
    headers = (
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: text/event-stream\r\n"
        "Cache-Control: no-cache\r\n"
        "Connection: close\r\n"
        "\r\n"
    )
    writer.write(headers.encode("utf-8"))
    await writer.drain()


async def _write_sse_finish(
    writer: asyncio.StreamWriter,
    model: str,
    *,
    finish_reason: str = "stop",
    completion_chars: int = 0,
    prompt_tokens: int = 10,
) -> None:
    usage = _mock_usage(completion_chars=completion_chars, prompt_tokens=prompt_tokens)
    final_chunk = {
        "id": "mock-chatcmpl-stream",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {},
                "finish_reason": finish_reason,
            }
        ],
        "usage": usage,
    }
    writer.write(_sse_event(final_chunk))
    await writer.drain()
    # stream_options.include_usage 风格：额外发一帧仅含 usage 的 chunk，便于 SDK 聚合。
    usage_only_chunk = {
        "id": "mock-chatcmpl-stream",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [],
        "usage": usage,
    }
    writer.write(_sse_event(usage_only_chunk))
    writer.write(_sse_event("[DONE]"))
    await writer.drain()


async def _stream_text_content(
    writer: asyncio.StreamWriter,
    model: str,
    text: str,
    *,
    chunk_chars: int,
    token_interval_s: float,
    log_label: str = "content",
) -> None:
    await _write_sse_headers(writer)
    total_chunks = max(1, (len(text) + chunk_chars - 1) // chunk_chars)
    for offset in range(0, len(text), chunk_chars):
        piece = text[offset:offset + chunk_chars]
        chunk = {
            "id": "mock-chatcmpl-stream",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": piece},
                    "finish_reason": None,
                }
            ],
        }
        writer.write(_sse_event(chunk))
        await writer.drain()
        chunk_no = offset // chunk_chars + 1
        if chunk_no == 1 or chunk_no == total_chunks or chunk_no % 40 == 0:
            logger.info(
                "Streamed %s chunk %d/%d (%d chars, total=%d)",
                log_label,
                chunk_no,
                total_chunks,
                len(piece),
                len(text),
            )
        if chunk_no < total_chunks and token_interval_s > 0:
            await asyncio.sleep(token_interval_s)
    await _write_sse_finish(writer, model, finish_reason="stop", completion_chars=len(text))


async def _emit_tool_call_sse(
    writer: asyncio.StreamWriter,
    model: str,
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    token_interval_s: float,
) -> str:
    call_id = f"call_mock_{secrets.token_hex(12)}"
    tool_args_json = json.dumps(tool_args, ensure_ascii=False)

    first = {
        "id": "mock-chatcmpl-stream",
        "object": "chat.completion.chunk",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "delta": {
                    "tool_calls": [
                        {
                            "index": 0,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": tool_name, "arguments": ""},
                        }
                    ]
                },
                "finish_reason": None,
            }
        ],
    }
    writer.write(_sse_event(first))
    await writer.drain()

    arg_step = 96
    for offset in range(0, len(tool_args_json), arg_step):
        piece = tool_args_json[offset:offset + arg_step]
        chunk = {
            "id": "mock-chatcmpl-stream",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"arguments": piece},
                            }
                        ]
                    },
                    "finish_reason": None,
                }
            ],
        }
        writer.write(_sse_event(chunk))
        await writer.drain()
        if token_interval_s > 0:
            await asyncio.sleep(token_interval_s)

    logger.info(
        "Streamed tool_call name=%s args_len=%d call_id=%s",
        tool_name,
        len(tool_args_json),
        call_id,
    )
    return call_id


async def _stream_tool_call(
    writer: asyncio.StreamWriter,
    model: str,
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    token_interval_s: float,
) -> None:
    await _write_sse_headers(writer)
    await _emit_tool_call_sse(
        writer,
        model,
        tool_name,
        tool_args,
        token_interval_s=token_interval_s,
    )
    await _write_sse_finish(
        writer,
        model,
        finish_reason="tool_calls",
        completion_chars=len(tool_name) + len(json.dumps(tool_args, ensure_ascii=False)),
    )


async def _stream_content_and_tool_call(
    writer: asyncio.StreamWriter,
    model: str,
    intro: str,
    tool_name: str,
    tool_args: dict[str, Any],
    *,
    chunk_chars: int,
    token_interval_s: float,
) -> None:
    await _write_sse_headers(writer)
    total_chunks = max(1, (len(intro) + chunk_chars - 1) // chunk_chars)
    for offset in range(0, len(intro), chunk_chars):
        piece = intro[offset:offset + chunk_chars]
        chunk = {
            "id": "mock-chatcmpl-stream",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": piece},
                    "finish_reason": None,
                }
            ],
        }
        writer.write(_sse_event(chunk))
        await writer.drain()
        if offset // chunk_chars + 1 < total_chunks and token_interval_s > 0:
            await asyncio.sleep(token_interval_s)
    await _emit_tool_call_sse(
        writer,
        model,
        tool_name,
        tool_args,
        token_interval_s=token_interval_s,
    )
    tool_args_json = json.dumps(tool_args, ensure_ascii=False)
    await _write_sse_finish(
        writer,
        model,
        finish_reason="tool_calls",
        completion_chars=len(intro) + len(tool_name) + len(tool_args_json),
    )


async def _stream_chat_completion(
    writer: asyncio.StreamWriter,
    model: str,
    *,
    token_count: int,
    token_interval_s: float,
    text: str | None = None,
    chunk_chars: int = LOADTEST_STREAM_CHUNK_CHARS,
) -> None:
    if text is not None:
        await _stream_text_content(
            writer,
            model,
            text,
            chunk_chars=max(32, chunk_chars),
            token_interval_s=token_interval_s,
            log_label="novel",
        )
        return

    await _write_sse_headers(writer)
    completion_chars = 0
    for i in range(1, token_count + 1):
        token = f"mock token{i}"
        completion_chars += len(token)
        chunk = {
            "id": "mock-chatcmpl-stream",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": token},
                    "finish_reason": None,
                }
            ],
        }
        writer.write(_sse_event(chunk))
        await writer.drain()
        logger.info("Streamed token: %s", token)
        if i < token_count and token_interval_s > 0:
            await asyncio.sleep(token_interval_s)
    await _write_sse_finish(
        writer,
        model,
        finish_reason="stop",
        completion_chars=completion_chars,
    )


def _non_stream_completion(
    model: str,
    *,
    content: str | None = None,
    tool_call: dict[str, Any] | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    finish_reason = "stop"
    if tool_call is not None:
        message["tool_calls"] = [tool_call]
        message["content"] = content
        finish_reason = "tool_calls"
    completion_chars = len(content or "")
    if tool_call is not None:
        fn = tool_call.get("function") if isinstance(tool_call.get("function"), dict) else {}
        completion_chars += len(str(fn.get("name") or "")) + len(str(fn.get("arguments") or ""))
    return {
        "id": "mock-chatcmpl-123",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": finish_reason,
            }
        ],
        "usage": _mock_usage(completion_chars=completion_chars),
    }


async def _handle_request(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    token_count: int,
    token_interval_s: float,
    stats: _RequestStats,
    profile: str,
    novel_chars: int,
    chunk_chars: int,
    excerpt_chars: int,
) -> None:
    stream = False
    try:
        method, path, headers, payload = await _read_http_request(reader)
        stream = _wants_stream(headers, payload)
        await stats.begin(stream=stream)
        logger.info(
            "Request: %s %s stream=%s body_bytes=%s accept=%s",
            method,
            path,
            payload.get("stream"),
            headers.get("content-length", "?"),
            headers.get("accept", ""),
        )

        if method == "GET" and path == "/health":
            body = json.dumps({"status": "ok"})
            writer.write(_http_response(200, body))
            await writer.drain()
            return

        if method == "GET" and path.rstrip("/") == "/v1/models":
            writer.write(_http_response(200, _models_payload()))
            await writer.drain()
            return

        if method == "POST" and path.rstrip("/") == "/v1/chat/completions":
            model = str(payload.get("model") or "mock-model")
            use_loadtest = _should_use_novel_scenario(profile, payload)
            if use_loadtest:
                plan, scenario, stage = _plan_loadtest_response(
                    payload,
                    novel_chars=novel_chars,
                    excerpt_chars=excerpt_chars,
                )
                if _is_session_memory_request(payload):
                    logger.info(
                        "Session memory scenario kind=%s tool=%s notes_path=%s",
                        plan.kind,
                        plan.tool_name,
                        _parse_session_memory_notes_path(payload.get("messages") or []),
                    )
                else:
                    logger.info(
                        "Agent loadtest scenario=%s kind=%s stage=%d novel_chars=%d excerpt_chars=%d",
                        scenario,
                        plan.kind,
                        stage,
                        novel_chars,
                        excerpt_chars,
                    )
                if stream:
                    if (
                        plan.kind == "intro_and_tool_call"
                        and plan.tool_name
                        and plan.tool_args is not None
                    ):
                        await _stream_content_and_tool_call(
                            writer,
                            model,
                            plan.text or "",
                            plan.tool_name,
                            plan.tool_args,
                            chunk_chars=chunk_chars,
                            token_interval_s=token_interval_s,
                        )
                    elif plan.kind == "tool_call" and plan.tool_name and plan.tool_args is not None:
                        await _stream_tool_call(
                            writer,
                            model,
                            plan.tool_name,
                            plan.tool_args,
                            token_interval_s=token_interval_s,
                        )
                    else:
                        await _stream_chat_completion(
                            writer,
                            model,
                            token_count=token_count,
                            token_interval_s=token_interval_s,
                            text=plan.text or _NOVEL_FINAL_MESSAGE,
                            chunk_chars=chunk_chars,
                        )
                    return

                if (
                    plan.kind in {"tool_call", "intro_and_tool_call"}
                    and plan.tool_name
                    and plan.tool_args is not None
                ):
                    tool_call = {
                        "id": f"call_mock_{secrets.token_hex(12)}",
                        "type": "function",
                        "function": {
                            "name": plan.tool_name,
                            "arguments": json.dumps(plan.tool_args, ensure_ascii=False),
                        },
                    }
                    response = _non_stream_completion(model, content=plan.text, tool_call=tool_call)
                else:
                    response = _non_stream_completion(model, content=plan.text or _NOVEL_FINAL_MESSAGE)
                body = json.dumps(response, ensure_ascii=False)
                writer.write(_http_response(200, body))
                await writer.drain()
                return

            if stream:
                logger.info(
                    "Generic mock tokens (profile=%s, use --profile loadtest for novel scenario)",
                    profile,
                )
                await _stream_chat_completion(
                    writer,
                    model,
                    token_count=token_count,
                    token_interval_s=token_interval_s,
                )
                return

            content = " ".join(f"mock token{i}" for i in range(1, token_count + 1))
            logger.info("Non-stream response content: %s", content[:120])
            response = _non_stream_completion(model, content=content)
            body = json.dumps(response, ensure_ascii=False)
            writer.write(_http_response(200, body))
            await writer.drain()
            return

        writer.write(_http_response(404, json.dumps({"error": "not found"})))
        await writer.drain()
    except Exception as exc:
        logger.exception("Error handling request: %s", exc)
        writer.write(_http_response(500, json.dumps({"error": str(exc)})))
        await writer.drain()
    finally:
        await stats.end()
        writer.close()
        await writer.wait_closed()


async def _stats_loop(stats: _RequestStats, interval_s: float) -> None:
    while True:
        await asyncio.sleep(interval_s)
        active, total, stream_total = await stats.snapshot()
        logger.info(
            "[stats] active=%d total=%d stream_total=%d",
            active,
            total,
            stream_total,
        )


def _configure_logging() -> None:
    class _TimestampFormatter(logging.Formatter):
        def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
            dt = datetime.fromtimestamp(record.created)
            base = dt.strftime(datefmt or "%Y-%m-%d %H:%M:%S")
            return f"{base}.{int(record.msecs):03d}"

    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(_TimestampFormatter("%(asctime)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
    root.addHandler(handler)


def _argv_has(flag: str) -> bool:
    return flag in sys.argv


def _env_int(name: str) -> int | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 须为整数，当前={raw!r}") from exc


def _env_float(name: str) -> float | None:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} 须为数字，当前={raw!r}") from exc


def _resolve_stream_params(args: argparse.Namespace) -> tuple[int, float]:
    """token 数 / 间隔：CLI > 环境变量 > profile 默认（e2e=20/2s，loadtest=5/0.05s）。"""
    if _argv_has("--stream-token-count"):
        token_count = args.stream_token_count
    else:
        env_count = _env_int("MOCK_LLM_STREAM_TOKEN_COUNT")
        if env_count is not None:
            token_count = env_count
        elif args.profile == "loadtest":
            token_count = LOADTEST_STREAM_TOKEN_COUNT
        else:
            token_count = DEFAULT_STREAM_TOKEN_COUNT

    if _argv_has("--stream-token-interval"):
        token_interval = args.stream_token_interval
    else:
        env_interval = _env_float("MOCK_LLM_STREAM_TOKEN_INTERVAL")
        if env_interval is not None:
            token_interval = env_interval
        elif args.profile == "loadtest":
            token_interval = LOADTEST_STREAM_TOKEN_INTERVAL_S
        else:
            token_interval = DEFAULT_STREAM_TOKEN_INTERVAL_S

    return max(1, token_count), max(0.0, token_interval)


async def main(
    host: str,
    port: int,
    *,
    token_count: int,
    token_interval_s: float,
    stats_interval_s: float,
    profile: str,
    novel_chars: int,
    chunk_chars: int,
    excerpt_chars: int,
) -> None:
    stats = _RequestStats()
    stats_task: asyncio.Task[None] | None = None
    if stats_interval_s > 0:
        stats_task = asyncio.create_task(_stats_loop(stats, stats_interval_s))

    async def _handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _handle_request(
            reader,
            writer,
            token_count=token_count,
            token_interval_s=token_interval_s,
            stats=stats,
            profile=profile,
            novel_chars=novel_chars,
            chunk_chars=chunk_chars,
            excerpt_chars=excerpt_chars,
        )

    server = await asyncio.start_server(_handler, host, port)
    addr = server.sockets[0].getsockname()
    logger.info(
        "Mock LLM server listening on http://%s:%d (profile=%s tokens=%d interval=%ss novel_chars=%d chunk_chars=%d)",
        addr[0],
        addr[1],
        profile,
        token_count,
        token_interval_s,
        novel_chars,
        chunk_chars,
    )
    if profile == "loadtest":
        logger.info(
            "loadtest profile active: per-session stage + done flag; "
            "tool fail-fast (stop later tools after a failed tool result); "
            "re-route after task done: intent/payload/sequence "
            "(priority: cron_delivery>scheduled_task>skill>file>travel; skip prior completed scenarios)"
        )
    logger.info("Health: http://%s:%d/health", addr[0], addr[1])

    stop_event = asyncio.Event()

    def _request_stop(*_args: object) -> None:
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_a: _request_stop())

    try:
        async with server:
            serve_task = asyncio.create_task(server.serve_forever())
            await stop_event.wait()
            server.close()
            await server.wait_closed()
            serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await serve_task
    finally:
        if stats_task is not None:
            stats_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stats_task
        active, total, stream_total = await stats.snapshot()
        logger.info(
            "[shutdown] active=%d total=%d stream_total=%d",
            active,
            total,
            stream_total,
        )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OpenAI 兼容 Mock LLM（Enterprise Runtime 压测 / E2E）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，压测对外服务可用 0.0.0.0")
    parser.add_argument("--port", type=int, default=19999, help="HTTP 端口")
    parser.add_argument(
        "--profile",
        choices=("e2e", "loadtest"),
        default="e2e",
        help="e2e: mock token；loadtest: 模拟 Agent 多工具小说创作（todo/write_file/read_file/send_file）",
    )
    parser.add_argument(
        "--novel-chars",
        type=int,
        default=LOADTEST_NOVEL_CHARS,
        help="loadtest 小说场景正文总字数（默认 32000，模拟长文而非真实十万字）",
    )
    parser.add_argument(
        "--stream-chunk-chars",
        type=int,
        default=LOADTEST_STREAM_CHUNK_CHARS,
        help="流式 SSE 每次输出的字符数（loadtest 小说场景）",
    )
    parser.add_argument(
        "--chat-excerpt-chars",
        type=int,
        default=LOADTEST_CHAT_EXCERPT_CHARS,
        help="loadtest 第 2 轮在聊天区展示的开篇章节字数（默认 6000）",
    )
    parser.add_argument(
        "--stream-token-count",
        type=int,
        default=DEFAULT_STREAM_TOKEN_COUNT,
        help=(
            "e2e 固定 mock token 个数（mock token1..N）。"
            "优先级：本参数 > 环境变量 MOCK_LLM_STREAM_TOKEN_COUNT > "
            f"profile 默认（e2e={DEFAULT_STREAM_TOKEN_COUNT}，loadtest={LOADTEST_STREAM_TOKEN_COUNT}）"
        ),
    )
    parser.add_argument(
        "--stream-token-interval",
        type=float,
        default=DEFAULT_STREAM_TOKEN_INTERVAL_S,
        help=(
            "相邻 token 间隔（秒）。"
            "优先级：本参数 > 环境变量 MOCK_LLM_STREAM_TOKEN_INTERVAL > "
            f"profile 默认（e2e={DEFAULT_STREAM_TOKEN_INTERVAL_S}，loadtest={LOADTEST_STREAM_TOKEN_INTERVAL_S}）"
        ),
    )
    parser.add_argument(
        "--stats-interval",
        type=float,
        default=30.0,
        help="周期性打印 [stats] 的间隔（秒）；0 表示关闭",
    )
    return parser.parse_args()


if __name__ == "__main__":
    _configure_logging()
    cli_args = _parse_args()
    count, interval = _resolve_stream_params(cli_args)
    try:
        asyncio.run(
            main(
                cli_args.host,
                cli_args.port,
                token_count=count,
                token_interval_s=interval,
                stats_interval_s=max(0.0, cli_args.stats_interval),
                profile=cli_args.profile,
                novel_chars=max(1000, cli_args.novel_chars),
                chunk_chars=max(32, cli_args.stream_chunk_chars),
                excerpt_chars=max(200, cli_args.chat_excerpt_chars),
            )
        )
    except KeyboardInterrupt:
        pass
