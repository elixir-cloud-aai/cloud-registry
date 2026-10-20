"""Helpers for tests of the Helm chart."""

import json
from pathlib import Path
import shutil
import subprocess

import yaml

CHART = Path(__file__).parents[1] / "deployment"
HELM = shutil.which("helm")
# Name of the Secret with the MongoDB credentials, required as authentication
# is enabled by default
MONGODB_SECRET = "mongodb-credentials"  # noqa: S105 (a Secret's name, no credential)
# Group mapped to the role `admin`, required by default
ADMIN_GROUP = "test-admins"


def render(
    *args: str,
    mongodb_secret: str | None = MONGODB_SECRET,
    admin_group: str | None = ADMIN_GROUP,
    chart: Path = CHART,
) -> list[dict]:
    """Render the chart with `helm template` arguments.

    Args:
        args: Arguments of `helm template`.
        mongodb_secret: Name of the Secret with the MongoDB credentials; not
            set if `None`.
        admin_group: Group mapped to the role `admin`; not set if `None`.
        chart: Chart directory or packaged chart.
    """
    assert HELM is not None
    if admin_group is not None:
        mapping = json.dumps({admin_group: "admin"})
        args = (
            "--set-json",
            f"config.security.access_control.group_mapping={mapping}",
            *args,
        )
    if mongodb_secret is not None:
        args = ("--set", f"mongodb.auth.existingSecret={mongodb_secret}", *args)
    rendered = subprocess.run(  # noqa: S603
        [
            HELM,
            "template",
            "cloud-registry",
            str(chart),
            *args,
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return [doc for doc in yaml.safe_load_all(rendered) if doc]
