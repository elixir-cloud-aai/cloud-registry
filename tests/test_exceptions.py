"""Unit tests for `cloud_registry.exceptions`."""

from collections.abc import Iterator
import json

from connexion.exceptions import BadRequestProblem, ExtraParameterProblem
from flask import Flask
from foca.config.access import get_app_config
from foca.utils.logging import escape_log_value
import pytest

from cloud_registry.exceptions import (
    MAX_DETAIL_LENGTH,
    BadRequest,
    problem,
    shorten_detail,
    validation_problem,
)
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

    That is, the media type of error responses (`exceptions.media_type`), as
    FOCA's.
    """
    get_app_config(unit_app).exceptions.media_type = media_type
    assert problem(BadRequest()).content_type == media_type
    assert validation_problem(BadRequestProblem()).content_type == media_type


@pytest.mark.parametrize(
    "exc, detail",
    [
        (BadRequestProblem(detail="'a' is too long"), "'a' is too long"),
        (BadRequestProblem(detail=["a: no int", "b: no int"]), "a: no int; b: no int"),
        (BadRequestProblem(), "The request is malformed."),
        (BadRequestProblem(detail=""), "The request is malformed."),
        (ExtraParameterProblem([], ["page"]), "Extra query parameter(s) page"),
    ],
    ids=["message", "messages", "none", "empty", "extra_parameter"],
)
def test_validation_problem(
    exc: BadRequestProblem | ExtraParameterProblem, detail: str
) -> None:
    """Validation errors give `400` with their reason.

    Or with the mapped details, if they have none.
    """
    response = validation_problem(exc)
    assert response.status_code == 400
    assert response.content_type == ERROR_MEDIA_TYPE
    body = json.loads(response.get_data())
    assert body["title"] == "Bad request"
    assert body["status"] == 400
    assert body["detail"].startswith(detail)


def test_validation_problem_long_detail(caplog: pytest.LogCaptureFixture) -> None:
    """Long reasons are shortened in the middle in responses and logs.

    E.g., reasons quoting a large rejected value; they are also escaped in
    logs.
    """
    exc = BadRequestProblem(detail=f"'\n{'a' * 10000}' is too long - 'name'")
    with caplog.at_level("INFO", logger="cloud_registry.exceptions"):
        response = validation_problem(exc)
    head = MAX_DETAIL_LENGTH // 2
    tail = "' is too long - 'name'"
    rest = MAX_DETAIL_LENGTH - 1 - head - len(tail)
    truncated = f"'\n{'a' * (head - 2)}…{'a' * rest}{tail}"
    assert json.loads(response.get_data())["detail"] == truncated
    assert caplog.messages == [f"Request rejected: {escape_log_value(truncated)}"]


@pytest.mark.parametrize(
    "length, shortened",
    [(MAX_DETAIL_LENGTH, False), (MAX_DETAIL_LENGTH + 1, True)],
    ids=["max", "longer"],
)
def test_shorten_detail_boundary(length: int, shortened: bool) -> None:
    """Details up to `MAX_DETAIL_LENGTH` characters are kept.

    Longer ones are shortened to that many characters, including "…".
    """
    detail = "x" * length
    result = shorten_detail(detail)
    assert (result != detail) is shortened
    assert len(result) == MAX_DETAIL_LENGTH
    assert result.endswith("x")


def test_validation_problem_long_messages() -> None:
    """Messages of form data are shortened together."""
    exc = BadRequestProblem(detail=["a" * MAX_DETAIL_LENGTH, "b: no int"])
    detail = json.loads(validation_problem(exc).get_data())["detail"]
    assert len(detail) == MAX_DETAIL_LENGTH
    assert detail.startswith("a")
    assert detail.endswith("a; b: no int")


def test_problem_long_description() -> None:
    """Descriptions of other problems are shortened, too."""
    exc = BadRequest(description=f"{'a' * 10000} is invalid")
    detail = json.loads(problem(exc).get_data())["detail"]
    assert len(detail) == MAX_DETAIL_LENGTH
    assert detail.endswith("a is invalid")
