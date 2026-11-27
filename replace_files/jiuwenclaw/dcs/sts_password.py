from __future__ import annotations

import os
import logging

logger = logging.getLogger(__name__)


def get_sts_dcs_password() -> str | None:
    """Fetch DCS password from STS, returning None if STS is unavailable."""
    try:
        from jiuwenclaw.sts_service import sts_config

        env = os.environ.get("DCS_PASSWD_SK_TAG", "")
        config_name = f"MicroService/HAG/JiuwenSkillCreator/dcs_passwd_sk/{env}"

        logger.info(f"Fetching STS DCS password with config_name: {config_name}")
        password = sts_config.get_sts_config(config_name)

        if password:
            logger.info("STS DCS password retrieved successfully")
        else:
            logger.warning("STS DCS password is empty or None")

        return password
    except Exception as e:
        logger.error(f"Failed to get STS DCS password: {e}")
        return None
