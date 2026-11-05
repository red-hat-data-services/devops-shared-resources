"""End-to-end operations on clean disposable candidate-source Git fixtures.

Fixture commits are test data, not production source commits or approved pins.
"""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from lib import tekton_reconciliation as operation

ROOT = Path(__file__).resolve().parents[1]


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.PIPE).decode().strip()


def init(root):
    root.mkdir(parents=True, exist_ok=True)
    git(root, "init", "-q")
    git(root, "config", "user.name", "Fixture")
    git(root, "config", "user.email", "fixture@example.com")


def commit(root):
    git(root, "add", ".")
    git(root, "commit", "-qm", "test fixture")
    return git(root, "rev-parse", "HEAD")


@pytest.fixture(scope="module")
def tooling(tmp_path_factory):
    root = tmp_path_factory.mktemp("clean-candidate-source")
    init(root)
    shutil.copytree(ROOT / "lib", root / "lib", ignore=shutil.ignore_patterns("__pycache__"))
    (root / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/validate-tekton", root / "scripts/validate-tekton")
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n")
    commit(root)
    return root


@pytest.fixture
def setup(tmp_path):
    source, target = tmp_path / "policy", tmp_path / "target"
    init(source)
    directory = source / "templates"
    directory.mkdir()
    (directory / "config.yaml").write_text("templates:\n  - name: all\n    file: all.yaml.j2\n    match_fn: is_pipeline_run\n")
    (directory / "all.yaml.j2").write_text('metadata:\n  labels:\n    reconciled: "yes"\n')
    commit(source)
    init(target)
    (target / ".tekton").mkdir()
    (target / ".tekton/example.yml").write_text("apiVersion: tekton.dev/v1\nkind: PipelineRun\nmetadata:\n  name: example-on-push\n")
    commit(target)
    git(target, "remote", "add", "origin", "https://github.com/example/component.git")
    return source, target


def run(tooling, *args):
    return subprocess.run([sys.executable, str(tooling / "scripts/validate-tekton"), *map(str, args)],
                          capture_output=True, text=True)


def plan(tooling, setup, tmp_path, *extra):
    source, target = setup
    report = tmp_path / "report"
    result = run(tooling, "--reconcile", "--path", target, "--config-dir", source / "templates",
                 "--branch", "rhoai-3.6", "--report-dir", report, *extra)
    return report, result


def resume_args(setup, report, tmp_path):
    source, target = setup
    body = tmp_path / "notes.md"
    body.write_text("Trusted policy review notes")
    return ["--resume-report", report, "--path", target, "--config-dir", source / "templates",
            "--branch", "rhoai-3.6", "--repository", "example/component", "--head", "automation/test",
            "--marker", "policy-marker", "--title", "Reconcile pipelines", "--body-file", body,
            "--lifecycle", "create-or-report"]


def test_actual_cli_plan_verify_repeat_without_target_writes(tooling, setup, tmp_path):
    target = setup[1]
    original = (target / ".tekton/example.yml").read_bytes()
    report, result = plan(tooling, setup, tmp_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["status"] == "ready"
    assert data["changes"][0]["path"] == ".tekton/example.yml"
    for _ in range(2):
        verified = run(tooling, *resume_args(setup, report, tmp_path))
        assert verified.returncode == 0, verified.stderr
        assert json.loads(verified.stdout)["ready_to_publish"] is True
    assert (target / ".tekton/example.yml").read_bytes() == original
    assert not git(target, "status", "--porcelain")
    assert {path.stem for path in (report / "phases").glob("*.json") if not path.name.endswith("command.json")} == {
        "initial", "preview", "fix", "validation", "idempotence"}


@pytest.mark.parametrize("tamper", ["payload", "manifest", "base", "source", "dirty-target", "index", "untracked"])
def test_resume_rejects_tampering(tooling, setup, tmp_path, tamper):
    report, result = plan(tooling, setup, tmp_path)
    assert result.returncode == 0, result.stderr
    source, target = setup
    if tamper == "payload":
        (report / "bundle/files/.tekton/example.yml").write_text("evil")
    elif tamper == "manifest":
        manifest = json.loads((report / "bundle/manifest.json").read_text())
        manifest["changes"] = []
        (report / "bundle/manifest.json").write_text(json.dumps(manifest))
    elif tamper == "base":
        git(target, "commit", "--allow-empty", "-qm", "advance")
    elif tamper == "source":
        (source / "templates/all.yaml.j2").write_text("changed")
    else:
        (target / ("untracked" if tamper == "untracked" else ".tekton/example.yml")).write_text("unrelated")
        if tamper == "index":
            git(target, "add", ".")
    checked = run(tooling, *resume_args(setup, report, tmp_path))
    assert checked.returncode != 0


def test_explicit_template_deletion_requires_opt_in(tooling, setup, tmp_path):
    source, target = setup
    (source / "templates/all.yaml.j2").write_text('kind: "{{ _ if (metadata.get(\'name\') | delete_if(\'example-on-push\')) else _ }}"\n')
    commit(source)
    report, denied = plan(tooling, setup, tmp_path)
    assert denied.returncode != 0
    report = tmp_path / "allowed"
    result = run(tooling, "--reconcile", "--path", target, "--config-dir", source / "templates", "--branch", "rhoai-3.6",
                 "--report-dir", report, "--allowed-operations", "M,D")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["changes"][0]["status"] == "D"
    assert (target / ".tekton/example.yml").exists()


@pytest.mark.parametrize("kind", ["duplicate", "bundle", "symlink", "kind"])
def test_strict_existing_pipeline_profile(tooling, setup, tmp_path, kind):
    source, target = setup
    path = target / ".tekton/example.yml"
    if kind == "duplicate":
        path.write_text("kind: PipelineRun\nkind: PipelineRun\n")
    elif kind == "bundle":
        path.write_text(path.read_text() + "---\nkind: Task\n")
    elif kind == "symlink":
        path.unlink()
        path.symlink_to(source / "templates/all.yaml.j2")
    elif kind == "kind":
        (source / "templates/all.yaml.j2").write_text("kind: Task\n")
        commit(source)
    if kind in ("duplicate", "bundle", "symlink"):
        commit(target)
    _, result = plan(tooling, setup, tmp_path)
    assert result.returncode != 0


def test_no_pipeline_resource_stream_noop(tooling, setup, tmp_path):
    source, target = setup
    (target / ".tekton/example.yml").write_text("kind: Task\n---\nkind: ConfigMap\n")
    commit(target)
    report, result = plan(tooling, setup, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "no-changes"
    verified = run(tooling, *resume_args(setup, report, tmp_path))
    assert verified.returncode == 0, verified.stderr
    assert json.loads(verified.stdout)["ready_to_publish"] is False


def test_inventory_membership_batch_and_booleans(tooling, tmp_path):
    source = tmp_path / "release"
    init(source)
    for name in ("alpha", "beta"):
        (source / "pipelineruns" / name).mkdir(parents=True)
        (source / "pipelineruns" / name / "file").write_text("tracked")
    revision = commit(source)
    args = ["--inventory-root", source / "pipelineruns", "--branch", "rhoai-3.6", "--allowed-branches", "rhoai-3.6"]
    result = run(tooling, *args, "--json")
    assert result.returncode == 0, result.stderr
    assert len(result.stdout.splitlines()) == 1  # GITHUB_OUTPUT matrix=value transport
    assert json.loads(result.stdout)["include"] == [{"repository": n, "inventory_revision": revision} for n in ("alpha", "beta")]
    assert run(tooling, *args, "--publication-intent", "true").returncode != 0
    assert run(tooling, *args, "--publication-intent", "true", "--allow-batch-publication", "true").returncode == 0
    assert run(tooling, *args, "--select-repository", "missing").returncode != 0
    assert run(tooling, *args, "--publication-intent", "False").returncode != 0


def test_wrong_expected_policy_source(tooling, setup, tmp_path):
    _, result = plan(tooling, setup, tmp_path, "--source-sha", "0" * 40)
    assert result.returncode != 0
    assert "source SHA" in result.stderr


def test_dirty_shared_rejected_before_lib_execution(tooling, setup, tmp_path):
    copy = tmp_path / "dirty-shared"
    shutil.copytree(tooling, copy)
    (copy / "lib/tekton_reconciliation.py").write_text("raise RuntimeError('UNTRUSTED_EXECUTED')")
    _, result = plan(copy, setup, tmp_path)
    assert result.returncode != 0
    assert "must be clean" in result.stderr
    assert "UNTRUSTED_EXECUTED" not in result.stderr


def test_ignored_bytecode_not_executed(tooling, setup, tmp_path):
    import importlib.util
    import os
    import py_compile
    source = tooling / "lib/tekton_reconciliation.py"
    fake = tmp_path / "fake.py"
    evil = "raise RuntimeError('IGNORED_BYTECODE_EXECUTED')\n"
    fake.write_text(evil + " " * (source.stat().st_size - len(evil)))
    os.utime(fake, (source.stat().st_atime, source.stat().st_mtime))
    cache = Path(importlib.util.cache_from_source(str(source)))
    cache.parent.mkdir(exist_ok=True)
    py_compile.compile(str(fake), cfile=str(cache), doraise=True)
    _, result = plan(tooling, setup, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "IGNORED_BYTECODE_EXECUTED" not in result.stderr


def test_missing_trusted_module_uses_import_error_without_bytecode_fallback(tooling, setup, tmp_path):
    import py_compile
    fake = tmp_path / "ignored.py"
    fake.write_text("raise RuntimeError('SOURCELESS_BYTECODE_EXECUTED')\n")
    py_compile.compile(str(fake), cfile=str(tooling / "lib/review_missing.pyc"), doraise=True)
    source, target = setup
    module = source / "policy.py"
    module.write_text('''caught = 0
try:
    import lib.review_missing
except ModuleNotFoundError as exc:
    assert exc.name == "lib.review_missing"
    caught += 1
try:
    from lib import review_missing
except ImportError:
    caught += 1
CONTEXT_PATHS = ()
filters = {}
def decision(data, context):
    assert caught == 2
    return {"status": "skipped", "reason": "optional module unavailable", "variables": {}}
def check(*args):
    return True
''')
    commit(source)
    report, result = plan(tooling, setup, tmp_path, "--policy-python", module)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "no-changes"
    assert "SOURCELESS_BYTECODE_EXECUTED" not in result.stderr


def test_missing_scope_noop(tooling, setup, tmp_path):
    source, target = setup
    (target / ".tekton/example.yml").unlink()
    commit(target)
    report, result = plan(tooling, setup, tmp_path)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "no-changes"


def test_actual_publication_boundary_is_existing_publisher(tooling, setup, tmp_path, monkeypatch):
    report, result = plan(tooling, setup, tmp_path)
    assert result.returncode == 0, result.stderr
    monkeypatch.setattr(operation, "ROOT", tooling)
    calls = []
    def publish(root, request):
        operation.publisher.verify(root, request)
        calls.append(request)
        return {"status": "published", "url": "https://github.com/example/component/pull/1"}
    monkeypatch.setattr(operation.publisher, "publish", publish)
    assert operation.main([*map(str, resume_args(setup, report, tmp_path)), "--publish"]) == 0
    assert len(calls) == 1
    assert calls[0]["changes"][0]["status"] == "M"
    assert (report / "publication.json").exists()


@pytest.mark.parametrize("reject", [False, True])
def test_document_policy_actual_cli_with_readonly_context(tooling, setup, tmp_path, reject):
    source, target = setup
    module = source / "scan.py"
    module.write_text('''CONTEXT_PATHS = ("pipelines/allowed.yaml",)
filters = {}
def decision(data, context):
    assert context["branch"] == "rhoai-3.6"
    assert context["documents"]["pipelines/allowed.yaml"]["kind"] == "Pipeline"
    if data["metadata"]["name"] == "manual-on-push":
        return {"status": "manual", "reason": "task budget", "variables": {}}
    return {"status": "applicable", "reason": "", "variables": {}}
def check(original, candidate, context):
    assert "reconciled" not in original["metadata"].get("labels", {}) or original == candidate
    return REJECT is False
'''.replace("REJECT", repr(reject)))
    commit(source)
    directory = target / "pipelineruns/component"
    directory.mkdir(parents=True)
    (target / ".tekton").rename(directory / ".tekton")
    (directory / ".tekton/manual.yaml").write_text("kind: PipelineRun\nmetadata:\n  name: manual-on-push\n")
    (target / "pipelines").mkdir()
    (target / "pipelines/allowed.yaml").write_text("kind: Pipeline\n")
    commit(target)
    report, result = plan(tooling, setup, tmp_path, "--tekton-layout", "nested", "--policy-python", module)
    if reject:
        assert result.returncode != 0
    else:
        assert result.returncode == 0, result.stderr
        data = json.loads(result.stdout)
        assert data["diagnostics"]["counts"] == {"planned": 1, "manual": 1}
        args = resume_args(setup, report, tmp_path)
        checked = run(tooling, *args, "--policy-python", module)
        assert checked.returncode == 0, checked.stderr
    assert not git(target, "status", "--porcelain")


@pytest.mark.parametrize("phase", ["plan", "resume"])
def test_allowlist_applies_to_all_actions(tooling, setup, tmp_path, phase):
    if phase == "plan":
        _, result = plan(tooling, setup, tmp_path, "--allowed-branches", "other-release")
    else:
        report, planned = plan(tooling, setup, tmp_path)
        assert planned.returncode == 0, planned.stderr
        result = run(tooling, *resume_args(setup, report, tmp_path), "--allowed-branches", "other-release")
    assert result.returncode != 0
    assert "release not allowed" in result.stderr


def test_ordinary_cli_discovers_both_yaml_extensions(tooling, setup, tmp_path):
    source, target = setup
    shutil.copy2(target / ".tekton/example.yml", target / ".tekton/second.yaml")
    commit(target)
    checked = run(tooling, "--path", target, "--config-dir", source / "templates", "--branch", "rhoai-3.6", "--no-checkout", "--json")
    assert checked.returncode == 1  # both are intentionally nonconformant
    assert set(json.loads(checked.stdout)["files"]) == {"example.yml", "second.yaml"}


def test_inventory_trusted_root_alias_and_untrusted_descendant(tooling, tmp_path):
    source = tmp_path / "source"
    init(source)
    (source / "pipelineruns/component").mkdir(parents=True)
    (source / "pipelineruns/component/file").write_text("tracked")
    commit(source)
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    args = ["--inventory-root", alias / "pipelineruns", "--branch", "release", "--allowed-branches", "release"]
    result = run(tooling, *args)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["include"][0]["repository"] == "component"
    (source / "unsafe").symlink_to(source / "pipelineruns", target_is_directory=True)
    commit(source)
    rejected = run(tooling, "--inventory-root", source / "unsafe", "--branch", "release", "--allowed-branches", "release")
    assert rejected.returncode != 0
    assert "unsafe inventory" in rejected.stderr


def test_policy_module_root_alias_but_not_descendant_symlink(tooling, setup, tmp_path):
    source, target = setup
    module = source / "policy.py"
    module.write_text('''CONTEXT_PATHS = ()
filters = {}
def decision(data, context):
    return {"status": "skipped"}
def check(*args):
    return True
''')
    commit(source)
    alias = tmp_path / "alias"
    alias.symlink_to(source, target_is_directory=True)
    report, result = plan(tooling, setup, tmp_path, "--policy-python", alias / "policy.py")
    assert result.returncode == 0, result.stderr
    (source / "unsafe.py").symlink_to(module)
    commit(source)
    denied = run(tooling, "--reconcile", "--path", target, "--branch", "rhoai-3.6", "--config-dir", source / "templates",
                 "--policy-python", source / "unsafe.py", "--report-dir", tmp_path / "denied")
    assert denied.returncode != 0
    assert "symlink" in denied.stderr
    (source / "inner").symlink_to(source, target_is_directory=True)
    commit(source)
    denied = run(tooling, "--reconcile", "--path", target, "--branch", "rhoai-3.6", "--config-dir", source / "templates",
                 "--policy-python", source / "inner/policy.py", "--report-dir", tmp_path / "inner-denied")
    assert denied.returncode != 0
    assert "symlink" in denied.stderr


def test_graph_detachment():
    child = {"x": []}
    data = {"a": child, "b": child}
    result = operation.detached_data(data)
    result["a"]["x"].append(1)
    assert result["b"]["x"] == []
    child["cycle"] = child
    with pytest.raises(ValueError, match="cyclic"):
        operation.detached_data(data)
