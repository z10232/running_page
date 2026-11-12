"""读取 Keep 独立海拔图表，保留实际采样时间与高度。"""

import math
import zlib

import requests
from keep_sync import decode_runmap_data

ALTITUDE_CHART_API = (
    "https://api.gotokeep.com/minnow-webapp/v1/sportlog/sportData/chart/{run_id}"
)


class KeepAltitudeError(ValueError):
    """海拔响应不可用于导出，调用方应保留活动等待重试。"""


def get_keep_altitude_points(
    session: requests.Session, headers: dict, run_id: str
) -> list[dict]:
    """使用完整 Keep 活动编号，返回相对时间为分秒、海拔为米的采样。"""
    response = session.get(
        ALTITUDE_CHART_API.format(run_id=run_id),
        headers=headers,
        params={"itemCount": 1000},
        timeout=45,
    )
    response.raise_for_status()
    try:
        charts = response.json()["data"]["charts"]
    except (ValueError, KeyError, TypeError):
        raise KeepAltitudeError("Keep 海拔图表响应格式无效。") from None
    if not isinstance(charts, list) or any(not isinstance(c, dict) for c in charts):
        raise KeepAltitudeError("Keep 海拔图表列表格式无效。")
    chart = next(
        (chart for chart in charts if chart.get("chartMetaType") == "upliftedHeight"),
        None,
    )
    if chart is None:
        return []
    chart_data = chart.get("chartData")
    if chart_data is None or chart_data == "":
        return []
    if not isinstance(chart_data, str):
        raise KeepAltitudeError("Keep 海拔图表压缩数据格式无效。")
    if (
        chart.get("chartType") != "bar"
        or chart.get("gridType") != "altitudeChart"
        or chart.get("unit") != "米"
    ):
        raise KeepAltitudeError("Keep 海拔图表类型或单位发生变化，需要核对后再同步。")
    return _decode_altitude_points(chart_data)


def _decode_altitude_points(chart_data: str) -> list[dict]:
    try:
        samples = decode_runmap_data(chart_data)
    except (ValueError, zlib.error):
        raise KeepAltitudeError("Keep 海拔图表压缩数据损坏。") from None
    if not isinstance(samples, list):
        raise KeepAltitudeError("Keep 海拔图表采样格式无效。")

    points = []
    for sample in samples:
        if not isinstance(sample, dict):
            raise KeepAltitudeError("Keep 海拔图表采样格式无效。")
        seconds = sample.get("x")
        altitude = sample.get("y")
        if not _finite_number(seconds) or seconds < 0 or not _finite_number(altitude):
            raise KeepAltitudeError("Keep 海拔图表包含无效的时间或高度。")
        # bar 图的 y 是高度，min/max 是占位；不补起点、不变换基准、不生成坐标。
        points.append({"timestamp": seconds * 10, "altitude": altitude})
    return points


def _finite_number(value) -> bool:
    return type(value) in (int, float) and math.isfinite(value)
