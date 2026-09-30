"""Shared GitLab API helpers."""

import json
import re
import subprocess
import urllib.parse


def parse_project_path(url: str) -> str | None:
    """Extract the project path from a git remote URL."""
    m = re.search(r"[a-z+]+://[^/]+(:\d+)?/(.+?)(?:\.git)?$", url)
    if m:
        return m.group(2)
    m = re.search(r"[^/]:(.+?)(?:\.git)?$", url)
    if m:
        return m.group(1)
    return None


def get_project_path_from_remote() -> str | None:
    """Extract the GitLab project path from the current repo's origin remote."""
    r = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode != 0:
        return None
    return parse_project_path(r.stdout.strip())


def gitlab_api(
    endpoint: str,
    gitlab_host: str,
    method: str = "GET",
    fields: dict | None = None,
    json_body: dict | list | None = None,
):
    cmd = ["glab", "api", endpoint, "--hostname", gitlab_host]
    if method != "GET":
        cmd += ["--method", method]
    if json_body is not None:
        cmd += ["-H", "Content-Type: application/json", "--input", "-"]
        return subprocess.run(
            cmd, input=json.dumps(json_body), capture_output=True, text=True
        )
    for k, v in (fields or {}).items():
        cmd += ["-f", f"{k}={v}"]
    return subprocess.run(cmd, capture_output=True, text=True)


def project_exists(path: str, gitlab_host: str) -> bool:
    """Check if a project exists and is not pending deletion."""
    encoded = urllib.parse.quote(path, safe="")
    result = gitlab_api(f"projects/{encoded}", gitlab_host)
    if result.returncode != 0:
        return False
    data = json.loads(result.stdout)
    return not data.get("marked_for_deletion_at")


def auto_merge_mr(project_path: str, mr_iid: int, gitlab_host: str,
                  retries: int = 12, delay: int = 5) -> bool:
    """Set an MR to merge when its pipeline succeeds.

    Retries if no pipeline is running yet (GitLab returns 406).
    """
    import time
    encoded = urllib.parse.quote(project_path, safe="")
    for attempt in range(retries):
        result = gitlab_api(
            f"projects/{encoded}/merge_requests/{mr_iid}/merge",
            gitlab_host,
            method="PUT",
            fields={"merge_when_pipeline_succeeds": "true"},
        )
        if result.returncode == 0:
            return True
        if attempt < retries - 1:
            time.sleep(delay)
    return False


def merge_mr(project_path: str, mr_iid: int, gitlab_host: str, retries: int = 5) -> bool:
    """Attempt to merge an MR, retrying on failure."""
    import time
    encoded = urllib.parse.quote(project_path, safe="")
    for attempt in range(retries):
        result = gitlab_api(
            f"projects/{encoded}/merge_requests/{mr_iid}/merge",
            gitlab_host,
            method="PUT",
        )
        if result.returncode == 0:
            return True
        if attempt < retries - 1:
            time.sleep(5)
    return False


def list_namespace_projects(namespace: str, gitlab_host: str) -> list[dict]:
    """List all projects in a GitLab group/namespace as full project dicts."""
    encoded = urllib.parse.quote(namespace, safe="")
    projects = []
    page = 1
    while True:
        result = gitlab_api(
            f"groups/{encoded}/projects?per_page=100&page={page}&include_subgroups=false",
            gitlab_host,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"Could not list projects in namespace '{namespace}': {result.stderr}"
            )
        batch = json.loads(result.stdout)
        if not batch:
            break
        projects.extend(batch)
        page += 1
    return projects
