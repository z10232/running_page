import importlib
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import eviltransform
import pytest
from fit_tool.data_message import DataMessage
from fit_tool.fit_file import FitFile
from fit_tool.profile.profile_type import (
    Event,
    EventType,
    FileType,
    Manufacturer,
    Sport,
    SubSport,
)
from garmin_fit_sdk import Decoder, Stream


@pytest.fixture
def keep_fit(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).parent / "run_page"))
    return importlib.import_module("keep_fit")


@pytest.fixture
def activity():
    return {
        "id": "example_42_rn",
        "dataType": "outdoorRunning",
        "startTime": 1700000000000,
        "endTime": 1700000060000,
        "duration": 55.5,
        "distance": 1234.56,
        "accumulativeUpliftedHeight": 12.4,
        "heartRate": {"averageHeartRate": 146, "heartRates": None},
        "geoPoints": None,
    }


def decode_fit(content):
    assert isinstance(content, bytes)
    fit = FitFile.from_bytes(content)
    report = fit.validate()
    assert not report.has_errors, report.errors
    messages = {}
    for record in fit.records:
        if isinstance(record.message, DataMessage):
            messages.setdefault(record.message.name, []).append(record.message)
    return messages


def install_samples(keep_fit, monkeypatch, activity, gps=(), heart_rates=()):
    activity["geoPoints"] = "gps" if gps else None
    activity["heartRate"]["heartRates"] = "heart_rates" if heart_rates else None
    samples = {"gps": gps, "heart_rates": heart_rates}
    monkeypatch.setattr(
        keep_fit,
        "decode_runmap_data",
        lambda text, is_geo=False: deepcopy(samples[text]),
    )


def test_official_summary_and_timer_events_are_preserved(keep_fit, activity):
    content = keep_fit.build_keep_fit(activity)
    messages = decode_fit(content)
    file_id = messages["file_id"][0]
    assert file_id.type == FileType.ACTIVITY.value
    assert file_id.manufacturer == Manufacturer.DEVELOPMENT.value
    assert file_id.time_created == activity["startTime"]
    for summary_name in ("lap", "session"):
        summary = messages[summary_name][0]
        assert summary.start_time == activity["startTime"]
        assert summary.timestamp == activity["endTime"]
        assert summary.total_distance == pytest.approx(activity["distance"], abs=0.01)
        assert summary.total_timer_time == activity["duration"]
        assert summary.total_elapsed_time == 60
        assert summary.total_ascent == pytest.approx(12.4, abs=0.5)
        assert summary.avg_heart_rate == 146
    session = messages["session"][0]
    assert session.sport == Sport.RUNNING.value
    assert session.first_lap_index == 0
    assert session.num_laps == 1
    assert messages["activity"][0].num_sessions == 1
    assert messages["activity"][0].total_timer_time == 55.5
    assert [
        (event.event, event.event_type, event.timestamp) for event in messages["event"]
    ] == [
        (Event.TIMER.value, EventType.START.value, activity["startTime"]),
        (Event.TIMER.value, EventType.STOP_ALL.value, activity["endTime"]),
    ]
    decoded, errors = Decoder(Stream.from_byte_array(bytearray(content))).read()
    assert not errors
    assert decoded["session_mesgs"][0]["total_distance"] == pytest.approx(1234.56)
    assert decoded["session_mesgs"][0]["total_timer_time"] == 55.5
    assert decoded["session_mesgs"][0]["start_time"] == datetime.fromtimestamp(
        activity["startTime"] / 1000, UTC
    )


@pytest.mark.parametrize("absolute", [False, True])
@pytest.mark.parametrize("timestamp_key", ["timestamp", "unixTimestamp"])
def test_gps_heart_rate_and_altitude_use_real_sample_times(
    keep_fit, activity, monkeypatch, absolute, timestamp_key
):
    origin = activity["startTime"] // 100 if absolute else 0
    gps = [
        {
            timestamp_key: origin,
            "latitude": 30.0,
            "longitude": 120.0,
            "altitude": 0.0,
            "currentTotalDistance": 0.0,
        },
        {
            timestamp_key: origin + 100,
            "latitude": 30.001,
            "longitude": 120.001,
            "altitude": 100.2,
            "currentTotalDistance": 300.5,
        },
    ]
    heart_rates = [
        {"timestamp": origin, "beatsPerMinute": 140},
        {"timestamp": origin + 50, "beatsPerMinute": 145},
        {"timestamp": origin + 100, "beatsPerMinute": 150},
    ]
    install_samples(keep_fit, monkeypatch, activity, gps, heart_rates)
    original = deepcopy(activity)
    records = decode_fit(keep_fit.build_keep_fit(activity))["record"]
    assert activity == original
    assert [record.timestamp for record in records] == [
        activity["startTime"] + delta for delta in (0, 5000, 10000)
    ]
    assert [record.heart_rate for record in records] == [140, 145, 150]
    for record, point in ((records[0], gps[0]), (records[2], gps[1])):
        lat, lon = eviltransform.gcj2wgs(point["latitude"], point["longitude"])
        assert record.position_lat == pytest.approx(lat, abs=1e-7)
        assert record.position_long == pytest.approx(lon, abs=1e-7)
        assert record.altitude == pytest.approx(point["altitude"], abs=0.1)
        assert record.distance == point["currentTotalDistance"]
    assert records[1].position_lat is None
    assert records[1].position_long is None
    assert records[1].altitude is None
    assert records[1].distance is None


def test_indoor_running_preserves_heart_rate_without_inventing_gps(
    keep_fit, activity, monkeypatch
):
    activity["dataType"] = "indoorRunning"
    heart_rates = [
        {"timestamp": 0, "beatsPerMinute": 135},
        {"timestamp": 200, "beatsPerMinute": 155},
    ]
    install_samples(keep_fit, monkeypatch, activity, heart_rates=heart_rates)
    messages = decode_fit(keep_fit.build_keep_fit(activity))
    assert messages["session"][0].sub_sport == SubSport.INDOOR_RUNNING.value
    records = messages["record"]
    assert len(records) == 2
    assert [record.heart_rate for record in records] == [135, 155]
    assert [record.timestamp for record in records] == [
        activity["startTime"],
        activity["startTime"] + 20000,
    ]
    assert all(
        record.position_lat is None
        and record.position_long is None
        and record.altitude is None
        for record in records
    )


def test_activity_without_samples_has_only_a_real_finish_record(keep_fit, activity):
    activity["heartRate"] = None
    messages = decode_fit(keep_fit.build_keep_fit(activity))
    records = messages["record"]
    assert len(records) == 1
    finish = records[0]
    assert finish.timestamp == activity["endTime"]
    assert finish.distance == activity["distance"]
    assert finish.position_lat is None
    assert finish.position_long is None
    assert finish.altitude is None
    assert finish.heart_rate is None
    assert finish.power is None
    assert finish.cadence is None
    assert finish.speed is None


@pytest.mark.parametrize("elevation", [0.0, None, "missing"])
def test_zero_ascent_is_valid_and_absent_ascent_stays_absent(
    keep_fit, activity, elevation
):
    if elevation == "missing":
        activity.pop("accumulativeUpliftedHeight")
    else:
        activity["accumulativeUpliftedHeight"] = elevation
    messages = decode_fit(keep_fit.build_keep_fit(activity))
    expected = 0 if elevation == 0 else None
    assert messages["lap"][0].total_ascent == expected
    assert messages["session"][0].total_ascent == expected


def test_untimed_samples_are_not_given_fabricated_timestamps(
    keep_fit, activity, monkeypatch
):
    gps = [{"latitude": 30.0, "longitude": 120.0, "altitude": 50}]
    heart_rates = [{"beatsPerMinute": 155}]
    install_samples(keep_fit, monkeypatch, activity, gps, heart_rates)
    records = decode_fit(keep_fit.build_keep_fit(activity))["record"]
    assert len(records) == 1
    assert records[0].timestamp == activity["endTime"]
    assert records[0].position_lat is None
    assert records[0].heart_rate is None


def test_missing_altitude_is_not_replaced_with_zero(keep_fit, activity, monkeypatch):
    gps = [{"timestamp": 0, "latitude": 30.0, "longitude": 120.0}]
    install_samples(keep_fit, monkeypatch, activity, gps=gps)
    record = decode_fit(keep_fit.build_keep_fit(activity))["record"][0]
    assert record.position_lat is not None
    assert record.altitude is None


def test_zero_distance_and_unknown_average_heart_rate_keep_their_meaning(
    keep_fit, activity
):
    activity["distance"] = 0
    activity["heartRate"]["averageHeartRate"] = None
    messages = decode_fit(keep_fit.build_keep_fit(activity))
    assert messages["session"][0].total_distance == 0
    assert messages["lap"][0].total_distance == 0
    assert messages["session"][0].avg_heart_rate is None
    assert messages["lap"][0].avg_heart_rate is None


def test_summary_metrics_are_not_invented_from_samples(keep_fit, activity, monkeypatch):
    activity.pop("accumulativeUpliftedHeight")
    activity["heartRate"]["averageHeartRate"] = None
    gps = [
        {"timestamp": 0, "latitude": 30.0, "longitude": 120.0, "altitude": 100},
        {"timestamp": 100, "latitude": 30.001, "longitude": 120.0, "altitude": 150},
    ]
    heart_rates = [
        {"timestamp": 0, "beatsPerMinute": 130},
        {"timestamp": 100, "beatsPerMinute": 160},
    ]
    install_samples(keep_fit, monkeypatch, activity, gps, heart_rates)
    messages = decode_fit(keep_fit.build_keep_fit(activity))
    assert messages["session"][0].total_ascent is None
    assert messages["lap"][0].total_ascent is None
    assert messages["session"][0].avg_heart_rate is None


@pytest.mark.parametrize("absolute", [False, True])
def test_fractional_second_samples_keep_their_real_time(
    keep_fit, activity, monkeypatch, absolute
):
    origin = activity["startTime"] // 100 if absolute else 0
    heart_rates = [{"timestamp": origin + 5, "beatsPerMinute": 150}]
    install_samples(keep_fit, monkeypatch, activity, heart_rates=heart_rates)
    record = decode_fit(keep_fit.build_keep_fit(activity))["record"][0]
    assert record.timestamp == activity["startTime"]
    assert record.time128 == 0.5


def test_fractional_activity_boundaries_do_not_move_timer_after_first_record(
    keep_fit, activity, monkeypatch
):
    activity["startTime"] += 600
    activity["endTime"] += 600
    heart_rates = [{"timestamp": 0, "beatsPerMinute": 150}]
    install_samples(keep_fit, monkeypatch, activity, heart_rates=heart_rates)
    messages = decode_fit(keep_fit.build_keep_fit(activity))
    start_second_ms = activity["startTime"] // 1000 * 1000
    assert messages["event"][0].timestamp == start_second_ms
    assert messages["session"][0].start_time == start_second_ms
    assert messages["record"][0].timestamp == start_second_ms
    assert messages["record"][0].time128 == pytest.approx(0.6, abs=1 / 128)
    assert messages["session"][0].total_elapsed_time == 60


def test_rebuilding_the_same_activity_produces_identical_fit_bytes(
    keep_fit, activity, monkeypatch
):
    gps = [
        {"timestamp": 0, "latitude": 30.0, "longitude": 120.0, "altitude": 10},
        {"timestamp": 100, "latitude": 30.001, "longitude": 120.0, "altitude": 20},
    ]
    heart_rates = [
        {"timestamp": 0, "beatsPerMinute": 130},
        {"timestamp": 100, "beatsPerMinute": 160},
    ]
    install_samples(keep_fit, monkeypatch, activity, gps, heart_rates)
    assert keep_fit.build_keep_fit(activity) == keep_fit.build_keep_fit(
        deepcopy(activity)
    )
