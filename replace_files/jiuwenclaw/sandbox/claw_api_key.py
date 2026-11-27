from __future__ import annotations

import binascii
import logging
import secrets

logger = logging.getLogger(__name__)

CLAW_APIKEY_LENGTH = 16


def get_claw_api_key() -> str:
    try:
        bytes_data = secrets.token_bytes(CLAW_APIKEY_LENGTH)
        hex_str = binascii.hexlify(bytes_data).decode("utf-8").upper()
        return "SK-" + hex_str
    except Exception as e:
        logger.error("reset genClawApiKey failed. %s", e)
        raise Exception("genClawApiKey failed.") from e
