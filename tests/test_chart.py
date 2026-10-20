"""Tests for the MongoDB deployment and the probes of the Helm chart."""

import subprocess

import pytest

from tests.test_config import HELM
from tests.test_config import _render as _render_docs

AUTH = ("--set", "mongodb.auth.enabled=true", "--set", "mongodb.auth.existingSecret=s")

pytestmark = pytest.mark.skipif(HELM is None, reason="helm not installed")


def _render(*args: str) -> dict:
    """Render the chart and index the documents by kind and name."""
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in _render_docs("--set", "host_name=x", *args)
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


def test_mongodb_pinned_and_recreated():
    """MongoDB is pinned to a major version and replaced, not rolled."""
    docs = _render()
    assert _container(docs, "mongodb")["image"].endswith(":7.0")
    assert docs[("Deployment", "mongodb")]["spec"]["strategy"] == {"type": "Recreate"}


def test_probes():
    """App and MongoDB have readiness and liveness probes."""
    docs = _render()
    for name in ("cloud-registry", "mongodb"):
        container = _container(docs, name)
        assert {"readinessProbe", "livenessProbe"} <= container.keys()


def test_auth_disabled():
    """Without authentication, no credentials or init script are used."""
    docs = _render()
    assert not _secret_refs(_container(docs, "cloud-registry"))
    assert not _secret_refs(_container(docs, "mongodb"))
    assert ("ConfigMap", "mongo-init-script") not in docs


def test_auth_enabled():
    """With authentication, credentials come from the existing Secret only."""
    docs = _render(*AUTH)
    assert _secret_refs(_container(docs, "cloud-registry")) == {
        "MONGO_USERNAME": ("s", "username"),
        "MONGO_PASSWORD": ("s", "password"),
    }
    assert _secret_refs(_container(docs, "mongodb")) == {
        "MONGO_INITDB_ROOT_USERNAME": ("s", "root-username"),
        "MONGO_INITDB_ROOT_PASSWORD": ("s", "root-password"),
        "MONGO_APP_USERNAME": ("s", "username"),
        "MONGO_APP_PASSWORD": ("s", "password"),
    }
    script = docs[("ConfigMap", "mongo-init-script")]["data"]["create-app-user.js"]
    assert "process.env.MONGO_APP_PASSWORD" in script
    assert not [kind for kind, _ in docs if kind == "Secret"]


def test_auth_requires_secret():
    """Enabling authentication without a Secret fails."""
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render("--set", "mongodb.auth.enabled=true")
    assert "mongodb.auth.existingSecret must be set" in exc.value.stderr
