"""Tests for the images, the MongoDB deployment and the probes of the Helm
chart.
"""

import re
import subprocess

import pytest
import yaml

from tests.helm import CHART, HELM, MONGODB_SECRET, render
from tests.test_config import CONFIG_FILE

NO_AUTH = ("--set", "mongodb.auth.enabled=false")
PROBES = ("startupProbe", "readinessProbe", "livenessProbe")

pytestmark = pytest.mark.skipif(HELM is None, reason="helm not installed")


def _render(*args: str, **kwargs) -> dict:
    """Render the chart and index the documents by kind and name."""
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in render("--set", "host_name=x", *args, **kwargs)
    }


def _container(docs: dict, name: str) -> dict:
    """Get the container of a Deployment."""
    return docs[("Deployment", name)]["spec"]["template"]["spec"]["containers"][0]


def _secret_refs(container: dict) -> dict:
    """Map environment variables to the Secret keys they are taken from."""
    return {
        var["name"]: (
            var["valueFrom"]["secretKeyRef"]["name"],
            var["valueFrom"]["secretKeyRef"]["key"],
        )
        for var in container.get("env", [])
        if "secretKeyRef" in var.get("valueFrom", {})
    }


def _literal_env(container: dict) -> set:
    """Get the names of environment variables with literal values."""
    return {var["name"] for var in container.get("env", []) if "valueFrom" not in var}


def _startup_window(container: dict) -> int:
    """Get the seconds a container may take to start before it is restarted."""
    probe = container["startupProbe"]
    return probe.get("initialDelaySeconds", 0) + (
        probe["periodSeconds"] * probe["failureThreshold"]
    )


def test_config_change_restarts_app():
    """Changing the config (e.g., the service info) restarts the app."""

    def checksum(*args: str) -> str:
        template = _render(*args)[("Deployment", "cloud-registry")]["spec"]["template"]
        return template["metadata"]["annotations"]["checksum/config"]

    changed = ("--set", "config.custom.endpoints.service_info.environment=prod")
    assert checksum() != checksum(*changed)


def _app_image(*args: str, **kwargs) -> str:
    """Render the chart and get the app image."""
    return _container(_render(*args, **kwargs), "cloud-registry")["image"]


def test_image_defaults_to_app_version():
    """Without a tag, the image of the chart's `appVersion` is deployed (`dev`
    for the chart in the repository).
    """
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    assert chart["appVersion"] == "dev"
    assert _app_image() == "elixircloud/cloud-registry:dev"


def test_image_of_packaged_chart(tmp_path):
    """Released charts deploy the image of their version, as set by the
    release workflow.
    """
    assert HELM is not None
    subprocess.run(  # noqa: S603
        [HELM, "package", str(CHART), "--version", "0.3.0"]
        + ["--app-version", "0.3.0", "-d", str(tmp_path)],
        capture_output=True,
        check=True,
    )
    package = tmp_path / "cloud-registry-0.3.0.tgz"
    assert _app_image(chart=package) == "elixircloud/cloud-registry:0.3.0"


@pytest.mark.parametrize(
    ("args", "image"),
    [
        (("--set", "cloud_registry.tag=sha-abc"), "elixircloud/cloud-registry:sha-abc"),
        (
            ("--set", "cloud_registry.image=registry.example.org:5000/cr"),
            "registry.example.org:5000/cr:dev",
        ),
        # Image references with a tag or digest, as in earlier chart versions
        (
            ("--set", "cloud_registry.image=elixircloud/cloud-registry:20261020"),
            "elixircloud/cloud-registry:20261020",
        ),
        (
            ("--set", f"cloud_registry.image=cr@sha256:{'0' * 64}"),
            f"cr@sha256:{'0' * 64}",
        ),
    ],
)
def test_image_set(args, image):
    """The image and its tag can be set."""
    assert _app_image(*args) == image


def test_image_tag_conflict():
    """A tag cannot be set for an image that has one."""
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render(
            "--set",
            "cloud_registry.image=elixircloud/cloud-registry:0.3.0",
            "--set",
            "cloud_registry.tag=dev",
        )
    assert "cloud_registry.tag must not be set" in exc.value.stderr


def test_mongodb_pinned_and_recreated():
    """MongoDB is pinned to a minor version and replaced, not rolled."""
    docs = _render()
    image = _container(docs, "mongodb")["image"]
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert image == values["mongodb"]["image"]
    assert re.fullmatch(r".+:\d+\.\d+", image)
    assert docs[("Deployment", "mongodb")]["spec"]["strategy"] == {"type": "Recreate"}


def test_probes():
    """App and MongoDB have probes on their ports."""
    docs = _render()
    app = _container(docs, "cloud-registry")
    for probe in PROBES:
        assert app[probe]["httpGet"] == {"path": "/ga4gh/registry/v1/ui/", "port": 8080}
    mongodb = _container(docs, "mongodb")
    for probe in PROBES:
        assert mongodb[probe]["tcpSocket"] == {"port": 27017}


def test_startup_windows():
    """App and MongoDB may take 5 and 30 minutes to start, respectively."""
    docs = _render()
    app = _container(docs, "cloud-registry")
    mongodb = _container(docs, "mongodb")
    assert _startup_window(app) == 5 * 60
    assert _startup_window(mongodb) == 30 * 60
    for container in (app, mongodb):
        assert "initialDelaySeconds" not in container["livenessProbe"]


def test_probe_path():
    """The path of the app's probes can be changed."""
    docs = _render("--set", "cloud_registry.probePath=/health")
    app = _container(docs, "cloud-registry")
    for probe in PROBES:
        assert app[probe]["httpGet"]["path"] == "/health"


@pytest.mark.parametrize("path", ["health", "null"])
def test_probe_path_invalid(path):
    """Probe paths must be set and absolute."""
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render("--set", f"cloud_registry.probePath={path}")
    assert "probePath" in exc.value.stderr


def _init_script_volumes(docs: dict) -> tuple[list, list]:
    """Get the init script volumes and mounts of the MongoDB Deployment."""
    pod = docs[("Deployment", "mongodb")]["spec"]["template"]["spec"]
    volumes = [vol for vol in pod["volumes"] if vol["name"] == "init-script"]
    mounts = [
        mount
        for mount in pod["containers"][0]["volumeMounts"]
        if mount["name"] == "init-script"
    ]
    return volumes, mounts


def test_auth_disabled():
    """Without authentication, no Secret, credentials or init script are
    needed.
    """
    docs = _render(*NO_AUTH, mongodb_secret=None)
    for name in ("cloud-registry", "mongodb"):
        assert not _secret_refs(_container(docs, name))
    assert _literal_env(_container(docs, "cloud-registry")) == {"FOCA_CONFIG_OVERLAY"}
    assert _literal_env(_container(docs, "mongodb")) == set()
    assert ("ConfigMap", "mongo-init-script") not in docs
    assert _init_script_volumes(docs) == ([], [])


def test_auth_enabled():
    """Authentication is on by default, with credentials from the Secret only."""
    docs = _render()
    app = _container(docs, "cloud-registry")
    assert _secret_refs(app) == {
        "MONGO_USERNAME": (MONGODB_SECRET, "username"),
        "MONGO_PASSWORD": (MONGODB_SECRET, "password"),
    }
    mongodb = _container(docs, "mongodb")
    assert _secret_refs(mongodb) == {
        "MONGO_INITDB_ROOT_USERNAME": (MONGODB_SECRET, "root-username"),
        "MONGO_INITDB_ROOT_PASSWORD": (MONGODB_SECRET, "root-password"),
        "MONGO_APP_USERNAME": (MONGODB_SECRET, "username"),
        "MONGO_APP_PASSWORD": (MONGODB_SECRET, "password"),
    }
    # No other variables, e.g., with literal credentials
    assert _literal_env(app) == {"FOCA_CONFIG_OVERLAY"}
    assert _literal_env(mongodb) == set()
    script = docs[("ConfigMap", "mongo-init-script")]["data"]["create-app-user.js"]
    assert "process.env.MONGO_APP_PASSWORD" in script
    # The app user is created in the database the app authenticates against
    config = yaml.safe_load(CONFIG_FILE.read_text())
    (database,) = config["db"]["dbs"]
    assert script.count(f"'{database}'") == 2
    volumes, mounts = _init_script_volumes(docs)
    assert [vol["configMap"]["name"] for vol in volumes] == ["mongo-init-script"]
    assert [mount["mountPath"] for mount in mounts] == ["/docker-entrypoint-initdb.d"]
    assert not [kind for kind, _ in docs if kind == "Secret"]


@pytest.mark.parametrize("args", [(), ("--set", "mongodb.auth.existingSecret=")])
def test_auth_requires_secret(args):
    """Authentication (the default) without a Secret fails."""
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render(*args, mongodb_secret=None)
    assert "mongodb.auth.existingSecret must be set" in exc.value.stderr
