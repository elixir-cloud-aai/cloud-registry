"""Helpers for tests of the Helm chart."""

from pathlib import Path
import shutil
import subprocess

import yaml

CHART = Path(__file__).parents[1] / "deployment"
HELM = shutil.which("helm")
# Name of the Secret with the MongoDB credentials, required as authentication
# is enabled by default
MONGODB_SECRET = "mongodb-credentials"  # noqa: S105 (a Secret's name, no credential)


def render(*args: str, mongodb_secret: str | None = MONGODB_SECRET) -> list[dict]:
    """Render the chart with `helm template` arguments.

    Args:
        args: Arguments of `helm template`.
        mongodb_secret: Name of the Secret with the MongoDB credentials; not
            set if `None`.
    """
    assert HELM is not None
    if mongodb_secret is not None:
        args = ("--set", f"mongodb.auth.existingSecret={mongodb_secret}", *args)
    rendered = subprocess.run(  # noqa: S603
        [
            HELM,
            "template",
            "cloud-registry",
            str(CHART),
            *args,
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return [doc for doc in yaml.safe_load_all(rendered) if doc]
