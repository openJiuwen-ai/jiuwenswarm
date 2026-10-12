"""Compatibility alias for cron persistence now owned by Agent Runtime."""

import sys

from jiuwenswarm.gateway.embedded.runtime.session.project_store import list_projects
from jiuwenswarm.runtime.cron.cron_job_mutations import set_cron_project_lister
from jiuwenswarm.runtime.cron import store as _implementation

set_cron_project_lister(list_projects)

sys.modules[__name__] = _implementation
