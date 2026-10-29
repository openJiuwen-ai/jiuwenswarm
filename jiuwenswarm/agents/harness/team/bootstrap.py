# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Bootstrap helpers for JiuwenSwarm team integrations."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import threading
from typing import Iterator

from jiuwenswarm.common import utils
from jiuwenswarm.common.utils import (
    get_user_workspace_dir,
    migrate_team_skill_views,
)

# The SDK exposes process-wide path setters. Serialize their use together with
# migration and synchronous visibility consumers, including different owners.
_storage_lock = threading.RLock()


class TeamStoragePreparation:
    """Cache a successful migration for one owner and its current directories.

    The task-local Team home participates in the identity as well as the host
    home and shared library. Directory changes invalidate the cache; exceptions
    propagate without recording success so the next entry can retry.
    """

    def __init__(self) -> None:
        self._prepared_identity: tuple[Path, Path, Path] | None = None

    @contextmanager
    def activate(self) -> Iterator[None]:
        """Prepare once and keep SDK path bindings stable for a sync consumer."""
        from openjiuwen.agent_teams.paths import (
            configure_global_skills_dir,
            configure_openjiuwen_home,
            get_openjiuwen_home,
        )

        with _storage_lock:
            home = Path(get_user_workspace_dir()).resolve()
            configure_openjiuwen_home(home)
            library = Path(utils.configure_skill_library()).resolve()
            # Rebind even on cache hits: a different owner may have used the
            # process-wide SDK paths since this owner's last request.
            configure_global_skills_dir(library)
            identity = (home, Path(get_openjiuwen_home()).resolve(), library)
            if self._prepared_identity != identity:
                self._prepared_identity = None
                migrate_team_skill_views(library_dir=library, strict=True)
                self._prepared_identity = identity
            yield

    def prepare(self) -> None:
        """Finish migration before a Team consumer is allowed to proceed."""
        with self.activate():
            pass


_default_preparation = TeamStoragePreparation()


def prepare_team_storage(preparation: TeamStoragePreparation | None = None) -> None:
    """Prepare Team paths and legacy visibility before a Team consumer reads.

    Re-evaluate directory identities on entry but scan only after an identity
    change or a failed attempt. Owners may supply their own cache. This installs
    no Team runtime or organization adapters.
    """
    (preparation or _default_preparation).prepare()


def configure_agent_teams_home() -> None:
    """Activate Team storage and organization integrations on Team use."""
    prepare_team_storage()
    # Register installer only; Catalog/Launcher stay lazy until first org tool use.
    from jiuwenswarm.agents.harness.team.expert_org.wiring import (
        register_expert_adapter_installer,
    )
    from jiuwenswarm.agents.harness.team.summary_org.wiring import (
        register_summary_adapter_installer,
    )

    register_expert_adapter_installer()
    register_summary_adapter_installer()
    # Team leaders browse and spawn installed experts through this catalog.
    from jiuwenswarm.agents.harness.team.experts import install_expert_catalog

    install_expert_catalog()
