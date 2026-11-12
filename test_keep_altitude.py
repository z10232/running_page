import base64
import gzip
import importlib
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests


@pytest.fixture
def keep_altitude(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "run_page"))
    return importlib.import_module("keep_altitude")


def encode_samples(points):
    """合成压缩图表样例，不包含用户海拔数据。"""
    payload = json.dumps(points).encode()
    return base64.b64encode(gzip.compress(payload, mtime=0)).decode()


def altitude_chart(points):
    return {
        "chartMetaType": "upliftedHeight",
        "title": "海拔高度",
        "unit": "米",
        "chartType": "bar",
        "gridType": "altitudeChart",
        "chartData": encode_samples(points),
    }


def session_for(charts):
    response = Mock(spec=requests.Response)
    response.json.return_value = {"data": {"charts": charts}}
    session = Mock(spec=requests.Session)
    session.get.return_value = response
    return session


def test_uses_full_id_and_current_endpoint_with_existing_headers(keep_altitude):
    chart = altitude_chart(
        [
            {"x": 15, "y": 40.5, "min": 0, "max": 0},
            {"x": 30, "y": 38.25, "min": 0, "max": 0},
        ]
    )
    session = session_for([chart])
    headers = {"Authorization": "Bearer example-token"}
    points = keep_altitude.get_keep_altitude_points(session, headers, "example_42_rn")
    session.get.assert_called_once_with(
        "https://api.gotokeep.com/minnow-webapp/v1/sportlog/sportData/chart/example_42_rn",
        headers=headers,
        params={"itemCount": 1000},
        timeout=45,
    )
    session.get.return_value.raise_for_status.assert_called_once()
    assert points == [
        {"timestamp": 150, "altitude": 40.5},
        {"timestamp": 300, "altitude": 38.25},
    ]


def test_bar_uses_y_ignores_waterfall_and_preserves_zero_without_gps(keep_altitude):
    charts = [
        {
            "chartMetaType": "heartRate",
            "chartType": "waterfall",
            "chartData": encode_samples(
                [
                    {"x": 0, "y": 0, "min": 110, "max": 120},
                ]
            ),
        },
        altitude_chart(
            [
                {"x": 0, "y": 0.0, "min": 0, "max": 0},
                {"x": 15, "y": -2.5, "min": 0, "max": 0},
                {"x": 30, "y": 4, "min": 0, "max": 0},
            ]
        ),
    ]
    original = deepcopy(charts)
    points = keep_altitude.get_keep_altitude_points(
        session_for(charts), {}, "example_42_rn"
    )
    assert points == [
        {"timestamp": 0, "altitude": 0.0},
        {"timestamp": 150, "altitude": -2.5},
        {"timestamp": 300, "altitude": 4},
    ]
    assert charts == original
    assert all(set(point) == {"timestamp", "altitude"} for point in points)


@pytest.mark.parametrize("charts", [[], [{"chartMetaType": "heartRate"}]])
def test_absent_altitude_chart_returns_empty(keep_altitude, charts):
    assert (
        keep_altitude.get_keep_altitude_points(session_for(charts), {}, "example_42_rn")
        == []
    )


@pytest.mark.parametrize("chart_data", [None, "", "missing"])
def test_absent_chart_data_returns_empty(keep_altitude, chart_data):
    chart = altitude_chart([])
    if chart_data == "missing":
        del chart["chartData"]
    else:
        chart["chartData"] = chart_data
    assert (
        keep_altitude.get_keep_altitude_points(
            session_for([chart]), {}, "example_42_rn"
        )
        == []
    )


def test_valid_empty_compressed_series_returns_empty(keep_altitude):
    assert (
        keep_altitude.get_keep_altitude_points(
            session_for([altitude_chart([])]), {}, "example_42_rn"
        )
        == []
    )


def test_http_failure_propagates_instead_of_looking_like_missing_altitude(
    keep_altitude,
):
    session = session_for([])
    session.get.return_value.raise_for_status.side_effect = requests.HTTPError(
        "example failure"
    )
    with pytest.raises(requests.HTTPError):
        keep_altitude.get_keep_altitude_points(session, {}, "example_42_rn")
    session.get.return_value.json.assert_not_called()


@pytest.mark.parametrize("chart_data", ["damaged-compressed-data", 0, [], {}])
def test_corrupt_or_non_string_chart_data_raises_safe_error(keep_altitude, chart_data):
    chart = altitude_chart([])
    chart["chartData"] = chart_data
    with pytest.raises(ValueError, match="海拔") as error:
        keep_altitude.get_keep_altitude_points(
            session_for([chart]), {}, "example_42_rn"
        )
    assert "damaged-compressed-data" not in str(error.value)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"data": None},
        {"data": {}},
        {"data": {"charts": {}}},
        {"data": {"charts": [None]}},
    ],
)
def test_malformed_chart_response_is_not_reported_as_empty(keep_altitude, payload):
    session = session_for([])
    session.get.return_value.json.return_value = payload
    with pytest.raises(ValueError, match="海拔"):
        keep_altitude.get_keep_altitude_points(session, {}, "example_42_rn")


@pytest.mark.parametrize(
    "key,value", [("unit", "英尺"), ("chartType", "waterfall"), ("gridType", "other")]
)
def test_changed_altitude_semantics_must_be_confirmed_before_conversion(
    keep_altitude, key, value
):
    chart = altitude_chart([{"x": 0, "y": 5.0}])
    chart[key] = value
    with pytest.raises(ValueError, match="海拔"):
        keep_altitude.get_keep_altitude_points(
            session_for([chart]), {}, "example_42_rn"
        )


@pytest.mark.parametrize(
    "points",
    [
        None,
        {},
        [None],
        [{"x": 0}],
        [{"y": 1}],
        [{"x": -1, "y": 2}],
        [{"x": True, "y": 2}],
        [{"x": 0, "y": False}],
        [{"x": "0", "y": 2}],
        [{"x": 0, "y": "2"}],
        [{"x": [0], "y": 2}],
        [{"x": 0, "y": [2]}],
        [{"x": float("nan"), "y": 2}],
        [{"x": 0, "y": float("inf")}],
        [{"x": float("inf"), "y": 2}],
        [{"x": 0, "y": float("nan")}],
    ],
)
def test_invalid_sample_is_not_silently_skipped(keep_altitude, points):
    with pytest.raises(ValueError, match="海拔"):
        keep_altitude.get_keep_altitude_points(
            session_for([altitude_chart(points)]), {}, "example_42_rn"
        )
