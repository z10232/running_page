"""用真实 Git 仓库验证同步任务写回数据及保留高驰回执的行为。"""

import json
import subprocess
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).parent / ".github/workflows/run_data_sync.yml"


def git(path, *args):
    result = subprocess.run(
        ["git", "-C", str(path), *args], capture_output=True, text=True, check=True
    )
    return result.stdout.strip()


def write_json(repo, path, data):
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(data) + "\n")


def commit(repo, message):
    git(repo, "add", ".")
    git(repo, "commit", "-m", message)


def configure_author(repo):
    git(repo, "config", "user.name", "Sync Test")
    git(repo, "config", "user.email", "sync-test@example.invalid")


@pytest.fixture
def repositories(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=master", str(remote)],
        check=True,
        capture_output=True,
    )
    author = tmp_path / "author"
    subprocess.run(
        ["git", "clone", str(remote), str(author)], check=True, capture_output=True
    )
    configure_author(author)
    write_json(author, "src/static/activities.json", ["existing-run"])
    write_json(author, "activities/keep2coros.json", {"uploaded": ["existing-run"]})
    commit(author, "初始跑步数据")
    git(author, "push", "origin", "master")
    worker = tmp_path / "worker"
    subprocess.run(
        ["git", "clone", str(remote), str(worker)], check=True, capture_output=True
    )
    configure_author(worker)
    return remote, author, worker


def publish_runs(worker):
    workflow = yaml.safe_load(WORKFLOW.read_text())
    step = next(
        s for s in workflow["jobs"]["sync"]["steps"] if s.get("name") == "Push new runs"
    )
    return subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", step["run"]],
        cwd=worker,
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("detached", [False, True])
def test_publish_keeps_remote_receipts_when_remote_advances(repositories, detached):
    remote, author, worker = repositories
    if detached:
        git(worker, "checkout", "--detach", "HEAD")
    # 任务采集期间，远端已有后续高驰回执；同步只写自己的跑步数据。
    write_json(
        author,
        "activities/keep2coros.json",
        {"uploaded": ["existing-run", "later-receipt"]},
    )
    commit(author, "保存高驰成功回执")
    git(author, "push", "origin", "master")
    write_json(worker, "src/static/activities.json", ["existing-run", "new-run"])

    result = publish_runs(worker)

    assert result.returncode == 0, result.stderr
    assert json.loads(git(remote, "show", "master:src/static/activities.json")) == [
        "existing-run",
        "new-run",
    ]
    assert json.loads(git(remote, "show", "master:activities/keep2coros.json")) == {
        "uploaded": ["existing-run", "later-receipt"]
    }
    assert git(worker, "status", "--porcelain") == ""


def test_publish_without_changes_keeps_existing_commit(repositories):
    remote, _, worker = repositories
    original = git(remote, "rev-parse", "master")
    result = publish_runs(worker)
    assert result.returncode == 0, result.stderr
    assert git(remote, "rev-parse", "master") == original


def test_publish_rejection_remains_a_failure(repositories):
    remote, _, worker = repositories
    hook = remote / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    write_json(worker, "src/static/activities.json", ["existing-run", "new-run"])
    result = publish_runs(worker)
    assert result.returncode != 0
    assert json.loads(git(remote, "show", "master:src/static/activities.json")) == [
        "existing-run"
    ]
