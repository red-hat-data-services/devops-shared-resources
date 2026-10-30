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


def _bot_comment(
    body: str,
    *,
    cid: int = 1,
    created: str = "2026-10-29T12:00:00Z",
    login: str = "rhods-ci-bot",
) -> dict[str, Any]:
    return {
        "id": cid,
        "created_at": created,
        "body": body,
        "user": {"login": login},
    }


# ---------------------------------------------------------------------------
# Fixtures — real shapes from GAP Leaders / screenshot
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

# Casual discussion — must NOT conclude (no structured marker).
DISCUSSION_MENTIONS_PASSED = (
    "When Early Gate passed last week, did we retain the logs?"
)

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
# trust helpers
# ---------------------------------------------------------------------------


def test_default_trusted_author_is_rhods_ci_bot() -> None:
    assert eg.trusted_early_gate_authors("") == frozenset({"rhods-ci-bot"})
    assert eg.is_trusted_early_gate_author("rhods-ci-bot") is True
    assert eg.is_trusted_early_gate_author("random-user") is False
    assert eg.is_trusted_early_gate_author(None) is False


def test_trusted_authors_from_csv_override() -> None:
    assert eg.trusted_early_gate_authors("bot-a, bot-b") == frozenset({"bot-a", "bot-b"})


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
        (HEADLINE_ONLY_PASSED, False),
        (DISCUSSION_MENTIONS_PASSED, False),
        (OPERATOR_PROGRESS_PASSED, False),
        (QUEUE_COMMENT, False),
        (TRIGGER_EARLY_GATE, False),
        ("", False),
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
        (FAILED_BARE_MARKER, "failure"),
        (FAILED_WITH_EMPTY_DIGESTS, "failure"),
        (HEADLINE_ONLY_PASSED, None),
        (DISCUSSION_MENTIONS_PASSED, None),
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


# ---------------------------------------------------------------------------
# select_latest_early_gate_outcome — trust + latest
# ---------------------------------------------------------------------------


def test_select_latest_prefers_newest_created_at() -> None:
    comments = [
        _bot_comment(FAILED_BARE_MARKER, cid=1, created="2026-10-01T00:00:00Z"),
        _bot_comment(PASSED_BARE_MARKER, cid=2, created="2026-10-29T00:00:00Z"),
    ]
    selected = eg.select_latest_early_gate_outcome(comments)
    assert selected is not None
    assert selected.overall_status == "success"
    assert selected.comment_id == 2
    assert selected.author == "rhods-ci-bot"


def test_select_ignores_untrusted_author_spoof() -> None:
    comments = [
        _bot_comment(
            PASSED_BARE_MARKER,
            cid=1,
            created="2026-10-29T12:00:00Z",
            login="attacker",
        ),
        _bot_comment(
            FAILED_BARE_MARKER,
            cid=2,
            created="2026-10-29T11:00:00Z",
            login="rhods-ci-bot",
        ),
    ]
    selected = eg.select_latest_early_gate_outcome(comments)
    assert selected is not None
    assert selected.overall_status == "failure"
    assert selected.author == "rhods-ci-bot"


def test_select_ignores_discussion_phrase_from_trusted_author() -> None:
    comments = [
        _bot_comment(DISCUSSION_MENTIONS_PASSED, cid=1),
    ]
    assert eg.select_latest_early_gate_outcome(comments) is None


def test_select_ignores_progress_and_triggers() -> None:
    comments = [
        _bot_comment(TRIGGER_EARLY_GATE, cid=1, created="2026-10-01T00:00:00Z"),
        _bot_comment(OPERATOR_PROGRESS_PASSED, cid=2, created="2026-10-02T00:00:00Z"),
        _bot_comment(FAILED_WITH_EMPTY_DIGESTS, cid=3, created="2026-10-03T00:00:00Z"),
    ]
    selected = eg.select_latest_early_gate_outcome(comments)
    assert selected is not None
    assert selected.overall_status == "failure"


def test_select_latest_none_when_only_noise() -> None:
    comments = [
        _bot_comment(QUEUE_COMMENT, cid=1),
        _bot_comment(RESEED_MARKER_NO_VERDICT, cid=2),
    ]
    assert eg.select_latest_early_gate_outcome(comments) is None


def test_select_latest_tie_break_by_id() -> None:
    comments = [
        _bot_comment(FAILED_BARE_MARKER, cid=10, created="2026-10-01T00:00:00Z"),
        _bot_comment(PASSED_BARE_MARKER, cid=20, created="2026-10-01T00:00:00Z"),
    ]
    selected = eg.select_latest_early_gate_outcome(comments)
    assert selected is not None
    assert selected.overall_status == "success"
    assert selected.comment_id == 20


# ---------------------------------------------------------------------------
# decide_overall_from_comments — reconcile latest (not wake-up body)
# ---------------------------------------------------------------------------


def test_decide_noop_when_already_success() -> None:
    overall, reason = eg.decide_overall_from_comments(
        current_overall="success",
        comments=[_bot_comment(FAILED_BARE_MARKER)],
    )
    assert overall is None
    assert "already final" in reason


def test_decide_noop_when_already_failure() -> None:
    overall, reason = eg.decide_overall_from_comments(
        current_overall="failure",
        comments=[_bot_comment(PASSED_BARE_MARKER)],
    )
    assert overall is None
    assert "already final" in reason


def test_decide_noop_when_not_early_gate_triggered() -> None:
    overall, reason = eg.decide_overall_from_comments(
        current_overall=None,
        comments=[_bot_comment(PASSED_BARE_MARKER)],
    )
    assert overall is None
    assert "early-gate-triggered" in reason


def test_decide_applies_latest_trusted_failure() -> None:
    overall, reason = eg.decide_overall_from_comments(
        current_overall="early-gate-triggered",
        comments=[_bot_comment(FAILED_BARE_MARKER, cid=9)],
    )
    assert overall == "failure"
    assert "latest Early Gate comment" in reason


def test_decide_prefers_newer_failure_over_older_success() -> None:
    """Stale wake-up / older success must not win over a newer failure."""
    comments = [
        _bot_comment(PASSED_BARE_MARKER, cid=1, created="2026-10-01T00:00:00Z"),
        _bot_comment(FAILED_BARE_MARKER, cid=2, created="2026-10-29T00:00:00Z"),
    ]
    overall, _ = eg.decide_overall_from_comments(
        current_overall="early-gate-triggered",
        comments=comments,
    )
    assert overall == "failure"


def test_decide_prefers_newer_success_over_older_failure() -> None:
    comments = [
        _bot_comment(FAILED_BARE_MARKER, cid=1, created="2026-10-01T00:00:00Z"),
        _bot_comment(PASSED_BARE_MARKER, cid=2, created="2026-10-29T00:00:00Z"),
    ]
    overall, _ = eg.decide_overall_from_comments(
        current_overall="early-gate-triggered",
        comments=comments,
    )
    assert overall == "success"


def test_decide_rejects_untrusted_only_comments() -> None:
    overall, reason = eg.decide_overall_from_comments(
        current_overall="early-gate-triggered",
        comments=[
            _bot_comment(PASSED_BARE_MARKER, login="evil-user"),
        ],
    )
    assert overall is None
    assert "no trusted terminal" in reason


# ---------------------------------------------------------------------------
# pagination / list helpers
# ---------------------------------------------------------------------------


def test_flatten_slurp_multi_page() -> None:
    page1 = [_bot_comment(FAILED_BARE_MARKER, cid=1)]
    page2 = [_bot_comment(PASSED_BARE_MARKER, cid=2)]
    flat = eg._flatten_gh_paginate_slurp([page1, page2])
    assert len(flat) == 2
    assert flat[1]["id"] == 2


def test_flatten_single_page_array() -> None:
    page = [_bot_comment(PASSED_BARE_MARKER, cid=1)]
    flat = eg._flatten_gh_paginate_slurp(page)
    assert len(flat) == 1


def test_list_leader_pr_comments_uses_paginate_slurp() -> None:
    page1 = [_bot_comment(FAILED_BARE_MARKER, cid=1, created="2026-10-01T00:00:00Z")]
    page2 = [_bot_comment(PASSED_BARE_MARKER, cid=2, created="2026-10-29T00:00:00Z")]
    seen: list[list[str]] = []

    def fake_gh(args: list[str]) -> str:  # type: ignore[type-arg]
        seen.append(list(args))
        assert "--paginate" in args
        assert "--slurp" in args
        return json.dumps([page1, page2])

    got = eg.list_leader_pr_comments(
        "https://github.com/acme/gap/pull/9",
        gh_runner=fake_gh,
    )
    assert len(got) == 2
    selected = eg.select_latest_early_gate_outcome(got)
    assert selected is not None
    assert selected.overall_status == "success"


def test_list_leader_pr_comments_rejects_bad_json() -> None:
    def fake_gh(args: list[str]) -> str:  # type: ignore[type-arg]
        return "not-json"

    with pytest.raises(eg.GapPrMonitorError, match="Invalid JSON"):
        eg.list_leader_pr_comments(
            "https://github.com/acme/gap/pull/9",
            gh_runner=fake_gh,
        )


def test_list_without_slurp_extra_data_is_avoided_by_slurp_contract() -> None:
    """Document the failure mode review caught: concat pages break json.loads."""
    page1 = json.dumps([_bot_comment(FAILED_BARE_MARKER, cid=1)])
    page2 = json.dumps([_bot_comment(PASSED_BARE_MARKER, cid=2)])
    concatenated = page1 + "\n" + page2
    with pytest.raises(json.JSONDecodeError):
        json.loads(concatenated)
    # --slurp shape parses:
    slurped = json.loads(
        json.dumps(
            [
                [_bot_comment(FAILED_BARE_MARKER, cid=1)],
                [_bot_comment(PASSED_BARE_MARKER, cid=2)],
            ]
        )
    )
    assert len(eg._flatten_gh_paginate_slurp(slurped)) == 2


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


def test_run_applies_success_without_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state(tmp_path, overall="early-gate-triggered")

    def fake_list(url: str, **kwargs: Any) -> list[dict[str, Any]]:
        return [_bot_comment(PASSED_WITH_DIGESTS, cid=5)]

    monkeypatch.setattr(eg, "list_leader_pr_comments", fake_list)
    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        publish=False,
    )
    assert result.applied is True
    assert result.overall_status == "success"
    saved = json.loads(
        (tmp_path / "GAP Leaders" / "2026-10-29_gap-egtest01" / "state.json").read_text()
    )
    assert saved["overall-status"] == "success"


def test_run_applies_failure_without_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state(tmp_path, overall="early-gate-triggered")

    monkeypatch.setattr(
        eg,
        "list_leader_pr_comments",
        lambda *a, **k: [_bot_comment(FAILED_BARE_MARKER)],
    )
    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        publish=False,
    )
    assert result.applied is True
    assert result.overall_status == "failure"


def test_run_ignores_untrusted_spoof(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state(tmp_path, overall="early-gate-triggered")
    monkeypatch.setattr(
        eg,
        "list_leader_pr_comments",
        lambda *a, **k: [
            _bot_comment(PASSED_BARE_MARKER, login="not-the-bot"),
        ],
    )
    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        publish=False,
    )
    assert result.applied is False
    saved = json.loads(
        (tmp_path / "GAP Leaders" / "2026-10-29_gap-egtest01" / "state.json").read_text()
    )
    assert saved["overall-status"] == "early-gate-triggered"


def test_run_reconciles_latest_when_older_success_and_newer_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Simulates delayed/retry wake-up after a newer failure was posted."""
    _state(tmp_path, overall="early-gate-triggered")
    monkeypatch.setattr(
        eg,
        "list_leader_pr_comments",
        lambda *a, **k: [
            _bot_comment(PASSED_BARE_MARKER, cid=1, created="2026-10-01T00:00:00Z"),
            _bot_comment(FAILED_BARE_MARKER, cid=2, created="2026-10-29T00:00:00Z"),
        ],
    )
    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        publish=False,
    )
    assert result.applied is True
    assert result.overall_status == "failure"


def test_run_noop_when_already_final(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state(tmp_path, overall="success")
    monkeypatch.setattr(
        eg,
        "list_leader_pr_comments",
        lambda *a, **k: [_bot_comment(FAILED_BARE_MARKER)],
    )
    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        publish=False,
    )
    assert result.applied is False
    assert result.overall_status == "success"


def test_run_noop_when_not_triggered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state(tmp_path, overall=None)
    monkeypatch.setattr(
        eg,
        "list_leader_pr_comments",
        lambda *a, **k: [_bot_comment(PASSED_BARE_MARKER)],
    )
    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        publish=False,
    )
    assert result.applied is False


def test_run_dry_run_does_not_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _state(tmp_path, overall="early-gate-triggered")
    before = path.read_text()
    monkeypatch.setattr(
        eg,
        "list_leader_pr_comments",
        lambda *a, **k: [_bot_comment(PASSED_WITH_DIGESTS)],
    )
    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        publish=False,
        dry_run=True,
    )
    assert result.applied is True
    assert result.dry_run is True
    assert path.read_text() == before


def test_run_publish_path_greens_leader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state(tmp_path, overall="early-gate-triggered")
    checkouts: list[str] = []

    def fake_checkout(repo_root: Path, head_ref: str, **kwargs: Any) -> None:
        checkouts.append(head_ref)

    def fake_publish(*args: Any, **kwargs: Any) -> PublishResult:
        return PublishResult(sha="abc123", committed=True, dry_run=False)

    class FakeUpdater:
        def __init__(self, **kwargs: Any) -> None:
            pass

    monkeypatch.setattr(
        eg,
        "list_leader_pr_comments",
        lambda *a, **k: [_bot_comment(PASSED_WITH_DIGESTS)],
    )
    monkeypatch.setattr(eg, "checkout_leader_branch", fake_checkout)
    monkeypatch.setattr(eg, "publish_state", fake_publish)
    monkeypatch.setattr(eg, "verify_leader_head", lambda *a, **k: None)
    monkeypatch.setattr(eg, "post_leader_conclusion", lambda *a, **k: _status_ok())

    result = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        publish=True,
        head_ref="gap-leader/gap-egtest01",
        updater_factory=FakeUpdater,
    )
    assert result.applied is True
    assert result.leader_status_posted is True
    assert result.published_sha == "abc123"
    assert checkouts == ["gap-leader/gap-egtest01"]


def test_run_requires_leader_url(tmp_path: Path) -> None:
    with pytest.raises(eg.GapPrMonitorError, match="Leader PR URL"):
        eg.run_early_gate_results(
            repo_root=tmp_path,
            leader_pr_url="",
            trigger_id="gap-egtest01",
        )


def test_main_dry_run_exit_zero(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state(tmp_path, overall="early-gate-triggered")
    monkeypatch.setattr(
        eg,
        "list_leader_pr_comments",
        lambda *a, **k: [_bot_comment(PASSED_BARE_MARKER)],
    )
    code = eg.main(
        [
            "--repo-root",
            str(tmp_path),
            "--leader-pr-url",
            "https://github.com/acme/gap/pull/9",
            "--trigger-id",
            "gap-egtest01",
            "--dry-run",
        ]
    )
    assert code == 0


def test_main_missing_leader_exits_nonzero() -> None:
    with pytest.raises(SystemExit):
        eg.main(["--repo-root", ".", "--trigger-id", "gap-egtest01"])


def test_second_run_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _state(tmp_path, overall="early-gate-triggered")
    monkeypatch.setattr(
        eg,
        "list_leader_pr_comments",
        lambda *a, **k: [_bot_comment(PASSED_BARE_MARKER)],
    )
    first = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        publish=False,
    )
    monkeypatch.setattr(
        eg,
        "list_leader_pr_comments",
        lambda *a, **k: [_bot_comment(FAILED_BARE_MARKER)],
    )
    second = eg.run_early_gate_results(
        repo_root=tmp_path,
        leader_pr_url="https://github.com/acme/gap/pull/9",
        trigger_id="gap-egtest01",
        publish=False,
    )
    assert first.applied is True
    assert second.applied is False
    assert second.overall_status == "success"
