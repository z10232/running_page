"""把固定历史基线之后的 Keep 跑步独立同步到高驰中国平台。"""

import argparse
import json
import os
import time
from pathlib import Path

import httpx
import requests
from coros_client import CorosClient, CorosError
from keep_fit import build_keep_fit
from keep_sync import get_single_run_data, get_to_download_runs_ids, login

IMPORT_WAIT_SECONDS = 120
POLL_INTERVAL_SECONDS = 5


class SyncError(Exception):
    """可以安全写入公开任务日志的同步错误。"""


class ImportFailed(SyncError):
    """高驰已明确拒绝本次文件，可在下次同步时重试。"""


def run_sync(
    keep_session: requests.Session,
    keep_headers: dict,
    coros: CorosClient,
    manifest_path: Path,
    sample_id: str | None = None,
) -> int:
    """逐条保存已确认的高驰回执，返回本轮未完成的活动数。"""
    manifest = _load_manifest(manifest_path)
    runs = get_to_download_runs_ids(keep_session, keep_headers, "running")
    candidates = _new_runs(runs, manifest, sample_id)
    failures = 0
    print(f"本轮待上传高驰的 Keep 跑步：{len(candidates)} 条。")
    for index, (keep_id, run_id) in enumerate(candidates, start=1):
        try:
            response = get_single_run_data(
                keep_session, keep_headers, run_id, "running"
            )
            if not response or not response.get("data"):
                raise SyncError("没有取得 Keep 跑步详情。")
            _upload_activity(keep_id, response["data"], coros, manifest, manifest_path)
        except (SyncError, CorosError) as error:
            failures += 1
            print(f"第 {index} 条活动尚未完成高驰同步：{error}")
        except Exception:  # noqa: BLE001
            # 每条活动是独立的成功边界；保留其他成功回执，原始异常不进入公开日志。
            failures += 1
            print(f"第 {index} 条活动尚未完成高驰同步，将在下次任务中继续处理。")
    print(f"高驰同步完成：成功 {len(candidates) - failures} 条，未完成 {failures} 条。")
    return failures


def _load_manifest(path: Path) -> dict:
    try:
        manifest = json.loads(path.read_text())
        valid = (
            isinstance(manifest["baseline_ids"], list)
            and all(isinstance(value, str) for value in manifest["baseline_ids"])
            and isinstance(manifest["uploaded"], dict)
            and isinstance(manifest["pending"], dict)
        )
    except (OSError, ValueError, KeyError, TypeError):
        valid = False
    if not valid:
        # 固定基线由部署时建立；缺失时停止，避免意外把全部历史记录发送到高驰。
        raise SyncError("高驰上传基线清单缺失或无效，请恢复后再运行。")
    return manifest


def _save_manifest(path: Path, manifest: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _new_runs(runs: list[str], manifest: dict, sample_id: str | None):
    all_runs = {run_id.split("_")[1]: run_id for run_id in runs}
    uploaded = manifest["uploaded"]
    if sample_id is not None:
        if sample_id not in all_runs:
            raise SyncError("指定的验证样例不在 Keep 跑步列表中。")
        return [] if sample_id in uploaded else [(sample_id, all_runs[sample_id])]
    excluded = set(manifest["baseline_ids"]) | uploaded.keys()
    return [
        (keep_id, run_id)
        for keep_id, run_id in all_runs.items()
        if keep_id not in excluded
    ]


def _upload_activity(
    keep_id: str,
    activity: dict,
    coros: CorosClient,
    manifest: dict,
    manifest_path: Path,
) -> None:
    existing = coros.find_activity(activity)
    if existing:
        _record_success(keep_id, {"activity_id": existing}, manifest, manifest_path)
        return

    filename = f"keep-{keep_id}.fit"
    task_id = manifest["pending"].get(keep_id)
    if task_id is None:
        # 远端任务可恢复提交回执丢失的情况；已失败任务让下一次上传重新建立任务。
        task_id = _recover_task(coros.import_tasks(), filename)
        if task_id is None:
            task_id = coros.submit_fit(filename, build_keep_fit(activity))
        manifest["pending"][keep_id] = task_id
        _save_manifest(manifest_path, manifest)

    try:
        _wait_for_import(coros, task_id)
    except ImportFailed:
        del manifest["pending"][keep_id]
        _save_manifest(manifest_path, manifest)
        raise
    _record_success(keep_id, {"task_id": task_id}, manifest, manifest_path)


def _recover_task(tasks: list[dict], filename: str) -> str | None:
    for task in tasks:
        if task.get("originalFilename") != filename:
            continue
        status = task.get("status")
        if status in (0, 1, 3) or (status == 2 and not task.get("errorSize")):
            return str(task["id"])
    return None


def _wait_for_import(coros: CorosClient, task_id: str) -> None:
    checks = IMPORT_WAIT_SECONDS // POLL_INTERVAL_SECONDS + 1
    for attempt in range(checks):
        task = next(
            (task for task in coros.import_tasks() if str(task["id"]) == task_id),
            None,
        )
        if task is not None:
            status = task.get("status")
            if status == 2 and not task.get("errorSize"):
                return
            if status == 2 or (isinstance(status, int) and status < 0):
                raise ImportFailed("高驰导入任务失败，下次同步将重新上传。")
        if attempt + 1 < checks:
            time.sleep(POLL_INTERVAL_SECONDS)
    # 查不到任务也保留编号：先等待目标端恢复，避免一次响应丢失导致重复活动。
    raise SyncError("高驰尚未确认导入完成，已保留任务供下次同步继续核对。")


def _record_success(keep_id: str, receipt: dict, manifest: dict, path: Path) -> None:
    manifest["uploaded"][keep_id] = receipt
    manifest["pending"].pop(keep_id, None)
    _save_manifest(path, manifest)


def _numeric_keep_id(value: str) -> str:
    if not value.isascii() or not value.isdecimal():
        raise argparse.ArgumentTypeError("验证样例必须是单个数字 Keep ID。")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description="同步新增 Keep 跑步到高驰中国平台")
    parser.add_argument(
        "--manifest", type=Path, default=Path("activities/keep2coros.json")
    )
    parser.add_argument(
        "--sample-id", type=_numeric_keep_id, help="仅处理已获授权的单条 Keep 验证样例"
    )
    options = parser.parse_args()
    required = ("KEEP_MOBILE", "KEEP_PASSWORD", "COROS_ACCOUNT", "COROS_PASSWORD")
    if any(not os.environ.get(name) for name in required):
        print("请先配置 Keep 和高驰账号、密码对应的环境变量。")
        return 1
    try:
        with (
            requests.Session() as keep_session,
            httpx.Client(timeout=60) as coros_session,
        ):
            result = login(
                keep_session, os.environ["KEEP_MOBILE"], os.environ["KEEP_PASSWORD"]
            )
            if not result:
                raise SyncError("Keep 登录未成功。")
            keep_session, headers = result
            coros = CorosClient(coros_session)
            coros.login(os.environ["COROS_ACCOUNT"], os.environ["COROS_PASSWORD"])
            failures = run_sync(
                keep_session, headers, coros, options.manifest, options.sample_id
            )
            return 1 if failures else 0
    except (SyncError, CorosError) as error:
        print(str(error))
        return 1
    except Exception:  # noqa: BLE001
        print("Keep 到高驰同步未完成，请检查账号配置或稍后重新运行。")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
