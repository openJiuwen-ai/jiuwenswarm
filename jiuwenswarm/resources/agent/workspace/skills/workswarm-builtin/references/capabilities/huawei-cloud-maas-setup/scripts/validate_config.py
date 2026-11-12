#!/usr/bin/env python3
"""验证 jiuwenswarm 中的华为云 MaaS 配置是否有效。

用法:
    python validate_config.py              # 验证配置并测试连通性
    python validate_config.py --check-only # 仅检查配置是否存在，不测试连通性
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import urllib.request
from urllib.parse import urljoin

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')


def check_config_exists() -> tuple[bool, dict[str, str]]:
    """检查配置中是否存在有效的 API 配置。返回 (是否有效, 配置字典)。"""
    try:
        from jiuwenswarm.common.config import get_config
    except ImportError:
        logging.error("错误: 无法导入 jiuwenswarm 模块，请确保在 jiuwenswarm 环境中运行")
        return False, {}

    config = get_config()
    models = config.get("models") or {}
    defaults = models.get("defaults") if isinstance(models, dict) else None

    if not isinstance(defaults, list) or not defaults:
        return False, {}

    # 查找默认模型配置
    default_entry = None
    for entry in defaults:
        if isinstance(entry, dict) and entry.get("is_default"):
            default_entry = entry
            break
    if default_entry is None:
        default_entry = defaults[0]

    mcc = default_entry.get("model_client_config", {}) if isinstance(default_entry, dict) else {}
    api_base = mcc.get("api_base", "")
    api_key = mcc.get("api_key", "")
    model_name = mcc.get("model_name", "")
    provider = mcc.get("client_provider", "")

    is_valid = bool(api_base and api_key and model_name)
    return is_valid, {
        "api_base": api_base,
        "api_key": api_key,
        "model_name": model_name,
        "model_provider": provider,
    }


def test_connectivity(api_base: str, api_key: str, model_name: str) -> tuple[bool, str]:
    """向华为云 MaaS API 发送测试请求，验证连通性。"""
    # 构建请求 URL（OpenAI 兼容接口）
    url = urljoin(api_base.rstrip("/") + "/", "chat/completions")

    payload = json.dumps(
        {
            "model": model_name,
            "messages": [
                {"role": "user", "content": "Hi"},
            ],
            "max_tokens": 5,
        }
    ).encode("utf-8")

    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read().decode("utf-8")
            data = json.loads(body)
            # 检查是否有 choices
            if "choices" in data:
                return True, "验证成功：API 连通正常"
            return False, f"API 返回异常: {body[:200]}"
    except urllib.error.HTTPError as e:
        error_body = ""
        try:
            error_body = e.read().decode("utf-8")[:300]
        except Exception as exc:
            logging.warning(f"读取错误响应体失败: {exc}")
        if e.code == 401:
            return False, "认证失败：API Key 无效或尚未生效（创建后需等待几分钟）"
        if e.code == 404:
            return False, f"接口不存在：请检查 API 地址是否正确。URL: {url}"
        return False, f"HTTP {e.code}: {error_body}"
    except urllib.error.URLError as e:
        return False, f"网络错误: {e.reason}"
    except Exception as e:
        return False, f"未知错误: {e}"


def main() -> int:
    parser = argparse.ArgumentParser(description="验证华为云 MaaS 配置")
    parser.add_argument("--check-only", action="store_true", help="仅检查配置是否存在，不测试连通性")
    args = parser.parse_args()

    is_valid, config = check_config_exists()

    if not is_valid:
        logging.error("[FAIL] 未找到有效的 API 配置")
        logging.info("  请先运行 config_writer.py 写入配置，或通过配置面板手动填写。")
        return 1

    masked_key = (
        config["api_key"][:4] + "****" + config["api_key"][-4:]
        if len(config["api_key"]) > 8
        else "****"
    )
    logging.info("[OK] 配置已存在:")
    logging.info(f"  API 地址: {config['api_base']}")
    logging.info(f"  API Key:  {masked_key}")
    logging.info(f"  模型名称: {config['model_name']}")
    logging.info(f"  接入方式: {config['model_provider']}")

    if args.check_only:
        return 0

    logging.info("\n正在测试 API 连通性...")
    success, message = test_connectivity(
        config["api_base"], config["api_key"], config["model_name"]
    )

    if success:
        logging.info(f"[OK] {message}")
        return 0
    else:
        logging.error(f"[FAIL] {message}")
        logging.info("\n提示: API Key 创建后可能需要几分钟生效，请稍后重试。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
