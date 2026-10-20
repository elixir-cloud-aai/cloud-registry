"""Helpers for tests of the Helm chart."""

from pathlib import Path
import shutil
import subprocess

import yaml

CHART = Path(__file__).parents[1] / "deployment"
HELM = shutil.which("helm")
# Name of the Secret with the MongoDB credentials (required, as authentication
# is enabled by default); override it with `--set` to test without
MONGODB_SECRET = "mongodb-credentials"  # noqa: S105 (a Secret's name, no credential)


def render(*args: str) -> list[dict]:
    """Render the chart with `helm template` arguments."""
    assert HELM is not None
    rendered = subprocess.run(  # noqa: S603
        [
            HELM,
            "template",
            "cloud-registry",
            str(CHART),
            "--set",
            f"mongodb.auth.existingSecret={MONGODB_SECRET}",
            *args,
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return [doc for doc in yaml.safe_load_all(rendered) if doc]
