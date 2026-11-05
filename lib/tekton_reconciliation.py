"""Preview Tekton reconciliation or plan, validate and publish in one operation.

Trusted workflows choose the source, templates and optional narrow Python policy.
Exact-change plans exist only inside this invocation; saved reports are diagnostic.
"""
from collections import Counter
from io import StringIO
import argparse
import html
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import types
from urllib.parse import quote, urlsplit

from ruamel.yaml import YAML
from lib import reconciliation as worker
from lib import validated_pr_publisher as publisher
from lib.tekton import classify_pipeline, extract_component_prefix, render_and_merge, resolve_overlay
from lib.tekton_config import load_config

ROOT = Path(__file__).resolve().parents[1]


def detached_data(value, ancestors=(), budget=None):
    budget = [100000] if budget is None else budget
    budget[0] -= 1
    if budget[0] < 0 or len(ancestors) > 128:
        raise ValueError("YAML graph exceeds validation limits")
    if isinstance(value, (dict, list)):
        if id(value) in ancestors:
            raise ValueError("cyclic YAML aliases are not supported")
        ancestors += (id(value),)
        if isinstance(value, dict):
            if any(not isinstance(key, str) for key in value):
                raise ValueError("YAML mapping keys must be strings")
            return {key: detached_data(item, ancestors, budget) for key, item in value.items()}
        return [detached_data(item, ancestors, budget) for item in value]
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("YAML values must be JSON-compatible scalars")
    return value


def git(root, *args):
    return worker.git(root, *args).decode().strip()


def source_identity(path):
    root = Path(git(path, "rev-parse", "--show-toplevel"))
    if git(root, "status", "--porcelain", "--untracked-files=all"):
        raise ValueError("trusted source checkout must be clean")
    return root, git(root, "rev-parse", "HEAD")


def policy_digest(directory):
    files = worker.snapshot(directory.parent, directory.name)
    digest = __import__("hashlib").sha256()
    for name in files:
        relative = Path(name).relative_to(directory.name).as_posix()
        digest.update(relative.encode() + b"\0" + worker.read_file(directory.parent, name))
    return digest.hexdigest()


def source_relative(path, source):
    """Normalize caller root aliases, not symlinks within a source checkout."""
    path = Path(path).absolute()
    for parent in reversed((path, *path.parents)):
        if parent.resolve() == source:
            name = path.relative_to(parent).as_posix()
            worker.contained(source, name)
            return name
    raise ValueError("path is outside trusted source")


def load_policy(path, source):
    if not path:
        return None
    path = Path(path).absolute()
    name = source_relative(path, source)
    data = worker.read_file(source, name)
    if worker.git(source, "show", f"HEAD:{name}") != data:
        raise ValueError("policy bytes differ from trusted source")
    module = types.ModuleType("trusted_tekton_policy")
    module.__file__ = str(path)
    exec(compile(data, str(path), "exec"), module.__dict__)
    for key in ("decision", "check"):
        if not callable(getattr(module, key, None)):
            raise ValueError(f"policy lacks {key}")
    return module


def documents(root, scope, layout):
    """Strict inventory first; resource-only streams stay untouched."""
    inventory = worker.snapshot(root, scope)
    yaml = YAML(typ="safe", pure=True)
    runs = {}
    for name in inventory:
        path = Path(name)
        if path.suffix not in (".yaml", ".yml"):
            continue
        if layout == "nested" and not (len(path.parts) == 4 and path.parts[0] == "pipelineruns" and path.parts[2] == ".tekton"):
            continue
        docs = list(yaml.load_all(worker.read_file(root, name)))
        docs = [detached_data(doc) for doc in docs]
        if any(isinstance(doc, dict) and doc.get("kind") == "PipelineRun" for doc in docs):
            if len(docs) != 1:
                raise ValueError(f"bundled PipelineRun stream: {name}")
            runs[name] = docs[0]
    return runs


def context_for(root, policy, branch):
    context = {"branch": branch, "documents": {}}
    yaml = YAML(typ="safe", pure=True)
    for name in getattr(policy, "CONTEXT_PATHS", ()):
        path = worker.contained(root, name)
        context["documents"][name] = detached_data(yaml.load(worker.read_file(root, name))) if path.exists() else None
    return context


def scan_phase(stage, phase, config, policy, branch, scope, layout):
    from lib import tekton_filters
    for name, function in getattr(policy, "filters", {}).items():
        if not isinstance(name, str) or not callable(function):
            raise ValueError("invalid policy filter")
        setattr(tekton_filters, name, tekton_filters.jinja_filter(function))
    context = context_for(stage, policy, branch)
    yaml = YAML()
    yaml.preserve_quotes, yaml.width = True, 4096
    rows, copies = [], []
    for name, original in documents(stage, scope, layout).items():
        data = yaml.load(worker.read_file(stage, name))
        decision = policy.decision(detached_data(original), detached_data(context))
        status = decision["status"]
        if status not in ("applicable", "skipped", "manual", "unresolved"):
            raise ValueError("invalid policy decision")
        row = {"path": name, "status": status, "reason": decision.get("reason", "")}
        if status == "applicable":
            template = config.get_template(classify_pipeline(Path(name).name, data, config))
            if template is None or template.parent:
                raise ValueError("policy requires an existing non-parent template")
            overlay = resolve_overlay(config.config_dir, template.file,
                                      extract_component_prefix(Path(name).name, data),
                                      data.get("metadata", {}).get("labels", {}).get("appstudio.openshift.io/component"))
            errors = []
            candidate = render_and_merge(config.config_dir, template.file, data, Path(name).name,
                                         branch, mode="validate" if phase == "validation" else "fix",
                                         cli_vars=decision.get("variables", {}),
                                         errors=errors if phase == "validation" else None, overlay_path=overlay)
            if errors or policy.check(detached_data(original), detached_data(candidate), detached_data(context)) is not True:
                raise ValueError(f"policy rejected candidate {name}: {errors}")
            row["status"] = "conformant" if candidate == data else "planned"
            if candidate != data:
                buffer = StringIO()
                yaml.dump(candidate, buffer)
                serialized = buffer.getvalue()
                if detached_data(YAML(typ="safe", pure=True).load(serialized)) != detached_data(candidate):
                    raise ValueError("serialized candidate differs")
                copies.append((stage / name, serialized))
        rows.append(row)
    if phase in ("validation", "idempotence") and copies:
        raise ValueError("validation/idempotence still plans changes")
    if phase == "fix":
        for path, text in copies:
            path.write_text(text)
    return {"files": rows, "counts": dict(Counter(row["status"] for row in rows))}


def plan(args, report):
    root, config_dir = args.path.resolve(), args.config_dir.resolve()
    _, revision = source_identity(ROOT)
    source, policy_revision = source_identity(config_dir)
    if args.source_sha and args.source_sha != policy_revision:
        raise ValueError("policy source differs from expected source SHA")
    config = load_config(str(config_dir))
    raw = YAML(typ="safe", pure=True).load((config_dir / "config.yaml").read_text())
    if config.parent_ref or any("generate" in row for row in raw.get("templates", [])):
        raise ValueError("existing-file reconciliation does not support generation/external parents")
    layout = config.tekton_layout
    scope = ".tekton" if layout == "flat" else "pipelineruns"
    policy = load_policy(args.policy_python, source)
    if policy is None and layout != "flat":
        raise ValueError("nested reconciliation currently requires a document policy")
    allowed = args.allowed_operations.split(",")
    if allowed not in (["M"], ["M", "D"]):
        raise ValueError("allowed operations must be M or M,D")
    if policy and allowed != ["M"]:
        raise ValueError("document policy profile is modifications only")
    before_runs = documents(root, scope, layout)
    phases = {}
    selected, deletions = [], []
    def validate(stage, phase):
        if policy:
            output = scan_phase(stage, phase, config, policy, args.branch, scope, layout)
        else:
            flags = {"initial": [], "preview": ["--fix", "--dry-run"], "fix": ["--fix"],
                     "validation": [], "idempotence": ["--fix"]}[phase]
            completed = worker.run_validation([sys.executable, "-B", "-X", f"pycache_prefix={report / 'python-cache'}", str(ROOT / "scripts/validate-tekton"),
                "--path", str(stage), "--config-dir", str(config_dir), "--branch", args.branch,
                "--no-checkout", "--json", *flags], stage, report / "phases", phase)
            output = json.loads(completed.stdout) if completed.stdout.strip() else {"files": {}}
            if completed.returncode and phase != "initial":
                raise ValueError(f"{phase} validation failed; see phase reports")
            if not isinstance(output.get("files"), dict):
                raise ValueError("invalid validator report")
            after = documents(stage, scope, layout)
            if any(name in after and after[name].get("kind") != "PipelineRun" for name in before_runs):
                raise ValueError("resource kind changed")
            # A changed existing PipelineRun must still be discoverable as one.
            inventory = worker.snapshot(stage, scope)
            for name in before_runs:
                if name in inventory and name not in after:
                    raise ValueError("PipelineRun changed resource kind")
        phases[phase] = output
        logs = report / "phases"
        logs.mkdir(exist_ok=True)
        (logs / f"{phase}.json").write_text(json.dumps(output, indent=2) + "\n")
        return output
    def select(initial, inventory):
        if policy:
            selected.extend(row["path"] for row in initial["files"] if row["status"] == "planned")
        else:
            for name, details in initial["files"].items():
                relative = ".tekton/" + name
                if Path(name).name != name or relative not in before_runs:
                    raise ValueError("validator selected a non-PipelineRun")
                selected.append(relative)
                if details.get("delete"):
                    deletions.append(relative)
        if deletions and "D" not in allowed:
            raise ValueError("policy requested deletion but caller permits only M")
        return selected, deletions
    context_paths = []
    for name in getattr(policy, "CONTEXT_PATHS", ()):
        if worker.contained(root, name).exists():
            context_paths.append(name)
    provenance = {"branch": args.branch, "policy_revision": policy_revision,
                  "worker_revision": revision, "validator_revision": revision, "publisher_revision": revision,
                  "policy_digest": policy_digest(config_dir), "inventory_revision": args.inventory_revision,
                  "policy_python": str(Path(args.policy_python).resolve().relative_to(source)) if policy else None}
    manifest = worker.reconcile(root, scope=scope, report=report, provenance=provenance,
                                validate=validate, select=select, context_paths=context_paths)
    result = {"status": "ready" if manifest["changes"] else "no-changes", "base": manifest["base_oid"],
              "policy_digest": provenance["policy_digest"], "policy_digest_short": provenance["policy_digest"][:12],
              "provenance": provenance, "scope": scope, "allowed_operations": allowed,
              "changes": manifest["changes"], "diagnostics": phases["initial"]}
    (report / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def display_text(value):
    """Escape untrusted display data without allowing new Markdown/HTML blocks."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(value))
    return re.sub(r"([\\`*_{}\[\]()#!|~])", r"\\\1", html.escape(text, quote=False))


def repository_name(value):
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", value or ""))


def commit_id(value):
    return bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value or ""))


def safe_server(value):
    try:
        parts = urlsplit(value)
    except ValueError:
        return None
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or not re.fullmatch(r"[A-Za-z0-9.-]+(?::[0-9]+)?", parts.netloc)
            or parts.query or parts.fragment or parts.path not in ("", "/")
            or re.search(r"[\s<>\\]", value)):
        return None
    return value.rstrip("/")


def policy_repository_url(source):
    """Use only the actual policy origin; never infer it from the target or run."""
    try:
        origin = git(source, "remote", "get-url", "origin").rstrip("/").removesuffix(".git")
    except subprocess.CalledProcessError:
        return None
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if origin.startswith(prefix) and repository_name(origin[len(prefix):]):
            return "https://github.com/" + origin[len(prefix):]
    return None


def linked_path(path, repository, revision, kind):
    """Link a safe repository-relative path at an immutable commit, else plain text."""
    text = display_text(path)
    try:
        worker.safe_name(path)
    except ValueError:
        return text
    if not repository or not commit_id(revision):
        return text
    return f"[{text}]({repository}/{kind}/{revision}/{quote(path, safe='/')})"


def workflow_context(environ):
    lines = ["## Created by", ""]
    repo = environ.get("GITHUB_REPOSITORY", "")
    name = environ.get("GITHUB_WORKFLOW", "")
    reference = environ.get("GITHUB_WORKFLOW_REF", "")
    server = safe_server(environ.get("GITHUB_SERVER_URL", "https://github.com"))
    if not name and not reference and not environ.get("GITHUB_RUN_ID"):
        return lines + ["Local run (GitHub Actions context unavailable)."]
    lines.append(f"- **Workflow:** {display_text(name or 'Not supplied')}")
    if "/.github/workflows/" in reference:
        workflow_repo, file_ref = reference.split("/.github/workflows/", 1)
        filename, separator, ref = file_ref.partition("@refs/")
        if not separator:
            filename, separator, ref = file_ref.rpartition("@")
        path = ".github/workflows/" + filename
        revision = environ.get("GITHUB_WORKFLOW_SHA", "")
        if not revision and workflow_repo == repo:
            revision = environ.get("GITHUB_SHA", "")
        base = f"{server}/{workflow_repo}" if server and repository_name(workflow_repo) and separator else None
        label = linked_path(path, base, revision, "blob")
        lines.append(f"- **Workflow file:** {label}")
        if not base or not commit_id(revision):
            lines.append(f"- **Workflow ref:** {display_text(reference)} (exact commit unavailable)")
    elif reference:
        lines.append(f"- **Workflow ref:** {display_text(reference)}")
    run = environ.get("GITHUB_RUN_ID", "")
    if run.isdigit() and server and repository_name(repo):
        lines.append(f"- **Run:** [{display_text(run)}]({server}/{repo}/actions/runs/{run})")
    elif run:
        lines.append(f"- **Run:** {display_text(run)}")
    return lines


def summary(result, *, policy_source, config_dir, environ=None):
    """Render presentation context without changing plan/provenance identities."""
    environ = os.environ if environ is None else environ
    lines = ["", *workflow_context(environ), "", "## Templates", ""]
    provenance = result["provenance"]
    revision = provenance["policy_revision"]
    repository = policy_repository_url(policy_source)
    try:
        config_path = Path(config_dir).resolve().relative_to(Path(policy_source).resolve()).as_posix()
    except ValueError:
        config_path, repository = str(config_dir), None
    lines += [f"- **Config directory:** {linked_path(config_path, repository, revision, 'tree')}",
              f"- **Policy revision:** {display_text(revision)}"]
    if provenance.get("policy_python"):
        lines.append(f"- **Policy module:** {linked_path(provenance['policy_python'], repository, revision, 'blob')}")
    lines += ["", "## Changes", ""]
    for status, heading in (("M", "Modified"), ("D", "Deleted")):
        paths = [row["path"] for row in result["changes"] if row["status"] == status]
        lines += [f"### {heading} ({len(paths)})", ""]
        lines += [f"- {display_text(path)}" for path in paths] if paths else ["- None"]
        lines.append("")
    lines += ["## Shared reconciliation checks", "",
              "Preview, fix, non-fix validation and idempotence passed. Only selected existing files changed.",
              "No automatic merge or workflow dispatch is requested."]
    diagnostics = result.get("diagnostics", {})
    rows = diagnostics.get("files", [])
    deferred = [row for row in rows if row["status"] in ("manual", "unresolved")] if isinstance(rows, list) else []
    if deferred:
        counts = Counter(row["status"] for row in deferred)
        lines += ["", "## Deferred cases", "", f"Manual: {counts['manual']}; unresolved: {counts['unresolved']}."]
        lines += [f"- {row['status']}: {display_text(row['path'])}: {display_text(row.get('reason', ''))}" for row in deferred]
    lines += ["", "## Provenance", "", "```json", json.dumps(provenance, indent=2), "```"]
    return "\n".join(lines)


def publish_candidate(args, result, report):
    """Publish only this invocation's fully checked candidate, never a saved report."""
    root, config_dir = args.path.resolve(), args.config_dir.resolve()
    _, revision = source_identity(ROOT)
    source, policy_revision = source_identity(config_dir)
    if args.inventory_revision is not None and args.inventory_revision != result["provenance"].get("inventory_revision"):
        raise ValueError("stale inventory receipt")
    if args.allowed_operations.split(",") != result["allowed_operations"]:
        raise ValueError("operation scope differs from receipt")
    expected = dict(result["provenance"])
    if any(expected.get(key) != revision for key in ("worker_revision", "validator_revision", "publisher_revision")):
        raise ValueError("stale shared tooling receipt")
    expected.update(branch=args.branch, policy_revision=policy_revision, policy_digest=policy_digest(config_dir),
                    policy_python=str(Path(args.policy_python).resolve().relative_to(source)) if args.policy_python else None)
    if expected != result["provenance"] or (args.source_sha and args.source_sha != policy_revision):
        raise ValueError("stale policy/source receipt")
    if args.branch in ("main", "master"):
        raise ValueError("release publication cannot target main/master")
    head = args.head_prefix + result["policy_digest_short"] if args.head_prefix else args.head
    if not head:
        raise ValueError("publication requires --head or --head-prefix")
    for key in ("repository", "marker", "title", "body_file", "lifecycle"):
        if not getattr(args, key):
            raise ValueError(f"publication requires --{key.replace('_', '-')}")
    body = args.body_file.read_text() + summary(result, policy_source=source, config_dir=config_dir)
    if args.tracking_url:
        if not re.fullmatch(r"https://[^\s<>]+", args.tracking_url):
            raise ValueError("invalid tracking URL")
        body += f"\n\nTracking: [issue]({args.tracking_url})\n"
    request = {"version": 1, "repository": args.repository, "base": args.branch,
               "base_oid": result["base"], "head": head, "marker": args.marker,
               "title": args.title, "body": body, "lifecycle": args.lifecycle,
               "changes": result["changes"], "provenance": expected}
    publisher.validate_request(request)
    worker.clean_base(root, result["base"])
    for ref in (args.branch, head):
        worker.git(root, "check-ref-format", f"refs/heads/{ref}")
    rows = result["changes"]
    bundle = json.loads(worker.read_file(report, "bundle/manifest.json"), object_pairs_hook=worker.unique_object)
    if bundle.get("changes") != rows:
        raise ValueError("candidate differs from this invocation's checked plan")
    worker.prepare_bundle(root, report / "bundle", scope=result["scope"], base_oid=result["base"],
                          expected_provenance=expected, allowed_statuses=tuple(result["allowed_operations"]),
                          authorize=lambda row, original, candidate: row in rows)
    return publisher.publish(root, request)


def boolean(value):
    if value not in ("true", "false"):
        raise argparse.ArgumentTypeError("expected true or false")
    return value == "true"


def inventory(args):
    if not args.allowed_branches or args.branch not in args.allowed_branches.split(","):
        raise ValueError("release not allowed")
    root = args.inventory_root.absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("unsafe inventory root")
    source, revision = source_identity(root)
    root = worker.contained(source, source_relative(root, source))
    names = []
    for path in sorted(root.iterdir()):
        if path.is_symlink():
            raise ValueError("symlink inventory entry")
        if path.is_dir():
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", path.name):
                raise ValueError("unsafe repository")
            names.append(path.name)
    selected = args.select_repository
    if selected == "all":
        if args.publication_intent and not args.allow_batch_publication:
            raise ValueError("batch publication requires explicit confirmation")
    elif selected in names:
        names = [selected]
    else:
        raise ValueError("repository is not managed by this release")
    if not names:
        raise ValueError("empty repository inventory")
    return {"include": [{"repository": name, "inventory_revision": revision} for name in names]}


def save_diagnostics(internal, report):
    """Persist useful reports, never the internal candidate checkout or bundle."""
    for name in ("result.json", "enforcement.patch", "publication.json"):
        path = internal / name
        if path.is_file():
            shutil.copyfile(path, report / name)
    if (internal / "phases").is_dir():
        shutil.copytree(internal / "phases", report / "phases")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--inventory-root", type=Path)
    for name in ("path", "config-dir", "policy-python", "report-dir", "body-file", "github-output"):
        parser.add_argument("--" + name, type=Path)
    for name in ("branch", "source-sha", "inventory-revision", "repository", "marker", "title", "tracking-url", "allowed-branches"):
        parser.add_argument("--" + name)
    head = parser.add_mutually_exclusive_group()
    head.add_argument("--head")
    head.add_argument("--head-prefix", help="Append this plan's policy digest (first 12 characters), without an extra separator")
    parser.add_argument("--allowed-operations", default="M")
    parser.add_argument("--select-repository", default="all")
    parser.add_argument("--publication-intent", type=boolean, default=False)
    parser.add_argument("--allow-batch-publication", type=boolean, default=False)
    parser.add_argument("--lifecycle", choices=("create-or-report", "create-or-update"))
    parser.add_argument("--publish", action="store_true", help="Plan, validate and publish in this invocation using caller-supplied authentication")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.allowed_branches and args.branch not in args.allowed_branches.split(","):
            raise ValueError("release not allowed")
        if args.inventory_root:
            if args.publish:
                raise ValueError("inventory never publishes")
            result = inventory(args)
        else:
            if not args.path or not args.config_dir or not args.branch or not args.report_dir:
                raise ValueError("path, config-dir, branch and report-dir are required")
            root, report = args.path.resolve(), args.report_dir.resolve()
            if report.is_relative_to(root) or root.is_relative_to(report):
                raise ValueError("report and target must be disjoint")
            report.mkdir(parents=True, exist_ok=False)
            with tempfile.TemporaryDirectory(prefix="tekton-reconcile-") as temp:
                internal = Path(temp) / "plan"
                try:
                    result = plan(args, internal)
                    if args.publish:
                        publication = publish_candidate(args, result, internal) if result["changes"] else {"status": "no-changes"}
                        (internal / "publication.json").write_text(json.dumps(publication, indent=2) + "\n")
                        result["publication"] = publication
                        if publication.get("url"):
                            result["publication_url"] = publication["url"]
                finally:
                    save_diagnostics(internal, report)
        if args.github_output:
            with args.github_output.open("a") as stream:
                for key in ("status", "base", "policy_digest_short", "publication_url"):
                    if key in result:
                        value = str(result[key])
                        if "\n" in value or "\r" in value:
                            raise ValueError("unsafe workflow output")
                        stream.write(f"{key}={value}\n")
        print(json.dumps(result, indent=None if args.json else 2))
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
