"""Unit tests for `cloud_registry.exceptions`."""

import json

from connexion.exceptions import BadRequestProblem, ExtraParameterProblem
import pytest

from cloud_registry.exceptions import MAX_DETAIL_LENGTH, validation_problem
from cloud_registry.utils import escape_log_value


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
    truncated = (
        f"'\n{'a' * (head - 2)}…{'a' * (MAX_DETAIL_LENGTH - head - len(tail))}{tail}"
    )
    assert json.loads(response.get_data())["detail"] == truncated
    assert caplog.messages == [f"Request rejected: {escape_log_value(truncated)}"]
