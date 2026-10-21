"""Tests of the shared fixtures (`tests/conftest.py`)."""

import os
import socket
from typing import Any

import mongomock
from pymongo import MongoClient
import pytest

from tests import conftest
from tests.mock_data import DB


def test_db_client(db_client: MongoClient[dict[str, Any]]) -> None:
    """Tests use MongoDB at `MONGODB_TEST_URI` if it is set, else mongomock."""
    uri = os.environ.get("MONGODB_TEST_URI")
    assert isinstance(db_client, mongomock.MongoClient) is not bool(uri)
    if uri:
        assert db_client.server_info()["version"]
    assert isinstance(db_client[DB]["services"], conftest.COLLECTION_CLASS)


@pytest.mark.parametrize("run", [1, 2])
def test_db_client_empty(db_client: MongoClient[dict[str, Any]], run: int) -> None:
    """Each test starts with an empty database `DB`."""
    services = db_client[DB]["services"]
    assert services.count_documents({}) == 0
    services.insert_one({"id": f"run{run}"})


def test_db_addresses() -> None:
    """The addresses of a URI's hosts are resolved; default port 27017."""
    assert conftest._db_addresses("") == frozenset()
    assert conftest._db_addresses(
        "mongodb://127.0.0.1:12345,127.0.0.2/?replicaSet=rs"
    ) == {
        ("127.0.0.1", 12345),
        ("127.0.0.2", 27017),
    }


@pytest.mark.usefixtures("app_env")
def test_no_network() -> None:
    """Connections other than to the test database are blocked."""
    with (
        socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
        pytest.raises(OSError, match="Network access blocked in tests"),
    ):
        sock.connect(("192.0.2.1", 443))


def test_no_network_db(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connections to the test database are passed on."""
    calls: list[object] = []
    monkeypatch.setattr(conftest, "DB_ADDRESSES", frozenset({("127.0.0.1", 12345)}))
    monkeypatch.setattr(
        conftest, "_CONNECT", lambda sock, address: calls.append(address)
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        conftest._no_network(sock, ("127.0.0.1", 12345))
        with pytest.raises(OSError, match="Network access blocked in tests"):
            conftest._no_network(sock, ("127.0.0.1", 12346))
        with pytest.raises(OSError, match="Network access blocked in tests"):
            conftest._no_network(sock, "/tmp/socket")  # noqa: S108 (not created)
    assert calls == [("127.0.0.1", 12345)]
