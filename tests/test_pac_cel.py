"""Protocol failure tests plus integration with the real, pinned Go libraries."""
from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from lib.pac_cel import TriggerParseError, evaluate_expressions
from lib.gap_check_selection import (
    PullRequestContext, cel_matches, fetch_file_collections, select_build_checks,
)


def context(**kwargs):
    return PullRequestContext(
        number=20, base_ref="stable", head_sha="a" * 40,
        labels=frozenset(), changed_files=("src/nested/main.go",), **kwargs,
    )


@pytest.mark.parametrize("failure", [
    FileNotFoundError(), subprocess.TimeoutExpired("helper", 15),
])
def test_helper_unavailable(failure):
    with patch("lib.pac_cel.subprocess.run", side_effect=failure):
        assert isinstance(evaluate_expressions(["true"], {})[0], TriggerParseError)


@pytest.mark.parametrize("output", [
    "not json", "{}", "[]", '[{"value": 1}]', '[{"value": "false"}]', "[null]",
])
def test_invalid_responses(output):
    with patch("lib.pac_cel.subprocess.run", return_value=subprocess.CompletedProcess([], 0, output, "")):
        assert isinstance(evaluate_expressions(["true"], {})[0], TriggerParseError)


def test_helper_failure_retains_expected_check(caplog):
    document = """
kind: PipelineRun
metadata:
  name: component-pr
  annotations:
    pipelinesascode.tekton.dev/on-cel-expression: 'false'
"""
    with patch("lib.pac_cel.subprocess.run", side_effect=FileNotFoundError()):
        assert len(select_build_checks([document], context(), app_slug="konflux")) == 1
    assert "retaining expected check" in caplog.text


def test_real_evaluation_and_missing_facts(pac_cel_binary):
    assert cel_matches('source_branch.startsWith("feature/") && "src/*.go".pathChanged()', context(source_branch="feature/x"))
    with pytest.raises(TriggerParseError):
        cel_matches('has(body.action)', context())
    for expression in ('files.added.size() > 0', 'has(files.added)', 'has(files["added"])'):
        with pytest.raises(TriggerParseError):
            cel_matches(expression, context())
    with pytest.raises(TriggerParseError):
        cel_matches('"not boolean"', context())


def test_batch_once(pac_cel_binary):
    documents = [
        f"""kind: PipelineRun
metadata:
  name: component-{index}
  annotations:
    pipelinesascode.tekton.dev/on-cel-expression: '{expression}'
""" for index, expression in enumerate(["true", "false", "true"])
    ]
    original = subprocess.run
    with patch("lib.pac_cel.subprocess.run", wraps=original) as run:
        checks = select_build_checks(documents, context(), app_slug="konflux")
    assert len(checks) == 2
    assert run.call_count == 1


def test_file_statuses_and_rename():
    from json import dumps
    files = [
        {"filename": "new.txt", "previous_filename": "old.txt", "status": "renamed"},
        {"filename": "deleted.txt", "status": "removed"},
        {"filename": "added.txt", "status": "added"},
        {"filename": "modified.txt", "status": "modified"},
    ]
    collections = fetch_file_collections("owner", "repo", 20, lambda _: dumps(files))
    assert collections == {
        "all": ("new.txt", "deleted.txt", "added.txt", "modified.txt"),
        "renamed": ("new.txt",), "deleted": ("deleted.txt",),
        "added": ("added.txt",), "modified": ("modified.txt",),
    }


def test_file_limit_is_not_a_no_build_signal():
    from json import dumps
    from lib.github_check_runs import CheckQueryError
    files = [{"filename": str(i), "status": "added"} for i in range(3000)]
    with patch("lib.gap_check_selection.collect_github_pages", return_value=files):
        with pytest.raises(CheckQueryError, match="truncated"):
            fetch_file_collections("o", "r", 1, lambda _: "")
