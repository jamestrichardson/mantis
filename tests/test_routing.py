"""Tests for mantis.routing: the model-call failure taxonomy,
classification function, and safe-detail extraction (#16).
"""

from __future__ import annotations

import httpx
import openai
import pytest

from mantis.routing import (
    DEFAULT_ELIGIBLE_FAILURE_KINDS,
    ModelCallAttempt,
    ModelCallFailureKind,
    classify_model_call_exception,
    safe_model_call_detail,
)


def _status_error(cls, status_code: int, message: str = "boom") -> Exception:
    response = httpx.Response(status_code, request=httpx.Request("POST", "http://litellm.example.test"))
    return cls(message, response=response, body=None)


def _connection_error(message: str = "boom") -> Exception:
    return openai.APIConnectionError(request=httpx.Request("POST", "http://litellm.example.test"))


def _timeout_error() -> Exception:
    return openai.APITimeoutError(request=httpx.Request("POST", "http://litellm.example.test"))


# ---------------------------------------------------------------------------
# classify_model_call_exception
# ---------------------------------------------------------------------------


def test_timeout_is_classified_as_timeout_not_connection():
    # APITimeoutError subclasses APIConnectionError -- must be checked
    # first, or every timeout would be misclassified.
    assert classify_model_call_exception(_timeout_error()) == ModelCallFailureKind.TIMEOUT


def test_connection_error_is_classified_as_connection():
    assert classify_model_call_exception(_connection_error()) == ModelCallFailureKind.CONNECTION


def test_rate_limit_is_classified_as_rate_limit():
    exc = _status_error(openai.RateLimitError, 429)
    assert classify_model_call_exception(exc) == ModelCallFailureKind.RATE_LIMIT


def test_authentication_is_classified_as_authentication():
    exc = _status_error(openai.AuthenticationError, 401)
    assert classify_model_call_exception(exc) == ModelCallFailureKind.AUTHENTICATION


def test_permission_denied_is_classified_as_authorization():
    exc = _status_error(openai.PermissionDeniedError, 403)
    assert classify_model_call_exception(exc) == ModelCallFailureKind.AUTHORIZATION


@pytest.mark.parametrize(
    "cls,status",
    [
        (openai.BadRequestError, 400),
        (openai.UnprocessableEntityError, 422),
        (openai.NotFoundError, 404),
    ],
)
def test_client_request_errors_are_classified_as_bad_request(cls, status):
    exc = _status_error(cls, status)
    assert classify_model_call_exception(exc) == ModelCallFailureKind.BAD_REQUEST


def test_internal_server_error_is_classified_as_server_error():
    exc = _status_error(openai.InternalServerError, 500)
    assert classify_model_call_exception(exc) == ModelCallFailureKind.SERVER_ERROR


def test_response_validation_error_is_classified_as_invalid_response():
    response = httpx.Response(200, request=httpx.Request("POST", "http://x"), content=b"{}")
    exc = openai.APIResponseValidationError(response=response, body=None)
    assert classify_model_call_exception(exc) == ModelCallFailureKind.INVALID_RESPONSE


def test_an_unrecognized_exception_type_is_classified_as_unknown():
    assert classify_model_call_exception(ValueError("something else entirely")) == ModelCallFailureKind.UNKNOWN


def test_a_generic_openai_error_with_no_specific_subclass_is_unknown():
    assert classify_model_call_exception(openai.OpenAIError("generic")) == ModelCallFailureKind.UNKNOWN


# ---------------------------------------------------------------------------
# Default eligibility set
# ---------------------------------------------------------------------------


def test_default_eligible_kinds_are_exactly_the_documented_four():
    assert DEFAULT_ELIGIBLE_FAILURE_KINDS == frozenset(
        {
            ModelCallFailureKind.TIMEOUT,
            ModelCallFailureKind.CONNECTION,
            ModelCallFailureKind.RATE_LIMIT,
            ModelCallFailureKind.SERVER_ERROR,
        }
    )


@pytest.mark.parametrize(
    "kind",
    [
        ModelCallFailureKind.AUTHENTICATION,
        ModelCallFailureKind.AUTHORIZATION,
        ModelCallFailureKind.BAD_REQUEST,
        ModelCallFailureKind.INVALID_RESPONSE,
        ModelCallFailureKind.UNKNOWN,
    ],
)
def test_non_eligible_kinds_are_excluded_from_the_default_set(kind):
    assert kind not in DEFAULT_ELIGIBLE_FAILURE_KINDS


# ---------------------------------------------------------------------------
# safe_model_call_detail -- never the raw provider error body
# ---------------------------------------------------------------------------


def test_safe_detail_never_includes_the_raw_exception_message():
    # A real example encountered during #13 qualification: an nginx 504
    # Gateway Time-out HTML error page as the exception's own message.
    html_body = "<html><head><title>504 Gateway Time-out</title></head><body>nginx</body></html>"
    exc = _status_error(openai.InternalServerError, 504, message=html_body)

    detail = safe_model_call_detail(exc)

    assert html_body not in detail
    assert "<html>" not in detail
    assert "nginx" not in detail


def test_safe_detail_includes_the_exception_class_name_and_status_code():
    exc = _status_error(openai.AuthenticationError, 401, message="Incorrect API key provided: sk-***")

    detail = safe_model_call_detail(exc)

    assert "AuthenticationError" in detail
    assert "401" in detail
    assert "sk-" not in detail  # never leaks the secret embedded in the raw message


def test_safe_detail_for_an_exception_with_no_status_code_is_just_the_class_name():
    detail = safe_model_call_detail(ValueError("some detailed internal message"))

    assert detail == "ValueError"
    assert "detailed internal message" not in detail


# ---------------------------------------------------------------------------
# ModelCallAttempt.to_dict()
# ---------------------------------------------------------------------------


def test_model_call_attempt_to_dict_is_bounded_and_json_safe():
    import json

    attempt = ModelCallAttempt(
        iteration=1,
        attempt_number=2,
        requested_alias="fallback-alias",
        routing_reason="fallback",
        outcome="error",
        failure_kind=ModelCallFailureKind.TIMEOUT,
        detail="APITimeoutError",
        latency_seconds=1.23,
    )

    d = attempt.to_dict()
    json.dumps(d)  # must not raise -- every value is a plain JSON type

    assert d["failure_kind"] == "timeout"  # the enum's .value, not the Enum member
    assert d["requested_alias"] == "fallback-alias"
    assert d["routing_reason"] == "fallback"


def test_model_call_attempt_to_dict_success_has_no_failure_kind():
    attempt = ModelCallAttempt(
        iteration=1,
        attempt_number=1,
        requested_alias="primary",
        routing_reason="primary",
        outcome="ok",
        total_tokens=42,
        backend_model="resolved/primary",
    )

    d = attempt.to_dict()

    assert d["failure_kind"] is None
    assert d["detail"] is None
    assert d["total_tokens"] == 42
    assert d["backend_model"] == "resolved/primary"
