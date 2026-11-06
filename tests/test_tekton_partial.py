"""Strict-profile partial previews; fixture commits are isolated test data only."""
import json
from pathlib import Path
import subprocess

import pytest

from lib import tekton_reconciliation as operation
from lib.tekton_preview_summary import render_preview_summary
from test_tekton_reconciliation import tooling, setup, commit as fixture_commit, git, plan, operation_args


def commit(root):
    try:
        return fixture_commit(root)
    except subprocess.CalledProcessError as exc:
        raise AssertionError(exc.stderr.decode()) from exc

ROOT = Path(__file__).resolve().parents[1]
BAD = b"kind: PipelineRun\nspec: {}\nspec: {canary: publiccanarysamplevalue}\n"


def strict(source):
    config = source / "templates/config.yaml"
    config.write_text(config.read_text() + "selection_template: selection.json.j2\n")
    (config.parent / "selection.json.j2").write_text('{"status":"applicable","reason":""}')
    commit(source)


@pytest.mark.parametrize("case", ["changed", "unchanged", "all-bad"])
@pytest.mark.parametrize("publish", [False, True])
def test_partial_cli_reports_patch_phases_and_never_publishes(tooling, setup, tmp_path, monkeypatch, case, publish):
    source, target = setup
    strict(source)
    valid = target / ".tekton/example.yml"
    if case == "unchanged":
        valid.write_text(valid.read_text() + '  labels: {reconciled: "yes"}\n')
    elif case == "all-bad":
        valid.write_bytes(BAD)
    bad = target / ".tekton/bad.yml"
    bad.write_bytes(BAD)
    bad.chmod(0o755)
    commit(target)
    before = {path.name: path.read_bytes() for path in (valid, bad)}
    summary = tmp_path / "summary.md"
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    report, completed = plan(tooling, setup, tmp_path, *(["--publish"] if publish else []))
    assert completed.returncode == 2, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "partial"
    errors = result["diagnostics"]["errors"]
    assert len(errors) == (2 if case == "all-bad" else 1)
    assert all(row["error_class"] == "DuplicateKeyError" for row in errors)
    assert result["diagnostics"]["counts"]["error"] == len(errors)
    assert len(result["changes"]) == (1 if case == "changed" else 0)
    patch = (report / "enforcement.patch").read_text()
    assert ("reconciled" in patch) == (case == "changed")
    assert "bad.yml" not in patch and "publiccanarysamplevalue" not in completed.stdout + completed.stderr
    for phase in ("initial", "preview", "fix", "validation", "idempotence"):
        data = json.loads((report / f"phases/{phase}.json").read_text())
        assert data["errors"] == errors
        assert data["counts"]["error"] == len(errors)
    assert json.loads((report / "result.json").read_text())["status"] == "partial"
    assert {path.name: path.read_bytes() for path in (valid, bad)} == before
    assert bad.stat().st_mode & 0o777 == 0o755
    assert not git(target, "status", "--porcelain")
    if publish:
        assert result["publication"]["status"] == "blocked"
        assert json.loads((report / "publication.json").read_text())["status"] == "blocked"
        assert not summary.exists()
    else:
        text = summary.read_text()
        assert "NOT PUBLISHABLE" in text and ".tekton/bad.yml" in text


@pytest.mark.parametrize("data,kind", [(BAD, "DuplicateKeyError"), (b"kind: PipelineRun\nx: [\n", "ParserError"), (b'kind: PipelineRun\nx: "bad\\q"\n', "ScannerError"), (b"kind: PipelineRun\nx: \xff\n", "ReaderError")])
def test_only_allowlisted_parse_failures_isolated(tmp_path, data, kind):
    (tmp_path / ".tekton").mkdir()
    (tmp_path / ".tekton/bad.yml").write_bytes(data)
    errors = []
    assert operation.documents(tmp_path, ".tekton", "flat", errors=errors) == {}
    assert errors[0]["error_class"] == kind
    with pytest.raises(Exception):
        operation.documents(tmp_path, ".tekton", "flat")


@pytest.mark.parametrize("data", [b"kind: PipelineRun\nx: !unknown target\n", b"kind: PipelineRun\nx: *missing\n", b"kind: PipelineRun\nx: &cycle [*cycle]\n", b"kind: PipelineRun\nx: !!set {a: null}\n", b"kind: PipelineRun\n---\nkind: Task\n"])
def test_nonlocal_security_and_stream_failures_remain_fatal(tmp_path, data):
    (tmp_path / ".tekton").mkdir()
    (tmp_path / ".tekton/bad.yml").write_bytes(data)
    errors = []
    with pytest.raises(Exception):
        operation.documents(tmp_path, ".tekton", "flat", errors=errors)
    assert errors == []


@pytest.mark.parametrize("mutation", ["bytes", "mode", "repaired", "new-bad"])
def test_excluded_inventory_is_immutable(tmp_path, mutation):
    (tmp_path / ".tekton").mkdir()
    bad = tmp_path / ".tekton/bad.yml"
    bad.write_bytes(BAD)
    errors = []
    operation.documents(tmp_path, ".tekton", "flat", errors=errors)
    excluded = operation.ExcludedFiles.capture(tmp_path, errors)
    if mutation == "bytes":
        bad.write_bytes(BAD + b"# changed\n")
    elif mutation == "mode":
        bad.chmod(0o755)
    elif mutation == "repaired":
        bad.write_text("kind: PipelineRun\n")
    else:
        (bad.parent / "second.yml").write_bytes(BAD)
    with pytest.raises(ValueError, match="malformed-file inventory"):
        excluded.inspect(tmp_path, ".tekton", "flat")


def test_nonpolicy_subprocess_profile_stays_fail_closed(tooling, setup, tmp_path):
    (setup[1] / ".tekton/bad.yml").write_bytes(BAD)
    commit(setup[1])
    report, completed = plan(tooling, setup, tmp_path)
    assert completed.returncode == 1
    assert not (report / "result.json").exists()


@pytest.mark.parametrize("changes", [[], [{"status": "M", "path": ".tekton/good.yml"}]])
@pytest.mark.parametrize("status", ["partial", "ready", "no-changes"])
def test_publish_entrypoints_block_before_publisher_or_nochange(setup, tmp_path, monkeypatch, capsys, changes, status):
    def forbidden(*args, **kwargs):
        pytest.fail("publisher must not be called for partial result")
    monkeypatch.setattr(operation.publisher, "publish", forbidden)
    result = {"status": status, "changes": changes, "diagnostics": {"errors": [{"path": "bad", "error_class": "ParserError", "reason": "invalid YAML syntax"}]}}
    with pytest.raises(ValueError, match="not publishable"):
        operation.publish_candidate(None, result, None)
    # CLI boundary unit test: deliberately mocked plan, not source-identity proof.
    def mocked_plan(args, report):
        report.mkdir()
        (report / "enforcement.patch").write_text("")
        return result.copy()
    monkeypatch.setattr(operation, "plan", mocked_plan)
    monkeypatch.setattr(operation, "publish_candidate", forbidden)
    assert operation.main([*map(str, operation_args(setup, tmp_path / "report")), "--publish"]) == 2
    assert json.loads(capsys.readouterr().out)["publication"]["status"] == "blocked"


def test_partial_summary_escapes_precise_paths_and_allbad_warning():
    path = '.tekton/<script>"&.yaml'
    result = {"status": "partial", "changes": [], "diagnostics": {"errors": [{"path": path}], "counts": {"error": 1}}}
    rendered = render_preview_summary(result, "")
    assert "NOT PUBLISHABLE" in rendered and "File errors: 1" in rendered
    assert "&lt;script&gt;&quot;&amp;.yaml" in rendered and "<script>" not in rendered


@pytest.mark.parametrize("error", [MemoryError, RecursionError])
def test_runtime_resource_failures_never_become_partial(tmp_path, monkeypatch, error):
    (tmp_path / ".tekton").mkdir()
    (tmp_path / ".tekton/good.yml").write_text("kind: PipelineRun\n")
    def fail(*args, **kwargs):
        raise error("resource failure")
    monkeypatch.setattr(operation, "detached_data", fail)
    errors = []
    with pytest.raises(error):
        operation.documents(tmp_path, ".tekton", "flat", errors=errors)
    assert errors == []


def test_graph_depth_budget_remains_fatal(tmp_path):
    (tmp_path / ".tekton").mkdir()
    (tmp_path / ".tekton/deep.yml").write_text("kind: PipelineRun\nx: " + "[" * 140 + "0" + "]" * 140 + "\n")
    errors = []
    with pytest.raises(ValueError, match="validation limits"):
        operation.documents(tmp_path, ".tekton", "flat", errors=errors)
    assert errors == []


def test_context_parse_errors_still_fatal_with_bad_target(tooling, setup, tmp_path):
    source, target = setup
    strict(source)
    config = source / "templates/config.yaml"
    config.write_text(config.read_text() + "context_paths: [context.yml]\n")
    commit(source)
    (target / "context.yml").write_text("kind: Pipeline\nx: [\n")
    (target / ".tekton/bad.yml").write_bytes(b"kind: PipelineRun\nx: [\n")
    commit(target)
    report, completed = plan(tooling, setup, tmp_path)
    assert completed.returncode == 1
    assert not (report / "result.json").exists()


def test_real_duplicate_plus_valid_file_clean_fixture_cli(tooling, setup, tmp_path):
    source, target = setup
    strict(source)
    fixture = ROOT / "tests/fixtures/tekton/malformed/autorag-main-a3418537.yaml"
    bad = target / ".tekton/autorag.yml"
    bad.write_bytes(fixture.read_bytes())
    commit(target)
    report, completed = plan(tooling, setup, tmp_path)
    assert completed.returncode == 2, completed.stderr
    result = json.loads(completed.stdout)
    assert result["status"] == "partial"
    assert [row["path"] for row in result["changes"]] == [".tekton/example.yml"]
    assert result["diagnostics"]["errors"][0]["path"] == ".tekton/autorag.yml"
    assert bad.read_bytes() == fixture.read_bytes()
    assert "autorag" not in (report / "enforcement.patch").read_text()
    assert not git(target, "status", "--porcelain")


def test_exact_public_autorag_duplicate_fixture(tmp_path):
    (tmp_path / ".tekton").mkdir()
    source = ROOT / "tests/fixtures/tekton/malformed/autorag-main-a3418537.yaml"
    (tmp_path / ".tekton/autorag.yml").write_bytes(source.read_bytes())
    errors = []
    assert operation.documents(tmp_path, ".tekton", "flat", errors=errors) == {}
    assert errors == [{"path": ".tekton/autorag.yml", "error_class": "DuplicateKeyError", "reason": "duplicate YAML mapping key at line 84, column 3"}]
