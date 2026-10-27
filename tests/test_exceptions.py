"""Unit tests for `cloud_registry.exceptions`.

Building problem details is FOCA's (`problem_response()`); the app adds the
codes of conflicts (cf. `tests/test_conflicts.py`).
"""

from collections.abc import Iterator

from flask import Flask
from foca.config.access import get_app_config
import pytest
from werkzeug.exceptions import HTTPException

from cloud_registry.exceptions import BadRequest, problem
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
    get_app_config(unit_app).exceptions.media_type = media_type
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
