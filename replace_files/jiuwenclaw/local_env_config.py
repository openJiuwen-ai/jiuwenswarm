import os
import sys
import logging

logger = logging.getLogger(__name__)

ENV_CONFIG_DICT = {}


# get方法agentserver和gateway都会使用
def get_local_config(name: str, default=None):
    global ENV_CONFIG_DICT
    # agentserver使用ENV_CONFIG_DICT存储
    if name in ENV_CONFIG_DICT:
        return ENV_CONFIG_DICT[name]
    # gateway使用os.env存储
    elif name in os.environ:
        # 需要使用解密能力
        return decrypt(name, os.environ[name])
    else:
        return default


# set方法只有agentserver使用
def set_local_config(name: str, value):
    global ENV_CONFIG_DICT
    if not value:
        ENV_CONFIG_DICT.pop(name, None)
        return
    ENV_CONFIG_DICT[name] = value


def decrypt(name, cipher):
    reg_mod = sys.modules.get("jiuwenclaw.extensions.registry")
    if reg_mod is not None and hasattr(reg_mod, "ExtensionRegistry"):
        try:
            crypto = reg_mod.ExtensionRegistry.get_instance().get_crypto_provider()
            is_need_decrypt = "api_key" in name.lower() or "token" in name.lower()
            if is_need_decrypt and crypto:
                return crypto.decrypt(cipher)
        except Exception as e:
            logger.warning(f"Decryption failed exception: {e}")
    return cipher


def encrypt(name, text):
    reg_mod = sys.modules.get("jiuwenclaw.extensions.registry")
    if reg_mod is not None and hasattr(reg_mod, "ExtensionRegistry"):
        try:
            crypto = reg_mod.ExtensionRegistry.get_instance().get_crypto_provider()
            is_need_decrypt = "api_key" in name.lower() or "token" in name.lower()
            if is_need_decrypt and crypto:
                return crypto.encrypt(text)
        except Exception as e:
            logger.warning(f"Encryption failed exception: {e}")
    return text