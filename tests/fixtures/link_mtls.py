"""Temporary TLS fixtures built from the production issuer; not an installer."""

import json
import os
import shutil
import tempfile

from openjiuwen_runtime.foundation.security.link_certificate_bundle import (
    issue_bundle,
    materialize_role,
)
from openjiuwen_runtime.foundation.security.link_profile import LinkProfileError


def atomic_json(path, value):
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=".fixture-")
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def provision(
    output,
    *,
    mtls_deployment_id,
    endpoints,
    mtls_binding_epoch=1,
    test_sans=None,
):
    if output.exists():
        raise LinkProfileError("output already exists")
    bundle = issue_bundle(
        mtls_deployment_id=mtls_deployment_id,
        endpoints=endpoints,
        mtls_binding_epoch=mtls_binding_epoch,
        sans=test_sans,
    )
    output.mkdir(mode=0o700)
    for role in bundle["materials"]:
        path = materialize_role(bundle, role, output / role)
        data = json.loads(path.read_text())
        # Socket tests have no service database. Persistence has separate tests.
        data.pop("persistence", None)
        atomic_json(path, data)
    return {
        "directory": str(output),
        "mtls_deployment_id": mtls_deployment_id,
        "mtls_binding_id": bundle["mtls_binding_id"],
        "mtls_binding_epoch": mtls_binding_epoch,
    }


def install(bundle, role):
    from openjiuwen_runtime.foundation.security import link_profile as profiles

    target = profiles.DEFAULT_IDENTITY_ROOT / role
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(bundle / role, target)
