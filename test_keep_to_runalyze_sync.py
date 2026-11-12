import importlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def sync(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "run_page"))
    return importlib.import_module("keep_to_runalyze_sync")


@pytest.fixture
def state_file(tmp_path):
    path = tmp_path / "keep2runalyze.json"
    path.write_text(json.dumps({"accepted": {}, "pending": {}}))
    return path


@pytest.fixture
def keep_runs(sync, monkeypatch):
    details = {
        "1": {"id": "user_1_rn", "startTime": 1700000000000, "duration": 60},
        "2": {"id": "user_2_rn", "startTime": 1700000100000, "duration": 90},
        "3": {"id": "user_3_rn", "startTime": 1700000200000, "duration": 120},
    }
    monkeypatch.setattr(
        sync, "get_to_download_runs_ids", lambda *_: ["user_1_rn", "user_2_rn"]
    )
    fetch = Mock(
        side_effect=lambda _, __, run_id, ___: {"data": details[run_id.split("_")[1]]}
    )
    monkeypatch.setattr(sync, "get_single_run_data", fetch)
    monkeypatch.setattr(
        sync, "build_keep_fit", lambda data, **kwargs: b"fit:" + data["id"].encode()
    )
    monkeypatch.setattr(sync, "get_keep_altitude_points", Mock(return_value=[]))
    monkeypatch.setattr(sync.time, "sleep", Mock())
    return fetch


def receipt(queue_id):
    return {"sha256": f"digest-{queue_id}", "http_status": 201, "queue_id": queue_id}


def confirmed(queue_id):
    return {**receipt(queue_id), "activity_id": queue_id + 1000}


def client():
    runalyze = Mock(spec=["ping", "upload_fit", "get_import_status"])
    runalyze.upload_fit.side_effect = lambda filename, _: receipt(
        int(filename[5:-4]) + 100
    )
    runalyze.get_import_status.side_effect = lambda queue_id: {
        "queue_id": queue_id,
        "activity_id": queue_id + 1000,
        "status": "anything",
        "state": None,
    }
    return runalyze


def read_state(path):
    return json.loads(path.read_text())


def test_all_keep_runs_are_candidates_without_coros_history_baseline(
    sync, state_file, keep_runs
):
    runalyze = client()
    assert sync.run_sync(object(), {}, runalyze, state_file) == 0
    assert [call.args for call in runalyze.upload_fit.call_args_list] == [
        ("keep-1.fit", b"fit:user_1_rn"),
        ("keep-2.fit", b"fit:user_2_rn"),
    ]
    assert read_state(state_file) == {
        "accepted": {"1": confirmed(101), "2": confirmed(102)},
        "pending": {},
    }


def test_confirmed_upload_is_skipped_before_fetching_keep_detail(
    sync, state_file, keep_runs
):
    state_file.write_text(
        json.dumps({"accepted": {"1": confirmed(101)}, "pending": {}})
    )
    runalyze = client()
    assert sync.run_sync(object(), {}, runalyze, state_file) == 0
    assert [call.args[2] for call in keep_runs.call_args_list] == ["user_2_rn"]
    assert read_state(state_file)["accepted"]["1"] == confirmed(101)
    runalyze.upload_fit.assert_called_once_with("keep-2.fit", b"fit:user_2_rn")


def test_queue_receipt_is_saved_before_query_and_confirmed_only_with_activity_id(
    sync, state_file, keep_runs
):
    runalyze = client()

    def status(queue_id):
        saved = read_state(state_file)
        keep_id = str(queue_id - 100)
        assert saved["pending"][keep_id] == receipt(queue_id)
        assert keep_id not in saved["accepted"]
        return {"queue_id": queue_id, "activity_id": queue_id + 1000}

    runalyze.get_import_status.side_effect = status
    assert sync.run_sync(object(), {}, runalyze, state_file) == 0
    assert read_state(state_file)["accepted"] == {
        "1": confirmed(101),
        "2": confirmed(102),
    }
    assert read_state(state_file)["pending"] == {}
    assert not state_file.with_suffix(".tmp").exists()


def test_unconfirmed_queue_is_saved_and_next_run_queries_without_uploading(
    sync, state_file, keep_runs
):
    runalyze = client()
    runalyze.get_import_status.side_effect = lambda queue_id: {
        "queue_id": queue_id,
        "activity_id": None,
        "status": "queued",
        "state": None,
    }
    assert sync.run_sync(object(), {}, runalyze, state_file) == 2
    assert read_state(state_file) == {
        "accepted": {},
        "pending": {"1": receipt(101), "2": receipt(102)},
    }
    runalyze.upload_fit.reset_mock()
    keep_runs.reset_mock()
    runalyze.get_import_status.side_effect = lambda queue_id: {
        "queue_id": queue_id,
        "activity_id": queue_id + 1000,
    }
    assert sync.run_sync(object(), {}, runalyze, state_file) == 0
    runalyze.upload_fit.assert_not_called()
    keep_runs.assert_not_called()
    assert read_state(state_file)["pending"] == {}


@pytest.mark.parametrize("activity_id", [None, 0, -1, "123", True])
def test_status_text_alone_never_claims_final_import(
    sync, state_file, keep_runs, activity_id, capsys
):
    runalyze = client()
    runalyze.get_import_status.side_effect = lambda queue_id: {
        "queue_id": queue_id,
        "activity_id": activity_id,
        "status": "success",
        "state": "finished",
    }
    assert sync.run_sync(object(), {}, runalyze, state_file, sample_id="1") == 1
    assert read_state(state_file)["accepted"] == {}
    assert read_state(state_file)["pending"] == {"1": receipt(101)}
    assert "已入库" not in capsys.readouterr().out


def test_existing_pending_is_checked_before_new_uploads(sync, state_file, keep_runs):
    state_file.write_text(json.dumps({"accepted": {}, "pending": {"2": receipt(102)}}))
    runalyze = client()

    def upload(filename, content):
        assert read_state(state_file)["accepted"] == {"2": confirmed(102)}
        return receipt(101)

    runalyze.upload_fit.side_effect = upload
    assert sync.run_sync(object(), {}, runalyze, state_file) == 0
    assert [call.args[2] for call in keep_runs.call_args_list] == ["user_1_rn"]


def test_polling_wait_is_shared_by_the_batch_instead_of_each_pending_record(
    sync, state_file, keep_runs
):
    runalyze = client()
    runalyze.get_import_status.side_effect = lambda queue_id: {
        "queue_id": queue_id,
        "activity_id": None,
    }
    assert sync.run_sync(object(), {}, runalyze, state_file) == 2
    assert runalyze.get_import_status.call_count == 12
    pauses = [call.args[0] for call in sync.time.sleep.call_args_list]
    assert pauses.count(5) == 5
    assert pauses.count(1) == 14  # 两次POST和十二次GET分别保持正常请求间隔。


def test_later_poll_can_confirm_without_another_upload(sync, state_file, keep_runs):
    runalyze = client()
    runalyze.get_import_status.side_effect = [
        {"queue_id": 101, "activity_id": None},
        {"queue_id": 101, "activity_id": 1101},
    ]
    assert sync.run_sync(object(), {}, runalyze, state_file, sample_id="1") == 0
    assert read_state(state_file)["accepted"] == {"1": confirmed(101)}
    runalyze.upload_fit.assert_called_once()


def test_conversion_failure_keeps_other_confirmations_and_hides_private_exception(
    sync, state_file, keep_runs, monkeypatch, capsys
):
    build = Mock(side_effect=[ValueError("private-keep-payload"), b"second-fit"])
    monkeypatch.setattr(sync, "build_keep_fit", build)
    runalyze = client()
    assert sync.run_sync(object(), {}, runalyze, state_file) == 1
    runalyze.upload_fit.assert_called_once_with("keep-2.fit", b"second-fit")
    assert set(read_state(state_file)["accepted"]) == {"2"}
    assert "private-keep-payload" not in capsys.readouterr().out


def test_failed_keep_detail_is_not_saved_and_next_record_continues(
    sync, state_file, keep_runs
):
    keep_runs.side_effect = [None, {"data": {"id": "user_2_rn"}}]
    runalyze = client()
    assert sync.run_sync(object(), {}, runalyze, state_file) == 1
    assert set(read_state(state_file)["accepted"]) == {"2"}


@pytest.mark.parametrize("message", ["API Token 无效。", "RUNALYZE 请求已达到限额。"])
def test_api_auth_or_rate_error_stops_batch_and_keeps_earlier_confirmation(
    sync, state_file, keep_runs, monkeypatch, message
):
    monkeypatch.setattr(
        sync,
        "get_to_download_runs_ids",
        lambda *_: ["user_1_rn", "user_2_rn", "user_3_rn"],
    )
    runalyze = client()
    runalyze.upload_fit.side_effect = [receipt(101), sync.RunalyzeError(message)]
    with pytest.raises(sync.RunalyzeError, match=message):
        sync.run_sync(object(), {}, runalyze, state_file)
    assert read_state(state_file)["accepted"] == {"1": confirmed(101)}
    assert len(keep_runs.call_args_list) == 2
    assert runalyze.upload_fit.call_count == 2


def test_status_api_error_keeps_new_pending_receipt_for_next_run(
    sync, state_file, keep_runs
):
    runalyze = client()
    runalyze.get_import_status.side_effect = sync.RunalyzeError("请求频率限制。")
    with pytest.raises(sync.RunalyzeError, match="请求频率"):
        sync.run_sync(object(), {}, runalyze, state_file)
    assert read_state(state_file) == {"accepted": {}, "pending": {"1": receipt(101)}}
    runalyze.upload_fit.assert_called_once()


def test_missing_manifest_stops_before_fetching_or_uploading(sync, tmp_path, keep_runs):
    runalyze = client()
    with pytest.raises(sync.SyncError, match="清单"):
        sync.run_sync(object(), {}, runalyze, tmp_path / "missing.json")
    keep_runs.assert_not_called()
    runalyze.upload_fit.assert_not_called()


def test_manifest_from_coros_cannot_be_used_as_runalyze_acceptance(
    sync, state_file, keep_runs
):
    state_file.write_text(json.dumps({"uploaded": {"1": {"activity_id": "old"}}}))
    runalyze = client()
    with pytest.raises(sync.SyncError, match="清单"):
        sync.run_sync(object(), {}, runalyze, state_file)
    keep_runs.assert_not_called()
    runalyze.upload_fit.assert_not_called()


def test_sample_selects_exactly_one_record_and_batch_later_skips_it(
    sync, state_file, keep_runs
):
    runalyze = client()
    assert sync.run_sync(object(), {}, runalyze, state_file, sample_id="2") == 0
    runalyze.upload_fit.assert_called_once_with("keep-2.fit", b"fit:user_2_rn")
    assert set(read_state(state_file)["accepted"]) == {"2"}
    runalyze.upload_fit.reset_mock()
    assert sync.run_sync(object(), {}, runalyze, state_file) == 0
    runalyze.upload_fit.assert_called_once_with("keep-1.fit", b"fit:user_1_rn")


def test_sample_does_not_query_other_pending_records(sync, state_file, keep_runs):
    state_file.write_text(json.dumps({"accepted": {}, "pending": {"1": receipt(101)}}))
    runalyze = client()
    assert sync.run_sync(object(), {}, runalyze, state_file, sample_id="2") == 0
    assert [call.args for call in runalyze.get_import_status.call_args_list] == [(102,)]
    assert read_state(state_file)["pending"] == {"1": receipt(101)}


def test_unknown_sample_never_falls_back_to_importing_other_records(
    sync, state_file, keep_runs
):
    runalyze = client()
    with pytest.raises(sync.SyncError, match="样例"):
        sync.run_sync(object(), {}, runalyze, state_file, sample_id="999")
    runalyze.upload_fit.assert_not_called()
    keep_runs.assert_not_called()


@pytest.mark.parametrize("sample_id", ["all", "1,2", "", "１２"])
def test_cli_rejects_non_numeric_sample_before_network(
    sync, monkeypatch, capsys, sample_id
):
    monkeypatch.setattr(
        "sys.argv", ["keep_to_runalyze_sync.py", "--sample-id", sample_id]
    )
    keep_session = Mock()
    runalyze_session = Mock()
    monkeypatch.setattr(sync.requests, "Session", keep_session)
    monkeypatch.setattr(sync.httpx, "Client", runalyze_session)
    with pytest.raises(SystemExit) as error:
        sync.main()
    assert error.value.code == 2
    assert "数字 Keep ID" in capsys.readouterr().err
    keep_session.assert_not_called()
    runalyze_session.assert_not_called()


def test_manifest_write_failure_stops_before_submitting_another_activity(
    sync, state_file, keep_runs, monkeypatch
):
    def fail_write(path, content, *args, **kwargs):
        raise OSError("private-filesystem-error")

    monkeypatch.setattr(Path, "write_text", fail_write)
    runalyze = client()
    with pytest.raises(OSError):
        sync.run_sync(object(), {}, runalyze, state_file)
    runalyze.upload_fit.assert_called_once_with("keep-1.fit", b"fit:user_1_rn")
    runalyze.get_import_status.assert_not_called()
    assert read_state(state_file)["accepted"] == {}


def test_cli_reports_safe_api_reason_and_never_exposes_unknown_error(
    sync, state_file, monkeypatch, capsys
):
    monkeypatch.setattr(
        "sys.argv", ["keep_to_runalyze_sync.py", "--manifest", str(state_file)]
    )
    for name in ("KEEP_MOBILE", "KEEP_PASSWORD", "RUNALYZE_API_TOKEN"):
        monkeypatch.setenv(name, "example-value")
    runalyze = client()
    monkeypatch.setattr(sync, "RunalyzeClient", Mock(return_value=runalyze))
    runalyze.ping.side_effect = sync.RunalyzeError("RUNALYZE Token 无效。")
    assert sync.main() == 1
    assert "RUNALYZE Token 无效。" in capsys.readouterr().out
    runalyze.upload_fit.assert_not_called()
    runalyze.ping.side_effect = RuntimeError("private-token-value")
    assert sync.main() == 1
    output = capsys.readouterr().out
    assert "同步已停止" in output
    assert "private-token-value" not in output
    runalyze.upload_fit.assert_not_called()


def test_confirmation_is_saved_before_next_upload(sync, state_file, keep_runs):
    runalyze = client()

    def upload(filename, content):
        if filename == "keep-2.fit":
            assert read_state(state_file) == {
                "accepted": {"1": confirmed(101)},
                "pending": {},
            }
        return receipt(int(filename[5:-4]) + 100)

    runalyze.upload_fit.side_effect = upload
    assert sync.run_sync(object(), {}, runalyze, state_file) == 0


@pytest.mark.parametrize(
    "manifest",
    [
        {"accepted": {"1": receipt(101)}, "pending": {}},
        {"accepted": {}, "pending": {"1": {"http_status": 201}}},
    ],
)
def test_unconfirmed_or_incomplete_receipt_cannot_silently_skip_or_repeat_upload(
    sync, state_file, keep_runs, manifest
):
    state_file.write_text(json.dumps(manifest))
    runalyze = client()
    with pytest.raises(sync.SyncError, match="清单"):
        sync.run_sync(object(), {}, runalyze, state_file)
    runalyze.upload_fit.assert_not_called()
    runalyze.get_import_status.assert_not_called()
    keep_runs.assert_not_called()


def test_no_gps_run_uploads_independent_altitude_curve_in_the_fit(
    sync, state_file, keep_runs, monkeypatch
):
    from fit_tool.data_message import DataMessage
    from fit_tool.fit_file import FitFile
    from keep_fit import build_keep_fit

    activity = {
        "id": "user_1_rn",
        "dataType": "outdoorRunning",
        "startTime": 1700000000000,
        "endTime": 1700000060000,
        "duration": 60,
        "distance": 200,
        "accumulativeUpliftedHeight": 30,
        "heartRate": None,
        "geoPoints": "",
    }
    keep_runs.side_effect = None
    keep_runs.return_value = {"data": activity}
    altitude = Mock(
        return_value=[
            {"timestamp": 50, "altitude": 100},
            {"timestamp": 150, "altitude": 90},
        ]
    )
    monkeypatch.setattr(sync, "get_keep_altitude_points", altitude, raising=False)
    monkeypatch.setattr(sync, "build_keep_fit", build_keep_fit)
    runalyze = client()
    session = object()
    headers = {"Authorization": "test-auth"}

    assert sync.run_sync(session, headers, runalyze, state_file, sample_id="1") == 0
    altitude.assert_called_once_with(session, headers, "user_1_rn")
    fit = FitFile.from_bytes(runalyze.upload_fit.call_args.args[1])
    records = [
        record.message
        for record in fit.records
        if isinstance(record.message, DataMessage) and record.message.name == "record"
    ]
    assert [record.altitude for record in records] == [100, 90]
    assert all(
        record.position_lat is None and record.position_long is None
        for record in records
    )


def test_altitude_fetch_failure_is_retried_next_run_instead_of_uploading_without_it(
    sync, state_file, keep_runs, monkeypatch, capsys
):
    altitude = Mock(side_effect=[RuntimeError("private-server-value"), []])
    monkeypatch.setattr(sync, "get_keep_altitude_points", altitude, raising=False)
    runalyze = client()
    assert sync.run_sync(object(), {}, runalyze, state_file) == 1
    assert set(read_state(state_file)["accepted"]) == {"2"}
    assert "private-server-value" not in capsys.readouterr().out
