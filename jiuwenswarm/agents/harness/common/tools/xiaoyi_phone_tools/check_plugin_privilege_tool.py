from __future__ import annotations

import asyncio
import json
from typing import Any

from openjiuwen.core.foundation.tool import tool

from jiuwenswarm.common.utils import logger

from .utils import execute_device_command, raise_if_device_error


INTENT_PERMISSION_MAP: dict[str, list[str]] = {
    "GetCurrentLocation": [
        "ohos.permission.LOCATION",
        "ohos.permission.APPROXIMATELY_LOCATION",
    ],
    "SearchCalendarEvent": ["ohos.permission.READ_WHOLE_CALENDAR"],
    "CreateCalendarEvent": ["ohos.permission.WRITE_WHOLE_CALENDAR"],
    "DeleteCalendarEvent": ["ohos.permission.WRITE_WHOLE_CALENDAR"],
    "ModifyCalendarEvent": ["ohos.permission.WRITE_WHOLE_CALENDAR"],
    "SearchNote": ["ohos.permission.READ_NOTE"],
    "CreateNote": ["ohos.permission.WRITE_NOTE"],
    "ModifyNote": ["ohos.permission.WRITE_NOTE"],
    "SearchContactLocal": ["ohos.permission.READ_CONTACTS"],
    "SearchPhotoVideo": ["ohos.permission.READ_IMAGEVIDEO"],
    "SaveMediaToGallery": ["ohos.permission.WRITE_IMAGEVIDEO"],
    "SearchFile": ["ohos.permission.FILE_ACCESS_MANAGER"],
    "SaveFileToFileManager": ["ohos.permission.FILE_SAVE_MANAGER"],
    "SearchAlarm": ["ohos.permission.READ_ALARM"],
    "CreateAlarm": ["ohos.permission.WRITE_ALARM"],
    "ModifyAlarm": ["ohos.permission.WRITE_ALARM"],
    "DeleteAlarm": ["ohos.permission.WRITE_ALARM"],
    "SearchMessage": ["ohos.permission.READ_SMS"],
    "SendShortMessage": ["ohos.permission.SEND_MESSAGES"],
    "StartCall": ["ohos.permission.PLACE_CALL"],
}


def build_check_plugin_privilege_command(
    check_intent_name: str,
    permission_ids: list[str],
) -> dict[str, Any]:
    return {
        "header": {
            "namespace": "Common",
            "name": "Action",
        },
        "payload": {
            "cardParam": {},
            "executeParam": {
                "achieveType": "INTENT",
                "actionResponse": True,
                "bundleName": "com.huawei.hmos.vassistant",
                "dimension": "",
                "executeMode": "background",
                "intentName": "CheckPlugInPrivilege",
                "intentParam": {
                    "checkIntentName": check_intent_name,
                    "permissionId": permission_ids,
                },
                "needUnlock": False,
                "permissionId": [],
                "timeOut": 5,
            },
            "needUploadResult": True,
            "pageControlRelated": False,
            "responses": [
                {
                    "displayText": "",
                    "resultCode": "",
                    "ttsText": "",
                }
            ],
        },
    }


def ensure_plugin_privilege_granted(
    check_intent_name: str,
    outputs: dict[str, Any],
) -> None:
    """Reject explicit privilege denial and device-side error results."""
    if not isinstance(outputs, dict):
        raise RuntimeError(
            f"Plugin privilege check returned an invalid result: {check_intent_name}"
        )

    for field_name in ("authorized", "granted"):
        if field_name not in outputs:
            continue
        value = outputs[field_name]
        normalized = str(value).strip().lower()
        if value is False or value == 0 or normalized in {
            "false",
            "0",
            "denied",
            "deny",
            "no",
        }:
            raise RuntimeError(
                f"Plugin privilege was denied for intent: {check_intent_name}"
            )

    raise_if_device_error(
        outputs,
        f"Plugin privilege check failed for intent {check_intent_name}",
    )


async def execute_plugin_privilege_check(
    check_intent_name: str,
) -> dict[str, Any]:
    """Execute the OpenClaw-compatible privilege command."""
    permission_ids = INTENT_PERMISSION_MAP.get(check_intent_name)
    if permission_ids is None:
        raise ValueError(f"Unsupported device intent: {check_intent_name}")

    command = build_check_plugin_privilege_command(
        check_intent_name=check_intent_name,
        permission_ids=permission_ids,
    )
    logger.info(
        "[CRON_DEVICE] phase=PRIVILEGE_CHECK_BEGIN intent_name=%s",
        check_intent_name,
    )
    try:
        outputs = await execute_device_command(
            "CheckPlugInPrivilege",
            command,
            timeout=60.0,
        )
    except asyncio.TimeoutError as exc:
        raise RuntimeError(
            f"Plugin privilege check timed out (60 seconds), intentName: {check_intent_name}"
        ) from exc
    logger.info(
        "[CRON_DEVICE] phase=PRIVILEGE_CHECK_DONE intent_name=%s "
        "retErrCode=%r errMsg=%r authorized=%r granted=%r outputs=%r",
        check_intent_name,
        outputs.get("retErrCode"),
        outputs.get("errMsg"),
        outputs.get("authorized"),
        outputs.get("granted"),
        outputs,
    )
    return outputs


@tool(
    name="check_plugin_privilege",
    description=(
        "Scheduled-task privilege check tool."
        "〖Usage scenario〗Use only when creating a scheduled task; calling it in any other scenario is "
        "strictly forbidden. When a scheduled task is recognized to require the user's device-side tools, "
        "you must call this tool to check privileges."
        "〖Prerequisite〗Before calling this tool, you must confirm that the tools mentioned in the user's "
        "scheduled task exist in the list of tools available to the current model. If the current tool list "
        "contains no tool definition matching the user's request, do not call this tool; instead, directly "
        "tell the user that the current device does not support that feature."
        "〖Supported intent names and their privileges〗"
        "GetCurrentLocation (get the user's location), "
        "SearchCalendarEvent (search the user's calendar events), "
        "CreateCalendarEvent (create a calendar event), "
        "DeleteCalendarEvent (delete a calendar event), "
        "ModifyCalendarEvent (modify a calendar event), "
        "SearchNote (search the user's notes), "
        "CreateNote (create a note), "
        "ModifyNote (modify a note), "
        "SearchContactLocal (search the user's contacts), "
        "SearchPhotoVideo (search the user's gallery photos or videos), "
        "SaveMediaToGallery (save images/videos to the gallery), "
        "SearchFile (search files in the user's file manager), "
        "SaveFileToFileManager (save a file to the file manager), "
        "SearchAlarm (search alarms), "
        "CreateAlarm (create an alarm), "
        "ModifyAlarm (modify an alarm), "
        "DeleteAlarm (delete an alarm), "
        "SearchMessage (search SMS messages), "
        "SendShortMessage (send an SMS), "
        "StartCall (make a phone call)."
        "〖Multiple calls〗If the user's scheduled-task instruction involves multiple device-side tools, call "
        "this tool once for each tool, in turn, to check its privilege. If a call times out or fails, retry "
        "at most once."
        "〖Reply constraint〗If the tool returns that authorization was not granted or any other error, just "
        "fully describe the missing authorization or the error content; do not proactively provide the user "
        "with solutions."
        "〖Usage constraint 1〗Whenever a scheduled task is being created and it involves the use of device "
        "plugins, this tool must be called to check privileges."
        "〖Usage constraint 2〗During scheduled-task execution, calling this tool is forbidden; this tool is "
        "only called on demand when creating a scheduled task."
    ),
)
async def check_plugin_privilege(checkIntentName: str) -> dict[str, Any]:
    permission_ids = INTENT_PERMISSION_MAP.get(checkIntentName)
    if permission_ids is None:
        return {
            "content": [
                {
                    "type": "text",
                    "text": (
                        f"Unsupported tool intent name: {checkIntentName}. "
                        "Please confirm the intent name is in the supported list."
                    ),
                }
            ]
        }

    outputs = await execute_plugin_privilege_check(checkIntentName)

    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(outputs, ensure_ascii=False),
            }
        ]
    }
