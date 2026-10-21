"""App exceptions."""

from json import dumps

from connexion.exceptions import (
    BadRequestProblem,
    ExtraParameterProblem,
    Forbidden,
    OAuthProblem,
    Unauthorized,
)
from flask import Response
from werkzeug.exceptions import (
    BadRequest,
    Conflict,
    HTTPException,
    InternalServerError,
    NotFound,
    RequestEntityTooLarge,
)

# exceptions raised in app context
exceptions = {
    Exception: {
        "title": "Unknown error",
        "detail": "An unexpected error occurred.",
        "status": 500,
    },
    BadRequestProblem: {
        "title": "Bad request",
        "detail": "The request is malformed",
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
    # title as for other errors, from the closest mapped exception class
    title = next(
        exceptions[cls]["title"] for cls in type(exc).__mro__ if cls in exceptions
    )
    details = {
        "title": title,
        "status": exc.code,
        "detail": exc.description,
        **extensions,
    }
    return Response(
        dumps(details), status=exc.code, mimetype="application/problem+json"
    )
