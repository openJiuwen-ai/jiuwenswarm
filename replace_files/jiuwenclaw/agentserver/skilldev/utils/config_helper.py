# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
# -*- coding: utf-8 -*-
import json
import os
from pathlib import Path
from typing import Any
import yaml
import logging

from .base_config import BaseConfig

logger = logging.getLogger(__name__)


class ConfigHelper:
    """
    配置类，支持多环境配置加载
    """

    def __init__(self, env: str = 'local'):
        """
        初始化配置类
        """
        if env not in ('local', 'cloud'):
            raise RuntimeError("env must be 'local' or 'cloud'")

        self.env = env
        self.config = {}
        self.config_file = self._get_config_file_path()
        self._load_config()

    def _get_config_file_path(self) -> str:
        """
        获取配置文件路径

        :return: 配置文件绝对路径
        """
        # 当前文件的路径
        current_file = Path(__file__).resolve()

        # 当前文件所在的文件夹
        current_dir = current_file.parent

        # 同级文件夹
        sibling_dir = current_dir.parent / "config"

        # 获取文件
        local_path = sibling_dir / "spec_records.yaml"
        remote_path = r"/opt/huawei/app/nuwa/service-config/normal/user-config.properties"
        if self.env == 'local':
            return os.path.join(BaseConfig.PROJECT_ROOT.parent.parent, local_path)
        else:
            return remote_path

    def _load_config(self):
        """加载配置文件"""

        def convert_bool_to_str(obj: Any) -> Any:
            """递归地将布尔值转换为字符串"""
            if isinstance(obj, dict):
                return {k: convert_bool_to_str(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [convert_bool_to_str(item) for item in obj]
            elif isinstance(obj, bool):
                if obj is True:
                    return "true"
                return "false"
            else:
                return obj

        try:
            with open(self.config_file, 'r', encoding='utf-8') as f:
                if self.env == 'local':
                    self.config = convert_bool_to_str(yaml.safe_load(f))
                    logger.info(f'加载环境：{self.get("ENVIRONMENT")}')
                else:
                    self.config = json.load(f)
                    logger.info(f'加载环境：{self.get("ENVIRONMENT")}')

        except json.JSONDecodeError as e:
            logger.warning(f"JSON 格式错误: {e.msg}", e.doc, e.pos)
        except Exception as e:
            logger.error(f"读取配置文件失败，类型：{type(e).__name__}")

    def get(self, key: str) -> Any:
        """
        获取配置值
        """
        if key not in self.config:
            return ""
        return self.config[key]

    def __getitem__(self, key: str) -> Any:
        """
        通过[]操作符获取配置值

        :param key: 配置键
        :return: 配置值
        :raises KeyError: 当配置不存在时
        """
        value = self.get(key)
        if value is None:
            logger.error(f"配置项 '{key}' 不存在")
            raise RuntimeError(f"config key does not exist: {key}")
        return value

    def __contains__(self, key: str) -> bool:
        """
        检查配置项是否存在

        :param key: 配置键
        :return: 是否存在
        """
        keys = key.split('.')
        value = self.config

        try:
            for k in keys:
                if isinstance(value, dict) and k in value:
                    value = value[k]
                else:
                    return False
            return True
        except Exception:
            return False
