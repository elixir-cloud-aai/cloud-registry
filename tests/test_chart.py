"""Tests for the resources of the Helm chart."""

import json
from pathlib import Path
import re
import shlex
import subprocess
from typing import Any

import pytest
import yaml

from tests.gunicorn_settings import load_settings
from tests.helm import (
    APP,
    CHART,
    HELM,
    MONGODB,
    MONGODB_SECRET,
    Manifest,
    render,
    render_notes,
    render_with,
)
from tests.mock_data import MAX_CONTENT_LENGTH
from tests.test_config import CONFIG_FILE

NO_AUTH = ("--set", "mongodb.auth.enabled=false")
NO_INGRESS = ("--set", "cloudRegistry.ingress.enabled=false")
# The app's NetworkPolicy is enabled (MongoDB's is by default)
APP_POLICY = ("--set", "cloudRegistry.networkPolicy.enabled=true")
# The cluster serves OpenShift Routes
ROUTE_API = ("--api-versions", "route.openshift.io/v1")
INIT_SCRIPT = f"{MONGODB}-init-script"
# Mount path of the containers' temporary directory
TMP = "/tmp"  # noqa: S108 (a path in the container)
# Default size limit of the temporary directories
TMP_SIZE_LIMIT = "64Mi"
# Annotation of ingress-nginx for the size limit of request bodies; by
# default the app's limit, in bytes
BODY_SIZE = "nginx.ingress.kubernetes.io/proxy-body-size"
BODY_SIZE_LIMIT = str(MAX_CONTENT_LENGTH)
PROBES = ("startupProbe", "readinessProbe", "livenessProbe")
# Environment variables of the app with literal values (no credentials)
APP_ENV = {"FOCA_CONFIG_OVERLAY", "GUNICORN_CMD_ARGS"}
# Default image tag
APP_VERSION = yaml.safe_load((CHART / "Chart.yaml").read_text())["appVersion"]

# Rendered manifests, by kind and name
Docs = dict[tuple[str, str], Manifest]

pytestmark = pytest.mark.skipif(HELM is None, reason="helm not installed")


def _render(*args: str, **kwargs: Any) -> Docs:
    """Render the chart and index the documents by kind and name.

    Args:
        args: Arguments of `helm template`.
        kwargs: Keyword arguments of `tests.helm.render()`.

    Returns:
        The rendered manifests, by kind and name.
    """
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in render("--set", "hostName=x", *args, **kwargs)
    }


def _container(docs: Docs, name: str) -> dict[str, Any]:
    """Get the container of a Deployment.

    Args:
        docs: Rendered manifests.
        name: Name of the Deployment.

    Returns:
        The (first) container of the Deployment's pods.
    """
    return docs[("Deployment", name)]["spec"]["template"]["spec"]["containers"][0]


def _secret_refs(container: dict[str, Any]) -> dict[str, tuple[str, str]]:
    """Map environment variables to the Secret keys they are taken from.

    Args:
        container: Container spec.

    Returns:
        Name and key of the Secret, by environment variable.
    """
    return {
        var["name"]: (
            var["valueFrom"]["secretKeyRef"]["name"],
            var["valueFrom"]["secretKeyRef"]["key"],
        )
        for var in container.get("env", [])
        if "secretKeyRef" in var.get("valueFrom", {})
    }


def _literal_env(container: dict[str, Any]) -> set[str]:
    """Get the names of environment variables with literal values.

    Args:
        container: Container spec.

    Returns:
        Names of the variables.
    """
    return {var["name"] for var in container.get("env", []) if "valueFrom" not in var}


def _startup_window(container: dict[str, Any]) -> int:
    """Get the seconds a container may take to start before it is restarted.

    Args:
        container: Container spec.

    Returns:
        Seconds until the startup probe fails.
    """
    probe = container["startupProbe"]
    return probe.get("initialDelaySeconds", 0) + (
        probe["periodSeconds"] * probe["failureThreshold"]
    )


def _init_script_volumes(
    docs: Docs,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Get the init script volumes and mounts of the MongoDB Deployment.

    Args:
        docs: Rendered manifests.

    Returns:
        Volumes and volume mounts of the init script.
    """
    pod = docs[("Deployment", MONGODB)]["spec"]["template"]["spec"]
    volumes = [vol for vol in pod["volumes"] if vol["name"] == "init-script"]
    mounts = [
        mount
        for mount in pod["containers"][0]["volumeMounts"]
        if mount["name"] == "init-script"
    ]
    return volumes, mounts


def _pod(docs: Docs, name: str) -> dict[str, Any]:
    """Get the pod spec of a Deployment.

    Args:
        docs: Rendered manifests.
        name: Name of the Deployment.

    Returns:
        The pod spec.
    """
    return docs[("Deployment", name)]["spec"]["template"]["spec"]


def _overlay(docs: Docs, app: str = APP) -> dict[str, Any]:
    """Get the config overlay of the app.

    Args:
        docs: Rendered manifests.
        app: Name of the app's resources.

    Returns:
        The config overlay.
    """
    overlay: dict[str, Any] = yaml.safe_load(
        docs[("ConfigMap", f"{app}-config")]["data"]["overlay.yaml"]
    )
    return overlay


def test_config_change_restarts_app() -> None:
    """Changing the config (e.g., the service info) restarts the app."""

    def checksum(*args: str) -> str:
        """Render the chart and get the checksum of the app's config."""
        template = _render(*args)[("Deployment", APP)]["spec"]["template"]
        return template["metadata"]["annotations"]["checksum/config"]

    changed = ("--set", "config.service_info.service.environment=prod")
    assert checksum() != checksum(*changed)


def _app_image(*args: str, **kwargs: Any) -> str:
    """Render the chart and get the app image.

    Args:
        args: Arguments of `helm template`.
        kwargs: Keyword arguments of `tests.helm.render()`.

    Returns:
        The image of the app's container.
    """
    return _container(_render(*args, **kwargs), "cloud-registry")["image"]


def test_image_defaults_to_app_version() -> None:
    """Without a tag, the image of the chart's `appVersion` is deployed.

    That is `dev` for the chart in the repository.
    """
    assert _app_image() == f"elixircloud/cloud-registry:{APP_VERSION}"


# Chart versions are SemVer, app versions PEP 440; the pairs mirror the
# mapping in the release workflow, which is not tested itself
@pytest.mark.parametrize(
    ("chart_version", "app_version"),
    [
        ("0.3.0", "0.3.0"),
        ("0.3.0-a1", "0.3.0a1"),
        ("0.3.0-b2", "0.3.0b2"),
        ("0.3.0-rc1", "0.3.0rc1"),
    ],
)
def test_image_of_packaged_chart(
    tmp_path: Path, chart_version: str, app_version: str
) -> None:
    """Released charts deploy the image of their version.

    The release workflow sets it.
    """
    assert HELM is not None
    subprocess.run(  # noqa: S603 (argument list, no shell)
        [
            HELM,
            "package",
            str(CHART),
            "--version",
            chart_version,
            "--app-version",
            app_version,
            "-d",
            str(tmp_path),
        ],
        capture_output=True,
        check=True,
    )
    package = tmp_path / f"cloud-registry-{chart_version}.tgz"
    assert _app_image(chart=package) == f"elixircloud/cloud-registry:{app_version}"


@pytest.mark.parametrize(
    ("args", "image"),
    [
        (("--set", "cloudRegistry.tag=sha-abc"), "elixircloud/cloud-registry:sha-abc"),
        (
            ("--set", "cloudRegistry.image=registry.example.org:5000/cr"),
            f"registry.example.org:5000/cr:{APP_VERSION}",
        ),
        # Image references with a tag or digest, as in earlier chart versions
        (("--set", "cloudRegistry.image=host:5000/cr:1.0"), "host:5000/cr:1.0"),
        (
            ("--set", "cloudRegistry.image=elixircloud/cloud-registry:20261020"),
            "elixircloud/cloud-registry:20261020",
        ),
        (
            ("--set", f"cloudRegistry.image=cr@sha256:{'0' * 64}"),
            f"cr@sha256:{'0' * 64}",
        ),
    ],
)
def test_image_set(args: tuple[str, ...], image: str) -> None:
    """The image and its tag can be set."""
    assert _app_image(*args) == image


def test_image_tag_conflict() -> None:
    """A tag cannot be set for an image that has one."""
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render(
            "--set",
            "cloudRegistry.image=elixircloud/cloud-registry:0.3.0",
            "--set",
            "cloudRegistry.tag=dev",
        )
    assert "cloudRegistry.tag must not be set" in exc.value.stderr


def test_mongodb_pinned_and_recreated() -> None:
    """MongoDB is pinned to a minor version and replaced, not rolled."""
    docs = _render()
    image = _container(docs, MONGODB)["image"]
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert image == values["mongodb"]["image"]
    assert re.fullmatch(r".+:\d+\.\d+", image)
    assert docs[("Deployment", MONGODB)]["spec"]["strategy"] == {"type": "Recreate"}


def test_probes() -> None:
    """App and MongoDB have probes on their ports."""
    docs = _render()
    app = _container(docs, APP)
    for probe in PROBES:
        assert app[probe]["httpGet"] == {"path": "/ga4gh/registry/v1/ui/", "port": 8080}
    mongodb = _container(docs, MONGODB)
    for probe in PROBES:
        assert mongodb[probe]["tcpSocket"] == {"port": 27017}


def test_startup_windows() -> None:
    """App and MongoDB may take 5 and 30 minutes to start, respectively."""
    docs = _render()
    app = _container(docs, APP)
    mongodb = _container(docs, MONGODB)
    assert _startup_window(app) == 5 * 60
    assert _startup_window(mongodb) == 30 * 60
    for container in (app, mongodb):
        assert "initialDelaySeconds" not in container["livenessProbe"]


def test_probe_path() -> None:
    """The path of the app's probes can be changed."""
    docs = _render("--set", "cloudRegistry.probePath=/health")
    app = _container(docs, APP)
    for probe in PROBES:
        assert app[probe]["httpGet"]["path"] == "/health"


@pytest.mark.parametrize("path", ["health", "null"])
def test_probe_path_invalid(path: str) -> None:
    """Probe paths must be set and absolute."""
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render("--set", f"cloudRegistry.probePath={path}")
    assert "probePath" in exc.value.stderr


def test_auth_disabled() -> None:
    """Without authentication, no Secret, credentials or init script are needed."""
    docs = _render(*NO_AUTH, mongodb_secret=None)
    for name in (APP, MONGODB):
        assert not _secret_refs(_container(docs, name))
    assert _literal_env(_container(docs, APP)) == APP_ENV
    assert _literal_env(_container(docs, MONGODB)) == set()
    assert ("ConfigMap", INIT_SCRIPT) not in docs
    assert _init_script_volumes(docs) == ([], [])


def test_auth_enabled() -> None:
    """Authentication is on by default, with credentials from the Secret only."""
    docs = _render()
    app = _container(docs, APP)
    assert _secret_refs(app) == {
        "MONGO_USERNAME": (MONGODB_SECRET, "username"),
        "MONGO_PASSWORD": (MONGODB_SECRET, "password"),
    }
    mongodb = _container(docs, MONGODB)
    assert _secret_refs(mongodb) == {
        "MONGO_INITDB_ROOT_USERNAME": (MONGODB_SECRET, "root-username"),
        "MONGO_INITDB_ROOT_PASSWORD": (MONGODB_SECRET, "root-password"),
        "MONGO_APP_USERNAME": (MONGODB_SECRET, "username"),
        "MONGO_APP_PASSWORD": (MONGODB_SECRET, "password"),
    }
    # No other variables, e.g., with literal credentials
    assert _literal_env(app) == APP_ENV
    assert _literal_env(mongodb) == set()
    script = docs[("ConfigMap", INIT_SCRIPT)]["data"]["create-app-user.js"]
    assert "process.env.MONGO_APP_PASSWORD" in script
    # The app user is created in the database the app authenticates against
    config = yaml.safe_load(CONFIG_FILE.read_text())
    (database,) = config["db"]["dbs"]
    assert script.count(f"'{database}'") == 2
    volumes, mounts = _init_script_volumes(docs)
    assert [vol["configMap"]["name"] for vol in volumes] == [INIT_SCRIPT]
    assert [mount["mountPath"] for mount in mounts] == ["/docker-entrypoint-initdb.d"]
    assert not [kind for kind, _ in docs if kind == "Secret"]


@pytest.mark.parametrize("args", [(), ("--set", "mongodb.auth.existingSecret=")])
def test_auth_requires_secret(args: tuple[str, ...]) -> None:
    """Authentication (the default) without a Secret fails."""
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render(*args, mongodb_secret=None)
    assert "mongodb.auth.existingSecret must be set" in exc.value.stderr


@pytest.mark.parametrize(
    ("release", "app"),
    [
        ("cloud-registry", "cloud-registry"),
        ("prod-cloud-registry", "prod-cloud-registry"),
        ("prod", "prod-cloud-registry"),
    ],
)
def test_names(release: str, app: str) -> None:
    """Resource names are derived from the release name."""
    # The app's NetworkPolicy is opt-in, so it is enabled to cover all kinds
    docs = _render(*APP_POLICY, release=release)
    mongodb = f"{app}-mongodb"
    assert set(docs) == {
        ("ConfigMap", f"{app}-config"),
        ("ConfigMap", f"{mongodb}-init-script"),
        ("Deployment", app),
        ("Deployment", mongodb),
        ("Ingress", app),
        ("NetworkPolicy", app),
        ("NetworkPolicy", mongodb),
        ("PersistentVolumeClaim", mongodb),
        ("Service", app),
        ("Service", mongodb),
    }


@pytest.mark.parametrize(
    ("release", "app"), [("prod", "prod-foo"), ("foo-prod", "foo-prod")]
)
def test_name_override(release: str, app: str) -> None:
    """`nameOverride` replaces the chart name in names and labels."""
    docs = _render("--set", "nameOverride=foo", release=release)
    assert ("Deployment", app) in docs
    assert ("Deployment", f"{app}-mongodb") in docs
    for doc in docs.values():
        assert doc["metadata"]["labels"]["app.kubernetes.io/name"] == "foo"


def test_fullname_override() -> None:
    """`fullnameOverride` sets the names; long names are truncated.

    So Service names are DNS labels, without collisions.
    """
    docs = _render("--set", "fullnameOverride=registry")
    assert {name for _, name in docs} >= {"registry", "registry-mongodb"}
    docs = _render("--set", f"fullnameOverride={'x' * 70}")
    names = {name for kind, name in docs if kind == "Service"}
    assert names == {"x" * 63, "x" * 55 + "-mongodb"}


def test_numeric_strings() -> None:
    """Values that look like numbers or booleans are rendered as strings."""
    docs = _render(
        "--set-string",
        "fullnameOverride=123,cloudRegistry.ingress.className=true,hostName=1",
        "--set-string",
        "mongodb.auth.existingSecret=456",
        mongodb_secret=None,
        release="789",
    )
    deployment = docs[("Deployment", "123")]
    assert deployment["metadata"]["labels"]["app.kubernetes.io/instance"] == "789"
    assert (
        docs[("Service", "123")]["spec"]["selector"]["app.kubernetes.io/instance"]
        == "789"
    )
    ingress = docs[("Ingress", "123")]
    assert ingress["spec"]["ingressClassName"] == "true"
    assert ingress["spec"]["rules"][0]["host"] == "1"
    assert (
        ingress["spec"]["rules"][0]["http"]["paths"][0]["backend"]["service"]["name"]
        == "123"
    )
    env = {
        var["name"]: var["valueFrom"]["secretKeyRef"]["name"]
        for var in _container(docs, "123")["env"]
        if "valueFrom" in var
    }
    assert set(env.values()) == {"456"}


def test_fullname_override_collision() -> None:
    """Names under which app and MongoDB resources would collide are rejected."""
    name = "x" * 55 + "-mongodb"
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render("--set", f"fullnameOverride={name}")
    assert f"would both be named {name}" in exc.value.stderr


def test_releases_disjoint() -> None:
    """Two releases in one namespace share no resource names or pods.

    Neither selects the pods of the other.
    """
    first = _render(*APP_POLICY, release="first")
    second = _render(*APP_POLICY, release="second")
    assert not set(first) & set(second)
    selectors = [
        tuple(sorted(doc["spec"]["selector"].items()))
        for docs in (first, second)
        for (kind, _), doc in docs.items()
        if kind == "Service"
    ]
    assert len(set(selectors)) == 4


@pytest.mark.parametrize("key", ["cloud_registry", "cloudRegistry"])
def test_app_name_removed(key: str) -> None:
    """The removed `appName` of the legacy charts is rejected.

    Also under the renamed key.
    """
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render("--set", f"{key}.appName=cloud-registry")
    assert f"{key}.appName was removed" in exc.value.stderr
    assert "fullnameOverride" in exc.value.stderr


@pytest.mark.parametrize(
    ("old", "new", "value"),
    [
        ("host_name", "hostName", '"registry.example.org"'),
        ("cloud_registry", "cloudRegistry", '{"tag": "0.3.0"}'),
        ("cloud_registry", "cloudRegistry", "{}"),
    ],
    ids=["host_name", "cloud_registry", "cloud_registry-empty"],
)
def test_renamed_keys(old: str, new: str, value: str) -> None:
    """Keys renamed to camelCase (Helm conventions) are rejected.

    The error names the new key, as they would otherwise be ignored.
    """
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render("--set-json", f"{old}={value}")
    assert f"{old} was renamed to {new}" in exc.value.stderr


@pytest.mark.parametrize(
    "removed, replacement",
    [
        ("config.custom.max_content_length", "config.server.max_content_length"),
        ("config.custom.endpoints.service_info", "config.service_info.service"),
    ],
    ids=["max_content_length", "service_info"],
)
def test_custom_config_removed(removed: str, replacement: str) -> None:
    """Settings of the custom config replaced by FOCA's are rejected.

    Replaced in the FOCA 0.15 upgrade; the error names the replacement.
    """
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render("--set-json", f'{removed}={{"name": "x"}}')
    assert f"{removed} was removed" in exc.value.stderr
    assert f"set {replacement} instead" in exc.value.stderr


def test_upgrade() -> None:
    """Upgrades render (the cluster is not looked up by `helm template`)."""
    assert ("Deployment", APP) in _render("--is-upgrade")


# Labels that Helm adds to all resources it creates, of any chart version
HELM_LABELS = {"app.kubernetes.io/managed-by": "Helm"}


@pytest.mark.parametrize(
    ("owner", "labels", "fails"),
    [
        ("cloud-registry", HELM_LABELS, True),
        ("cloud-registry", {**HELM_LABELS, "app": "mongodb"}, True),
        ("other", HELM_LABELS, False),
        ("", {}, False),
        (
            "cloud-registry",
            {**HELM_LABELS, "app.kubernetes.io/instance": "cloud-registry"},
            False,
        ),
    ],
    ids=["legacy", "legacy-labels", "other-release", "unmanaged", "current"],
)
def test_upgrade_legacy(owner: str, labels: dict[str, str], fails: bool) -> None:
    """Upgrades fail if the release has MongoDB resources of a legacy chart.

    Legacy charts are versions 2 and 3, whose resources the upgrade would
    delete; checked with a resource as returned by `lookup`, which finds
    nothing in `helm template`.
    """
    resource = {
        "metadata": {
            "name": "mongodb",
            "annotations": {"meta.helm.sh/release-name": owner} if owner else {},
            "labels": labels,
        }
    }
    # A template string literal (JSON-quoted) with the resource as JSON
    literal = json.dumps(json.dumps(resource))
    template = (
        '{{- include "cloud-registry.failIfLegacy" (dict "root" . "kind"'
        f' "PersistentVolumeClaim" "resource" (fromJson {literal})) }}}}'
    )
    args = ({"legacy-test.yaml": template}, "--set", "hostName=x")
    if fails:
        with pytest.raises(subprocess.CalledProcessError) as exc:
            render_with(*args)
        assert "legacy chart (version 2 or 3)" in exc.value.stderr
    else:
        render_with(*args)


@pytest.mark.parametrize(
    ("name", "component"),
    [("prod-cloud-registry", "app"), ("prod-cloud-registry-mongodb", "mongodb")],
)
def test_labels(name: str, component: str) -> None:
    """Deployments and Services select the pods by the standard labels."""
    docs = _render(release="prod")
    selector = {
        "app.kubernetes.io/name": "cloud-registry",
        "app.kubernetes.io/instance": "prod",
        "app.kubernetes.io/component": component,
    }
    deployment = docs[("Deployment", name)]
    assert deployment["spec"]["selector"] == {"matchLabels": selector}
    assert deployment["spec"]["template"]["metadata"]["labels"] == selector
    assert docs[("Service", name)]["spec"]["selector"] == selector


def test_labels_all() -> None:
    """All resources have the standard labels."""
    docs = _render(*APP_POLICY, release="prod")
    chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
    for doc in docs.values():
        labels = doc["metadata"]["labels"]
        assert labels["app.kubernetes.io/name"] == "cloud-registry"
        assert labels["app.kubernetes.io/instance"] == "prod"
        assert labels["app.kubernetes.io/component"] in ("app", "mongodb")
        assert labels["app.kubernetes.io/managed-by"] == "Helm"
        assert labels["app.kubernetes.io/version"] == chart["appVersion"]
        assert labels["helm.sh/chart"] == f"cloud-registry-{chart['version']}"


@pytest.mark.parametrize(
    ("name", "writable"),
    [(APP, {TMP}), (MONGODB, {TMP, "/data/configdb", "/data/db"})],
)
def test_security_context(name: str, writable: set[str]) -> None:
    """Containers run as non-root and without privileges.

    Also without a service account token or service links and with a
    read-only root file system; directories they write to are volumes.
    """
    docs = _render()
    pod = _pod(docs, name)
    # neither the app nor MongoDB call the Kubernetes API
    assert pod["automountServiceAccountToken"] is False
    # no `<SERVICE>_PORT` variables of Services in the namespace (FOCA reads
    # e.g. `MONGO_PORT` as configuration)
    assert pod["enableServiceLinks"] is False
    assert pod["securityContext"]["runAsNonRoot"] is True
    assert pod["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
    context = pod["containers"][0]["securityContext"]
    assert context["allowPrivilegeEscalation"] is False
    assert context["capabilities"] == {"drop": ["ALL"]}
    assert context["readOnlyRootFilesystem"] is True
    volumes = {vol["name"]: vol for vol in pod["volumes"]}
    mounts = {
        mount["mountPath"]: volumes[mount["name"]]
        for mount in pod["containers"][0]["volumeMounts"]
        if not mount.get("readOnly")
    }
    assert set(mounts) == writable
    data = mounts.pop("/data/db", None)
    if name == MONGODB:
        assert data is not None
        assert data["persistentVolumeClaim"] == {"claimName": MONGODB}
    assert mounts.pop(TMP)["emptyDir"] == {"sizeLimit": TMP_SIZE_LIMIT}
    for volume in mounts.values():
        assert volume["emptyDir"] == {}


def _tmp_volume(docs: Docs, name: str) -> dict[str, Any]:
    """Get the volume of a Deployment mounted at `/tmp`.

    Args:
        docs: Rendered manifests.
        name: Name of the Deployment.

    Returns:
        The volume.
    """
    pod = _pod(docs, name)
    (mount,) = [
        m for m in pod["containers"][0]["volumeMounts"] if m["mountPath"] == TMP
    ]
    (volume,) = [vol for vol in pod["volumes"] if vol["name"] == mount["name"]]
    return volume


@pytest.mark.parametrize(
    ("name", "values"), [(APP, "cloudRegistry"), (MONGODB, "mongodb")]
)
def test_tmp_size_limit(tmp_path: Path, name: str, values: str) -> None:
    """The size limit of the temporary directory can be changed or removed.

    It can be changed to a quantity or a number of bytes, also from values
    files, which have floats.
    """
    docs = _render("--set", f"{values}.tmpSizeLimit=1.5Gi")
    assert _tmp_volume(docs, name)["emptyDir"] == {"sizeLimit": "1.5Gi"}
    docs = _render("--set", f"{values}.tmpSizeLimit=67108864")
    assert _tmp_volume(docs, name)["emptyDir"] == {"sizeLimit": "67108864"}
    values_file = tmp_path / "values.yaml"
    values_file.write_text(yaml.safe_dump({values: {"tmpSizeLimit": 67108864}}))
    docs = _render("-f", str(values_file))
    assert _tmp_volume(docs, name)["emptyDir"] == {"sizeLimit": "67108864"}
    docs = _render("--set", f"{values}.tmpSizeLimit=null")
    assert _tmp_volume(docs, name)["emptyDir"] == {}


def _gunicorn_args(docs: Docs) -> str:
    """Get the Gunicorn options of the app (`GUNICORN_CMD_ARGS`).

    Args:
        docs: Rendered manifests.

    Returns:
        The options.
    """
    env = _container(docs, APP)["env"]
    (args,) = [var["value"] for var in env if var["name"] == "GUNICORN_CMD_ARGS"]
    return args


def _grace_period(docs: Docs) -> int:
    """Get the termination grace period of the app's pod.

    Args:
        docs: Rendered manifests.

    Returns:
        The grace period, in seconds.
    """
    return _pod(docs, APP)["terminationGracePeriodSeconds"]


def test_gunicorn_defaults() -> None:
    """By default, Gunicorn gets the image's settings."""
    docs = _render()
    cfg = load_settings(_gunicorn_args(docs)).cfg
    image = load_settings().cfg
    for name in ("workers", "threads", "timeout", "graceful_timeout", "keepalive"):
        assert getattr(cfg, name) == getattr(image, name), name


def test_gunicorn_values(tmp_path: Path) -> None:
    """Gunicorn's settings can be changed, with further options.

    Also from values files, which have floats; further options may contain
    spaces.
    """
    values = {
        "workers": 2,
        "threads": 8,
        "timeout": 60,
        "gracefulTimeout": 45,
        "keepalive": 0,
        "extraArgs": ["--access-logfile", "-", "--access-logformat", "%(h)s %(r)s"],
    }
    values_file = tmp_path / "values.yaml"
    values_file.write_text(yaml.safe_dump({"cloudRegistry": {"gunicorn": values}}))
    docs = _render("-f", str(values_file))
    cfg = load_settings(_gunicorn_args(docs)).cfg
    assert (cfg.workers, cfg.threads, cfg.timeout) == (2, 8, 60)
    assert (cfg.graceful_timeout, cfg.keepalive) == (45, 0)
    assert (cfg.accesslog, cfg.access_log_format) == ("-", "%(h)s %(r)s")


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (
            ("--set", "cloudRegistry.gunicorn.workers=0"),
            "'/cloudRegistry/gunicorn/workers': minimum",
        ),
        (
            ("--set-json", "cloudRegistry.gunicorn.threads=1.5"),
            "'/cloudRegistry/gunicorn/threads': got number, want integer",
        ),
        (
            ("--set-string", "cloudRegistry.gunicorn.timeout=30"),
            "'/cloudRegistry/gunicorn/timeout': got string, want integer",
        ),
        (
            ("--set", "cloudRegistry.gunicorn.gracefulTimeout=null"),
            "'/cloudRegistry/gunicorn': missing property 'gracefulTimeout'",
        ),
        (
            ("--set", "cloudRegistry.gunicorn.keepalive=-1"),
            "'/cloudRegistry/gunicorn/keepalive': minimum",
        ),
        (
            ("--set-json", """cloudRegistry.gunicorn.extraArgs=["--name='x'"]"""),
            "'/cloudRegistry/gunicorn/extraArgs/0': ",
        ),
        (
            ("--set", "cloudRegistry.gunicorn.preload=true"),
            "'/cloudRegistry/gunicorn': additional properties 'preload'",
        ),
        (
            ("--set", "cloudRegistry.gunicorn=null"),
            "'/cloudRegistry': missing property 'gunicorn'",
        ),
    ],
    ids=[
        "workers",
        "threads",
        "timeout",
        "graceful",
        "keepalive",
        "quote",
        "unknown",
        "null",
    ],
)
def test_gunicorn_invalid(args: tuple[str, ...], error: str) -> None:
    """Gunicorn's settings must be integers in their ranges.

    Further options must not contain single quotes, and other settings are
    rejected.
    """
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render(*args)
    assert error in exc.value.stderr


def _render_extra_args(*extra_args: str) -> Docs:
    """Render the chart with further Gunicorn options.

    Args:
        extra_args: Further Gunicorn options (`cloudRegistry.gunicorn.extraArgs`).

    Returns:
        The rendered manifests, by kind and name.
    """
    return _render(
        "--set-json", f"cloudRegistry.gunicorn.extraArgs={json.dumps(extra_args)}"
    )


@pytest.mark.parametrize(
    "extra_args",
    [
        ("--bind", "0.0.0.0:9090"),
        ("--bind=0.0.0.0:9090",),
        ("--bin", "0.0.0.0:9090"),
        ("-b", "0.0.0.0:9090"),
        ("-b0.0.0.0:9090",),
        ("-Rb", "0.0.0.0:9090"),
        ("--preload",),
        ("--pre",),
        ("--graceful-timeout", "60"),
        ("--graceful-timeout=60",),
        ("--graceful", "60"),
    ],
)
def test_gunicorn_reserved_args(extra_args: tuple[str, ...]) -> None:
    """Further options must not change reserved settings.

    They must not change the address (the chart's ports), preload the app
    (MongoDB clients must be created after forking) or set the graceful
    timeout (the grace period is derived from `gracefulTimeout`), also
    abbreviated (as Gunicorn accepts them) or with attached values; Gunicorn
    itself would apply them.
    """
    cfg = load_settings(shlex.join(extra_args)).cfg
    assert (cfg.bind, cfg.preload_app, cfg.graceful_timeout) != (
        ["0.0.0.0:8080"],
        False,
        20,
    )
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render_extra_args(*extra_args)
    assert "cloudRegistry.gunicorn.extraArgs must not set" in exc.value.stderr
    assert repr(extra_args[0]).strip("'") in exc.value.stderr


def test_gunicorn_other_args() -> None:
    """Other further options are passed on.

    Also short ones and option values that look like reserved options (which
    Gunicorn takes as values, as they contain spaces or follow `=`).
    """
    extra_args = (
        "-R",
        "--backlog",
        "64",
        "--access-logformat",
        "--bind %(h)s",
        "--name=-bx",
    )
    cfg = load_settings(_gunicorn_args(_render_extra_args(*extra_args))).cfg
    assert (cfg.enable_stdio_inheritance, cfg.backlog) == (True, 64)
    assert (cfg.access_log_format, cfg.proc_name) == ("--bind %(h)s", "-bx")
    assert (cfg.bind, cfg.preload_app) == (["0.0.0.0:8080"], False)


def test_gunicorn_port() -> None:
    """The app's container, probe and Service ports are Gunicorn's port.

    Gunicorn listens on the port of the image's command; the app config's
    `server.port` applies to the development server only.
    """
    (bind,) = load_settings().cfg.bind
    port = int(bind.rpartition(":")[2])
    docs = _render()
    app = _container(docs, APP)
    (container_port,) = app["ports"]
    assert (container_port["name"], container_port["containerPort"]) == ("http", port)
    for probe in PROBES:
        assert app[probe]["httpGet"]["port"] in (port, "http"), probe
    (service_port,) = docs[("Service", APP)]["spec"]["ports"]
    assert service_port["targetPort"] in (port, "http")


def test_pre_stop_default() -> None:
    """By default, the app keeps serving for 5 seconds after its pod is deleted.

    That is until the ingress controller removes it from its endpoints; the
    termination grace period covers that, Gunicorn's graceful timeout and a
    margin of 10 seconds.
    """
    docs = _render()
    hook = _container(docs, APP)["lifecycle"]["preStop"]
    assert hook == {"exec": {"command": ["python", "-c", "import time; time.sleep(5)"]}}
    assert _grace_period(docs) == 5 + 20 + 10


def test_pre_stop_values(tmp_path: Path) -> None:
    """The delay before stopping can be changed or removed (`0`).

    Also from values files, which have floats; the grace period follows it
    and the graceful timeout.
    """
    values_file = tmp_path / "values.yaml"
    values_file.write_text(
        yaml.safe_dump(
            {
                "cloudRegistry": {
                    "preStopSleepSeconds": 15,
                    "gunicorn": {"gracefulTimeout": 45},
                }
            }
        )
    )
    docs = _render("-f", str(values_file))
    command = _container(docs, APP)["lifecycle"]["preStop"]["exec"]["command"]
    assert command[-1] == "import time; time.sleep(15)"
    assert _grace_period(docs) == 15 + 45 + 10
    docs = _render("--set", "cloudRegistry.preStopSleepSeconds=0")
    assert "lifecycle" not in _container(docs, APP)
    assert _grace_period(docs) == 20 + 10


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("-1", "'/cloudRegistry/preStopSleepSeconds': minimum"),
        ("1.5", "'/cloudRegistry/preStopSleepSeconds': got number, want integer"),
        ("null", "'/cloudRegistry': missing property 'preStopSleepSeconds'"),
    ],
)
def test_pre_stop_invalid(value: str, error: str) -> None:
    """The delay must be a non-negative integer."""
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render("--set-json", f"cloudRegistry.preStopSleepSeconds={value}")
    assert error in exc.value.stderr


def test_security_context_user_ids() -> None:
    """MongoDB runs as the image's user `mongodb`, except on OpenShift.

    OpenShift assigns user and group IDs, also if they are set in the values.
    """
    ids = {"runAsUser": 999, "runAsGroup": 999, "fsGroup": 999}
    context = _pod(_render(), MONGODB)["securityContext"]
    assert ids.items() <= context.items()
    docs = _render(
        "--api-versions",
        "security.openshift.io/v1",
        "--set",
        "cloudRegistry.podSecurityContext.runAsUser=1000",
        "--set",
        "cloudRegistry.securityContext.runAsUser=1000",
        "--set",
        "mongodb.securityContext.runAsGroup=999",
    )
    for name in (APP, MONGODB):
        pod = _pod(docs, name)
        assert not set(ids) & set(pod["securityContext"])
        assert pod["securityContext"]["runAsNonRoot"] is True
        context = pod["containers"][0]["securityContext"]
        assert not set(ids) & set(context)
        assert context["readOnlyRootFilesystem"] is True


def test_security_context_values() -> None:
    """Security contexts can be changed via the values."""
    docs = _render(
        "--set",
        "cloudRegistry.securityContext.readOnlyRootFilesystem=false",
        "--set",
        "mongodb.podSecurityContext.fsGroup=null",
    )
    app = _container(docs, APP)["securityContext"]
    assert app["readOnlyRootFilesystem"] is False
    assert app["allowPrivilegeEscalation"] is False
    assert "fsGroup" not in _pod(docs, MONGODB)["securityContext"]


@pytest.mark.parametrize(
    ("release", "app"), [("cloud-registry", APP), ("prod", "prod-cloud-registry")]
)
def test_db_host(release: str, app: str) -> None:
    """The app connects to the MongoDB Service of its release.

    Unless the config sets another host.
    """
    docs = _render(release=release)
    assert _overlay(docs, app)["db"] == {"host": f"{app}-mongodb"}
    assert ("Service", f"{app}-mongodb") in docs
    overlay = _overlay(_render("--set", "config.db.host=external"))
    assert overlay["db"] == {"host": "external"}


def test_ingress() -> None:
    """The Ingress routes the host to the app's Service.

    It has no class by default; its only annotation limits request bodies
    (for ingress-nginx) to the app's limit.
    """
    ingress = _render()[("Ingress", APP)]
    assert ingress["metadata"]["annotations"] == {BODY_SIZE: BODY_SIZE_LIMIT}
    assert "ingressClassName" not in ingress["spec"]
    assert "tls" not in ingress["spec"]
    (rule,) = ingress["spec"]["rules"]
    assert rule["host"] == "x"
    (path,) = rule["http"]["paths"]
    assert path["backend"]["service"] == {"name": APP, "port": {"name": "http"}}


def test_ingress_options() -> None:
    """Ingress class and annotations can be set.

    Annotations take precedence over those requesting a certificate.
    """
    ingress = _render(
        "--set",
        "cloudRegistry.ingress.className=nginx",
        "--set",
        "cloudRegistry.ingress.https.enabled=true",
        "--set-json",
        'cloudRegistry.ingress.annotations={"a.example.org/b": "c",'
        ' "kubernetes.io/tls-acme": "false"}',
    )[("Ingress", APP)]
    assert ingress["spec"]["ingressClassName"] == "nginx"
    assert ingress["metadata"]["annotations"] == {
        "a.example.org/b": "c",
        "cert-manager.io/cluster-issuer": "letsencrypt-prod",
        "kubernetes.io/tls-acme": "false",
        BODY_SIZE: BODY_SIZE_LIMIT,
    }
    assert ingress["spec"]["tls"] == [
        {"hosts": ["x"], "secretName": f"{APP}-ingress-secret"}
    ]


def test_ingress_body_size_default() -> None:
    """By default, ingress-nginx limits request bodies as the app does.

    To the default limit of the app, as in the base config.
    """
    config = yaml.safe_load(CONFIG_FILE.read_text())
    assert config["server"]["max_content_length"] == MAX_CONTENT_LENGTH
    annotations = _render()[("Ingress", APP)]["metadata"]["annotations"]
    assert annotations[BODY_SIZE] == BODY_SIZE_LIMIT


def test_ingress_body_size_app(tmp_path: Path) -> None:
    """The size limit of request bodies of ingress-nginx follows the app's.

    Also from values files, which have floats.
    """
    docs = _render("--set", "config.server.max_content_length=8388608")
    assert docs[("Ingress", APP)]["metadata"]["annotations"] == {BODY_SIZE: "8388608"}
    values_file = tmp_path / "values.yaml"
    config = {"server": {"max_content_length": 8388608}}
    values_file.write_text(yaml.safe_dump({"config": config}))
    ingress = _render("-f", str(values_file))[("Ingress", APP)]
    assert ingress["metadata"]["annotations"] == {BODY_SIZE: "8388608"}


@pytest.mark.parametrize(
    "args",
    [
        ("--set", "config.server.max_content_length=null"),
        ("--set", "config.server.max_content_length=0"),
        ("--set-string", "config.server.max_content_length=1m"),
    ],
    ids=["null", "zero", "string"],
)
def test_ingress_body_size_invalid(args: tuple[str, ...]) -> None:
    """Rendering fails unless the app's body size limit is a positive integer.

    0 would mean no limit for ingress-nginx.
    """
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render(*args)
    assert "max_content_length" in exc.value.stderr


def test_ingress_body_size_custom_null() -> None:
    """`config.custom: null` is like no custom config.

    Ingress-nginx gets the app's default limit, and the generated custom
    config is kept.
    """
    docs = _render("--set", "config.custom=null")
    ingress = docs[("Ingress", APP)]
    assert ingress["metadata"]["annotations"] == {BODY_SIZE: BODY_SIZE_LIMIT}
    assert _overlay(docs)["custom"]["endpoints"]["service"]["external_host"] == "x"


def test_server_null() -> None:
    """`config.server: null` is like no server config.

    Ingress-nginx gets the app's default limit, and the overlay has no
    `server` (FOCA would refuse `null`).
    """
    docs = _render("--set-json", "config.server=null")
    ingress = docs[("Ingress", APP)]
    assert ingress["metadata"]["annotations"] == {BODY_SIZE: BODY_SIZE_LIMIT}
    assert "server" not in _overlay(docs)


def test_ingress_body_size_annotation() -> None:
    """An annotation's size limit of request bodies overrides the app's.

    `null` removes it.
    """

    def render(limit: str | None) -> Manifest:
        """Render the Ingress with a size limit annotation."""
        annotations = json.dumps({BODY_SIZE: limit})
        args = ("--set-json", f"cloudRegistry.ingress.annotations={annotations}")
        return _render(*args)[("Ingress", APP)]

    ingress = render("8m")
    assert ingress["metadata"]["annotations"] == {BODY_SIZE: "8m"}
    ingress = render(None)
    assert "annotations" not in ingress["metadata"]


def test_route() -> None:
    """Without an Ingress, an OpenShift Route is created if the cluster serves them."""
    assert not [
        kind for kind, _ in _render(*NO_INGRESS) if kind in ("Ingress", "Route")
    ]
    docs = _render(*NO_INGRESS, *ROUTE_API)
    route = docs[("Route", APP)]
    assert route["spec"]["to"]["name"] == APP
    assert route["spec"]["port"] == {"targetPort": "http"}
    assert ("Ingress", APP) not in docs
    # The Ingress takes precedence
    docs = _render(*ROUTE_API)
    assert ("Ingress", APP) in docs
    assert ("Route", APP) not in docs


@pytest.mark.parametrize(
    ("args", "exposed", "tls_note"),
    [
        ((), True, True),
        (("--set", "cloudRegistry.ingress.https.enabled=true"), True, False),
        (ROUTE_API, True, True),
        ((*NO_INGRESS, *ROUTE_API), True, False),
        (NO_INGRESS, False, False),
    ],
    ids=["ingress", "ingress-https", "ingress-route-api", "route", "none"],
)
def test_notes(args: tuple[str, ...], exposed: bool, tls_note: bool) -> None:
    """The notes show how to reach the app.

    They show the app's HTTPS URL (as advertised by the app) with a request,
    noting if the Ingress has no TLS, or warn and show a port forward if the
    app is not exposed.
    """
    notes = render_notes("--set", "hostName=x", "--namespace", "registry", *args)
    url = "https://x" if exposed else "http://localhost:8080"
    assert f'curl -X GET "{url}/ga4gh/registry/v1/service-info"' in notes
    assert ("WARNING" in notes) is not exposed
    assert ("TLS must be terminated in front" in notes) is tls_note
    forward = f"kubectl port-forward svc/{APP} 8080:8080 -n registry\n"
    assert (forward in notes) is not exposed
    assert f"kubectl rollout status deploy/{APP} -n registry" in notes


# Default peers allowed to connect to the app: the namespace of ingress-nginx
INGRESS_NGINX = [
    {
        "namespaceSelector": {
            "matchLabels": {"kubernetes.io/metadata.name": "ingress-nginx"}
        }
    }
]


def _network_policies(docs: Docs) -> dict[str, Manifest]:
    """Get the NetworkPolicies by name.

    Args:
        docs: Rendered manifests.

    Returns:
        The NetworkPolicies, by name.
    """
    return {name: doc for (kind, name), doc in docs.items() if kind == "NetworkPolicy"}


def _pod_labels(docs: Docs, name: str) -> dict[str, str]:
    """Get the pod labels of a Deployment.

    Args:
        docs: Rendered manifests.
        name: Name of the Deployment.

    Returns:
        The labels of the Deployment's pods.
    """
    return docs[("Deployment", name)]["spec"]["template"]["metadata"]["labels"]


def _only_ingress(policy: Manifest) -> list[dict[str, Any]]:
    """Get the ingress rules of a NetworkPolicy that restricts ingress only.

    Checks that the NetworkPolicy does not restrict egress.

    Args:
        policy: NetworkPolicy.

    Returns:
        The ingress rules.
    """
    spec = policy["spec"]
    assert spec["policyTypes"] == ["Ingress"]
    assert "egress" not in spec
    return spec["ingress"]


def test_network_policy_defaults() -> None:
    """By default, only the app's pods of the release may connect to MongoDB.

    They may connect on its port; the app has no NetworkPolicy.
    """
    docs = _render(release="prod")
    policies = _network_policies(docs)
    assert set(policies) == {"prod-cloud-registry-mongodb"}
    policy = policies["prod-cloud-registry-mongodb"]
    mongodb_labels = _pod_labels(docs, "prod-cloud-registry-mongodb")
    assert policy["spec"]["podSelector"] == {"matchLabels": mongodb_labels}
    app_labels = _pod_labels(docs, "prod-cloud-registry")
    assert _only_ingress(policy) == [
        {
            "from": [{"podSelector": {"matchLabels": app_labels}}],
            "ports": [{"port": 27017, "protocol": "TCP"}],
        }
    ]
    container = _container(docs, "prod-cloud-registry-mongodb")
    assert container["ports"][0]["containerPort"] == 27017


def test_network_policy_disabled() -> None:
    """The MongoDB NetworkPolicy can be disabled."""
    docs = _render("--set", "mongodb.networkPolicy.enabled=false")
    assert not _network_policies(docs)


def test_network_policy_app() -> None:
    """If enabled, only ingress-nginx may connect to the app by default.

    Only its namespace may connect, on the app's port.
    """
    docs = _render(*APP_POLICY)
    policy = _network_policies(docs)[APP]
    assert policy["spec"]["podSelector"] == {"matchLabels": _pod_labels(docs, APP)}
    assert _only_ingress(policy) == [
        {"from": INGRESS_NGINX, "ports": [{"port": 8080, "protocol": "TCP"}]}
    ]
    assert _container(docs, APP)["ports"][0]["containerPort"] == 8080
    assert MONGODB in _network_policies(docs)


def test_network_policy_app_ingress_from(tmp_path: Path) -> None:
    """The peers allowed to connect to the app can be changed."""
    peers = [
        {
            "namespaceSelector": {
                "matchLabels": {"policy-group.network.openshift.io/ingress": ""}
            }
        },
        {"ipBlock": {"cidr": "10.0.0.0/8", "except": ["10.1.0.0/16"]}},
    ]
    values_file = tmp_path / "values.yaml"
    values_file.write_text(
        yaml.safe_dump(
            {
                "cloudRegistry": {
                    "networkPolicy": {"enabled": True, "ingressFrom": peers}
                }
            }
        )
    )
    docs = _render("-f", str(values_file))
    (rule,) = _only_ingress(_network_policies(docs)[APP])
    assert rule["from"] == peers


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (
            ("--set-json", "cloudRegistry.networkPolicy.ingressFrom=[]"),
            "'/cloudRegistry/networkPolicy/ingressFrom': minItems",
        ),
        (
            ("--set-json", "cloudRegistry.networkPolicy.ingressFrom=[{}]"),
            "'/cloudRegistry/networkPolicy/ingressFrom/0': minProperties",
        ),
        (
            ("--set", "cloudRegistry.networkPolicy.ingressFrom=null"),
            "ingressFrom must list at least one NetworkPolicy peer",
        ),
        (
            (
                "--set-json",
                'cloudRegistry.networkPolicy.ingressFrom=[{"podselector": {}}]',
            ),
            "additional properties 'podselector'",
        ),
        (
            (
                "--set-json",
                'cloudRegistry.networkPolicy.ingressFrom=[{"ipBlock": {}}]',
            ),
            "missing property 'cidr'",
        ),
        (
            (
                "--set-json",
                "cloudRegistry.networkPolicy.ingressFrom="
                '[{"ipBlock": {"cidr": "10.0.0.0/8", "excpt": []}}]',
            ),
            "additional properties 'excpt'",
        ),
        (
            (
                "--set-json",
                "cloudRegistry.networkPolicy.ingressFrom="
                '[{"ipBlock": {"cidr": "10.0.0.0/8"}, "podSelector": {}}]',
            ),
            "'/cloudRegistry/networkPolicy/ingressFrom/0': maxProperties",
        ),
        (
            ("--set-string", "mongodb.networkPolicy.enabled=false"),
            "'/mongodb/networkPolicy/enabled': got string, want boolean",
        ),
    ],
    ids=[
        "empty",
        "empty-peer",
        "null",
        "unknown-peer-field",
        "ip-block-cidr",
        "ip-block-unknown-field",
        "ip-block-selector",
        "enabled-type",
    ],
)
def test_network_policy_invalid(args: tuple[str, ...], error: str) -> None:
    """Rendering fails without peers, with unknown peer fields or wrong types.

    No peers allowed to connect to the app would allow all sources.
    """
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render(*APP_POLICY, *args)
    assert error in exc.value.stderr
