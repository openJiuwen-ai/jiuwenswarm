# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""
Celia find skills search - Helper Script

This is a helper script for batch generation or CLI usage.
The main skill workflow is defined in SKILL.md and executed by Claude.

Usage:
    python search.py "query"
"""
import os
import uuid
import json
import argparse
import requests
import hmac
import hashlib
import base64
from datetime import datetime
import logging
from jiuwenclaw.agentserver.open_ability_utils import get_oa_auth_headers
from jiuwenclaw.sandbox import sandbox_routing_enabled


logger = logging.getLogger(__name__)


def generate_auth_headers(env_config):
    ts = datetime.utcnow().strftime("%Y%m%d%H%M%S%f")[:-3]
    appid = env_config["appid"]
    key = env_config["key"]
    
    message = ts + appid
    sign = hmac.new(key.encode(), message.encode(), hashlib.sha256).digest()
    sign = base64.b64encode(sign).decode()
    
    return {
        "x-access-key": appid,
        "x-ts": ts,
        "x-sign": sign
    }


def format_skill_data(raw_skills):
    """
    格式化技能数据，添加安装状态标识，调整字段名称
    """
    formatted_skills = []

    for skill in raw_skills:
        # 检查是否已安装
        status = "未安装"

        # 构建格式化后的技能字典
        formatted_skill = {
            "skillId": skill.get("skillId", ""),
            "skillName": skill.get("skillName", ""),
            "skillDesc": skill.get("skillDesc", ""),
            "downloadPath": skill.get("packUrl", ""),  # 将packUrl重命名为downloadPath
            "status": status
        }

        formatted_skills.append(formatted_skill)

    return formatted_skills


def search_skills(query):
    if sandbox_routing_enabled():
        api_url = os.getenv("SKILL_SEARCH_URL")
        auth_headers = get_oa_auth_headers()
    else:
        raw = os.getenv("SKILL_SEARCH_ENV_CONFIG", "")
        if not raw:
            logger.error("[SkillSearch] 环境变量 SKILL_SEARCH_ENV_CONFIG 未设置")
            return None, 0
        try:
            env_config = json.loads(raw)
        except json.JSONDecodeError as e:
            logger.error("[SkillSearch] SKILL_SEARCH_ENV_CONFIG JSON 解析失败: %s", e)
            return None, 0
        api_url = env_config["url"]
        auth_headers = generate_auth_headers(env_config)
    trace_id = str(uuid.uuid4())

    headers = {
        'Content-Type': 'application/json',
        'x-skill-id': 'celia_find_skills',
        'x-hag-trace-id': trace_id,
        'x-request-from': 'openclaw',
    }
    headers.update(auth_headers)

    payload = {
        "query": query,
        "caller": "jiuwen"
    }

    try:
        # verify = env_config.get("verify", True)
        response = requests.post(api_url, headers=headers, json=payload, timeout=30, verify=False)
        response.raise_for_status()
        try:
            response_data = response.json()
        except json.JSONDecodeError as e:
            logger.error("[SkillSearch] JSON解析失败: %s, 原始内容: %s", e, response.text)
            return None, 0

        if response_data.get("errorCode") == "0" and "content" in response_data and "skills" in response_data["content"]:
            formatted_data = response_data["content"]["skills"]
            formatted_data = [
                skill for skill in formatted_data
                if skill.get("skillId") not in ("find-skills", "skill-creator")
            ]
            total = response_data["content"]["total"]
            logger.debug("[SkillSearch] skill列表: %s", json.dumps(formatted_data, indent=2, ensure_ascii=False))
            return formatted_data, total
        else:
            logger.warning("[SkillSearch] 响应数据格式异常，未找到skill列表, 原始响应: %s",
                           json.dumps(response_data, indent=2, ensure_ascii=False))
            return None, 0

    except requests.exceptions.Timeout:
        logger.error("[SkillSearch] Request timed out (30s)")
        return None, 0
    except requests.exceptions.RequestException as e:
        logger.error("[SkillSearch] HTTP请求异常: %s", e)
        return None, 0
    except Exception as e:
        logger.error("[SkillSearch] 未知错误: %s", e)
        return None, 0


def parse_args():
    """解析命令行参数"""
    parser = argparse.ArgumentParser(description="查找技能")
    parser.add_argument("--query", required=True, help="查找技能的关键词")
    return parser.parse_args()


def main():
    """主函数"""
    args = parse_args()
    search_skills(query=args.query)


if __name__ == '__main__':
    main()
