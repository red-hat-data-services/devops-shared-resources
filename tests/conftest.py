from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def configure_git_identity(repo_path: Path) -> None:
    """Set local git author identity (CI runners often have none configured)."""
    run(["git", "config", "user.name", "test"], cwd=repo_path)
    run(["git", "config", "user.email", "test@example.com"], cwd=repo_path)


def set_bare_repo_default_branch(bare_repo: Path, branch: str) -> None:
    """Point a bare remote's HEAD at a branch so clones check out the expected ref."""
    run(
        ["git", "--git-dir", str(bare_repo), "symbolic-ref", "HEAD", f"refs/heads/{branch}"],
        cwd=bare_repo.parent,
    )


def run(cmd: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str]:
    if cmd[:2] == ["git", "commit"]:
        cmd = [*cmd[:2], "--no-verify", *cmd[2:]]
    result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"command failed: {' '.join(cmd)}\nstdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


@pytest.fixture(scope="session")
def pac_cel_binary(tmp_path_factory):
    """Build the actual Go helper; never substitute a mocked CEL engine."""
    import os
    import shutil

    if not shutil.which("go"):
        pytest.skip("Go is required for PaC CEL integration tests")
    root = Path(__file__).resolve().parents[1]
    binary = tmp_path_factory.mktemp("pac-cel") / "pac-cel-evaluate"
    subprocess.run(
        ["go", "build", "-o", str(binary), "."],
        cwd=root / "tools" / "pac-cel", check=True,
        env={**os.environ, "GOTOOLCHAIN": "local"},
    )
    previous = os.environ.get("PAC_CEL_EVALUATOR")
    os.environ["PAC_CEL_EVALUATOR"] = str(binary)
    try:
        yield binary
    finally:
        if previous is None:
            os.environ.pop("PAC_CEL_EVALUATOR", None)
        else:
            os.environ["PAC_CEL_EVALUATOR"] = previous


@pytest.fixture
def git_repo_factory(tmp_path: Path):
    def _create_repo(name: str, *, default_branch: str = "main") -> Path:
        repo_path = tmp_path / name
        repo_path.mkdir()
        run(["git", "init", "-b", default_branch], cwd=repo_path)
        configure_git_identity(repo_path)
        return repo_path

    return _create_repo


# Legacy notification regression fixtures explicitly select their original destination.
@pytest.fixture(autouse=True)
def notification_test_channel(monkeypatch):
    monkeypatch.setenv("GAP_SLACK_CHANNEL", "C0883L8FARE")
