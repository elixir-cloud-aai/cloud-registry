"""Tests for the resources of the Helm chart."""

import json
import re
import shlex
import subprocess

import pytest
import yaml

from cloud_registry.service_models.custom_config import MAX_CONTENT_LENGTH
from tests.gunicorn_settings import load_settings
from tests.helm import (
    APP,
    CHART,
    HELM,
    MONGODB,
    MONGODB_SECRET,
    render,
    render_notes,
    render_with,
)
from tests.test_config import CONFIG_FILE

NO_AUTH = ("--set", "mongodb.auth.enabled=false")
NO_INGRESS = ("--set", "cloud_registry.ingress.enabled=false")
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


def _init_script_volumes(docs: dict) -> tuple[list, list]:
    """Get the init script volumes and mounts of the MongoDB Deployment."""
    pod = docs[("Deployment", MONGODB)]["spec"]["template"]["spec"]
    volumes = [vol for vol in pod["volumes"] if vol["name"] == "init-script"]
    mounts = [
        mount
        for mount in pod["containers"][0]["volumeMounts"]
        if mount["name"] == "init-script"
    ]
    return volumes, mounts


def _pod(docs: dict, name: str) -> dict:
    """Get the pod spec of a Deployment."""
    return docs[("Deployment", name)]["spec"]["template"]["spec"]


def _overlay(docs: dict, app: str = APP) -> dict:
    """Get the config overlay of the app."""
    return yaml.safe_load(docs[("ConfigMap", f"{app}-config")]["data"]["overlay.yaml"])


def test_config_change_restarts_app():
    """Changing the config (e.g., the service info) restarts the app."""

    def checksum(*args: str) -> str:
        template = _render(*args)[("Deployment", APP)]["spec"]["template"]
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
def test_image_of_packaged_chart(tmp_path, chart_version, app_version):
    """Released charts deploy the image of their version, as set by the
    release workflow.
    """
    assert HELM is not None
    subprocess.run(  # noqa: S603
        [HELM, "package", str(CHART), "--version", chart_version]
        + ["--app-version", app_version, "-d", str(tmp_path)],
        capture_output=True,
        check=True,
    )
    package = tmp_path / f"cloud-registry-{chart_version}.tgz"
    assert _app_image(chart=package) == f"elixircloud/cloud-registry:{app_version}"


@pytest.mark.parametrize(
    ("args", "image"),
    [
        (("--set", "cloud_registry.tag=sha-abc"), "elixircloud/cloud-registry:sha-abc"),
        (
            ("--set", "cloud_registry.image=registry.example.org:5000/cr"),
            f"registry.example.org:5000/cr:{APP_VERSION}",
        ),
        # Image references with a tag or digest, as in earlier chart versions
        (("--set", "cloud_registry.image=host:5000/cr:1.0"), "host:5000/cr:1.0"),
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
    image = _container(docs, MONGODB)["image"]
    values = yaml.safe_load((CHART / "values.yaml").read_text())
    assert image == values["mongodb"]["image"]
    assert re.fullmatch(r".+:\d+\.\d+", image)
    assert docs[("Deployment", MONGODB)]["spec"]["strategy"] == {"type": "Recreate"}


def test_probes():
    """App and MongoDB have probes on their ports."""
    docs = _render()
    app = _container(docs, APP)
    for probe in PROBES:
        assert app[probe]["httpGet"] == {"path": "/ga4gh/registry/v1/ui/", "port": 8080}
    mongodb = _container(docs, MONGODB)
    for probe in PROBES:
        assert mongodb[probe]["tcpSocket"] == {"port": 27017}


def test_startup_windows():
    """App and MongoDB may take 5 and 30 minutes to start, respectively."""
    docs = _render()
    app = _container(docs, APP)
    mongodb = _container(docs, MONGODB)
    assert _startup_window(app) == 5 * 60
    assert _startup_window(mongodb) == 30 * 60
    for container in (app, mongodb):
        assert "initialDelaySeconds" not in container["livenessProbe"]


def test_probe_path():
    """The path of the app's probes can be changed."""
    docs = _render("--set", "cloud_registry.probePath=/health")
    app = _container(docs, APP)
    for probe in PROBES:
        assert app[probe]["httpGet"]["path"] == "/health"


@pytest.mark.parametrize("path", ["health", "null"])
def test_probe_path_invalid(path):
    """Probe paths must be set and absolute."""
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render("--set", f"cloud_registry.probePath={path}")
    assert "probePath" in exc.value.stderr


def test_auth_disabled():
    """Without authentication, no Secret, credentials or init script are
    needed.
    """
    docs = _render(*NO_AUTH, mongodb_secret=None)
    for name in (APP, MONGODB):
        assert not _secret_refs(_container(docs, name))
    assert _literal_env(_container(docs, APP)) == APP_ENV
    assert _literal_env(_container(docs, MONGODB)) == set()
    assert ("ConfigMap", INIT_SCRIPT) not in docs
    assert _init_script_volumes(docs) == ([], [])


def test_auth_enabled():
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
def test_auth_requires_secret(args):
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
def test_names(release, app):
    """Resource names are derived from the release name."""
    docs = _render(release=release)
    mongodb = f"{app}-mongodb"
    assert set(docs) == {
        ("ConfigMap", f"{app}-config"),
        ("ConfigMap", f"{mongodb}-init-script"),
        ("Deployment", app),
        ("Deployment", mongodb),
        ("Ingress", app),
        ("PersistentVolumeClaim", mongodb),
        ("Service", app),
        ("Service", mongodb),
    }


@pytest.mark.parametrize(
    ("release", "app"), [("prod", "prod-foo"), ("foo-prod", "foo-prod")]
)
def test_name_override(release, app):
    """`nameOverride` replaces the chart name in names and labels."""
    docs = _render("--set", "nameOverride=foo", release=release)
    assert ("Deployment", app) in docs
    assert ("Deployment", f"{app}-mongodb") in docs
    for doc in docs.values():
        assert doc["metadata"]["labels"]["app.kubernetes.io/name"] == "foo"


def test_fullname_override():
    """`fullnameOverride` sets the names; long names are truncated, so that
    Service names are DNS labels, without collisions.
    """
    docs = _render("--set", "fullnameOverride=registry")
    assert {name for _, name in docs} >= {"registry", "registry-mongodb"}
    docs = _render("--set", f"fullnameOverride={'x' * 70}")
    names = {name for kind, name in docs if kind == "Service"}
    assert names == {"x" * 63, "x" * 55 + "-mongodb"}


def test_numeric_strings():
    """Values that look like numbers or booleans are rendered as strings."""
    docs = _render(
        "--set-string",
        "fullnameOverride=123,cloud_registry.ingress.className=true,host_name=1",
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


def test_fullname_override_collision():
    """Names under which app and MongoDB resources would collide are
    rejected.
    """
    name = "x" * 55 + "-mongodb"
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render("--set", f"fullnameOverride={name}")
    assert f"would both be named {name}" in exc.value.stderr


def test_releases_disjoint():
    """Two releases in one namespace share no resource names, and neither
    selects the pods of the other.
    """
    first = _render(release="first")
    second = _render(release="second")
    assert not set(first) & set(second)
    selectors = [
        tuple(sorted(doc["spec"]["selector"].items()))
        for docs in (first, second)
        for (kind, _), doc in docs.items()
        if kind == "Service"
    ]
    assert len(set(selectors)) == 4


def test_app_name_removed():
    """The removed `cloud_registry.appName` is rejected."""
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render("--set", "cloud_registry.appName=cloud-registry")
    assert "fullnameOverride" in exc.value.stderr


def test_upgrade():
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
def test_upgrade_legacy(owner, labels, fails):
    """Upgrades fail if the release has MongoDB resources of a legacy chart
    (version 2 or 3), which the upgrade would delete; checked with a resource as
    returned by `lookup`, which finds nothing in `helm template`.
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
    args = ({"legacy-test.yaml": template}, "--set", "host_name=x")
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
def test_labels(name, component):
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


def test_labels_all():
    """All resources have the standard labels."""
    docs = _render(release="prod")
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
def test_security_context(name, writable):
    """Containers run as non-root, without privileges and with a read-only
    root file system; directories they write to are volumes.
    """
    docs = _render()
    pod = _pod(docs, name)
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
        assert data["persistentVolumeClaim"] == {"claimName": MONGODB}
    assert mounts.pop(TMP)["emptyDir"] == {"sizeLimit": TMP_SIZE_LIMIT}
    for volume in mounts.values():
        assert volume["emptyDir"] == {}


def _tmp_volume(docs: dict, name: str) -> dict:
    """Get the volume of a Deployment mounted at `/tmp`."""
    pod = _pod(docs, name)
    (mount,) = [
        m for m in pod["containers"][0]["volumeMounts"] if m["mountPath"] == TMP
    ]
    (volume,) = [vol for vol in pod["volumes"] if vol["name"] == mount["name"]]
    return volume


@pytest.mark.parametrize(
    ("name", "values"), [(APP, "cloud_registry"), (MONGODB, "mongodb")]
)
def test_tmp_size_limit(tmp_path, name, values):
    """The size limit of the temporary directory can be changed (to a
    quantity or a number of bytes, also from values files, which have
    floats) or removed.
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


def _gunicorn_args(docs: dict) -> str:
    """Get the Gunicorn options of the app (`GUNICORN_CMD_ARGS`)."""
    env = _container(docs, APP)["env"]
    (args,) = [var["value"] for var in env if var["name"] == "GUNICORN_CMD_ARGS"]
    return args


def _grace_period(docs: dict) -> int:
    """Get the termination grace period of the app's pod."""
    return _pod(docs, APP)["terminationGracePeriodSeconds"]


def test_gunicorn_defaults():
    """By default, Gunicorn gets the image's settings."""
    docs = _render()
    cfg = load_settings(_gunicorn_args(docs)).cfg
    image = load_settings().cfg
    for name in ("workers", "threads", "timeout", "graceful_timeout", "keepalive"):
        assert getattr(cfg, name) == getattr(image, name), name


def test_gunicorn_values(tmp_path):
    """Gunicorn's settings can be changed (also from values files, which
    have floats), with further options, which may contain spaces.
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
    values_file.write_text(yaml.safe_dump({"cloud_registry": {"gunicorn": values}}))
    docs = _render("-f", str(values_file))
    cfg = load_settings(_gunicorn_args(docs)).cfg
    assert (cfg.workers, cfg.threads, cfg.timeout) == (2, 8, 60)
    assert (cfg.graceful_timeout, cfg.keepalive) == (45, 0)
    assert (cfg.accesslog, cfg.access_log_format) == ("-", "%(h)s %(r)s")


@pytest.mark.parametrize(
    ("args", "error"),
    [
        (
            ("--set", "cloud_registry.gunicorn.workers=0"),
            "'/cloud_registry/gunicorn/workers': minimum",
        ),
        (
            ("--set-json", "cloud_registry.gunicorn.threads=1.5"),
            "'/cloud_registry/gunicorn/threads': got number, want integer",
        ),
        (
            ("--set-string", "cloud_registry.gunicorn.timeout=30"),
            "'/cloud_registry/gunicorn/timeout': got string, want integer",
        ),
        (
            ("--set", "cloud_registry.gunicorn.gracefulTimeout=null"),
            "'/cloud_registry/gunicorn': missing property 'gracefulTimeout'",
        ),
        (
            ("--set", "cloud_registry.gunicorn.keepalive=-1"),
            "'/cloud_registry/gunicorn/keepalive': minimum",
        ),
        (
            ("--set-json", """cloud_registry.gunicorn.extraArgs=["--name='x'"]"""),
            "'/cloud_registry/gunicorn/extraArgs/0': ",
        ),
        (
            ("--set", "cloud_registry.gunicorn.preload=true"),
            "'/cloud_registry/gunicorn': additional properties 'preload'",
        ),
        (
            ("--set", "cloud_registry.gunicorn=null"),
            "'/cloud_registry': missing property 'gunicorn'",
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
def test_gunicorn_invalid(args, error):
    """Gunicorn's settings must be integers in their ranges, further options
    must not contain single quotes, and other settings are rejected.
    """
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render(*args)
    assert error in exc.value.stderr


def _render_extra_args(*extra_args: str) -> dict:
    """Render the chart with further Gunicorn options."""
    return _render(
        "--set-json", f"cloud_registry.gunicorn.extraArgs={json.dumps(extra_args)}"
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
def test_gunicorn_reserved_args(extra_args):
    """Further options must not change the address (the chart's ports),
    preload the app (MongoDB clients must be created after forking) or set
    the graceful timeout (the grace period is derived from
    `gracefulTimeout`), also abbreviated (as Gunicorn accepts them) or with
    attached values; Gunicorn itself would apply them.
    """
    cfg = load_settings(shlex.join(extra_args)).cfg
    assert (cfg.bind, cfg.preload_app, cfg.graceful_timeout) != (
        ["0.0.0.0:8080"],
        False,
        20,
    )
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render_extra_args(*extra_args)
    assert "cloud_registry.gunicorn.extraArgs must not set" in exc.value.stderr
    assert repr(extra_args[0]).strip("'") in exc.value.stderr


def test_gunicorn_other_args():
    """Other further options are passed on, also short ones and option values
    that look like reserved options (which Gunicorn takes as values, as they
    contain spaces or follow `=`).
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


def test_gunicorn_port():
    """The app's container port, its probes and the Service's target port
    are the port Gunicorn listens on (the image's command; the app config's
    `server.port` applies to the development server only).
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


def test_pre_stop_default():
    """By default, the app keeps serving for 5 seconds after its pod is
    deleted (until the ingress controller removes it from its endpoints),
    and the termination grace period covers that, Gunicorn's graceful
    timeout and a margin of 10 seconds.
    """
    docs = _render()
    hook = _container(docs, APP)["lifecycle"]["preStop"]
    assert hook == {"exec": {"command": ["python", "-c", "import time; time.sleep(5)"]}}
    assert _grace_period(docs) == 5 + 20 + 10


def test_pre_stop_values(tmp_path):
    """The delay can be changed (also from values files, which have floats)
    or removed (`0`); the grace period follows it and the graceful timeout.
    """
    values_file = tmp_path / "values.yaml"
    values_file.write_text(
        yaml.safe_dump(
            {
                "cloud_registry": {
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
    docs = _render("--set", "cloud_registry.preStopSleepSeconds=0")
    assert "lifecycle" not in _container(docs, APP)
    assert _grace_period(docs) == 20 + 10


@pytest.mark.parametrize(
    ("value", "error"),
    [
        ("-1", "'/cloud_registry/preStopSleepSeconds': minimum"),
        ("1.5", "'/cloud_registry/preStopSleepSeconds': got number, want integer"),
        ("null", "'/cloud_registry': missing property 'preStopSleepSeconds'"),
    ],
)
def test_pre_stop_invalid(value, error):
    """The delay must be a non-negative integer."""
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render("--set-json", f"cloud_registry.preStopSleepSeconds={value}")
    assert error in exc.value.stderr


def test_security_context_user_ids():
    """MongoDB runs as the image's user `mongodb`, except on OpenShift, which
    assigns user and group IDs, also if they are set in the values.
    """
    ids = {"runAsUser": 999, "runAsGroup": 999, "fsGroup": 999}
    context = _pod(_render(), MONGODB)["securityContext"]
    assert ids.items() <= context.items()
    docs = _render(
        "--api-versions",
        "security.openshift.io/v1",
        "--set",
        "cloud_registry.podSecurityContext.runAsUser=1000",
        "--set",
        "cloud_registry.securityContext.runAsUser=1000",
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


def test_security_context_values():
    """Security contexts can be changed via the values."""
    docs = _render(
        "--set",
        "cloud_registry.securityContext.readOnlyRootFilesystem=false",
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
def test_db_host(release, app):
    """The app connects to the MongoDB Service of its release, unless the
    config sets another host.
    """
    docs = _render(release=release)
    assert _overlay(docs, app)["db"] == {"host": f"{app}-mongodb"}
    assert ("Service", f"{app}-mongodb") in docs
    overlay = _overlay(_render("--set", "config.db.host=external"))
    assert overlay["db"] == {"host": "external"}


def test_ingress():
    """The Ingress routes the host to the app's Service, without class by
    default; its only annotation limits request bodies (for ingress-nginx)
    to the app's limit.
    """
    ingress = _render()[("Ingress", APP)]
    assert ingress["metadata"]["annotations"] == {BODY_SIZE: BODY_SIZE_LIMIT}
    assert "ingressClassName" not in ingress["spec"]
    assert "tls" not in ingress["spec"]
    (rule,) = ingress["spec"]["rules"]
    assert rule["host"] == "x"
    (path,) = rule["http"]["paths"]
    assert path["backend"]["service"] == {"name": APP, "port": {"name": "http"}}


def test_ingress_options():
    """Ingress class and annotations can be set; annotations take precedence
    over those requesting a certificate.
    """
    ingress = _render(
        "--set",
        "cloud_registry.ingress.className=nginx",
        "--set",
        "cloud_registry.ingress.https.enabled=true",
        "--set-json",
        'cloud_registry.ingress.annotations={"a.example.org/b": "c",'
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


def test_ingress_body_size_default():
    """By default, ingress-nginx limits request bodies to the default limit
    of the app, as in the base config.
    """
    config = yaml.safe_load(CONFIG_FILE.read_text())
    assert config["custom"]["max_content_length"] == MAX_CONTENT_LENGTH
    annotations = _render()[("Ingress", APP)]["metadata"]["annotations"]
    assert annotations[BODY_SIZE] == BODY_SIZE_LIMIT


def test_ingress_body_size_app(tmp_path):
    """The size limit of request bodies of ingress-nginx follows that of
    the app, also from values files (which have floats).
    """
    ingress = _render("--set", "config.custom.max_content_length=8388608")
    assert ingress[("Ingress", APP)]["metadata"]["annotations"] == {
        BODY_SIZE: "8388608"
    }
    values_file = tmp_path / "values.yaml"
    config = {"custom": {"max_content_length": 8388608}}
    values_file.write_text(yaml.safe_dump({"config": config}))
    ingress = _render("-f", str(values_file))[("Ingress", APP)]
    assert ingress["metadata"]["annotations"] == {BODY_SIZE: "8388608"}


@pytest.mark.parametrize(
    "args",
    [
        ("--set", "config.custom.max_content_length=null"),
        ("--set", "config.custom.max_content_length=0"),
        ("--set-string", "config.custom.max_content_length=1m"),
    ],
    ids=["null", "zero", "string"],
)
def test_ingress_body_size_invalid(args):
    """Rendering fails unless the app's size limit of request bodies is a
    positive integer (0 would mean no limit for ingress-nginx).
    """
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render(*args)
    assert "max_content_length" in exc.value.stderr


def test_ingress_body_size_custom_null():
    """`config.custom: null` is like no custom config: ingress-nginx gets
    the app's default limit, and the generated custom config is kept.
    """
    docs = _render("--set", "config.custom=null")
    ingress = docs[("Ingress", APP)]
    assert ingress["metadata"]["annotations"] == {BODY_SIZE: BODY_SIZE_LIMIT}
    assert _overlay(docs)["custom"]["endpoints"]["service"]["external_host"] == "x"


def test_ingress_body_size_annotation():
    """The size limit of request bodies set by an annotation takes
    precedence over that of the app; `null` removes it.
    """

    def render(limit):
        annotations = json.dumps({BODY_SIZE: limit})
        args = ("--set-json", f"cloud_registry.ingress.annotations={annotations}")
        return _render(*args)[("Ingress", APP)]

    ingress = render("8m")
    assert ingress["metadata"]["annotations"] == {BODY_SIZE: "8m"}
    ingress = render(None)
    assert "annotations" not in ingress["metadata"]


def test_route():
    """Without an Ingress, an OpenShift Route is created if the cluster
    serves Routes.
    """
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
        (("--set", "cloud_registry.ingress.https.enabled=true"), True, False),
        (ROUTE_API, True, True),
        ((*NO_INGRESS, *ROUTE_API), True, False),
        (NO_INGRESS, False, False),
    ],
    ids=["ingress", "ingress-https", "ingress-route-api", "route", "none"],
)
def test_notes(args, exposed, tls_note):
    """The notes show the app's HTTPS URL (as advertised by the app) with a
    request, noting if the Ingress has no TLS, or warn and show a port
    forward if the app is not exposed.
    """
    notes = render_notes("--set", "host_name=x", "--namespace", "registry", *args)
    url = "https://x" if exposed else "http://localhost:8080"
    assert f'curl -X GET "{url}/ga4gh/registry/v1/service-info"' in notes
    assert ("WARNING" in notes) is not exposed
    assert ("TLS must be terminated in front" in notes) is tls_note
    forward = f"kubectl port-forward svc/{APP} 8080:8080 -n registry\n"
    assert (forward in notes) is not exposed
    assert f"kubectl rollout status deploy/{APP} -n registry" in notes
