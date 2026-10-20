"""Tests for the app config and the config overlay rendered by the Helm chart."""

from pathlib import Path
import shutil
import subprocess

from foca.config.config_parser import ConfigParser
from foca.models.config import Config
import pytest
import yaml

ROOT = Path(__file__).parents[1]
CONFIG_FILE = ROOT / "cloud_registry" / "config.yaml"
CHART = ROOT / "deployment"
CUSTOM_CONFIG_MODEL = "cloud_registry.service_models.custom_config.CustomConfig"
HELM = shutil.which("helm")


def _load_config(overlay: dict, tmp_path: Path, monkeypatch) -> Config:
    """Load the base config with an overlay."""
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(overlay))
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(path))
    return ConfigParser(
        config_file=CONFIG_FILE,
        custom_config_model=CUSTOM_CONFIG_MODEL,
        format_logs=False,
    ).config


def _render_overlay(*args: str) -> dict:
    """Render the chart's config overlay with `helm template` arguments."""
    assert HELM is not None
    rendered = subprocess.run(  # noqa: S603
        [HELM, "template", "cloud-registry", str(CHART), *args],
        capture_output=True,
        check=True,
        text=True,
    ).stdout
    overlays = [
        doc["data"]["overlay.yaml"]
        for doc in yaml.safe_load_all(rendered)
        if doc and doc["kind"] == "ConfigMap" and "overlay.yaml" in doc["data"]
    ]
    assert len(overlays) == 1
    return yaml.safe_load(overlays[0])


def test_config_overlay(tmp_path, monkeypatch):
    """An overlay merges mappings recursively and replaces other values,
    including lists.
    """
    config = _load_config(
        {
            "security": {"auth": {"audience": ["a"], "verify_audience": True}},
            "custom": {"endpoints": {"service": {"external_host": "example.org"}}},
        },
        tmp_path,
        monkeypatch,
    )
    assert config.security.auth.audience == ["a"]
    assert config.security.auth.verify_audience is True
    assert config.security.auth.issuers  # kept from the base config
    assert config.custom.endpoints.service.external_host == "example.org"
    assert config.custom.endpoints.service.external_port == 8080
    assert config.db.host == "mongodb"


def test_config_overlay_missing(tmp_path, monkeypatch):
    """A missing overlay file is an error."""
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(tmp_path / "missing.yaml"))
    with pytest.raises(FileNotFoundError):
        ConfigParser(config_file=CONFIG_FILE, format_logs=False)


@pytest.mark.skipif(HELM is None, reason="helm not installed")
class TestChartOverlay:
    """Tests for the config overlay rendered by the Helm chart."""

    def test_host_name(self, tmp_path, monkeypatch):
        """`host_name` sets the public URL."""
        overlay = _render_overlay("--set", "host_name=registry.example.org")
        service = _load_config(overlay, tmp_path, monkeypatch).custom.endpoints.service
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
        config = _load_config(overlay, tmp_path, monkeypatch)
        assert config.custom.endpoints.service.external_host == "registry.example.org"
        assert config.custom.endpoints.service.external_port == 8443
        assert config.security.auth.audience == ["test-client"]

    def test_host_name_required(self):
        """Rendering fails without `host_name`."""
        with pytest.raises(subprocess.CalledProcessError) as exc:
            _render_overlay()
        assert "host_name" in exc.value.stderr
