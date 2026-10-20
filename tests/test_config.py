"""Tests for the app config and the config overlay rendered by the Helm chart."""

import fnmatch
import os
from pathlib import Path
import subprocess
import sys

from foca.config.config_parser import ConfigParser
from foca.models.config import Config
from pydantic import ValidationError
import pytest
import yaml

from tests.conftest import CONFIG_OVERLAY
from tests.helm import ADMIN_GROUP, CHART, HELM, render

if sys.version_info >= (3, 11):
    import tomllib
else:  # pytest depends on tomli on Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).parents[1]
CONFIG_FILE = ROOT / "cloud_registry" / "config.yaml"
CUSTOM_CONFIG_MODEL = "cloud_registry.service_models.custom_config.CustomConfig"


def _parse_config() -> Config:
    """Parse the base config, with overlays from `FOCA_CONFIG_OVERLAY`."""
    return ConfigParser(
        config_file=CONFIG_FILE,
        custom_config_model=CUSTOM_CONFIG_MODEL,
        format_logs=False,
    ).config


def _load_config(tmp_path: Path, monkeypatch, *overlays: dict) -> Config:
    """Load the base config with the test overlay and further overlays."""
    paths = [str(CONFIG_OVERLAY)]
    for index, overlay in enumerate(overlays):
        path = tmp_path / f"overlay{index}.yaml"
        path.write_text(yaml.safe_dump(overlay))
        paths.append(str(path))
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", os.pathsep.join(paths))
    return _parse_config()


def _get_overlay_configmap(docs: list[dict]) -> dict:
    """Get the ConfigMap holding the config overlay from rendered docs."""
    configmaps = [
        doc
        for doc in docs
        if doc["kind"] == "ConfigMap" and "overlay.yaml" in doc["data"]
    ]
    assert len(configmaps) == 1
    return configmaps[0]


def _render_overlay(*args: str, **kwargs) -> dict:
    """Render the chart's config overlay with `helm template` arguments."""
    docs = render(*args, **kwargs)
    return yaml.safe_load(_get_overlay_configmap(docs)["data"]["overlay.yaml"])


def test_config_overlay(tmp_path, monkeypatch):
    """An overlay merges mappings recursively and replaces other values,
    including lists.
    """
    config = _load_config(
        tmp_path,
        monkeypatch,
        {
            "security": {"auth": {"audience": ["a"], "verify_audience": True}},
            "custom": {"endpoints": {"service": {"external_host": "example.org"}}},
        },
    )
    assert config.security.auth.audience == ["a"]
    assert config.security.auth.verify_audience is True
    assert config.security.auth.issuers  # kept from the base config
    assert config.custom.endpoints.service.external_host == "example.org"
    assert config.custom.endpoints.service.external_port == 8080
    assert config.db.host == "mongodb"


def test_config_overlays(tmp_path, monkeypatch):
    """Later overlays take precedence."""
    service = {"custom": {"endpoints": {"service": {"external_host": "first"}}}}
    later = {"custom": {"endpoints": {"service": {"external_host": "second"}}}}
    config = _load_config(tmp_path, monkeypatch, service, later)
    assert config.custom.endpoints.service.external_host == "second"


def test_config_requires_audience(monkeypatch):
    """The base config alone is invalid: deployments must set the audience."""
    monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
    with pytest.raises(ValidationError, match="audience"):
        _parse_config()


def test_config_dev_overlay(monkeypatch):
    """The Docker Compose overlay accepts tokens for any audience and does not
    require admin groups.
    """
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(ROOT / "config.dev.yaml"))
    config = _parse_config()
    assert config.security.auth.verify_audience is False
    assert config.custom.access_control.require_admin is False


def test_config_overlay_missing(tmp_path, monkeypatch):
    """A missing overlay file is an error."""
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(tmp_path / "missing.yaml"))
    with pytest.raises(FileNotFoundError):
        _parse_config()


@pytest.mark.skipif(HELM is None, reason="helm not installed")
class TestChartOverlay:
    """Tests for the config overlay rendered by the Helm chart."""

    def test_host_name(self, tmp_path, monkeypatch):
        """`host_name` sets the public URL."""
        overlay = _render_overlay("--set", "host_name=registry.example.org")
        config = _load_config(tmp_path, monkeypatch, overlay)
        service = config.custom.endpoints.service
        assert (service.url_prefix, service.external_host, service.external_port) == (
            "https",
            "registry.example.org",
            443,
        )

    def test_config_precedence(self, tmp_path, monkeypatch):
        """Values in `config` take precedence over generated values."""
        overlay = _render_overlay(
            "-f",
            str(CHART / "ci" / "test-values.yaml"),
            "--set",
            "config.custom.endpoints.service.external_port=8443",
        )
        config = _load_config(tmp_path, monkeypatch, overlay)
        assert config.custom.endpoints.service.external_host == "registry.example.org"
        assert config.custom.endpoints.service.external_port == 8443
        assert config.security.auth.audience == ["test-client"]

    def test_service_info(self, tmp_path, monkeypatch):
        """Service info set via `config` is merged into the base config."""
        overlay = _render_overlay(
            "-f",
            str(CHART / "ci" / "test-values.yaml"),
            "--set",
            "config.custom.endpoints.service_info.environment=prod",
        )
        config = _load_config(tmp_path, monkeypatch, overlay)
        info = config.custom.endpoints.service_info
        assert info["environment"] == "prod"
        assert info["id"] == "ELIXIR_CLOUD_SERVICE_REGISTRY_1"

    def test_admin_group_required(self):
        """Rendering fails without a group mapped to the role `admin`."""
        with pytest.raises(subprocess.CalledProcessError) as exc:
            _render_overlay("--set", "host_name=example.org", admin_group=None)
        assert "must map a group to the role admin" in exc.value.stderr

    def test_admin_group_optional(self):
        """Without admin groups, rendering works if admins are not required."""
        overlay = _render_overlay(
            "--set",
            "host_name=example.org",
            "--set",
            "config.custom.access_control.require_admin=false",
            admin_group=None,
        )
        assert overlay["custom"]["access_control"] == {"require_admin": False}

    def test_admin_group(self, tmp_path, monkeypatch):
        """The admin group is passed to the app."""
        overlay = _render_overlay("--set", "host_name=example.org")
        config = _load_config(tmp_path, monkeypatch, overlay)
        assert config.security.access_control.group_mapping[ADMIN_GROUP] == "admin"

    def test_host_name_required(self):
        """Rendering fails without `host_name`."""
        with pytest.raises(subprocess.CalledProcessError) as exc:
            _render_overlay()
        assert "host_name must be set" in exc.value.stderr

    @pytest.mark.parametrize(
        "value",
        [
            "config=foo",
            "host_name=1",
            "cloud_registry.ingress.className=1",
            "cloud_registry.ingress.annotations.a=1",
            "cloud_registry.ingress.class=nginx",
            "cloud_registry.ingress.https.secret=tls",
        ],
        ids=[
            "config",
            "host_name",
            "ingress_class_name",
            "ingress_annotation",
            "ingress_unknown",
            "ingress_https_unknown",
        ],
    )
    def test_values_schema(self, value):
        """Values of the wrong type are rejected."""
        with pytest.raises(subprocess.CalledProcessError) as exc:
            _render_overlay("--set", "host_name=example.org", "--set", value)
        assert "values don't meet the specifications of the schema" in exc.value.stderr

    def test_deployment_mounts_overlay(self):
        """The app reads the overlay from the mounted ConfigMap."""
        docs = render("--set", "host_name=example.org")
        configmap = _get_overlay_configmap(docs)
        pod = next(
            doc
            for doc in docs
            if doc["kind"] == "Deployment"
            and doc["metadata"]["name"] == "cloud-registry"
        )["spec"]["template"]["spec"]
        container = pod["containers"][0]
        env = {var["name"]: var.get("value") for var in container["env"]}
        mount = next(
            mount
            for mount in container["volumeMounts"]
            if Path(env["FOCA_CONFIG_OVERLAY"]).parent == Path(mount["mountPath"])
        )
        volume = next(vol for vol in pod["volumes"] if vol["name"] == mount["name"])
        assert volume["configMap"]["name"] == configmap["metadata"]["name"]
        assert Path(env["FOCA_CONFIG_OVERLAY"]).name == "overlay.yaml"


def test_package_data():
    """Files that the base config references are installed with the package."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    patterns = pyproject["tool"]["setuptools"]["package-data"]["cloud_registry"]
    config = yaml.safe_load(CONFIG_FILE.read_text())
    files = [
        config["security"]["access_control"]["model"],
        *(path for spec in config["api"]["specs"] for path in spec["path"]),
    ]
    for path in ["config.yaml", *files]:
        assert (ROOT / "cloud_registry" / path).is_file(), path
        # setuptools matches the patterns from the package directory
        assert any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns), path
