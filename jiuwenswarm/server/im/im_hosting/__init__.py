"""User-state IM hosting (poll, watermarks, digital-avatar enrollment)."""

from jiuwenswarm.server.im.im_hosting.paths import hosting_db_path, hosting_root_dir
from jiuwenswarm.server.im.im_hosting.service import HostingPollService, get_hosting_service

__all__ = [
    "HostingPollService",
    "get_hosting_service",
    "hosting_db_path",
    "hosting_root_dir",
]
