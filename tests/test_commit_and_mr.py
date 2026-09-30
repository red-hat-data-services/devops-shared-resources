from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

from lib.tekton_gitlab import parse_project_path

import importlib.machinery
import importlib.util


def _load_commit_and_mr():
    path = str(REPO_ROOT / "scripts" / "commit-and-mr")
    loader = importlib.machinery.SourceFileLoader("commit_and_mr", path)
    spec = importlib.util.spec_from_loader("commit_and_mr", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


_cm = _load_commit_and_mr()
build_mr_description = _cm.build_mr_description
build_commit_message = _cm.build_commit_message


# --- parse_project_path ---


def test_parse_project_path_https():
    assert parse_project_path("https://gitlab.cee.redhat.com/rhoai/private/repo.git") == "rhoai/private/repo"


def test_parse_project_path_https_no_dotgit():
    assert parse_project_path("https://gitlab.cee.redhat.com/rhoai/private/repo") == "rhoai/private/repo"


def test_parse_project_path_ssh():
    assert parse_project_path("git@gitlab.cee.redhat.com:rhoai/private/repo.git") == "rhoai/private/repo"


def test_parse_project_path_ssh_no_dotgit():
    assert parse_project_path("git@gitlab.cee.redhat.com:rhoai/private/repo") == "rhoai/private/repo"


def test_parse_project_path_ssh_scheme():
    assert parse_project_path("ssh://git@gitlab.cee.redhat.com:22/rhoai/private/repo.git") == "rhoai/private/repo"


def test_parse_project_path_ssh_scheme_no_port():
    assert parse_project_path("ssh://git@gitlab.cee.redhat.com/rhoai/private/repo.git") == "rhoai/private/repo"


def test_parse_project_path_unrecognized():
    assert parse_project_path("not-a-url") is None


# --- build_commit_message ---


def test_build_commit_message_includes_details():
    changes = [
        {"file": "push.yaml", "details": ["bad namespace", "wrong image"]},
    ]
    result = build_commit_message("Fix tekton on rhoai-3.5", changes, False)
    lines = result.split("\n")
    assert lines[0] == "Fix tekton on rhoai-3.5"
    assert lines[1] == ""
    assert "push.yaml:" in result
    assert "  - bad namespace" in result
    assert "  - wrong image" in result


def test_build_commit_message_skip_builds():
    changes = [{"file": "a.yaml", "details": ["err"]}]
    result = build_commit_message("Fix issues", changes, True)
    assert result.startswith("Fix issues [skip tkn]")


def test_build_commit_message_no_details():
    changes = [{"file": "push.yaml", "details": []}]
    result = build_commit_message("Fix issues", changes, False)
    assert "push.yaml: fixed" in result


def test_build_commit_message_multiple_files():
    changes = [
        {"file": "push.yaml", "details": ["err1"]},
        {"file": "pr.yaml", "details": ["err2"]},
    ]
    result = build_commit_message("Fix stuff", changes, False)
    assert "push.yaml:" in result
    assert "pr.yaml:" in result
    assert "  - err1" in result
    assert "  - err2" in result


# --- build_mr_description ---


def test_build_mr_description_basic():
    changes = [
        {"file": "push.yaml", "details": ["bad namespace", "wrong image"]},
    ]
    result = build_mr_description(changes)
    assert "## Changes" in result
    assert "### `push.yaml`" in result
    assert "- bad namespace" in result
    assert "- wrong image" in result


def test_build_mr_description_multiple_files():
    changes = [
        {"file": "push.yaml", "details": ["err1"]},
        {"file": "pr.yaml", "details": ["err2"]},
    ]
    result = build_mr_description(changes)
    assert "### `push.yaml`" in result
    assert "### `pr.yaml`" in result


def test_build_mr_description_no_details():
    changes = [{"file": "push.yaml", "details": []}]
    result = build_mr_description(changes)
    assert "### `push.yaml`" in result
