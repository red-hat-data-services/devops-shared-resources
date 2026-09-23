from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from lib.pr_status_updater import GhCommandError, PullRequestRef, StatusUpdateResult
from scripts.gap_pr_monitor import (
    GapPrMonitorError,
    MonitorResult,
    PrMergeInfo,
    apply_pr_statuses,
    apply_success_statuses,
    append_github_output,
    classify_pr,
    extract_pr_urls,
    extract_trigger_id_from_labels,
    fetch_pr_merge_info,
    load_state,
    main,
    resolve_inputs_for_cli,
    resolve_state_file,
    run_stage1_monitor,
    save_state,
    split_labels_csv,
    state_path_for_output,
    state_path_for_trigger,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _sample_state() -> dict[str, Any]:
    return {
        "pull-requests": [
            {
                "repo": "kserve-branch",
                "pr-url": "https://github.com/rhoai-rhtap/kserve-branch/pull/15",
                "pr-status": "new",
                "builds": [],
            },
            {
                "repo": "kubeflow",
                "pr-url": "https://github.com/rhoai-rhtap/kubeflow/pull/85",
                "pr-status": "new",
                "builds": [],
            },
        ]
    }


def _info(
    *,
    url: str = "https://github.com/rhoai-rhtap/kserve-branch/pull/99",
    state: str = "OPEN",
    mergeable: str | None = "MERGEABLE",
    merge_state_status: str | None = "CLEAN",
) -> PrMergeInfo:
    pr = PullRequestRef("rhoai-rhtap", "kserve-branch", 99)
    return PrMergeInfo(
        url=url,
        pr=pr,
        state=state,
        mergeable=mergeable,
        merge_state_status=merge_state_status,
    )


class FakeUpdater:
    """Records post_status_for_pr calls for assertions."""

    def __init__(self, *, check_name: str, dry_run: bool = False) -> None:
        self.check_name = check_name
        self.dry_run = dry_run
        self.posts: list[tuple[str, str, dict]] = []
        self.fail_urls: set[str] = set()

    def post_status_for_pr(self, pr_url: str, status: str, **kwargs):
        if pr_url in self.fail_urls:
            raise RuntimeError(f"boom posting {pr_url}")
        self.posts.append((pr_url, status, kwargs))
        slug, number_s = pr_url.split("github.com/")[1].split("/pull/")
        owner, repo = slug.split("/")
        return StatusUpdateResult(
            pr=PullRequestRef(owner, repo, int(number_s.rstrip("/"))),
            head_sha="abc",
            state="failure" if status == "failure" else "success",
            context=self.check_name,
            dry_run=self.dry_run,
            skipped=False,
        )


def _gh_payloads_by_number(mapping: dict[str, dict]) -> Any:
    def fake_gh(args: list[str]) -> str:
        # Expected: pr view <n> -R owner/repo --json ...
        assert args[0] == "pr"
        assert args[1] == "view"
        number = args[2]
        assert args[3] == "-R"
        assert "--json" in args
        if number not in mapping:
            raise GhCommandError(["gh", *args], 1, f"PR {number} not found")
        return json.dumps(mapping[number])

    return fake_gh


# ---------------------------------------------------------------------------
# state.json load / save / extract
# ---------------------------------------------------------------------------


def test_load_and_extract_pr_urls(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, _sample_state())
    payload = load_state(path)
    assert extract_pr_urls(payload) == [
        "https://github.com/rhoai-rhtap/kserve-branch/pull/15",
        "https://github.com/rhoai-rhtap/kubeflow/pull/85",
    ]


def test_save_state_creates_parent_dirs(tmp_path: Path) -> None:
    path = tmp_path / "gap-leaders" / "gap-abc" / "state.json"
    save_state(path, _sample_state())
    assert path.is_file()


def test_load_state_missing_file(tmp_path: Path) -> None:
    with pytest.raises(GapPrMonitorError, match="not found"):
        load_state(tmp_path / "missing.json")


def test_load_state_rejects_invalid_json(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("{not-json", encoding="utf-8")
    with pytest.raises(GapPrMonitorError, match="Invalid JSON"):
        load_state(path)


def test_load_state_rejects_non_object(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text("[]\n", encoding="utf-8")
    with pytest.raises(GapPrMonitorError, match="object"):
        load_state(path)


def test_load_state_requires_pull_requests_array(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    path.write_text('{"pull-requests": {}}\n', encoding="utf-8")
    with pytest.raises(GapPrMonitorError, match="pull-requests"):
        load_state(path)


def test_extract_pr_urls_requires_pr_url() -> None:
    with pytest.raises(GapPrMonitorError, match="pr-url"):
        extract_pr_urls({"pull-requests": [{"repo": "x", "pr-status": "new"}]})


def test_extract_pr_urls_rejects_non_object_entry() -> None:
    with pytest.raises(GapPrMonitorError, match="must be an object"):
        extract_pr_urls({"pull-requests": ["not-an-object"]})


def test_extract_pr_urls_rejects_blank_pr_url() -> None:
    with pytest.raises(GapPrMonitorError, match="pr-url"):
        extract_pr_urls({"pull-requests": [{"repo": "x", "pr-url": "  "}]})


def test_apply_success_statuses() -> None:
    payload = _sample_state()
    apply_success_statuses(
        payload, ["https://github.com/rhoai-rhtap/kserve-branch/pull/15"]
    )
    assert payload["pull-requests"][0]["pr-status"] == "success"
    assert payload["pull-requests"][1]["pr-status"] == "new"


def test_apply_pr_statuses_mixed() -> None:
    payload = _sample_state()
    apply_pr_statuses(
        payload,
        {
            "https://github.com/rhoai-rhtap/kserve-branch/pull/15": "merge-failure",
            "https://github.com/rhoai-rhtap/kubeflow/pull/85": "success",
        },
    )
    assert payload["pull-requests"][0]["pr-status"] == "merge-failure"
    assert payload["pull-requests"][1]["pr-status"] == "success"


def test_apply_pr_statuses_ignores_unknown_urls() -> None:
    payload = _sample_state()
    apply_pr_statuses(payload, {"https://example.com/pull/1": "success"})
    assert all(e["pr-status"] == "new" for e in payload["pull-requests"])


# ---------------------------------------------------------------------------
# trigger id / state path resolution (RHOAIENG-93564)
# ---------------------------------------------------------------------------


def test_state_path_for_trigger(tmp_path: Path) -> None:
    path = state_path_for_trigger(
        "gap-4e997b5f8c224668b51d2fc8b4677495", root=tmp_path
    )
    assert path == (tmp_path / "gap-leaders" / "gap-4e997b5f8c224668b51d2fc8b4677495" / "state.json")


@pytest.mark.parametrize(
    "bad_id",
    ["", "   ", "not-a-gap-id", "GAP-abc", "gap", "gap-", "foo-gap-1"],
)
def test_state_path_for_trigger_rejects_bad_id(bad_id: str) -> None:
    with pytest.raises(GapPrMonitorError, match="Invalid trigger id"):
        state_path_for_trigger(bad_id)


def test_resolve_state_file_prefers_explicit(tmp_path: Path) -> None:
    explicit = tmp_path / "custom" / "state.json"
    explicit.parent.mkdir()
    explicit.write_text("{}\n", encoding="utf-8")
    resolved = resolve_state_file(state_file=str(explicit), trigger_id="gap-abc")
    assert resolved == explicit.resolve()


def test_resolve_state_file_from_trigger(tmp_path: Path) -> None:
    resolved = resolve_state_file(trigger_id="gap-abc123", root=tmp_path)
    assert resolved == (tmp_path / "gap-leaders" / "gap-abc123" / "state.json").resolve()


def test_resolve_state_file_from_gap_label(tmp_path: Path) -> None:
    resolved = resolve_state_file(
        labels=["gated-artifacts-promoter", "gap-e2e20260923133000", "other"],
        root=tmp_path,
    )
    assert resolved == (
        tmp_path / "gap-leaders" / "gap-e2e20260923133000" / "state.json"
    ).resolve()


def test_resolve_state_file_trigger_id_wins_over_labels(tmp_path: Path) -> None:
    resolved = resolve_state_file(
        trigger_id="gap-from-flag",
        labels=["gap-from-label"],
        root=tmp_path,
    )
    assert resolved == (tmp_path / "gap-leaders" / "gap-from-flag" / "state.json").resolve()


def test_extract_trigger_id_from_labels() -> None:
    assert (
        extract_trigger_id_from_labels(
            ["gated-artifacts-promoter", "gap-abc", "x"]
        )
        == "gap-abc"
    )
    assert extract_trigger_id_from_labels(["gated-artifacts-promoter"]) is None
    assert extract_trigger_id_from_labels([]) is None


def test_split_labels_csv() -> None:
    assert split_labels_csv("gated-artifacts-promoter, gap-abc ,x") == [
        "gated-artifacts-promoter",
        "gap-abc",
        "x",
    ]
    assert split_labels_csv("") == []
    assert split_labels_csv(None) == []


def test_resolve_inputs_for_cli_prefers_cli_over_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GAP_STATE_FILE", "from-env.json")
    monkeypatch.setenv("GAP_TRIGGER_ID", "gap-from-env")
    monkeypatch.setenv("GAP_PR_LABELS", "gap-from-env-label")
    state, trigger, labels = resolve_inputs_for_cli(
        state_file="cli-state.json",
        trigger_id="gap-from-cli",
        labels=["gap-cli-label"],
    )
    assert state == "cli-state.json"
    assert trigger == "gap-from-cli"
    assert labels == ["gap-cli-label"]


def test_resolve_inputs_for_cli_reads_env_when_cli_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GAP_STATE_FILE", "")
    monkeypatch.setenv("GAP_TRIGGER_ID", "gap-from-env")
    monkeypatch.setenv(
        "GAP_PR_LABELS", "gated-artifacts-promoter,gap-from-env-label"
    )
    state, trigger, labels = resolve_inputs_for_cli(
        state_file=None,
        trigger_id=None,
        labels=None,
    )
    assert state is None
    assert trigger == "gap-from-env"
    assert labels == ["gated-artifacts-promoter", "gap-from-env-label"]


def test_append_github_output_and_relative_path(tmp_path: Path) -> None:
    out = tmp_path / "github_output"
    root = tmp_path / "repo"
    state = root / "gap-leaders" / "gap-x" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text("{}\n", encoding="utf-8")
    append_github_output(
        "state_path",
        state_path_for_output(state, repo_root=root),
        output_file=str(out),
    )
    assert out.read_text(encoding="utf-8") == "state_path=gap-leaders/gap-x/state.json\n"


def test_main_writes_github_output_from_ci_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "leader"
    state = root / "gap-leaders" / "gap-cienv" / "state.json"
    state.parent.mkdir(parents=True)
    state.write_text(json.dumps(_sample_state()), encoding="utf-8")
    out = tmp_path / "out"
    monkeypatch.setenv("GAP_TRIGGER_ID", "gap-cienv")
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.chdir(root)

    def fake_run(path: Path, **kwargs: Any) -> MonitorResult:
        return MonitorResult(
            state_path=path,
            updated_urls=[],
            skipped_urls=[],
            merge_failure_urls=[],
            success_urls=[],
            dry_run=True,
        )

    monkeypatch.setattr(
        "scripts.gap_pr_monitor.run_stage1_monitor", fake_run
    )
    assert main(["--repo-root", str(root), "--dry-run"]) == 0
    assert "state_path=gap-leaders/gap-cienv/state.json" in out.read_text(
        encoding="utf-8"
    )


def test_resolve_state_file_gap_dir_fallback_exactly_one(tmp_path: Path) -> None:
    only = tmp_path / "gap-leaders" / "gap-onlyone" / "state.json"
    only.parent.mkdir(parents=True)
    only.write_text("{}\n", encoding="utf-8")
    # distractor at repo root must NOT be picked
    other = tmp_path / "gap-distractor" / "state.json"
    other.parent.mkdir()
    other.write_text("{}\n", encoding="utf-8")

    resolved = resolve_state_file(root=tmp_path, allow_gap_dir_fallback=True)
    assert resolved == only.resolve()


def test_resolve_state_file_gap_dir_fallback_rejects_many(tmp_path: Path) -> None:
    for name in ("gap-one", "gap-two"):
        p = tmp_path / "gap-leaders" / name / "state.json"
        p.parent.mkdir(parents=True)
        p.write_text("{}\n", encoding="utf-8")
    with pytest.raises(GapPrMonitorError, match="Multiple gap-leaders"):
        resolve_state_file(root=tmp_path, allow_gap_dir_fallback=True)


def test_resolve_state_file_requires_concrete_source() -> None:
    with pytest.raises(GapPrMonitorError, match="Cannot locate state.json"):
        resolve_state_file()


def test_resolve_state_file_no_loose_find_without_fallback(tmp_path: Path) -> None:
    """Without gap-* label/trigger, random state.json must NOT be used."""
    loose = tmp_path / "random" / "state.json"
    loose.parent.mkdir()
    loose.write_text("{}\n", encoding="utf-8")
    with pytest.raises(GapPrMonitorError, match="Cannot locate state.json"):
        resolve_state_file(root=tmp_path, allow_gap_dir_fallback=False)


# ---------------------------------------------------------------------------
# fetch_pr_merge_info — uses gh pr view --json (mocked)
# ---------------------------------------------------------------------------


def test_fetch_pr_merge_info_builds_gh_command_and_parses() -> None:
    seen: list[list[str]] = []

    def fake_gh(args: list[str]) -> str:
        seen.append(list(args))
        return json.dumps(
            {
                "state": "OPEN",
                "mergeable": "CONFLICTING",
                "mergeStateStatus": "DIRTY",
                "url": "https://github.com/rhoai-rhtap/kserve-branch/pull/20",
            }
        )

    info = fetch_pr_merge_info(
        "https://github.com/rhoai-rhtap/kserve-branch/pull/20",
        gh_runner=fake_gh,
    )
    assert seen[0] == [
        "pr",
        "view",
        "20",
        "-R",
        "rhoai-rhtap/kserve-branch",
        "--json",
        "state,mergeable,mergeStateStatus,url",
    ]
    assert info.mergeable == "CONFLICTING"
    assert info.merge_state_status == "DIRTY"
    assert info.state == "OPEN"
    assert info.pr.number == 20


def test_fetch_pr_merge_info_handles_null_mergeable() -> None:
    def fake_gh(args: list[str]) -> str:
        return json.dumps(
            {
                "state": "open",
                "mergeable": None,
                "mergeStateStatus": None,
                "url": "https://github.com/rhoai-rhtap/kserve-branch/pull/20",
            }
        )

    info = fetch_pr_merge_info(
        "https://github.com/rhoai-rhtap/kserve-branch/pull/20",
        gh_runner=fake_gh,
    )
    assert info.mergeable is None
    assert info.merge_state_status is None
    assert info.state == "OPEN"


def test_fetch_pr_merge_info_invalid_json() -> None:
    def fake_gh(args: list[str]) -> str:
        return "not-json{"

    with pytest.raises(GapPrMonitorError, match="Invalid JSON"):
        fetch_pr_merge_info(
            "https://github.com/rhoai-rhtap/kserve-branch/pull/20",
            gh_runner=fake_gh,
        )


def test_fetch_pr_merge_info_invalid_url() -> None:
    with pytest.raises(ValueError):
        fetch_pr_merge_info("not-a-url", gh_runner=lambda a: "{}")


# ---------------------------------------------------------------------------
# classify_pr — conflict vs success vs merged
# ---------------------------------------------------------------------------


def test_classify_conflicting_mergeable() -> None:
    decision = classify_pr(
        _info(mergeable="CONFLICTING", merge_state_status="DIRTY"),
        retry_unknown=False,
    )
    assert decision.pr_status == "merge-failure"
    assert decision.cli_status == "failure"
    assert "merge conflict" in decision.description


def test_classify_dirty_merge_state_alone() -> None:
    """DIRTY in mergeStateStatus is enough even if mergeable is odd."""
    decision = classify_pr(
        _info(mergeable="MERGEABLE", merge_state_status="DIRTY"),
        retry_unknown=False,
    )
    assert decision.pr_status == "merge-failure"


def test_classify_mergeable_success() -> None:
    decision = classify_pr(
        _info(mergeable="MERGEABLE", merge_state_status="CLEAN"),
        retry_unknown=False,
    )
    assert decision.pr_status == "success"
    assert decision.cli_status == "completed"
    assert decision.skip_status_post is False


@pytest.mark.parametrize("merge_state", ["CLEAN", "UNSTABLE", "BEHIND", "BLOCKED", "HAS_HOOKS"])
def test_classify_non_conflict_merge_states_are_success(merge_state: str) -> None:
    decision = classify_pr(
        _info(mergeable="MERGEABLE", merge_state_status=merge_state),
        retry_unknown=False,
    )
    assert decision.pr_status == "success"


def test_classify_already_merged_skips_post() -> None:
    decision = classify_pr(
        _info(state="MERGED", mergeable=None, merge_state_status=None),
        retry_unknown=False,
    )
    assert decision.pr_status == "success"
    assert decision.skip_status_post is True


def test_classify_unknown_retries_then_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sleeps: list[float] = []
    monkeypatch.setattr(
        "scripts.gap_pr_monitor.time.sleep", lambda s: sleeps.append(s)
    )
    calls = {"n": 0}

    def fake_gh(args: list[str]) -> str:
        calls["n"] += 1
        return json.dumps(
            {
                "state": "OPEN",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "url": "https://github.com/rhoai-rhtap/kserve-branch/pull/99",
            }
        )

    decision = classify_pr(
        _info(mergeable="UNKNOWN", merge_state_status=None),
        gh_runner=fake_gh,
        retry_unknown=True,
    )
    assert sleeps == [1.5]
    assert calls["n"] == 1
    assert decision.pr_status == "success"


def test_classify_unknown_retries_then_conflict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("scripts.gap_pr_monitor.time.sleep", lambda s: None)

    def fake_gh(args: list[str]) -> str:
        return json.dumps(
            {
                "state": "OPEN",
                "mergeable": "CONFLICTING",
                "mergeStateStatus": "DIRTY",
                "url": "https://github.com/rhoai-rhtap/kserve-branch/pull/99",
            }
        )

    decision = classify_pr(
        _info(mergeable=None, merge_state_status=None),
        gh_runner=fake_gh,
        retry_unknown=True,
    )
    assert decision.pr_status == "merge-failure"


# ---------------------------------------------------------------------------
# run_stage1_monitor end-to-end (mocked gh + updater)
# ---------------------------------------------------------------------------


def test_run_stage1_monitor_posts_success_and_merge_failure(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, _sample_state())
    updater_holder: dict[str, FakeUpdater] = {}

    def factory(*, check_name: str, dry_run: bool = False) -> FakeUpdater:
        updater_holder["u"] = FakeUpdater(check_name=check_name, dry_run=dry_run)
        return updater_holder["u"]

    fake_gh = _gh_payloads_by_number(
        {
            "15": {
                "state": "OPEN",
                "mergeable": "CONFLICTING",
                "mergeStateStatus": "DIRTY",
                "url": "https://github.com/rhoai-rhtap/kserve-branch/pull/15",
            },
            "85": {
                "state": "OPEN",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "url": "https://github.com/rhoai-rhtap/kubeflow/pull/85",
            },
        }
    )

    result = run_stage1_monitor(path, updater_factory=factory, gh_runner=fake_gh)
    posts = updater_holder["u"].posts
    assert set(result.merge_failure_urls) == {
        "https://github.com/rhoai-rhtap/kserve-branch/pull/15"
    }
    assert set(result.success_urls) == {
        "https://github.com/rhoai-rhtap/kubeflow/pull/85"
    }
    assert ("https://github.com/rhoai-rhtap/kserve-branch/pull/15", "failure") in [
        (u, s) for u, s, _ in posts
    ]
    assert ("https://github.com/rhoai-rhtap/kubeflow/pull/85", "completed") in [
        (u, s) for u, s, _ in posts
    ]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["pull-requests"][0]["pr-status"] == "merge-failure"
    assert saved["pull-requests"][1]["pr-status"] == "success"


def test_run_stage1_monitor_skips_status_for_merged(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(
        path,
        {
            "pull-requests": [
                {
                    "repo": "kserve-branch",
                    "pr-url": "https://github.com/rhoai-rhtap/kserve-branch/pull/15",
                    "pr-status": "new",
                    "builds": [],
                }
            ]
        },
    )
    holder: dict[str, FakeUpdater] = {}

    def factory(*, check_name: str, dry_run: bool = False) -> FakeUpdater:
        holder["u"] = FakeUpdater(check_name=check_name, dry_run=dry_run)
        return holder["u"]

    fake_gh = _gh_payloads_by_number(
        {
            "15": {
                "state": "MERGED",
                "mergeable": None,
                "mergeStateStatus": None,
                "url": "https://github.com/rhoai-rhtap/kserve-branch/pull/15",
            }
        }
    )
    result = run_stage1_monitor(path, updater_factory=factory, gh_runner=fake_gh)
    assert holder["u"].posts == []
    assert result.skipped_urls == [
        "https://github.com/rhoai-rhtap/kserve-branch/pull/15"
    ]
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["pull-requests"][0]["pr-status"] == "success"


def test_run_stage1_monitor_dry_run_does_not_write(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, _sample_state())

    fake_gh = _gh_payloads_by_number(
        {
            "15": {
                "state": "OPEN",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "url": "https://github.com/rhoai-rhtap/kserve-branch/pull/15",
            },
            "85": {
                "state": "OPEN",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "url": "https://github.com/rhoai-rhtap/kubeflow/pull/85",
            },
        }
    )
    run_stage1_monitor(
        path, dry_run=True, updater_factory=FakeUpdater, gh_runner=fake_gh
    )
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert all(e["pr-status"] == "new" for e in saved["pull-requests"])


def test_run_stage1_monitor_empty_prs_raises(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, {"pull-requests": []})
    with pytest.raises(GapPrMonitorError, match="No pull requests"):
        run_stage1_monitor(
            path,
            updater_factory=FakeUpdater,
            gh_runner=lambda a: "{}",
        )


def test_run_stage1_monitor_inspect_error_without_continue(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, _sample_state())

    def fake_gh(args: list[str]) -> str:
        raise GhCommandError(["gh", *args], 1, "api fail")

    with pytest.raises(GapPrMonitorError, match="Failed to inspect"):
        run_stage1_monitor(
            path, updater_factory=FakeUpdater, gh_runner=fake_gh
        )


def test_run_stage1_monitor_continue_on_error_partial(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(path, _sample_state())
    holder: dict[str, FakeUpdater] = {}

    def factory(*, check_name: str, dry_run: bool = False) -> FakeUpdater:
        holder["u"] = FakeUpdater(check_name=check_name, dry_run=dry_run)
        return holder["u"]

    def fake_gh(args: list[str]) -> str:
        number = args[2]
        if number == "15":
            raise GhCommandError(["gh", *args], 1, "gone")
        return json.dumps(
            {
                "state": "OPEN",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "url": "https://github.com/rhoai-rhtap/kubeflow/pull/85",
            }
        )

    with pytest.raises(GapPrMonitorError, match="failed during monitor"):
        run_stage1_monitor(
            path,
            continue_on_error=True,
            updater_factory=factory,
            gh_runner=fake_gh,
        )
    saved = json.loads(path.read_text(encoding="utf-8"))
    # Second PR still classified/persisted when continue_on_error
    assert saved["pull-requests"][1]["pr-status"] == "success"


def test_run_stage1_monitor_post_error_hard_fail(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(
        path,
        {
            "pull-requests": [
                {
                    "repo": "kserve-branch",
                    "pr-url": "https://github.com/rhoai-rhtap/kserve-branch/pull/15",
                    "pr-status": "new",
                    "builds": [],
                }
            ]
        },
    )

    class BoomUpdater(FakeUpdater):
        def post_status_for_pr(self, pr_url, status, **kwargs):
            raise RuntimeError("status API 403")

    fake_gh = _gh_payloads_by_number(
        {
            "15": {
                "state": "OPEN",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "url": "https://github.com/rhoai-rhtap/kserve-branch/pull/15",
            }
        }
    )
    with pytest.raises(GapPrMonitorError, match="Failed to post status"):
        run_stage1_monitor(path, updater_factory=BoomUpdater, gh_runner=fake_gh)


def test_run_stage1_monitor_description_override(tmp_path: Path) -> None:
    path = tmp_path / "state.json"
    save_state(
        path,
        {
            "pull-requests": [
                {
                    "repo": "kubeflow",
                    "pr-url": "https://github.com/rhoai-rhtap/kubeflow/pull/85",
                    "pr-status": "new",
                    "builds": [],
                }
            ]
        },
    )
    holder: dict[str, FakeUpdater] = {}

    def factory(*, check_name: str, dry_run: bool = False) -> FakeUpdater:
        holder["u"] = FakeUpdater(check_name=check_name, dry_run=dry_run)
        return holder["u"]

    fake_gh = _gh_payloads_by_number(
        {
            "85": {
                "state": "OPEN",
                "mergeable": "MERGEABLE",
                "mergeStateStatus": "CLEAN",
                "url": "https://github.com/rhoai-rhtap/kubeflow/pull/85",
            }
        }
    )
    run_stage1_monitor(
        path,
        description="custom desc",
        updater_factory=factory,
        gh_runner=fake_gh,
    )
    assert holder["u"].posts[0][2].get("description") == "custom desc"


# ---------------------------------------------------------------------------
# CLI main()
# ---------------------------------------------------------------------------


def test_main_requires_state_or_trigger() -> None:
    assert main([]) == 1


def test_main_success(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "state.json"
    save_state(path, _sample_state())

    def fake_run(state_path, **kwargs):
        return MonitorResult(
            state_path=Path(state_path),
            updated_urls=["https://github.com/rhoai-rhtap/kserve-branch/pull/15"],
            skipped_urls=[],
            merge_failure_urls=[
                "https://github.com/rhoai-rhtap/kserve-branch/pull/15"
            ],
            success_urls=["https://github.com/rhoai-rhtap/kubeflow/pull/85"],
            dry_run=False,
        )

    monkeypatch.setattr("scripts.gap_pr_monitor.run_stage1_monitor", fake_run)
    code = main(["--state-file", str(path)])
    assert code == 0
    out = capsys.readouterr().out
    assert "merge-failure:" in out
    assert "success:" in out
    assert "Posted status" in out


def test_main_with_trigger_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, Path] = {}

    def fake_run(state_path, **kwargs):
        seen["path"] = Path(state_path)
        return MonitorResult(
            state_path=Path(state_path),
            updated_urls=[],
            skipped_urls=[],
            merge_failure_urls=[],
            success_urls=[],
            dry_run=False,
        )

    monkeypatch.setattr("scripts.gap_pr_monitor.run_stage1_monitor", fake_run)
    code = main(["--trigger-id", "gap-abc123", "--repo-root", str(tmp_path)])
    assert code == 0
    assert seen["path"] == (tmp_path / "gap-leaders" / "gap-abc123" / "state.json").resolve()


def test_main_reports_monitor_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    path = tmp_path / "state.json"
    save_state(path, _sample_state())

    def fake_run(state_path, **kwargs):
        raise GapPrMonitorError("nope")

    monkeypatch.setattr("scripts.gap_pr_monitor.run_stage1_monitor", fake_run)
    assert main(["--state-file", str(path)]) == 1
    assert "ERROR: nope" in capsys.readouterr().err
