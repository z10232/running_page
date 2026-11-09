import hashlib
import importlib
from pathlib import Path

import httpx
import pytest


@pytest.fixture
def api(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "run_page"))
    return importlib.import_module("runalyze_client")


def test_ping_uses_personal_token_header(api):
    def handle(request):
        assert str(request.url) == "https://runalyze.com/api/v1/ping"
        assert request.headers["token"] == "example-personal-token"
        assert "example-personal-token" not in str(request.url)
        return httpx.Response(200, json={"message": "pong"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as session:
        api.RunalyzeClient(session, "example-personal-token").ping()


def test_upload_posts_original_fit_and_records_queue_not_completion(api):
    content = b"example-fit"

    def handle(request):
        assert request.method == "POST"
        assert str(request.url) == "https://runalyze.com/api/v1/activities/uploads"
        assert request.headers["token"] == "example-personal-token"
        assert "multipart/form-data;" in request.headers["content-type"]
        assert b'name="file"; filename="keep-123.fit"' in request.content
        assert content in request.content
        return httpx.Response(
            201,
            json={"queue_id": 123, "activity_id": None, "original_file": "private"},
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as session:
        receipt = api.RunalyzeClient(session, "example-personal-token").upload_fit(
            "keep-123.fit", content
        )
    assert receipt == {
        "sha256": hashlib.sha256(content).hexdigest(),
        "http_status": 201,
        "queue_id": 123,
    }


@pytest.mark.parametrize("activity_id", [None, 456])
def test_import_status_reads_free_queue_endpoint_and_only_known_fields(
    api, activity_id
):
    def handle(request):
        assert request.method == "GET"
        assert str(request.url) == "https://runalyze.com/api/v1/activities/uploads/123"
        assert request.headers["token"] == "example-personal-token"
        payload = {
            "queue_id": 123,
            "status": "successfully imported" if activity_id else "not proccessed yet",
            "state": "stored" if activity_id else "unprocessed",
            "message": "private-do-not-store",
        }
        if activity_id:
            payload["activity_id"] = activity_id
        return httpx.Response(200, json=payload)

    with httpx.Client(transport=httpx.MockTransport(handle)) as session:
        status = api.RunalyzeClient(
            session, "example-personal-token"
        ).get_import_status(123)
    assert status == {
        "queue_id": 123,
        "activity_id": activity_id,
        "status": "successfully imported" if activity_id else "not proccessed yet",
        "state": "stored" if activity_id else "unprocessed",
    }


@pytest.mark.parametrize("payload", [{}, {"queue_id": None}, {"queue_id": "123"}])
def test_upload_requires_numeric_queue_receipt(api, payload):
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(201, json=payload)
            )
        ) as session,
        pytest.raises(api.RunalyzeError),
    ):
        api.RunalyzeClient(session, "example-personal-token").upload_fit(
            "keep.fit", b"fit"
        )


def test_invalid_response_is_safe_for_public_logs(api):
    with (
        httpx.Client(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, text="private-invalid-json")
            )
        ) as session,
        pytest.raises(api.RunalyzeError) as error,
    ):
        api.RunalyzeClient(session, "example-personal-token").get_import_status(123)
    assert "private-invalid-json" not in str(error.value)


@pytest.mark.parametrize("status", [200, 202, 400, 401, 403, 429, 500])
def test_upload_does_not_claim_acceptance_on_unexpected_response(api, status):
    def handle(request):
        return httpx.Response(status, json={"detail": "private-server-value"})

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as session,
        pytest.raises(api.RunalyzeError) as error,
    ):
        api.RunalyzeClient(session, "secret-example").upload_fit("keep-123.fit", b"fit")
    assert "private-server-value" not in str(error.value)
    assert "secret-example" not in str(error.value)


def test_network_exception_is_safe_for_public_logs(api):
    def handle(request):
        raise httpx.ReadTimeout("private-exception-value", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as session,
        pytest.raises(api.RunalyzeError) as error,
    ):
        api.RunalyzeClient(session, "secret-example").upload_fit("keep-123.fit", b"fit")
    assert "private-exception-value" not in str(error.value)
    assert "secret-example" not in str(error.value)


def test_read_timeout_retries_queue_query_without_reuploading(api, monkeypatch):
    calls = []
    monkeypatch.setattr("time.sleep", lambda _: None)

    def handle(request):
        calls.append(request.method)
        if len(calls) == 1:
            raise httpx.ReadTimeout("private-timeout", request=request)
        return httpx.Response(200, json={"queue_id": 123, "activity_id": 456})

    with httpx.Client(transport=httpx.MockTransport(handle)) as session:
        status = api.RunalyzeClient(session, "example").get_import_status(123)
    assert status["activity_id"] == 456
    assert calls == ["GET", "GET"]


@pytest.mark.parametrize("method,expected_calls", [("GET", 2), ("POST", 1)])
def test_network_recovery_is_bounded_and_never_retries_upload(
    api, monkeypatch, method, expected_calls
):
    calls = []
    monkeypatch.setattr("time.sleep", lambda _: None)

    def handle(request):
        calls.append(request.method)
        raise httpx.ReadTimeout("private-timeout", request=request)

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as session,
        pytest.raises(api.RunalyzeError) as error,
    ):
        client = api.RunalyzeClient(session, "secret-example")
        if method == "GET":
            client.get_import_status(123)
        else:
            client.upload_fit("keep.fit", b"fit")
    assert calls == [method] * expected_calls
    assert "private-timeout" not in str(error.value)
    assert "secret-example" not in str(error.value)
