"""高驰中国训练平台的登录、文件导入和导入结果查询。"""

import base64
import hashlib
import json
from datetime import datetime
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile
from zoneinfo import ZoneInfo

import bcrypt
import httpx
import oss2

API_URL = "https://teamcnapi.coros.com"
TRAINING_URL = "https://trainingcn.coros.com"


class CorosError(Exception):
    """可以安全展示给任务日志的高驰操作错误。"""


class CorosClient:
    def __init__(self, session: httpx.Client):
        self.session = session
        self.user_id = ""

    def _request(self, method, path, **kwargs):
        response = self.session.request(method, API_URL + path, **kwargs)
        response.raise_for_status()
        payload = response.json()
        if payload.get("result") != "0000":
            # 服务端响应可能包含账号信息，只记录稳定错误码。
            raise CorosError(
                f"高驰接口失败（代码 {payload.get('result', 'unknown')}）。"
            )
        return payload.get("data")

    def login(self, account: str, password: str):
        salt = bcrypt.gensalt(rounds=10, prefix=b"2a")
        password_digest = hashlib.md5(password.encode()).hexdigest().encode()
        data = self._request(
            "POST",
            "/account/login",
            json={
                "account": account,
                "accountType": 2,
                "p1": bcrypt.hashpw(password_digest, salt).decode(),
                "p2": salt.decode(),
            },
        )
        if data.get("twoFactorRequired"):
            raise CorosError("高驰账号需要额外验证，请先在训练平台完成验证。")
        if not data.get("accessToken") or not data.get("userId"):
            raise CorosError("高驰登录没有返回有效会话。")
        if int(data.get("regionId", 2)) != 2:
            raise CorosError("此同步任务用于高驰中国区账号。")
        token = data["accessToken"]
        self.user_id = str(data["userId"])
        self.session.headers.update(
            {
                "accessToken": token,
                "Origin": TRAINING_URL,
                "Referer": TRAINING_URL + "/",
                "YFHeader": json.dumps({"userId": self.user_id, "language": "zh-CN"}),
            }
        )
        self.session.cookies.set("CPL-coros-token", token, domain=".coros.com")
        self.session.cookies.set("CPL-coros-region", "2", domain=".coros.com")

    def import_tasks(self):
        return self._request(
            "POST", "/activity/fit/getImportSportList", json={"size": 10}
        )

    def find_activity(self, activity: dict):
        """恢复本地清单未提交、但高驰已经接收活动的情况。"""
        start_time = int(activity["startTime"]) // 1000
        day = datetime.fromtimestamp(start_time, ZoneInfo("Asia/Shanghai")).strftime(
            "%Y%m%d"
        )
        page = 1
        while True:
            data = self._request(
                "GET",
                "/activity/query",
                params={
                    "startDay": day,
                    "endDay": day,
                    "modeList": "100,101,102,103",
                    "pageNumber": page,
                    "size": 100,
                },
            )
            rows = data.get("dataList", [])
            for row in rows:
                if (
                    row.get("startTime") == start_time
                    and row.get("workoutTime") is not None
                    and abs(float(row["workoutTime"]) - float(activity["duration"]))
                    <= 2
                ):
                    return str(row["labelId"])
            if page >= data.get("totalPage", 1) or not rows:
                return None
            page += 1

    def _storage_credentials(self):
        response = self.session.get(TRAINING_URL + "/api/proxy/oss/sts")
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 200:
            raise CorosError("无法获取高驰文件导入的临时上传凭证。")
        data = payload["data"]
        if data.get("service") != "aliyun":
            raise CorosError("高驰中国区返回了尚未支持的文件存储服务。")
        encoded = data["credentials"].replace("9y78gpoERW4lBNYL", "")
        return json.loads(base64.b64decode(encoded))

    def submit_fit(self, filename: str, content: bytes):
        digest = hashlib.md5(content).hexdigest()
        archive_bytes = BytesIO()
        with ZipFile(archive_bytes, "w", ZIP_DEFLATED) as archive:
            archive.writestr(f"{digest}/{filename}", content)
        credentials = self._storage_credentials()
        auth = oss2.StsAuth(
            credentials["AccessKeyId"],
            credentials["AccessKeySecret"],
            credentials["SecurityToken"],
        )
        bucket = oss2.Bucket(
            auth,
            f"https://{credentials['Region']}.aliyuncs.com",
            credentials["Bucket"],
            connect_timeout=60,
        )
        object_key = f"fit_zip/{self.user_id}/{digest}.zip"
        try:
            bucket.put_object(object_key, archive_bytes.getvalue())
        except oss2.exceptions.OssError:
            # SDK 错误可能携带临时鉴权信息，不把原始错误写入公开日志。
            raise CorosError("活动文件上传到高驰临时存储失败。") from None
        metadata = {
            "source": 1,
            "timezone": 32,
            "bucket": credentials["Bucket"],
            "md5": digest,
            "size": len(content),
            "object": object_key,
            "serviceName": "aliyun",
            "oriFileName": filename,
        }
        data = self._request(
            "POST",
            "/activity/fit/import",
            files={
                "jsonParameter": (None, json.dumps(metadata)),
            },
        )
        if not data or not data.get("id"):
            raise CorosError("高驰没有返回活动导入任务编号。")
        return str(data["id"])
