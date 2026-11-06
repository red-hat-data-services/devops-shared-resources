"""Data-only existing-file reconciliation, not policy authorization.

Load this standalone module from trusted workflow-selected source. Artifacts never
select executable code. Hashes and provenance comparisons provide integrity only;
a trusted consumer must authorize every change independently on replay.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_CHANGES = 10000


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    if not isinstance(name, str):
        raise ValueError("path must be a string")
    path = PurePosixPath(name)
    if (not name or not path.parts or path.is_absolute() or path.as_posix() != name
            or any(p in ("..", ".git") for p in path.parts)
            or "\\" in name or any(ord(c) < 32 for c in name)):
        raise ValueError("unsafe path")
    return name


def contained(root, name):
    # The caller chooses the root, so normalize trusted aliases (e.g. /var).
    # Artifact-selected descendants must never traverse a symlink.
    root = Path(root).resolve()
    path = root / safe_name(name)
    for entry in (path, *path.parents):
        if entry == root:
            break
        if entry.is_symlink():
            raise ValueError("symlink path")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("path escape")
    return path


def read_file(root, name):
    path = contained(root, name)
    if not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("missing, nonregular or oversized file")
    return path.read_bytes()


def snapshot_paths(root, paths):
    """Inventory trusted explicit file selections, including cross-directory context."""
    result = {}
    for name in paths:
        if name in result:
            raise ValueError("duplicate snapshot path")
        path = contained(root, name)
        result[name] = {"sha256": digest(read_file(root, name)),
                        "mode": "100755" if path.stat().st_mode & 0o100 else "100644"}
    return result


def snapshot(root, scope):
    """Inventory a relative directory, rejecting symlinks and special files."""
    root = Path(root).resolve()
    directory = contained(root, scope)
    if not directory.exists():
        return {}
    if not directory.is_dir():
        raise ValueError("scope is not a directory")
    result = {}
    for path in sorted(directory.rglob("*")):
        name = path.relative_to(root).as_posix()
        contained(root, name)
        if path.is_dir():
            continue
        data = read_file(root, name)
        result[name] = {"sha256": digest(data),
                        "mode": "100755" if path.stat().st_mode & 0o100 else "100644"}
    return result


def verify_changes(before, after, selected, deletions=()):
    """Enforce caller-selected existing-file scope; never infer policy from hashes."""
    selected, deletions = set(selected), set(deletions)
    if not selected <= before.keys() or not deletions <= selected:
        raise ValueError("selection must name existing files")
    if after.keys() - before.keys() or before.keys() - after.keys() != deletions:
        raise ValueError("unexpected additions/deletions")
    changes = []
    for name, old in sorted(before.items()):
        safe_name(name)
        new = after.get(name)
        if new == old:
            continue
        if name not in selected or (new and new["mode"] != old["mode"]):
            raise ValueError("unselected change or changed mode")
        changes.append({"path": name, "status": "M" if new else "D", "mode": old["mode"],
                        "before_sha256": old["sha256"],
                        "after_sha256": new["sha256"] if new else None})
    return changes


def git(root, *args):
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True).stdout


def clean_base(root, base_oid):
    if not isinstance(base_oid, str) or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", base_oid):
        raise ValueError("invalid base OID")
    if git(root, "rev-parse", "HEAD").decode().strip() != base_oid:
        raise ValueError("stale base")
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("checkout must be clean")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def validate_manifest(manifest):
    if (not isinstance(manifest, dict) or set(manifest) != {"version", "base_oid", "provenance", "changes"}
            or type(manifest.get("version")) is not int or manifest["version"] != 1
            or not isinstance(manifest.get("base_oid"), str)
            or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", manifest["base_oid"])
            or not isinstance(manifest.get("provenance"), dict)
            or not isinstance(manifest.get("changes"), list)
            or len(manifest["changes"]) > MAX_CHANGES):
        raise ValueError("malformed plan")
    seen = set()
    for row in manifest["changes"]:
        if not isinstance(row, dict) or set(row) != {"path", "status", "mode", "before_sha256", "after_sha256"}:
            raise ValueError("malformed change")
        name = safe_name(row["path"])
        if name in seen or row["status"] not in ("M", "D") or row["mode"] not in ("100644", "100755"):
            raise ValueError("duplicate path or unsupported change")
        seen.add(name)
        for key in ("before_sha256", "after_sha256"):
            value = row[key]
            if key == "after_sha256" and row["status"] == "D":
                if value is not None:
                    raise ValueError("deleted file has content hash")
            elif not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("invalid hash")
    return manifest


def write_bundle(bundle, *, base_oid, provenance, changes, candidate_root):
    """Write data-only exact bytes; destination must not already exist."""
    manifest = validate_manifest({"version": 1, "base_oid": base_oid,
                                  "provenance": provenance, "changes": changes})
    copies = []
    for row in changes:
        if row["status"] == "M":
            data = read_file(candidate_root, row["path"])
            if digest(data) != row["after_sha256"]:
                raise ValueError("candidate hash mismatch")
            copies.append((row["path"], data))
    bundle = Path(bundle)
    bundle.mkdir(parents=True, exist_ok=False)
    for name, data in copies:
        path = bundle / "files" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (bundle / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def prepare_bundle(root, bundle, *, scope, base_oid, expected_provenance, authorize,
                   allowed_statuses=("M",), allow_applied=False):
    """Authorize ALL changes before ANY writes in a fresh clean checkout.

    authorize(row, original_bytes, candidate_bytes_or_None) must explicitly return
    True after independent consumer policy checks. No policy code comes from data.
    This does not publish; consumers still revalidate and use the exact publisher.
    """
    root, bundle = Path(root).resolve(), Path(bundle).resolve()
    safe_name(scope)
    contained(root, scope)
    if bundle.resolve().is_relative_to(root.resolve()):
        raise ValueError("bundle must be outside checkout")
    manifest = validate_manifest(json.loads(read_file(bundle, "manifest.json"),
                                            object_pairs_hook=unique_object))
    if manifest.get("base_oid") != base_oid or manifest["provenance"] != expected_provenance:
        raise ValueError("stale plan or provenance mismatch")
    def check_checkout():
        if not allow_applied:
            clean_base(root, base_oid)
            return
        if git(root, "rev-parse", "HEAD").decode().strip() != base_oid:
            raise ValueError("stale base")
        if git(root, "diff", "--cached", "--name-only") or git(root, "ls-files", "--others", "--exclude-standard"):
            raise ValueError("index/untracked files must be clean")
        changed = set(git(root, "diff", "--name-only", "-z").decode().split("\0")) - {""}
        if not changed <= {row["path"] for row in manifest["changes"]}:
            raise ValueError("unrelated checkout changes")
    check_checkout()
    copies = []
    for row in manifest["changes"]:
        name = row["path"]
        if row["status"] not in allowed_statuses:
            raise ValueError("operation not allowed by consumer")
        if not PurePosixPath(name).is_relative_to(PurePosixPath(scope)):
            raise ValueError("path outside trusted scope")
        path = contained(root, name)
        entry = git(root, "ls-tree", "HEAD", "--", name).split()
        if len(entry) < 3 or entry[0].decode() != row["mode"] or entry[1] != b"blob":
            raise ValueError("not an existing regular tracked file")
        original = git(root, "show", f"HEAD:{name}")
        candidate = read_file(bundle, f"files/{name}") if row["status"] == "M" else None
        current = read_file(root, name) if path.exists() else None
        permitted = (original, candidate) if allow_applied else (original,)
        if (current not in permitted or digest(original) != row["before_sha256"]
                or (candidate is not None and digest(candidate) != row["after_sha256"])
                or (current is not None and bool(path.stat().st_mode & 0o100) != (row["mode"] == "100755"))):
            raise ValueError("content or mode mismatch")
        if authorize(dict(row), original, candidate) is not True:
            raise ValueError("consumer did not authorize change")
        copies.append((path, current, candidate, row["mode"]))
    # Trusted callbacks must be read-only; detect accidental writes before replay.
    check_checkout()
    for path, original, candidate, mode in copies:
        contained(root, path.relative_to(root).as_posix())
        current = read_file(root, path.relative_to(root).as_posix()) if path.exists() else None
        if (current != original or (current is not None
                and bool(path.stat().st_mode & 0o100) != (mode == "100755"))):
            raise ValueError("target changed during authorization")
    for path, _, candidate, _ in copies:
        if candidate is None:
            if path.exists():
                path.unlink()
        else:
            path.write_bytes(candidate)
    return manifest


def run_validation(argv, cwd, report, label):
    """Capture a trusted command's reports; caller interprets status and semantics."""
    if not re.fullmatch(r"[a-z][a-z0-9_-]*", label):
        raise ValueError("unsafe report label")
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True)
    report = Path(report)
    report.mkdir(parents=True, exist_ok=True)
    (report / f"{label}.json").write_text(result.stdout)
    (report / f"{label}.log").write_text(result.stderr)
    (report / f"{label}.command.json").write_text(json.dumps(
        {"argv": [str(arg) for arg in argv], "returncode": result.returncode}, indent=2) + "\n")
    return result


def reconcile(root, *, scope, report, provenance, validate, select, context_paths=()):
    """Plan isolated existing-file reconciliation and export a replayable bundle.

    Trusted validate(stage, phase) runs/interprets initial, preview, fix,
    validation and idempotence. It must reject failed non-initial checks itself.
    select(initial_report, before_snapshot) returns (selected_paths, deletions).
    Resource parsing, policy ownership and semantic reports remain consumer-owned.
    No callbacks are serialized or invoked by replay except mandatory authorize.
    """
    root, report = Path(root).resolve(), Path(report).resolve()
    contained(root, scope)
    if report.resolve().is_relative_to(root) or root.is_relative_to(report.resolve()):
        raise ValueError("report and checkout must be disjoint")
    base_oid = git(root, "rev-parse", "HEAD").decode().strip()
    clean_base(root, base_oid)
    before = snapshot(root, scope)
    context = snapshot_paths(root, context_paths)
    if context.keys() & before.keys():
        raise ValueError("context overlaps writable scope")
    for name in context:
        tracked_context = git(root, "--literal-pathspecs", "ls-files", "-z", "--", name)
        if tracked_context.split(b"\0") != [name.encode(), b""]:
            raise ValueError("context must be tracked")
    tracked = set(git(root, "ls-files", "-z", "--", scope).decode().split("\0")) - {""}
    if tracked != set(before):
        raise ValueError("scope differs from tracked files")
    report.mkdir(parents=True, exist_ok=False)
    stage = report / "stage"
    stage.mkdir()
    if (root / scope).exists():
        shutil.copytree(root / scope, stage / scope)
    else:
        (stage / scope).mkdir(parents=True)
    for name in context:
        dest = stage / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(root / name, dest)
    git(stage, "init", "-q")
    if before or context:
        git(stage, "add", "-f", "--", *before, *context)
    baseline_index = git(stage, "write-tree")

    def phase(name):
        output = validate(stage, name)
        if git(stage, "write-tree") != baseline_index:
            raise ValueError("validator modified stage index")
        if snapshot_paths(stage, context) != context:
            raise ValueError("read-only context changed")
        changed = set(git(stage, "diff", "--name-only", "-z").decode().split("\0")) - {""}
        if changed - before.keys() or git(stage, "ls-files", "--others", "--exclude-standard"):
            raise ValueError("validator wrote outside existing-file scope")
        return output

    initial = phase("initial")
    if snapshot(stage, scope) != before:
        raise ValueError("initial validation modified files")
    selected, deletions = select(initial, before)
    phase("preview")
    if snapshot(stage, scope) != before:
        raise ValueError("dry run modified files")
    phase("fix")
    after = snapshot(stage, scope)
    changes = verify_changes(before, after, selected, deletions)
    phase("validation")
    if snapshot(stage, scope) != after:
        raise ValueError("validation modified files")
    phase("idempotence")
    if snapshot(stage, scope) != after:
        raise ValueError("reconciliation is not idempotent")
    # All stage changes must be in scope, even if a validator writes elsewhere.
    changed = set(git(stage, "diff", "--name-only", "-z").decode().split("\0")) - {""}
    untracked = git(stage, "ls-files", "--others", "--exclude-standard")
    if changed != {row["path"] for row in changes} or untracked:
        raise ValueError("validator changed files outside plan")
    clean_base(root, base_oid)
    if snapshot_paths(stage, context) != context or snapshot_paths(root, context) != context:
        raise ValueError("read-only context changed")
    if snapshot(root, scope) != before:
        raise ValueError("target changed during planning")
    patch = git(stage, "diff", "--binary", "--", scope)
    (report / "enforcement.patch").write_bytes(patch)
    return write_bundle(report / "bundle", base_oid=base_oid, provenance=provenance,
                        changes=changes, candidate_root=stage)
