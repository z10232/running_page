"""把 Keep 跑步详情导出为保留原始测量值的 FIT 活动。"""

import eviltransform
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.event_message import EventMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.record_message import RecordMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (
    Activity,
    Event,
    EventType,
    FileType,
    LapTrigger,
    Manufacturer,
    SessionTrigger,
    Sport,
    SubSport,
)
from keep_sync import (
    TIMESTAMP_THRESHOLD_IN_DECISECOND,
    TRANS_GCJ02_TO_WGS84,
    decode_runmap_data,
)


def build_keep_fit(activity: dict) -> bytes:
    """接收 Keep 详情的 data 对象；缺失的测量值保持缺失，0 爬升有效。"""
    start_ms = activity["startTime"]
    end_ms = activity["endTime"]
    sub_sport = (
        SubSport.INDOOR_RUNNING
        if activity["dataType"] == "indoorRunning"
        else SubSport.GENERIC
    )
    records = _build_records(activity)

    file_id = FileIdMessage()
    file_id.type = FileType.ACTIVITY
    # 通用软件导出标记，不冒充运动手表或其他硬件。
    file_id.manufacturer = Manufacturer.DEVELOPMENT
    file_id.product = 0
    file_id.serial_number = 0
    file_id.product_name = "running_page/Keep"
    file_id.time_created = _fit_timestamp(start_ms)

    lap = LapMessage()
    _set_summary(lap, activity)
    lap.event = Event.LAP
    lap.event_type = EventType.STOP
    lap.lap_trigger = LapTrigger.SESSION_END
    lap.sport = Sport.RUNNING
    lap.sub_sport = sub_sport

    session = SessionMessage()
    _set_summary(session, activity)
    session.event = Event.SESSION
    session.event_type = EventType.STOP
    session.sport = Sport.RUNNING
    session.sub_sport = sub_sport
    session.first_lap_index = 0
    session.num_laps = 1
    session.trigger = SessionTrigger.ACTIVITY_END

    summary = ActivityMessage()
    summary.timestamp = _fit_timestamp(end_ms)
    summary.total_timer_time = activity["duration"]
    summary.num_sessions = 1
    summary.type = Activity.MANUAL
    summary.event = Event.ACTIVITY
    summary.event_type = EventType.STOP

    builder = FitFileBuilder(auto_define=True)
    builder.add(file_id)
    builder.add(_timer_event(start_ms, EventType.START))
    builder.add_all(records)
    builder.add(_timer_event(end_ms, EventType.STOP_ALL))
    builder.add_all([lap, session, summary])
    return builder.build().to_bytes()


def _set_summary(message: LapMessage | SessionMessage, activity: dict) -> None:
    message.message_index = 0
    message.start_time = _fit_timestamp(activity["startTime"])
    message.timestamp = _fit_timestamp(activity["endTime"])
    message.total_elapsed_time = (activity["endTime"] - activity["startTime"]) / 1000
    message.total_timer_time = activity["duration"]
    message.total_distance = activity["distance"]

    ascent = activity.get("accumulativeUpliftedHeight")
    if ascent is not None:
        # FIT 标准累计爬升字段的精度为 1 米，由编码库完成量化。
        message.total_ascent = ascent
    average_hr = (activity.get("heartRate") or {}).get("averageHeartRate")
    if average_hr is not None and average_hr > 0:
        message.avg_heart_rate = average_hr


def _timer_event(timestamp_ms: int, event_type: EventType) -> EventMessage:
    event = EventMessage()
    event.timestamp = _fit_timestamp(timestamp_ms)
    event.event = Event.TIMER
    event.event_type = event_type
    return event


def _build_records(activity: dict) -> list[RecordMessage]:
    records: dict[int, RecordMessage] = {}
    if activity.get("geoPoints"):
        points = decode_runmap_data(activity["geoPoints"], is_geo=True)
        for point in points:
            record = _sample_record(records, point, activity["startTime"])
            if record is None:
                continue
            latitude = point.get("latitude")
            longitude = point.get("longitude")
            if latitude is not None and longitude is not None:
                if TRANS_GCJ02_TO_WGS84:
                    latitude, longitude = eviltransform.gcj2wgs(latitude, longitude)
                record.position_lat = latitude
                record.position_long = longitude
            if point.get("altitude") is not None:
                record.altitude = point["altitude"]
                record.enhanced_altitude = point["altitude"]
            if point.get("currentTotalDistance") is not None:
                record.distance = point["currentTotalDistance"]

    heart_rates = (activity.get("heartRate") or {}).get("heartRates")
    if heart_rates:
        for point in decode_runmap_data(heart_rates):
            heart_rate = point.get("beatsPerMinute")
            if heart_rate is None or heart_rate <= 0:
                continue
            record = _sample_record(records, point, activity["startTime"])
            if record is not None:
                record.heart_rate = heart_rate

    if not records:
        # FIT 活动至少有一条记录；只写真实结束时刻和官方距离，不补采样点。
        finish = _record_at(activity["endTime"])
        finish.distance = activity["distance"]
        return [finish]
    return [records[timestamp] for timestamp in sorted(records)]


def _sample_record(
    records: dict[int, RecordMessage], point: dict, start_ms: int
) -> RecordMessage | None:
    timestamp = point.get("timestamp")
    if timestamp is None:
        timestamp = point.get("unixTimestamp")
    if timestamp is None:
        return None
    timestamp_ms = round(timestamp * 100)
    if timestamp <= TIMESTAMP_THRESHOLD_IN_DECISECOND:
        timestamp_ms += start_ms

    if timestamp_ms not in records:
        records[timestamp_ms] = _record_at(timestamp_ms)
    return records[timestamp_ms]


def _record_at(timestamp_ms: int) -> RecordMessage:
    record = RecordMessage()
    record.timestamp = _fit_timestamp(timestamp_ms)
    if timestamp_ms % 1000:
        record.time128 = (timestamp_ms % 1000) / 1000
    return record


def _fit_timestamp(timestamp_ms: int) -> int:
    # FIT 普通时间字段以整秒保存，采样记录的小数部分由 time128 保留。
    return timestamp_ms // 1000 * 1000
