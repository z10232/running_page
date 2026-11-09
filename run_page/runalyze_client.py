"""RUNALYZE 免费个人 API 的文件提交与导入确认。"""

import hashlib

import httpx

API_URL = "https://runalyze.com/api/v1"


class RunalyzeError(Exception):
    """可以安全写入公开任务日志的 API 错误。"""


class RunalyzeClient:
    def __init__(self, session: httpx.Client, token: str):
        self.session = session
        self.token = token

    def _request(self, method: str, path: str, expected_status: int, **kwargs):
        try:
            response = self.session.request(
                method,
                API_URL + path,
                headers={"token": self.token},
                **kwargs,
            )
        except httpx.HTTPError:
            raise RunalyzeError("RUNALYZE 连接未完成，请稍后重新运行。") from None
        if response.status_code in (401, 403):
            raise RunalyzeError(
                "RUNALYZE Token 无效、已过期或缺少 Activity → Write (File) 权限。"
            )
        if response.status_code == 429:
            raise RunalyzeError("RUNALYZE 已限制请求频率，请稍后重新运行。")
        if response.status_code != expected_status:
            # 响应可能含账号或活动详情，公开日志只保留 HTTP 状态。
            raise RunalyzeError(
                f"RUNALYZE 接口请求未成功（HTTP {response.status_code}）。"
            )
        return response

    def ping(self) -> None:
        self._request("GET", "/ping", 200)

    def upload_fit(self, filename: str, content: bytes) -> dict:
        response = self._request(
            "POST",
            "/activities/uploads",
            201,
            files={"file": (filename, content, "application/octet-stream")},
        )
        payload = self._response_data(response)
        queue_id = payload.get("queue_id")
        if type(queue_id) is not int or queue_id <= 0:
            raise RunalyzeError("RUNALYZE 没有返回有效队列编号，请核对导入队列后重试。")
        return {
            "sha256": hashlib.sha256(content).hexdigest(),
            "http_status": 201,
            "queue_id": queue_id,
        }

    def get_import_status(self, queue_id: int) -> dict:
        response = self._request("GET", f"/activities/uploads/{queue_id}", 200)
        payload = self._response_data(response)
        if payload.get("queue_id") != queue_id:
            raise RunalyzeError("RUNALYZE 导入回执的队列编号不匹配。")
        # 免费 API 支持查询上传队列；只保留确认入库需要的字段。
        return {
            "queue_id": queue_id,
            "activity_id": payload.get("activity_id"),
            "status": payload.get("status"),
            "state": payload.get("state"),
        }

    @staticmethod
    def _response_data(response: httpx.Response) -> dict:
        try:
            payload = response.json()
        except ValueError:
            raise RunalyzeError("RUNALYZE 返回了无法解析的导入回执。") from None
        if not isinstance(payload, dict):
            raise RunalyzeError("RUNALYZE 返回了无法识别的导入回执。")
        return payload
