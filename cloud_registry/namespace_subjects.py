"""Namespace the stored subjects of a token issuer, for several issuers.

With several trusted token issuers, FOCA namespaces subjects by issuer
(`<namespace>|<sub>`; by default, the namespace is the issuer), so that equal
subjects of different issuers are different users. Subjects stored while the
issuer's subjects were bare (e.g., while it was the only trusted issuer) then
no longer match its users. This migration rewrites them to the namespaced
form:

- users in stored policy rules (`user:<sub>`, e.g., owners and maintainers
  of services, or roles granted via FOCA's permissions API); configured
  rules (`security.access_control.policies`) are left as they are, as the
  app replaces them with those of the config on startup, so change the
  config instead;
- the users who added service types (`created_by` of `service_types`), for
  the limit of unused types and the visibility of pending types;
- the users of the stored display names (`user_id` of `user_names`).

Subjects that are already namespaced (with `|`) are kept, so the migration
can be run again, e.g., after an interrupted run. It then marks the stored
rules as changed, so that the cached enforcers of running app instances load
them again (FOCA's `Adapter.mark_changed()`).

Run it once the config trusts the new issuer (so that it names the
namespace), with the app's config (`FOCA_CONFIG_OVERLAY`), e.g., in a
container of the app: `python -m cloud_registry.namespace_subjects
<namespace>` (add `--dry-run` to only count the subjects). Until then, the
issuer's stored users lose their roles. Prefer `namespace: ""` for the
issuer trusted so far, which keeps its subjects bare (see "Several token
issuers" in `docs/upgrading.md`).
"""

import argparse
from collections.abc import Sequence
import logging
import re
from typing import Any

from flask import current_app
from foca import get_collection as get_foca_collection
from foca import get_config
from foca.models.config import NAMESPACE_SEPARATOR, IssuerConfig
from foca.security.access_control.constants import CONFIGURED_FIELD
from pymongo.collection import Collection
from pymongo.errors import DuplicateKeyError

from cloud_registry.app import create_app
from cloud_registry.ga4gh.registry.service_types import CREATED_BY
from cloud_registry.utils import get_collection

logger = logging.getLogger(__name__)

# Fields of policy rules with values (`ptype` names the rule type)
RULE_FIELDS = tuple(f"v{index}" for index in range(6))


def _is_bare(subject: object) -> bool:
    """Check whether a subject is stored bare, i.e., not namespaced.

    Args:
        subject: Stored subject; any value.

    Returns:
        Whether the subject is a string without the separator of namespaces.
    """
    return isinstance(subject, str) and NAMESPACE_SEPARATOR not in subject


def check_namespace(namespace: str) -> None:
    """Check that a namespace is one of the trusted issuers.

    Must be called in an app context.

    Args:
        namespace: Namespace of the issuer's subjects.

    Raises:
        ValueError: The namespace is invalid (cf. FOCA's `IssuerConfig`),
            or no trusted issuer has it (`security.auth.issuers`), e.g.,
            as the config doesn't trust the new issuer yet, or keeps the
            issuer's subjects bare.
    """
    # FOCA's validation of namespaces (no separator, commas or line breaks)
    IssuerConfig(issuer="-", namespace=namespace)
    namespaces = get_config().security.auth.get_namespaces()
    if not namespace or namespace not in namespaces.values():
        known = sorted(value for value in namespaces.values() if value)
        raise ValueError(
            f"No trusted issuer has the namespace {namespace!r}: the "
            f"namespaces of the config are {known}; trust several issuers "
            "('security.auth.issuers') first"
        )


def _policy_collection() -> Collection[dict[str, Any]]:
    """Get the collection of the stored policy rules of the current app.

    Returns:
        The collection (`security.access_control.collection_name`, which
        FOCA registers as a collection of the app).
    """
    access_control = get_config().security.access_control
    return get_foca_collection(
        db=access_control.db_name, collection=access_control.collection_name
    )


def _namespace_rules(namespace: str, dry_run: bool) -> tuple[int, int]:
    """Namespace the users of the stored policy rules.

    Args:
        namespace: Namespace of the issuer's subjects.
        dry_run: Only count the rules.

    Returns:
        Number of stored rules with bare users (rewritten, unless
        `dry_run`), and of configured rules with bare users (kept).
    """
    prefix = get_config().security.access_control.subject_prefix
    collection = _policy_collection()
    users = {"$regex": f"^{re.escape(prefix)}[^{re.escape(NAMESPACE_SEPARATOR)}]*$"}
    rewritten = configured = 0
    for rule in collection.find({"$or": [{field: users} for field in RULE_FIELDS]}):
        if rule.get(CONFIGURED_FIELD):
            configured += 1
            continue
        rewritten += 1
        if dry_run:
            continue
        changed = {
            field: f"{prefix}{namespace}{NAMESPACE_SEPARATOR}{value[len(prefix) :]}"
            for field in RULE_FIELDS
            if isinstance(value := rule.get(field), str)
            and value.startswith(prefix)
            and _is_bare(value[len(prefix) :])
        }
        collection.update_one({"_id": rule["_id"]}, {"$set": changed})
    return rewritten, configured


def _namespace_service_types(namespace: str, dry_run: bool) -> int:
    """Namespace the users who added service types.

    Args:
        namespace: Namespace of the issuer's subjects.
        dry_run: Only count the types.

    Returns:
        Number of types added by bare users (rewritten, unless `dry_run`).
    """
    collection = get_collection("service_types")
    count = 0
    for service_type in collection.find({CREATED_BY: {"$type": "string"}}):
        subject = service_type[CREATED_BY]
        if not _is_bare(subject):
            continue
        count += 1
        if not dry_run:
            collection.update_one(
                {"_id": service_type["_id"]},
                {"$set": {CREATED_BY: f"{namespace}{NAMESPACE_SEPARATOR}{subject}"}},
            )
    return count


def _namespace_user_names(namespace: str, dry_run: bool) -> int:
    """Namespace the users of the stored display names.

    A name that the namespaced user already has for the service (e.g., as
    they used the registry since the config change) is kept, and the bare
    one deleted.

    Args:
        namespace: Namespace of the issuer's subjects.
        dry_run: Only count the names.

    Returns:
        Number of names of bare users (rewritten, unless `dry_run`).
    """
    collection = get_collection("user_names")
    count = 0
    for name in collection.find({"user_id": {"$type": "string"}}):
        if not _is_bare(name["user_id"]):
            continue
        count += 1
        if dry_run:
            continue
        user_id = f"{namespace}{NAMESPACE_SEPARATOR}{name['user_id']}"
        try:
            collection.update_one({"_id": name["_id"]}, {"$set": {"user_id": user_id}})
        except DuplicateKeyError:
            collection.delete_one({"_id": name["_id"]})
    return count


def namespace_subjects(namespace: str, dry_run: bool = False) -> dict[str, int]:
    """Namespace the stored subjects of an issuer.

    Must be called in an app context. All bare subjects are taken as the
    issuer's: run it for the issuer whose subjects were stored bare, i.e.,
    the one trusted so far.

    Args:
        namespace: Namespace of the issuer's subjects, as in the config
            (by default, the issuer, e.g., `https://login.example.org/`).
        dry_run: Only count the subjects to rewrite, without changes.

    Returns:
        Numbers of stored policy rules (`rules`), service types
        (`service_types`) and display names (`user_names`) of bare users
        (rewritten, unless `dry_run`), and of configured rules with bare
        users (`configured_rules`; kept, cf. the module docs).

    Raises:
        ValueError: No trusted issuer has the namespace (cf.
            `check_namespace()`).
    """
    check_namespace(namespace)
    rules, configured = _namespace_rules(namespace, dry_run)
    counts = {
        "rules": rules,
        "configured_rules": configured,
        "service_types": _namespace_service_types(namespace, dry_run),
        "user_names": _namespace_user_names(namespace, dry_run),
    }
    if configured:
        logger.warning(
            "%d configured policy rules ('security.access_control.policies') "
            "name bare users: namespace them in the config (e.g., "
            "'user:%s%s<sub>').",
            configured,
            namespace,
            NAMESPACE_SEPARATOR,
        )
    if not dry_run:
        current_app.config["casbin_adapter"].mark_changed()
    return counts


def main(argv: Sequence[str] | None = None) -> int:
    """Namespace the stored subjects of an issuer, with the app's config.

    Args:
        argv: Command line arguments, without the program name: the
            namespace, and optionally `--dry-run`.

    Returns:
        The exit status: 0 on success, 1 if the namespace is not that of a
        trusted issuer.
    """
    parser = argparse.ArgumentParser(
        prog="python -m cloud_registry.namespace_subjects",
        description="Namespace the stored subjects of a token issuer.",
    )
    parser.add_argument("namespace", help="namespace of the issuer's subjects")
    parser.add_argument(
        "--dry-run", action="store_true", help="only count the subjects"
    )
    args = parser.parse_args(argv)
    app = create_app().app
    with app.app_context():
        try:
            counts = namespace_subjects(args.namespace, dry_run=args.dry_run)
        except ValueError as exc:
            print(f"error: {exc}")
            return 1
    verb = "To namespace" if args.dry_run else "Namespaced"
    print(", ".join(f"{verb} {key}: {value}" for key, value in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
