"""Helpers for tests of the Helm chart."""

from pathlib import Path
import shutil
import subprocess

import yaml

CHART = Path(__file__).parents[1] / "deployment"
HELM = shutil.which("helm")


def render(*args: str) -> list[dict]:
    """Render the chart with `helm template` arguments."""
    assert HELM is not None
    rendered = subprocess.run(  # noqa: S603
        [HELM, "template", "cloud-registry", str(CHART), *args],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return [doc for doc in yaml.safe_load_all(rendered) if doc]
