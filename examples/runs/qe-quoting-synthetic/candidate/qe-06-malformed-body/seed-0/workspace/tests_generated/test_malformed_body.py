"""Malformed request bodies: the service must answer 400 with a field, never
raise out of the handler.
"""
from __future__ import annotations

import pytest

from quoting.service import handle_quote


@pytest.mark.parametrize(
    "body",
    [b"", b"not json", b"{", b"[]", b'"a string"', b"42", b"null"],
)
def test_a_body_that_is_not_a_json_object_is_rejected(body):
    status, payload = handle_quote(body)
    assert status == 400
    assert payload["field"] == "body"


def test_invalid_utf8_is_rejected_rather_than_raising():
    status, payload = handle_quote(b"\xff\xfe")
    assert status == 400
    assert payload["field"] == "body"


def test_an_empty_json_object_reports_the_first_missing_field():
    status, payload = handle_quote(b"{}")
    assert status == 400
    assert payload["field"] == "weight_grams"
