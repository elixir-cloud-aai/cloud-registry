"""App exceptions."""

from collections.abc import Iterable
from typing import Any, ClassVar
from urllib.parse import unquote

from connexion.exceptions import (
    BadRequestProblem,
    ExtraParameterProblem,
    Forbidden,
    OAuthProblem,
    Unauthorized,
)
from flask import Response
from foca.errors import FieldError, InvalidFieldsProblem
from foca.errors.exceptions import problem_response
from foca.security.access_control import LastOwner
from werkzeug.exceptions import (
    BadGateway,
    BadRequest,
    Conflict,
    HTTPException,
    InternalServerError,
    NotFound,
    RequestEntityTooLarge,
    TooManyRequests,
)

# Default code of conflicts without a code of their own (`code` member of
# their problem details; cf. `CodedConflict`), e.g., of FOCA's permissions API
CONFLICT_CODE = "conflict"
# Detail of conflicts with fixed problem details
CONFLICT_DETAIL = "The request conflicts with the current state of the resource."

# Codes of invalid fields (`code` of the entries of `errors` of `400`
# responses; cf. `InvalidFields`) that the app's own checks report, besides
# FOCA's generic codes of its schema validation, some of which the app's
# checks report too (`INVALID_VALUE`, `INVALID_FORMAT`, `TOO_LONG`).
# Documented in the API specification (schema `FieldError`) and in
# `docs/api.md` (Errors)
INVALID_URL = "invalid-url"
SCHEME_NOT_ALLOWED = "scheme-not-allowed"
UNREGISTERED_TYPE = "unregistered-type"
UNUSABLE_ID = "unusable-id"
NOT_SERVICE_INFO_URL = "not-service-info-url"
PATH_NOT_NORMALIZED = "path-not-normalized"
INVALID_VALUE = "invalid-value"
INVALID_FORMAT = "invalid-format"
TOO_LONG = "too-long"
FIELD_ERROR_CODES = (
    INVALID_URL,
    SCHEME_NOT_ALLOWED,
    UNREGISTERED_TYPE,
    UNUSABLE_ID,
    NOT_SERVICE_INFO_URL,
    PATH_NOT_NORMALIZED,
    INVALID_VALUE,
    INVALID_FORMAT,
    TOO_LONG,
)


def field_name(error: FieldError) -> str:
    """Get the name of an invalid field, for descriptions and logs.

    Args:
        error: Invalid field.

    Returns:
        The keys of its JSON Pointer (cf. `foca.errors.json_pointer()`),
        unescaped and joined by dots (e.g., `organization.url` for
        `#/organization/url`), or the name of its parameter.
    """
    if error.pointer is None:
        return str(error.parameter)
    segments = unquote(error.pointer.removeprefix("#")).split("/")[1:]
    return ".".join(
        segment.replace("~1", "/").replace("~0", "~") for segment in segments
    )


class InvalidFields(BadRequest):
    """`400 Bad Request` for invalid fields that the app's own checks found.

    Requests get the invalid fields like those of FOCA's schema validation,
    in the member `errors` of the problem details (cf. `reject()`). Checks
    of values that aren't those of the request (e.g., of fetched service
    info) use the description instead, which names the fields.

    Args:
        description: Description; by default, the names (cf.
            `field_name()`) and details of the invalid fields.
        field_errors: Invalid fields, with JSON Pointers into the checked
            object (e.g., the request body) or the names of parameters; their
            details never quote the values.

    Attributes:
        field_errors: Invalid fields.
    """

    def __init__(
        self,
        description: str | None = None,
        *,
        field_errors: Iterable[FieldError] = (),
    ) -> None:
        """Create the exception."""
        self.field_errors = tuple(field_errors)
        if description is None and self.field_errors:
            description = "; ".join(
                f"'{field_name(error)}': {error.detail}" for error in self.field_errors
            )
        super().__init__(description)


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
    TooManyRequests: {
        "title": "Too many requests",
        "detail": "Too many requests; try again later.",
        "status": 429,
    },
    BadGateway: {
        "title": "Bad gateway",
        "detail": "An upstream server could not be reached.",
        "status": 502,
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


def reject(exc: HTTPException) -> Response:
    """Respond to a request that the app's own checks reject.

    Invalid fields of the request (cf. `InvalidFields`) are raised as FOCA's
    `InvalidFieldsProblem`, so that FOCA responds with them like with those
    of its schema validation: `400`, with the problem type and code
    `invalid-request`, the fields in `errors`, and a `detail` that names
    them by location and code (cf. FOCA's `exceptions.validation_reasons`),
    and logs them.

    Args:
        exc: werkzeug exception with a request-specific description.

    Returns:
        Problem details response of exceptions without invalid fields (cf.
        `problem()`).

    Raises:
        foca.errors.InvalidFieldsProblem: The exception has invalid fields.
    """
    if isinstance(exc, InvalidFields) and exc.field_errors:
        raise InvalidFieldsProblem(exc.field_errors) from exc
    return problem(exc)
