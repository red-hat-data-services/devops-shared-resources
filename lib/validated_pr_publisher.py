"""Publish independently validated existing-file changes, never plan policy fixes.

Requests are constructed by trusted adapters after policy validation. Hashes are
integrity checks, not authorization. No templates or artifact-supplied code run.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import subprocess
from typing import Callable


Runner = Callable[..., str]


def command(args, cwd, *, input=None):
    """Execute argument arrays without shell interpolation or credential discovery."""
    return subprocess.run(args, cwd=cwd, input=input, text=True, check=True,
                          capture_output=True).stdout.strip()


def digest(contents):
    """Return the SHA-256 of exact file bytes."""
    return hashlib.sha256(contents).hexdigest()


def validate_request(request):
    """Validate the versioned data contract, paths, identities, and supported modes."""
    if request.get("version") != 1:
        raise ValueError("unsupported publication request version")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", request["repository"]):
        raise ValueError("invalid repository")
    if request["lifecycle"] not in ("create-or-report", "create-or-update"):
        raise ValueError("unsupported lifecycle")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", request["base_oid"]):
        raise ValueError("invalid base commit")
    if not request["head"].startswith("automation/") or request["head"] == request["base"]:
        raise ValueError("publication head must be a distinct automation branch")
    for key in ("marker", "title", "body"):
        if not isinstance(request[key], str) or not request[key].strip():
            raise ValueError(f"missing {key}")
    if "\n" in request["marker"] or "\r" in request["marker"]:
        raise ValueError("marker must be one line")
    paths = set()
    for change in request["changes"]:
        name = change["path"]
        path = PurePosixPath(name)
        if (not name or path.is_absolute() or path.as_posix() != name
                or any(part in ("..", ".git") for part in path.parts)
                or any(ord(c) < 32 for c in name) or "\\" in name or name in paths):
            raise ValueError("unsafe or duplicate change path")
        paths.add(name)
        if change["status"] not in ("M", "D") or change["mode"] not in ("100644", "100755"):
            raise ValueError("only existing regular-file modifications/deletions are supported")
        for field in ("before_sha256", "after_sha256"):
            value = change[field]
            if field == "after_sha256" and change["status"] == "D":
                if value is not None:
                    raise ValueError("deleted file must have no after hash")
            elif not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
                raise ValueError("invalid file hash")


def verify(root, request, run=command):
    """Verify already-applied bytes, modes, base, and whole-checkout change scope."""
    root = Path(root).resolve()
    validate_request(request)
    git = lambda *args: run(["git", *args], root)
    for ref in (request["base"], request["head"]):
        git("check-ref-format", f"refs/heads/{ref}")
    if git("rev-parse", "HEAD") != request["base_oid"]:
        raise ValueError("checkout differs from validated base")
    origin = git("remote", "get-url", "origin").removesuffix(".git")
    if origin not in (f"https://github.com/{request['repository']}", f"git@github.com:{request['repository']}"):
        raise ValueError("origin does not match repository")
    if git("diff", "--cached", "--name-only"):
        raise ValueError("index must be clean before publishing")
    if git("ls-files", "--others", "--exclude-standard"):
        raise ValueError("untracked files block publication")
    expected = sorted((c["status"], c["path"]) for c in request["changes"])
    raw = git("diff", "HEAD", "--no-renames", "--name-status", "-z")
    fields = raw.split("\0") if raw else []
    if fields and fields[-1] == "":
        fields.pop()
    if len(fields) % 2 or sorted(zip(fields[::2], fields[1::2])) != expected:
        raise ValueError("checkout changes differ from validated manifest")
    for change in request["changes"]:
        path = root / change["path"]
        if any(p.is_symlink() for p in (path, *path.parents) if p != root and p.is_relative_to(root)):
            raise ValueError("symlink change path")
        entry = git("ls-tree", "HEAD", "--", change["path"]).split()
        if len(entry) < 3 or entry[0] != change["mode"] or entry[1] != "blob":
            raise ValueError("base file is missing or has an unexpected mode")
        # Read bytes directly: text decoding/strip would corrupt binary/trailing data.
        original = subprocess.run(["git", "show", f"HEAD:{change['path']}"], cwd=root,
                                  check=True, capture_output=True).stdout
        if digest(original) != change["before_sha256"]:
            raise ValueError("base content hash differs")
        if change["status"] == "D":
            if path.exists():
                raise ValueError("planned deletion still exists")
        elif (not path.is_file() or digest(path.read_bytes()) != change["after_sha256"]
              or bool(path.stat().st_mode & 0o100) != (change["mode"] == "100755")):
            raise ValueError("file bytes or executable mode differ from validation")
    git("diff", "HEAD", "--check")
    return root


def publish(root, request, run=command):
    """Publish exact checked bytes with explicit PR lifecycle and no force-push."""
    root = verify(root, request, run)
    if not request["changes"]:
        return {"status": "no-changes"}
    git = lambda *args: run(["git", *args], root)
    repo, base, head = (request[k] for k in ("repository", "base", "head"))
    base_ref, head_ref = f"refs/heads/{base}", f"refs/heads/{head}"
    remote = git("ls-remote", "--heads", "origin", base_ref).split()
    if not remote or remote[0] != request["base_oid"]:
        raise ValueError("remote base advanced; prepare again")
    prs = json.loads(run(["gh", "pr", "list", "--repo", repo, "--base", base,
                          "--state", "all", "--limit", "1000", "--json",
                          "number,url,state,headRefName,body"], root))
    if len(prs) >= 1000:
        raise ValueError("PR inventory may be truncated")
    matching = [pr for pr in prs if pr["state"] == "OPEN" and
                (pr["headRefName"] == head or request["marker"] in (pr.get("body") or ""))]
    if len(matching) > 1:
        raise ValueError("multiple matching PRs require review")
    if matching and request["lifecycle"] == "create-or-report":
        return {"status": "existing-pr", "url": matching[0]["url"]}
    if request["lifecycle"] == "create-or-report" and any(pr["headRefName"] == head for pr in prs):
        raise ValueError("prior PR on publication branch requires disposition")
    if matching and matching[0]["headRefName"] != head:
        raise ValueError("cannot update a different policy branch")
    existing = git("ls-remote", "--heads", "origin", head_ref).split()
    old = existing[0] if existing else None
    if old and request["lifecycle"] == "create-or-report":
        raise ValueError("publication branch already exists without an open PR")
    if old:
        git("fetch", "--no-tags", "origin", head_ref)
        if git("rev-parse", "FETCH_HEAD") != old:
            raise ValueError("publication branch moved")
        if request["marker"] not in git("show", "-s", "--format=%B", old).splitlines():
            raise ValueError("branch lacks generated commit marker")
    # Recheck after remote reads and immediately before staging the exact paths.
    verify(root, request, run)
    git("add", "--", *(c["path"] for c in request["changes"]))
    raw = git("diff", "--cached", "--no-renames", "--name-status", "-z")
    fields = raw.rstrip("\0").split("\0")
    if sorted(zip(fields[::2], fields[1::2])) != sorted((c["status"], c["path"]) for c in request["changes"]):
        raise ValueError("index differs from manifest")
    for change in request["changes"]:
        if change["status"] == "M":
            entry = git("ls-files", "--stage", "--", change["path"]).split()
            staged = subprocess.run(["git", "show", f":{change['path']}"], cwd=root,
                                    check=True, capture_output=True).stdout
            if entry[0] != change["mode"] or digest(staged) != change["after_sha256"]:
                raise ValueError("staged bytes or mode differ from validation")
    tree = git("write-tree")
    base_marker = f"Validated-Publication-Base: {request['base_oid']}"
    message = f"{request['title']}\n\n{request['marker']}\n{base_marker}\n"
    identity = git("var", "GIT_AUTHOR_IDENT").rsplit(">", 1)[0] + ">"
    message += f"\nSigned-off-by: {identity}\n"
    if (old and git("rev-parse", f"{old}^{{tree}}") == tree
            and base_marker in git("show", "-s", "--format=%B", old).splitlines()):
        commit = old
    else:
        args = ["git", "commit-tree", tree, "-p", request["base_oid"]]
        if old and old != request["base_oid"]:
            args += ["-p", old]
        commit = run(args, root, input=message)
        git("push", "origin", f"{commit}:{head_ref}")
    body = request["body"]
    if request["marker"] not in body:
        body = request["marker"] + "\n\n" + body
    if matching:
        url = run(["gh", "pr", "edit", str(matching[0]["number"]), "--repo", repo,
                   "--title", request["title"], "--body", body], root)
        url = matching[0]["url"]
    else:
        url = run(["gh", "pr", "create", "--repo", repo, "--base", base, "--head", head,
                   "--title", request["title"], "--body", body], root)
    return {"status": "published", "url": url.strip(), "commit": commit,
            "branch": head, "base_oid": request["base_oid"],
            "provenance": request.get("provenance", {})}
