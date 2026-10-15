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
    cel_matches,
    fetch_feasibility_run,
    fetch_tekton_documents,
    resolved_pipeline_name,
    select_build_checks,
    select_feasibility_checks,
)
from lib.gap_pr_gates import decide_repository_pr, RepositoryPullRequest
from lib.github_check_runs import CheckRun, evaluate_checks, revision_deadline
from lib.github_cli import GhCommandError
from lib.pr_status_updater import PRStatusUpdater, PullRequestRef, StatusUpdateResult
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
    own_pipeline = select_build_checks(
        [EVAL_HUB_PIPELINE],
        _context(changed_files=(".tekton/odh-eval-hub-pull-request.yaml",)),
        app_slug="konflux-internal-p02",
    )
    assert [item.key for item in own_pipeline] == ["odh-eval-hub-v3-6"]


def test_files_all_exists_matches_when_any_changed_file_is_outside_tekton() -> None:
    mixed = select_build_checks(
        [EVAL_HUB_PIPELINE],
        _context(changed_files=("README.md", ".tekton/other.yaml")),
        app_slug="konflux-internal-p02",
    )
    assert [item.key for item in mixed] == ["odh-eval-hub-v3-6"]

    universal = EVAL_HUB_PIPELINE.replace("files.all.exists", "files.all.all")
    every_file = select_build_checks(
        [universal],
        _context(changed_files=("README.md", ".tekton/other.yaml")),
        app_slug="konflux-internal-p02",
    )
    assert every_file == []
    assert cel_matches("files.all.all(p, p.matches('^README'))", _context(changed_files=()))
    assert not cel_matches("files.all.exists(p, p.matches('^README'))", _context(changed_files=()))


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


def test_undiscovered_feasibility_run_does_not_shrink_to_the_setup_job() -> None:
    missing = FeasibilityRun(
        found=False,
        status=None,
        conclusion=None,
        head_sha=SHA,
        jobs=(),
    )
    assert select_feasibility_checks(missing) == []
    empty_inventory = FeasibilityRun(
        found=True,
        status="completed",
        conclusion="success",
        head_sha=SHA,
        jobs=(),
    )
    assert select_feasibility_checks(empty_inventory) == []


def _workflow_run(
    run_id: int,
    *,
    attempt: int,
    started: str,
    status: str,
    conclusion: str | None,
    **extra: Any,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": run_id,
        "name": "Main-to-release feasibility",
        "path": ".github/workflows/main-release-feasibility.yml",
        "head_sha": SHA,
        "status": status,
        "conclusion": conclusion,
        "run_attempt": attempt,
        "run_started_at": started,
    }
    payload.update(extra)
    return payload


def _fetch_feasibility(
    runs: list[dict[str, Any]],
    jobs: dict[int, list[dict[str, Any]]],
) -> tuple[FeasibilityRun, list[str]]:
    calls: list[str] = []

    def runner(args: list[str]) -> str:
        endpoint = args[1]
        calls.append(endpoint)
        if "/jobs" in endpoint:
            run_id = int(endpoint.split("/runs/", 1)[1].split("/", 1)[0])
            batch = jobs[run_id]
            return json.dumps({"total_count": len(batch), "jobs": batch})
        return json.dumps({"total_count": len(runs), "workflow_runs": runs})

    return fetch_feasibility_run("acme", "widget", SHA, runner), calls


def _feasibility_job(name: str, status: str, conclusion: str | None) -> dict[str, Any]:
    return {
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "head_sha": SHA,
    }


def test_later_feasibility_run_beats_an_older_higher_attempt() -> None:
    result, calls = _fetch_feasibility(
        [
            _workflow_run(
                10,
                attempt=3,
                started="2026-10-06T10:00:00Z",
                status="completed",
                conclusion="success",
                updated_at="2026-10-06T16:00:00Z",
            ),
            _workflow_run(
                20,
                attempt=1,
                started="2026-10-06T12:00:00Z",
                status="in_progress",
                conclusion=None,
                updated_at="2026-10-06T12:01:00Z",
            ),
        ],
        {
            10: [_feasibility_job("obsolete-matrix", "completed", "success")],
            20: [_feasibility_job("current-matrix", "in_progress", None)],
        },
    )
    assert result.found is True
    assert result.status == "in_progress"
    assert [job.name for job in result.jobs] == ["current-matrix"]
    assert any("/runs/20/jobs" in call for call in calls)
    assert not any("/runs/10/jobs" in call for call in calls)


def test_later_rerun_of_an_earlier_run_id_is_the_latest_execution() -> None:
    result, calls = _fetch_feasibility(
        [
            _workflow_run(
                50,
                attempt=5,
                started="2026-10-06T11:00:00Z",
                status="completed",
                conclusion="success",
                created_at="2026-10-06T11:00:00Z",
            ),
            _workflow_run(
                4,
                attempt=2,
                started="2026-10-06T15:00:00Z",
                status="in_progress",
                conclusion=None,
                created_at="2026-10-06T09:00:00Z",
            ),
        ],
        {
            50: [_feasibility_job("newer-id-matrix", "completed", "success")],
            4: [_feasibility_job("rerun-matrix", "in_progress", None)],
        },
    )
    assert result.status == "in_progress"
    assert [job.name for job in result.jobs] == ["rerun-matrix"]
    assert any("/runs/4/jobs" in call for call in calls)
    assert not any("/runs/50/jobs" in call for call in calls)


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
        self.committer_date = "2026-10-06T10:00:00Z"
        self.author_date = "2026-10-06T10:00:00Z"
        self.commit_shas: list[str] = []
        self.commit_endpoints: list[str] = []
        self.pr_views: list[list[str]] = []
        self.hide_head_commit = False
        self.state = "OPEN"
        self.mergeable = "MERGEABLE"
        self.merge_state = "CLEAN"
        self.no_workflow = False

    def __call__(self, args: list[str]) -> str:
        if args[0] == "pr":
            self.pr_views.append(list(args))
            return json.dumps(
                {
                    "state": self.state,
                    "mergeable": self.mergeable,
                    "mergeStateStatus": self.merge_state,
                    "baseRefName": "stable",
                    "headRefOid": self.head_sha,
                    "labels": [],
                    "commits": self._listed_commits(),
                    "url": URL,
                }
            )
        endpoint = args[1]
        if "check-runs" in endpoint:
            if self.fail_checks:
                raise GhCommandError(["gh", *args], 1, "check API unavailable")
            return json.dumps({"total_count": len(self.check_runs), "check_runs": self.check_runs})
        if "/commits/" in endpoint and "check-runs" not in endpoint and "/statuses" not in endpoint:
            sha = endpoint.split("/commits/", 1)[1].split("?", 1)[0].split("/", 1)[0]
            self.commit_shas.append(sha)
            self.commit_endpoints.append(endpoint)
            return json.dumps(
                {
                    "sha": sha,
                    "commit": {
                        "author": {"date": self.author_date},
                        "committer": {"date": self.committer_date},
                    },
                }
            )
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

    def _listed_commits(self) -> list[dict[str, str]]:
        if not self.hide_head_commit:
            return [{"oid": self.head_sha, "committedDate": self.committer_date}]
        return [
            {"oid": f"{index:040x}", "committedDate": self.author_date}
            for index in range(100)
        ]


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
    assert updater.posts[0][2]["head_sha"] == SHA
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


def test_unfinished_build_stays_pending_past_the_revision_deadline(tmp_path: Path) -> None:
    github = _GitHub()
    github.check_runs = [
        _build_check(
            "odh-eval-hub-on-pull-request-65317",
            status="queued",
            conclusion=None,
        ),
        *_feasibility_success_payloads(),
    ]
    path = _state(tmp_path, builds=[])
    result, updater = _run(path, github)
    assert result.pending_urls == [URL]
    assert result.build_failure_urls == []
    assert updater.posts[0][1] == "pending"
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-pending"

    for status in ("queued", "in_progress"):
        github.check_runs[0] = _build_check(
            "odh-eval-hub-on-pull-request-65317",
            status=status,
            conclusion=None,
        )
        still_running, updater = _run(path, github, now=COMMITTER + timedelta(hours=7))
        assert still_running.pending_urls == [URL], status
        assert still_running.build_failure_urls == []
        assert updater.posts[0][1] == "pending"
        assert "timed out" not in " ".join(still_running.reports)
        assert _saved(path)["pull-requests"][0]["pr-status"] == "build-pending"

    github.check_runs[0] = _build_check("odh-eval-hub-on-pull-request-65317")
    passed, updater = _run(path, github, now=COMMITTER + timedelta(hours=7))
    assert passed.success_urls == [URL]
    assert passed.build_failure_urls == []
    assert updater.posts[0][1] == "success"
    assert _saved(path)["pull-requests"][0]["pr-status"] == "success"


def test_missing_check_is_not_a_no_build_success(tmp_path: Path) -> None:
    github = _GitHub()
    github.check_runs = [*_feasibility_success_payloads()]
    path = _state(tmp_path, builds=[])
    result, _updater = _run(path, github)
    assert result.pending_urls == [URL]
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-pending"


def test_mixed_tekton_change_keeps_a_missing_build_pending_past_the_deadline(
    tmp_path: Path,
) -> None:
    github = _GitHub()
    github.files = ["README.md", ".tekton/other.yaml"]
    github.check_runs = [*_feasibility_success_payloads()]
    path = _state(tmp_path, builds=[])
    result, updater = _run(path, github)
    assert result.success_urls == []
    assert result.pending_urls == [URL]
    assert updater.posts[0][1] == "pending"
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-pending"

    still_missing, updater = _run(path, github, now=COMMITTER + timedelta(hours=6))
    assert still_missing.success_urls == []
    assert still_missing.build_failure_urls == []
    assert still_missing.pending_urls == [URL]
    assert updater.posts[0][1] == "pending"
    assert "timed out" not in " ".join(still_missing.reports)
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-pending"


def test_mixed_tekton_change_fails_when_the_selected_build_fails(tmp_path: Path) -> None:
    github = _GitHub()
    github.files = ["README.md", ".tekton/other.yaml"]
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317", "failure"),
        *_feasibility_success_payloads(),
    ]
    path = _state(tmp_path, builds=[])
    result, updater = _run(path, github)
    assert result.success_urls == []
    assert result.build_failure_urls == [URL]
    assert updater.posts[0][1] == "failure"
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-failure"
    assert any("odh-eval-hub-v3-6" in line for line in result.reports)


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


def test_undiscovered_feasibility_workflow_stays_unresolved_until_jobs_are_known(
    tmp_path: Path,
) -> None:
    github = _GitHub()
    github.no_workflow = True
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317"),
        _feasibility_success_payloads()[0],
    ]
    path = _state(tmp_path, builds=[])
    result, updater = _run(path, github)
    assert result.success_urls == []
    assert result.pending_urls == [URL]
    assert updater.posts[0][1] == "pending"
    assert "has not been discovered" in result.reports[0]
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-pending"

    expired, updater = _run(path, github, now=COMMITTER + timedelta(hours=6))
    assert expired.success_urls == []
    assert expired.merge_failure_urls == [URL]
    assert updater.posts[0][1] == "failure"
    assert "timed out after 6h" in expired.reports[0]
    assert _saved(path)["pull-requests"][0]["pr-status"] == "merge-failure"

    github.no_workflow = False
    github.jobs[2] = _job("main-release-feasibility (rhoai-3.0)", "failure")
    github.workflow_conclusion = "failure"
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317"),
        *_feasibility_success_payloads(fail_last=True),
    ]
    discovered, _updater = _run(path, github)
    assert discovered.success_urls == []
    assert discovered.merge_failure_urls == [URL]
    assert any("rhoai-3.0" in line for line in discovered.reports)

    github.jobs[2] = _job("main-release-feasibility (rhoai-3.0)", "success")
    github.workflow_conclusion = "success"
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317"),
        *_feasibility_success_payloads(),
    ]
    passed, updater = _run(path, github)
    assert passed.success_urls == [URL]
    assert updater.posts[0][1] == "success"
    assert _saved(path)["pull-requests"][0]["pr-status"] == "success"


def test_unfinished_workflow_uses_the_revision_deadline(tmp_path: Path) -> None:
    github = _GitHub()
    github.workflow_status = "in_progress"
    github.workflow_conclusion = ""
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317"),
        *_feasibility_success_payloads(),
    ]
    path = _state(tmp_path, builds=[])
    result, updater = _run(path, github)
    assert result.success_urls == []
    assert result.pending_urls == [URL]
    assert result.merge_failure_urls == []
    assert updater.posts[0][1] == "pending"
    assert "still in progress" in result.reports[0]
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-pending"

    expired, updater = _run(path, github, now=COMMITTER + timedelta(hours=6))
    assert expired.pending_urls == []
    assert expired.build_failure_urls == []
    assert expired.merge_failure_urls == [URL]
    assert updater.posts[0][1] == "failure"
    assert "timed out after 6h" in expired.reports[0]
    assert _saved(path)["pull-requests"][0]["pr-status"] == "merge-failure"

    again, _updater = _run(path, github, now=COMMITTER + timedelta(hours=7))
    assert again.merge_failure_urls == [URL]
    assert again.pending_urls == []
    assert "timed out after 6h" in again.reports[0]
    assert _saved(path)["pull-requests"][0]["pr-status"] == "merge-failure"

    new_sha = "c" * 40
    github.head_sha = new_sha
    github.committer_date = "2026-10-06T16:00:00Z"
    for payload in github.check_runs:
        payload["head_sha"] = new_sha
    for job in github.jobs:
        job["head_sha"] = new_sha

    holder: dict[str, _Updater] = {}

    def factory(*, check_name: str, dry_run: bool = False) -> _Updater:
        holder["updater"] = _Updater(check_name=check_name, dry_run=dry_run)
        return holder["updater"]

    refreshed = run_monitor(
        path,
        updater_factory=factory,
        gh_runner=github,
        now=datetime(2026, 10, 6, 16, 30, tzinfo=timezone.utc),
        build_app_slug="konflux-internal-p02",
        build_app_id=906404,
    )
    assert refreshed.pending_urls == [URL]
    assert refreshed.merge_failure_urls == []
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-pending"

    refreshed_deadline = run_monitor(
        path,
        updater_factory=factory,
        gh_runner=github,
        now=datetime(2026, 10, 6, 22, 0, tzinfo=timezone.utc),
        build_app_slug="konflux-internal-p02",
        build_app_id=906404,
    )
    assert refreshed_deadline.merge_failure_urls == [URL]
    assert refreshed_deadline.pending_urls == []
    assert "timed out after 6h" in refreshed_deadline.reports[0]
    assert _saved(path)["pull-requests"][0]["pr-status"] == "merge-failure"


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


def test_missing_build_stays_pending_past_the_revision_deadline(tmp_path: Path) -> None:
    github = _GitHub()
    github.check_runs = [*_feasibility_success_payloads()]
    path = _state(tmp_path, builds=[])
    expired, _updater = _run(path, github, now=COMMITTER + timedelta(hours=6, seconds=1))
    assert expired.pending_urls == [URL]
    assert expired.build_failure_urls == []
    assert expired.merge_failure_urls == []
    assert "timed out" not in " ".join(expired.reports)
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-pending"


def test_truncated_commit_list_uses_the_captured_head_sha(tmp_path: Path) -> None:
    github = _GitHub()
    github.hide_head_commit = True
    github.author_date = "2026-10-06T08:00:00Z"
    github.committer_date = "2026-10-06T10:00:00Z"
    github.workflow_status = "in_progress"
    github.workflow_conclusion = ""
    github.check_runs = [
        _build_check("odh-eval-hub-on-pull-request-65317"),
        *_feasibility_success_payloads(),
    ]
    path = _state(tmp_path, builds=[])
    listed = github._listed_commits()
    assert len(listed) == 100
    assert SHA not in {item["oid"] for item in listed}

    pending, _updater = _run(
        path,
        github,
        now=datetime(2026, 10, 6, 15, 0, tzinfo=timezone.utc),
    )
    assert pending.pending_urls == [URL]
    assert pending.build_failure_urls == []
    assert pending.merge_failure_urls == []
    assert github.pr_views
    assert github.commit_endpoints == [f"repos/red-hat-data-services/eval-hub/commits/{SHA}"]
    assert all("commits" not in fields for view in github.pr_views for fields in view)

    expired, _updater = _run(
        path,
        github,
        now=datetime(2026, 10, 6, 16, 0, tzinfo=timezone.utc),
    )
    assert expired.merge_failure_urls == [URL]
    assert expired.build_failure_urls == []
    assert expired.pending_urls == []
    assert "timed out after 6h" in expired.reports[0]
    assert github.commit_shas == [SHA, SHA]
    assert github.commit_endpoints == [
        f"repos/red-hat-data-services/eval-hub/commits/{SHA}",
        f"repos/red-hat-data-services/eval-hub/commits/{SHA}",
    ]


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
    assert decision.head_sha == SHA
    assert "odh-eval-hub-v3-6" in decision.reason
    assert decision.target_url is not None and "pipelinerun" in decision.target_url
    assert len(decision.description) <= 140


class _Cmd:
    def __init__(self, stdout: str = "") -> None:
        self.stdout = stdout
        self.returncode = 0
        self.stderr = ""


def _flag_values(command: list[str]) -> dict[str, str]:
    values: dict[str, str] = {}
    for index, part in enumerate(command):
        if part == "-f" and index + 1 < len(command):
            key, _, value = command[index + 1].partition("=")
            values[key] = value
    return values


def _passing_check_runs() -> list[dict[str, Any]]:
    return [
        _build_check("odh-eval-hub-on-pull-request-65317"),
        *_feasibility_success_payloads(),
    ]


def _status_updater_factory(runner: Any) -> Any:
    def factory(*, check_name: str, dry_run: bool = False) -> PRStatusUpdater:
        return PRStatusUpdater(check_name=check_name, dry_run=dry_run, runner=runner)

    return factory


def test_moved_head_is_reevaluated_instead_of_receiving_the_old_success(tmp_path: Path) -> None:
    github = _GitHub()
    github.check_runs = _passing_check_runs()
    posts: list[list[str]] = []
    head_moved = {"value": False}
    new_sha = "c" * 40

    def gh_runner(args: list[str]) -> str:
        if args[0] == "pr" and head_moved["value"]:
            github.head_sha = new_sha
        return github(args)

    def updater_runner(command: list[str]) -> _Cmd:
        if "--method" in command:
            posts.append(list(command))
            return _Cmd()
        path = command[2] if len(command) > 2 else ""
        if "/pulls/" in path:
            head_moved["value"] = True
            return _Cmd(
                stdout=json.dumps(
                    {
                        "sha": new_sha,
                        "owner": "red-hat-data-services",
                        "repo": "eval-hub",
                        "state": "open",
                    }
                )
            )
        return _Cmd()

    path = _state(tmp_path, builds=[])
    result = run_monitor(
        path,
        updater_factory=_status_updater_factory(updater_runner),
        gh_runner=gh_runner,
        now=NOW,
        build_app_slug="konflux-internal-p02",
        build_app_id=906404,
    )
    assert result.success_urls == []
    assert result.pending_urls == [URL]
    assert _saved(path)["pull-requests"][0]["pr-status"] == "build-pending"
    assert posts
    posted = [" ".join(command) for command in posts]
    assert all("state=success" not in command for command in posted)
    assert any(f"statuses/{new_sha}" in command and "state=pending" in command for command in posted)
    assert not any(f"statuses/{SHA}" in command and "state=success" in command for command in posted)


def test_head_that_moves_again_is_deferred_without_a_success_post(tmp_path: Path) -> None:
    github = _GitHub()
    github.check_runs = _passing_check_runs()
    posts: list[list[str]] = []
    views = {"n": 0}
    pulls = {"n": 0}
    new_sha = "c" * 40
    later_sha = "d" * 40

    def gh_runner(args: list[str]) -> str:
        if args[0] == "pr":
            views["n"] += 1
            if views["n"] > 1:
                github.head_sha = new_sha
        return github(args)

    def updater_runner(command: list[str]) -> _Cmd:
        if "--method" in command:
            posts.append(list(command))
            return _Cmd()
        path = command[2] if len(command) > 2 else ""
        if "/pulls/" in path:
            pulls["n"] += 1
            sha = new_sha if pulls["n"] == 1 else later_sha
            return _Cmd(
                stdout=json.dumps(
                    {
                        "sha": sha,
                        "owner": "red-hat-data-services",
                        "repo": "eval-hub",
                        "state": "open",
                    }
                )
            )
        return _Cmd()

    path = _state(tmp_path, builds=[])
    result = run_monitor(
        path,
        updater_factory=_status_updater_factory(updater_runner),
        gh_runner=gh_runner,
        now=NOW,
        build_app_slug="konflux-internal-p02",
        build_app_id=906404,
    )
    assert posts == []
    assert result.success_urls == []
    assert _saved(path)["pull-requests"][0]["pr-status"] == "new"
    assert URL in result.skipped_urls
    assert any("deferred" in line and URL in line for line in result.reports)


def test_current_head_is_published_and_an_unchanged_status_is_skipped(tmp_path: Path) -> None:
    github = _GitHub()
    github.check_runs = _passing_check_runs()
    posts: list[list[str]] = []
    published: dict[str, str] = {}

    def updater_runner(command: list[str]) -> _Cmd:
        if "--method" in command:
            posts.append(list(command))
            published.update(_flag_values(command))
            return _Cmd()
        path = command[2] if len(command) > 2 else ""
        if "/pulls/" in path:
            return _Cmd(
                stdout=json.dumps(
                    {
                        "sha": SHA,
                        "owner": "red-hat-data-services",
                        "repo": "eval-hub",
                        "state": "open",
                    }
                )
            )
        if path.endswith("/statuses") and published:
            return _Cmd(
                stdout=json.dumps(
                    {
                        "state": published["state"],
                        "description": published.get("description"),
                        "target_url": published.get("target_url"),
                    }
                )
            )
        return _Cmd()

    path = _state(tmp_path, builds=[])
    first = run_monitor(
        path,
        updater_factory=_status_updater_factory(updater_runner),
        gh_runner=github,
        now=NOW,
        build_app_slug="konflux-internal-p02",
        build_app_id=906404,
    )
    assert first.success_urls == [URL]
    assert first.updated_urls == [URL]
    assert len(posts) == 1
    posted = " ".join(posts[0])
    assert f"statuses/{SHA}" in posted
    assert "state=success" in posted

    second = run_monitor(
        path,
        updater_factory=_status_updater_factory(updater_runner),
        gh_runner=github,
        now=NOW,
        build_app_slug="konflux-internal-p02",
        build_app_id=906404,
    )
    assert second.success_urls == [URL]
    assert second.skipped_urls == [URL]
    assert second.updated_urls == []
    assert len(posts) == 1
