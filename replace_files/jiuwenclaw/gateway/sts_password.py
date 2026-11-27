from __future__ import annotations

import os


def get_sts_dcs_password() -> str | None:
    """Fetch DCS password from STS, returning None if STS is unavailable."""
    try:
        from jiuwenclaw.sts_service import sts_config

        env = os.environ.get("HAG_OSMS_STS_SK_TAG", "")
        config_name = (
            f"MicroService/HAG/JiuwenSkillCreator/obs_transfer_hag_osms_sk/{env}"
        )
        return sts_config.get_sts_config(config_name)
    except Exception:
        return None
