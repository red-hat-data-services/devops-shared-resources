from __future__ import annotations

from io import StringIO

import pytest

from lib.repository_automerge import RepositoryAutoMergeError, RepositoryAutoMergeResult
from scripts import manage_repository_automerge


class FakeOperations:
    def __init__(self, states: dict[str, str | Exception] | None = None) -> None:
        self.states = states or {}
        self.auth_checked = False
        self.calls: list[tuple[str, bool]] = []

    def check_authentication(self) -> None:
        self.auth_checked = True

    def manage(
        self,
        repository: str,
        *,
        apply: bool = False,
    ) -> RepositoryAutoMergeResult:
        self.calls.append((repository, apply))
        configured = self.states.get(repository, "would_enable")
        if isinstance(configured, Exception):
            raise configured
        return RepositoryAutoMergeResult(
            requested_repository=repository,
            repository=repository,
            status="enabled" if apply and configured == "would_enable" else configured,
        )


def install_fake_operations(
    monkeypatch: pytest.MonkeyPatch,
    fake: FakeOperations,
) -> None:
    monkeypatch.setattr(
        manage_repository_automerge,
        "check_authentication",
        fake.check_authentication,
    )
    monkeypatch.setattr(
        manage_repository_automerge,
        "manage_repository_automerge",
        fake.manage,
    )


def test_collect_repositories_normalizes_and_deduplicates_arguments() -> None:
    repositories = manage_repository_automerge.collect_repositories(
        ["kserve", "other/repo", "kserve"],
        stdin=StringIO("ignored\n"),
        default_org="red-hat-data-services",
    )

    assert repositories == ["red-hat-data-services/kserve", "other/repo"]


def test_collect_repositories_reads_newline_separated_stdin() -> None:
    repositories = manage_repository_automerge.collect_repositories(
        [],
        stdin=StringIO("kserve\n\nother/repo\nkserve\n"),
        default_org="red-hat-data-services",
    )

    assert repositories == ["red-hat-data-services/kserve", "other/repo"]


@pytest.mark.parametrize("repository", ["owner/repo/extra", "owner/", "bad name"])
def test_normalize_repository_rejects_invalid_names(repository: str) -> None:
    with pytest.raises(ValueError):
        manage_repository_automerge.normalize_repository(
            repository,
            default_org="red-hat-data-services",
        )


def test_main_audits_by_default(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    operations = FakeOperations(
        {
            "red-hat-data-services/kserve": "would_enable",
            "red-hat-data-services/kubeflow": "already_enabled",
        }
    )
    install_fake_operations(monkeypatch, operations)

    code = manage_repository_automerge.main(["kserve", "kubeflow"], stdin=StringIO())

    assert code == 0
    assert operations.auth_checked
    assert operations.calls == [
        ("red-hat-data-services/kserve", False),
        ("red-hat-data-services/kubeflow", False),
    ]
    output = capsys.readouterr().out
    assert "WOULD_ENABLE" in output
    assert "ALREADY_ENABLED" in output
    assert "updated=0" in output


def test_main_applies_repositories_from_stdin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    operations = FakeOperations()
    install_fake_operations(monkeypatch, operations)

    code = manage_repository_automerge.main(
        ["--apply"], stdin=StringIO("kserve\nkubeflow\n")
    )

    assert code == 0
    assert operations.calls == [
        ("red-hat-data-services/kserve", True),
        ("red-hat-data-services/kubeflow", True),
    ]
    output = capsys.readouterr().out
    assert output.count("ENABLED") == 2
    assert "updated=2" in output


def test_main_continues_after_repository_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    operations = FakeOperations(
        {"red-hat-data-services/kserve": RepositoryAutoMergeError("API failed")}
    )
    install_fake_operations(monkeypatch, operations)

    code = manage_repository_automerge.main(["kserve", "kubeflow"], stdin=StringIO())

    assert code == 1
    assert len(operations.calls) == 2
    captured = capsys.readouterr()
    assert "FAILED" in captured.err
    assert "failures=1" in captured.out


def test_main_rejects_empty_stdin(capsys: pytest.CaptureFixture[str]) -> None:
    code = manage_repository_automerge.main([], stdin=StringIO())

    assert code == 1
    assert "provide at least one repository" in capsys.readouterr().err
