"""Tests for the MongoDB deployment and the probes of the Helm chart."""

import subprocess

import pytest

from tests.helm import HELM, render

CREDENTIALS = "mongodb-credentials"
AUTH = (
    "--set",
    "mongodb.auth.enabled=true",
    "--set",
    f"mongodb.auth.existingSecret={CREDENTIALS}",
)

pytestmark = pytest.mark.skipif(HELM is None, reason="helm not installed")


def _render(*args: str) -> dict:
    """Render the chart and index the documents by kind and name."""
    return {
        (doc["kind"], doc["metadata"]["name"]): doc
        for doc in render("--set", "host_name=x", *args)
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
    """App and MongoDB have probes on their ports."""
    docs = _render()
    app = _container(docs, "cloud-registry")
    for probe in ("readinessProbe", "livenessProbe"):
        assert app[probe]["httpGet"] == {"path": "/ga4gh/registry/v1/ui/", "port": 8080}
    mongodb = _container(docs, "mongodb")
    for probe in ("startupProbe", "readinessProbe", "livenessProbe"):
        assert mongodb[probe]["tcpSocket"] == {"port": 27017}


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
    """Without authentication, no credentials or init script are used."""
    docs = _render()
    assert not _secret_refs(_container(docs, "cloud-registry"))
    assert not _secret_refs(_container(docs, "mongodb"))
    assert ("ConfigMap", "mongo-init-script") not in docs
    assert _init_script_volumes(docs) == ([], [])


def test_auth_enabled():
    """With authentication, credentials come from the existing Secret only."""
    docs = _render(*AUTH)
    assert _secret_refs(_container(docs, "cloud-registry")) == {
        "MONGO_USERNAME": (CREDENTIALS, "username"),
        "MONGO_PASSWORD": (CREDENTIALS, "password"),
    }
    assert _secret_refs(_container(docs, "mongodb")) == {
        "MONGO_INITDB_ROOT_USERNAME": (CREDENTIALS, "root-username"),
        "MONGO_INITDB_ROOT_PASSWORD": (CREDENTIALS, "root-password"),
        "MONGO_APP_USERNAME": (CREDENTIALS, "username"),
        "MONGO_APP_PASSWORD": (CREDENTIALS, "password"),
    }
    script = docs[("ConfigMap", "mongo-init-script")]["data"]["create-app-user.js"]
    assert "process.env.MONGO_APP_PASSWORD" in script
    volumes, mounts = _init_script_volumes(docs)
    assert [vol["configMap"]["name"] for vol in volumes] == ["mongo-init-script"]
    assert [mount["mountPath"] for mount in mounts] == ["/docker-entrypoint-initdb.d"]
    assert not [kind for kind, _ in docs if kind == "Secret"]


def test_auth_requires_secret():
    """Enabling authentication without a Secret fails."""
    with pytest.raises(subprocess.CalledProcessError) as exc:
        _render("--set", "mongodb.auth.enabled=true")
    assert "mongodb.auth.existingSecret must be set" in exc.value.stderr
