#!/usr/bin/env python
# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Profile storage for browser runtime drivers."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class BrowserProfile:
    """Persisted browser profile metadata."""

    name: str
    driver_type: str = "remote"
    cdp_url: str = ""
    browser_binary: str = ""
    user_data_dir: str = ""
    debug_port: int = 0
    host: str = "127.0.0.1"
    extra_args: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "BrowserProfile":
        debug_port_raw = raw.get("debug_port")
        try:
            debug_port = int(debug_port_raw or 0)
        except (TypeError, ValueError):
            debug_port = 0
        return cls(
            name=str(raw.get("name") or "").strip(),
            driver_type=str(raw.get("driver_type") or "remote").strip().lower() or "remote",
            cdp_url=str(raw.get("cdp_url") or "").strip(),
            browser_binary=str(raw.get("browser_binary") or "").strip(),
            user_data_dir=str(raw.get("user_data_dir") or "").strip(),
            debug_port=debug_port,
            host=str(raw.get("host") or "127.0.0.1").strip() or "127.0.0.1",
            extra_args=[str(x) for x in (raw.get("extra_args") or []) if str(x).strip()],
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class BrowserProfileStore:
    """JSON-backed profile store with selected-profile tracking."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()
        self._profiles: Dict[str, BrowserProfile] = {}
        self._selected: str = ""
        self._load()

    def _load(self) -> None:
        self._profiles = {}
        self._selected = ""
        if not self.path.exists():
            return
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return

        if not isinstance(payload, dict):
            return

        self._selected = str(payload.get("selected_profile") or "").strip()
        for item in payload.get("profiles") or []:
            if not isinstance(item, dict):
                continue
            profile = BrowserProfile.from_dict(item)
            if not profile.name:
                continue
            self._profiles[profile.name] = profile

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "selected_profile": self._selected,
            "profiles": [profile.to_dict() for profile in sorted(self._profiles.values(), key=lambda p: p.name)],
        }
        self.path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def list_profiles(self) -> List[BrowserProfile]:
        return sorted(self._profiles.values(), key=lambda p: p.name)

    def get_profile(self, name: str) -> Optional[BrowserProfile]:
        key = (name or "").strip()
        if not key:
            return None
        return self._profiles.get(key)

    def upsert_profile(self, profile: BrowserProfile, *, select: bool = False) -> BrowserProfile:
        name = (profile.name or "").strip()
        if not name:
            raise ValueError("profile.name is required")
        profile.name = name
        profile.driver_type = (profile.driver_type or "remote").strip().lower() or "remote"
        self._profiles[name] = profile
        if select:
            self._selected = name
        elif self._selected and self._selected not in self._profiles:
            self._selected = ""
        self.save()
        return profile

    def remove_profile(self, name: str) -> bool:
        key = (name or "").strip()
        if not key or key not in self._profiles:
            return False
        del self._profiles[key]
        if self._selected == key:
            self._selected = ""
        self.save()
        return True

    def select_profile(self, name: str) -> BrowserProfile:
        key = (name or "").strip()
        profile = self._profiles.get(key)
        if profile is None:
            raise KeyError(f"profile not found: {key}")
        self._selected = key
        self.save()
        return profile

    def selected_name(self) -> str:
        return self._selected

    def selected_profile(self) -> Optional[BrowserProfile]:
        if not self._selected:
            return None
        return self._profiles.get(self._selected)
