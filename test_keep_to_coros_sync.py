import importlib
import json
from pathlib import Path
from unittest.mock import Mock

import pytest


@pytest.fixture
def sync(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "run_page"))
    return importlib.import_module("keep_to_coros_sync")


@pytest.fixture
def state_file(tmp_path):
    path = tmp_path / "keep2coros.json"
    path.write_text(json.dumps({"baseline_ids": ["1"], "uploaded": {}, "pending": {}}))
    return path


@pytest.fixture
def keep_runs(sync, monkeypatch):
    details = {
        "2": {"id": "user_2_rn", "startTime": 1700000000000, "duration": 60},
        "3": {"id": "user_3_rn", "startTime": 1700000100000, "duration": 90},
    }
    monkeypatch.setattr(
        sync, "get_to_download_runs_ids", lambda *_: ["user_1_rn", "user_2_rn"]
    )
    fetch = Mock(
        side_effect=lambda _, __, run_id, ___: {"data": details[run_id.split("_")[1]]}
    )
    monkeypatch.setattr(sync, "get_single_run_data", fetch)
    monkeypatch.setattr(
        sync, "build_keep_fit", lambda data: b"fit:" + data["id"].encode()
    )
    monkeypatch.setattr(sync.time, "sleep", lambda _: None)
    return fetch


def client(sync):
    coros = Mock(spec=sync.CorosClient)
    coros.find_activity.return_value = None
    coros.import_tasks.return_value = []
    coros.submit_fit.return_value = "task-2"
    return coros


def read_state(path):
    return json.loads(path.read_text())


def test_fixed_baseline_is_excluded_independently_of_main_database(
    sync, state_file, keep_runs
):
    coros = client(sync)
    coros.import_tasks.side_effect = [
        [],
        [{"id": "task-2", "status": 2, "errorSize": 0}],
    ]
    assert sync.run_sync(object(), {}, coros, state_file) == 0
    assert keep_runs.call_args.args[2] == "user_2_rn"
    coros.submit_fit.assert_called_once_with("keep-2.fit", b"fit:user_2_rn")
    saved = read_state(state_file)
    assert saved["baseline_ids"] == ["1"]
    assert saved["uploaded"] == {"2": {"task_id": "task-2"}}
    assert saved["pending"] == {}
    assert len(keep_runs.call_args_list) == 1


def test_target_activity_recovers_lost_local_receipt(sync, state_file, keep_runs):
    coros = client(sync)
    coros.find_activity.return_value = "existing-activity"
    assert sync.run_sync(object(), {}, coros, state_file) == 0
    assert read_state(state_file)["uploaded"] == {
        "2": {"activity_id": "existing-activity"}
    }
    coros.submit_fit.assert_not_called()


def test_uploaded_id_is_not_fetched_again(sync, state_file, keep_runs):
    state = read_state(state_file)
    state["uploaded"]["2"] = {"task_id": "completed"}
    state_file.write_text(json.dumps(state))
    coros = client(sync)
    assert sync.run_sync(object(), {}, coros, state_file) == 0
    keep_runs.assert_not_called()
    coros.submit_fit.assert_not_called()


def test_processing_task_is_persisted_without_claiming_upload_success(
    sync, state_file, keep_runs
):
    coros = client(sync)
    coros.import_tasks.side_effect = [
        [],
        *[[{"id": "task-2", "status": 1, "errorSize": 0}]] * 25,
    ]
    assert sync.run_sync(object(), {}, coros, state_file) == 1
    saved = read_state(state_file)
    assert saved["uploaded"] == {}
    assert saved["pending"] == {"2": "task-2"}


def test_resume_pending_task_without_submitting_a_second_file(
    sync, state_file, keep_runs
):
    state = read_state(state_file)
    state["pending"]["2"] = "task-2"
    state_file.write_text(json.dumps(state))
    coros = client(sync)
    coros.import_tasks.side_effect = [
        [{"id": "task-2", "status": 3, "errorSize": 0}],
        [{"id": "task-2", "status": 2, "errorSize": 0}],
    ]
    assert sync.run_sync(object(), {}, coros, state_file) == 0
    assert read_state(state_file)["uploaded"] == {"2": {"task_id": "task-2"}}
    assert read_state(state_file)["pending"] == {}
    coros.submit_fit.assert_not_called()


def test_lost_local_receipt_recovers_task_by_deterministic_filename(
    sync, state_file, keep_runs
):
    coros = client(sync)
    task = {
        "id": "task-2",
        "status": 2,
        "errorSize": 0,
        "originalFilename": "keep-2.fit",
    }
    coros.import_tasks.return_value = [task]
    assert sync.run_sync(object(), {}, coros, state_file) == 0
    assert read_state(state_file)["uploaded"] == {"2": {"task_id": "task-2"}}
    coros.submit_fit.assert_not_called()


def test_failed_task_is_retried_on_next_run(sync, state_file, keep_runs):
    state = read_state(state_file)
    state["pending"]["2"] = "failed-task"
    state_file.write_text(json.dumps(state))
    coros = client(sync)
    failed = {
        "id": "failed-task",
        "status": 2,
        "errorSize": 1,
        "originalFilename": "keep-2.fit",
    }
    coros.import_tasks.return_value = [failed]
    assert sync.run_sync(object(), {}, coros, state_file) == 1
    assert read_state(state_file)["pending"] == {}
    assert read_state(state_file)["uploaded"] == {}
    coros.submit_fit.assert_not_called()

    coros.import_tasks.side_effect = [
        [failed],
        [{"id": "task-2", "status": 2, "errorSize": 0}],
    ]
    assert sync.run_sync(object(), {}, coros, state_file) == 0
    coros.submit_fit.assert_called_once()
    assert read_state(state_file)["uploaded"] == {"2": {"task_id": "task-2"}}


def test_missing_pending_task_is_kept_for_reconciliation(sync, state_file, keep_runs):
    state = read_state(state_file)
    state["pending"]["2"] = "unlisted-task"
    state_file.write_text(json.dumps(state))
    coros = client(sync)
    assert sync.run_sync(object(), {}, coros, state_file) == 1
    coros.submit_fit.assert_not_called()
    assert read_state(state_file)["pending"] == {"2": "unlisted-task"}


def test_partial_success_is_saved_and_failure_does_not_expose_payload(
    sync, state_file, keep_runs, monkeypatch, capsys
):
    monkeypatch.setattr(
        sync,
        "get_to_download_runs_ids",
        lambda *_: ["user_2_rn", "user_3_rn"],
    )
    coros = client(sync)
    coros.find_activity.side_effect = [
        RuntimeError("private-response"),
        "already-there",
    ]
    assert sync.run_sync(object(), {}, coros, state_file) == 1
    assert read_state(state_file)["uploaded"] == {"3": {"activity_id": "already-there"}}
    assert "private-response" not in capsys.readouterr().out


def test_missing_baseline_manifest_cannot_import_all_history(sync, tmp_path):
    coros = client(sync)
    with pytest.raises(sync.SyncError, match="基线"):
        sync.run_sync(object(), {}, coros, tmp_path / "missing.json")
    coros.submit_fit.assert_not_called()


def test_explicit_sample_upload_selects_only_that_baseline_id(
    sync, state_file, keep_runs, monkeypatch
):
    monkeypatch.setattr(
        sync, "get_to_download_runs_ids", lambda *_: ["user_1_rn", "user_2_rn"]
    )
    monkeypatch.setattr(
        sync,
        "get_single_run_data",
        lambda *_: {
            "data": {"id": "user_1_rn", "startTime": 1700000000000, "duration": 60}
        },
    )
    coros = client(sync)
    coros.find_activity.return_value = "sample-activity"
    assert sync.run_sync(object(), {}, coros, state_file, sample_id="1") == 0
    assert read_state(state_file)["uploaded"] == {
        "1": {"activity_id": "sample-activity"}
    }
    assert read_state(state_file)["baseline_ids"] == ["1"]


def test_each_success_is_saved_before_processing_the_next_activity(
    sync, state_file, keep_runs, monkeypatch
):
    monkeypatch.setattr(
        sync,
        "get_to_download_runs_ids",
        lambda *_: ["user_2_rn", "user_3_rn"],
    )
    coros = client(sync)

    def find(activity):
        if activity["id"] == "user_2_rn":
            return "first-success"
        assert read_state(state_file)["uploaded"] == {
            "2": {"activity_id": "first-success"}
        }
        raise RuntimeError("private-network-failure")

    coros.find_activity.side_effect = find
    assert sync.run_sync(object(), {}, coros, state_file) == 1
    assert read_state(state_file)["uploaded"] == {"2": {"activity_id": "first-success"}}


def test_pending_id_is_saved_before_polling_and_is_not_a_success(
    sync, state_file, keep_runs
):
    coros = client(sync)

    def tasks():
        if coros.submit_fit.called:
            assert read_state(state_file)["pending"] == {"2": "task-2"}
            assert read_state(state_file)["uploaded"] == {}
            return [{"id": "task-2", "status": 2, "errorSize": 0}]
        return []

    coros.import_tasks.side_effect = tasks
    assert sync.run_sync(object(), {}, coros, state_file) == 0


def test_unknown_sample_id_cannot_expand_to_other_runs(sync, state_file, keep_runs):
    coros = client(sync)
    with pytest.raises(sync.SyncError, match="样例"):
        sync.run_sync(object(), {}, coros, state_file, sample_id="999")
    keep_runs.assert_not_called()
    coros.submit_fit.assert_not_called()
    assert read_state(state_file)["uploaded"] == {}


@pytest.mark.parametrize("sample_id", ["all", "1,2", ""])
def test_cli_rejects_non_numeric_sample_before_creating_network_sessions(
    sync, monkeypatch, capsys, sample_id
):
    monkeypatch.setattr("sys.argv", ["keep_to_coros_sync.py", "--sample-id", sample_id])
    keep_session = Mock()
    coros_session = Mock()
    monkeypatch.setattr(sync.requests, "Session", keep_session)
    monkeypatch.setattr(sync.httpx, "Client", coros_session)
    with pytest.raises(SystemExit) as error:
        sync.main()
    assert error.value.code == 2
    assert "数字 Keep ID" in capsys.readouterr().err
    keep_session.assert_not_called()
    coros_session.assert_not_called()


def test_safe_import_failure_reason_is_visible_without_raw_exception(
    sync, state_file, keep_runs, capsys
):
    coros = client(sync)
    coros.find_activity.side_effect = sync.CorosError("高驰账号需要额外验证。")
    assert sync.run_sync(object(), {}, coros, state_file) == 1
    assert "高驰账号需要额外验证。" in capsys.readouterr().out


@pytest.mark.parametrize("error_fields", [{}, {"errorSize": None}])
def test_completed_task_can_omit_zero_error_count(
    sync, state_file, keep_runs, error_fields
):
    coros = client(sync)
    task = {
        "id": "task-2",
        "status": 2,
        "finishSize": 1,
        "size": 1,
        "originalFilename": "keep-2.fit",
        **error_fields,
    }
    coros.import_tasks.return_value = [task]
    assert sync.run_sync(object(), {}, coros, state_file) == 0
    assert read_state(state_file)["uploaded"] == {"2": {"task_id": "task-2"}}
    coros.submit_fit.assert_not_called()
