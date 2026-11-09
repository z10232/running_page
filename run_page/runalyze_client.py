"""RUNALYZE 免费个人 API 的认证检查和活动文件上传。"""

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
                f"RUNALYZE 接口未确认接收（HTTP {response.status_code}）。"
            )
        return response

    def ping(self) -> None:
        self._request("GET", "/ping", 200)

    def upload_fit(self, filename: str, content: bytes) -> dict:
        self._request(
            "POST",
            "/activities/uploads",
            201,
            files={"file": (filename, content, "application/octet-stream")},
        )
        # 免费 API 仅返回队列接收结果，清单不把它标为最终入库成功。
        return {"sha256": hashlib.sha256(content).hexdigest(), "http_status": 201}
