"""Real local Git fixtures; no remote calls or policy implementations."""
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

spec = importlib.util.spec_from_file_location("reconciliation", Path(__file__).parents[1] / "lib/reconciliation.py")
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)


def git(root, *args):
    return subprocess.check_output(["git", *args], cwd=root).decode().strip()


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q")
    git(root, "config", "user.name", "Test")
    git(root, "config", "user.email", "test@example.com")
    (root / "files").mkdir()
    for name in ("a", "b"):
        (root / "files" / name).write_bytes(b"old\n")
    git(root, "add", ".")
    git(root, "commit", "-qm", "base")
    return root


def plan(root, tmp_path, *, delete=False):
    def validate(stage, phase):
        if phase == "fix":
            (stage / "files/a").write_bytes(b"new\n")
            if delete:
                (stage / "files/b").unlink()
    return r.reconcile(root, scope="files", report=tmp_path / "report", provenance={"policy": "trusted"},
                       validate=validate, select=lambda initial, before: (list(before), ["files/b"] if delete else []))


def replay(root, tmp_path, manifest, **kwargs):
    return r.prepare_bundle(root, tmp_path / "report/bundle", scope="files",
                            base_oid=git(root, "rev-parse", "HEAD"), expected_provenance=manifest["provenance"],
                            authorize=kwargs.pop("authorize", lambda *args: True), **kwargs)


def test_mixed_plan_and_replay(checkout, tmp_path):
    manifest = plan(checkout, tmp_path, delete=True)
    assert [row["status"] for row in manifest["changes"]] == ["M", "D"]
    assert (checkout / "files/a").read_bytes() == b"old\n"
    replay(checkout, tmp_path, manifest, allowed_statuses=("M", "D"))
    assert (checkout / "files/a").read_bytes() == b"new\n"
    assert not (checkout / "files/b").exists()


def test_late_authorization_failure_never_copies(checkout, tmp_path):
    manifest = plan(checkout, tmp_path, delete=True)
    calls = []
    def authorize(row, old, new):
        calls.append(row["path"])
        return row["status"] != "D"
    with pytest.raises(ValueError, match="authorize"):
        replay(checkout, tmp_path, manifest, allowed_statuses=("M", "D"), authorize=authorize)
    assert calls == ["files/a", "files/b"]
    assert (checkout / "files/a").read_bytes() == b"old\n"
    assert (checkout / "files/b").exists()


@pytest.mark.parametrize("phase", ["initial", "preview", "validation", "idempotence"])
def test_read_only_phases(checkout, tmp_path, phase):
    def validate(stage, current):
        if current == phase:
            (stage / "files/a").write_bytes(b"bad")
    with pytest.raises(ValueError):
        r.reconcile(checkout, scope="files", report=tmp_path / "report", provenance={}, validate=validate,
                    select=lambda *args: (["files/a"], []))
    assert (checkout / "files/a").read_bytes() == b"old\n"


@pytest.mark.parametrize("kind", ["addition", "mode", "outside", "symlink", "unselected", "unexpected-deletion"])
def test_unsafe_fix(checkout, tmp_path, kind):
    def validate(stage, phase):
        if phase != "fix":
            return
        if kind == "addition":
            (stage / "files/new").write_bytes(b"new")
        elif kind == "mode":
            (stage / "files/a").chmod(0o755)
        elif kind == "outside":
            (stage / "outside").write_bytes(b"bad")
        elif kind == "symlink":
            (stage / "files/a").unlink()
            (stage / "files/a").symlink_to(checkout / "files/a")
        elif kind == "unexpected-deletion":
            (stage / "files/a").unlink()
        else:
            (stage / "files/b").write_bytes(b"bad")
    with pytest.raises(ValueError):
        r.reconcile(checkout, scope="files", report=tmp_path / "report", provenance={}, validate=validate,
                    select=lambda *args: (["files/a"], []))


@pytest.mark.parametrize("tamper", ["bytes", "base", "provenance", "duplicate", "path", "mode", "status", "scope", "extra-field"])
def test_replay_tampering(checkout, tmp_path, tamper):
    manifest = plan(checkout, tmp_path)
    row = manifest["changes"][0]
    if tamper == "bytes":
        (tmp_path / "report/bundle/files/files/a").write_bytes(b"evil")
    elif tamper == "base":
        manifest["base_oid"] = "0" * 40
    elif tamper == "provenance":
        manifest["provenance"] = {"policy": "evil"}
    elif tamper == "duplicate":
        manifest["changes"].append(dict(row))
    elif tamper == "path":
        row["path"] = "../escape"
    elif tamper == "mode":
        row["mode"] = "100755"
    elif tamper == "status":
        row["status"] = "A"
    elif tamper == "scope":
        row["path"] = "outside/a"
    else:
        row["argv"] = ["evil"]
    (tmp_path / "report/bundle/manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError):
        r.prepare_bundle(checkout, tmp_path / "report/bundle", scope="files", base_oid=git(checkout, "rev-parse", "HEAD"),
                         expected_provenance={"policy": "trusted"}, authorize=lambda *args: True)
    assert (checkout / "files/a").read_bytes() == b"old\n"


def test_deletion_requires_explicit_opt_in(checkout, tmp_path):
    manifest = plan(checkout, tmp_path, delete=True)
    with pytest.raises(ValueError, match="operation"):
        replay(checkout, tmp_path, manifest)
    assert (checkout / "files/a").read_bytes() == b"old\n"


@pytest.mark.parametrize("name", ["../a", "/a", "files//a", "files/./a", ".git/config", "files/../a", "files\\a", "files/a\n"])
def test_paths(name):
    with pytest.raises(ValueError):
        r.safe_name(name)


def test_trusted_root_alias_and_untrusted_descendant(checkout, tmp_path):
    (tmp_path / "link").symlink_to(checkout, target_is_directory=True)
    assert r.snapshot(tmp_path / "link", "files") == r.snapshot(checkout, "files")
    (checkout / "alias").symlink_to(checkout / "files", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        r.snapshot(checkout, "alias")


def test_dirty_checkout_and_stale_head(checkout, tmp_path):
    manifest = plan(checkout, tmp_path)
    (checkout / "untracked").write_bytes(b"bad")
    with pytest.raises(ValueError, match="clean"):
        replay(checkout, tmp_path, manifest)


def test_capture_trusted_command(tmp_path):
    import sys
    result = r.run_validation([sys.executable, "-c", "print('{}'); raise SystemExit(3)"], tmp_path, tmp_path / "logs", "initial")
    assert result.returncode == 3
    assert json.loads((tmp_path / "logs/initial.command.json").read_text())["returncode"] == 3


@pytest.mark.parametrize("delete", [False, True])
def test_repeat_replay_authorizes_original_git_bytes(checkout, tmp_path, delete):
    manifest = plan(checkout, tmp_path, delete=delete)
    calls = []
    def authorize(row, original, candidate):
        assert original == b"old\n"
        calls.append(row["path"])
        return True
    for _ in range(2):
        replay(checkout, tmp_path, manifest, allow_applied=True,
               allowed_statuses=("M", "D"), authorize=authorize)
    assert len(calls) == 2 * len(manifest["changes"])
    (checkout / "files/a").write_bytes(b"unrelated")
    with pytest.raises(ValueError, match="content"):
        replay(checkout, tmp_path, manifest, allow_applied=True, allowed_statuses=("M", "D"))


@pytest.mark.parametrize("dirty", ["index", "untracked", "unrelated"])
def test_repeat_replay_rejects_other_changes(checkout, tmp_path, dirty):
    manifest = plan(checkout, tmp_path)
    if dirty == "index":
        (checkout / "files/a").write_bytes(b"new\n")
        git(checkout, "add", "files/a")
    elif dirty == "untracked":
        (checkout / "new").write_bytes(b"bad")
    else:
        (checkout / "files/b").write_bytes(b"bad")
    with pytest.raises(ValueError):
        replay(checkout, tmp_path, manifest, allow_applied=True)


@pytest.mark.parametrize("mutate", [False, True])
def test_trusted_readonly_context(checkout, tmp_path, mutate):
    (checkout / "context").mkdir()
    (checkout / "context/pipeline").write_bytes(b"context")
    git(checkout, "add", ".")
    git(checkout, "commit", "-qm", "context")
    def validate(stage, phase):
        assert (stage / "context/pipeline").read_bytes() == b"context"
        if mutate and phase == "idempotence":
            (stage / "context/pipeline").write_bytes(b"bad")
    args = dict(scope="files", report=tmp_path / "report", provenance={}, validate=validate,
                select=lambda *args: ([], []), context_paths=["context/pipeline"])
    if mutate:
        with pytest.raises(ValueError):
            r.reconcile(checkout, **args)
    else:
        assert r.reconcile(checkout, **args)["changes"] == []
    assert (checkout / "context/pipeline").read_bytes() == b"context"


def test_stale_actual_head(checkout, tmp_path):
    manifest = plan(checkout, tmp_path)
    git(checkout, "commit", "--allow-empty", "-qm", "advance")
    with pytest.raises(ValueError, match="stale"):
        r.prepare_bundle(checkout, tmp_path / "report/bundle", scope="files", base_oid=manifest["base_oid"],
                         expected_provenance=manifest["provenance"], authorize=lambda *args: True)


@pytest.mark.parametrize("manifest", [[], {}, {"version": True}, {"version": 9}])
def test_malformed_manifest(manifest):
    with pytest.raises(ValueError):
        r.validate_manifest(manifest)


def test_duplicate_json_key(checkout, tmp_path):
    manifest = plan(checkout, tmp_path)
    path = tmp_path / "report/bundle/manifest.json"
    path.write_text(path.read_text().replace('"version": 1', '"version": 1, "version": 1'))
    with pytest.raises(ValueError, match="duplicate JSON"):
        replay(checkout, tmp_path, manifest)


def test_base_hash_is_not_authorization(checkout, tmp_path):
    manifest = plan(checkout, tmp_path)
    manifest["changes"][0]["before_sha256"] = r.digest(b"fake")
    (tmp_path / "report/bundle/manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="content"):
        replay(checkout, tmp_path, manifest)


@pytest.mark.parametrize("phase", ["initial", "preview", "fix", "validation", "idempotence"])
def test_context_and_index_checked_every_phase(checkout, tmp_path, phase):
    (checkout / "context").write_bytes(b"context")
    git(checkout, "add", "context")
    git(checkout, "commit", "-qm", "context")
    seen = []
    def validate(stage, current):
        seen.append(current)
        if current == phase:
            (stage / "context").write_bytes(b"bad")
    with pytest.raises(ValueError, match="context"):
        r.reconcile(checkout, scope="files", report=tmp_path / "report", provenance={},
                    validate=validate, select=lambda *args: ([], []), context_paths=["context"])
    assert seen[-1] == phase


@pytest.mark.parametrize("phase", ["initial", "preview", "fix", "validation", "idempotence"])
def test_staged_outside_addition_rejected_immediately(checkout, tmp_path, phase):
    seen = []
    def validate(stage, current):
        seen.append(current)
        if current == phase:
            (stage / "outside").write_bytes(b"bad")
            git(stage, "add", "outside")
    with pytest.raises(ValueError, match="index"):
        r.reconcile(checkout, scope="files", report=tmp_path / "report", provenance={},
                    validate=validate, select=lambda *args: ([], []))
    assert seen[-1] == phase


def test_authorizer_cannot_change_mode(checkout, tmp_path):
    manifest = plan(checkout, tmp_path)
    def authorize(row, *args):
        (checkout / row["path"]).chmod(0o755)
        return True
    with pytest.raises(ValueError, match="changed during authorization"):
        replay(checkout, tmp_path, manifest, allow_applied=True, authorize=authorize)
    assert (checkout / "files/a").read_bytes() == b"old\n"


def test_symlink_payload_directory(checkout, tmp_path):
    manifest = plan(checkout, tmp_path)
    bundle = tmp_path / "report/bundle"
    (bundle / "files").rename(bundle / "actual")
    (bundle / "files").symlink_to(bundle / "actual", target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        replay(checkout, tmp_path, manifest)


@pytest.mark.parametrize("name", ["ignored.ctx", "context*.ctx", "context[ab].ctx"])
def test_ignored_context_is_not_authorized_by_pathspec(checkout, tmp_path, name):
    (checkout / "context-a.ctx").write_bytes(b"tracked")
    git(checkout, "add", "context-a.ctx")
    (checkout / ".gitignore").write_text("*.ctx\n")
    git(checkout, "add", ".gitignore")
    git(checkout, "commit", "-qm", "tracked and ignored context fixture")
    (checkout / name).write_bytes(b"ignored")
    report = tmp_path / "report"
    with pytest.raises(ValueError, match="context must be tracked"):
        r.reconcile(checkout, scope="files", report=report, provenance={},
                    validate=lambda *args: None, select=lambda *args: ([], []), context_paths=[name])
    assert not report.exists()


def test_literal_tracked_context_with_pathspec_characters(checkout, tmp_path):
    name = "context[ab]*.ctx"
    (checkout / name).write_bytes(b"tracked literally")
    git(checkout, "--literal-pathspecs", "add", "--", name)
    git(checkout, "commit", "-qm", "literal context fixture")
    def validate(stage, phase):
        assert (stage / name).read_bytes() == b"tracked literally"
    manifest = r.reconcile(checkout, scope="files", report=tmp_path / "report", provenance={},
                           validate=validate, select=lambda *args: ([], []), context_paths=[name])
    assert manifest["changes"] == []


def test_missing_scope_is_empty(checkout, tmp_path):
    manifest = r.reconcile(checkout, scope="absent", report=tmp_path / "report", provenance={},
                           validate=lambda *args: None, select=lambda *args: ([], []))
    assert manifest["changes"] == []
