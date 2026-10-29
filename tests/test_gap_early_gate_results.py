"""Unit tests for GAP Early Gate results monitor (Leader comment → overall)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts import gap_early_gate_results as eg
from scripts.gap_pr_monitor import PublishResult
from lib.pr_status_updater import PullRequestRef, StatusUpdateResult


def _status_ok() -> StatusUpdateResult:
    return StatusUpdateResult(
        pr=PullRequestRef(owner="acme", repo="gap", number=9),
        head_sha="abc123",
        state="success",
        context="gated artifacts promoter",
        dry_run=False,
        skipped=False,
    )


# ---------------------------------------------------------------------------
# Fixtures — real shapes from GAP Leaders #4 / #5 / #51 / screenshot
# ---------------------------------------------------------------------------

PASSED_WITH_DIGESTS = """\
<!-- early-gate-build-complete
OPERATOR_IMAGE_REF_BY_DIGEST=quay.io/rhoai/pull-request-pipelines:odh-operator-eg-5
BUNDLE_IMAGE_REF_BY_DIGEST=quay.io/rhoai/pull-request-pipelines:odh-operator-bundle-eg-5
FBC_IMAGE_REF_BY_DIGEST=quay.io/rhoai/pull-request-pipelines:rhoai-fbc-fragment-eg-5
-->
:white_check_mark: **Early Gate passed** — `3.5.2`

| Field | Value |
|-------|-------|
| **Status** | :white_check_mark: SUCCESS |
| **Forked from** | `stable` _(fallback — the leader PR carries no operator PR)_ |
| **Ephemeral branch** | `eg-5-fba1d3d8-ed47-4cfa-b5af-3aaaf6f3ef6b` _(deleted when the run finished)_ |
| **PipelineRun** | `rhods-operator-gap-eg-build-bm2bq` |

**Artifacts**

All three in `quay.io/rhoai/pull-request-pipelines`.
"""

PASSED_BARE_MARKER = """\
<!-- early-gate-build-complete -->
:white_check_mark: **Early Gate passed** — `3.6`

| Field | Value |
|-------|-------|
| **Status** | :white_check_mark: SUCCESS |
| **Forked from** | `rhoai-3.6` |
| **Ephemeral branch** | `eg-51-7d909b18-266a-4846-868a-9da30889aece` _(deleted when the run finished)_ |
| **PipelineRun** | `early-gate-ci-build-6zdqn` |

**Artifacts**
"""

FAILED_BARE_MARKER = """\
<!-- early-gate-build-complete -->
:x: **Early Gate failed** — `3.6`

| Field | Value |
|-------|-------|
| **Status** | :x: FAILED |
| **Ephemeral branch** | `eg-54-8c2839eb-6598-45a4-89b4-b76f7cfe7f73` _(deleted when the run finished)_ |
| **PipelineRun** | `early-gate-ci-build-wsncm` |

**Blocks**

| Block | Build | DR-6 verification |
|-------|-------|-------------------|
| 1 · Operator | :x: | |
"""

FAILED_WITH_EMPTY_DIGESTS = """\
<!-- early-gate-build-complete
OPERATOR_IMAGE_REF_BY_DIGEST=
BUNDLE_IMAGE_REF_BY_DIGEST=
FBC_IMAGE_REF_BY_DIGEST=
-->
:x: **Early Gate failed** — `3.5`

| Field | Value |
|-------|-------|
| **Status** | :x: FAILED |
| **Ephemeral branch** | `eg-4-5cb27b85-1abc-4825-ba0b-a68b4a588381` _(deleted when the run finished)_ |
| **PipelineRun** | `rhods-operator-gap-eg-build-s88ff` |
"""

OPERATOR_PROGRESS_PASSED = """\
<!-- eg-operator-build-status -->
## :white_check_mark: EG Operator Build - Passed

**Repo:** rhoai-rhtap/rhods-operator-gap
**Build:** Succeeded | **Pipeline:** Completed
"""

OPERATOR_PROGRESS_FAILED = """\
<!-- eg-operator-build-status -->
## :x: EG Operator Build - Failed

**Repo:** rhoai-rhtap/rhods-operator-gap
"""

QUEUE_COMMENT = """\
<!-- early-gate-queue-url queue_url=/queue/item/209207 correlation_id=eg-4-1792619976 -->
**Early Gate Test - Job Queued**
"""

TRIGGER_EARLY_GATE = "/early-gate"
TRIGGER_EARLY_GATE_BUILD = "/early-gate-build\n"

RESEED_MARKER_NO_VERDICT = """\
<!-- early-gate-build-complete
OPERATOR_IMAGE_REF_BY_DIGEST=
BUNDLE_IMAGE_REF_BY_DIGEST=
FBC_IMAGE_REF_BY_DIGEST=quay.io/rhoai/pull-request-pipelines@sha256:abc
-->

**[re-seed]** Reusing a pre-built FBC digest. Supersedes the tag-based marker.
"""

HEADLINE_ONLY_PASSED = """\
:white_check_mark: **Early Gate passed** — `3.5`

| Field | Value |
|-------|-------|
| **Status** | :white_check_mark: SUCCESS |
"""


def _state(tmp_path: Path, *, overall: str | None, trigger: str = "gap-egtest01") -> Path:
    folder = tmp_path / "GAP Leaders" / f"2026-10-29_{trigger}"
    folder.mkdir(parents=True)
    payload: dict[str, Any] = {
        "pull-requests": [
            {
                "repo": "kserve-branch",
                "pr-url": "https://github.com/example/kserve-branch/pull/1",
                "pr-status": "success",
                "builds": [],
            }
        ]
    }
    if overall is not None:
        payload["overall-status"] = overall
    path = folder / "state.json"
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# comment_looks_like_early_gate_complete
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (PASSED_WITH_DIGESTS, True),
        (PASSED_BARE_MARKER, True),
        (FAILED_BARE_MARKER, True),
        (FAILED_WITH_EMPTY_DIGESTS, True),
        (HEADLINE_ONLY_PASSED, True),
        (OPERATOR_PROGRESS_PASSED, False),
        (OPERATOR_PROGRESS_FAILED, False),
        (QUEUE_COMMENT, False),
        (TRIGGER_EARLY_GATE, False),
        (TRIGGER_EARLY_GATE_BUILD, False),
        ("", False),
        ("random comment", False),
        ("early-gate-build-complete without verdict yet", True),
    ],
)
def test_comment_looks_like_early_gate_complete(body: str, expected: bool) -> None:
    assert eg.comment_looks_like_early_gate_complete(body) is expected


# ---------------------------------------------------------------------------
# parse_early_gate_outcome_from_body
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (PASSED_WITH_DIGESTS, "success"),
        (PASSED_BARE_MARKER, "success"),
        (HEADLINE_ONLY_PASSED, "success"),
        (FAILED_BARE_MARKER, "failure"),
        (FAILED_WITH_EMPTY_DIGESTS, "failure"),
        (OPERATOR_PROGRESS_PASSED, None),
        (OPERATOR_PROGRESS_FAILED, None),
        (QUEUE_COMMENT, None),
        (TRIGGER_EARLY_GATE, None),
        (TRIGGER_EARLY_GATE_BUILD, None),
        (RESEED_MARKER_NO_VERDICT, None),
        ("", None),
        ("early-gate-build-complete alone", None),
    ],
)
def test_parse_early_gate_outcome_from_body(body: str, expected: str | None) -> None:
    assert eg.parse_early_gate_outcome_from_body(body) == expected


def test_parse_does_not_treat_failed_word_in_progress_as_overall() -> None:
    body = "<!-- eg-operator-build-status -->\nEG Operator Build - Failed\n"
    assert eg.parse_early_gate_outcome_from_body(body) is None


# ---------------------------------------------------------------------------
# select_latest_early_gate_outcome
# ---------------------------------------------------------------------------


def test_select_latest_prefers_newest_created_at() -> None:
    comments = [
        {
            "id": 1,
            "created_at": "2026-10-01T00:00:00Z",
            "body": FAILED_BARE_MARKER,
            "user": {"login": "rhods-ci-bot"},
        },
        {
            "id": 2,
            "created_at": "2026-10-29T00:00:00Z",
            "body": PASSED_BARE_MARKER,
            "user": {"login": "rhods-ci-bot"},
        },
    ]
    selected = eg.select_latest_early_gate_outcome(comments)
    assert selected is not None
    assert selected.overall_status == "success"
    assert selected.comment_id == 2


def test_select_latest_ignores_progress_and_triggers() -> None:
    comments = [
        {"id": 1, "created_at": "2026-10-01T00:00:00Z", "body": TRIGGER_EARLY_GATE},
        {"id": 2, "created_at": "2026-10-02T00:00:00Z", "body": OPERATOR_PROGRESS_PASSED},
        {"id": 3, "created_at": "2026-10-03T00:00:00Z", "body": FAILED_WITH_EMPTY_DIGESTS},
    ]
    selected = eg.select_latest_early_gate_outcome(comments)
    assert selected is not None
    assert selected.overall_status == "failure"


def test_select_latest_none_when_only_noise() -> None:
    comments = [
        {"id": 1, "created_at": "2026-10-01T00:00:00Z", "body": QUEUE_COMMENT},
        {"id": 2, "created_at": "2026-10-02T00:00:00Z", "body": RESEED_MARKER_NO_VERDICT},
    ]
    assert eg.select_latest_early_gate_outcome(comments) is None


def test_select_latest_tie_break_by_id() -> None:
    comments = [
        {
            "id": 10,
            "created_at": "2026-10-01T00:00:00Z",
            "body": FAILED_BARE_MARKER,
        },
        {
            "id": 20,
            "created_at": "2026-10-01T00:00:00Z",
            "body": PASSED_BARE_MARKER,
        },
    ]
    selected = eg.select_latest_early_gate_outcome(comments)
    assert selected is not None
    assert selected.overall_status == "success"
    assert selected.comment_id == 20


# ---------------------------------------------------------------------------
# decide_overall_from_comments
# ---------------------------------------------------------------------------


def test_decide_noop_when_already_success() -> None:
    overall, reason = eg.decide_overall_from_comments(
        current_overall="success",
        comments=[{"id": 1, "created_at": "t", "body": FAILED_BARE_MARKER}],
    )
    assert overall is None
    assert "already final" in reason


def test_decide_noop_when_already_failure() -> None:
    overall, reason = eg.decide_overall_from_comments(
        current_overall="failure",
        comments=[{"id": 1, "created_at": "t", "body": PASSED_BARE_MARKER}],
    )
    assert overall is None
    assert "already final" in reason


def test_decide_noop_when_not_early_gate_triggered() -> None:
    overall, reason = eg.decide_overall_from_comments(
        current_overall=None,
        comments=[{"id": 1, "created_at": "t", "body": PASSED_BARE_MARKER}],
    )
    assert overall is None
    assert "early-gate-triggered" in reason


def test_decide_noop_when_overall_missing_pending_children_style() -> None:
    overall, _ = eg.decide_overall_from_comments(
        current_overall="",
        comment_body=PASSED_WITH_DIGESTS,
        comments=[],
    )
    assert overall is None


def test_decide_applies_from_comment_body_when_triggered() -> None:
    overall, reason = eg.decide_overall_from_comments(
        current_overall="early-gate-triggered",
        comments=[],
        comment_body=PASSED_WITH_DIGESTS,
    )
    assert overall == "success"
    assert "triggering comment" in reason


def test_decide_applies_failure_from_comment_body() -> None:
    overall, _ = eg.decide_overall_from_comments(
        current_overall="early-gate-triggered",
        comments=[],
        comment_body=FAILED_BARE_MARKER,
    )
    assert overall == "failure"


def test_decide_ignores_non_terminal_trigger_body() -> None:
    overall, reason = eg.decide_overall_from_comments(
        current_overall="early-gate-triggered",
        comments=[],
        comment_body=TRIGGER_EARLY_GATE_BUILD,
    )
    assert overall is None
    assert "not a terminal" in reason


def test_decide_lists_comments_when_no_body() -> None:
    overall, reason = eg.decide_overall_from_comments(
        current_overall="early-gate-triggered",
        comments=[
            {"id": 1, "created_at": "2026-10-01T00:00:00Z", "body": FAILED_BARE_MARKER},
        ],
        comment_body=None,
    )
    assert overall == "failure"
    assert "latest Early Gate comment" in reason


def test_decide_no_terminal_comment_found() -> None:
    overall, reason = eg.decide_overall_from_comments(
        current_overall="early-gate-triggered",
        comments=[{"id": 1, "created_at": "t", "body": OPERATOR_PROGRESS_PASSED}],
    )
    assert overall is None
    assert "no terminal" in reason


# ---------------------------------------------------------------------------
# list / fetch helpers
# ---------------------------------------------------------------------------


def test_list_leader_pr_comments_parses_json() -> None:
    payload = [
        {"id": 1, "body": "hi", "created_at": "t", "user": {"login": "a"}},
    ]

    def fake_gh(args: list[str]) -> str:  # type: ignore[type-arg]
        assert "api" in args
        assert "comments" in args[-2] or args[-1].endswith("comments") or any(
            "comments" in a for a in args
        )
        return json.dumps(payload)

    got = eg.list_leader_pr_comments(
        "https://github.com/acme/gap/pull/9",
        gh_runner=fake_gh,
    )
    assert len(got) == 1
    assert got[0]["id"] == 1


def test_list_leader_pr_comments_rejects_bad_json() -> None:
    def fake_gh(args: list[str]) -> str:  # type: ignore[type-arg]
        return "not-json"

    with pytest.raises(eg.GapPrMonitorError, match="Invalid JSON"):
        eg.list_leader_pr_comments(
            "https://github.com/acme/gap/pull/9",
            gh_runner=fake_gh,
        )


def test_fetch_leader_labels() -> None:
    def fake_gh(args: list[str]) -> str:  # type: ignore[type-arg]
        return json.dumps(
            {
                "labels": [
                    {"name": "gated-artifacts-promoter"},
                    {"name": "gap-egtest01"},
                ]
            }
        )

    labels = eg.fetch_leader_labels(
        "https://github.com/acme/gap/pull/9",
        gh_runner=fake_gh,
    )
    assert labels == ["gated-artifacts-promoter", "gap-egtest01"]


def test_fetch_leader_head_ref() -> None:
    def fake_gh(args: list[str]) -> str:  # type: ignore[type-arg]
        return json.dumps({"headRefName": "gap-leader/gap-egtest01"})

    assert (
        eg.fetch_leader_head_ref(
            "https://github.com/acme/gap/pull/9",
            gh_runner=fake_gh,
        )
        == "gap-leader/gap-egtest01"
    )


def test_fetch_leader_head_ref_empty_raises() -> None:
    def fake_gh(args: list[str]) -> str:  # type: ignore[type-arg]
        return json.dumps({"headRefName": ""})

    with pytest.raises(eg.GapPrMonitorError, match="empty headRefName"):
        eg.fetch_leader_head_ref(
            "https://github.com/acme/gap/pull/9",
            gh_runner=fake_gh,
        )


# ---------------------------------------------------------------------------
# run_early_gate_results
# ---------------------------------------------------------------------------


def test_run_applies_success_without_publish(tmp_path: Path) -> None:
    _state(tmp_path, overall="early-gate-triggered")
    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        comment_body=PASSED_WITH_DIGESTS,
        publish=False,
    )
    assert result.applied is True
    assert result.overall_status == "success"
    assert result.leader_status_posted is False
    saved = json.loads(
        (tmp_path / "GAP Leaders" / "2026-10-29_gap-egtest01" / "state.json").read_text()
    )
    assert saved["overall-status"] == "success"


def test_run_applies_failure_without_publish(tmp_path: Path) -> None:
    _state(tmp_path, overall="early-gate-triggered")
    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        comment_body=FAILED_BARE_MARKER,
        publish=False,
    )
    assert result.applied is True
    assert result.overall_status == "failure"
    saved = json.loads(
        (tmp_path / "GAP Leaders" / "2026-10-29_gap-egtest01" / "state.json").read_text()
    )
    assert saved["overall-status"] == "failure"


def test_run_noop_when_already_final(tmp_path: Path) -> None:
    _state(tmp_path, overall="success")
    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        comment_body=FAILED_BARE_MARKER,
        publish=False,
    )
    assert result.applied is False
    assert result.overall_status == "success"
    saved = json.loads(
        (tmp_path / "GAP Leaders" / "2026-10-29_gap-egtest01" / "state.json").read_text()
    )
    assert saved["overall-status"] == "success"


def test_run_noop_when_not_triggered(tmp_path: Path) -> None:
    _state(tmp_path, overall=None)
    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        comment_body=PASSED_BARE_MARKER,
        publish=False,
    )
    assert result.applied is False
    assert "overall-status" not in json.loads(
        (tmp_path / "GAP Leaders" / "2026-10-29_gap-egtest01" / "state.json").read_text()
    )


def test_run_dry_run_does_not_write(tmp_path: Path) -> None:
    path = _state(tmp_path, overall="early-gate-triggered")
    before = path.read_text()
    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        comment_body=PASSED_WITH_DIGESTS,
        publish=False,
        dry_run=True,
    )
    assert result.applied is True
    assert result.dry_run is True
    assert result.overall_status == "success"
    assert path.read_text() == before


def test_run_publish_path_greens_leader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _state(tmp_path, overall="early-gate-triggered")
    checkouts: list[str] = []
    published: list[str] = []

    def fake_checkout(repo_root: Path, head_ref: str, **kwargs: Any) -> None:
        checkouts.append(head_ref)

    def fake_publish(*args: Any, **kwargs: Any) -> PublishResult:
        published.append(kwargs.get("commit_message") or args[2] if False else "ok")
        return PublishResult(sha="abc123", committed=True, dry_run=False)

    class FakeUpdater:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def get_pull_head(self, ref: Any) -> Any:
            class H:
                sha = "abc123"

            return H()

        def post_status_for_pr(self, *args: Any, **kwargs: Any) -> StatusUpdateResult:
            return _status_ok()

    monkeypatch.setattr(eg, "checkout_leader_branch", fake_checkout)
    monkeypatch.setattr(eg, "publish_state", fake_publish)
    monkeypatch.setattr(eg, "verify_leader_head", lambda *a, **k: None)
    monkeypatch.setattr(eg, "post_leader_conclusion", lambda *a, **k: _status_ok())

    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        comment_body=PASSED_WITH_DIGESTS,
        publish=True,
        head_ref="gap-leader/gap-egtest01",
        updater_factory=FakeUpdater,
    )
    assert result.applied is True
    assert result.leader_status_posted is True
    assert result.published_sha == "abc123"
    assert checkouts == ["gap-leader/gap-egtest01"]
    assert published


def test_run_requires_leader_url(tmp_path: Path) -> None:
    with pytest.raises(eg.GapPrMonitorError, match="Leader PR URL"):
        eg.run_early_gate_results(
            repo_root=tmp_path,
            leader_pr_url="",
            trigger_id="gap-egtest01",
            comment_body=PASSED_BARE_MARKER,
        )


def test_run_uses_listed_comments_when_body_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state(tmp_path, overall="early-gate-triggered")

    def fake_list(url: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [
            {
                "id": 9,
                "created_at": "2026-10-29T12:00:00Z",
                "body": FAILED_BARE_MARKER,
                "user": {"login": "rhods-ci-bot"},
            }
        ]

    monkeypatch.setattr(eg, "list_leader_pr_comments", fake_list)
    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        comment_body=None,
        publish=False,
    )
    assert result.applied is True
    assert result.overall_status == "failure"


def test_main_dry_run_exit_zero(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _state(tmp_path, overall="early-gate-triggered")
    code = eg.main(
        [
            "--repo-root",
            str(tmp_path),
            "--leader-pr-url",
            "https://github.com/acme/gap/pull/9",
            "--trigger-id",
            "gap-egtest01",
            "--comment-body",
            PASSED_BARE_MARKER,
            "--dry-run",
        ]
    )
    assert code == 0


def test_main_missing_leader_exits_nonzero() -> None:
    # argparse.error raises SystemExit
    with pytest.raises(SystemExit):
        eg.main(["--repo-root", ".", "--trigger-id", "gap-egtest01"])


def test_second_run_idempotent(tmp_path: Path) -> None:
    _state(tmp_path, overall="early-gate-triggered")
    first = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        comment_body=PASSED_BARE_MARKER,
        publish=False,
    )
    second = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        comment_body=FAILED_BARE_MARKER,
        publish=False,
    )
    assert first.applied is True
    assert second.applied is False
    assert second.overall_status == "success"


def test_failure_then_cannot_flip_to_success(tmp_path: Path) -> None:
    _state(tmp_path, overall="early-gate-triggered")
    eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        comment_body=FAILED_BARE_MARKER,
        publish=False,
    )
    again = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        comment_body=PASSED_BARE_MARKER,
        publish=False,
    )
    assert again.applied is False
    assert again.overall_status == "failure"
