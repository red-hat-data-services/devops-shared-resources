"""Protocol failure tests plus integration with the real, pinned Go libraries."""
from __future__ import annotations

import subprocess
from dataclasses import replace
from unittest.mock import patch

import pytest

from lib.pac_cel import TriggerParseError, evaluate_expressions
from lib.gap_check_selection import (
    PullRequestContext, cel_matches, fetch_file_collections, pipeline_is_expected,
    select_build_checks,
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


# Trigger expressions captured from GAP Leader #64's child PRs. Keep these
# self-contained: regression tests must not depend on GitHub or /tmp evidence.
_RELEASE_PUSH = (
    'event == "push" && '
    'target_branch.matches("^rhoai-[A-Za-z0-9_][A-Za-z0-9_.-]{0,40}$") && '
    '!(target_branch in ["rhoai-3.5", "rhoai-3.6-ea.1", "rhoai-3.6-ea.2"])'
)
_DISABLED_DASHBOARD_PUSH = """false && event == "push"
&& target_branch == "main"
&& ( "dashboard-operator/test/e2e/**".pathChanged()
      || "dashboard-operator/api/**".pathChanged()
      || "dashboard-operator/go.mod".pathChanged()
      || "dashboard-operator/go.sum".pathChanged()
      || "dashboard-operator/Dockerfile.e2e".pathChanged()
      || "dashboard-operator/Dockerfile.e2e.dockerignore".pathChanged()
      || ".tekton/odh-dashboard-operator-e2e-push.yaml".pathChanged() )
"""


@pytest.mark.parametrize("base_ref", ["stable", "rhoai-3.7", "v1.2.3"])
@pytest.mark.parametrize("name, expression, expected", [
    pytest.param(
        "odh-llm-d-kv-pvc-evictor-on-tag",
        'event == "push" && target_branch.startsWith("v")', False,
        id="kv-cache-tag",
    ),
    pytest.param(
        "odh-mcp-lifecycle-module-operator-on-release-push",
        _RELEASE_PUSH, False, id="mcp-release-push",
    ),
    pytest.param(
        "odh-mcp-lifecycle-module-operator-e2e-on-release-push",
        _RELEASE_PUSH, False, id="mcp-e2e-release-push",
    ),
    pytest.param(
        "odh-dashboard-operator-e2e-on-push",
        _DISABLED_DASHBOARD_PUSH, False, id="disabled-dashboard-push",
    ),
    pytest.param(
        "mixed-on-push",
        '(event == "push" && target_branch.startsWith("v")) || event == "pull_request"',
        True, id="mixed-push-pr",
    ),
    pytest.param(
        "mixed-on-tag",
        'event == "pull_request" || (event == "push" && target_branch.startsWith("v"))',
        True, id="mixed-pr-push",
    ),
])
def test_real_push_tag_triggers_during_pr_evaluation(
    pac_cel_binary, caplog, base_ref, name, expression, expected,
):
    import yaml

    pr = replace(context(), base_ref=base_ref)
    pipeline = {
        "kind": "PipelineRun",
        "metadata": {
            "name": name,
            "labels": {"appstudio.openshift.io/component": "test-component"},
            "annotations": {"pipelinesascode.tekton.dev/on-cel-expression": expression},
        },
    }
    assert cel_matches(expression, pr) is expected
    assert pipeline_is_expected(pipeline, pr) is expected
    checks = select_build_checks([yaml.safe_dump(pipeline)], pr, app_slug="konflux")
    assert [check.name_suffix for check in checks] == ([name] if expected else [])
    # A false result must come from CEL, not a conservative parse-error fallback.
    assert "retaining expected check" not in caplog.text


def test_unavailable_webhook_facts_retain_pr_build(pac_cel_binary, caplog):
    expression = 'body.repository.full_name == "some/repo"'
    with pytest.raises(TriggerParseError):
        cel_matches(expression, context())
    pipeline = {
        "kind": "PipelineRun",
        "metadata": {
            "name": "unknown-webhook-pr",
            "annotations": {"pipelinesascode.tekton.dev/on-cel-expression": expression},
        },
    }
    assert pipeline_is_expected(pipeline, context()) is True
    assert "retaining expected check" in caplog.text


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
