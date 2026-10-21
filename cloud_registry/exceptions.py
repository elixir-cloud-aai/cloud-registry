"""App exceptions."""

from json import dumps
import logging
from typing import Any

from connexion.exceptions import (
    BadRequestProblem,
    ExtraParameterProblem,
    Forbidden,
    OAuthProblem,
    Unauthorized,
)
from flask import Response
from foca import get_config
from foca.utils.logging import escape_log_value
from werkzeug.exceptions import (
    BadRequest,
    Conflict,
    HTTPException,
    InternalServerError,
    NotFound,
    RequestEntityTooLarge,
)

logger = logging.getLogger(__name__)

# Maximum length of details in problem details responses (and of reasons in
# their log messages); longer ones are shortened in the middle (keeping,
# e.g., the field names at their end), as they may quote rejected values,
# which can be as large as the request body
MAX_DETAIL_LENGTH = 500

# exceptions raised in app context
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
        "detail": "The request conflicts with the current state of the resource.",
        "status": 409,
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


def shorten_detail(detail: str) -> str:
    """Shorten a detail to `MAX_DETAIL_LENGTH` characters.

    Keeps its start and end.

    Args:
        detail: Description of a problem.

    Returns:
        The detail if it is not longer than `MAX_DETAIL_LENGTH`, otherwise
        its first and last characters, joined by "…", with
        `MAX_DETAIL_LENGTH` characters in total.
    """
    if len(detail) <= MAX_DETAIL_LENGTH:
        return detail
    head = MAX_DETAIL_LENGTH // 2
    return f"{detail[:head]}…{detail[head + 1 - MAX_DETAIL_LENGTH :]}"


def problem(exc: HTTPException, **extensions: str) -> Response:
    """Build a problem details response with the description of an exception.

    FOCA's problem details are fixed per exception class, so responses with
    details specific to a request are built here.

    Args:
        exc: Exception with a request-specific description.
        extensions: Extension members of the problem details.

    Returns:
        Problem details response.
    """
    return _response(
        title=_mapped(exc)["title"],
        status=exc.code,  # type: ignore[arg-type]
        detail=exc.description,  # type: ignore[arg-type]
        **extensions,
    )


def validation_problem(exc: BadRequestProblem | ExtraParameterProblem) -> Response:
    """Build a problem details response for a request that failed validation.

    The response has the reason (e.g., the validator's message) of
    Connexion's validation. Registered as error handler for these exceptions (cf.
    `cloud_registry.app.create_app()`), as FOCA's handler only serves the
    fixed details of the exception class.

    Args:
        exc: Validation error, with the reason as `detail` (a string, or a
            list of them for form data).

    Returns:
        Problem details response (`400`), with the reason as `detail`
        (shortened, cf. `shorten_detail()`), or the mapped details if there
        is none.
    """
    mapped = _mapped(exc)
    detail = exc.detail
    if isinstance(detail, list):
        detail = "; ".join(str(item) for item in detail)
    if not isinstance(detail, str) or not detail:
        detail = mapped["detail"]
    logger.info("Request rejected: %s", escape_log_value(shorten_detail(detail)))
    return _response(title=mapped["title"], status=mapped["status"], detail=detail)


def _mapped(exc: Exception) -> dict[str, Any]:
    """Get the problem details mapped to the closest class of an exception.

    Args:
        exc: Exception.

    Returns:
        Problem details of the closest class in `exceptions`.
    """
    return next(exceptions[cls] for cls in type(exc).__mro__ if cls in exceptions)


def _response(title: str, status: int, detail: str, **extensions: str) -> Response:
    """Build a problem details response.

    Must be called in an app context.

    Args:
        title: Title of the problem.
        status: HTTP status code.
        detail: Description of the problem; shortened, as it may quote
            values of the request (cf. `shorten_detail()`).
        extensions: Extension members of the problem details.

    Returns:
        Problem details response, with the media type of all error responses
        (`exceptions.media_type`), as FOCA's.
    """
    details = {
        "title": title,
        "status": status,
        "detail": shorten_detail(detail),
        **extensions,
    }
    return Response(
        dumps(details),
        status=status,
        content_type=get_config().exceptions.media_type,
    )
