"""Unit tests for `cloud_registry.exceptions`.

Building problem details is FOCA's (`problem_response()`); the app adds the
codes of conflicts (cf. `tests/test_conflicts.py`).
"""

from collections.abc import Iterator

from flask import Flask
from foca import get_config
from foca.errors import FieldError
import pytest
from werkzeug.exceptions import HTTPException

from cloud_registry.exceptions import BadRequest, field_name, problem
from tests.mock_data import ERROR_MEDIA_TYPE


@pytest.fixture(autouse=True)
def app_context(unit_app: Flask) -> Iterator[None]:
    """Problem details are built in an app context (for their media type)."""
    with unit_app.app_context():
        yield


@pytest.mark.parametrize(
    "media_type",
    [ERROR_MEDIA_TYPE, "application/problem+json"],
    ids=["json", "problem"],
)
def test_problem_media_type(unit_app: Flask, media_type: str) -> None:
    """Problem details built by the app have the configured media type.

    That is, the media type of error responses (`exceptions.media_type`).
    """
    get_config().exceptions.media_type = media_type
    assert problem(BadRequest()).content_type == media_type


class _NoDescription(HTTPException):
    """HTTP exception with a code, but without a description."""

    code = 400


@pytest.mark.parametrize(
    "exc",
    [HTTPException(description="Something failed."), _NoDescription()],
    ids=["no_code", "no_description"],
)
def test_problem_incomplete_exception(exc: HTTPException) -> None:
    """Problem details need an exception with a code and a description."""
    with pytest.raises(ValueError, match="status code and a description"):
        problem(exc)


@pytest.mark.parametrize(
    "location, name",
    [
        ({"pointer": "#/organization/url"}, "organization.url"),
        ({"pointer": "#/a~1b/c~0d/%C3%A4"}, "a/b.c~d.\u00e4"),
        ({"parameter": "serviceId"}, "serviceId"),
    ],
)
def test_field_name(location: dict[str, str], name: str) -> None:
    """Fields are named by the unescaped keys of their pointers, or parameters."""
    assert field_name(FieldError(detail="Invalid.", code="x", **location)) == name
