# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""布局类型与注册表。storage 不内置业务 name。"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class FileLayout:
    """文件落盘方式。

    ``path`` 必须是绝对路径，或带 ``{field}`` 的绝对模板，由装配层写入。
    storage 只替换占位符（缺字段则 glob），不拼接 root、不查 named_files。

        /ws/service_default/agent_default/.checkpoint/session_map.json
        /ws/gateway/cron/service_{service_id}/agent_{agent_id}/cron_jobs.json
        /home/user/.jiuwenswarm/config.yaml

    backend 在磁盘形态与协议 record（一条 JSON 可序列化 dict）之间转换：

        落盘        磁盘上                         协议里的一条 record
        ----------  -----------------------------  -----------------------------
        JSON map    {"k1": {"foo": 1}}             {"id": "k1", "foo": 1}
        JSON list   [{"id": "k1", "foo": 1}]       {"id": "k1", "foo": 1}
        YAML map    web: {enabled: true}           {"id": "web", "enabled": true}
        YAML 整段   logging: {level: INFO, ...}    {"level": "INFO", "console_level": "INFO", ...}

    JSON 同时支持 map / list，是为了兼容已有 personal 文件，不是协议有两套 API：
    查找表历史上把主键当对象 key（map）；文档列表把主键留在每条 record 里（list）。
    ``shape`` 只决定 JSON 怎么编码；YAML 与 DB 都不用它。

    JSON ``shape="map"``：对象 key 来自 ``key_fields[0]``（写时从 body 剥掉，读时写回）。
    JSON ``shape="list"``：磁盘就是 record 数组，主键留在每条 object 里。
    ``json_document_key``（仅 JSON list）：磁盘为 ``{"version": 1, "<key>": [records]}``，
    兼容 ``CronJobStore`` 的 ``cron_jobs.json`` 包装格式。
    YAML 不用 ``shape``：``yaml_pointer`` 取片段；有 ``key_fields`` 时同样把 mapping key 注入 record。

    ``key_fields`` 声明 record 主键由哪些字段组成（JSON / YAML 都用，DB 不用）：
    1. CRUD：get / update / delete / 判重时按这些字段比对。
    2. mapping 编解码：只用第一个字段当对象 key（写盘从 body 剥掉，读回写进 record）。
    3. 空元组：没有按行主键，整段当一份 document，该 name 最多一条。
       YAML overlay（``yaml_pointer="/logging"``）::

           logging:
             level: INFO
             console_level: INFO
             gateway: INFO
             channel: INFO
             agent_server: INFO
             full: INFO

           record: {"level": "INFO", "console_level": "INFO", "gateway": "INFO",
                    "channel": "INFO", "agent_server": "INFO", "full": "INFO"}
    JSON list 不靠它改变文件形状，但仍用第 1 点定位记录。
    """

    path: str  # absolute path, or absolute template with {field}
    format: Literal["json", "yaml"] = "json"
    shape: Literal["map", "list"] = "map"  # JSON only; YAML ignores this
    yaml_pointer: str = ""  # YAML only; JSON ignores this. fragment path e.g. "/channels"
    key_fields: tuple[str, ...] = ()  # record primary key; first field is map key. empty = single document
    # JSON list only：record 数组包在 document 对象的该字段下（如 cron ``jobs``）
    json_document_key: str = ""
    # YAML only：片段是标量时，用该字段名包装/解包为单字段 record（如 preferred_language）
    yaml_scalar_field: str = ""


@dataclass(frozen=True)
class DbLayout:
    """数据库落盘方式。"""

    table: str


@dataclass(frozen=True)
class StoreLayout:
    file: FileLayout | None = None
    db: DbLayout | None = None


class StoreRegistry:
    """name → 落盘布局。由装配层填充，backend 只查询。"""

    def __init__(self) -> None:
        self._layouts: dict[str, StoreLayout] = {}

    def register(self, name: str, layout: StoreLayout) -> None:
        key = str(name or "").strip()
        if not key:
            raise ValueError("name is empty")
        if not isinstance(layout, StoreLayout):
            raise TypeError("layout must be StoreLayout")
        self._layouts[key] = layout

    def register_many(self, layouts: Mapping[str, StoreLayout]) -> None:
        for name, layout in layouts.items():
            self.register(name, layout)

    def get(self, name: str) -> StoreLayout | None:
        key = str(name or "").strip()
        if not key:
            return None
        return self._layouts.get(key)

    def all(self) -> dict[str, StoreLayout]:
        return dict(self._layouts)

    def db_table_names(self) -> frozenset[str]:
        names: set[str] = set()
        for layout in self._layouts.values():
            table = layout.db.table if layout.db is not None else ""
            table_name = str(table or "").strip()
            if table_name:
                names.add(table_name)
        return frozenset(names)


__all__ = [
    "DbLayout",
    "FileLayout",
    "StoreLayout",
    "StoreRegistry",
]
