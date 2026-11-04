# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Skill 上传 multipart 处理函数 entry-point 工厂（主仓侧装配点）。

gateway 进程经 ``importlib.metadata.entry_points(group="gateway.skill_upload_handlers")``
发现本模块的 ``build_skill_upload_handlers`` 工厂（在 ``pyproject.toml`` 中声明），
调用后获得一个实现 ``SkillUploadHandlers`` 协议的对象，注入到
``WebChannel.skill_upload_handlers``。gateway 侧不 import 本模块（R3 不变量），
仅经 entry-point 元数据间接加载。

本模块属主仓 ``jiuwenswarm.server.runtime.skill.*``，可自由引用 ``SkillManager``
与 ``skills_multipart_http`` 处理函数；闭包绑定后交由 gateway 调用。
"""

from __future__ import annotations

from typing import Any


def build_skill_upload_handlers() -> Any:
    """返回绑定主仓 multipart handlers 的 SkillUploadHandlers 实现。

    gateway 侧 ``SkillUploadHandlers`` 为 ``typing.Protocol``（duck-typed），
    本函数返回的对象按其方法签名实现，无需继承（避免主仓 import gateway）。
    """
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
    from jiuwenswarm.server.runtime.skill.skills_multipart_http import (
        handle_skills_create_from_knowledge_http,
        handle_skills_import_http,
        handle_skills_uninstall_http,
        handle_skills_upload_temp_http,
    )

    class _Handlers:
        """SkillUploadHandlers 协议实现（绑定主仓 multipart handlers + SkillManager）。"""

        @staticmethod
        def handle_skills_upload_temp_http(
            *, content_type: str, body: bytes, caller_id: str = ""
        ) -> tuple[int, dict[str, Any]]:
            return handle_skills_upload_temp_http(
                content_type=content_type, body=body, caller_id=caller_id
            )

        @staticmethod
        def handle_skills_import_http(
            *,
            content_type: str,
            body: bytes,
            use_local_manager: bool = False,
            caller_id: str = "",
            cluster_id: str = "",
        ) -> tuple[int, dict[str, Any]]:
            return handle_skills_import_http(
                content_type=content_type,
                body=body,
                use_local_manager=use_local_manager,
                caller_id=caller_id,
                cluster_id=cluster_id,
            )

        @staticmethod
        def handle_skills_create_from_knowledge_http(
            *, content_type: str, body: bytes, use_local_manager: bool = False
        ) -> tuple[int, dict[str, Any]]:
            return handle_skills_create_from_knowledge_http(
                content_type=content_type,
                body=body,
                use_local_manager=use_local_manager,
            )

        @staticmethod
        def handle_skills_uninstall_http(
            *, name: str, origin: str = ""
        ) -> tuple[int, dict[str, Any]]:
            return handle_skills_uninstall_http(name=name, origin=origin)

        @staticmethod
        def list_local_user_skills() -> list[dict[str, Any]]:
            mgr = SkillManager()
            mgr.reload_state()
            return mgr.get_local_skills()

    return _Handlers()
