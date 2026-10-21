"""Unit tests for `cloud_registry.exceptions`."""

import json

from connexion.exceptions import BadRequestProblem, ExtraParameterProblem
from foca.utils.logging import escape_log_value
import pytest

from cloud_registry.exceptions import (
    MAX_DETAIL_LENGTH,
    BadRequest,
    problem,
    shorten_detail,
    validation_problem,
)


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
def test_validation_problem(exc, detail):
    """Validation errors give `400` with their reason, or the mapped
    details if they have none.
    """
    response = validation_problem(exc)
    assert response.status_code == 400
    assert response.mimetype == "application/problem+json"
    body = json.loads(response.get_data())
    assert body["title"] == "Bad request"
    assert body["status"] == 400
    assert body["detail"].startswith(detail)


def test_validation_problem_long_detail(caplog):
    """Long reasons (e.g., quoting a large rejected value) are shortened in
    the middle in responses and logs, and escaped in logs.
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
def test_shorten_detail_boundary(length, shortened):
    """Details up to `MAX_DETAIL_LENGTH` characters are kept; longer ones
    are shortened to that many characters, including "…".
    """
    detail = "x" * length
    result = shorten_detail(detail)
    assert (result != detail) is shortened
    assert len(result) == MAX_DETAIL_LENGTH
    assert result.endswith("x")


def test_validation_problem_long_messages():
    """Messages of form data are shortened together."""
    exc = BadRequestProblem(detail=["a" * MAX_DETAIL_LENGTH, "b: no int"])
    detail = json.loads(validation_problem(exc).get_data())["detail"]
    assert len(detail) == MAX_DETAIL_LENGTH
    assert detail.startswith("a")
    assert detail.endswith("a; b: no int")


def test_problem_long_description():
    """Descriptions of other problems are shortened, too."""
    exc = BadRequest(description=f"{'a' * 10000} is invalid")
    detail = json.loads(problem(exc).get_data())["detail"]
    assert len(detail) == MAX_DETAIL_LENGTH
    assert detail.endswith("a is invalid")
