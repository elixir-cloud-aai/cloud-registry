"""Unit tests for `cloud_registry.exceptions`."""

import json

from connexion.exceptions import BadRequestProblem, ExtraParameterProblem
import pytest

from cloud_registry.exceptions import validation_problem


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
