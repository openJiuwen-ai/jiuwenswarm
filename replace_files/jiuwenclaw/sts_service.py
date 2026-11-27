# -*- coding: utf-8 -*-
# Copyright (c) Huawei Technologies Co., Ltd. 2025-2025. All rights reserved.
import os
import configparser

from pystssdk import sts_api
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HTTP_WS_AUTH_STS_ENV = os.environ['HTTP_WS_AUTH_STS_SK_TAG']
HAG_OSMS_STS_ENV = os.environ['HAG_OSMS_STS_SK_TAG']
DCS_PASSWD_SK = os.environ['DCS_PASSWD_SK_TAG']
HAG_OSMS_STS_SK = os.environ['HAG_OSMS_STS_SK_TAG']

# 敏感配置坐标项
SERVICE_ACCOUNT_CLIENT_ID = os.getenv('SERVICE_ACCOUNT_CLIENT_ID')
SERVICE_ACCOUNT_TAG = os.getenv('SERVICE_ACCOUNT_TAG')
SERVICE_ACCOUNT_KEYID = os.getenv('SERVICE_ACCOUNT_KEYID')
JWT_AUD_URI = os.getenv('JWT_AUD_URI')
LOCAL_FLAG = True


class StsConfig:
    def __init__(self, config_path, config_name_list):
        if not config_path:
            raise RuntimeError("config_path must be set")
        if not config_name_list:
            raise RuntimeError("config_name_list must be set")
        from jiuwenclaw.sandbox.sandbox_routing_settings import sandbox_routing_enabled
        if sandbox_routing_enabled():
            # 沙箱路由场景下，需要额外加载STS敏感配置，获取privateKey，用于沙箱服务接口鉴权jwt生成
            config_name_list.append(self.sensitive_config)
        self.config_path = config_path
        self.config_name_list = config_name_list
        if LOCAL_FLAG:
            sts_api.init(config_path=self.config_path)
        else:
            sts_api.init(service="HAG", micro_service=os.environ['STS_MICROSERVICE'],
                         sts_server_domain=os.environ['STS_SERVICE_DOMAIN'])
        self.encrypted_configs, self.missing_configs = sts_api.fetch_sensitive_configs(self.config_name_list)
        logger.info("[STS] StsConfig init: config_path=%s, fetched %d configs, missing=%s",
                    self.config_path, len(self.encrypted_configs), self.missing_configs)

    @property
    def sensitive_config(self):
        """拼接STS敏感配置坐标"""
        for value in [SERVICE_ACCOUNT_CLIENT_ID, SERVICE_ACCOUNT_TAG, SERVICE_ACCOUNT_KEYID]:
            if not value or (isinstance(value, str) and value.strip() == ""):
                raise ValueError(f"STS 敏感配置不完整：环境变量 '{value}' 不能为空。")
        return (f"Credential/IAM/Service/HAG/{SERVICE_ACCOUNT_CLIENT_ID}:crsservice"
                f"/{SERVICE_ACCOUNT_TAG}/keyId/{SERVICE_ACCOUNT_KEYID}")

    def get_sts_config(self, config_name):
        """
        config_name:敏感项ID
        """
        logger.info("[STS] get_sts_config: config_name=%s", config_name)
        logger.info("[STS] encrypted_configs keys=%s", list(self.encrypted_configs.keys()))
        logger.info("[STS] config_name in encrypted_configs=%s", config_name in self.encrypted_configs)

        decrypt_value = sts_api.decrypt_sensitive_config(
            self.encrypted_configs[config_name]
        )
        return decrypt_value


# 新增配置项时，都需要在此处刷新。
env_config_list = [f"MicroService/HAG/JiuwenSkillCreator/http_ws_auth_sk/{HTTP_WS_AUTH_STS_ENV}",
                   f"MicroService/HAG/JiuwenSkillCreator/obs_transfer_hag_osms_sk/{HAG_OSMS_STS_ENV}",
                   f"MicroService/HAG/JiuwenSkillCreator/dcs_passwd_sk/{DCS_PASSWD_SK}",
                   f"MicroService/HAG/JiuwenSkillCreator/dcs_passwd_sk/{HAG_OSMS_STS_SK}"]

if LOCAL_FLAG:
    env_config_path = os.environ['STS_INI_CONFIG_PATH']
    logger.info("[STS] LOCAL_FLAG=True, env_config_path=%s", env_config_path)
    # 修改文件
    # 读取现有配置
    config = configparser.ConfigParser()
    config.read(env_config_path, encoding='utf-8')  # 替换为你的文件路径

    # 在 [sts] 节末尾追加键值对
    if not config.has_section('sts'):
        config.add_section('sts')

    config.set('sts', 'server_domain', os.environ['STS_SERVICE_DOMAIN'])

    # 保存文件
    with open(env_config_path, 'w', encoding='utf-8') as f:
        config.write(f)
else:
    env_config_path = os.path.join(PROJECT_ROOT, "config/sts/STS_dev_remote.ini")

sts_config = StsConfig(env_config_path, env_config_list)
