from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
import yaml

from lib.main_release_feasibility import (
    GLOBAL_IGNORE_FILES,
    FeasibilityError,
    ReleasePolicy,
    check_releases,
    load_release_policy,
)
from tests.conftest import run


@pytest.fixture
def policy() -> ReleasePolicy:
    return ReleasePolicy(
        repository="red-hat-data-services/component",
        component="component",
        enabled=True,
        releases=("rhoai-3.6",),
        ignore_files=GLOBAL_IGNORE_FILES,
    )


@pytest.fixture
def config_files(tmp_path: Path) -> tuple[Path, Path]:
    source_map = tmp_path / "source-map.yaml"
    releases = tmp_path / "releases.yaml"
    source_map.write_text(yaml.safe_dump({"git": [{
        "name": "component",
        "repo-url": "https://github.com/red-hat-data-services/component.git",
        "automerge": "yes",
    }]}))
    releases.write_text(yaml.safe_dump({"releases": ["rhoai-3.6"]}))
    return source_map, releases


def _write_config(source_map: Path, **fields) -> None:
    payload = yaml.safe_load(source_map.read_text())
    payload["git"][0].update(fields)
    source_map.write_text(yaml.safe_dump(payload))


def _commit(repo: Path, path: str, content: str | bytes | None) -> None:
    file = repo / path
    if content is None:
        run(["git", "rm", "--", path], cwd=repo)
    else:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(content.encode() if isinstance(content, str) else content)
        run(["git", "add", "--", path], cwd=repo)
    run(["git", "commit", "-m", "Update test file"], cwd=repo)


def _sha(repo: Path, ref: str = "HEAD") -> str:
    return run(["git", "rev-parse", ref], cwd=repo).stdout.strip()


def test_config_matching_exclusions_and_releases(config_files) -> None:
    source_map, releases = config_files
    _write_config(
        source_map,
        **{
            "repo-url": "https://github.com/red-hat-data-services/MLServer.git",
            "name": "mlserver",
            "ignore-files": ".tekton/*, build/**, release/**, .tekton/*",
        },
    )
    releases.write_text(yaml.safe_dump({"releases": [
        "rhoai-2.16", "rhoai-3.6", "rhoai-3.7-ea.1", "rhoai-3.6",
    ]}))

    policy = load_release_policy(source_map, releases, repository="RED-HAT-DATA-SERVICES/mlserver")

    assert policy.component == "mlserver"
    assert policy.enabled
    assert policy.releases == ("rhoai-3.6", "rhoai-3.7-ea.1")
    assert policy.ignore_files == (".tekton/*", "build/**", "release/**", *GLOBAL_IGNORE_FILES)


@pytest.mark.parametrize("enabled", ["yes", "no"])
@pytest.mark.parametrize("quoted", [True, False])
def test_config_accepts_quoted_and_unquoted_yes_no(config_files, enabled, quoted) -> None:
    source_map, releases = config_files
    _write_config(source_map, automerge=enabled, **{"manual-sync": "yes"})
    if not quoted:
        source_map.write_text(source_map.read_text().replace(f"'{enabled}'", enabled))
    policy = load_release_policy(source_map, releases, repository="red-hat-data-services/component")
    assert policy.enabled == (enabled == "yes")
    assert bool(policy.skip_reason) == (enabled == "no")


def test_deduplicated_exclusions_and_empty_releases(config_files) -> None:
    source_map, releases = config_files
    _write_config(source_map, **{"ignore-files": ".tekton/**, .github/renovate.json"})
    releases.write_text("releases: []\n")
    policy = load_release_policy(source_map, releases, repository="red-hat-data-services/component")
    assert policy.ignore_files == (".tekton/**", *GLOBAL_IGNORE_FILES)
    assert policy.skip_reason == "No active release branches (rhoai-2.* is excluded)."


@pytest.mark.parametrize("mutation,match", [
    ({"git": []}, "found 0"),
    ({"git": None}, "'git' list"),
    ({"git": [None]}, "must be a mapping"),
    ({"git": [{"repo-url": "https://example.com/org/repo.git"}]}, "Invalid source-map"),
])
def test_invalid_or_missing_source_mapping_fails(config_files, mutation, match) -> None:
    source_map, releases = config_files
    source_map.write_text(yaml.safe_dump(mutation))
    with pytest.raises(FeasibilityError, match=match):
        load_release_policy(source_map, releases, repository="red-hat-data-services/component")


def test_duplicate_mapping_fails(config_files) -> None:
    source_map, releases = config_files
    payload = yaml.safe_load(source_map.read_text())
    payload["git"].append(dict(payload["git"][0]))
    source_map.write_text(yaml.safe_dump(payload))
    with pytest.raises(FeasibilityError, match="found 2"):
        load_release_policy(source_map, releases, repository="red-hat-data-services/component")


@pytest.mark.parametrize("fields", [
    {"automerge": "maybe"}, {"automerge": 1}, {"automerge": None},
    {"automerge": True}, {"automerge": False},
    {"ignore-files": 42}, {"ignore-files": [42]}, {"ignore-files": [".tekton/*"]},
    {"ignore-files": "first,\nsecond"}, {"ignore-files": "first,\tsecond"},
])
def test_invalid_mapping_fields_fail(config_files, fields) -> None:
    source_map, releases = config_files
    _write_config(source_map, **fields)
    with pytest.raises(FeasibilityError):
        load_release_policy(source_map, releases, repository="red-hat-data-services/component")


@pytest.mark.parametrize("payload", [
    {"releases": None}, {"releases": "rhoai-3.6"}, {"releases": [None]},
    {"releases": [""]}, {"releases": ["rhoai-3.6 "]}, {"releases": ["../bad"]},
])
def test_invalid_release_configuration_fails(config_files, payload) -> None:
    source_map, releases = config_files
    releases.write_text(yaml.safe_dump(payload))
    with pytest.raises(FeasibilityError):
        load_release_policy(source_map, releases, repository="red-hat-data-services/component")


@pytest.mark.parametrize("content", ["[", "[]", "null"])
def test_invalid_yaml_fails(config_files, content) -> None:
    source_map, releases = config_files
    source_map.write_text(content)
    with pytest.raises(FeasibilityError):
        load_release_policy(source_map, releases, repository="red-hat-data-services/component")


@pytest.mark.parametrize("layout", ["fast-forward", "diverged", "already-contained"])
def test_clean_merges(git_repo_factory, policy, layout) -> None:
    repo = git_repo_factory("clean")
    _commit(repo, "README.md", "base\n")
    run(["git", "branch", "rhoai-3.6"], cwd=repo)
    _commit(repo, "candidate.txt", "new code\n")
    candidate = _sha(repo)
    if layout == "diverged":
        run(["git", "checkout", "rhoai-3.6"], cwd=repo)
        _commit(repo, "release.txt", "release configuration\n")
    elif layout == "already-contained":
        run(["git", "branch", "-f", "rhoai-3.6", "main"], cwd=repo)

    source_sha, results = check_releases(repo, source_ref=candidate, policy=policy)

    assert source_sha == candidate
    assert results[0].status == "passed"
    assert results[0].target_sha == _sha(repo, "rhoai-3.6")
    assert results[0].conflict_files == ()


def test_checks_all_releases_and_preserves_original_checkout(git_repo_factory, policy) -> None:
    repo = git_repo_factory("conflicts")
    _commit(repo, "README.md", "base\n")
    run(["git", "branch", "rhoai-3.6"], cwd=repo)
    run(["git", "branch", "rhoai-3.7"], cwd=repo)
    _commit(repo, "README.md", "candidate\n")
    candidate = _sha(repo)
    run(["git", "checkout", "rhoai-3.6"], cwd=repo)
    _commit(repo, "README.md", "release\n")
    (repo / "README.md").write_text("uncommitted user changes\n")
    (repo / "untracked.txt").write_text("user data\n")
    attributes = repo / ".git/info/attributes"
    attributes.parent.mkdir(parents=True, exist_ok=True)
    attributes.write_text("README.md diff=text\n")
    before_status = run(["git", "status", "--porcelain"], cwd=repo).stdout
    before_refs = run(["git", "show-ref"], cwd=repo).stdout
    before_config = (repo / ".git/config").read_text()
    before_count = run(["git", "rev-list", "--all", "--count"], cwd=repo).stdout

    _, results = check_releases(
        repo, source_ref=candidate,
        policy=replace(policy, releases=("rhoai-3.6", "rhoai-3.7")),
    )

    assert [(result.branch, result.status) for result in results] == [
        ("rhoai-3.6", "conflicts"), ("rhoai-3.7", "passed"),
    ]
    assert results[0].conflict_files == ("README.md",)
    assert run(["git", "status", "--porcelain"], cwd=repo).stdout == before_status
    assert run(["git", "show-ref"], cwd=repo).stdout == before_refs
    assert run(["git", "rev-list", "--all", "--count"], cwd=repo).stdout == before_count
    assert (repo / ".git/config").read_text() == before_config
    assert attributes.read_text() == "README.md diff=text\n"
    assert (repo / "README.md").read_text() == "uncommitted user changes\n"
    assert (repo / "untracked.txt").read_text() == "user data\n"


@pytest.mark.parametrize("kind", ["content", "add-add", "modify-delete", "delete-modify", "binary"])
@pytest.mark.parametrize("path,pattern", [
    (".tekton/task.yaml", ".tekton/*"),
    (".tekton/nested/task.yaml", ".tekton/*"),
    (".github/renovate.json", ".github/renovate.json"),
    (".tekton/README.md", ".tekton/README.md"),
])
def test_excluded_conflicts_are_feasible(git_repo_factory, policy, kind, path, pattern) -> None:
    repo = git_repo_factory("ignored")
    _commit(repo, "README.md", "base\n")
    if kind != "add-add":
        _commit(repo, path, b"base\0\n" if kind == "binary" else "base\n")
    run(["git", "branch", "rhoai-3.6"], cwd=repo)
    source = None if kind == "delete-modify" else (
        b"source\0\n" if kind == "binary" else "source\n"
    )
    _commit(repo, path, source)
    candidate = _sha(repo)
    run(["git", "checkout", "rhoai-3.6"], cwd=repo)
    target = None if kind == "modify-delete" else (
        b"release\0\n" if kind == "binary" else "release\n"
    )
    _commit(repo, path, target)

    _, results = check_releases(
        repo, source_ref=candidate,
        policy=replace(policy, ignore_files=(pattern, *GLOBAL_IGNORE_FILES)),
    )

    assert results[0].status == "passed", results[0].message
    assert results[0].conflict_files == ()
    if kind in ("modify-delete", "delete-modify"):
        assert results[0].ignored_conflicts == (path,)


def test_mixed_ignored_and_blocking_conflicts(git_repo_factory, policy) -> None:
    repo = git_repo_factory("mixed")
    _commit(repo, "README.md", "base\n")
    _commit(repo, ".tekton/task.yaml", "base\n")
    run(["git", "branch", "rhoai-3.6"], cwd=repo)
    _commit(repo, "README.md", "candidate\n")
    _commit(repo, ".tekton/task.yaml", "candidate\n")
    candidate = _sha(repo)
    run(["git", "checkout", "rhoai-3.6"], cwd=repo)
    _commit(repo, "README.md", "release\n")
    _commit(repo, ".tekton/task.yaml", None)

    _, results = check_releases(
        repo, source_ref=candidate, policy=replace(policy, ignore_files=(".tekton/*",))
    )

    assert results[0].status == "conflicts"
    assert results[0].conflict_files == ("README.md",)
    assert results[0].ignored_conflicts == (".tekton/task.yaml",)


def test_exclusions_are_case_sensitive(git_repo_factory, policy) -> None:
    repo = git_repo_factory("case-sensitive")
    _commit(repo, "Build.yaml", "base\n")
    run(["git", "branch", "rhoai-3.6"], cwd=repo)
    _commit(repo, "Build.yaml", "candidate\n")
    candidate = _sha(repo)
    run(["git", "checkout", "rhoai-3.6"], cwd=repo)
    _commit(repo, "Build.yaml", "release\n")
    _, results = check_releases(
        repo, source_ref=candidate, policy=replace(policy, ignore_files=("build.yaml",))
    )
    assert results[0].status == "conflicts"
    assert results[0].conflict_files == ("Build.yaml",)


@pytest.mark.parametrize("path,pattern", [
    ("[build].yaml", r"\[build\].yaml"),
    ("b.yaml", "[[:alpha:]].yaml"),
    ("b.yaml", "[^a].yaml"),
    ("b.yaml", "@(a|b).yaml"),
])
@pytest.mark.parametrize("downstream_deleted", [True, False])
def test_bash_exclusions_resolve_structural_conflicts(
    git_repo_factory, policy, path, pattern, downstream_deleted
) -> None:
    repo = git_repo_factory("bash-globs")
    _commit(repo, path, "base\n")
    run(["git", "branch", "rhoai-3.6"], cwd=repo)
    _commit(repo, path, "candidate\n" if downstream_deleted else None)
    candidate = _sha(repo)
    run(["git", "checkout", "rhoai-3.6"], cwd=repo)
    _commit(repo, path, None if downstream_deleted else "release\n")

    _, results = check_releases(
        repo, source_ref=candidate, policy=replace(policy, ignore_files=(pattern,))
    )

    assert results[0].status == "passed", results[0].message
    assert results[0].ignored_conflicts == (path,)


def test_config_loader_does_not_change_pyyaml_global_behavior(config_files) -> None:
    source_map, releases = config_files
    load_release_policy(source_map, releases, repository="red-hat-data-services/component")
    assert yaml.safe_load("value: yes\n") == {"value": True}


@pytest.mark.parametrize("source_renames", [True, False])
@pytest.mark.parametrize("destination,expected", [
    (".tekton/renamed.yaml", "passed"), ("src/task.yaml", "conflicts"),
])
def test_rename_delete_conflicts_check_the_resulting_path(
    git_repo_factory, policy, source_renames, destination, expected
) -> None:
    repo = git_repo_factory("rename-delete")
    _commit(repo, "README.md", "base\n")
    original = ".tekton/task.yaml"
    _commit(repo, original, "base\n")
    run(["git", "branch", "rhoai-3.6"], cwd=repo)

    def rename() -> None:
        (repo / destination).parent.mkdir(parents=True, exist_ok=True)
        run(["git", "mv", "--", original, destination], cwd=repo)
        run(["git", "commit", "-m", "Rename task"], cwd=repo)

    if source_renames:
        rename()
    else:
        _commit(repo, original, None)
    candidate = _sha(repo)
    run(["git", "checkout", "rhoai-3.6"], cwd=repo)
    if source_renames:
        _commit(repo, original, None)
    else:
        rename()

    _, results = check_releases(
        repo, source_ref=candidate, policy=replace(policy, ignore_files=(".tekton/*",))
    )

    assert results[0].status == expected, results[0].message
    if expected == "conflicts":
        assert results[0].conflict_files == (destination,)
    else:
        assert results[0].ignored_conflicts == (destination,)


def test_candidate_includes_existing_stable_contents(git_repo_factory, policy) -> None:
    repo = git_repo_factory("candidate")
    _commit(repo, "README.md", "base\n")
    run(["git", "branch", "rhoai-3.6"], cwd=repo)
    run(["git", "checkout", "-b", "stable"], cwd=repo)
    _commit(repo, "README.md", "stable-only change\n")
    run(["git", "checkout", "main"], cwd=repo)
    _commit(repo, "code.txt", "incoming code\n")
    main = _sha(repo)
    run(["git", "checkout", "stable"], cwd=repo)
    run(["git", "merge", "--no-edit", "main"], cwd=repo)
    candidate = _sha(repo)
    run(["git", "checkout", "rhoai-3.6"], cwd=repo)
    _commit(repo, "README.md", "release-only change\n")

    _, main_results = check_releases(repo, source_ref=main, policy=policy)
    _, candidate_results = check_releases(repo, source_ref=candidate, policy=policy)

    assert main_results[0].status == "passed"
    assert candidate_results[0].status == "conflicts"
    assert candidate_results[0].conflict_files == ("README.md",)


def test_missing_release_does_not_hide_other_results(git_repo_factory, policy) -> None:
    repo = git_repo_factory("missing")
    _commit(repo, "README.md", "candidate\n")
    run(["git", "branch", "rhoai-3.7"], cwd=repo)
    _, results = check_releases(
        repo, source_ref="HEAD", policy=replace(policy, releases=("rhoai-3.6", "rhoai-3.7"))
    )
    assert results[0].status == "error"
    assert results[0].target_sha is None
    assert "missing" in results[0].message
    assert results[1].status == "passed"


def test_unrelated_histories_are_an_error_not_a_pass(git_repo_factory, policy) -> None:
    repo = git_repo_factory("unrelated")
    _commit(repo, "README.md", "candidate\n")
    candidate = _sha(repo)
    run(["git", "checkout", "--orphan", "rhoai-3.6"], cwd=repo)
    _commit(repo, "README.md", "unrelated release\n")
    _, results = check_releases(repo, source_ref=candidate, policy=policy)
    assert results[0].status == "error"
    assert "unrelated histories" in results[0].message


def test_shallow_checkout_is_rejected(git_repo_factory, tmp_path, policy) -> None:
    origin = git_repo_factory("origin")
    _commit(origin, "README.md", "base\n")
    _commit(origin, "README.md", "candidate\n")
    shallow = tmp_path / "shallow"
    run(["git", "clone", "--depth=1", origin.as_uri(), str(shallow)], cwd=tmp_path)
    with pytest.raises(FeasibilityError, match="Full Git history"):
        check_releases(shallow, source_ref="HEAD", policy=policy)


def test_prefers_remote_release_ref(git_repo_factory, policy) -> None:
    repo = git_repo_factory("remote")
    _commit(repo, "README.md", "base\n")
    run(["git", "branch", "rhoai-3.6"], cwd=repo)
    _commit(repo, "README.md", "candidate\n")
    candidate = _sha(repo)
    run(["git", "update-ref", "refs/remotes/origin/rhoai-3.6", candidate], cwd=repo)
    _, results = check_releases(repo, source_ref=candidate, policy=policy)
    assert results[0].target_sha == candidate
    assert results[0].status == "passed"
