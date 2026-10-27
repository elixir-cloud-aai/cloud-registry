"""App exceptions."""

from typing import Any, ClassVar

from connexion.exceptions import (
    BadRequestProblem,
    ExtraParameterProblem,
    Forbidden,
    OAuthProblem,
    Unauthorized,
)
from flask import Response
from foca.errors.exceptions import problem_response
from foca.security.access_control import LastOwner
from werkzeug.exceptions import (
    BadRequest,
    Conflict,
    HTTPException,
    InternalServerError,
    NotFound,
    RequestEntityTooLarge,
)

# Default code of conflicts without a code of their own (`code` member of
# their problem details; cf. `CodedConflict`), e.g., of FOCA's permissions API
CONFLICT_CODE = "conflict"
# Detail of conflicts with fixed problem details
CONFLICT_DETAIL = "The request conflicts with the current state of the resource."


class CodedConflict(Conflict):
    """`409 Conflict` with a stable code, for clients to tell conflicts apart.

    The code is served as the extension member `code` of the problem details
    built with `problem()`, next to the unchanged `detail`, and documented in
    the API specification (schema `ConflictError`). Conflicts that reach
    FOCA's error handler instead (i.e., raised, not returned) get the code of
    their entry in `exceptions` (default: `CONFLICT_CODE`), so add an entry
    for those, as for FOCA's `LastOwner`.

    Attributes:
        problem_code: Code of the conflict (kebab case); not `code`, which
            is the status code of werkzeug's exceptions.
    """

    problem_code: ClassVar[str] = CONFLICT_CODE


# Detail of `ServiceBusy`
SERVICE_BUSY_DETAIL = (
    "The service or the new identifier is being renamed or changed; try again later."
)


class ServiceBusy(CodedConflict):
    """The service is being renamed, or changed while it is to be renamed.

    Raised by changes of services that waited in vain for a rename to finish,
    and returned by renames that waited in vain for changes or another rename
    (cf. `cloud_registry.ga4gh.registry.locks`); in `exceptions`, so that the
    raised one has its code, too.
    """

    problem_code = "service-busy"

    def __init__(self) -> None:
        """Create the exception, with a description of what to do."""
        super().__init__(SERVICE_BUSY_DETAIL)


# exceptions raised in app context; entries of conflicts have their code
# (`code`; cf. `CodedConflict`)
exceptions = {
    Exception: {
        "title": "Unknown error",
        "detail": "An unexpected error occurred.",
        "status": 500,
    },
    BadRequestProblem: {
        "title": "Bad request",
        "detail": "The request is malformed.",
        "status": 400,
    },
    BadRequest: {
        "title": "Bad request",
        "detail": "The request is malformed.",
        "status": 400,
    },
    ExtraParameterProblem: {
        "title": "Bad request",
        "detail": "The request is malformed.",
        "status": 400,
    },
    Unauthorized: {
        "title": "Unauthorized",
        "detail": "The request is unauthorized.",
        "status": 401,
    },
    OAuthProblem: {
        "title": "Unauthorized",
        "detail": "The request is unauthorized.",
        "status": 401,
    },
    Forbidden: {
        "title": "Forbidden",
        "detail": "The requester is not authorized to perform this action.",
        "status": 403,
    },
    NotFound: {
        "title": "Not found",
        "detail": "The requested resource wasn't found.",
        "status": 404,
    },
    Conflict: {
        "title": "Conflict",
        "detail": CONFLICT_DETAIL,
        "status": 409,
        "code": CONFLICT_CODE,
    },
    # raised by FOCA's helpers of resource roles (cf.
    # `cloud_registry.ga4gh.registry.permissions`)
    LastOwner: {
        "title": "Conflict",
        "detail": CONFLICT_DETAIL,
        "status": 409,
        "code": "last-owner",
    },
    ServiceBusy: {
        "title": "Conflict",
        "detail": SERVICE_BUSY_DETAIL,
        "status": 409,
        "code": ServiceBusy.problem_code,
    },
    RequestEntityTooLarge: {
        "title": "Content Too Large",
        "detail": "The request body exceeds the size limit.",
        "status": 413,
    },
    InternalServerError: {
        "title": "Internal server error",
        "detail": "An unexpected error occurred.",
        "status": 500,
    },
}


def problem(exc: HTTPException, **extensions: Any) -> Response:
    """Build a problem details response with the description of an exception.

    See FOCA's `problem_response()`; conflicts (`409`) have their code
    (`code`): the one of the class for coded conflicts (cf.
    `CodedConflict`), otherwise the one of their entry in `exceptions`.

    Args:
        exc: werkzeug exception with a request-specific description.
        extensions: Extension members of the problem details; for
            conflicts, not `code`, which is set from the exception.

    Returns:
        Problem details response.
    """
    if isinstance(exc, Conflict):
        # the code of the conflict wins over extension members of the caller;
        # for conflicts without a code of their own, FOCA's
        # `problem_response()` sends the members of their entry in
        # `exceptions`, including `code` (cf. `tests/test_conflicts.py`)
        extensions.pop("code", None)
        if isinstance(exc, CodedConflict):
            extensions["code"] = exc.problem_code
    return problem_response(exc, **extensions)
