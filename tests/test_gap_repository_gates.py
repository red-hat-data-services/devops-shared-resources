from __future__ import annotations

import base64
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from lib.gap_check_selection import (
    FEASIBILITY_SETUP_JOB,
    FeasibilityRun,
    PullRequestContext,
    WorkflowJob,
    fetch_tekton_documents,
    resolved_pipeline_name,
    select_build_checks,
    select_feasibility_checks,
)
from lib.gap_pr_gates import decide_repository_pr, RepositoryPullRequest
from lib.github_check_runs import CheckRun, evaluate_checks, revision_deadline
from lib.github_cli import GhCommandError
from lib.pr_status_updater import PullRequestRef, StatusUpdateResult
from scripts.gap_pr_monitor import run_monitor

SHA = "a" * 40
OTHER_SHA = "b" * 40
COMMITTER = datetime(2026, 10, 6, 10, 0, tzinfo=timezone.utc)
NOW = COMMITTER + timedelta(minutes=30)
URL = "https://github.com/red-hat-data-services/eval-hub/pull/20"
APP = {"id": 906404, "slug": "konflux-internal-p02"}


EVAL_HUB_PIPELINE = """\
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  name: odh-eval-hub-on-pull-request-65317
  labels:
    appstudio.openshift.io/component: odh-eval-hub-v3-6
  annotations:
    pipelinesascode.tekton.dev/on-cel-expression: |
      event == "pull_request"
      && (files.all.exists(p, !p.matches('^\\\\.tekton/')) || ".tekton/odh-eval-hub-pull-request.yaml".pathChanged())
spec:
  params:
  - name: build-platforms
    value:
    - linux/x86_64
    - linux/arm64
    - linux/ppc64le
    - linux/s390x
"""

SECOND_PIPELINE = """\
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  name: odh-core-bff-on-pull-request-{{pull_request_number}}
  labels:
    appstudio.openshift.io/component: odh-core-bff
  annotations:
    pipelinesascode.tekton.dev/on-event: "[pull_request]"
    pipelinesascode.tekton.dev/on-target-branch: "[{{target_branch}}]"
"""

DOCS_SCOPED_PIPELINE = """\
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  name: trainer-on-pull-request
  labels:
    appstudio.openshift.io/component: trainer-v3-6
  annotations:
    pipelinesascode.tekton.dev/on-cel-expression: |
      event == "pull_request" && "src/**".pathChanged()
"""

LABEL_PIPELINE = """\
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  name: manual-on-pull-request
  labels:
    appstudio.openshift.io/component: manual-component
  annotations:
    pipelinesascode.tekton.dev/on-event: "[pull_request]"
    pipelinesascode.tekton.dev/on-target-branch: "[stable]"
    pipelinesascode.tekton.dev/on-label: "[kfbuild-all]"
    pipelinesascode.tekton.dev/on-comment: "^/build-konflux"
"""


def _context(**overrides: Any) -> PullRequestContext:
    base = dict(
        number=20,
        base_ref="stable",
        head_sha=SHA,
        labels=frozenset(),
        changed_files=("README.md",),
    )
    base.update(overrides)
    return PullRequestContext(**base)


def test_component_label_is_not_used_as_the_check_name() -> None:
    checks = select_build_checks([EVAL_HUB_PIPELINE], _context(), app_slug="konflux-internal-p02")
    assert len(checks) == 1
    assert checks[0].key == "odh-eval-hub-v3-6"
    assert checks[0].name_suffix == "odh-eval-hub-on-pull-request-65317"
    assert checks[0].name_suffix != "odh-core-bff-on-pull-request-2646"


REVISION_FALLBACK_PIPELINE = """\
apiVersion: tekton.dev/v1
kind: PipelineRun
metadata:
  name: odh-praxis-extproc-on-pull-request-{{pull_request_number}}
  labels:
    appstudio.openshift.io/component: pull-request-pipelines-odh-praxis-extproc
  annotations:
    pipelinesascode.tekton.dev/on-event: "[pull_request]"
    pipelinesascode.tekton.dev/on-target-branch: "[{{target_branch}}]"
spec:
  pipelineRef:
    params:
    - name: revision
      value: '{{ cel: pac.target_branch.matches("^rhoai-\\d+\\.\\d+(-ea\\.\\d+)?$") ? pac.target_branch : "main" }}'
    resolver: git
"""


def test_revision_fallback_cel_does_not_change_the_expected_build() -> None:
    checks = select_build_checks(
        [REVISION_FALLBACK_PIPELINE],
        _context(number=49, base_ref="stable"),
        app_slug="konflux-internal-p02",
    )
    assert len(checks) == 1
    assert checks[0].name_suffix == "odh-praxis-extproc-on-pull-request-49"
    assert checks[0].key == "pull-request-pipelines-odh-praxis-extproc"


def test_pull_request_number_template_and_stable_names() -> None:
    assert resolved_pipeline_name("odh-core-bff-on-pull-request-{{pull_request_number}}", 2646) == (
        "odh-core-bff-on-pull-request-2646"
    )
    assert resolved_pipeline_name("odh-eval-hub-v3-6-ea-1-on-pull-request", 275) == (
        "odh-eval-hub-v3-6-ea-1-on-pull-request"
    )


def test_multi_arch_pipelinerun_is_one_check() -> None:
    checks = select_build_checks([EVAL_HUB_PIPELINE], _context(), app_slug="konflux-internal-p02")
    assert len(checks) == 1


def test_docs_only_change_skips_path_scoped_pipeline() -> None:
    checks = select_build_checks(
        [DOCS_SCOPED_PIPELINE],
        _context(changed_files=("docs/guide.md",)),
        app_slug="konflux-internal-p02",
    )
    assert checks == []

    triggered = select_build_checks(
        [DOCS_SCOPED_PIPELINE],
        _context(changed_files=("src/main.go",)),
        app_slug="konflux-internal-p02",
    )
    assert [item.key for item in triggered] == ["trainer-v3-6"]


def test_label_gated_pipeline_is_not_expected_without_the_label() -> None:
    assert select_build_checks([LABEL_PIPELINE], _context(), app_slug="konflux-internal-p02") == []
    labeled = select_build_checks(
        [LABEL_PIPELINE],
        _context(labels=frozenset({"kfbuild-all"})),
        app_slug="konflux-internal-p02",
    )
    assert [item.name_suffix for item in labeled] == ["manual-on-pull-request"]


def test_cel_ignores_other_tekton_files_but_matches_product_changes() -> None:
    only_other_tekton = select_build_checks(
        [EVAL_HUB_PIPELINE],
        _context(changed_files=(".tekton/other.yaml",)),
        app_slug="konflux-internal-p02",
    )
    assert only_other_tekton == []
    product_change = select_build_checks(
        [EVAL_HUB_PIPELINE],
        _context(changed_files=("README.md",)),
        app_slug="konflux-internal-p02",
    )
    assert len(product_change) == 1


def test_feasibility_requires_every_matrix_job() -> None:
    run = FeasibilityRun(
        found=True,
        status="completed",
        conclusion="success",
        head_sha=SHA,
        jobs=(
            WorkflowJob(FEASIBILITY_SETUP_JOB, "completed", "success", None, None),
            WorkflowJob("main-release-feasibility (rhoai-2.25)", "completed", "success", None, None),
            WorkflowJob("main-release-feasibility (rhoai-3.0)", "completed", "failure", None, None),
        ),
    )
    expected = select_feasibility_checks(run)
    assert [item.key for item in expected] == [
        FEASIBILITY_SETUP_JOB,
        "main-release-feasibility (rhoai-2.25)",
        "main-release-feasibility (rhoai-3.0)",
    ]
    assert [item.name_suffix for item in expected] == [item.key for item in expected]
    for check_name in (
        lambda item: item.name_suffix or "",
        lambda item: f"Main-to-release feasibility / {item.name_suffix}",
    ):
        runs = [
            _actions_check(check_name(item), "failure" if "3.0" in item.key else "success")
            for item in expected
        ]
        result = evaluate_checks(expected, runs, now=NOW, deadline=revision_deadline(COMMITTER))
        assert result.outcome == "fail"
        assert "rhoai-3.0" in result.findings[2].key


def test_missing_tekton_directory_is_not_a_query_error() -> None:
    def runner(args: list[str]) -> str:
        raise GhCommandError(["gh", *args], 1, "HTTP 404: Not Found")

    assert fetch_tekton_documents("acme", "widget", SHA, runner) == []


class _Updater:
    def __init__(self, *, check_name: str, dry_run: bool = False) -> None:
        self.check_name = check_name
        self.dry_run = dry_run
        self.posts: list[tuple[str, str, dict[str, Any]]] = []

    def post_status_for_pr(self, pr_url: str, status: str, **kwargs: Any) -> StatusUpdateResult:
        self.posts.append((pr_url, status, kwargs))
        return StatusUpdateResult(
            pr=PullRequestRef("red-hat-data-services", "eval-hub", 20),
            head_sha=SHA,
            state=status,
            context=self.check_name,
            dry_run=self.dry_run,
            skipped=False,
        )


class _GitHub:
    def __init__(self) -> None:
        self.files = ["README.md"]
        self.tekton = {"odh-eval-hub-pull-request.yaml": EVAL_HUB_PIPELINE}
        self.check_runs: list[dict[str, Any]] = []
        self.workflow_status = "completed"
        self.workflow_conclusion = "success"
        self.jobs = [
            _job(FEASIBILITY_SETUP_JOB, "success"),
            _job("main-release-feasibility (rhoai-2.25)", "success"),
            _job("main-release-feasibility (rhoai-3.0)", "success"),
        ]
        self.fail_checks = False
        self.head_sha = SHA
        self.state = "OPEN"
        self.mergeable = "MERGEABLE"
        self.merge_state = "CLEAN"
        self.no_workflow = False

    def __call__(self, args: list[str]) -> str:
        if args[0] == "pr":
            return json.dumps(
                {
                    "state": self.state,
                    "mergeable": self.mergeable,
                    "mergeStateStatus": self.merge_state,
                    "baseRefName": "stable",
                    "headRefOid": self.head_sha,
                    "labels": [],
                    "commits": [{"oid": self.head_sha, "committedDate": "2026-10-06T10:00:00Z"}],
                    "url": URL,
                }
            )
        endpoint = args[1]
        if "check-runs" in endpoint:
            if self.fail_checks:
                raise GhCommandError(["gh", *args], 1, "check API unavailable")
            return json.dumps({"total_count": len(self.check_runs), "check_runs": self.check_runs})
        if "/pulls/" in endpoint and "/files" in endpoint:
            return json.dumps([{"filename": name} for name in self.files])
        if "contents/.tekton/" in endpoint:
            name = endpoint.split("contents/.tekton/", 1)[1].split("?", 1)[0]
            text = self.tekton[name]
            encoded = base64.b64encode(text.encode()).decode()
            return json.dumps({"encoding": "base64", "content": encoded, "name": name})
        if "contents/.tekton" in endpoint:
            return json.dumps([{"name": name, "type": "file"} for name in self.tekton])
        if "/jobs" in endpoint:
            return json.dumps({"total_count": len(self.jobs), "jobs": self.jobs})
        if "actions/runs" in endpoint:
            if self.no_workflow:
                return json.dumps({"total_count": 0, "workflow_runs": []})
            return json.dumps(
                {
                    "total_count": 1,
                    "workflow_runs": [
                        {
                            "id": 9,
                            "name": "Main-to-release feasibility",
                            "path": ".github/workflows/main-release-feasibility.yml",
                            "head_sha": self.head_sha,
                            "status": self.workflow_status,
                            "conclusion": self.workflow_conclusion,
                            "run_attempt": 2,
                        }
                    ],
                }
            )
        raise AssertionError(args)


def _job(name: str, conclusion: str, status: str = "completed") -> dict[str, str]:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "head_sha": SHA,
        "html_url": f"https://github.com/red-hat-data-services/eval-hub/actions/runs/9/job/{name}",
        "started_at": "2026-10-06T10:05:00Z",
    }


def _actions_check(name: str, conclusion: str, status: str = "completed") -> CheckRun:
    return CheckRun(
        id=abs(hash(name)) % 100000,
        name=name,
        status=status,
        conclusion=conclusion,
        head_sha=SHA,
        html_url=f"https://github.com/red-hat-data-services/eval-hub/runs/{name}",
        details_url=None,
        external_id=None,
        app_slug="github-actions",
        app_id=15368,
        started_at="2026-10-06T10:05:00Z",
    )


def _build_check(
    suffix: str,
    conclusion: str = "success",
    *,
    status: str = "completed",
    head_sha: str = SHA,
    started_at: str = "2026-10-06T10:05:00Z",
    run_id: int = 1,
) -> dict[str, Any]:
    return {
        "id": run_id,
        "name": f"Konflux Production Internal / {suffix}",
        "status": status,
        "conclusion": conclusion,
        "head_sha": head_sha,
        "html_url": f"https://github.com/red-hat-data-services/eval-hub/runs/{run_id}",
        "details_url": f"https://konflux.example/ns/rhoai-tenant/pipelinerun/{suffix}-s8lwr",
        "external_id": f"{suffix}-s8lwr",
        "started_at": started_at,
        "app": APP,
    }


def _state(tmp_path: Path, *, builds: list[dict[str, str]] | None = None) -> Path:
    path = tmp_path / "state.json"
    path.write_text(
        json.dumps(
            {
                "pull-requests": [
                    {
                        "repo": "eval-hub",
                        "pr-url": URL,
                        "pr-status": "new",
                        "builds": builds
                        if builds is not None
                        else [{"component": "existing", "image": "quay.io/example/existing:1"}],
                    }
                ]
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def _run(path: Path, github: _GitHub, *, now: datetime = NOW) -> tuple[Any, _Updater]:
    holder: dict[str, _Updater] = {}

    def factory(*, check_name: str, dry_run: bool = False) -> _Updater:
        holder["updater"] = _Updater(check_name=check_name, dry_run=dry_run)
        return holder["updater"]

    result = run_monitor(
        path,
        updater_factory=factory,
        gh_runner=github,
        now=now,
        build_app_slug="konflux-internal-p02",
        build_app_id=906404,
    )
    return result, holder["updater"]


def _saved(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def test_all_current_checks_pass_and_preserve_image_uri(tmp_path: Path) -> None:
    github = _GitHub()
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317"),
        _build_check(
            "ignored",
            run_id=99,
            head_sha=OTHER_SHA,
        ),
    ]
    github.check_runs.extend(
        [
            {
                "id": 10,
                "name": "Main-to-release feasibility / Select release sync inputs",
                "status": "completed",
                "conclusion": "success",
                "head_sha": SHA,
                "html_url": "https://github.com/red-hat-data-services/eval-hub/runs/10",
                "app": {"slug": "github-actions", "id": 15368},
                "started_at": "2026-10-06T10:01:00Z",
            },
            {
                "id": 11,
                "name": "Main-to-release feasibility / main-release-feasibility (rhoai-2.25)",
                "status": "completed",
                "conclusion": "success",
                "head_sha": SHA,
                "html_url": "https://github.com/red-hat-data-services/eval-hub/runs/11",
                "app": {"slug": "github-actions", "id": 15368},
                "started_at": "2026-10-06T10:02:00Z",
            },
            {
                "id": 12,
                "name": "Main-to-release feasibility / main-release-feasibility (rhoai-3.0)",
                "status": "completed",
                "conclusion": "success",
                "head_sha": SHA,
                "html_url": "https://github.com/red-hat-data-services/eval-hub/runs/12",
                "app": {"slug": "github-actions", "id": 15368},
                "started_at": "2026-10-06T10:03:00Z",
            },
        ]
    )
    path = _state(tmp_path)
    result, updater = _run(path, github)
    assert result.success_urls == [URL]
    assert updater.posts[0][1] == "success"
    saved = _saved(path)
    assert saved["pull-requests"][0]["pr-status"] == "success"
    assert saved["pull-requests"][0]["builds"] == [
        {"component": "existing", "image": "quay.io/example/existing:1"}
    ]
    assert "odh-eval-hub-v3-6" not in json.dumps(saved["pull-requests"][0]["builds"])
    assert any("odh-eval-hub-v3-6" in line for line in result.reports) or result.success_urls


def test_bare_feasibility_job_names_pass(tmp_path: Path) -> None:
    github = _GitHub()
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317"),
        *_feasibility_success_payloads(bare=True),
    ]
    path = _state(tmp_path, builds=[])
    result, updater = _run(path, github)
    assert result.success_urls == [URL]
    assert updater.posts[0][1] == "success"


def test_mixed_component_results_fail_the_repository(tmp_path: Path) -> None:
    github = _GitHub()
    github.tekton["bff.yaml"] = SECOND_PIPELINE
    github.files = ["pkg/main.go", "README.md"]
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317", "success", run_id=1),
        _build_check("odh-core-bff-on-pull-request-20", "failure", run_id=2),
        *_feasibility_success_payloads(),
    ]
    path = _state(tmp_path, builds=[])
    result, updater = _run(path, github)
    assert result.build_failure_urls == [URL]
    assert updater.posts[0][1] == "failure"
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-failure"
    assert any("odh-core-bff" in line and "pipelinerun" in line for line in result.reports)
    assert all(item["pr-status"] != "success" for item in _saved(path)["pull-requests"])


def test_pending_component_blocks_and_times_out_on_the_original_deadline(tmp_path: Path) -> None:
    github = _GitHub()
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317", status="in_progress", conclusion=None),
        *_feasibility_success_payloads(),
    ]
    path = _state(tmp_path, builds=[])
    result, updater = _run(path, github)
    assert result.pending_urls == [URL]
    assert updater.posts[0][1] == "pending"
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-pending"

    expired, updater = _run(path, github, now=COMMITTER + timedelta(hours=6))
    assert expired.build_failure_urls == [URL]
    assert updater.posts[0][1] == "failure"
    assert expired.reports
    assert "timed out after 6h" in expired.reports[0]
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-failure"


def test_missing_check_is_not_a_no_build_success(tmp_path: Path) -> None:
    github = _GitHub()
    github.check_runs = [*_feasibility_success_payloads()]
    path = _state(tmp_path, builds=[])
    result, _updater = _run(path, github)
    assert result.pending_urls == [URL]
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-pending"


def test_skipped_build_fails_immediately(tmp_path: Path) -> None:
    github = _GitHub()
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317", "skipped"),
        *_feasibility_success_payloads(),
    ]
    path = _state(tmp_path, builds=[])
    result, updater = _run(path, github)
    assert result.build_failure_urls == [URL]
    assert updater.posts[0][1] == "failure"


def test_feasibility_failure_blocks_when_builds_pass(tmp_path: Path) -> None:
    github = _GitHub()
    github.jobs[2] = _job("main-release-feasibility (rhoai-3.0)", "failure")
    github.workflow_conclusion = "failure"
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317"),
        *_feasibility_success_payloads(fail_last=True),
    ]
    path = _state(tmp_path, builds=[])
    result, updater = _run(path, github)
    assert result.merge_failure_urls == [URL]
    assert result.success_urls == []
    assert updater.posts[0][1] == "failure"
    assert _saved(path)["pull-requests"][0]["pr-status"] == "merge-failure"
    assert any("rhoai-3.0" in line for line in result.reports)


def test_pending_feasibility_blocks_a_green_build(tmp_path: Path) -> None:
    github = _GitHub()
    github.workflow_status = "in_progress"
    github.workflow_conclusion = ""
    github.jobs[2] = _job("main-release-feasibility (rhoai-3.0)", "", status="queued")
    payloads = _feasibility_success_payloads()
    payloads[-1]["status"] = "queued"
    payloads[-1]["conclusion"] = None
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317"),
        *payloads,
    ]
    path = _state(tmp_path, builds=[])
    result, updater = _run(path, github)
    assert result.pending_urls == [URL]
    assert updater.posts[0][1] == "pending"
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-pending"


def test_no_build_change_still_requires_feasibility(tmp_path: Path) -> None:
    github = _GitHub()
    github.tekton = {"trainer.yaml": DOCS_SCOPED_PIPELINE}
    github.files = ["docs/guide.md"]
    github.check_runs = [*_feasibility_success_payloads()]
    path = _state(tmp_path, builds=[])
    result, updater = _run(path, github)
    assert result.success_urls == [URL]
    assert "no component builds expected" in updater.posts[0][2]["description"]

    github.jobs[1] = _job("main-release-feasibility (rhoai-2.25)", "failure")
    github.check_runs = [*_feasibility_success_payloads()]
    github.check_runs[1]["conclusion"] = "failure"
    failed, _updater = _run(path, github)
    assert failed.merge_failure_urls == [URL]
    assert _saved(path)["pull-requests"][0]["pr-status"] == "merge-failure"


def test_check_query_error_cannot_pass_and_hits_the_deadline(tmp_path: Path) -> None:
    github = _GitHub()
    github.fail_checks = True
    path = _state(tmp_path, builds=[])
    result, updater = _run(path, github)
    assert result.success_urls == []
    assert updater.posts[0][1] == "pending"
    assert "check API unavailable" in result.reports[0]

    expired, updater = _run(path, github, now=COMMITTER + timedelta(hours=6, seconds=1))
    assert expired.success_urls == []
    assert updater.posts[0][1] == "failure"
    assert _saved(path)["pull-requests"][0]["pr-status"] == "merge-failure"


def test_older_revision_cannot_authorize_the_current_sha(tmp_path: Path) -> None:
    github = _GitHub()
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317", head_sha=OTHER_SHA),
        *_feasibility_success_payloads(),
    ]
    path = _state(tmp_path, builds=[])
    result, _updater = _run(path, github)
    assert result.pending_urls == [URL]


def test_direct_success_path_is_not_used_for_a_skipped_expected_build(tmp_path: Path) -> None:
    github = _GitHub()
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317", "neutral"),
        *_feasibility_success_payloads(),
    ]
    path = _state(tmp_path, builds=[])
    result, _updater = _run(path, github)
    assert result.build_failure_urls == [URL]


def _feasibility_success_payloads(*, fail_last: bool = False, bare: bool = False) -> list[dict[str, Any]]:
    names = [
        "Select release sync inputs" if bare else "Main-to-release feasibility / Select release sync inputs",
        (
            "main-release-feasibility (rhoai-2.25)"
            if bare
            else "Main-to-release feasibility / main-release-feasibility (rhoai-2.25)"
        ),
        (
            "main-release-feasibility (rhoai-3.0)"
            if bare
            else "Main-to-release feasibility / main-release-feasibility (rhoai-3.0)"
        ),
    ]
    payloads = []
    for index, name in enumerate(names, start=10):
        conclusion = "failure" if fail_last and index == 12 else "success"
        payloads.append(
            {
                "id": index,
                "name": name,
                "status": "completed",
                "conclusion": conclusion,
                "head_sha": SHA,
                "html_url": f"https://github.com/red-hat-data-services/eval-hub/runs/{index}",
                "app": {"slug": "github-actions", "id": 15368},
                "started_at": f"2026-10-06T10:0{index - 9}:00Z",
            }
        )
    return payloads


def test_merge_conflict_stays_merge_failure_without_checks(tmp_path: Path) -> None:
    github = _GitHub()
    github.mergeable = "CONFLICTING"
    github.merge_state = "DIRTY"
    path = _state(tmp_path, builds=[])
    result, updater = _run(path, github)
    assert result.merge_failure_urls == [URL]
    assert updater.posts[0][1] == "failure"
    assert _saved(path)["pull-requests"][0]["pr-status"] == "merge-failure"


def test_new_commit_starts_a_new_six_hour_window(tmp_path: Path) -> None:
    github = _GitHub()
    github.check_runs = [*_feasibility_success_payloads()]
    path = _state(tmp_path, builds=[])
    expired, _updater = _run(path, github, now=COMMITTER + timedelta(hours=6, seconds=1))
    assert expired.build_failure_urls == [URL]

    github.head_sha = "c" * 40
    for payload in github.check_runs:
        payload["head_sha"] = github.head_sha
    for job in github.jobs:
        job["head_sha"] = github.head_sha

    def view(args: list[str]) -> str:
        if args[0] == "pr":
            body = json.loads(_GitHub.__call__(github, args))
            body["commits"] = [{"oid": github.head_sha, "committedDate": "2026-10-06T16:00:00Z"}]
            body["headRefOid"] = github.head_sha
            return json.dumps(body)
        return _GitHub.__call__(github, args)

    holder: dict[str, _Updater] = {}

    def factory(*, check_name: str, dry_run: bool = False) -> _Updater:
        holder["updater"] = _Updater(check_name=check_name, dry_run=dry_run)
        return holder["updater"]

    refreshed = run_monitor(
        path,
        updater_factory=factory,
        gh_runner=view,
        now=datetime(2026, 10, 6, 16, 30, tzinfo=timezone.utc),
        build_app_slug="konflux-internal-p02",
        build_app_id=906404,
    )
    assert refreshed.pending_urls == [URL]
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-pending"


def test_decide_repository_pr_reports_component_and_link() -> None:
    pull_request = RepositoryPullRequest(
        url=URL,
        owner="red-hat-data-services",
        repo="eval-hub",
        number=20,
        state="OPEN",
        mergeable="MERGEABLE",
        merge_state_status="CLEAN",
        base_ref="stable",
        head_sha=SHA,
        labels=frozenset(),
        changed_files=("README.md",),
        committer_at=COMMITTER,
    )
    check = CheckRun(
        id=5,
        name="Konflux Production Internal / odh-eval-hub-on-pull-request-65317",
        status="completed",
        conclusion="failure",
        head_sha=SHA,
        html_url="https://github.com/red-hat-data-services/eval-hub/runs/5",
        details_url="https://konflux.example/ns/rhoai-tenant/pipelinerun/odh-eval-hub-on-pull-request-65317-s8lwr",
        external_id="odh-eval-hub-on-pull-request-65317-s8lwr",
        app_slug="konflux-internal-p02",
        app_id=906404,
        started_at="2026-10-06T10:05:00Z",
    )
    feasibility_runs = [
        _actions_check("Main-to-release feasibility / Select release sync inputs", "success"),
    ]
    feasibility = FeasibilityRun(
        found=True,
        status="completed",
        conclusion="success",
        head_sha=SHA,
        jobs=(WorkflowJob(FEASIBILITY_SETUP_JOB, "completed", "success", None, None),),
    )
    decision = decide_repository_pr(
        pull_request,
        tekton_documents=[EVAL_HUB_PIPELINE],
        tekton_error=None,
        check_runs=[check, *feasibility_runs],
        check_query_error=None,
        feasibility=feasibility,
        now=NOW,
        build_app_slug="konflux-internal-p02",
    )
    assert decision.pr_status == "build-failure"
    assert "odh-eval-hub-v3-6" in decision.reason
    assert decision.target_url is not None and "pipelinerun" in decision.target_url
    assert len(decision.description) <= 140
