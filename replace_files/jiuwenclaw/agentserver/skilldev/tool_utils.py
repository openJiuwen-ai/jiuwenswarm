import json
import os
import sys
from pathlib import Path


def generate_tool_scripts_and_usage(tools: list[dict], save_path: Path):
    tool_usage_data = []
    tool_usage_file = os.path.join(str(save_path), "tool_usage.json")
    if os.path.exists(tool_usage_file):
        with open(tool_usage_file, "r", encoding="utf-8") as f:
            tool_usage_data = json.load(f)
    
    for tool in tools:
        tool_id = tool["toolId"]
        name = tool["name"]
        description = tool.get("description", "")
        protocol = tool.get("protocol", "REST")
        parameters = tool.get("parameters", {})

        if protocol == "REST":
            template = TOOL_CALL_REST_TEMPLATE
        else:
            template = TOOL_CALL_WS_TEMPLATE

        script_content = template.replace(
            "plugin_id = {plugin_id}", f'plugin_id = "{tool_id}"'
        ).replace(
            "action_name = {action_name}", f'action_name = "{name}"'
        )

        script_path = os.path.join(str(save_path), f"{name}.py")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(script_content)

        props = parameters.get("properties", {})
        required = parameters.get("required", [])

        param_list = []
        for param_name, param_info in props.items():
            param_list.append({
                "name": param_name,
                "type": param_info.get("type", "unknown"),
                "description": param_info.get("description", ""),
                "required": param_name in required
            })

        param_parts = []
        for p in param_list:
            param_parts.append(f"{p['name']}=<{p['type']}>")
        usage_method = f"python {name}.py --params {' '.join(param_parts)}" if param_parts else f"python {name}.py"

        usage_entry = {
            "tool_name": name,
            "script_path": script_path,
            "description": description,
            "parameters": param_list,
            "usage": usage_method,
        }

        tool_usage_data.append(usage_entry)
    with open(tool_usage_file, "w", encoding="utf-8") as f:
        json.dump(tool_usage_data, f, ensure_ascii=False, indent=2)
    return tool_usage_data


TOOL_CALL_REST_TEMPLATE = """
import requests
import json
from datetime import datetime, timezone
import hashlib
import hmac
import base64
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
claw_root = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(current_dir))))))
env_path = os.path.join(claw_root, "config", ".env")
load_dotenv(dotenv_path=env_path)

RESTFUL_URL = os.getenv("RESTFUL_URL")
SECRET_KEY = os.getenv("SECRET_KEY")

def generate_timestamp() -> str:
    "生成时间戳：yyyyMMddHHmmssSSS"
    now = datetime.now(timezone.utc)
    base_time = now.strftime("%Y%m%d%H%M%S")
    milliseconds = f"{now.microsecond // 1000:03d}"
    return base_time + milliseconds

def generate_signature(timestamp: str, access_key: str, secret_key: str) -> str:
    "生成签名"
    message = timestamp + access_key
    signature = hmac.new(
        secret_key.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    signature_bytes = bytes.fromhex(signature)
    return base64.b64encode(signature_bytes).decode('utf-8')

def call_action_executor_api(
    plugin_id: str, 
    action_name: str, 
    params: Dict[str, Any],
    session_id: str = "sessionId002",
    interaction_id: str = "interactionId001"
) -> Optional[requests.Response]:
    "调用动作执行器接口"
    url = RESTFUL_URL
    
    # 认证参数
    access_key = "10001"
    secret_key = SECRET_KEY
    
    # 生成时间戳和签名
    timestamp = generate_timestamp()
    signature = generate_signature(timestamp, access_key, secret_key)
    
    headers = {
        "Content-Type": "application/json",
        "x-access-key": access_key,
        "x-sign": signature,
        "x-ts": timestamp,
        "x-hag-trace-id": "rytest99527",
        "x-prd-pkg-name": "com.huawei.vassistant"
    }
    
    request_body = {
        "session": {
            "sessionId": session_id,
            "isNew": True,
            "interactionId": interaction_id
        },
        "endpoint": {
            "countryCode": "CN",
            "locale": "zh-CN",
            "device": {
                "deviceType": 0,
                "phoneType": "F810",
                "city": {
                    "province": "江苏",
                    "city": "南京"
                },
                "timezone": "",
                "sysVer": "8.0.0",
                "deviceId": "1231",
                "manufacturer": "HUAWEI",
                "sysDisplayVer": "1.1.1.1",
                "prdVer": "4.0.0.0",
                "ohosVersion": "",
                "net": "4G",
                "ohosApiVersion": ""
            }
        },
        "version": "V4",
        "actions": [
            {
                "actionSn": "1",
                "actionExecutorTask": {
                    "pluginId": plugin_id,
                    "realMachineTest": "1",
                    "content": params,
                    "actionName": action_name
                }
            }
        ],
        "utterance": {
            "original": ""
        }
    }
      
    try:
        response = requests.post(
            url,
            headers=headers,
            json=request_body,
            verify=False,
            timeout=30  # 添加超时设置
        )
        
        return response
        
    except requests.exceptions.SSLError as e:
        print(f"SSL错误: {e}")
        return None
    except requests.exceptions.Timeout as e:
        print(f"请求超时: {e}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"请求错误: {e}")
        return None

if __name__ == "__main__":
    import urllib3
    import argparse

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    parser = argparse.ArgumentParser(description="调用动作执行器接口")
    parser.add_argument("--params", nargs="*", default=[], help="动作参数，格式: key=value，可传多个，例如 --params movieName=731 test1=test1")
    args = parser.parse_args()

    params = {}
    for item in args.params:
        if "=" in item:
            k, v = item.split("=", 1)
            params[k] = v
        else:
            print(f"⚠️ 参数格式错误，已跳过: {item}（正确格式: key=value）")

    plugin_id = {plugin_id}
    action_name = {action_name}

    response = call_action_executor_api(plugin_id, action_name, params)

    if response and response.status_code == 200:
        print("✅ 接口调用成功！")
        try:
            result = response.json()
            print(f"返回数据: {json.dumps(result, indent=2, ensure_ascii=False)}")
        except:
            pass
    else:
        print("❌ 接口调用失败！")
"""

TOOL_CALL_WS_TEMPLATE = """
import requests
import json
from datetime import datetime, timezone
import hashlib
import hmac
import base64
from typing import Optional, Dict, Any
from dotenv import load_dotenv
import os

current_dir = os.path.dirname(os.path.abspath(__file__))
claw_root = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(current_dir))))))
env_path = os.path.join(claw_root, "config", ".env")
load_dotenv(dotenv_path=env_path)

WS_URL = os.getenv("WS_URL")
SECRET_KEY = os.getenv("SECRET_KEY")

def generate_timestamp() -> str:
    "生成时间戳：yyyyMMddHHmmssSSS"
    now = datetime.now(timezone.utc)
    base_time = now.strftime("%Y%m%d%H%M%S")
    milliseconds = f"{now.microsecond // 1000:03d}"
    return base_time + milliseconds

def generate_signature(timestamp: str, access_key: str, secret_key: str) -> str:
    "生成签名"
    message = timestamp + access_key
    signature = hmac.new(
        secret_key.encode('utf-8'),
        message.encode('utf-8'),
        hashlib.sha256
    ).hexdigest()
    signature_bytes = bytes.fromhex(signature)
    return base64.b64encode(signature_bytes).decode('utf-8')

def call_action_executor_api(
    plugin_id: str, 
    action_name: str, 
    params: Dict[str, Any],
    session_id: str = "sessionId002",
    interaction_id: str = "interactionId001"
) -> Optional[requests.Response]:
    "调用动作执行器接口"
    url = WS_URL
    
    # 认证参数
    access_key = "10001"
    secret_key = SECRET_KEY
    
    # 生成时间戳和签名
    timestamp = generate_timestamp()
    signature = generate_signature(timestamp, access_key, secret_key)
    
    headers = {
        "Content-Type": "application/json",
        "x-access-key": access_key,
        "x-sign": signature,
        "x-ts": timestamp,
        "x-hag-trace-id": "rytest99527",
        "x-prd-pkg-name": "com.huawei.vassistant"
    }
    
    request_body = {
        "session": {
            "sessionId": session_id,
            "isNew": True,
            "interactionId": interaction_id
        },
        "endpoint": {
            "countryCode": "CN",
            "locale": "zh-CN",
            "device": {
                "deviceType": 0,
                "phoneType": "F810",
                "city": {
                    "province": "江苏",
                    "city": "南京"
                },
                "timezone": "",
                "sysVer": "8.0.0",
                "deviceId": "1231",
                "manufacturer": "HUAWEI",
                "sysDisplayVer": "1.1.1.1",
                "prdVer": "4.0.0.0",
                "ohosVersion": "",
                "net": "4G",
                "ohosApiVersion": ""
            }
        },
        "version": "V4",
        "actions": [
            {
                "actionSn": "1",
                "actionExecutorTask": {
                    "pluginId": plugin_id,
                    "realMachineTest": "1",
                    "content": params,
                    "actionName": action_name
                }
            }
        ],
        "utterance": {
            "original": ""
        }
    }
      
    try:
        response = requests.post(
            url,
            headers=headers,
            json=request_body,
            verify=False,
            timeout=30  # 添加超时设置
        )
        
        return response
        
    except requests.exceptions.SSLError as e:
        print(f"SSL错误: {e}")
        return None
    except requests.exceptions.Timeout as e:
        print(f"请求超时: {e}")
        return None
    except requests.exceptions.RequestException as e:
        print(f"请求错误: {e}")
        return None

if __name__ == "__main__":
    import urllib3
    import argparse

    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    parser = argparse.ArgumentParser(description="调用动作执行器接口")
    parser.add_argument("--params", nargs="*", default=[], help="动作参数，格式: key=value，可传多个，例如 --params movieName=731 test1=test1")
    args = parser.parse_args()

    params = {}
    for item in args.params:
        if "=" in item:
            k, v = item.split("=", 1)
            params[k] = v
        else:
            print(f"⚠️ 参数格式错误，已跳过: {item}（正确格式: key=value）")

    plugin_id = {plugin_id}
    action_name = {action_name}

    response = call_action_executor_api(plugin_id, action_name, params)

    if response and response.status_code == 200:
        print("✅ 接口调用成功！")
        try:
            result = response.json()
            print(f"返回数据: {json.dumps(result, indent=2, ensure_ascii=False)}")
        except:
            pass
    else:
        print("❌ 接口调用失败！")
"""
