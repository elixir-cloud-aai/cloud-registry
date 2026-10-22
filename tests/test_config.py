"""Tests for the app config and the config overlay rendered by the Helm chart."""

import fnmatch
import os
from pathlib import Path
import re
import subprocess
import tomllib
from typing import Any

from foca.config.config_parser import ConfigParser
from foca.models.config import Config, IndexConfig
from packaging.requirements import Requirement
from pydantic import ValidationError
import pytest
import yaml

from cloud_registry.ga4gh.registry.service import KEY
from cloud_registry.service_models.custom_config import CustomConfig
from tests.conftest import CONFIG_OVERLAY
from tests.helm import (
    ADMIN_GROUP,
    AUDIENCE,
    CHART,
    HELM,
    Manifest,
    render,
)

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


def _custom(config: Config) -> CustomConfig:
    """Get the custom config, which FOCA keeps as an untyped extra field.

    Args:
        config: Parsed config.

    Returns:
        The custom config.
    """
    custom = (config.model_extra or {}).get("custom")
    assert isinstance(custom, CustomConfig)
    return custom


def _load_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *overlays: dict[str, Any]
) -> Config:
    """Load the base config with the test overlay and further overlays.

    Args:
        tmp_path: Directory to write the overlays to.
        monkeypatch: Fixture to set `FOCA_CONFIG_OVERLAY` with.
        overlays: Further overlays, in order of precedence.

    Returns:
        The parsed config.
    """
    paths = [str(CONFIG_OVERLAY)]
    for index, overlay in enumerate(overlays):
        path = tmp_path / f"overlay{index}.yaml"
        path.write_text(yaml.safe_dump(overlay))
        paths.append(str(path))
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", os.pathsep.join(paths))
    return _parse_config()


def _get_overlay_configmap(docs: list[Manifest]) -> Manifest:
    """Get the ConfigMap holding the config overlay from rendered docs.

    Args:
        docs: Rendered manifests.

    Returns:
        The ConfigMap.
    """
    configmaps = [
        doc
        for doc in docs
        if doc["kind"] == "ConfigMap" and "overlay.yaml" in doc["data"]
    ]
    assert len(configmaps) == 1
    return configmaps[0]


def _render_overlay(*args: str, **kwargs: Any) -> dict[str, Any]:
    """Render the chart's config overlay with `helm template` arguments.

    Args:
        args: Arguments of `helm template`.
        kwargs: Keyword arguments of `tests.helm.render()`.

    Returns:
        The config overlay.
    """
    docs = render(*args, **kwargs)
    overlay: dict[str, Any] = yaml.safe_load(
        _get_overlay_configmap(docs)["data"]["overlay.yaml"]
    )
    return overlay


def _keys(index: IndexConfig) -> dict[str, Any]:
    """Get the keys of an index as a mapping of fields to directions.

    Args:
        index: Index config.

    Returns:
        Directions of the indexed fields, by field.
    """
    assert index.keys is not None
    return dict(index.keys)


def test_config_overlay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An overlay merges mappings recursively and replaces other values.

    Lists are replaced too.
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
    assert config.security.auth.verify_token_type is True  # kept from the base
    assert _custom(config).endpoints.service.external_host == "example.org"
    assert _custom(config).endpoints.service.external_port == 8080
    assert config.db is not None
    assert config.db.host == "mongodb"


def test_config_overlays(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Later overlays take precedence."""
    service = {"custom": {"endpoints": {"service": {"external_host": "first"}}}}
    later = {"custom": {"endpoints": {"service": {"external_host": "second"}}}}
    config = _load_config(tmp_path, monkeypatch, service, later)
    assert _custom(config).endpoints.service.external_host == "second"


def test_config_no_identity_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    """The base config trusts no identity provider and maps no groups.

    Issuers, audience, claims and group mappings are deployment
    configuration, without defaults, so the base config alone is invalid, as
    access control requires trusted issuers (FOCA).
    """
    base = yaml.safe_load(CONFIG_FILE.read_text())
    auth = base["security"]["auth"]
    assert auth["issuers"] == []
    assert not {"audience", "userinfo_claims", "clients"} & set(auth)
    access_control = base["security"]["access_control"]
    assert not {"claim_groups", "group_mapping"} & set(access_control)
    monkeypatch.delenv("FOCA_CONFIG_OVERLAY", raising=False)
    with pytest.raises(ValidationError, match=r"auth\.issuers"):
        _parse_config()


def test_config_requires_audience(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Deployments that set issuers must set the audience (FOCA)."""
    overlay = {"security": {"auth": {"issuers": ["https://login.example.org/"]}}}
    path = tmp_path / "overlay.yaml"
    path.write_text(yaml.safe_dump(overlay))
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(path))
    with pytest.raises(ValidationError, match="audience"):
        _parse_config()


# Values specific to an identity provider or deployment, which the
# repository must not contain (examples use fictitious values)
IDP_SPECIFIC = re.compile(
    # Names not preceded or followed by letters (e.g., also `perun_groups`)
    r"lifescience|(?<![a-z])ls[ -]login(?![a-z])|eduperson|(?<![a-z])perun(?![a-z])"
    r"|(?<![a-z])ecp(?![a-z])|urn:geant",
    re.IGNORECASE,
)


def _text_lines(path: Path) -> list[str]:
    """Get the lines of a file, or none if it is not text (UTF-8).

    Args:
        path: The file.

    Returns:
        Its lines.
    """
    try:
        return path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        return []


def test_text_lines(tmp_path: Path) -> None:
    """Files that aren't UTF-8 text (e.g., images) have no lines."""
    (tmp_path / "text.md").write_text("a\nb\n")
    (tmp_path / "image.png").write_bytes(b"\x89PNG\r\n\x1a\n\xff")
    assert _text_lines(tmp_path / "text.md") == ["a", "b"]
    assert _text_lines(tmp_path / "image.png") == []


def test_repository_generic() -> None:
    """No file of the repository has identity provider specific values.

    Guides for specific identity providers or deployments are kept
    elsewhere.
    """
    files = (
        subprocess.run(
            ["git", "ls-files", "-z"],  # noqa: S607 (git from PATH)
            capture_output=True,
            check=True,
            cwd=ROOT,
        )
        .stdout.decode()
        .split("\0")
    )
    found = [
        f"{name}:{number}: {line.strip()}"
        for name in files
        # This file names the values to find
        if name not in ("", "tests/test_config.py") and (ROOT / name).is_file()
        for number, line in enumerate(_text_lines(ROOT / name), start=1)
        if IDP_SPECIFIC.search(line)
    ]
    assert found == []


def test_config_dev_overlay(monkeypatch: pytest.MonkeyPatch) -> None:
    """The Docker Compose overlay is permissive.

    It accepts tokens for any audience, does not require admin groups and
    allows `http` service URLs (e.g., of services on `localhost`).
    """
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(ROOT / "config.dev.yaml"))
    config = _parse_config()
    assert config.security.auth.verify_audience is False
    assert _custom(config).access_control.require_admin is False
    assert _custom(config).endpoints.services.url_schemes == ["http", "https"]


def test_config_service_info_placeholders(monkeypatch: pytest.MonkeyPatch) -> None:
    """The base config's service info names no real organization.

    Its `id` and `organization` are placeholders with reserved example domains
    (RFC 2606), and it sets no contact.
    """
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(CONFIG_OVERLAY))
    service_info = _parse_config().service_info
    assert service_info is not None
    info = service_info.service
    assert info["id"] == "org.example.cloud-registry"
    assert info["organization"] == {
        "name": "Example Organization",
        "url": "https://example.org",
    }
    assert "contactUrl" not in info
    assert "elixir-on" not in CONFIG_FILE.read_text()


def test_config_url_schemes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The base config allows `https` service URLs only."""
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(CONFIG_OVERLAY))
    assert _custom(_parse_config()).endpoints.services.url_schemes == ["https"]


def test_config_resource_roles(monkeypatch: pytest.MonkeyPatch) -> None:
    """The roles on services are those of the API, and set in the config.

    FOCA's ownership helpers grant and revoke the roles of
    `security.access_control.resource_roles`, which the API lists as `Role`.
    """
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(CONFIG_OVERLAY))
    spec = yaml.safe_load(
        (ROOT / "cloud_registry" / "api" / "additions.openapi.yaml").read_text()
    )
    roles = spec["components"]["schemas"]["Role"]["enum"]
    assert (
        "resource_roles"
        in yaml.safe_load(CONFIG_FILE.read_text())["security"]["access_control"]
    )
    assert _parse_config().security.access_control.resource_roles == roles


def test_service_key_index(monkeypatch: pytest.MonkeyPatch) -> None:
    """Service keys are unique, and the index is sparse.

    Being sparse, the index can be created before keys are added to services
    stored without one.
    """
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(CONFIG_OVERLAY))
    db = _parse_config().db
    assert db is not None and db.dbs is not None
    collections = db.dbs["serviceStore"].collections
    assert collections is not None
    indexes = collections["services"].indexes
    assert indexes is not None
    key_index = next(index for index in indexes if _keys(index) == {KEY: 1})
    assert key_index.options == {"unique": True, "sparse": True}


def test_config_overlay_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing overlay file is an error."""
    monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(tmp_path / "missing.yaml"))
    with pytest.raises(FileNotFoundError):
        _parse_config()


@pytest.mark.skipif(HELM is None, reason="helm not installed")
class TestChartOverlay:
    """Tests for the config overlay rendered by the Helm chart."""

    def test_host_name(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """`hostName` sets the public URL."""
        overlay = _render_overlay("--set", "hostName=registry.example.org")
        config = _load_config(tmp_path, monkeypatch, overlay)
        service = _custom(config).endpoints.service
        assert (service.url_prefix, service.external_host, service.external_port) == (
            "https",
            "registry.example.org",
            443,
        )

    def test_config_precedence(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Values in `config` take precedence over generated values."""
        overlay = _render_overlay(
            "-f",
            str(CHART / "ci" / "test-values.yaml"),
            "--set",
            "config.custom.endpoints.service.external_port=8443",
        )
        config = _load_config(tmp_path, monkeypatch, overlay)
        assert _custom(config).endpoints.service.external_host == (
            "registry.example.org"
        )
        assert _custom(config).endpoints.service.external_port == 8443
        assert config.security.auth.audience == ["test-client"]

    def test_service_info(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Service info set via `config` is merged into the base config."""
        overlay = _render_overlay(
            "-f",
            str(CHART / "ci" / "test-values.yaml"),
            "--set",
            "config.service_info.service.environment=prod",
        )
        config = _load_config(tmp_path, monkeypatch, overlay)
        assert config.service_info is not None
        info = config.service_info.service
        assert info["environment"] == "prod"
        assert info["id"] == "org.example.cloud-registry"

    def test_admin_group_required(self) -> None:
        """Rendering fails without a group mapped to the role `admin`."""
        with pytest.raises(subprocess.CalledProcessError) as exc:
            _render_overlay("--set", "hostName=example.org", admin_group=None)
        assert "must map a group to the role admin" in exc.value.stderr

    @pytest.mark.parametrize(
        "args",
        [
            (),
            ("--set-json", "config.security.auth.issuers=[]"),
            ("--set-json", "config.security.auth.issuers=null"),
            ("--set-json", 'config.security.auth.issuers=[" "]'),
            ("--set-json", 'config.security.auth.issuers=["https://x.org/", ""]'),
            ("--set-json", "config.security.auth.issuers=[1]"),
            ("--set-json", 'config.security.auth.issuers=[" https://x.org/"]'),
            ("--set-string", "config.security.auth.issuers=https://x.org/"),
            ("--set-json", "config.security.auth=null"),
        ],
        ids=[
            "missing",
            "empty",
            "null",
            "blank",
            "one-blank",
            "number",
            "padded",
            "string",
            "auth-null",
        ],
    )
    def test_issuers_required(self, args: tuple[str, ...]) -> None:
        """Rendering fails without trusted token issuers.

        The base config trusts none, and FOCA refuses access control without
        them; checked before the audience.
        """
        with pytest.raises(subprocess.CalledProcessError) as exc:
            _render_overlay(
                "--set", "hostName=example.org", *args, issuer=None, audience=None
            )
        assert "config.security.auth.issuers must" in exc.value.stderr

    def test_issuers(self) -> None:
        """The issuers and the group claim are passed to the app.

        Checked in the rendered overlay, as the test overlay sets them, too.
        """
        overlay = _render_overlay(
            "--set",
            "hostName=example.org",
            issuer="https://issuer.example.org/",
            group_claim="memberships",
        )
        assert overlay["security"]["auth"]["issuers"] == ["https://issuer.example.org/"]
        assert overlay["security"]["access_control"]["claim_groups"] == "memberships"

    @pytest.mark.parametrize(
        "args",
        [(), ("--set", "config.security.access_control.claim_groups= ")],
        ids=["missing", "blank"],
    )
    def test_group_claim_required(self, args: tuple[str, ...]) -> None:
        """Rendering fails with group mappings, but without a group claim.

        As the app on startup, as no mapping would apply.
        """
        with pytest.raises(subprocess.CalledProcessError) as exc:
            _render_overlay("--set", "hostName=example.org", *args, group_claim=None)
        assert "config.security.access_control.claim_groups must" in exc.value.stderr

    @pytest.mark.parametrize(
        "value", ['["groups"]', "123", "true", "false", "0", '" groups"', '"  "']
    )
    def test_group_claim_invalid(self, value: str) -> None:
        """Group claims that the app rejects are rejected.

        Those that aren't strings (FOCA's config), and those with surrounding
        whitespace, as FOCA looks up the claim as written. Checked after the
        issuers.
        """
        with pytest.raises(subprocess.CalledProcessError) as exc:
            _render_overlay(
                "--set",
                "hostName=example.org",
                "--set-json",
                f"config.security.access_control.claim_groups={value}",
            )
        assert "claim_groups must be the name of a claim" in exc.value.stderr
        with pytest.raises(subprocess.CalledProcessError) as exc:
            _render_overlay(
                "--set",
                "hostName=example.org",
                "--set-json",
                f"config.security.access_control.claim_groups={value}",
                issuer=None,
            )
        assert "config.security.auth.issuers must" in exc.value.stderr

    def test_admin_group_optional(self) -> None:
        """Without admin groups, rendering works if admins are not required."""
        overlay = _render_overlay(
            "--set",
            "hostName=example.org",
            "--set",
            "config.custom.access_control.require_admin=false",
            admin_group=None,
            group_claim=None,
        )
        assert overlay["custom"]["access_control"] == {"require_admin": False}

    @pytest.mark.parametrize(
        "args",
        [
            (),
            ("--set-json", "config.security.auth.audience=[]"),
            ("--set-json", 'config.security.auth.audience=[" "]'),
            ("--set-json", "config.security.auth.audience=null"),
            ("--set", "config.security.auth.verify_audience=true"),
            ("--set-string", "config.security.auth.verify_audience=true"),
            ("--set-json", "config.security.auth.verify_audience=null"),
            ("--set-json", 'config.security.auth.issuers=["https://x.org/"]'),
        ],
        ids=[
            "missing",
            "empty",
            "blank",
            "null",
            "verified",
            "string",
            "null_verify",
            "issuers",
        ],
    )
    def test_audience_required(self, args: tuple[str, ...]) -> None:
        """Rendering fails without an audience.

        The app would refuse to start, as FOCA requires one for trusted
        issuers.
        """
        with pytest.raises(subprocess.CalledProcessError) as exc:
            _render_overlay("--set", "hostName=example.org", *args, audience=None)
        assert "config.security.auth.audience must" in exc.value.stderr

    @pytest.mark.parametrize(
        "args",
        [
            ("--set", "config.security.auth.verify_audience=false"),
            ("--set-string", "config.security.auth.verify_audience=false"),
            ("--set-string", "config.security.auth.verify_audience=False"),
            ("--set-string", "config.security.auth.verify_audience=no"),
            ("--set", "config.security.auth.verify_audience=0"),
        ],
        ids=["disabled", "string", "capitalized", "no", "zero"],
    )
    def test_audience_optional(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]
    ) -> None:
        """Without audience validation, no audience is needed.

        As for the app, which parses `verify_audience` as pydantic does
        booleans.
        """
        overlay = _render_overlay("--set", "hostName=example.org", *args, audience=None)
        # without the test overlay, which sets an audience
        path = tmp_path / "overlay.yaml"
        path.write_text(yaml.safe_dump(overlay))
        monkeypatch.setenv("FOCA_CONFIG_OVERLAY", str(path))
        assert _parse_config().security.auth.audience is None

    def test_audience(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The audience is passed to the app."""
        overlay = _render_overlay("--set", "hostName=example.org")
        config = _load_config(tmp_path, monkeypatch, overlay)
        assert config.security.auth.audience == [AUDIENCE]

    def test_admin_group(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """The admin group is passed to the app."""
        overlay = _render_overlay("--set", "hostName=example.org")
        config = _load_config(tmp_path, monkeypatch, overlay)
        group_mapping = config.security.access_control.group_mapping
        assert group_mapping is not None
        assert group_mapping[ADMIN_GROUP] == "admin"

    def test_host_name_required(self) -> None:
        """Rendering fails without `hostName`."""
        with pytest.raises(subprocess.CalledProcessError) as exc:
            _render_overlay()
        assert "hostName must be set" in exc.value.stderr

    @pytest.mark.parametrize(
        "value",
        [
            "config=foo",
            "hostName=1",
            "cloudRegistry.ingress.className=1",
            "cloudRegistry.ingress.annotations.a=1",
            "cloudRegistry.ingress.class=nginx",
            "cloudRegistry.ingress.https.secret=tls",
            "cloudRegistry.tmpSizeLimit=64MB",
            "cloudRegistry.tmpSizeLimit=",
            "cloudRegistry.tmpSizeLimit=0",
            "mongodb.tmpSizeLimit=1K",
            "mongodb.tmpSizeLimit=0Mi",
        ],
        ids=[
            "config",
            "hostName",
            "ingress_class_name",
            "ingress_annotation",
            "ingress_unknown",
            "ingress_https_unknown",
            "tmp_size_limit_unit",
            "tmp_size_limit_empty",
            "tmp_size_limit_zero",
            "mongodb_tmp_size_limit_unit",
            "mongodb_tmp_size_limit_zero",
        ],
    )
    def test_values_schema(self, value: str) -> None:
        """Values of the wrong type are rejected."""
        with pytest.raises(subprocess.CalledProcessError) as exc:
            _render_overlay("--set", "hostName=example.org", "--set", value)
        assert "values don't meet the specifications of the schema" in exc.value.stderr

    def test_deployment_mounts_overlay(self) -> None:
        """The app reads the overlay from the mounted ConfigMap."""
        docs = render("--set", "hostName=example.org")
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


def test_package_data() -> None:
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


def _foca_requirement() -> Requirement:
    """Get the `foca` requirement of the app (`pyproject.toml`)."""
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    (foca,) = [
        requirement
        for requirement in map(Requirement, pyproject["project"]["dependencies"])
        if requirement.name == "foca"
    ]
    return foca


def _base_image_tag() -> str:
    """Get the tag of the FOCA base image (`Dockerfile`), without Python."""
    match = re.search(
        r"^FROM elixircloud/foca:(?P<version>[^-@\s]+)",
        (ROOT / "Dockerfile").read_text(),
        flags=re.MULTILINE,
    )
    assert match, "No FOCA base image in the Dockerfile"
    return match["version"]


def test_base_image_foca() -> None:
    """The base image has the FOCA that the `foca` requirement pins.

    So the image build does not install another FOCA on top of it: a FOCA
    release that satisfies the requirement or, for a direct reference to a
    FOCA commit (temporary, see `scripts/check_release_pins.py`), FOCA's
    development image (tagged by date) of that commit, which the `Dockerfile`
    records; the image build checks the FOCA version of the image
    (`scripts/image_requirements.py`).
    """
    foca, tag = _foca_requirement(), _base_image_tag()
    if foca.url is None:
        assert foca.specifier.contains(tag), (
            f"Base image has FOCA {tag}, but {foca} is required"
        )
    else:
        commit = re.fullmatch(
            r"git\+https://github\.com/elixir-cloud-aai/foca@(?P<sha>[0-9a-f]{40})",
            foca.url,
        )
        assert commit, f"FOCA must be pinned to a full commit SHA, not {foca.url}"
        assert re.fullmatch(r"[0-9]{8}", tag), (
            "A FOCA commit needs FOCA's development image (tagged by date)"
        )
        dockerfile = (ROOT / "Dockerfile").read_text()
        assert f"## FOCA commit: {commit['sha']}\n" in dockerfile, (
            "The Dockerfile must record the FOCA commit of its base image"
        )


def test_python_version() -> None:
    """The app targets one Python version, that of the base image.

    That is, the Python version of the base image in the `Dockerfile` is the
    minimum of `requires-python`, the only one in the classifiers and the one
    of mypy, and all workflows and Read the Docs use it.
    """
    match = re.search(
        r"^FROM elixircloud/foca:[^-@\s]+-py(?P<version>3\.\d+)@",
        (ROOT / "Dockerfile").read_text(),
        flags=re.MULTILINE,
    )
    assert match, "No Python version in the base image tag of the Dockerfile"
    image = match["version"]
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert pyproject["project"]["requires-python"] == f">={image}"
    assert pyproject["tool"]["mypy"]["python_version"] == image
    assert pyproject["tool"]["ruff"]["target-version"] == f"py{image.replace('.', '')}"
    prefix = "Programming Language :: Python :: 3."
    classifiers = [
        classifier.removeprefix("Programming Language :: Python :: ")
        for classifier in pyproject["project"]["classifiers"]
        if classifier.startswith(prefix)
    ]
    assert classifiers == [image]
    workflows = sorted((ROOT / ".github" / "workflows").glob("*.yaml"))
    assert workflows
    for workflow in workflows:
        for name, job in yaml.safe_load(workflow.read_text())["jobs"].items():
            for step in job.get("steps", []):
                if "actions/setup-python@" in step.get("uses", ""):
                    version = step["with"]["python-version"]
                    assert version == image, f"{workflow.name}: {name}"
    readthedocs = yaml.safe_load((ROOT / ".readthedocs.yaml").read_text())
    assert readthedocs["build"]["tools"]["python"] == image
