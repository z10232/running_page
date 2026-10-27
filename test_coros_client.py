import hashlib
import importlib
import json
from io import BytesIO
from pathlib import Path
from zipfile import ZipFile

import bcrypt
import httpx
import pytest


@pytest.fixture
def coros(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "run_page"))
    return importlib.import_module("coros_client")


def response(data):
    return httpx.Response(200, json={"result": "0000", "data": data})


def test_phone_password_login_uses_current_web_protocol(coros):
    def handle(request):
        assert str(request.url) == "https://teamcnapi.coros.com/account/login"
        body = json.loads(request.content)
        assert body["account"] == "13800000000"
        assert body["accountType"] == 2
        assert bcrypt.checkpw(
            hashlib.md5(b"example-password").hexdigest().encode(),
            body["p1"].encode(),
        )
        assert body["p2"] == body["p1"][:29]
        assert "example-password" not in request.content.decode()
        return response({"accessToken": "example-token", "userId": "42", "regionId": 2})

    with httpx.Client(transport=httpx.MockTransport(handle)) as session:
        client = coros.CorosClient(session)
        client.login("13800000000", "example-password")
        assert session.headers["accessToken"] == "example-token"
        assert session.cookies.get("CPL-coros-token") == "example-token"


def test_login_failure_does_not_expose_server_payload(coros):
    def handle(request):
        return httpx.Response(200, json={"result": "1002", "message": "private-value"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as session:
        with pytest.raises(coros.CorosError, match="1002") as error:
            coros.CorosClient(session).login("13800000000", "example-password")
        assert "private-value" not in str(error.value)


def test_two_factor_login_is_not_reported_as_success(coros):
    def handle(request):
        return response({"twoFactorRequired": True, "loginTicket": "private-ticket"})

    with (
        httpx.Client(transport=httpx.MockTransport(handle)) as session,
        pytest.raises(coros.CorosError, match="验证"),
    ):
        coros.CorosClient(session).login("13800000000", "example-password")


def test_fit_import_uploads_zip_then_submits_task(coros, monkeypatch):
    import base64

    credentials = {
        "Region": "oss-cn-shenzhen",
        "Bucket": "coros-example",
        "AccessKeyId": "example-id",
        "AccessKeySecret": "example-secret",
        "SecurityToken": "example-sts",
    }
    encoded = base64.b64encode(json.dumps(credentials).encode()).decode()
    uploaded = []

    class Bucket:
        def __init__(self, auth, endpoint, bucket, **kwargs):
            assert endpoint == "https://oss-cn-shenzhen.aliyuncs.com"
            assert bucket == "coros-example"

        def put_object(self, key, content, **kwargs):
            uploaded.append((key, content))

    monkeypatch.setattr(coros.oss2, "Bucket", Bucket)
    fit = b"example-fit-content"
    digest = hashlib.md5(fit).hexdigest()

    def handle(request):
        if request.url.path == "/account/login":
            return response(
                {"accessToken": "example-token", "userId": "42", "regionId": 2}
            )
        if request.url.path == "/api/proxy/oss/sts":
            assert request.headers["accessToken"] == "example-token"
            return httpx.Response(
                200,
                json={
                    "code": 200,
                    "data": {
                        "service": "aliyun",
                        "credentials": "9y78gpoERW4lBNYL" + encoded,
                    },
                },
            )
        assert request.url.path == "/activity/fit/import"
        assert len(uploaded) == 1
        assert request.headers["content-type"].startswith("multipart/form-data;")
        assert 'name="jsonParameter"' in request.content.decode()
        start = request.content.index(b'{"source"')
        end = request.content.index(b"\r\n", start)
        metadata = json.loads(request.content[start:end])
        assert metadata == {
            "source": 1,
            "timezone": 32,
            "bucket": "coros-example",
            "md5": digest,
            "size": len(fit),
            "object": f"fit_zip/42/{digest}.zip",
            "serviceName": "aliyun",
            "oriFileName": "keep-7.fit",
        }
        return response({"id": "task-7"})

    with httpx.Client(transport=httpx.MockTransport(handle)) as session:
        client = coros.CorosClient(session)
        client.login("13800000000", "example-password")
        assert client.submit_fit("keep-7.fit", fit) == "task-7"
    assert uploaded[0][0] == f"fit_zip/42/{digest}.zip"
    with ZipFile(BytesIO(uploaded[0][1])) as archive:
        assert archive.namelist() == [f"{digest}/keep-7.fit"]
        assert archive.read(archive.namelist()[0]) == fit


def test_import_tasks_keep_processing_and_error_states(coros):
    tasks = [
        {"id": "1", "status": 1, "errorSize": 0},
        {"id": "2", "status": 2, "errorSize": 0},
        {"id": "3", "status": 2, "errorSize": 1},
    ]

    def handle(request):
        assert request.url.path == "/activity/fit/getImportSportList"
        assert json.loads(request.content) == {"size": 10}
        return response(tasks)

    with httpx.Client(transport=httpx.MockTransport(handle)) as session:
        assert coros.CorosClient(session).import_tasks() == tasks


def test_existing_activity_is_reconciled_by_start_time_and_duration(coros):
    activity = {"startTime": 1793016000000, "duration": 2833}

    def handle(request):
        assert request.url.path == "/activity/query"
        assert request.url.params["startDay"] == "20261026"
        assert request.url.params["endDay"] == "20261026"
        return response(
            {
                "totalPage": 1,
                "dataList": [
                    {"labelId": "other", "startTime": 1793016000, "workoutTime": 30},
                    {
                        "labelId": "matching",
                        "startTime": 1793016000,
                        "workoutTime": 2833,
                    },
                ],
            }
        )

    with httpx.Client(transport=httpx.MockTransport(handle)) as session:
        assert coros.CorosClient(session).find_activity(activity) == "matching"
