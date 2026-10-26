"""Locks of services, so that renames don't interleave with other changes.

A rename (cf. `cloud_registry.ga4gh.registry.rename`) moves a service and its
roles in several writes, without transactions (which need a replica set).
Changes of the service in between (e.g., revoking a role after it was copied,
deleting the service after its roles were revoked) could be lost or apply to
the wrong service, so renames and changes exclude each other:

- A rename holds an exclusive lock on each of its identifiers: a document in
  `LOCKS` with the identifier as `_id` (unique), so that renames exclude
  each other, too.
- A change of a service (`PUT`, `DELETE`, granting and revoking roles) holds
  a shared lock while it runs: a document of its own (`_id`: `{"change":
  <random>}`) naming the service (`service`), so that changes don't exclude
  each other.

Each side stores its lock first and then checks for the other's: a change
that finds a rename lock removes its own and waits for the rename to finish;
a rename that finds changes keeps its lock (so that no new change starts) and
waits for them to finish. Each waits up to `LOCK_ATTEMPTS` times
`LOCK_WAIT`, then gives up (`409 Conflict`, cf. `ServiceBusy`). Locks older
than `LOCK_TIMEOUT` are stale (e.g., of a crashed worker): changes don't wait
for stale changes, and stale locks are removed or resumed (cf.
`cloud_registry.ga4gh.registry.rename.resume_renames()`).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
import time
from typing import Any
from uuid import uuid4

from pymongo.errors import DuplicateKeyError

from cloud_registry.exceptions import ServiceBusy
from cloud_registry.utils import get_collection, now

# Collection of the locks (cf. the module docstring), with an index of
# `service` and `started_at` (cf. `config.yaml`)
LOCKS = "service_locks"
# Age after which locks are stale; renames and changes take a few writes, so
# they are done by then unless they failed. Keep in sync with "a minute" in
# `docs/api.md` (Renaming services)
LOCK_TIMEOUT = timedelta(minutes=1)
# How long and how often renames and changes wait for each other (in total
# about a second)
LOCK_WAIT = 0.05
LOCK_ATTEMPTS = 20


def stale_before() -> datetime:
    """Get the time before which locks are stale.

    Returns:
        Current time minus `LOCK_TIMEOUT`.
    """
    return now() - LOCK_TIMEOUT


@contextmanager
def changing(service_id: str) -> Iterator[None]:
    """Hold a shared lock on a service while changing it.

    Waits for a rename of the service to finish (cf. the module docstring).

    Args:
        service_id: Identifier of the service.

    Yields:
        Nothing; the lock is held until the context exits.

    Raises:
        cloud_registry.exceptions.ServiceBusy: The service was still being
            renamed after waiting.
    """
    collection = get_collection(LOCKS)
    lock_id = {"change": uuid4().hex}
    for _ in range(LOCK_ATTEMPTS):
        collection.insert_one(
            {"_id": lock_id, "service": service_id, "started_at": now()}
        )
        if not collection.count_documents({"_id": service_id}, limit=1):
            break
        collection.delete_one({"_id": lock_id})
        time.sleep(LOCK_WAIT)
    else:
        raise ServiceBusy
    try:
        yield
    finally:
        collection.delete_one({"_id": lock_id})


def lock_exclusive(service_id: str, **fields: Any) -> str | None:
    """Take the exclusive lock on a service identifier, for a rename.

    Waits for changes of the service to finish (cf. the module docstring).

    Args:
        service_id: Service identifier.
        **fields: Further fields of the lock (e.g., the other identifier of
            the rename).

    Returns:
        Token of the lock (cf. `unlock()`), or `None` if the identifier is
        locked by another rename, or still being changed after waiting (then
        no lock is held).
    """
    collection = get_collection(LOCKS)
    token = uuid4().hex
    try:
        collection.insert_one(
            {"_id": service_id, "token": token, "started_at": now(), **fields}
        )
    except DuplicateKeyError:
        return None
    for _ in range(LOCK_ATTEMPTS):
        changes = {"service": service_id, "started_at": {"$gt": stale_before()}}
        if not collection.count_documents(changes, limit=1):
            return token
        time.sleep(LOCK_WAIT)
    unlock(service_id, token)
    return None


def unlock(service_id: str, token: str) -> None:
    """Release an exclusive lock, unless another holder has it now.

    Args:
        service_id: Service identifier.
        token: Token of the lock (cf. `lock_exclusive()`).
    """
    get_collection(LOCKS).delete_one({"_id": service_id, "token": token})


def remove_stale_changes() -> None:
    """Remove stale locks of changes (e.g., of crashed workers)."""
    get_collection(LOCKS).delete_many(
        {"service": {"$exists": True}, "started_at": {"$lte": stale_before()}}
    )
