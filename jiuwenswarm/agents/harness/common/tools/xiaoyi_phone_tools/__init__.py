# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Xiaoyi Handset Tools - 小艺手机端设备工具.

该目录包含需要连接小艺手机端设备才能使用的工具。
这些工具通过 WebSocket 与手机端通信，调用设备原生能力。

工具分类：
- 定位: get_user_location
- 备忘录: create_note, search_notes, modify_note
- 日历: create_calendar_event, search_calendar_event
- 联系人: search_contact
- 相册: search_photo_gallery, upload_photo
- 文件: search_file, upload_file, send_file_to_user
- 电话: call_phone
- 短信/消息: send_message, search_message
- 闹钟: create_alarm, search_alarms, modify_alarm, delete_alarm
- 收藏: query_collection, add_collection, delete_collection
- 保存: save_media_to_gallery, save_file_to_file_manager
- 推送记录: view_push_result
- GUI 自动化: xiaoyi_gui_agent
- 图像理解: image_reading
- 时间戳转换: convert_timestamp_to_utc8_time
"""

from importlib import import_module


# Permission policy imports the timestamp tool to verify its callable identity.
# Resolving that one local utility must not also prepare every handset tool.
_EXPORT_MODULES = {
    "get_user_location": ".location_tool",
    "create_note": ".note_tools",
    "search_notes": ".note_tools",
    "modify_note": ".note_tools",
    "create_calendar_event": ".calendar_tools",
    "search_calendar_event": ".calendar_tools",
    "search_contact": ".contact_tools",
    "search_photo_gallery": ".photo_tools",
    "upload_photo": ".photo_tools",
    "search_file": ".file_tools",
    "upload_file": ".file_tools",
    "call_phone": ".phone_tools",
    "send_message": ".message_tools",
    "search_message": ".message_tools",
    "create_alarm": ".alarm_tools",
    "search_alarms": ".alarm_tools",
    "modify_alarm": ".alarm_tools",
    "delete_alarm": ".alarm_tools",
    "query_collection": ".xiaoyi_collection_tool",
    "add_collection": ".xiaoyi_collection_tool",
    "delete_collection": ".xiaoyi_collection_tool",
    "save_media_to_gallery": ".save_tools",
    "save_file_to_file_manager": ".save_tools",
    "view_push_result": ".push_result_tool",
    "convert_timestamp_to_utc8_time": ".timestamp_tool",
    "xiaoyi_gui_agent": ".xiaoyi_gui_tool",
    "image_reading": ".image_reading_tool",
}

__all__ = [
    "get_user_location",
    "create_note",
    "search_notes",
    "modify_note",
    "create_calendar_event",
    "search_calendar_event",
    "search_contact",
    "search_photo_gallery",
    "upload_photo",
    "search_file",
    "upload_file",
    "call_phone",
    "send_message",
    "search_message",
    "create_alarm",
    "search_alarms",
    "modify_alarm",
    "delete_alarm",
    "query_collection",
    "add_collection",
    "delete_collection",
    "save_media_to_gallery",
    "save_file_to_file_manager",
    "view_push_result",
    "convert_timestamp_to_utc8_time",
    "xiaoyi_gui_agent",
    "image_reading",
]


def __getattr__(name: str):
    module_name = _EXPORT_MODULES.get(name)
    if module_name is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    value = getattr(import_module(module_name, __name__), name)
    globals()[name] = value
    return value
