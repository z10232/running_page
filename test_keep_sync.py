import importlib
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def keep_sync(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "run_page"))
    return importlib.import_module("keep_sync")


@pytest.fixture
def run_data(keep_sync, monkeypatch):
    points = [
        {
            "latitude": 30.0 + index * 0.001,
            "longitude": 120.0,
            "altitude": altitude,
            "timestamp": index * 100,
        }
        for index, altitude in enumerate([100, 110, 120, 130, 140])
    ]
    monkeypatch.setattr(keep_sync, "decode_runmap_data", lambda *args: deepcopy(points))
    return {
        "data": {
            "id": "example_42_rn",
            "startTime": 1700000000000,
            "endTime": 1700000060000,
            "duration": 60,
            "distance": 500,
            "dataType": "outdoorRunning",
            "heartRate": None,
            "geoPoints": "encoded",
            "timezone": "Asia/Shanghai",
            "region": "China",
        }
    }


def test_elevation_is_computed_without_exporting_gpx(keep_sync, run_data, monkeypatch):
    def reject_file_export(*args):
        pytest.fail("普通同步不应导出 GPX 文件")

    monkeypatch.setattr(keep_sync, "download_keep_gpx", reject_file_export)
    activity = keep_sync.parse_raw_data_to_nametuple(run_data, [], [])
    assert activity.elevation_gain == pytest.approx(40)


def test_export_option_preserves_elevation(keep_sync, run_data):
    activity = keep_sync.parse_raw_data_to_nametuple(
        run_data, ["42"], [], with_gpx=True
    )
    assert activity.elevation_gain == pytest.approx(40)


def test_missing_gps_does_not_block_sync(keep_sync, run_data):
    run_data["data"]["geoPoints"] = None
    activity = keep_sync.parse_raw_data_to_nametuple(run_data, [], [])
    assert activity.elevation_gain is None
    assert activity.distance == 500
    assert activity.map.summary_polyline == ""


@pytest.mark.parametrize("with_gpx", [False, True])
@pytest.mark.parametrize("keep_elevation", [123.5, 0.0])
def test_keep_elevation_takes_precedence_over_gps(
    keep_sync, run_data, with_gpx, keep_elevation
):
    run_data["data"]["accumulativeUpliftedHeight"] = keep_elevation
    activity = keep_sync.parse_raw_data_to_nametuple(
        run_data, ["42"], [], with_gpx=with_gpx
    )
    assert activity.elevation_gain == keep_elevation


def test_keep_elevation_is_preserved_without_gps(keep_sync, run_data):
    run_data["data"]["geoPoints"] = None
    run_data["data"]["accumulativeUpliftedHeight"] = 123.5
    activity = keep_sync.parse_raw_data_to_nametuple(run_data, [], [])
    assert activity.elevation_gain == 123.5
