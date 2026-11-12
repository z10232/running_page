"""提交 Keep 跑步并通过 RUNALYZE 文件导入队列确认入库。"""

import argparse
import json
import os
import time
from pathlib import Path

import httpx
import requests
from keep_altitude import get_keep_altitude_points
from keep_fit import build_keep_fit
from keep_sync import get_single_run_data, get_to_download_runs_ids, login
from runalyze_client import RunalyzeClient, RunalyzeError

IMPORT_CHECKS = 6
POLL_INTERVAL_SECONDS = 5
REQUEST_INTERVAL_SECONDS = 1


class SyncError(Exception):
    """可以安全写入公开任务日志的同步错误。"""


def run_sync(
    keep_session: requests.Session,
    keep_headers: dict,
    runalyze: RunalyzeClient,
    manifest_path: Path,
    sample_id: str | None = None,
) -> int:
    """先恢复已有队列，再提交新活动，返回未确认入库的活动数。"""
    manifest = _load_manifest(manifest_path)
    runs = get_to_download_runs_ids(keep_session, keep_headers, "running")
    candidates = _new_runs(runs, manifest["accepted"], sample_id)
    candidates.sort(key=lambda run: run[0] not in manifest["pending"])
    confirmed = 0
    failures = 0
    waiting = {}
    print(f"本轮待同步 RUNALYZE 的 Keep 跑步：{len(candidates)} 条。")
    for index, (keep_id, run_id) in enumerate(candidates, start=1):
        if keep_id not in manifest["pending"]:
            try:
                content = _prepare_fit(keep_session, keep_headers, run_id)
            except Exception:  # noqa: BLE001
                # 单条详情或转换失败不影响其他活动，原始数据和异常不进入公开日志。
                failures += 1
                print(f"第 {index} 条活动未能读取或转换，将在下次同步时重试。")
                continue
            _submit_import(keep_id, content, runalyze, manifest, manifest_path)
            print(f"第 {index} 条活动已接收排队。")

        if _confirm_import(keep_id, index, runalyze, manifest, manifest_path):
            confirmed += 1
        else:
            waiting[keep_id] = index

    confirmed += _poll_pending(waiting, runalyze, manifest, manifest_path)
    incomplete = failures + len(waiting)
    print(
        f"RUNALYZE 已确认入库 {confirmed} 条，仍在队列 {len(waiting)} 条，未提交 {failures} 条。"
    )
    return incomplete


def _submit_import(
    keep_id: str,
    content: bytes,
    runalyze: RunalyzeClient,
    manifest: dict,
    manifest_path: Path,
) -> None:
    # API 错误和回执保存失败立即结束本轮；已接收的队列先落盘再做后续请求。
    receipt = runalyze.upload_fit(f"keep-{keep_id}.fit", content)
    manifest["pending"][keep_id] = receipt
    _save_manifest(manifest_path, manifest)
    time.sleep(REQUEST_INTERVAL_SECONDS)


def _confirm_import(
    keep_id: str,
    index: int,
    runalyze: RunalyzeClient,
    manifest: dict,
    manifest_path: Path,
) -> bool:
    receipt = manifest["pending"][keep_id]
    status = runalyze.get_import_status(receipt["queue_id"])
    activity_id = status.get("activity_id")
    confirmed = _positive_integer(activity_id)
    if confirmed:
        manifest["accepted"][keep_id] = {**receipt, "activity_id": activity_id}
        del manifest["pending"][keep_id]
        _save_manifest(manifest_path, manifest)
        print(f"第 {index} 条活动已确认入库。")
    time.sleep(REQUEST_INTERVAL_SECONDS)
    return confirmed


def _poll_pending(
    waiting: dict[str, int],
    runalyze: RunalyzeClient,
    manifest: dict,
    manifest_path: Path,
) -> int:
    confirmed = 0
    # 全批次共享等待轮次，避免 112 条历史活动逐条等待造成任务累计超时。
    for _ in range(IMPORT_CHECKS - 1):
        if not waiting:
            break
        time.sleep(POLL_INTERVAL_SECONDS)
        for keep_id, index in list(waiting.items()):
            if _confirm_import(keep_id, index, runalyze, manifest, manifest_path):
                confirmed += 1
                del waiting[keep_id]
    return confirmed


def _positive_integer(value) -> bool:
    return type(value) is int and value > 0


def _load_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text())
        manifest.setdefault("pending", {})
        valid = (
            isinstance(manifest["accepted"], dict)
            and isinstance(manifest["pending"], dict)
            and all(
                isinstance(receipt, dict)
                and _positive_integer(receipt.get("activity_id"))
                for receipt in manifest["accepted"].values()
            )
            and all(
                isinstance(receipt, dict) and _positive_integer(receipt.get("queue_id"))
                for receipt in manifest["pending"].values()
            )
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        valid = False
    if not valid:
        # 清单由部署时明确建立，缺失时停止，防止状态丢失后重新提交全部历史。
        raise SyncError("RUNALYZE 接收回执清单缺失或无效，请恢复后再运行。")
    return manifest


def _save_manifest(path: Path, manifest: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _new_runs(runs: list[str], accepted: dict, sample_id: str | None):
    all_runs = {run_id.split("_")[1]: run_id for run_id in runs}
    if sample_id is not None:
        if sample_id not in all_runs:
            raise SyncError("指定的验证样例不在 Keep 跑步列表中。")
        return [] if sample_id in accepted else [(sample_id, all_runs[sample_id])]
    return [
        (keep_id, run_id)
        for keep_id, run_id in all_runs.items()
        if keep_id not in accepted
    ]


def _prepare_fit(
    keep_session: requests.Session, keep_headers: dict, run_id: str
) -> bytes:
    response = get_single_run_data(keep_session, keep_headers, run_id, "running")
    if not response or not response.get("data"):
        raise SyncError("没有取得 Keep 跑步详情。")
    activity = response["data"]
    altitude_points = []
    if not activity.get("geoPoints"):
        altitude_points = get_keep_altitude_points(keep_session, keep_headers, run_id)
    return build_keep_fit(activity, altitude_points=altitude_points)


def _numeric_keep_id(value: str) -> str:
    if not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("验证样例必须是单个数字 Keep ID。")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(
        description="提交 Keep 跑步并确认 RUNALYZE 导入结果"
    )
    parser.add_argument(
        "--manifest", type=Path, default=Path("activities/keep2runalyze.json")
    )
    parser.add_argument(
        "--sample-id", type=_numeric_keep_id, help="仅提交指定的单条 Keep 验证样例"
    )
    options = parser.parse_args()
    required = ("KEEP_MOBILE", "KEEP_PASSWORD", "RUNALYZE_API_TOKEN")
    if any(not os.environ.get(name) for name in required):
        print("请先配置 Keep 手机号、密码和 RUNALYZE_API_TOKEN 环境变量。")
        return 1
    try:
        with (
            requests.Session() as keep_session,
            httpx.Client(timeout=60) as runalyze_session,
        ):
            runalyze = RunalyzeClient(
                runalyze_session, os.environ["RUNALYZE_API_TOKEN"]
            )
            runalyze.ping()
            result = login(
                keep_session, os.environ["KEEP_MOBILE"], os.environ["KEEP_PASSWORD"]
            )
            if not result:
                raise SyncError("Keep 登录未成功。")
            keep_session, headers = result
            failures = run_sync(
                keep_session, headers, runalyze, options.manifest, options.sample_id
            )
            return 1 if failures else 0
    except (SyncError, RunalyzeError) as error:
        print(str(error))
        return 1
    except Exception:  # noqa: BLE001
        print(
            "Keep 到 RUNALYZE 同步已停止，请检查配置或稍后重试；已保存的队列与入库回执会保留。"
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
