# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Plugin-owned preferences for the two transition sound presets."""

SOUND_STORE_KEY = "transition_sound_preferences"
SOUND_ASSETS = {
    "whoosh": ("sounds/whoosh-compact.wav", "sounds/whoosh-workspace.wav"),
    "soft": ("sounds/soft-compact.wav", "sounds/soft-workspace.wav"),
}


def sound_preferences(saved: object = None) -> dict:
    """Read exact saved values; only an absent record uses defaults."""
    if saved is None:
        return {"schemaVersion": 1, "enabled": True, "preset": "whoosh", "volume": 25}
    if not isinstance(saved, dict) or set(saved) != {"schemaVersion", "enabled", "preset", "volume"}:
        raise ValueError("Invalid transition sound preferences")
    if type(saved["schemaVersion"]) is not int or saved["schemaVersion"] != 1:
        raise ValueError("Unsupported transition sound preferences")
    if type(saved["enabled"]) is not bool or not isinstance(saved["preset"], str) or saved["preset"] not in SOUND_ASSETS:
        raise ValueError("Invalid transition sound selection")
    if type(saved["volume"]) is not int or not 0 <= saved["volume"] <= 100 or saved["volume"] % 5:
        raise ValueError("Invalid transition sound volume")
    return dict(saved)


def changed_sound(values: dict, key: str, data: dict) -> dict:
    """Validate one global setting before persisting it."""
    if key not in {"enabled", "preset", "volume"}:
        raise ValueError("Unknown transition sound setting")
    value = data["checked"] if key == "enabled" else data["value"]
    return sound_preferences({**values, key: value})
