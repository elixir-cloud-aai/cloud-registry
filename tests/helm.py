"""Helpers for tests of the Helm chart."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import yaml

CHART = Path(__file__).parents[1] / "deployment"
HELM = shutil.which("helm")
# Release rendered by default, and the names of its app and MongoDB resources
RELEASE = "cloud-registry"
APP = "cloud-registry"
MONGODB = "cloud-registry-mongodb"
# Name of the Secret with the MongoDB credentials, required as authentication
# is enabled by default
MONGODB_SECRET = "mongodb-credentials"  # noqa: S105 (a Secret's name, no credential)
# Group mapped to the role `admin`, required by default
ADMIN_GROUP = "test-admins"
# Audience of the app's tokens, required by default
AUDIENCE = "test-client"
# Templates added to a copy of the chart to render its notes into a ConfigMap,
# as `helm template` does not render notes (and `helm install --dry-run` needs
# a cluster)
NOTES_HELPER = '{{- define "notes-test" }}NOTES{{- end }}'
NOTES_CONFIGMAP = """apiVersion: v1
kind: ConfigMap
metadata:
  name: notes-test
data:
  notes: {{ include "notes-test" . | toJson }}
"""


def render(
    *args: str,
    mongodb_secret: str | None = MONGODB_SECRET,
    admin_group: str | None = ADMIN_GROUP,
    audience: str | None = AUDIENCE,
    release: str = RELEASE,
    chart: Path = CHART,
) -> list[dict]:
    """Render the chart with `helm template` arguments.

    Args:
        args: Arguments of `helm template`.
        mongodb_secret: Name of the Secret with the MongoDB credentials; not
            set if `None`.
        admin_group: Group mapped to the role `admin`; not set if `None`.
        audience: Audience of the app's tokens; not set if `None`.
        release: Name of the release.
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
    if audience is not None:
        args = (
            "--set-json",
            f"config.security.auth.audience={json.dumps([audience])}",
            *args,
        )
    if mongodb_secret is not None:
        args = ("--set", f"mongodb.auth.existingSecret={mongodb_secret}", *args)
    rendered = subprocess.run(  # noqa: S603
        [
            HELM,
            "template",
            release,
            str(chart),
            *args,
        ],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    return [doc for doc in yaml.safe_load_all(rendered) if doc]


def render_with(templates: dict[str, str], *args: str, **kwargs) -> list[dict]:
    """Render a copy of the chart with additional templates.

    Args:
        templates: Contents of the additional templates, by file name.
        args: Arguments of `helm template`.
        kwargs: Keyword arguments of `render()`, except `chart`.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        chart = Path(tmp_dir) / "chart"
        shutil.copytree(CHART, chart)
        for name, template in templates.items():
            (chart / "templates" / name).write_text(template)
        return render(*args, chart=chart, **kwargs)


def render_notes(*args: str, **kwargs) -> str:
    """Render the chart's notes (`templates/NOTES.txt`); takes the arguments
    of `render_with()`, except `templates`.
    """
    notes = (CHART / "templates" / "NOTES.txt").read_text()
    templates = {
        "_notes-test.tpl": NOTES_HELPER.replace("NOTES", notes),
        "notes-test.yaml": NOTES_CONFIGMAP,
    }
    docs = render_with(templates, *args, **kwargs)
    (configmap,) = [doc for doc in docs if doc["metadata"]["name"] == "notes-test"]
    return configmap["data"]["notes"]
